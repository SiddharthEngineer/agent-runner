# AGENTS.md (auto-loaded every session; keep it short)

agent-runner: the unattended build runner and VPS upkeep for SiddharthEngineer's repos (Strelitzia, engineerfamily).
One copy is installed on the VPS (`~/.local/share/agent-runner/current`) and cron runs it. This repo's issues are also
the agent inbox: questions and notices that agents send the user (`inbox.py`, labels `agent-question`/`agent-notice`).

## Map
| Path | What |
|---|---|
| `agent-runner` | The CLI (`~/.local/bin/agent-runner` links to the installed copy): `queue`, `gate`, `inbox`, `intake`, `usage`, `config`, `health`, `preprod`, `versions`, `run`, `version`, `release`, `install` (the last two are the user's) |
| `install.sh` | `install.sh [<tag>]` (`agent-runner install`): mirror, release worktree, validate, switch `current` atomically (the user runs it) |
| `release.sh` | `agent-runner release [vX.Y.Z]`: tag `origin/main` as the next version and push it (the user runs it) |
| `ci.sh` | Shared by both: CI verdict on a commit, newest `vX.Y.Z` tag, version compare |
| `run.sh` | Cron entry (manager): upkeep, pick the next queued plan, run the implementer, gate, merge or send to preprod |
| `buildqueue.py` | Queue state, attempts, pause |
| `gate.py` | Risk gate (plan risk + diff checks); `--plan` checks a plan's size budget |
| `preprod.sh` | Single-occupant preprod lock: deploy a PR, health-check it, release it |
| `inbox.py` | Inbox issues in this repo, plus an ntfy push |
| `intake.py` | `scan`: queue plans pushed to an opted-in repo's `origin/<base>` with `**Queue:** yes` (every tick; refusals as notices) |
| `usage.py` | Claude usage gate (session and weekly caps, weekly pacing, daytime reserve) and `status` |
| `config.py` | `agents/runner.toml` loader and prod guard; config/state dirs |
| `health.py` | VPS health: `check` (disk, memory, swap, load) and `clean` (fixed routine/pressure rules, root-guarded), `tick` (alerts, diagnosis, idle Claude sessions on memory notices), `gate`, `sessions` |
| `settings/` | Headless Claude settings per role: `health-diagnose.json` (read-only diagnosis), `validate.json` (validator default) |
| `prompts/` | Prompts for headless runs: `health-diagnose.md`, `review-pr.md` (validator, review stage), `preprod-check.md` (validator, preprod stage) |
| `versions.py` | Repos with `[release]`: `sync` gives each merged PR the next patch tag and keeps a `vX.Y` pre-release; `promote` (the user's, on prod deploy) makes it full; `status` |
| `prstatus.py` | The status block at the top of every runner PR: `update <key>` after a transition, `sync` each tick |
| `mcp_config.py` | Per-run Playwright MCP config for the preprod stage (basic auth, storage state; 0600, deleted after) |
| `commands_diff.py` | `agent-runner check-commands <repo>…`: how the repos' PIV commands differ |
| `agents/runner.toml`, `agents/settings/`, `.claude/commands/` | This repo as a runner-built repo: its config, headless permissions per role, PIV commands |
| `tests/` | Hermetic pytest (temp state dirs, temp git repos; never the real `~/.local/state`) |
| `agents/ROADMAP.md`, `agents/plans/` | Tracker and plans for this repo, and the cross-repo design `shared-agent-runner.md` |

Seeded on 2026-10-19 from Strelitzia `scripts/agent/` at c4873de. Since the cutover
(2026-10-19) the live cron runs the installed release, so changes here go live only when the user installs a tag.

## Validate
`scripts/validate.sh` → one PASS line per step (`bash -n`, `py_compile`, pytest via `uv run --with pytest`).

## Rules
- Standard library only (Python 3.11+, `tomllib`). bash for the cron entry points.
- Never deploy prod, run `agent-runner release`/`install`/`versions promote`, edit the user's crontab or `~/.config/agent-runner/*`: those are the user's (Manual steps in the ROADMAP).
- Agents never push tags or edit releases. Only the runner's `versions sync` does, for repos with `[release]`.
- Tests never touch the real config or state dirs: set `HOME`/`AGENT_RUNNER_STATE` to a temp dir.
- Never put secrets, personal data or job data in an inbox issue, a plan or a log line.
