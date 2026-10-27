# Plan: Resolver, part 2: every block goes to the resolver first; triage goes away
**Status:** ❌ Not started
**Risk:** review (an unattended agent pushes fixes to PRs and every block path changes; the gate also flags `settings/*`)
**Depends on:** resolver-1-agent

## Goal
Every place run.sh blocks an item that has work to fix (implementer out of attempts, a red or missing CI on merge, a conflict out
of rounds, the validator's `blocked` or a validator that failed twice, a failed preprod check, fix rounds exhausted) calls
`buildqueue.py hold` instead of notifying. A **resolve stage** in the tick runs the resolver once on the oldest `awaiting-resolve`
item. Fixed → the item carries on (merge after green CI, or back to the validator). Transient → re-queued with fresh attempts.
Anything else → `blocked` and one notice with the resolver's diagnosis. The user hears nothing while the resolver is working or after it succeeds.

## Context
- Task 0: check part 1 merged: `grep -n "awaiting-resolve" buildqueue.py prstatus.py` and `ls prompts/resolve.md`.
- run.sh as of the end of the chain (search by name; line numbers moved): `notify_blocked` (triage, to remove), `block` (config
  blocks: no repo env, no runner.toml, refused config, plan not on base; these stay direct notices: the resolver can't fix the
  user's machine or config), `merge_pr` and `merge_failure`, the implementer `ready`/`finish` path (the `blocked)` case with
  `not_merged`), `review_stage` (`blocked`, `changes` out of rounds, validator failed twice), `preprod_stage` (`blocked`, `changes`
  out of rounds), and the conflict-round cap from runner-17. Keep any cost logging validator-loop-3 put around claude calls.
- The tick order: upkeep → health gate → preprod stage → review stage → next implementer item. Add the resolve stage after the review
  stage and before the next implementer item (finishing blocked work beats starting new work). `usage_gate` applies.
- Worktree: reuse `worktrees/<repo>/<slug>` if present, else add it from `origin/agent/<slug>` (`-B agent/<slug>`); no branch and no
  PR → skip the run and go straight to `blocked` with the plain notice.
- Implementer run shape to mirror (`run_claude`): `timeout`, `--settings $wt/agents/settings/implement.json`, `--permission-mode auto
  --permission-prompts none`, MCP config if present, `--output-format json`; usage-limit detection (give the turn back, pause 5 h).
- Tests: `tests/test_run.py` triage tests (`grep -n triage tests/test_run.py`) become resolver tests.

## Tasks
### Task 1: Block paths hold; the resolve stage
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify)
**What:**
- `hold_or_notify <title> <body> <stage>`: `buildqueue.py hold`; prints `awaiting-resolve` → log `$key: held for the resolver`, `pr_status`,
  no notice; prints `blocked` → today's notice (plus the resolver's diagnosis if `resolve_diagnosis` is set). Replace every
  `notify_blocked` call and the `not_merged` notice with it. Remove `notify_blocked` and the triage run.
- `resolve_stage`: `key=$(buildqueue.py oldest awaiting-resolve)`; `closed_by_hand` first; worktree as above; if `pr` is set and its
  checks failed, save `gh run view <id> --log-failed | tail -n 400` to `$STATE/results/<repo>/<slug>.ci-failed.log`; prompt from
  `prompts/resolve.md`; run Opus with a 60-minute timeout and `--max-turns 120`; log `logs/<time>-<repo>-<slug>.resolve.log`; result
  `results/<repo>/<slug>.resolve.json`. Set `resolve_tried=1` before the run.
- After the run: `fixed` and `validate.full` passes in the worktree → stage `implement`/`merge` with gate tier `low` → `merge_pr`
  (merged → `done`, `preprod_follow`, the usual merged notice; not merged → `blocked` + notice); tier `review` or stage
  `review`/`preprod` → `awaiting-review` (the validator re-reviews, preprod follows). `requeue` → `requeue --fresh-attempts --note`.
  `give-up`, no result, a failed `validate.full` or a timeout → `blocked`, one notice: block title, block body, then "Resolver
  (tried once, class `<class>`): <diagnosis>" and the log path. Store `resolve_diagnosis`.
**Validate:** `uv run --with pytest pytest tests/test_run.py -q` → passes with tests for: implementer out of attempts → awaiting-resolve,
no notice; resolver `fixed` on a low item → merged; `fixed` on a review item → awaiting-review; `requeue` → queued with 0 attempts;
`give-up` → blocked + one notice containing the diagnosis; a second block of the same item → blocked + notice, no resolver run;
`AGENT_RESOLVE=0` → blocked + notice at once; config blocks still notify directly.
**Status:** ✅ 2026-10-27 (this commit)

### Task 2: Retire triage; docs
**Runs:** unattended
**Files:** prompts/triage.md (delete), settings/triage.json (delete), README.md (modify), AGENTS.md (modify)
**What:** delete triage's prompt and settings. README: replace the "Triage" bullet in Limits with "Resolver" (when it runs, what it can
do, once per item, `AGENT_RESOLVE=0`, logs and result paths); tick list gains the resolve stage; item lifecycle gains `awaiting-resolve`.
AGENTS.md: the prompts row lists `resolve.md` instead of `triage.md`.
**Validate:** `grep -rn "triage" README.md AGENTS.md run.sh` → only a mention that `AGENT_TRIAGE=0` still turns the resolver off;
`scripts/validate.sh` passes.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes.
- `AGENT_DRY_RUN=1` tick with an `awaiting-resolve` item in a temp state dir logs "dry run: would run the resolver for <key>" and changes nothing.

## Human test steps
After `agent-runner release --install`:
1. `agent-runner queue list | grep -E "awaiting-resolve|blocked"` → nothing new is `blocked` without a resolver run first.
2. The next time an item gets stuck: `grep "resolver" ~/.local/state/agent-runner/runs.log | tail` shows `held for the resolver`, then
   `resolver start`, then `resolver fixed` / `requeue` / `give-up`. Only `give-up` (or a failed fix) sends you a notice, and it carries the diagnosis.
3. On a fixed PR: a `Resolver:` commit and one PR comment from the resolver.

## Decisions
1. The resolve stage runs before new implementer work, under the same one-Claude-at-a-time lock and usage gate.
2. Config blocks (no repo env, no runner.toml, refused config, plan not on base) skip the resolver: only the user can fix those.
3. A fixed `review`-tier item goes back to the validator rather than straight to merge: an unattended code change still gets a fresh-context review.
4. Risk `review`: this is the first agent besides the implementer that pushes to PR branches.
