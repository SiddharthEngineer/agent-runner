#!/usr/bin/env python3
"""Queue intake: plans pushed to a repo's base branch that say `**Queue:** yes` get queued without `queue add`.

Usage: python3 intake.py scan [--repo NAME] [--dry-run]

For each registered repo (`~/.config/agent-runner/repos/<name>.env`, or just `--repo`) whose runner.toml on
origin/<base> has `[intake] enabled = true`, reads `<plans>/*.md` (top level) from origin/<base>, never the working tree
or a PR branch. A plan opts in with a header line (before the first `## `):

    **Queue:** yes|no [at 2026-10-27T02:00Z] [after <repo>/<slug>|<slug>]

Intake once, local wins: a key already in the queue is the CLI's. The only thing intake does to it is cancel an item
that `**Queue:** no` now, if intake added it and it hasn't started. A new `yes` plan is checked (not done, a risk line,
an unattended task left, within the size budget, `at` with a timezone, `after` known); a refused plan gets one notice
per plan version (blob), remembered in $AGENT_RUNNER_STATE/intake.json. `--dry-run` prints the same lines and writes
nothing. Exit 0 unless the queue file is unreadable. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config  # noqa: E402
import gate  # noqa: E402

QUEUE_RE = re.compile(r"^\*\*Queue:\*\*[ \t]*(\S*)(.*)$", re.MULTILINE)
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass
class QueueLine:
    queue: bool
    at: str | None = None
    after: str | None = None


@dataclass
class Plan:
    repo: str
    slug: str
    path: str  # <plans>/<slug>.md
    blob: str
    sha: str  # origin/<base> commit it was read from
    text: str
    line: QueueLine
    rules: gate.Rules

    @property
    def key(self) -> str:
        return f"{self.repo}/{self.slug}"


def header(text: str) -> str:
    """The plan up to its first `## ` heading."""
    m = re.search(r"^## ", text, re.MULTILINE)
    return text[: m.start()] if m else text


def parse_queue_line(text: str) -> QueueLine | None:
    """The header's `**Queue:**` line, or None. Raises ValueError on a line that doesn't follow the grammar."""
    m = QUEUE_RE.search(header(text))
    if m is None:
        return None
    word, words = m.group(1).lower(), m.group(2).split()
    if word not in ("yes", "no"):
        raise ValueError(f"**Queue:** must be yes or no, not {m.group(1)!r}")
    line = QueueLine(queue=word == "yes")
    while words:
        if len(words) < 2 or words[0] not in ("at", "after") or getattr(line, words[0]) is not None:
            raise ValueError(f"can't read **Queue:** line: {m.group(0).strip()!r}")
        setattr(line, words[0], words[1])
        words = words[2:]
    if line.after is not None:
        repo, _, slug = line.after.rpartition("/")
        if (repo and not config.NAME_RE.match(repo)) or not SLUG_RE.match(slug):
            raise ValueError(f"after {line.after!r} must be <repo>/<slug> or <slug>")
    return line


def field(text: str, name: str) -> str | None:
    m = re.search(rf"^\*\*{name}:\*\*[ \t]*(.*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def runnable_tasks(text: str) -> int:
    """`### Task` sections with `**Runs:** unattended` whose `**Status:**` isn't ✅."""
    count = 0
    for section in re.split(r"^(?=### Task\b)", text, flags=re.MULTILINE)[1:]:
        section = re.split(r"^#{1,3} (?!Task\b)", section, maxsplit=1, flags=re.MULTILINE)[0]
        runs, status = field(section, "Runs"), field(section, "Status") or ""
        if runs and runs.lower().startswith("unattended") and not status.startswith("✅"):
            count += 1
    return count


