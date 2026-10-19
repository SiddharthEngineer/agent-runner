---
description: Queue a plan for an unattended build on the VPS, or show and manage the queue
argument-hint: <slug> [at <time> | in <3h>] [after <slug>] | list | cancel <slug> | answer <slug> <text>
---

The queue is machine-local and shared by every repo (`~/.local/state/agent-runner/queue.json`, items keyed `<repo>/<slug>`).
Cron runs `agent-runner run` every 5 minutes. It
starts Claude only when an item is due, there's no pause, the item has used fewer than 2 attempts, and the Claude
"Current session" usage is below `AGENT_MAX_SESSION_USAGE` (default 70%; above it the queue pauses until the window resets).

Arguments: `$ARGUMENTS`

- **`list`** (or no arguments): `agent-runner queue list`, plus `agent-runner preprod status`. Summarize it for the user.
- **`cancel <slug>`**: `agent-runner queue cancel agent-runner/<slug>`.
- **`answer <slug> <text>`**: `agent-runner queue answer agent-runner/<slug> "<text>"`. Normally the user just
  replies on the inbox issue and the runner picks it up.
- **`<slug> [at|in …] [after <slug>]`**:
  1. Check that `agents/plans/<slug>.md` exists, has a `**Risk:**` line, and has at least one `Runs: unattended` task that isn't ✅.
     If not, stop and say why (suggest `/plan`).
  2. Check that the plan is committed and pushed: `git fetch -q origin && git cat-file -e origin/main:agents/plans/<slug>.md`.
     If it isn't, offer to commit and push it.
  3. Convert the time to `--at YYYY-MM-DDTHH:MM` (VPS local time) or `--in 3h`. Ask if it's ambiguous.
  4. `agent-runner queue add <slug> --repo agent-runner [--at … | --in …] [--after <repo>/<slug>]`. Use `--after` when the plan depends on
     another queued plan, in this repo or another (its `**Depends on:**` line or Context): the item waits until that one is `done` (merged).
  5. Tell the user when the first eligible cron tick is (every 5 minutes), and that low-risk plans may merge on their own while review
     plans get a validator review first. A merge goes live only when the user tags and installs a release.
- If `crontab -l` doesn't contain `agent-runner run`, warn the user that the shared runner isn't in cron yet (the cutover, ROADMAP AM5).
