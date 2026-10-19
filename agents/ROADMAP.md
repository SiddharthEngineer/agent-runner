# agent-runner Roadmap

The tracker for the shared runner and VPS upkeep, and for the engineerfamily Parts of the programme. Strelitzia's two Parts are in
Strelitzia's own ROADMAP (R44, R45). Design: [shared-agent-runner](plans/shared-agent-runner.md). Finished plans move to `agents/plans/done/`.

Status: ⏳ queued · 🔄 in progress · 🟡 code done, user steps pending · ⏸ blocked · ✅ done (then archived)

## Active
All Parts planned on 2026-10-19. Every plan is `low` risk with unattended tasks, and its PR auto-merges after green CI (user). A merge
here goes live only when the user installs a tag. Four plans list paths on `gate.py`'s built-in Strelitzia high-risk list
(`.github/*`, `.claude/settings.json`, `scripts/validate.sh`): A2, A5, A13 and S2. The user's low-risk rule overrides that.

**Who builds what.** The live runner (Strelitzia `scripts/agent/run.sh`) only builds Strelitzia plans, so it can build S1 from its queue.
Everything else is built by Claude sessions, one at a time in this order, until the cutover (AM5). After that, the new runner builds every repo.
Later plans cite the planned interfaces of earlier ones, and each has a Task 0 check that its predecessor really merged.