def problems(plan: Plan, known: set[str]) -> list[str]:
    """Why a new `yes` plan can't be queued; `known` is every key in the queue (including this scan's)."""
    reasons = []
    with tempfile.NamedTemporaryFile("w", suffix=".md") as f:
        f.write(plan.text)
        f.flush()
        if gate.declared_risk(Path(f.name)) is None:
            reasons.append("no **Risk:** line")
        reasons += gate.evaluate_plan(Path(f.name), plan.rules)["split"]
    if runnable_tasks(plan.text) == 0:
        reasons.append("no unattended task left to run")
    if plan.line.at is not None:
        try:
            if datetime.fromisoformat(plan.line.at).tzinfo is None:
                reasons.append(f"at {plan.line.at} has no timezone (add Z or an offset)")
        except ValueError:
            reasons.append(f"at {plan.line.at!r} is not an ISO time")
    after = after_key(plan)
    if after is not None and after not in known:
        reasons.append(f"after {after} is not queued")
    return reasons


def after_key(plan: Plan) -> str | None:
    after = plan.line.after
    return None if after is None else after if "/" in after else f"{plan.repo}/{after}"


FETCH_TIMEOUT = 120  # seconds: a hung remote must not hold the tick


def git(clone: Path, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(clone), *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")


def runner_config(clone: Path, ref: str) -> config.RunnerConfig | None:
    """`agents/runner.toml` at `ref`, or None when it's missing or refused (run.sh reports a bad one)."""
    toml = git(clone, "show", f"{ref}:agents/runner.toml")
    if toml.returncode != 0:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".toml") as f:
        f.write(toml.stdout)
        f.flush()
        try:
            return config.load(Path(f.name))
        except config.ConfigError:
            return None


Refusal = tuple[str, str, str, list[str], str]  # key, repo, blob, reasons, plan path


def repo_plans(name: str) -> tuple[list[Plan], list[Refusal]]:
    """(plans with a `**Queue:**` line on origin/<base>, refusals for unreadable lines) for an opted-in repo."""
    env = config.repo_env(name)
    clone = env.get("AGENT_REPO")
    if not clone or not Path(clone).is_dir():
        return [], []
    clone = Path(clone)
    fetched = git(clone, "fetch", "-q", "origin", timeout=FETCH_TIMEOUT)
    probe = git(clone, "rev-parse", "--abbrev-ref", "origin/HEAD")
    head = probe.stdout.strip() if probe.returncode == 0 else f"origin/{env.get('AGENT_BASE') or 'main'}"
    cfg = runner_config(clone, head)
    if cfg is not None and f"origin/{cfg.repo.base}" != head:
        cfg = runner_config(clone, f"origin/{cfg.repo.base}")  # the base branch's own runner.toml decides
    if cfg is None or cfg.intake is None or not cfg.intake.enabled:
        return [], []
    if fetched.returncode != 0:
        err = fetched.stderr.strip().splitlines()
        print(f"intake: {name}: git fetch failed: {err[-1] if err else f'exit {fetched.returncode}'}")
        return [], []
    base = f"origin/{cfg.repo.base}"
    sha = git(clone, "rev-parse", base).stdout.strip()
    rules = gate.Rules(cfg.gate.high_risk, cfg.gate.doc_or_test, cfg.gate.code_line_budget, cfg.gate.tests_dir)
    plans_dir = cfg.repo.plans.rstrip("/")
    plans: list[Plan] = []
    bad: list[Refusal] = []
    for entry in git(clone, "ls-tree", base, f"{plans_dir}/").stdout.splitlines():
        meta, _, path = entry.partition("\t")
        _, kind, blob = meta.split()
        slug = path.rpartition("/")[2].removesuffix(".md")
        if kind != "blob" or not path.endswith(".md") or not SLUG_RE.match(slug):
            continue
        text = git(clone, "cat-file", "blob", blob).stdout
        if (field(header(text), "Status") or "").startswith("✅"):
            continue
        try:
            line = parse_queue_line(text)
        except ValueError as exc:
            bad.append((f"{name}/{slug}", name, blob, [str(exc)], path))
            continue
        if line is not None:
            plans.append(Plan(name, slug, path, blob, sha, text, line, rules))
    return plans, bad


