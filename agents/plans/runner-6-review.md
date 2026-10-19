# Plan: validator, review stage (fresh-context code review of `review` PRs)
**Status:** ✅ Done 2026-10-19 (all 4 tasks)
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19; nothing installed runs this until the cutover)
**Depends on:** runner-5-run

## Goal
When the implementer reports `ready` on a PR whose gate tier is `review`, a second Claude run with fresh context reviews it. It
sees only the plan, the PR diff and the validation output, never the implementer's session. It re-runs the repo's full validation,
reviews the diff against the plan's Goal and Files, checks that every runnable task is ✅, and returns `approve`, `changes` or
`blocked`. `changes` sends the item back to the implementer (its session resumed, the review as the note). `approve` moves the PR
on to preprod, or merges it if the repo has no preprod. `low` PRs skip the validator (design D3).

## Context
- Task 0 check: `run.sh` builds `<repo>/<slug>` items with per-repo config (runner-5-run). If not, `blocked`.
- `run.sh` after runner-5-run: the `ready` → gate → merge-or-`awaiting-preprod` branch, `run_claude`, the worktree layout. Find
  them with `grep -n 'gate.py\|awaiting-preprod\|run_claude' run.sh`.
- `buildqueue.py`: `RESULT_TO_STATUS` (`:49-54` at d361143), `cmd_finish`, `cmd_answer` (how a note and `resume=1` re-queue an item).
- Design D3: tick order, the review stage, "a `changes` costs an attempt", the validator's can/can't list.
- Gotcha: the runner's GitHub account owns both the PR and the review, and GitHub refuses `gh pr review --approve` on your own PR.
  Post the verdict with `gh pr review --comment` and keep the real verdict in the result file.
- Repos don't have their own validator settings until ef-runner-config / strelitzia-runner-config, so this repo ships defaults.

## Tasks
### Task 1: validator prompt and settings
**Runs:** unattended
**Files:** prompts/review-pr.md (new), settings/validate.json (new), tests/test_review.py (new)
**What:** `prompts/review-pr.md` is a template with `{repo}`, `{pr}`, `{plan_path}`, `{result_file}`, `{validate_full}`. Steps:
read the plan; `gh pr diff {pr}`; run `{validate_full}`; run the `/code-review` skill on the diff; check each finding against the
plan's Goal and Files; check that the tasks are ✅; post one `gh pr review {pr} --comment` with the verdict and the findings; and
write `{"verdict": "approve"|"changes"|"blocked", "summary": "...", "findings": [...]}` to `{result_file}` as the last action.
`changes` only for real defects or a plan requirement that's missing, never style. `settings/validate.json`: the implementer's
read and validate permissions (Strelitzia `scripts/agent/settings.json` is the model) plus `gh pr diff`, `gh pr view`,
`gh pr review --comment`. It denies `Edit`, `Write` (except the result file path pattern), `git commit`, `git push`, `gh pr merge`,
`make`, `docker compose`, `sudo`.
**Validate:** `uv run --no-project --with pytest pytest -q tests/test_review.py` → the template formats with all placeholders filled;
`settings/validate.json` parses, its `deny` has `Edit`, `Bash(git push:*)` and `Bash(gh pr merge:*)`, and nothing in `allow` matches `push`, `merge` or `commit`.
**Status:** ✅ 2026-10-19

### Task 2: the review stage in `run.sh`
**Runs:** unattended
**Files:** run.sh (modify), buildqueue.py (modify), tests/test_run.py (modify)
**What:** New item status `awaiting-review`. A `ready` result whose gate tier is `review` → `awaiting-review` (not
`awaiting-preprod`). Tick order (design D3): upkeep → (preprod stage: runner-7) → **review stage**: the oldest `awaiting-review`
item → detached worktree `worktrees/<repo>/review-<pr>` at the PR head → `claude -p "<filled prompt>" --model claude-opus-5-5
--settings <repo's agents/settings/validate.json, else settings/validate.json>` with a 1 h timeout → read
`results/<repo>/<slug>.review.json` → remove the worktree. At most one Claude run per tick, as now.
**Validate:** `pytest -q tests/test_run.py` with a stub `claude` that writes each verdict: `approve` → next status per Task 3;
`changes` → `queued` with the summary in notes and `resume=1`; a missing result → counts as a failed validator attempt (2 →
item `blocked`, notice sent).
**Status:** ✅ 2026-10-19

### Task 3: after the verdict
**Runs:** unattended
**Files:** run.sh (modify), buildqueue.py (modify), tests/test_run.py (modify)
**What:** `changes`: `buildqueue.py requeue <key> --note <summary>` (new). It is `answer` without resetting attempts, so the third failure
→ `blocked`. `approve`: if the repo's `runner.toml` has `[preprod]` → `awaiting-preprod` (preprod.sh tick deploys it); otherwise CI
green → merge with the repo's method, notice "merged after review", item `done`. `blocked` → item `blocked`, notice with the summary.
**Validate:** tests: agent-runner fixture (no `[preprod]`) + `approve` → merge call; Strelitzia fixture + `approve` →
`awaiting-preprod`; `changes` on an item at 1/2 attempts → queued; at 2/2 → `blocked`.
**Status:** ✅ 2026-10-19 (committed with Task 2)

### Task 4: docs
**Runs:** unattended
**Files:** README.md (modify), AGENTS.md (modify)
**What:** README: the item lifecycle with `awaiting-review`, the validator's limits, where its result and log live. AGENTS.md map:
`prompts/`, `settings/validate.json`.
**Validate:** `scripts/validate.sh` → all PASS.
**Status:** ✅ 2026-10-19

## Validation
- `scripts/validate.sh` passes.
- `AGENT_DRY_RUN=1` with a temp state holding one `awaiting-review` item → the log says `would run validator (review) for <key> PR #<n>`.

## Human test steps (preprod)
None (low risk). First live use: design Rollout step 6 (`review` engineerfamily smoke plan).

## Decisions
1. The validator gets a prompt file, not a slash command, so it works in repos that don't have it in `.claude/commands`.
2. Verdicts live in the result file; the PR gets a comment review, because GitHub won't let an account approve its own PR.
3. Opus for the review: it is the merge decision for `review` PRs.