| ID | Initiative | Plan | Repo | Status | Depends on |
|---|---|---|---|---|---|
| A1 | `runner.toml` loader, prod guard, config/state dirs | [runner-1-config](plans/runner-1-config.md) | agent-runner | ✅ | - |
| A3 | VPS health check and fixed-rule cleanup | [health-1-clean](plans/health-1-clean.md) | agent-runner | ⏳ | - |
| A5 | engineerfamily validate script, PR CI, `make prod-changes` | [ef-validate-ci](plans/ef-validate-ci.md) | engineerfamily | ⏳ | - |
| A2 | `agent-runner` CLI, `install.sh <tag>`, CI, README | [runner-2-install](plans/runner-2-install.md) | agent-runner | ⏳ | A1 |
| A4 | VPS health alerts and read-only diagnosis | [health-2-alert](plans/health-2-alert.md) | agent-runner | ⏳ | A1, A3 |
| A6 | Multi-repo queue keys, inbox projects, shared state | [runner-3-state](plans/runner-3-state.md) | agent-runner | ⏳ | A1, A4 |
| A7 | Global preprod lock driven by `runner.toml` | [runner-4-preprod](plans/runner-4-preprod.md) | agent-runner | ⏳ | A6 |
| A8 | `run.sh` builds every registered repo, dry run, health gate | [runner-5-run](plans/runner-5-run.md) | agent-runner | ⏳ | A7 |
| A9 | Validator: review stage | [runner-6-review](plans/runner-6-review.md) | agent-runner | ⏳ | A8 |
| A10 | Validator: preprod stage | [runner-7-preprod-check](plans/runner-7-preprod-check.md) | agent-runner | ⏳ | A9 |
| A11 | agent-runner builds itself (config, settings, commands, `check-commands`) | [runner-8-self-host](plans/runner-8-self-host.md) | agent-runner | ⏳ | A10 |
| A12 | engineerfamily layout: AGENTS.md, `agents/`, settings | [ef-agents-layout](plans/ef-agents-layout.md) | engineerfamily | ⏳ | A5 |
| A13 | engineerfamily `runner.toml`, agent settings, commands | [ef-runner-config](plans/ef-runner-config.md) | engineerfamily | ⏳ | A12, A11 |
| S1 | Strelitzia `runner.toml`, agent settings (add-only) | Strelitzia R44 [strelitzia-runner-config](https://github.com/SiddharthEngineer/Strelitzia/blob/trunk/agents/plans/strelitzia-runner-config.md) | Strelitzia | ⏳ | A10 |
| — | **Cutover** (AM3-AM5) | design Rollout 2-6 | VPS | ☐ user | A13, S1, A2 installed |
| S2 | Strelitzia commands on the CLI, delete `scripts/agent/` | Strelitzia R45 [strelitzia-runner-cleanup](https://github.com/SiddharthEngineer/Strelitzia/blob/trunk/agents/plans/strelitzia-runner-cleanup.md) | Strelitzia | ⏳ | cutover + 1 week |
| A14 | Drop the Strelitzia-era fallbacks | [runner-9-cleanup](plans/runner-9-cleanup.md) | agent-runner | ⏳ | S2, AM6 |
| A15 | Sonnet triage of blocked items (optional) | [runner-10-triage](plans/runner-10-triage.md) | agent-runner | ⏳ | A11 |

## Manual steps (you)

| ID | Step | Done |
|---|---|---|
| AM0 | Rename `agent-inbox` → `agent-runner` on GitHub. Done 2026-10-19 (user); Claude seeded the repo and pointed `AGENT_INBOX_REPO` at the new name in `~/.config/strelitzia-agent/env`. | ☑ |
| AM1 | After A2 merges: `git -C ~/src/agent-runner pull && git -C ~/src/agent-runner tag v0.1.0 origin/main && git -C ~/src/agent-runner push origin v0.1.0`, then `~/src/agent-runner/install.sh v0.1.0`. Check: `agent-runner version` → `v0.1.0`. Repeat with the next version after later merges you want live. | ☐ |
| AM2 | After A4 merges and is installed (AM1): read `agent-runner health clean --dry-run --pressure` once, then add `*/15 * * * * $HOME/.local/bin/agent-runner health tick` to the `dev` crontab and check `~/.local/state/agent-runner/health.log` after a tick. | ☐ |
| AM3 | Before the cutover, for A10's signed-in checks: sign the dev test account in to `https://preprod.engineerfamily.net/` in a local Playwright browser and save its storage state (`npx playwright open --save-storage=state.json https://preprod.engineerfamily.net/`), then copy it to `~/.config/agent-runner/preprod-storage-state.json` (mode 600). Redo it when the site session expires. Without it, signed-in steps come to you as "left for you". | ☐ |
| AM4 | Cutover config (design Rollout step 4): copy `~/.config/strelitzia-agent/{env,preprod.netrc}` to `~/.config/agent-runner/`, write `repos/{strelitzia,engineerfamily,agent-runner}.env` (`AGENT_REPO=~/src/<repo>`, `PREPROD_NETRC=…`), add `machine preprod.engineerfamily.net` to the netrc, and copy `queue.json` and `runs.jsonl` to `~/.local/state/agent-runner/`. | ☐ |
| AM5 | Cutover (design Rollout steps 2, 3, 5, 6): the dry run, draining the old queue, installing the latest tag, swapping the crontab line to `*/5 * * * * $HOME/.local/bin/agent-runner run`, and the smoke tests in each repo. Rollback: restore the old crontab line. | ☐ |
| AM6 | A week after AM5 with no trouble: queue S2, then delete `~/.config/strelitzia-agent` and `~/.local/state/strelitzia-agent`. | ☐ |

## Decisions (current)
- 2026-10-19 (user): All programme PRs are `low` and auto-merge after green CI. Nothing touches prod. Go-live steps (tags, installs,
  crontab, `~/.config/agent-runner`, the cutover) stay the user's.
- 2026-10-19 (user): Plan everything now. Plans after wave 1 cite planned interfaces, and each checks its predecessor in Task 0.
- 2026-10-19 (user): The old inbox repo is renamed `agent-runner` and holds the runner code too; inbox issues stay in it.
- 2026-10-19 (user): VPS-cleanup agent: fixed cleanup rules in code, plus a read-only Sonnet diagnosis when cleanup isn't enough.
  Clean up at 80% disk, under 400 MB of available RAM, or over 2 GB of swap; alert after cleanup or at 90% disk; load alerts only.
  Backups don't need to be kept (only dumps under 24 h old are spared).
- 2026-10-19: Strelitzia's switch is split: S1 only adds files before the cutover (so the live runner keeps working and can build it),
  and S2 moves the commands to the CLI after it.
