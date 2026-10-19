# Unattended build runner

Cron every 5 minutes → `run.sh`. It checks for due work with plain bash/Python and starts `claude -p` only when a queued plan is due.

| File | Role |
|---|---|
| `run.sh` | Cron entry: upkeep (inbox replies, preprod lock), pick the next item, run `/implement --unattended` in a worktree, re-validate, then merge or send to review |
| `buildqueue.py` | Queue state, attempt and daily caps, pause (`~/.local/state/strelitzia-agent/`) |
| `gate.py` | Risk gate: the plan's `**Risk:**` line plus diff checks decide auto-merge vs review (`--plan`: checks the plan's `**Files:**` lines, used by `/plan`) |
| `preprod.sh` | Single-occupant preprod lock: deploy a PR, health-check it, release it on merge or close |
| `inbox.py` | Questions and notices as issues in a private repo, plus an ntfy push |
| `settings.json`, `mcp.json` | Permissions and the Playwright MCP for headless runs (the agent cannot merge, deploy or edit these) |

## Limits (so it can't run away)
- Claude starts only if an item is due, the queue isn't paused, and the item has used fewer than 2 attempts (`AGENT_MAX_ATTEMPTS`).
  There's no daily run cap by default (`AGENT_MAX_RUNS_PER_DAY=0`; set a number to bring one back).
- Session-usage gate: before each run, `usage.py` sends one tiny Haiku prompt and reads the "Current session" (5-hour window)
  usage from its `rate_limit_event`. At or above `AGENT_MAX_SESSION_USAGE` (default `70`, percent; `0` turns it off) the run
  doesn't start and the queue pauses until the window resets. If the usage can't be read, that tick is skipped.
  It only gates starts: a run already going finishes even if it pushes usage past the limit.
- Merge policy: by default (`AGENT_MERGE_POLICY=gate`) only `low` plans that pass `gate.py` merge after green CI; the rest wait for preprod and you.
  `AGENT_MERGE_POLICY=all` merges every ready PR after green CI, also merges PRs already waiting for review once their CI is green,
  and redeploys preprod from trunk after each merge (`preprod.sh release`). A PR with red CI stays open and you get a notice.
- After each run the Docker build cache is pruned down to `AGENT_BUILD_CACHE_KEEP` (default `3GB`), so a long chain can't fill the disk.
- Each run is killed after `AGENT_MAX_RUNTIME` (3h). A `flock` allows one run at a time.
- Context cap: runs auto-compact at 200k tokens (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`; override with `AGENT_COMPACT_WINDOW`).
  Opus's 1M window otherwise lets a long run re-send 250–380k tokens every turn.
- A usage-limit exit pauses the queue for 5h, gives the attempt back, and the next run resumes that session (`resume=limit`).
- A run that ends without a result file, or whose `ready` fails `validate.sh --full`, counts as a failed attempt. After 2 failures the item is `blocked` and you get a notice.

## One-time setup (you)
Paths: the runner uses `~/.config/strelitzia-agent` and `~/.local/state/strelitzia-agent`. engineerfamily's compose service is `strelitzia`.

1. Create the private inbox repo: `gh repo create SiddharthEngineer/agent-inbox --private --description "Agent questions and notices"`.
2. Install the ntfy app and subscribe to a hard-to-guess topic (e.g. `strelitzia-<random>`).
3. Run `claude setup-token` and write `~/.config/strelitzia-agent/env` (mode 600):
   ```bash
   export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
   AGENT_REPO=/home/dev/src/Strelitzia   # dev clone the runner makes worktrees from
   AGENT_INBOX_REPO=SiddharthEngineer/agent-inbox
   NTFY_TOPIC=strelitzia-<random>
   PREPROD_APP_DIR=/srv/Strelitzia-preprod
   PREPROD_DEPLOY_CMD="make -C /srv/engineerfamily-preprod preprod-strelitzia-up"
   PREPROD_HEALTH_URL=http://127.0.0.1:8080/strelitzia/dashboard/api/health
   # Optional: wipe and reseed the preprod DB (scripts/seed_preprod.py) after every preprod deploy. See agents/DEPLOY.md.
   PREPROD_SEED_CMD="docker compose -p engineerfamily-preprod --project-directory /srv/engineerfamily-preprod -f /srv/engineerfamily-preprod/docker-compose.base.yml -f /srv/engineerfamily-preprod/docker-compose.preprod.yml run --rm --no-deps -T -v /srv/Strelitzia-preprod:/seed:ro strelitzia python /seed/scripts/seed_preprod.py"
   PREPROD_NETRC=/home/dev/.config/strelitzia-agent/preprod.netrc   # mode 600: machine 127.0.0.1 login … password … (preprod basic auth)
   ```
4. Install the cron line (`crontab -e`): `*/5 * * * * /home/dev/src/Strelitzia/scripts/agent/run.sh`

## Day to day
- Plan with Claude (`/plan`), then queue it (`/schedule-build <slug> [at 02:00] [after <slug>]`). `/schedule-build list` shows the queue and the preprod holder.
  `after <slug>` (`buildqueue.py add <slug> --after <other>`) keeps an item waiting until the other one is `done` (merged), for plans that build on each other.
- Logs: `~/.local/state/strelitzia-agent/runs.log` (one line per event) and `logs/<time>-<slug>.log` (full JSON output).
- When an agent asks a question, reply on the inbox issue. The next tick (within 5 minutes) re-queues the item with your answer.
  That run resumes the agent's own session (`claude --resume`), or starts fresh if Claude can't find it.
- Merging or closing the PR on preprod frees the lock. The next tick resets preprod to trunk and deploys the next waiting PR.

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
