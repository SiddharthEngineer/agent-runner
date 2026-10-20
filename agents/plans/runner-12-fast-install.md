# Plan: `install.sh` skips the test suite when CI already passed on the tag
**Status:** ✅ Done 2026-10-19 (all 3 tasks)
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19)
**Depends on:** runner-11-autopilot (merged)

## Goal
`install.sh v0.2.2` took about 2.5 minutes on the VPS, almost all of it the 160-test pytest run inside `scripts/validate.sh`
(the `tests/test_run.py` cases spawn `run.sh` with stubs, ~4 s each, on 2 vCPUs). CI runs the same `scripts/validate.sh` on every push
to `main`, and a tag is cut from `main`, so the tag's commit usually has a green CI run already. Install should take seconds then,
and still run the full suite when CI can't vouch for the commit.

## Context
- `install.sh`: mirror fetch, `worktree add`, then `"$dir/scripts/validate.sh"` must pass before `current` moves.
- `scripts/validate.sh`: steps `bash -n`, `py_compile`, `pytest` (via `uv run --with pytest`).
- `.github/workflows/ci.yml`: workflow `CI`, runs `scripts/validate.sh` on `pull_request` and `push` to `main`.
- `gh run list --repo SiddharthEngineer/agent-runner --commit <sha> --workflow CI --json conclusion,status` works with the VPS token
  (the repo is public since 2026-10-19).
- `tests/test_install.py`: the existing install tests (stub the mirror and `validate.sh`).

## Tasks
### Task 1: `validate.sh --quick`
**Runs:** unattended
**Files:** scripts/validate.sh (modify), tests/test_install.py (modify)
**What:** `scripts/validate.sh --quick` runs only `bash -n` and `py_compile`. No flag = today's behaviour (CI and agents unchanged).
**Validate:** `scripts/validate.sh --quick` prints two PASS lines and no pytest line; `scripts/validate.sh` still runs all three.
**Status:** ✅ 2026-10-19

### Task 2: install trusts green CI on the tag's commit
**Runs:** unattended
**Files:** install.sh (modify), tests/test_install.py (modify), README.md (modify)
**What:** After `worktree add`, resolve the tag's commit sha. If `gh` is available and
`gh run list --repo <owner/name from REMOTE> --commit <sha> --workflow CI` has a completed run with conclusion `success`, run
`validate.sh --quick` and print `CI passed on <short sha>; ran the quick checks only`. Otherwise (no `gh`, no run, still running,
failed, or any `gh` error) run the full `validate.sh` as today and print why. A new `--full` flag (`./install.sh --full <tag>`) always
runs the full suite. Usage text and README mention both. Keep the 3-release pruning and atomic relink as they are.
**Validate:** `pytest -q tests/test_install.py` with a stub `gh` on PATH: green run → quick checks only; failed run / no run / `gh`
exits non-zero → full suite; `--full` with a green run → full suite. `scripts/validate.sh` passes.
**Status:** ✅ 2026-10-19

### Task 3: tracking
**Runs:** unattended
**Files:** agents/ROADMAP.md (modify)
**What:** Row A17 ✅ with the PR number.
**Validate:** `scripts/validate.sh` passes.
**Status:** ✅ 2026-10-19

## Validation
- `scripts/validate.sh` passes; CI is green.

## Human test steps (preprod)
None (low risk). After the next tag (pushed from `main` once its CI run is green): `~/src/agent-runner/install.sh <tag>` finishes in
seconds and says CI passed; `install.sh --full <tag>` runs the whole suite.

## Decisions
1. CI's run on the exact commit is the evidence; a green run on a different commit doesn't count.
2. Anything uncertain falls back to the full suite, so a slow install is the worst case, never an untested one.
3. Every CI run on the commit must have succeeded (a failed run beside a green one means the full suite); stricter than Task 2 first said, per Decision 2 (self-review, 2026-10-19).
4. The CI question is about the commit checked out in releases/<tag>; a reused release whose HEAD is not the tag's commit, or with tracked local changes, gets the full suite (validator, 2026-10-20).
