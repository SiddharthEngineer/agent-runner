# Plan: Conflicting PRs: catch them before CI, merge the base, or send the implementer back
**Status:** ❌ Not started
**Risk:** low (run.sh merge path and queue bookkeeping; goes live only on install; runner programme PRs auto-merge on green CI, user 2026-10-19)
**Depends on:** intake-2-wiring (both edit run.sh and tests/test_run.py)

## Goal
GitHub runs no `pull_request` CI on a PR that conflicts with its base, so today the runner waits 30 minutes, says
"no CI checks on this repo" and blocks the item (agent-runner #127 on 2026-10-26: a ROADMAP conflict). After this plan:
1. Before waiting for CI, the runner checks whether the PR conflicts with `origin/<base>`.
2. If it does, the runner merges `origin/<base>` into the PR branch itself. A clean merge is pushed (a normal push, never forced),
   CI then runs on the new head and the item carries on.
3. If the merge has real conflicts, the item goes back to the implementer for a **conflict round**: the runner starts the merge in the
   item's worktree and the implementer resolves the markers, validates, commits and pushes. A conflict round doesn't use an attempt;
   at most 2 per item (`AGENT_MAX_CONFLICT_ROUNDS`), then the item is blocked.
4. Notices and PR comments name the real cause: "conflicts with main in agents/ROADMAP.md", never "no CI checks".

## Context
- `run.sh:257-283` `merge_pr` (exit codes 1 red/timeout, 2 no checks, 3 merge failed) and `ci_runs` (waits `AGENT_CI_WAIT` for Actions runs).
- `run.sh:299-306` `merge_failure`: the notice text per exit code.
- `merge_pr` callers: implementer `ready` path (`run.sh:≈648`, low tier), `review_stage` approve (`≈490`), `preprod_stage` approve (`≈410`).
  The `ready` path also runs `gate.py` first (`≈641`); check for conflicts before the gate, so the validator always reviews a mergeable PR.
- `run.sh:≈524-534`: the item worktree `worktrees/<repo>/<slug>` is reused if present, else made from `origin/agent/<slug>` (or the base).
- `run.sh:≈549-560`: the implementer prompt and the `resume` logic; a conflict round uses its own prompt (below), not `/implement` and not a resume.
- `buildqueue.py:268-286` `cmd_requeue` (notes, `resume`); `:190-199` `cmd_start` adds an attempt. run.sh's usage-limit path
  (`≈616-624`) gives an attempt back the same way a conflict round will (`attempts - 1`, floor 0).
- `agents/settings/implement.json` allows `git add`, `git commit`, `git fetch`, `git push origin HEAD`; it denies forced pushes. So
  the runner, not the implementer, runs `git merge`.
- `preprod.sh release <repo>`: a PR sent back for a conflict round must not keep holding the preprod lock.
- Tests: `tests/test_run.py` drives run.sh with stub `gh`/`claude` on PATH and temp git repos; mirror the `merge_pr` tests and add a
  `mergeable` field to the `gh pr view` stub. `tests/test_runner.py` for buildqueue.
- Gotcha: `gh pr view --json mergeable` is `UNKNOWN` until GitHub has computed it; poll up to 6 × 10 s, and treat a lasting `UNKNOWN`
  as "not conflicting" (the old CI wait then applies).

## Tasks
### Task 1: `buildqueue.py requeue --conflict`
**Runs:** unattended
**Files:** buildqueue.py (modify), tests/test_runner.py (modify)
**What:** `requeue <key> --conflict --note "<files>"`: appends `Conflict (<time>): <note>`, sets `conflict=1`, increments
`conflict_rounds`, gives one attempt back (`attempts = max(0, attempts - 1)`), status `queued`, `not_before` now, drops `resume`.
Over `AGENT_MAX_CONFLICT_ROUNDS` (default 2) it sets `blocked` instead and prints `blocked`. Otherwise prints `queued`.
**Validate:** `uv run --with pytest pytest tests/test_runner.py -q` → passes with tests for: round 1 and 2 queued with attempts unchanged
after the next `start`; round 3 blocked; `--conflict` and `--fresh-attempts` together is a usage error.
**Status:** ✅ 2026-10-27

### Task 2: run.sh: detect, merge the base, conflict rounds
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify)
**What:**
- `pr_mergeable <pr>` → prints `MERGEABLE`, `CONFLICTING` or `UNKNOWN` (polls as above).
- `sync_base <pr>`: in a temp detached worktree `worktrees/<repo>/sync-<pr>` at `origin/<pr head branch>`, `git merge --no-edit origin/$BASE`.
  Clean → `git push origin HEAD:refs/heads/<branch>`, log `$key: merged $BASE into PR #<pr> (clean), CI re-runs`, return 0.
  Conflicts → record `git diff --name-only --diff-filter=U` in `$conflict_files`, `git merge --abort`, return 1. Always remove the temp worktree.
