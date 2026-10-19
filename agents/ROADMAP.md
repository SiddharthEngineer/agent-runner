# agent-runner Roadmap

The tracker for the shared runner and VPS upkeep, and for the engineerfamily Parts of the programme until that repo has its own
`agents/`. Design: [shared-agent-runner](plans/shared-agent-runner.md). Finished plans move to `agents/plans/done/`.

Status: ⏳ queued · 🔄 in progress · 🟡 code done, user steps pending · ⏸ blocked · ✅ done (then archived)

## Active
Wave 1 (written 2026-10-19). Every Part runs **interactively**: the live runner (Strelitzia's `scripts/agent/run.sh`) only builds
Strelitzia, and these Parts change the runner itself or `.github/`. A1 and A3 can start now, and so can A5, which is in another repo.

| ID | Initiative | Plan | Risk | Status | Depends on |
|---|---|---|---|---|---|
| A1 | `runner.toml` loader, prod guard, config/state dirs | [runner-1-config](plans/runner-1-config.md) | review | ⏳ | - |
| A2 | `agent-runner` CLI, `install.sh <tag>`, CI, README | [runner-2-install](plans/runner-2-install.md) | review | ⏳ | A1 |
| A3 | VPS health check and fixed-rule cleanup | [health-1-clean](plans/health-1-clean.md) | review | ⏳ | - |
| A4 | VPS health alerts and read-only diagnosis | [health-2-alert](plans/health-2-alert.md) | review | ⏳ | A1, A3 |
| A5 | engineerfamily validate script, PR CI, `make prod-changes` | [ef-validate-ci](plans/ef-validate-ci.md) | review | ⏳ | - |

Later waves (plans not written yet; each is written with `/plan` once the code it builds on has merged): `runner-3-state`,
`runner-4-run`, `runner-5-review`, `runner-6-preprod-check`, `ef-agents-layout`, `ef-runner-config`, `strelitzia-runner-config`,
the cutover, `strelitzia-runner-cleanup`, `runner-7-triage`. Order and scope: the design's Parts table.
Known gap for `runner-4-run`: in this repo, `preprod.sh:47` still calls `$HERE/../validate.sh --preprod` (Strelitzia's script), so
the preprod health check has to move into `preprod.sh`, reading the `[preprod] health` list from `runner.toml`.

## Manual steps (you)

| ID | Step | Done |
|---|---|---|
| AM0 | Rename `agent-inbox` → `agent-runner` on GitHub. Done 2026-10-19 (user); Claude seeded the repo and pointed `AGENT_INBOX_REPO` at the new name in `~/.config/strelitzia-agent/env`. | ☑ |
| AM1 | After A2 merges: `git -C ~/src/agent-runner pull && git -C ~/src/agent-runner tag v0.1.0 origin/main && git -C ~/src/agent-runner push origin v0.1.0`, then `~/src/agent-runner/install.sh v0.1.0`. Check: `agent-runner version` → `v0.1.0`. | ☐ |
| AM2 | After A4 merges: tag and install it as in AM1 (next minor version). Read `agent-runner health clean --dry-run --pressure` once. Then add `*/15 * * * * $HOME/.local/bin/agent-runner health tick` to the `dev` crontab and check `~/.local/state/agent-runner/health.log` after a tick. The env comes from `~/.config/strelitzia-agent/env` until the cutover moves it. | ☐ |

## Decisions (current)
- 2026-10-19 (user): The old inbox repo is renamed `agent-runner` and holds the runner code too; inbox issues stay in it.
- 2026-10-19 (user): VPS-cleanup agent: fixed cleanup rules in code, plus a read-only Sonnet diagnosis when cleanup isn't enough.
  Clean up at 80% disk, under 400 MB of available RAM, or over 2 GB of swap; alert after cleanup or at 90% disk; load alerts only.
  Backups don't need to be kept (only dumps under 24 h old are spared).
- 2026-10-19 (user): Plan in waves: wave 1 written now, later Parts written once their predecessors merge.
