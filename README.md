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
agent-runner version                    # → v0.2.0
~/src/agent-runner/install.sh --list    # installed releases; * is active
~/src/agent-runner/install.sh v0.1.0    # rollback: switch back to an installed (or any) older tag
```
`install.sh` keeps a bare mirror at `~/.local/share/agent-runner/mirror.git`, one detached worktree per release under
`releases/<tag>`, and `current -> releases/<tag>` (switched atomically, only after the release's own `scripts/validate.sh`
passes). `~/.local/bin/agent-runner` points at `current/agent-runner`. The newest 3 releases and the active one are kept.
Agents never push tags or run `install.sh`.

## CLI
`agent-runner <command> [args…]`; the arguments pass straight through to the script.

| Command | Script | What |
|---|---|---|
| `queue` | `buildqueue.py` | Queue state, attempts, pause |
| `gate` | `gate.py` | Risk gate: the plan's `**Risk:**` line plus diff checks decide auto-merge vs review; `--plan` checks a plan's size budget; `--config` takes a repo's `runner.toml` |
| `inbox` | `inbox.py` | Questions and notices as issues in this repo, plus an ntfy push |
| `usage` | `usage.py` | Claude session-usage gate |
| `config` | `config.py` | `check`/`get` a repo's `agents/runner.toml` (refuses prod commands) |
| `health` | `health.py` | VPS health `check` and fixed-rule `clean` |
| `preprod` | `preprod.sh` | Single-occupant preprod lock: deploy a PR, health-check it, release it |
| `run` | `run.sh` | The cron entry (manager) |
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
Until the cutover the live cron runs Strelitzia's own `scripts/agent/run.sh`.

## Limits (so it can't run away)
- Claude starts only if an item is due, the queue isn't paused, and the item has used fewer than 2 attempts (`AGENT_MAX_ATTEMPTS`).
  There's no daily run cap by default (`AGENT_MAX_RUNS_PER_DAY=0`; set a number to bring one back).
- Session-usage gate: before each run, `usage.py` sends one tiny Haiku prompt and reads the "Current session" (5-hour window)
  usage from its `rate_limit_event`. At or above `AGENT_MAX_SESSION_USAGE` (default `70`, percent; `0` turns it off) the run
  doesn't start and the queue pauses until the window resets. If the usage can't be read, that tick is skipped.
  It only gates starts: a run already going finishes even if it pushes usage past the limit.
- Merging: `low` plans that pass `gate.py` merge after green CI; the rest wait for review on preprod.
  A PR with red CI stays open and you get a notice.
- After each run the Docker build cache is pruned down to `AGENT_BUILD_CACHE_KEEP` (default `3GB`), so a long chain can't fill the disk.
- Each run is killed after `AGENT_MAX_RUNTIME` (3h). A `flock` allows one Claude run at a time, whatever the repo or role.
- Context cap: runs auto-compact at 200k tokens (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`; override with `AGENT_COMPACT_WINDOW`).
- A usage-limit exit pauses the queue for 5h, gives the attempt back, and the next run resumes that session (`resume=limit`).
- A run that ends without a result file, or whose `ready` fails the repo's full validation, counts as a failed attempt. After 2
  failures the item is `blocked` and you get a notice.

## Day to day
- Plan with Claude (`/plan`), then queue it (`/schedule-build <slug> [at 02:00] [after <slug>]`).
- Logs: `runs.log` (one line per event) and `logs/<time>-<slug>.log` (full JSON output) in the state dir.
- When an agent asks a question, reply on the inbox issue. The next tick re-queues the item with your answer and resumes the
  agent's own session.

## Develop
`scripts/validate.sh` (`bash -n`, `py_compile`, hermetic pytest via `uv`) runs locally and in CI (`.github/workflows/ci.yml`, once it is added: ROADMAP AM1a).
Standard library only.

## VPS health (`health.py`)
The VPS runs prod, preprod and every agent run, so a full disk or memory pressure breaks them all at once. `health.py` measures
the box cheaply (no Claude) and frees disk with fixed rules. Alerts and the cron line come with health-2-alert.

```
python3 health.py check                        # JSON {"metrics": …, "problems": […]}; exit 1 when there are problems
python3 health.py clean --dry-run --pressure   # every rule and what it would free; deletes nothing
python3 health.py clean                        # routine rules (once a day), plus pressure rules while disk is over 80%
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
