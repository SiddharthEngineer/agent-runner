# agent-runner

The unattended build runner and VPS upkeep for SiddharthEngineer's repos (Strelitzia, engineerfamily, and this one). A
deterministic manager (`run.sh`, bash and Python, no Claude on a normal tick) picks queued plans, starts a Claude implementer in a
worktree, gates the PR, and merges it or sends it to preprod. Prod is never deployed by the runner. This repo's issues are also
the agent inbox. Design: [agents/plans/shared-agent-runner.md](agents/plans/shared-agent-runner.md).

## Install and rollback (you)
One copy is installed on the VPS, and cron and every repo call it, so runner changes go live only when you install a tag:
```bash
git -C ~/src/agent-runner pull && git -C ~/src/agent-runner tag v0.2.0 origin/main && git -C ~/src/agent-runner push origin v0.2.0
~/src/agent-runner/install.sh v0.2.0    # mirror, check out releases/v0.2.0, run its validate.sh, then switch current
~/src/agent-runner/install.sh --full v0.2.0  # same, but always run the full test suite
agent-runner version                    # → v0.2.0
~/src/agent-runner/install.sh --list    # installed releases; * is active
~/src/agent-runner/install.sh v0.1.0    # rollback: switch back to an installed (or any) older tag
```
`install.sh` keeps a bare mirror at `~/.local/share/agent-runner/mirror.git`, one detached worktree per release under
`releases/<tag>`, and `current -> releases/<tag>` (switched atomically, only after the release's own `scripts/validate.sh`
passes). `~/.local/bin/agent-runner` points at `current/agent-runner`. The newest 3 releases and the active one are kept.
If CI's run on the tag's exact commit succeeded (`gh run list`), install runs only `validate.sh --quick` (`bash -n`,
`py_compile`) and takes seconds; with no `gh`, no run, a run still going or failed, or any `gh` error, it runs the full suite
and says why. `--full` always runs the full suite.
Agents never push tags or run `install.sh`.

## CLI
`agent-runner <command> [args…]`; the arguments pass straight through to the script.

| Command | Script | What |
|---|---|---|
| `queue` | `buildqueue.py` | Queue state, attempts, pause |
| `gate` | `gate.py` | Risk gate: the plan's `**Risk:**` line plus diff checks decide auto-merge vs review; `--plan` checks a plan's size budget; `--config` takes a repo's `runner.toml` |
| `inbox` | `inbox.py` | Questions and notices as issues in this repo, plus an ntfy push; `sweep` closes notices of finished items |
| `usage` | `usage.py` | Claude session-usage gate |
| `config` | `config.py` | `check`/`get` a repo's `agents/runner.toml` (refuses prod commands) |
| `health` | `health.py` | VPS health `check` and fixed-rule `clean` |
| `preprod` | `preprod.sh` | Single-occupant preprod lock: deploy a PR, health-check it, release it |
| `run` | `run.sh` | The cron entry (manager) |
| `check-commands` | `commands_diff.py` | How the repos' `.claude/commands` differ once repo names, base branches and validate commands are swapped for placeholders |
| `version` | | The installed tag |

## Config and state
- `~/.config/agent-runner/` (yours; agents and the runner only read it): `env` for shared secrets and settings (Claude token,
  `AGENT_INBOX_REPO`, `NTFY_TOPIC`), `repos/<name>.env` for machine-local per-repo values (`AGENT_REPO=<dev clone>`,
  `PREPROD_NETRC=…`), and `health.toml` for health threshold overrides. For one release the runner falls back to
  `~/.config/strelitzia-agent/env`.
- Each repo describes itself in its own `agents/runner.toml` (base branch, merge method, validate commands, risk patterns,
  preprod commands), read from `origin/<base>`. Check one with `agent-runner config check agents/runner.toml`.
- `~/.local/state/agent-runner/` (or `$AGENT_RUNNER_STATE`): `queue.json`, locks, `runs.log`, `logs/`, `results/`,
  `worktrees/<repo>/<slug>`, and `health.json`/`health.log`.

## Cron (added at the cutover)
Nothing here edits the crontab. The lines you add (ROADMAP manual steps AM2 and AM5):
```
*/15 * * * * $HOME/.local/bin/agent-runner health tick
*/5 * * * * $HOME/.local/bin/agent-runner run
```
Since the cutover (2026-10-19) these two lines are the live cron; Strelitzia's old `scripts/agent/run.sh` line is commented out
until S2 deletes it.

