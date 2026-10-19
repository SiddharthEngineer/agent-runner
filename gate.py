#!/usr/bin/env python3
"""Risk gate: may this branch merge to trunk without human review?

Usage: python scripts/agent/gate.py <plan.md> [--base origin/trunk]
       python scripts/agent/gate.py <plan.md> --plan   (no git: checks the plan's **Files:** lines and size, for /plan)

The plan declares `**Risk:** low` or `**Risk:** review` (set with the user in /plan). A `low` plan still goes to
review if the diff touches a high-risk path, is larger than the budget, or weakens tests. Prints JSON
{"tier": "low"|"review", "reasons": [...]} and exits 0 for low, 1 for review, 2 on usage errors.
`--plan` also prints "split": reasons the plan is over the size budget (empty when it fits). It doesn't change the tier.
Standard library only.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

# Any change here needs a human, whatever the plan says.
HIGH_RISK = [
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    ".github/*",
    ".claude/settings.json",
    "scripts/agent/*",
    "scripts/validate.sh",
    "scripts/migrate_to_strelitzia.sh",  # moves the data dirs, rewrites crontab and ~/.ssh/config
    "src/strelitzia/database.py",
    "src/strelitzia/db_pg.py",
    "src/strelitzia/migrate.py",
    "src/strelitzia/config.py",
    "src/strelitzia/web/*",
    "dashboard/*",
    "*password*",
    "*credential*",
    "*secret*",
]
# Changed lines outside docs and tests that a low-risk change may have.
CODE_LINE_BUDGET = 250
DOC_OR_TEST = ["*.md", "agents/*", ".claude/commands/*", ".claude/agents/*", "tests/*"]

# One plan = one unattended run. Past runs: plans touching <=14 files took 19-73 turns; 22-49 files took 100-180 turns
# and most of a 5-hour usage window. Over any of these, /plan splits the plan into smaller ones.
PLAN_FILE_BUDGET = 12
PLAN_TASK_BUDGET = 4
PLAN_BYTE_BUDGET = 10_000

# Lines in a diff of tests/ that mean a test was weakened rather than added.
WEAKENING = [
    (r"^-\s*def test_", "removes a test function"),
    (r"^-\s*assert\b", "removes an assert"),
    (r"^\+.*pytest\.mark\.(skip|xfail)", "adds a skip/xfail marker"),
    (r"^\+.*pytest\.skip\(", "adds pytest.skip()"),
]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def declared_risk(plan: Path) -> str | None:
    m = re.search(r"^\*\*Risk:\*\*\s*(low|review)\b", plan.read_text(), re.MULTILINE | re.IGNORECASE)
    return m.group(1).lower() if m else None


def risk_reasons(plan: Path) -> list[str]:
    risk = declared_risk(plan)
    return [] if risk == "low" else [f"plan declares risk {risk or 'missing'}"]


def planned_files(plan: Path) -> list[str]:
    """Paths from every `**Files:** path (new|modify), path (modify)` field, in order, without duplicates.

    A field runs until the next `**Field:**`, heading or blank line, so wrapped lines and `- path` bullets count too.
    """
    fields: list[str] = []
    for line in plan.read_text().splitlines():
        if line.startswith("**Files:**"):
            fields.append(line.removeprefix("**Files:**"))
        elif fields and fields[-1] is not None and line.strip() and not line.startswith(("**", "#")):
            fields[-1] += "," + line.strip().removeprefix("- ")
        elif fields:
            fields.append(None)  # field ended
    files: list[str] = []
    for field in filter(None, fields):
        for entry in re.split(r"[,;]", re.sub(r"\([^)]*\)", "", field)):
            path = entry.strip().strip("`").strip()
            if path and path not in files:
                files.append(path)
    return files


def split_reasons(plan: Path, files: list[str]) -> list[str]:
    text = plan.read_text()
    tasks = len(re.findall(r"^### Task\b", text, re.MULTILINE))
    size = len(text.encode())
    reasons = []
    if len(files) > PLAN_FILE_BUDGET:
        reasons.append(f"{len(files)} planned files > budget {PLAN_FILE_BUDGET}")
    if tasks > PLAN_TASK_BUDGET:
        reasons.append(f"{tasks} tasks > budget {PLAN_TASK_BUDGET}")
    if size > PLAN_BYTE_BUDGET:
        reasons.append(f"{size} bytes > budget {PLAN_BYTE_BUDGET}")
    return reasons


def evaluate_plan(plan: Path) -> dict:
    files = planned_files(plan)
    reasons = risk_reasons(plan)
    reasons += [f"touches high-risk path {path}" for path in files if matches(path, HIGH_RISK)]
    return {"tier": "review" if reasons else "low", "reasons": reasons, "split": split_reasons(plan, files)}


def evaluate(plan: Path, base: str) -> dict:
    reasons = risk_reasons(plan)

    merge_base = git("merge-base", base, "HEAD").strip()
    code_lines = 0
    for line in git("diff", "--numstat", merge_base, "HEAD").splitlines():
        added, deleted, path = line.split("\t", 2)
        if matches(path, HIGH_RISK):
            reasons.append(f"touches high-risk path {path}")
        if not matches(path, DOC_OR_TEST) and added != "-":
            code_lines += int(added) + int(deleted)
    if code_lines > CODE_LINE_BUDGET:
        reasons.append(f"{code_lines} changed code lines > budget {CODE_LINE_BUDGET}")

    test_diff = git("diff", "-U0", merge_base, "HEAD", "--", "tests/")
    for pattern, why in WEAKENING:
        if re.search(pattern, test_diff, re.MULTILINE):
            reasons.append(f"test diff {why}")

    return {"tier": "review" if reasons else "low", "reasons": reasons, "code_lines": code_lines}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plan", type=Path)
    parser.add_argument("--base", default="origin/trunk")
    parser.add_argument("--plan", dest="plan_only", action="store_true", help="check the plan's declared files instead of the diff")
    args = parser.parse_args()
    if not args.plan.is_file():
        print(f"no such plan: {args.plan}", file=sys.stderr)
        return 2
    result = evaluate_plan(args.plan) if args.plan_only else evaluate(args.plan, args.base)
    print(json.dumps(result, indent=2))
    return 0 if result["tier"] == "low" else 1


if __name__ == "__main__":
    sys.exit(main())
