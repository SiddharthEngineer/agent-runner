# Plan: every runner PR shows its stage checklist at the top of its description
**Status:** ❌ Not started
**Risk:** low (no prod impact; runner programme PRs auto-merge on green CI, user 2026-10-19)
**Depends on:** - (goes live only when the user installs a release: AM8)

## Goal
The user checks status from the GitHub app on a phone or desktop, without Claude or the VPS (user, 2026-10-22). Every PR the
runner builds, in every registered repo, starts with a status block the runner keeps current:

```
<!-- agent-runner:status -->
**Runner status:** 🔁 Validator review: asked for changes (Task 3 permissions); implementer re-run queued, attempt 2/2 · updated 2026-10-22 16:40 UTC
1. ✅ Implementer opened the PR, 7 Human test steps
2. 🔁 Validator reviews the code: first review asked for changes: <first sentence of its summary>; queued to run again
3. ⏳ Runner deploys the branch to preprod
4. ⏳ Second validator runs the Human test steps on preprod and posts a checklist
5. ⏳ Merge, preprod back on main; steps it couldn't do come to you as a "merged; N steps left for you" issue
<!-- /agent-runner:status -->
```

Marks: ✅ done · 🔁 in progress or re-running · ⏳ not reached · ❌ failed (with why) · ⏸ blocked (with the inbox issue link) ·
➖ skipped (with why: `low` tier merges on green CI, so 2-4 are ➖; a repo with no `[preprod]` has 3-4 ➖).
The first line always says where it is now, so the GitHub app's PR list preview tells the story.

## Context
- `run.sh`: implementer `ready` → gate (≈575-600: `review` tier comments and sets `needs-review`; `low` merges);
  `review_stage` (≈386-440: `changes` requeues with note "Validator review of PR #… asks for changes: <summary>", `approve`
  sets `awaiting-preprod` or merges, `blocked`); `preprod_stage` (≈288-384: `approve` merges, `preprod_follow`, notifies
  "merged; N steps left for you"; `changes`; `blocked`); `closed_by_hand` (≈261). `preprod.sh deploy` moves an item to
  `on-preprod`. Statuses: `queued running awaiting-review awaiting-preprod on-preprod done blocked question cancelled`.
- `buildqueue.py`: `get/set/unset <key> <field>`, `list`; the item keeps `pr`, `attempts`, `note`, `review_attempts`,
  `preprod_verdict`. `notify` (run.sh:71) calls `inbox.py`, which prints the issue URL (inbox.py:101).
- The preprod validator already posts its checklist as a PR comment (`prompts/preprod-check.md` step 4).
- Each repo's `GH_REPO` and `[preprod]` come from its `runner.toml` (`load_item`, `cfg`). Standard library only.

## Tasks
### Task 1: `prstatus.py` renders and writes the block
**Runs:** unattended
**Files:** prstatus.py (create), tests/test_prstatus.py (create)
**What:** `render(item: dict, *, has_preprod: bool, human_steps: int, now) -> str` builds the block above from queue fields
only (no network), covering every status and verdict; a `changes` re-run shows the note's first sentence (≤140 chars) and
`attempt n/AGENT_MAX_ATTEMPTS`; `blocked`/`question` show the inbox issue URL from item field `issue` when set.
`human_steps` counts numbered lines under the PR body's "Human test steps" heading (0 → "⚠️ no Human test steps" on 1 for
a repo with preprod). `apply(body, block) -> str` replaces the marked block, or puts it first followed by a blank line; it
never touches the rest of the body. CLI: `prstatus.py update <key>` (one item) and `prstatus.py sync` (every item with a `pr`
that isn't `cancelled`, plus `done` items whose block isn't final yet, up to 14 days old): `gh pr view --json body,state`,
then `gh pr edit --body-file` only when the body changes. A `gh` failure logs one line and moves on (never fails the tick).
Tests: a render per stage (low tier, review path, changes re-run, approve, on-preprod, preprod changes, merged with N left,
blocked, no-preprod repo), `apply` idempotent and body-preserving.
**Validate:** `uv run --with pytest pytest -q tests/test_prstatus.py` passes.
**Status:** ❌

### Task 2: the runner records what the block needs and refreshes it
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify)
**What:** At each transition set the fields `render` reads: `tier` (gate), `review_verdict` and `review_summary`,
`preprod_comment` (the newest PR comment URL by the runner's account after the preprod validator), `left_count` and
`left_issue` (the URL `notify` prints for "merged; N steps left for you"), `issue` on blocked/question notices, `merged_at`.
Make `notify` echo the URL it gets. Call `prstatus.py update "$key"` right after each transition, and `prstatus.py sync`
once per tick after the inbox poll (also on a dry run: it only prints what it would edit). Tests with the stubbed `gh`:
the review `changes` path and the preprod `approve` path both record the fields and edit the PR body once.
**Validate:** `scripts/validate.sh` passes.
**Status:** ❌

### Task 3: docs and tracking
**Runs:** unattended
**Files:** README.md (modify), AGENTS.md (modify), agents/ROADMAP.md (modify), agents/plans/runner-14-pr-checklist.md (modify)
**What:** README: a "PR status block" section (the five stages, the marks, that it refreshes every tick, that editing
inside the markers is overwritten). AGENTS.md map row for `prstatus.py`. ROADMAP: A20 row ✅ with the PR; AM8 stays the user's.
**Validate:** `scripts/validate.sh` passes.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes; CI is green.

## Human test steps
None on preprod (agent-runner has no preprod). After AM8: the next runner PR in any repo, and engineerfamily #66 within one
tick, starts with the block, and its first line matches `agent-runner queue list`.

## Decisions
1. 2026-10-22 (user): all runner repos; the block sits at the top of the PR description, not in a comment.
2. The block is derived from queue state every tick (not only at transitions), so PRs opened before this ships, and
   anything a missed update left stale, catch up within 5 minutes.
3. Stage 5's release line ("Released in v1.4.2") comes in A21 (runner-15-versions).
