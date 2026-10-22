# Plan: preprod-only tasks don't fail review, and a failed preprod check stays on preprod
**Status:** ❌ Not started
**Risk:** low (no prod impact; runner programme PRs auto-merge on green CI, user 2026-10-19)
**Depends on:** - (goes live only when the user installs a release: AM7)

## Goal
Two runner problems from engineerfamily PR #63 (2026-10-21), which ended `blocked` without the user ever seeing it on preprod:
1. **Chicken and egg.** The plan had a task "run the layout check on preprod after the runner deploys the PR". The runner
   deploys only after the review validator approves, but `prompts/review-pr.md` step 6 requires every unattended task to be
   `✅`. The validator asked for changes, the implementer couldn't do the task either, and the second attempt blocked the item.
   Fix: at the review stage, a task that can only run on preprod and is `⏭️ skipped` for that reason is not a defect; the
   preprod stage covers it. Plans should put such checks in "Human test steps" (the preprod validator runs those).
2. **Gone before the user looks.** After a preprod-stage `changes` or `blocked` verdict, `run.sh` released preprod at once,
   so the user found preprod on `main`. Fix: keep the PR on preprod (lock held) after either verdict; the notice says so.
   The lock frees itself when the PR is merged or closed (`preprod.sh status`), when the same PR is deployed again after
   its fix, or by hand with `agent-runner preprod release <repo>`.

## Context
- `prompts/review-pr.md` (25 lines): step 6 is the `✅` rule; step 7 the verdicts.
- `run.sh:287-376` `preprod_stage`: the `changes)` branch (≈362) and the `*)` branch (≈372) call `preprod.sh release`; the
  "no verdict twice" path sets `verdict=blocked` first. `approve` merges and calls `preprod_follow` (unchanged).
- `preprod.sh deploy <repo> <pr>` already accepts a lock held by the same `<repo> <pr>`, so a re-queued PR redeploys over
  its own hold. `preprod.sh status` releases the lock when the holder's PR is merged or closed.
- `tests/test_run.py:470-500`: `test_preprod_approve_merges_releases_notifies`, `test_preprod_changes_releases_and_requeues`
  (stubbed `claude`, `gh`, `preprod.sh`; asserts on recorded calls). Mirror their style.
- `README.md` "Validator, review stage" / "Validator, preprod stage" bullets describe the verdict outcomes.

## Tasks
### Task 1: review prompt accepts preprod-only skips
**Runs:** unattended
**Files:** prompts/review-pr.md (modify)
**What:** Step 6: every `**Runs:** unattended` task must be `✅`, except a task that can only run against preprod (its Runs
or What line says so) and is `⏭️ skipped` for that reason: note it in the review as "left for the preprod stage", not as a
defect. Keep the prompt's placeholders (`{repo}`, `{pr}`, `{{…}}`) intact.
**Validate:** `.venv/bin/pytest -q tests/test_review.py` (or `uv run pytest -q tests/test_review.py`) passes.
**Status:** ✅ 2026-10-22 (f586c3d)

### Task 2: keep a failed PR on preprod
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify)
**What:** In `preprod_stage`, `changes` and `blocked`/no-verdict no longer call `preprod.sh release`. `changes` still
re-queues (and still notifies when out of attempts); add to every notice from these two branches: "It stays on preprod
(<base_url>) so you can look. Close the PR, or run `agent-runner preprod release <repo>`, to free preprod." Rename
`test_preprod_changes_releases_and_requeues` to `…_stays_and_requeues` and assert no release call; add a test that
`blocked` keeps the lock and notifies with the release command.
**Validate:** `scripts/validate.sh` passes.
**Status:** ❌

### Task 3: docs and tracking
**Runs:** unattended
**Files:** README.md (modify), agents/ROADMAP.md (modify), agents/plans/runner-13-preprod-holds.md (modify)
**What:** README: the review-stage rule for preprod-only tasks, a line that plans put preprod checks in Human test
steps, and the new `changes`/`blocked` preprod outcomes. ROADMAP: A19 row ✅ with the PR; AM7 stays for the user.
**Validate:** `scripts/validate.sh` passes.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes; CI is green.

## Decisions
1. 2026-10-22 (user): after a failed preprod check, the PR stays on preprod so the user can look at it.
2. Holding the global lock delays other repos' preprod deploys until the PR is fixed, merged, closed or released by hand.
   Accepted: preprod items are rare and the notice names the way out.
