# Plan: Validator loop, part 4: a `/fix` PR comment sends the PR back to the implementer
**Status:** ✅ Done 2026-10-27
**Risk:** low (a new upkeep script; goes live only on install; runner programme PRs auto-merge on green CI, user 2026-10-19)
**Depends on:** validator-loop-3-costs

## Goal
The user can send any open runner PR back to the implementer from the GitHub app: a PR comment whose first line starts with `/fix`
(for example `/fix the Users table should sort by email`) re-queues the item as a fix round, with the comment as the finding. This is
the "re-queue on a changes-requested review" half of the backlog item. A GitHub "Request changes" review can't be used: the runner
opens PRs as the user's own account (`gh api user` → `SiddharthEngineer`), and GitHub doesn't let an author request changes on
their own PR. It works on an item that is awaiting review, awaiting or on preprod, or blocked, so the user can unblock an item
from a phone. A user `/fix` doesn't use a round or an attempt: the user decided it.

## Context
- Task 0: check part 3 merged: `grep -n "def cmd_cost\|cost" usage.py | head -3`.
- `buildqueue.py` `cmd_requeue --fix` (part 1): add `--stage user`, which skips the rounds and stuck checks, doesn't increase
  `fix_rounds`, clears `last_unresolved`, and works from `blocked`. Note prefix `User /fix (<time>): …`.
- `run.sh:86-98` upkeep block (after `inbox.py sweep`, and after intake's line): add
  `python3 "$HERE/prcomments.py" scan >> "$STATE/runs.log" 2>&1 || true`; in a dry run `scan --dry-run`.
- `run.sh` resume opener (part 1): a newest note starting `User /fix` gets "The user asked for this change on the PR: …".
  Then the normal path: ready → review (a scoped re-review, part 2) → preprod.
- `inbox.py:poll` is the pattern for reading GitHub and re-queuing with a note; `prstatus.py:251-296` for `gh` calls per item and
  for registered repos (`repo_config`). Standard library only; `gh` through `subprocess`.
- Validators and the implementer post as the same account, so the trigger must be text they never write: the comment's body
  starts with `/fix` at column 0. Add to `prompts/review-pr.md`, `rereview-pr.md` and `preprod-check.md`: never start a comment
  with `/fix`.

## Tasks
### Task 1: `prcomments.py scan`
**Runs:** unattended
**Files:** prcomments.py (new), tests/test_prcomments.py (new), buildqueue.py (modify), tests/test_runner.py (modify)
**What:** for each queue item with a `pr` and status in `awaiting-review`, `awaiting-preprod`, `on-preprod`, `blocked`, `queued`
(not `running`): `gh pr view <pr> --repo <github> --json state,comments`. Take comments by the viewer (`viewerDidAuthor`) whose body
starts with `/fix` and whose `createdAt` is after the item's `fix_comment_at` (default: the item's `added`). For each new one, oldest
first: `requeue --fix --stage user --note "<comment text without /fix>"`, set `fix_comment_at`, and reply on the PR
`Queued: the implementer picks this up on the next free tick.` On a merged or closed PR, reply once:
`This PR is merged/closed; plan a follow-up instead.` and set `fix_comment_at`. `--dry-run` logs what it would do. A `gh` failure
logs one line and leaves the item alone. buildqueue gets `--stage user` as in Context.
**Validate:** `uv run --with pytest pytest tests/test_prcomments.py tests/test_runner.py -q` passes, with tests (fake `gh`): a new `/fix`
re-queues once and a second scan does nothing; a comment that doesn't start with `/fix` is ignored; a `blocked` item goes back to
`queued` with `fix_rounds` unchanged; a merged PR gets one reply; a `running` item is skipped.
**Status:** ✅ 2026-10-27

### Task 2: wire it in
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify), prompts/review-pr.md (modify), prompts/preprod-check.md (modify), prompts/rereview-pr.md (modify)
**What:** the upkeep line and the `User /fix` opener from Context; the "never start a comment with `/fix`" line in the three prompts.
**Validate:** `scripts/validate.sh` passes; a run test: a tick with a `/fix` comment on an `awaiting-preprod` item re-queues it, and
the next implementer run's prompt contains "The user asked for this change".
**Status:** ✅ 2026-10-27

### Task 3: docs and tracking
**Runs:** unattended
**Files:** README.md (modify), AGENTS.md (modify), agents/ROADMAP.md (modify)
**What:** README "Day to day": `/fix <what to change>` on a runner PR, which states it works in, and that it doesn't use rounds or
attempts. "How a tick works": the upkeep step. AGENTS.md Map: `prcomments.py`. ROADMAP: A27 ✅ with the PR; remove the
back-and-forth item from Backlog.
**Validate:** `scripts/validate.sh` passes.
**Status:** ✅ 2026-10-27

## Validation
- `scripts/validate.sh` passes; CI is green.

## Human test steps
After `agent-runner release --install`, on the next runner PR that is awaiting review or on preprod:
1. Comment `/fix <a small, real change>` from the GitHub app → within 5 minutes the PR gets the "Queued" reply.
2. `agent-runner queue list` shows the item `queued` (then `running`), with the same attempts and rounds.
3. The PR then gets a new commit, a "Re-review" review and, for a preprod repo, a new preprod checklist.

## Decisions
1. 2026-10-26: a `/fix` comment, not a "Request changes" review: GitHub doesn't allow those on your own PR, and the runner's account is yours.
2. A user `/fix` bypasses the round limit and the stuck check: the user is the person those limits protect.
3. Merged PRs are out of scope: a fix there is a new plan.