def notice(key: str, repo: str, reasons: list[str], plan_path: str) -> bool:
    """Send the refusal notice. False when inbox.py failed (try again next scan); True when sent or there's no inbox."""
    if not os.environ.get("AGENT_INBOX_REPO"):
        return True
    body = (f"Intake didn't queue `{plan_path}` from origin:\n\n" + "\n".join(f"- {r}" for r in reasons)
            + "\n\nFix the plan and push it (a new version is checked again), or queue it by hand.")
    sent = subprocess.run([sys.executable, str(HERE / "inbox.py"), "notify", "--slug", key, "--project", repo,
                           "--title", f"not queued: {'; '.join(reasons)}", "--body", body],
                          capture_output=True, text=True)
    if sent.returncode != 0:
        err = sent.stderr.strip().splitlines()
        print(f"intake: notice for {key} failed: {err[-1] if err else f'exit {sent.returncode}'}")
    return sent.returncode == 0


def scan(names: list[str], dry_run: bool) -> int:
    import buildqueue  # here, after main's load_env: it reads AGENT_RUNNER_STATE on import

    plans: list[Plan] = []
    bad: list[Refusal] = []
    for name in names:  # every fetch first, so the queue is read and written in one short step
        found, unreadable = repo_plans(name)
        plans += found
        bad += unreadable
    try:
        data = buildqueue.load()
    except (OSError, ValueError, KeyError, SystemExit) as exc:
        print(f"intake: can't read {buildqueue.QUEUE}: {exc}")
        return 1
    seen_path = config.state_dir() / "intake.json"
    try:
        seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    except (OSError, ValueError):
        seen = {}
    refusals = [r for r in bad if r[0] not in data["items"]]
    changed = False

    for plan in plans:  # intake once, local wins
        it = data["items"].get(plan.key)
        if it is not None and not plan.line.queue and it["status"] == "queued" and it.get("source") == "intake" \
                and it["attempts"] == 0:
            it.update(status="cancelled", updated=buildqueue.iso(buildqueue.now()))
            print(f"intake: cancelled {plan.key} (**Queue:** no)")
            changed = True

    pending = [p for p in plans if p.line.queue and p.key not in data["items"]]
    pending.sort(key=lambda p: p.line.after is not None)  # after-free plans first
    while pending:  # an `after` may point at a plan queued later in this scan: retry until nothing moves
        known = set(data["items"])
        ready = [p for p in pending if after_key(p) is None or after_key(p) in known]
        if not ready:
            ready = pending  # what's left can't be satisfied: refuse it
        for plan in ready:
            pending.remove(plan)
            reasons = problems(plan, set(data["items"]))
            if reasons:
                refusals.append((plan.key, plan.repo, plan.blob, reasons, plan.path))
                continue
            now = buildqueue.iso(buildqueue.now())
            after = after_key(plan)
            data["items"][plan.key] = {
                "repo": plan.repo,
                "slug": plan.slug,
                "plan": plan.path,
                "status": "queued",
                "not_before": buildqueue.parse_when(plan.line.at, None),
                "attempts": 0,
                "notes": [f"queued by intake from {plan.sha[:12]}"],
                **({"after": after} if after else {}),
                "source": "intake",
                "plan_blob": plan.blob,
                "added": now,
                "updated": now,
            }
            seen.pop(plan.key, None)
            after_text = f", after {after}" if after else ""
            print(f"intake: queued {plan.key} (not before {data['items'][plan.key]['not_before']}{after_text})")
            changed = True

    for key, repo, blob, reasons, path in refusals:
        if seen.get(key) == blob:
            continue
        print(f"intake: refused {key}: {'; '.join(reasons)}")
        if not dry_run and notice(key, repo, reasons, path):
            seen[key] = blob
            changed = True

    if changed and not dry_run:
        buildqueue.save(data)
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = seen_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(seen, indent=2, sort_keys=True) + "\n")
        tmp.replace(seen_path)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="intake.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="queue opted-in plans from origin/<base> of every registered repo")
    p.add_argument("--repo", help="just this registered repo")
    p.add_argument("--dry-run", action="store_true", help="print what would happen; write nothing")
    args = parser.parse_args(argv)
    config.load_env()
    if args.repo is not None:
        if not config.NAME_RE.match(args.repo):
            print(f"bad repo name: {args.repo}", file=sys.stderr)
            return 2
        names = [args.repo]
    else:
        names = sorted(p.stem for p in (config.config_dir() / "repos").glob("*.env") if config.NAME_RE.match(p.stem))
    return scan(names, args.dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
