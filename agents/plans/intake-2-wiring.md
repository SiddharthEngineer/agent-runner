# Plan: Queue intake from GitHub, part 2: every tick runs it, CLI and docs
**Status:** ❌ Not started
**Risk:** low (one upkeep line in run.sh, the CLI and docs; intake stays off until a repo's runner.toml opts in; runner programme PRs auto-merge on green CI, user 2026-10-19)
**Depends on:** intake-1-scan

## Goal
Each `agent-runner run` tick picks up newly pushed plans with `**Queue:** yes` from every opted-in repo, before it looks for the
next due item, so a plan pushed from a Claude cloud session starts building within about 5 minutes. `agent-runner intake` runs it by
hand. `/plan` writes the `**Queue:**` line and works in a cloud session with no `agent-runner` installed.

## Context
- Task 0: check intake-1 merged: `git show origin/main:intake.py | head -5` shows the docstring, and `grep -n "class Intake" config.py`.
- `run.sh:86-98`: upkeep block (inbox poll/sweep, versions sync, preprod tick); add `python3 "$HERE/intake.py" scan >> "$STATE/runs.log" 2>&1 || true`
  after `inbox.py sweep` and before `preprod.sh tick`. In a dry run, run `intake.py scan --dry-run` instead (it writes nothing).
- `agent-runner:9-38`: usage text and `case` (add `intake` to the `gate|inbox|…` python line).
- `tests/test_run.py`: how run.sh is driven in tests (stub `claude`/`gh`, temp state); add one test that a tick queues an opted-in plan.
- `README.md` sections "CLI", "How a tick works", "Day to day"; `AGENTS.md` Map table.
- `.claude/commands/plan.md:42-87` (template, step 4; hand-off, step 5) and `.claude/commands/schedule-build.md`.

## Tasks
### Task 1: Tick and CLI
**Runs:** unattended
**Files:** run.sh (modify), agent-runner (modify), tests/test_run.py (modify)
**What:** the upkeep line above (never fails the tick); `agent-runner intake scan [--repo N] [--dry-run]`.
**Validate:** `uv run --with pytest pytest tests/test_run.py -q` → passes, with a new test: a repo with `[intake] enabled = true` and a
plan with `**Queue:** yes` on origin → after one `run.sh` tick, `queue.json` has the item and `runs.log` has `intake: queued`.
**Status:** ✅ (2026-10-27)

### Task 2: `/plan` and `/schedule-build`
**Runs:** unattended
**Files:** .claude/commands/plan.md (modify), .claude/commands/schedule-build.md (modify)
**What:** the plan template header gains `**Queue:** yes | no [at <ISO time with Z>] [after <repo>/<slug>]`. Step 2: if
`agent-runner` isn't on PATH (a cloud session), skip `gate --plan` but still keep to the budget by hand; intake re-checks it.
Step 5: in a repo with `[intake] enabled`, pushing the plan with `**Queue:** yes` queues it (no `/schedule-build` needed); split parts
use `after`. `/schedule-build` says it is still the way to queue on the VPS directly, and for repos without intake.
**Validate:** `agent-runner check-commands agent-runner` (or `python3 commands_diff.py agent-runner`) runs without error;
`grep -n "Queue:" .claude/commands/plan.md` shows the template line.
**Status:** ❌

### Task 3: Docs
**Runs:** unattended
**Files:** README.md (modify), AGENTS.md (modify)
**What:** README: `intake` row in the CLI table; tick step 1 mentions intake; a short "Queue from anywhere" section (the `**Queue:**`
grammar, `[intake] enabled`, intake once / local wins, refusals as notices, only `origin/<base>` is read). AGENTS.md: `intake.py` row in the Map.
**Validate:** `grep -c "intake" README.md AGENTS.md` → both non-zero; `scripts/validate.sh` passes.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes.
- `AGENT_DRY_RUN=1` tick in a temp state dir with a fixture repo logs `intake: queued …` and leaves `queue.json` unchanged.

## Human test steps
After `agent-runner release --install`, and after adding `[intake]` with `enabled = true` to agent-runner's `agents/runner.toml` on main:
1. `agent-runner intake scan --dry-run` → no `queued` lines (no plan has a `**Queue:**` line yet).
2. From a Claude cloud session (or the GitHub web editor), push a tiny docs-only plan to agent-runner `main` with `**Queue:** yes`.
3. Within 5 minutes: `agent-runner queue list | grep <slug>` → `queued` (or `running`), and `grep "intake: queued" ~/.local/state/agent-runner/runs.log`.
4. Push a plan with 5 tasks and `**Queue:** yes` → one inbox notice `[agent-runner/<slug>] not queued: 5 tasks > budget 4`, and no second notice on later ticks.

## Decisions
1. Intake runs in upkeep, before the next-item pick, so a plan pushed now can start on the same tick.
2. Enabling intake in each repo's runner.toml is the user's step (it's a high-risk path and an access decision), not part of this plan.
