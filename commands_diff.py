#!/usr/bin/env python3
"""How the repos' copies of the PIV commands differ (design D4): `agent-runner check-commands <repo> <repo>…`.

Reads each repo's `.claude/commands/*.md` from `origin/<base>` of its dev clone (`AGENT_REPO` in
~/.config/agent-runner/repos/<repo>.env), replaces the repo-specific tokens (GitHub name, repo name, base branch, validate
commands) with placeholders, and prints `same <command>` or a unified diff per command. Read-only apart from `git fetch`.
Standard library only.
"""

from __future__ import annotations

import difflib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

CLI = re.compile(r"\bagent-runner (?=(queue|gate|inbox|usage|config|health|preprod|run|version|check-commands)\b)")


def git(clone: str, *args: str) -> str:
    return subprocess.run(["git", "-C", clone, *args], check=True, capture_output=True, text=True).stdout


def load(name: str) -> tuple[config.RunnerConfig, dict[str, str]]:
    """The repo's config and its commands (file name → text), both from origin/<base>."""
    clone = config.repo_env(name).get("AGENT_REPO")
    if not clone:
        sys.exit(f"no AGENT_REPO for {name} in {config.config_dir() / 'repos' / (name + '.env')}")
    git(clone, "fetch", "-q", "origin")
    base_ref = git(clone, "rev-parse", "--abbrev-ref", "origin/HEAD").strip()
    with tempfile.NamedTemporaryFile("w", suffix=".toml") as f:
        f.write(git(clone, "show", f"{base_ref}:agents/runner.toml"))
        f.flush()
        cfg = config.load(Path(f.name))
    names = git(clone, "ls-tree", "--name-only", f"origin/{cfg.repo.base}", ".claude/commands/").split()
    commands = {Path(n).name: git(clone, "show", f"origin/{cfg.repo.base}:{n}") for n in names if n.endswith(".md")}
    return cfg, commands


def normalize(text: str, cfg: config.RunnerConfig) -> str:
    text = CLI.sub("@CLI@ ", text)  # the runner's CLI name isn't a repo name, even in agent-runner's own commands
    text = text.replace(cfg.repo.github, "{github}")
    for command, token in sorted(((cfg.validate.full, "{validate_full}"), (cfg.validate.fast, "{validate_fast}")),
                                 key=lambda pair: -len(pair[0])):
        text = text.replace(command, token)
    text = re.sub(rf"\b{re.escape(cfg.repo.base)}\b", "{base}", text)
    text = re.sub(rf"(?<![\w-]){re.escape(cfg.repo.name)}(?![\w-])", "{repo}", text, flags=re.IGNORECASE)
    return text.replace("@CLI@ ", "agent-runner ")


def compare(repos: dict[str, tuple[config.RunnerConfig, dict[str, str]]]) -> list[str]:
    out = []
    names = sorted({c for _, commands in repos.values() for c in commands})
    first = next(iter(repos))
    for command in names:
        have = {r: normalize(cmds[command], cfg) for r, (cfg, cmds) in repos.items() if command in cmds}
        missing = [r for r in repos if r not in have]
        if missing:
            out.append(f"{command}: missing in {', '.join(missing)}")
        texts = list(have.values())
        if all(t == texts[0] for t in texts):
            if not missing:
                out.append(f"same {command}")
            continue
        ref = first if first in have else next(iter(have))
        for other, text in have.items():
            if other != ref and text != have[ref]:
                out += difflib.unified_diff(have[ref].splitlines(), text.splitlines(), f"{ref}/{command}", f"{other}/{command}",
                                            lineterm="")
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: commands_diff.py <repo> <repo> [<repo>…]", file=sys.stderr)
        return 2
    lines = compare({name: load(name) for name in argv})
    print("\n".join(lines))
    return 0 if all(line.startswith("same ") for line in lines) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
