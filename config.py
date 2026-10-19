#!/usr/bin/env python3
"""Per-repo runner config (`agents/runner.toml`) and the prod guard.

Usage: python3 config.py check <runner.toml>             (prints `ok`, or one problem per line and exits 1)
       python3 config.py get <runner.toml> <dotted.key>  (prints the value; a list prints one item per line)

A repo describes itself in `agents/runner.toml`: base branch, merge method, validate commands, risk-gate patterns and
preprod commands. `load` checks every field and refuses any preprod command that could touch prod, so no caller can
hold a `RunnerConfig` with a prod command in it. Standard library only (Python 3.11+ for `tomllib`).
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MERGE_METHODS = ("rebase", "merge", "squash")
NAME_RE = re.compile(r"^[a-z0-9-]+$")
GITHUB_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# A preprod command matching any of these could deploy, tag or restart prod (design D5).
PROD_PATTERNS = [
    r"deploy-prod",
    r"tag-prod",
    r"strelitzia-deploy",
    r"/srv/[^\s]*-prod\b",
    r"-p engineerfamily(\s|$)",
    r"engineerfamily-prod",
]


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Repo:
    name: str
    github: str
    base: str
    merge: str
    plans: str = "agents/plans"


@dataclass(frozen=True)
class Validate:
    fast: str
    full: str


@dataclass(frozen=True)
class Gate:
    high_risk: list[str]
    doc_or_test: list[str]
    code_line_budget: int = 250
    tests_dir: str = "tests/"


@dataclass(frozen=True)
class Health:
    url: str
    status: int | None = None
    json_ok: bool = False


@dataclass(frozen=True)
class Preprod:
    deploy: str
    release: str | None = None
    seed: str | None = None
    health: list[Health] = field(default_factory=list)


@dataclass(frozen=True)
class RunnerConfig:
    repo: Repo
    validate: Validate
    gate: Gate
    preprod: Preprod | None = None


class _Reader:
    """Reads typed fields from a TOML table and collects every problem instead of stopping at the first."""

    def __init__(self, problems: list[str]):
        self.problems = problems

    def table(self, data: dict, key: str, required: bool = True) -> dict | None:
        value = data.get(key)
        if value is None:
            if required:
                self.problems.append(f"missing [{key}]")
            return None
        if not isinstance(value, dict):
            self.problems.append(f"{key} must be a table")
            return None
        return value

    def get(self, data: dict, prefix: str, key: str, kind: type, default=..., required: bool = True):
        if key not in data:
            if default is not ...:
                return default
            if required:
                self.problems.append(f"missing {prefix}.{key}")
            return None
        value = data[key]
        ok = isinstance(value, kind) and not (kind is int and isinstance(value, bool))
        if kind is list:
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        if not ok:
            want = {list: "a list of strings", str: "a string", int: "an integer", bool: "true or false"}[kind]
            self.problems.append(f"{prefix}.{key} must be {want}")
            return None
        return value


def _parse(data: dict) -> tuple[RunnerConfig | None, list[str]]:
    problems: list[str] = []
    r = _Reader(problems)

    repo_t = r.table(data, "repo") or {}
    name = r.get(repo_t, "repo", "name", str)
    github = r.get(repo_t, "repo", "github", str)
    base = r.get(repo_t, "repo", "base", str)
    merge = r.get(repo_t, "repo", "merge", str)
    plans = r.get(repo_t, "repo", "plans", str, default="agents/plans")
    if name is not None and not NAME_RE.match(name):
        problems.append(f"repo.name {name!r} must match [a-z0-9-]+")
    if github is not None and not GITHUB_RE.match(github):
        problems.append(f"repo.github {github!r} must be owner/name")
    if merge is not None and merge not in MERGE_METHODS:
        problems.append(f"repo.merge {merge!r} must be one of {', '.join(MERGE_METHODS)}")

    val_t = r.table(data, "validate") or {}
    fast = r.get(val_t, "validate", "fast", str)
    full = r.get(val_t, "validate", "full", str)

    gate_t = r.table(data, "gate") or {}
    high_risk = r.get(gate_t, "gate", "high_risk", list)
    doc_or_test = r.get(gate_t, "gate", "doc_or_test", list)
    budget = r.get(gate_t, "gate", "code_line_budget", int, default=250)
    tests_dir = r.get(gate_t, "gate", "tests_dir", str, default="tests/")

    preprod = None
    pre_t = r.table(data, "preprod", required=False)
    if pre_t is not None:
        deploy = r.get(pre_t, "preprod", "deploy", str)
        release = r.get(pre_t, "preprod", "release", str, default=None)
        seed = r.get(pre_t, "preprod", "seed", str, default=None)
        checks: list[Health] = []
        raw = pre_t.get("health", [])
        if not isinstance(raw, list):
            problems.append("preprod.health must be a list of tables")
            raw = []
        for i, h in enumerate(raw):
            where = f"preprod.health[{i}]"
            if not isinstance(h, dict):
                problems.append(f"{where} must be a table")
                continue
            url = r.get(h, where, "url", str)
            status = r.get(h, where, "status", int, default=None)
            json_ok = r.get(h, where, "json_ok", bool, default=False)
            if status is None and not json_ok:
                status = 200
            if url is not None:
                checks.append(Health(url=url, status=status, json_ok=bool(json_ok)))
        if deploy is not None:
            preprod = Preprod(deploy=deploy, release=release, seed=seed, health=checks)

    if problems:
        return None, problems
    cfg = RunnerConfig(
        repo=Repo(name=name, github=github, base=base, merge=merge, plans=plans),
        validate=Validate(fast=fast, full=full),
        gate=Gate(high_risk=high_risk, doc_or_test=doc_or_test, code_line_budget=budget, tests_dir=tests_dir),
        preprod=preprod,
    )
    return cfg, prod_problems(cfg)


def prod_problems(cfg: RunnerConfig) -> list[str]:
    """Every [preprod] command that mentions a prod target, project or checkout."""
    if cfg.preprod is None:
        return []
    problems = []
    for key in ("deploy", "release", "seed"):
        command = getattr(cfg.preprod, key)
        for pattern in PROD_PATTERNS:
            if command and re.search(pattern, command):
                problems.append(f"preprod.{key} looks like a prod command (matches {pattern!r})")
    return problems


def load(path: Path) -> RunnerConfig:
    try:
        data = tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError([f"can't read {path}: {exc}"]) from exc
    cfg, problems = _parse(data)
    if problems:
        raise ConfigError(problems)
    return cfg


def get(cfg: RunnerConfig, dotted: str):
    value = cfg
    for part in dotted.split("."):
        if value is None or not hasattr(value, part):
            raise KeyError(dotted)
        value = getattr(value, part)
    return value


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "check":
        try:
            load(Path(argv[1]))
        except ConfigError as exc:
            print("\n".join(exc.problems))
            return 1
        print("ok")
        return 0
    if len(argv) == 3 and argv[0] == "get":
        try:
            value = get(load(Path(argv[1])), argv[2])
        except ConfigError as exc:
            print("\n".join(exc.problems), file=sys.stderr)
            return 1
        except KeyError:
            print(f"no such key: {argv[2]}", file=sys.stderr)
            return 2
        if isinstance(value, list):
            print("\n".join(str(v) for v in value))
        elif value is not None:
            print(value)
        return 0
    print("usage: config.py check <runner.toml> | get <runner.toml> <dotted.key>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
