---
description: Plan a feature or fix with the user and write agents/plans/<slug>.md (PIV step 1)
argument-hint: <feature description | GitHub issue # | path to an RCA note>
---

# Plan: $ARGUMENTS

Planning is always done **with the user**. Your output is one plan file that a fresh agent with no memory of this
conversation can implement and validate on its own, unattended.

## 1. Understand (ask, don't assume)
- If `$ARGUMENTS` is an issue number, read it: `gh issue view <n> --repo SiddharthEngineer/agent-runner`.
- Search the code for the patterns you'll mirror and read only the files you need. Don't read old plans
  (`agents/plans/done/`) unless the user points you at one.
- Ask the user questions (AskUserQuestion) until the goal, scope, edge cases and "done" are unambiguous.
  Give each question a recommended option. Several short rounds are better than one guess.

## 2. Decide risk with the user
- `low`: may auto-merge to main after green validation. Fits docs, tests, small refactors and lint fixes, and scraper or prompt tweaks.
- `review`: a PR that a fresh-context validator reviews before it merges (this repo has no preprod: a merge goes live only
  when the user tags and installs). Use it for everything else, and whenever you're unsure.
- `agent-runner gate --config agents/runner.toml` re-checks a `low` plan after the code is written and downgrades it to `review` if the diff
  touches high-risk paths (`[gate] high_risk` in `agents/runner.toml`), goes over the size budget, or weakens tests. Tell the user this.
- After writing the plan (step 4), run `agent-runner gate agents/plans/<slug>.md --plan --config agents/runner.toml`. If `agent-runner`
  isn't on PATH (a Claude cloud session), skip it but keep to the step 3 budget by hand: intake re-checks it and refuses an oversize plan. If a `low` plan
  comes back `review`, tell the user which files caused it (the `touches high-risk path` reasons), then either change
  the risk to `review` or narrow the files. If `"split"` isn't empty, go back to step 3 and split it.
- Mark a task `Runs: interactive` if it needs the user, the real VPS state (`~/.config/agent-runner`, `~/.local/state`, the crontab),
  an install, a tag, or secrets.
  Unattended runs skip those tasks and flag them in the PR.

## 3. Size it: one plan = one short unattended run
Every turn of an unattended run re-sends its whole context, so cost grows much faster than plan size. Plans touching ≤14 files
took 19–73 turns; plans touching 22–49 files took 100–180 turns and most of a 5-hour usage window.
- Budget per plan: **≤4 tasks, ≤12 files across all `**Files:**` lines, ≤10 KB**. `gate.py --plan` prints any overrun under `"split"`.
- When the feature is bigger, **always recommend splitting it** before writing anything, and show the user the proposed parts
  (AskUserQuestion, splitting as the recommended option). Write the full plan only if the user explicitly declines the split.
- Split along seams that each merge on their own with green validation: config/state → a script's logic → `run.sh` wiring → docs.
  Name the parts `<slug>-1-<part>`, `<slug>-2-<part>`, …, give each later part a `**Depends on:** <earlier slug>` line,
  and keep each part's Context to what that part needs.
- Context entries cite line ranges (`file.py:120-160`), not whole files, so the implementer reads only those lines.

## 4. Write `agents/plans/<slug>.md` (kebab-case)

```markdown
# Plan: <Title>
**Status:** ❌ Not started | 🔄 In progress | ✅ Complete (YYYY-MM-DD)
**Risk:** low | review (one-line reason)
**Issue:** #n (optional)
**Depends on:** <slug> (optional; an earlier part of a split plan)
**Queue:** yes | no [at <ISO time with Z>] [after <repo>/<slug>] (optional; repos with `[intake] enabled` queue it on push)

## Goal
One paragraph: the user-facing outcome.

## Context
- `path/file.py:120-160`: pattern to mirror / code to change (why)
- Gotchas: library quirks, data shapes, things that look wrong but are intentional.

## Tasks
### Task N: <Title>
**Runs:** unattended | interactive (why)
**Files:** path (new|modify)
**What:** the specific approach, with signatures for any new functions or dataclasses
**Validate:** `exact command` → expected result (a failing-first test for bug fixes)
**Status:** ❌ | ✅ (YYYY-MM-DD, commit) | ⏸ (reason)

## Validation
- `scripts/validate.sh` passes.
- Feature-specific commands with expected output (CLI runs against fixtures in a temp `HOME`/`AGENT_RUNNER_STATE`, …).

## Human test steps
Numbered steps the user follows after installing the release (`install.sh <tag>`): the command to run and what they should see.
Write "None (low risk)" if not applicable.

## Decisions
Numbered, one sentence each: the choice and why.
```

Rules: success means a command printed something checkable, never "looks good". Each task is one committed,
testable unit. Never put personal data (resume text, contact details, DB rows) in a plan. Point at the file instead.

## 5. Register and hand off
1. Add a row to the **Active** table in `agents/ROADMAP.md` (ID, initiative, plan link, risk, status ⏳, depends on), one per part.
   When a plan is finished, `/implement` moves it to `agents/plans/done/` and repoints the row (open plans stay in `agents/plans/`).
2. Show the user the plan summary, then commit both files on `main` and push (`git push origin main`), because the unattended
   runner only builds plans that are on `origin/main`.
3. In a repo with `[intake] enabled = true` in `agents/runner.toml`, pushing the plan with `**Queue:** yes` queues it on the
   runner's next tick (no `/schedule-build` needed, and it works from a cloud session). Give split parts `**Queue:** yes after <slug>-1-<part>`.
   `**Queue:** no` (or no line) leaves it for the user. Otherwise:
4. Ask the user: implement now in this session (`/implement agents/plans/<slug>.md`), or schedule it
   (`/schedule-build <slug> [time]`)? Queue split parts in order with `after`: `/schedule-build <slug>-2-<part> after <slug>-1-<part>`.
