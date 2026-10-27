# Plan: Validator loop, part 1: fix rounds don't use up attempts
**Status:** ❌ Not started
**Risk:** low (queue bookkeeping and run.sh wiring; goes live only on install; runner programme PRs auto-merge on green CI, user 2026-10-19)
**Depends on:** intake-2-wiring (both edit run.sh and tests/test_run.py)

## Goal
Today a validator `changes` verdict (review or preprod stage) re-queues the implementer and that fix run uses one of the item's 2
attempts, so one more miss blocks the item. The last four review items (engineerfamily site-perms 2 and 3, Strelitzia site-perms 4,
engineerfamily wordle-solver-3) each used both attempts. After this plan an item gets up to 3 **fix rounds**
(`AGENT_MAX_FIX_ROUNDS`, default 3), shared by both stages, that don't use attempts. Attempts count only runs that crash, time out,
write no result or fail `validate.full`. The fix run is told plainly it is a fix round, with the validator's findings.
The item is blocked (with the usual notice and triage) when rounds run out, or when the validator reports the same unresolved
finding two rounds running.

## Context
- Task 0: check intake-2 merged: `git log origin/main --oneline | grep -i intake` shows both parts, and `grep -n intake.py run.sh`.
- `buildqueue.py:52` `MAX_ATTEMPTS`; `:190-199` `cmd_start` (adds an attempt); `:202-225` `cmd_finish` (no valid result → failed
  attempt, drops `resume`/`session_id`); `:268-286` `cmd_requeue` (validator path keeps attempts, sets `resume=1`; `--fresh-attempts`
  is triage's); `:340-369` argparse.
- `run.sh:446-510` `review_stage`: `changes)` at ≈477 calls `requeue --note` and checks for `blocked`.
  `run.sh:423-431` `preprod_stage` `changes)` does the same with the failed steps.
- `run.sh:575-588`: on `resume=1` the opener says "The user answered your question." even after a validator `changes`. Wrong for a fix.
- `run.sh:185-250` `notify_blocked` (triage once per item); reuse it for rounds-exhausted and stuck blocks.
- `prstatus.py:68-212` `render`: `attempt_txt` (≈79-80) and the `rerun` lines (≈182-187) show "attempt N/2".
- Tests to mirror: `tests/test_runner.py` `q` fixture and `test_question_then_answer_requeues_with_fresh_attempts` (:66);
  `tests/test_run.py:441-456` (`test_review_changes_requeues_with_note`, `…_out_of_attempts_blocks`), `:525-546` (preprod changes);
  `tests/test_prstatus.py:61-110` (`test_review_changes_rerun`, `test_preprod_changes_rerun`).
- The review result file today: `{"verdict", "summary", "findings": [..]}`. Part 2 adds `"unresolved": [..]` (previous findings still
  not fixed). This part must already read it: missing or empty means none.

## Tasks
### Task 1: queue: fix rounds
**Runs:** unattended
**Files:** buildqueue.py (modify), tests/test_runner.py (modify)
**What:** `MAX_FIX_ROUNDS = int(os.environ.get("AGENT_MAX_FIX_ROUNDS", "3"))`. New `requeue --fix --stage review|preprod --note T
[--unresolved JSON]`: if `fix_rounds` (default 0) is already `MAX_FIX_ROUNDS`, or `--unresolved` is non-empty and the item's
`last_unresolved` is non-empty too (the same findings stuck two rounds), set `blocked` and print `blocked`, with `blocked_why`
`rounds` or `stuck`. Else: `fix_rounds += 1`, `fix_pending=1`, note appended as `"<Stage> validator, round N/M (<time>): T"`,
`last_unresolved` = the list, `status=queued`, `resume=1`; print `queued`. `cmd_start`: when `fix_pending` is set, pop it, set
`run_kind=fix` and don't add an attempt; otherwise `run_kind=build`. `cmd_finish` with no valid result and `run_kind=fix`: add
the attempt then (a crashed fix still counts), and if that reaches `MAX_ATTEMPTS`, `blocked`. `--fresh-attempts` also resets
`fix_rounds` and `last_unresolved`. The old validator `requeue` (no `--fix`) keeps working. `list` shows `rounds=N/M` when > 0.
**Validate:** `uv run --with pytest pytest tests/test_runner.py -q` passes, with new tests: three `--fix` re-queues each leave
`attempts` unchanged across start/finish(ready); a fourth blocks with `rounds`; a non-empty `--unresolved` twice in a row blocks
with `stuck`; a crashed fix run adds an attempt.
**Status:** ✅ 2026-10-27

### Task 2: run.sh: both stages use rounds; the fix run knows it's a fix
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify)
**What:** `review_stage` `changes)` → `requeue --fix --stage review --note "<summary> Findings: <json>" --unresolved "<result's
unresolved or []>"`; `preprod_stage` `changes)` → `requeue --fix --stage preprod` with the failed steps (no `--unresolved`). On
`blocked`: `notify_blocked` with title `blocked: validator still asks for changes after N rounds` (`rounds`) or
`blocked: the same findings came back unfixed` (`stuck`), body with the PR link and the unresolved list; the preprod branch keeps
its "stays on preprod" text. In the resume opener, when the newest note starts with `Review validator, round` or
`Preprod validator, round`, say: "A validator asked for changes (round N of M). Fix exactly these findings on the same branch,
re-run validation, push, and write the result file with status ready. Don't redo finished work." plus that note.
Otherwise the current openers. README env table gets `AGENT_MAX_FIX_ROUNDS`.
**Validate:** `scripts/validate.sh` passes. Update the existing changes tests to expect `--fix` and unchanged attempts; new: the
fourth `changes` blocks with the rounds notice; a resume after `changes` prompts with "round 1 of 3", not "The user answered".
**Status:** ✅ 2026-10-27

