# Plan: finished plans move to `agents/plans/done/`
**Status:** ⏳ Queued 2026-10-21
**Risk:** low (docs, one path lookup in `run.sh`; no prod impact; auto-merges on green CI, user 2026-10-19)
**Depends on:** -

## Goal
When a plan is finished, it moves from `agents/plans/<slug>.md` to `agents/plans/done/<slug>.md`, so `agents/plans/` only holds
plans that still have work. The ROADMAP already says "Finished plans move to `agents/plans/done/`", but nothing does it: `done/`
is empty and 18 finished plans sit in `agents/plans/`. Fix that once, make `/implement` do it from now on, and make the runner's
validators find a plan that the PR under review has moved.

## Context
- `.claude/commands/implement.md` §2 step 3: sets the plan's Status and the ROADMAP row. This is where the move belongs.
- `run.sh`: `plan=$PLANS/$slug.md` (~line 122) is checked on `origin/$BASE`, then passed as `plan_path` to `prompts/review-pr.md`
  (review worktree `$rwt`, ~line 390) and `prompts/preprod-check.md` (preprod worktree `$pwt`, ~line 314). Both worktrees are the
  PR head. If the implementer moved the plan in its PR, `$rwt/$plan` no longer exists there.
- `.claude/commands/schedule-build.md` step 1 checks `agents/plans/<slug>.md`. A plan in `done/` has no open tasks, so it must not be queued: leave that check as is.
- Finished plans today (Status ✅): ef-agents-layout, ef-runner-config, ef-validate-ci, health-1-clean, health-2-alert,
  runner-1-config, runner-3-state … runner-8-self-host, runner-10-triage, runner-11-autopilot, runner-12-fast-install,
  smoke-cutover-docs, smoke-roadmap-cutover. Also runner-2-install (🟡 only for `ci.yml`, which AM1a finished on 2026-10-19).
- Stay in `agents/plans/`: `shared-agent-runner.md` (the design doc, not a build plan), `runner-9-cleanup.md` (A14, queued), this plan until it's done.
- No other repo links to agent-runner plan files (checked 2026-10-21: no `agent-runner/blob/main/agents/plans/` in Strelitzia or engineerfamily).

## Tasks
### Task 1: validators find a moved plan
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify)
**What:** Add a small helper `pr_plan <worktree>`: prints `$plan` if `<worktree>/$plan` exists, else `$PLANS/done/$slug.md` if that
exists, else `$plan`. Use it for `plan_path` in the review and preprod-check prompts. The base-branch check stays on `$PLANS/$slug.md`.
**Validate:** `pytest -q tests/test_run.py 2>&1 | tail -n 20` → new tests pass: a review whose PR head has only
`agents/plans/done/<slug>.md` gets that path in the validator prompt; one with the plan in place gets `agents/plans/<slug>.md`.
**Status:** ✅ 2026-10-21 (this commit)

### Task 2: `/implement` moves the finished plan
**Runs:** unattended
**Files:** .claude/commands/implement.md (modify), .claude/commands/plan.md (modify)
**What:** implement.md §2 step 3: when every task is ✅ (none left for the user), `git mv agents/plans/<slug>.md agents/plans/done/<slug>.md`,
fix relative links in the moved file (one more `../`) and every link to it (`grep -rn "<slug>.md" agents README.md AGENTS.md`),
and point the ROADMAP row at `plans/done/<slug>.md`. A plan with "Left for you" steps stays put until the user finishes them.
plan.md §5: say the same in one line next to the ROADMAP row step.
**Validate:** `grep -n "plans/done" .claude/commands/implement.md` → the new step; `scripts/validate.sh` → all PASS (incl. `check-commands`).
**Status:** ✅ 2026-10-21 (this commit)

### Task 3: move today's finished plans
**Runs:** unattended
**Files:** agents/plans/*.md → agents/plans/done/ (git mv, 18 files), agents/ROADMAP.md (modify), agents/plans/shared-agent-runner.md (modify), README.md (modify if it links them), agents/plans/done/.gitkeep (delete once non-empty)
**What:** Set runner-2-install's Status to ✅ (AM1a did `ci.yml`). `git mv` the 18 plans in Context. Fix every relative link that
pointed at them or out of them, in this repo only. Don't edit their contents otherwise.
**Validate:** `ls agents/plans/*.md` → only `shared-agent-runner.md`, `runner-9-cleanup.md`, `plans-done-folder.md`. A link check prints nothing:
`python3 -c 'import re,pathlib,sys; [print(f,l) for f in pathlib.Path(".").glob("**/*.md") if ".git" not in f.parts for l in re.findall(r"\]\(([^)#:]+\.md)", f.read_text()) if not (f.parent/l).exists()]'`
**Status:** ✅ 2026-10-21 (this commit; no `done/.gitkeep` existed)

## Validation
- `scripts/validate.sh` passes.
- The link check in Task 3 prints nothing.

## Human test steps
None (low risk). After the next install, a plan the runner finishes lands in `agents/plans/done/` in its PR.

## Decisions
1. The implementer moves the plan in its own PR, so the move merges with the work; the validators look in `done/` as a fallback.
2. A plan with steps left for the user stays in `agents/plans/` until they're done, so open work stays visible.
3. This plan moves itself (Task 2's rule) when its own tasks are done.