## Limits (so it can't run away)
- Claude starts only if an item is due, the queue isn't paused, and the item has used fewer than 2 attempts (`AGENT_MAX_ATTEMPTS`).
  There's no daily run cap by default (`AGENT_MAX_RUNS_PER_DAY=0`; set a number to bring one back).
- Session-usage gate: before each run, `usage.py` sends one tiny Haiku prompt and reads the "Current session" (5-hour window)
  usage from its `rate_limit_event`. At or above `AGENT_MAX_SESSION_USAGE` (default `70`, percent; `0` turns it off) the run
  doesn't start and the queue pauses until the window resets. If the usage can't be read, that tick is skipped.
  It only gates starts: a run already going finishes even if it pushes usage past the limit.
- Merging: `low` plans that pass `gate.py` merge after green CI; the rest get a validator review, then preprod checks
  if the repo has `[preprod]`. A `low` PR whose CI is red or times out, whose repo reports no CI checks at all, or whose
  merge fails stays open, labelled `ci-failed`; the item is `blocked` and you get a notice with the PR link. Nothing
  merges without checks.
- CI fallback: green CI is `gh pr checks --watch` first (it also sees non-Actions checks). When the token can't read the
  check rollup ("Resource not accessible", as on agent-runner), the runner watches the Actions runs of the PR head instead
  (`gh run list --commit <sha>`, then `gh run watch --exit-status` each): all green merges, one red is "CI is red", and no
  run within 30 min (`AGENT_CI_WAIT`, seconds) is "no CI checks".
- Preprod follows each merge: after any merge (low, or after the validator), a repo with `[preprod]` gets
  `preprod.sh release <repo>`, so preprod runs the new base, unless another PR holds the lock (its release puts the base back
  later). One `runs.log` line either way; a failed redeploy sends a notice and leaves the item `done`.
- Hand merges: before the review or preprod stage acts on an item, the runner reads its PR's state. Merged by hand → `done`
  ("merged by hand"), preprod put on the base; closed → `cancelled` (preprod released if it held it). No validator run.