### Task 3: PR status block shows rounds
**Runs:** unattended
**Files:** prstatus.py (modify), tests/test_prstatus.py (modify)
**What:** when `rerun` is set, the stage line and first line say `fix round N/M` (from `fix_rounds`, `buildqueue.MAX_FIX_ROUNDS`)
instead of `attempt N/2`. A rounds/stuck block shows ⏸ with `blocked_why`. Other text unchanged.
**Validate:** `uv run --with pytest pytest tests/test_prstatus.py -q` passes; `test_review_changes_rerun` expects "fix round 1/3".
**Status:** ❌

### Task 4: docs and tracking
**Runs:** unattended
**Files:** README.md (modify), agents/ROADMAP.md (modify)
**What:** README "Limits" and both "Validator" sections: `changes` starts a fix round (3 per item, shared by both stages, no attempt
used), blocked when rounds run out or findings come back unfixed twice; attempts count failed runs only. ROADMAP: A24 ✅ with the PR.
**Validate:** `scripts/validate.sh` passes.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes; CI is green.
- Rehearsal: `T=$(mktemp -d)`; a queue with one item; `AGENT_RUNNER_STATE=$T python3 buildqueue.py requeue <key> --fix --stage review
  --note x` three times with `start`/`finish` between → `attempts` still 1; a fourth → `blocked`.

## Human test steps
After `agent-runner release --install`: the next review-tier item that gets `changes` shows "fix round 1/3" in its PR status block, and
`agent-runner queue list` shows `rounds=1/3` with the attempts unchanged.

## Decisions
1. 2026-10-26 (user): 3 fix rounds per item, not counted as attempts.
2. One budget for both stages, so a PR that bounces between review and preprod can't loop more than 3 times.
3. "Stuck" uses the validator's own `unresolved` list (part 2), not text matching of findings, which would be fragile.
4. A crashed or failed fix run still costs an attempt: that is a run failure, not a review outcome.
