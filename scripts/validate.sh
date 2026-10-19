#!/usr/bin/env bash
# One validation gate for humans, agents and CI. Run from the repo root (or any worktree).
#
#   scripts/validate.sh    # bash -n on every script, compile every module, hermetic pytest
#
# Prints one PASS/FAIL line per step and exits non-zero on the first failure. The runner is stdlib-only;
# pytest comes from uv (`uv run --with pytest`), so no venv is needed.
set -uo pipefail
cd "$(dirname "$0")/.."

step() {  # step <name> <command...>
    local name=$1; shift
    local out
    if out=$("$@" 2>&1); then
        echo "PASS  $name"
    else
        echo "FAIL  $name"
        echo "$out" | tail -n 60
        exit 1
    fi
}

step "bash -n" bash -c 'for f in *.sh scripts/*.sh; do bash -n "$f" || exit 1; done'
step "py_compile" python3 -m py_compile ./*.py
step "pytest" uv run --quiet --no-project --with pytest pytest -q tests
