# Plan: the runner finishes its own work (CI without check access, preprod after merge, PRs merged by hand)
**Status:** ❌ Not started
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19)
**Depends on:** runner-8-self-host (merged); the cutover (done 2026-10-19)

## Goal
The user wants every queued plan built, merged and put on preprod with no manual step. Three gaps seen on 2026-10-19 stop that:
1. `gh pr checks` fails on agent-runner itself ("GraphQL: Resource not accessible by personal access token … statusCheckRollup"):
   the token can read Actions runs there but not the check rollup. #64 had green CI and was blocked as "CI is red or timed out".
2. A `low` merge doesn't update preprod. Preprod only moves when a `review` PR holds the lock, so merged work sits off preprod.
3. A PR the user merged by hand (engineerfamily #51) still went through the validator review and preprod stages.

## Context
- `run.sh merge_pr` (`grep -n 'merge_pr()' run.sh`): `gh pr checks --watch --fail-fast`; exit 1 = red/timeout, 2 = no checks.
- `gh run list --repo R --commit <sha> --json databaseId,status,conclusion,name` and `gh run watch <id> --exit-status` work with the
  same token on all three repos (checked 2026-10-19).
- `preprod.sh release <repo>` drops the lock and redeploys `<repo>`'s base; `preprod.sh status` prints the holder or `preprod: free`.
  Repos with no `[preprod]` (agent-runner) exit 3 from deploy/release.
- Review stage: `grep -n 'oldest awaiting-review' run.sh`; preprod stage: `preprod_stage()`. Both read `pr` from the queue item.

## Tasks
### Task 1: CI fallback through Actions runs
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify)
**What:** In `merge_pr`, when `gh pr checks` output contains `Resource not accessible`, fall back: take the PR head sha
(`gh pr view --json headRefOid`), wait up to 30 min for `gh run list --commit <sha>` to list at least one run (poll every 30 s), then
`gh run watch <id> --exit-status` for each run. All succeed → merge as today; any failure → return 1; no run after 30 min → return 2.
**Validate:** `pytest -q tests/test_run.py` with a stub `gh`: permission error + green runs → merged; permission error + one failed
run → blocked with "CI is red or timed out"; permission error + no runs → "no CI checks".
**Status:** ✅ 2026-10-19

### Task 2: put preprod on the base after every merge
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify)
**What:** After a successful merge (low path and validator path), if the repo's `runner.toml` has `[preprod]` and `preprod.sh status`
says free (or the lock is this PR's, as today), run `preprod.sh release <repo>` so preprod runs the new base. If the lock is held by
another PR, leave it: that PR's release redeploys the base later. Log one line to `runs.log` either way. A failed redeploy sends a
notice and doesn't change the item's status.
**Validate:** stub `preprod.sh` records calls: low merge with lock free → `release <repo>`; lock held by another PR → no call;
repo without `[preprod]` → no call.
**Status:** ✅ 2026-10-19

### Task 3: PRs merged or closed by hand
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify)
**What:** Before the review stage and the preprod stage act on an item, read `gh pr view <pr> --json state`. `MERGED` → finish the
item `done` with summary "merged by hand", release preprod if this PR holds it (Task 2 puts the base back), no validator run.
`CLOSED` → cancel the item with that summary. The inbox sweep closes its notices.
**Validate:** stub `gh` returns `MERGED` for an `awaiting-review` item → status `done`, no `claude` call; `CLOSED` → `cancelled`.
**Status:** ✅ 2026-10-19

### Task 4: docs
**Runs:** unattended
**Files:** README.md (modify), agents/ROADMAP.md (modify)
**What:** README: the CI fallback, preprod follows each merge, hand merges are recognised. ROADMAP: row A16 ✅ with the PR number.
**Validate:** `scripts/validate.sh` passes.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes; CI is green.

## Human test steps (preprod)
None (low risk). After the next install, the next agent-runner PR merges on its own, and `agent-runner preprod status` is free with
preprod on each repo's latest base after a merge.

## Decisions
1. Keep `gh pr checks` as the first choice: it is the only call that also sees non-Actions checks.
2. Redeploying the base after merge replaces the old Strelitzia runner's "merge, then redeploy preprod" (it never moved to the new runner).