- `merge_pr`: before the checks wait, `CONFLICTING` → `sync_base`; 0 → carry on to the CI wait (now on the new head); 1 → return 4.
  Also, where it would return 2 ("no checks reported" or no Actions run), re-check `pr_mergeable` and return 4 if `CONFLICTING`.
- `merge_failure 4` → `conflicts with $BASE in <files>`.
- Implementer `ready` path: before `gate.py`, the same check and `sync_base`. On 4 here or from any `merge_pr` caller →
  `requeue --conflict --note "<files>"`; if that prints `blocked`, the existing blocked handling with the conflict reason; in
  `preprod_stage` also `preprod.sh release "$repo"` first. No `ci-failed` label for a conflict.
- Conflict round, at the start of an implementer run with `conflict=1`: `git -C $wt fetch -q origin`, fast-forward the worktree to
  `origin/<branch>` (reset it from `origin/<branch>` if it can't), then `git -C $wt merge --no-edit origin/$BASE`. Clean → push and skip
  Claude (write a `ready` result with the PR). Conflicts → run Claude with this prompt instead of `/implement`:
  "PR #<pr> for <plan> conflicts with origin/<base>. The runner started `git merge origin/<base>` in this worktree; these files have
  conflict markers: <files>. Resolve them, keeping both sides' intent; run `<validate.full>`; `git add` them, `git commit --no-edit`,
  `git push origin HEAD`. Then write the result file as for /implement (`ready` with the PR). Change nothing else."
  Unset `conflict` once the run ends, whatever its result. The normal `ready` path then gates and merges or reviews it.
**Validate:** `uv run --with pytest pytest tests/test_run.py -q` → passes with tests for: a CONFLICTING PR whose base merges cleanly is
pushed and merged (no block, no notice); a real conflict requeues a conflict round with the files in the note and no `ci-failed`
label; the conflict-round run gets the conflict prompt and the merge already started; a third conflict blocks with "conflicts with
main in …" in the notice; "no checks reported" on a conflicting PR says conflict, not "no CI checks".
**Status:** ❌

### Task 3: Docs
**Runs:** unattended
**Files:** README.md (modify)
**What:** "Limits": a "Conflicts" bullet (what the runner merges, conflict rounds, the cap, `AGENT_MAX_CONFLICT_ROUNDS`). "Merging": a
conflict is never reported as "no CI checks". Item lifecycle: a conflict round goes back to `queued`.
**Validate:** `grep -n "AGENT_MAX_CONFLICT_ROUNDS" README.md` → one or more lines; `scripts/validate.sh` passes.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes.
- `AGENT_DRY_RUN=1` tick in a temp state dir logs no merge or push.

## Human test steps
None (low risk). After `agent-runner release --install`, the next conflicting runner PR shows a `merged main into PR #n` line in
`runs.log` (clean) or a "Conflict" queue note and a conflict-round run (real conflict) instead of a 30-minute wait.

## Decisions
1. The runner merges the base (no rebase), so it never force-pushes and the implementer's permissions stay as they are.
2. A conflict round costs no attempt (the implementer did nothing wrong) but is capped at 2, so a churning base can't loop forever.
3. A clean base merge on an approved PR goes straight to CI and merge without another review: the gate already saw the PR's own diff,
   and a clean merge adds only what's already on the base. A conflict round goes back through the normal `ready` path (gate, review).
4. validator-loop-1 (fix rounds) may later fold conflict rounds into its round counter; this plan keeps them separate so it can merge first.