- After each run the Docker build cache is pruned down to `AGENT_BUILD_CACHE_KEEP` (default `3GB`), so a long chain can't fill the disk.
- Each run is killed after `AGENT_MAX_RUNTIME` (3h). A `flock` allows one Claude run at a time, whatever the repo or role.
- Context cap: runs auto-compact at 200k tokens (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`; override with `AGENT_COMPACT_WINDOW`).
- A usage-limit exit pauses the queue for 5h, gives the attempt back, and the next run resumes that session (`resume=limit`).
- A run that ends without a result file, or whose `ready` fails the repo's full validation, counts as a failed attempt. After 2
  failures the item is `blocked` and you get a notice.
- **Triage** (once per item): when an item turns `blocked` (2 failed attempts, or the review validator's `blocked` or no verdict twice), one read-only
  Sonnet run (15 min at most, [prompts/triage.md](prompts/triage.md), [settings/triage.json](settings/triage.json): reading,
  `tail`/`grep`/`ls`, `gh pr view/checks/diff`, `gh run view`; no writes) reads its last two logs, last result and PR, and
  classifies the cause: `transient` | `fixed-elsewhere` | `plan-problem` | `code-problem` | `environment`. The first two
  re-queue the item with fresh attempts and a note (a validator block goes back to the validator), and you get a short
  "re-queued after triage" notice; anything else gives
  the usual blocked notice with the diagnosis appended. It never edits code or merges. The item gets `triaged=1`, so a second
  block always goes straight to you. If the usage gate says no, you get the plain notice. Turn it off with `AGENT_TRIAGE=0`
  in `~/.config/agent-runner/env`. Its log is `logs/<time>-<repo>-<slug>.triage.log`.

## Preprod (`preprod.sh`)
Preprod is one environment for every repo (the `engineerfamily-preprod` compose project also builds Strelitzia), so one PR from
any repo holds it at a time:
```
agent-runner preprod status                     # the holder; releases the lock when its PR was merged or closed
agent-runner preprod deploy engineerfamily 41   # take the lock, deploy the PR head, health-check it (exit 4 if busy)
agent-runner preprod release [<repo>]           # drop the lock, put preprod back on the repo's base branch
agent-runner preprod tick                       # status, then deploy the oldest awaiting-preprod item of any repo
```
The lock (`preprod.lock` in the state dir) is `repo<TAB>pr<TAB>sha<TAB>time`; a 3-column line from the old runner is read as
`strelitzia`. Each repo's commands come from the `[preprod]` section of its `agents/runner.toml`, read from `origin/<base>` of
the dev clone named in `~/.config/agent-runner/repos/<repo>.env` (`AGENT_REPO`), never from the PR branch, and checked by the prod
guard before anything runs:
```toml
[preprod]
app_dir = "/srv/Strelitzia-preprod"   # optional: fetched and checked out at the PR sha before deploy
deploy = "make -C /srv/engineerfamily-preprod preprod-deploy ref={sha}"   # {sha} = the PR head (or the base, on release)
release = "make -C /srv/engineerfamily-preprod preprod-deploy"           # optional; default: deploy again at origin/<base>
seed = "…"                            # optional: runs after every deploy
netrc_env = "PREPROD_NETRC"           # the repos/<repo>.env variable with the netrc for basic auth (the default)
health = [{ url = "https://preprod.engineerfamily.net/", status = 200 },
          { url = "https://preprod.engineerfamily.net/strelitzia/dashboard/api/health", json_ok = true }]
```
Health checks go through Cloudflare with the netrc, so they prove the public host routes to the right nginx. Each URL gets
6 tries, 10 s apart; a failure prints the URL, the HTTP code and at most 300 characters of the body, and the deploy is rolled back.

## How a tick works (`run.sh`)
Every 5 minutes, for every registered repo, with at most one Claude run at a time (a `flock` on `run.lock`):
1. **Upkeep:** inbox replies (`inbox.py poll`), closing the notices of items that are `done` or `cancelled`
   (`inbox.py sweep`), and the global preprod lock (`preprod.sh tick`).
2. **Health gate:** while `health.py gate` says disk is critical, no Claude run starts (upkeep above still ran).
3. **Next item:** `buildqueue.py next` gives a due `<repo>/<slug>`; then the session-usage gate.
4. **The repo:** `AGENT_REPO` from `~/.config/agent-runner/repos/<repo>.env`. Its `agents/runner.toml` and the plan are read from
   `origin/<base>`; a missing plan, a missing config or one the prod guard refuses blocks the item with a notice.
5. **Implementer:** `/implement <plans>/<slug>.md --unattended` in `worktrees/<repo>/<slug>` on `agent/<slug>`, with the worktree's
   `agents/settings/implement.json` and `agents/mcp.json` (or `scripts/agent/settings.json`/`mcp.json` for a repo not yet on that layout).
6. **Merge or review:** the repo's `validate.full` again, `gate.py --config`, then `low` + green CI → `gh pr merge --<merge>`
   (`rebase`, `merge` or `squash`, from the config). A `review` tier goes to `awaiting-review` for the validator; a `low` PR
   whose CI isn't green gets `awaiting-preprod` and waits for you.

**Item lifecycle:** `queued → running → done` for `low` plans; `running → awaiting-review → (validator) → awaiting-preprod →
on-preprod → done` for `review` plans (a repo with no `[preprod]` merges straight after the validator approves). Also
`waiting-answer` (a question in the inbox), `blocked` and `cancelled`.
**Notices close themselves.** Each tick, an open `agent-notice` issue titled `[<key>] …` is closed (with a comment) once that
item is `done` or `cancelled`. A "merged; N steps left for you" notice with steps stays open for you to close. Health notices
(`[vps]`) are closed by `health.py` on recovery. If you finish a blocked item by hand, `agent-runner queue set <key> status=done`
closes its notices.

**Validator, review stage.** Each tick, before any new implementer run, the oldest `awaiting-review` item gets one
fresh-context Opus run (1 h at most) in a detached checkout of the PR head (`worktrees/<repo>/review-<pr>`, removed after). It
never sees the implementer's session: its prompt ([prompts/review-pr.md](prompts/review-pr.md)) is the plan, the PR diff and the
repo's full validation. It runs `/code-review`, posts one `gh pr review --comment` (GitHub won't let the runner's account
approve its own PR) and writes `results/<repo>/<slug>.review.json` (`approve | changes | blocked`, a summary, findings).
- `approve` → `awaiting-preprod`, or merged after green CI when the repo has no preprod.
- `changes` → re-queued for the implementer with the review as a note, resuming its session. It costs an attempt, so out of
  attempts it is `blocked` and you get a notice.
- `blocked`, or no verdict twice → `blocked`, with a notice.
**Validator, preprod stage.** Once an approved `review` PR is `on-preprod` (`preprod.sh tick` deployed it), the next tick runs
one Sonnet validator (45 min at most) with [prompts/preprod-check.md](prompts/preprod-check.md). It works through the plan's
Human test steps on the public preprod host: `curl` with the repo's netrc, and the Playwright MCP, whose browser gets the
basic-auth credentials and, if `~/.config/agent-runner/preprod-storage-state.json` exists, the dev test account's session
(ROADMAP AM3). Both go in a 0600 temp config (`mcp_config.py`) that is deleted after the run. It never signs in itself and never
types a password. Steps that need your own account, prod, email, money, a device, or a sign-in it doesn't have are
"left for you". It comments a checklist on the PR and writes `results/<repo>/<slug>.preprod.json`.
- `approve` → merged after green CI, preprod on the new base branch, and one notice "`[<key>] merged; N steps left for you`"
  listing the steps only you can do (left-for-you steps never block the merge).
- `changes` → preprod released and the item re-queued for the implementer with the failed steps as a note (costs an attempt).
- `blocked`, or no verdict twice → preprod released, item `blocked`, notice.
**Refresh the session** when it expires (about 30 days), as in AM3: sign the dev test account in with
`npx playwright open --save-storage=state.json https://preprod.engineerfamily.net/`, then copy `state.json` to
`~/.config/agent-runner/preprod-storage-state.json` (mode 600). Without it the validator still runs and leaves signed-in steps to you.

Its permissions ([settings/validate.json](settings/validate.json), or the repo's `agents/settings/validate.json`) allow reading,
the validate commands, `gh pr diff/view/checks` and `gh pr review --comment`, and writing only its result file. They deny
edits, commits, pushes, merges, `make`, `docker compose` and `sudo`. Its log is `logs/<time>-<repo>-<slug>.review.log`.

**Dry run:** `AGENT_DRY_RUN=1 agent-runner run` logs each decision to `runs.log` ("dry run: would run claude for
engineerfamily/x in …", "dry run: nothing due") and exits before the inbox poll, preprod, `claude`, merges and queue changes.
Rehearse the cutover against a copy of the old queue: `T=$(mktemp -d); cp ~/.local/state/strelitzia-agent/queue.json $T/;
AGENT_RUNNER_STATE=$T AGENT_DRY_RUN=1 agent-runner run; tail $T/runs.log`.

## Day to day
- Plan with Claude (`/plan`), then queue it (`/schedule-build <slug> [at 02:00] [after <slug>]`).
- **Queue keys.** One queue serves every repo. Items are keyed `<repo>/<slug>` (`strelitzia/site-sso-3-proxy-mode`,
  `engineerfamily/ef-agents-layout`): `agent-runner queue add <slug> --repo engineerfamily` (default `$AGENT_DEFAULT_REPO`,
  `strelitzia`). `--after` takes `repo/slug`, so a plan can wait for a merge in another repo; a bare slug means the same repo.
  Every other command takes the full key, or a bare slug when only one repo has an item with it (several → it exits and names them).
  Inbox issues are labelled with the item's repo.
- **Migration.** A `queue.json` from the old runner (bare keys) is rewritten once on first load: `<slug>` becomes
  `$AGENT_DEFAULT_REPO/<slug>`, and so does a bare `after`. State is in `$AGENT_RUNNER_STATE` (default
  `~/.local/state/agent-runner`); `$STRELITZIA_AGENT_STATE` still works as an alias until the cutover.
- Logs: `runs.log` (one line per event) and `logs/<time>-<slug>.log` (full JSON output) in the state dir.
- When an agent asks a question, reply on the inbox issue. The next tick re-queues the item with your answer and resumes the
  agent's own session.

## Develop
`scripts/validate.sh` (`bash -n`, `py_compile`, hermetic pytest via `uv`) runs locally and in CI (`.github/workflows/ci.yml`, once it is added: ROADMAP AM1a).
Standard library only.

## VPS health (`health.py`)
The VPS runs prod, preprod and every agent run, so a full disk or memory pressure breaks them all at once. `health.py` measures
the box cheaply (no Claude), frees disk with fixed rules, and tells you when that isn't enough.

```
python3 health.py check                        # JSON {"metrics": …, "problems": […]}; exit 1 when there are problems
python3 health.py clean --dry-run --pressure   # every rule and what it would free; deletes nothing
python3 health.py clean                        # routine rules (once a day), plus pressure rules while disk is over 80%
python3 health.py tick                         # cron: clean, re-measure, notify (below)
python3 health.py gate                         # exit 1 while disk is at or over disk_alert_pct (run.sh starts no Claude run then)
python3 health.py diagnose [--force]           # the read-only diagnosis on its own; --force ignores the 6 h spacing
```

**Thresholds** (override any of them in `~/.config/agent-runner/health.toml`; an unknown key is an error):
```toml
[thresholds]
disk_clean_pct = 80      # clean up at or over this disk use
disk_alert_pct = 90      # alert at or over this, whatever cleanup does
mem_available_mb = 400   # MemAvailable under this is a problem (alert only)
swap_used_mb = 2048      # swap used over this is a problem (alert only)
load_per_cpu = 2.0       # 15-minute load per CPU over this is a problem (alert only)
```

**Routine rules** (at most once every 23 h): Docker build cache down to 3 GB (`builder prune --keep-storage 3GB`), exited
containers older than 24 h, runner logs older than 14 days and results older than 30 days (both the `agent-runner` and the old
`strelitzia-agent` state dirs), runner worktrees older than 7 days whose branch is gone from origin, your own top-level `/tmp`
entries untouched for 3 days (never `/tmp/claude-*`), and backups in `~/backups` and `/srv/backups` older than 7 days.

**Pressure rules** (only while disk is over `disk_clean_pct`, or with `--pressure`; in this order, re-measuring after each and
stopping once under): all build cache, images unused for 7 days, backups older than 24 h, `uv cache prune`, `pip cache purge`,
and all but the newest Playwright build of each browser.

**Never touched:** Docker volumes (`volume` and `system prune` are never called), running or compose-managed containers' images,
`/srv/*` checkouts, any `.env` outside the backup dirs, databases, `/var/log` and the journal, and processes. Memory and load
never trigger a deletion. Every rule deletes only strictly inside its own root and skips symlinks; a refused path is logged.

**`health.log`** (in the state dir, `~/.local/state/agent-runner/`) has one JSON line per rule run:
`{"time", "rule", "tier", "dry_run", "freed_bytes"}`, plus `"error"` when the rule failed (the next rule still runs), and
`"rule": "refused"` lines for paths a guard skipped. `health.json` next to it records when the routine rules last ran.

**Alerts (`health tick`).** Each tick (its own lock, so ticks never overlap) runs `clean`, measures again, and compares the problems
with the open ones in `health.json`. A new problem opens one `agent-notice` issue in this repo labelled `vps-health`
(`[vps] disk 91% used (alert at 90%) after cleanup`) plus an ntfy push. The body has the metrics, what this tick's cleanup freed,
and the diagnosis. While it lasts, the issue gets at most one comment a day. When it clears, it gets a "Recovered" comment and is
closed. Load counts only when it was also high on a tick at least 15 minutes earlier. A failed `gh` call is logged and retried on
the next tick.

**Diagnosis.** Before the first notice of a problem, a short Sonnet run (`claude-sonnet-5-5`, 10 minutes, 15 turns,
`--permission-mode dontAsk` with [settings/health-diagnose.json](settings/health-diagnose.json)) looks around with `df`, `du`, `free`,
`ps`, `ls` and `docker system df/ps/images`, and writes what uses the space or memory and the commands you could run. It can't
edit, delete, kill or sudo. It is skipped (the notice goes out without it) when there is no `CLAUDE_CODE_OAUTH_TOKEN`, the
session-usage gate is over its limit, a build run holds a run lock, the last diagnosis was under 6 hours ago, or disk is at 95% or more.
The prompt is [prompts/health-diagnose.md](prompts/health-diagnose.md).

**Cron (you, ROADMAP AM2):** `*/15 * * * * $HOME/.local/bin/agent-runner health tick`, separate from the build runner's line so
upkeep keeps working when the runner is broken or paused.
