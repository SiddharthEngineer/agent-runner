# Plan: drop the Strelitzia-era fallbacks from the runner
**Status:** ❌ Not started
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19)
**Depends on:** strelitzia-runner-cleanup (Strelitzia repo). The old dirs (AM6) may still exist: nothing reads them after this plan

## Goal
After a week on the new runner with no trouble (design Rollout step 7), the runner stops reading the old
`~/.config/strelitzia-agent/env` and the `STRELITZIA_AGENT_STATE` alias, and its Strelitzia-only defaults go away. Everything comes from
`~/.config/agent-runner/` and each repo's `runner.toml`.

## Context
- Task 0 check: strelitzia-runner-cleanup is merged (`scripts/agent/` is gone from Strelitzia `origin/trunk`), and `crontab -l` has
  no active `scripts/agent/run.sh` line (a commented one is fine). If either fails, write `blocked` naming it and stop. Whether the old
  dirs still exist doesn't matter (user 2026-10-19: no manual step before this runs).
- `grep -rn 'strelitzia-agent\|STRELITZIA_AGENT_STATE\|AGENT_DEFAULT_REPO\|applypilot' --include=*.py --include=*.sh .` lists every site.
- `config.py env_file()` (runner-1-config): the fallback. `buildqueue.py` STATE alias and `DEFAULT_REPO` (runner-3-state).
  `gate.py`'s no-`--config` defaults (runner-1-config Decision 3). `health.py` (both state dirs). `inbox.py` legacy markers.

## Tasks
### Task 1: remove the fallbacks
**Runs:** unattended
**Files:** config.py (modify), buildqueue.py (modify), health.py (modify), inbox.py (modify), tests/test_config.py (modify), tests/test_runner.py (modify)
**What:** `env_file()` reads only `~/.config/agent-runner/env`. `STATE` reads only `AGENT_RUNNER_STATE` or `state_dir()`.
`DEFAULT_REPO` goes: `add` requires `--repo`, and bare keys in `queue.json` now cause an error ("migrate first"). Health cleans and
lock-checks only the new state dir. Keep reading the legacy inbox markers (old issues keep them).
**Validate:** `pytest -q tests` → passes; the fallback tests are replaced by tests that the old paths are **not** read (with
`HOME` holding only an old env file, `env_file()` → `None`).
**Status:** ✅ 2026-10-21 d49cb29

### Task 2: `gate.py` requires `--config`
**Runs:** unattended
**Files:** gate.py (modify), tests/test_runner.py (modify)
**What:** Delete the built-in Strelitzia `HIGH_RISK`/`DOC_OR_TEST`; `--config` becomes required (except `--plan`, which uses only
the global budgets when there's no config). Gate tests use the fixtures.
**Validate:** `python3 gate.py x.md` without `--config` → exit 2 with a usage message; `scripts/validate.sh` → all PASS.
**Status:** ✅ 2026-10-21 (this commit)

## Validation
- `scripts/validate.sh` passes.
- The `grep` from Context returns only the legacy inbox markers and tests.

## Human test steps (preprod)
None (low risk). The user installs the release that contains this (as AM1).

## Decisions
1. Bare queue keys become an error rather than a silent default, since every repo now registers explicitly.
