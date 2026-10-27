# Plan: Validator loop, part 2: a scoped Sonnet re-review after each fix
**Status:** ✅ Done 2026-10-27
**Risk:** low (a new prompt and run.sh wiring; goes live only on install; runner programme PRs auto-merge on green CI, user 2026-10-19)
**Depends on:** validator-loop-1-rounds

## Goal
Today every review is a full Opus review with `/code-review` (about $1.00–1.50 API-equivalent per run in the logs, as much as
many implementer runs), and a re-review after a fix is the same full review, so it can raise new nitpicks on code that didn't
change. After this plan the **first** review of an item stays Opus with the full prompt. Each later review (after a fix round,
from either stage) runs on **Sonnet** with a new prompt scoped to two things: is each previous finding fixed, and does the fix
diff (`<reviewed sha>..HEAD`) add a real defect. It runs the full validation too. It reports `unresolved` findings, which part 1
uses to block an item whose findings come back unfixed.

## Context
- Task 0: check part 1 merged: `grep -n "MAX_FIX_ROUNDS" buildqueue.py` and `grep -n -- "--fix" run.sh`.
- `run.sh:446-510` `review_stage`: builds `vprompt` from `prompts/review-pr.md` with `str.format` (≈459-461), runs
  `claude -p … --model claude-opus-5-5` (≈465), reads `verdict`/`summary`/`findings` from the result file.
- `run.sh:334-440` `preprod_stage` `changes)`: the failed steps become the next round's findings.
- `prompts/review-pr.md` (25 lines): the full review prompt; keep its rules on real defects, the single `gh pr review --comment`
  and the result file. `tests/test_review.py:10-18` checks a prompt's placeholders. Mirror it for the new prompt.
- `tests/test_run.py:326-470`: review stage tests (stubbed `claude` records its argv; assert on `--model` and prompt text).
- Queue fields from part 1: `fix_rounds`, `last_unresolved`, the `Review validator, round …` / `Preprod validator, round …` notes.

## Tasks
### Task 1: the re-review prompt
**Runs:** unattended
**Files:** prompts/rereview-pr.md (new), tests/test_review.py (modify)
**What:** placeholders `{repo} {pr} {plan_path} {result_file} {validate_full} {since_sha} {round} {max_rounds} {findings}`. Steps:
read the plan's Goal and the previous findings ({findings}, one per line); `git diff {since_sha}..HEAD` (the fix) and
`gh pr diff` only for context around a finding; run `{validate_full}`; for each previous finding say fixed or not, with
file:line; check only the fix diff for real defects (same definition as review-pr.md step 5). Rule: don't raise anything on
code the fix didn't touch, unless validation fails. Post one `gh pr review --comment` titled `Re-review (round {round} of
{max_rounds})`. Result: `{{"verdict", "summary", "findings": [new or still-open, file:line], "unresolved": [previous findings
not fixed]}}`. `approve` only when every previous finding is fixed, validation passes and the fix adds no defect.
**Validate:** `uv run --with pytest pytest tests/test_review.py -q` passes, with a placeholder test for the new prompt.
**Status:** ✅ 2026-10-27 (f4ae162)

### Task 2: run.sh picks the prompt and model
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify)
**What:** `review_stage` records `reviewed_sha` (the checked-out PR head) on every run; `preprod_stage` records it on `changes`
(the deployed sha). When `fix_rounds` > 0 and `reviewed_sha` is an ancestor of the new head (`git merge-base --is-ancestor`):
use `prompts/rereview-pr.md`, `--model "${AGENT_REREVIEW_MODEL:-claude-sonnet-5-5}"`, `timeout 30m`, findings from the newest
round note. Otherwise (first review, a force-push, no sha): today's prompt on Opus. Pass the result's `unresolved` to
`requeue --fix --unresolved`. `runs.log` says `validator (re-review, round N/M, sonnet)` or `validator (review, opus)`.
**Validate:** `scripts/validate.sh` passes; new tests: the first review runs Opus with review-pr.md; after a `changes` and a fix,
the next review runs Sonnet with `Re-review (round 1 of 3)` and the `since_sha`; a head that isn't a descendant falls back to Opus.
**Status:** ✅ 2026-10-27 (95066ec)

### Task 3: docs and tracking
**Runs:** unattended
**Files:** README.md (modify), AGENTS.md (modify), agents/ROADMAP.md (modify)
**What:** README "Validator, review stage": first review Opus + `/code-review`; re-reviews Sonnet, scoped to the previous
findings and the fix diff; `AGENT_REREVIEW_MODEL` overrides. AGENTS.md Map: the new prompt. ROADMAP: A25 ✅ with the PR.
**Validate:** `scripts/validate.sh` passes.
**Status:** ✅ 2026-10-27 (e8c0522)

## Validation
- `scripts/validate.sh` passes; CI is green.

## Human test steps
After `agent-runner release --install`: on the next item that gets `changes`, `runs.log` shows `validator (re-review, round 1/3,
sonnet)` and the PR has a "Re-review (round 1 of 3)" review that only discusses the previous findings and the fix.

## Decisions
1. 2026-10-26 (user): Opus for the first review, Sonnet for re-reviews.
2. Re-reviews don't run `/code-review`: the fix diff is small, and it's the main cost of the full review.
3. Scoping stops new nitpicks on unchanged code, which is what kept items looping. A failing validation can still be raised anywhere.
4. A re-review after a preprod `changes` gets the failed steps as its findings, so the code is checked before preprod runs again.
