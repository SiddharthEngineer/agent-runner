# agent-runner Roadmap

The tracker for the shared runner and VPS upkeep, and for the engineerfamily Parts of the programme. Strelitzia's two Parts are in
Strelitzia's own ROADMAP (R44, R45). Design: [shared-agent-runner](plans/shared-agent-runner.md). Finished plans move to `agents/plans/done/`.

Status: ⏳ queued · 🔄 in progress · 🟡 code done, user steps pending · ⏸ blocked · ✅ done (then archived)

## Active
All Parts planned on 2026-10-19. Every plan is `low` risk with unattended tasks, and its PR auto-merges after green CI (user). A merge
here goes live only when the user installs a tag. Four plans list paths on `gate.py`'s built-in Strelitzia high-risk list
(`.github/*`, `.claude/settings.json`, `scripts/validate.sh`): A2, A5, A13 and S2. The user's low-risk rule overrides that.

**Cutover 2026-10-19:** cron runs `agent-runner run` (v0.1.4). Fixes found by the smoke tests: #49, #51, #55, #56, #57, #61. Next: engineerfamily E2 (preprod without basic auth), then AM3 (signed-in preprod checks), AM6 closed early on 2026-10-21 and S2 is queued now.

**Who builds what.** The live runner (Strelitzia `scripts/agent/run.sh`) only builds Strelitzia plans, so it can build S1 from its queue.
Everything else is built by Claude sessions, one at a time in this order, until the cutover (AM5). After that, the new runner builds every repo.
Later plans cite the planned interfaces of earlier ones, and each has a Task 0 check that its predecessor really merged.

| ID | Initiative | Plan | Repo | Status | Depends on |
|---|---|---|---|---|---|
| A1 | `runner.toml` loader, prod guard, config/state dirs | [runner-1-config](plans/done/runner-1-config.md) | agent-runner | ✅ | - |
| A3 | VPS health check and fixed-rule cleanup | [health-1-clean](plans/done/health-1-clean.md) | agent-runner | ✅ | - |
| A5 | engineerfamily validate script, PR CI, `make prod-changes` | [ef-validate-ci](plans/done/ef-validate-ci.md) | engineerfamily | ✅ | - |
| A2 | `agent-runner` CLI, `install.sh <tag>`, CI, README | [runner-2-install](plans/done/runner-2-install.md) | agent-runner | ✅ | A1 |
| A4 | VPS health alerts and read-only diagnosis | [health-2-alert](plans/done/health-2-alert.md) | agent-runner | ✅ | A1, A3 |
| A6 | Multi-repo queue keys, inbox projects, shared state | [runner-3-state](plans/done/runner-3-state.md) | agent-runner | ✅ | A1, A4 |
| A7 | Global preprod lock driven by `runner.toml` | [runner-4-preprod](plans/done/runner-4-preprod.md) | agent-runner | ✅ | A6 |
| A8 | `run.sh` builds every registered repo, dry run, health gate | [runner-5-run](plans/done/runner-5-run.md) | agent-runner | ✅ | A7 |
| A9 | Validator: review stage | [runner-6-review](plans/done/runner-6-review.md) | agent-runner | ✅ | A8 |
| A10 | Validator: preprod stage | [runner-7-preprod-check](plans/done/runner-7-preprod-check.md) | agent-runner | ✅ | A9 |
| A11 | agent-runner builds itself (config, settings, commands, `check-commands`) | [runner-8-self-host](plans/done/runner-8-self-host.md) | agent-runner | ✅ | A10 |
| A12 | engineerfamily layout: AGENTS.md, `agents/`, settings | [ef-agents-layout](plans/done/ef-agents-layout.md) | engineerfamily | ✅ | A5 |
| A13 | engineerfamily `runner.toml`, agent settings, commands | [ef-runner-config](plans/done/ef-runner-config.md) | engineerfamily | ✅ | A12, A11 |
| S1 | Strelitzia `runner.toml`, agent settings (add-only) | Strelitzia R44 [strelitzia-runner-config](https://github.com/SiddharthEngineer/Strelitzia/blob/trunk/agents/plans/strelitzia-runner-config.md) | Strelitzia | ✅ (Strelitzia#81) | A10 |
| — | **Cutover** (AM3-AM5) | design Rollout 2-6 | VPS | ✅ 2026-10-19 (AM3 still open: no signed-in preprod checks until it's done) | A13, S1, A2 installed |
| S2 | Strelitzia commands on the CLI, delete `scripts/agent/` | Strelitzia R45 [strelitzia-runner-cleanup](https://github.com/SiddharthEngineer/Strelitzia/blob/trunk/agents/plans/strelitzia-runner-cleanup.md) | Strelitzia | ⏳ queued 2026-10-21 (moved up from 2026-10-26, user) | cutover |
| A14 | Drop the Strelitzia-era fallbacks | [runner-9-cleanup](plans/done/runner-9-cleanup.md) | agent-runner | ✅ #87 | S2 |
| A15 | Sonnet triage of blocked items (optional) | [runner-10-triage](plans/done/runner-10-triage.md) | agent-runner | ✅ PR open 2026-10-19 | A11 |
| A16 | CI fallback without check access, preprod follows each merge, hand-merged PRs | [runner-11-autopilot](plans/done/runner-11-autopilot.md) | agent-runner | ✅ #69 | cutover |
| A17 | `install.sh` skips the test suite when CI passed on the tag | [runner-12-fast-install](plans/done/runner-12-fast-install.md) | agent-runner | ✅ #75 | A16 |
| A18 | Finished plans move to `agents/plans/done/` (`/implement` does it; validators find moved plans; move today's 18) | [plans-done-folder](plans/done/plans-done-folder.md) | agent-runner | ✅ PR open 2026-10-21 | - |

## Manual steps (you)

| ID | Step | Done |
|---|---|---|
| AM0 | Rename `agent-inbox` → `agent-runner` on GitHub. Done 2026-10-19 (user); Claude seeded the repo and pointed `AGENT_INBOX_REPO` at the new name in `~/.config/strelitzia-agent/env`. | ☑ |
| AM1 | After A2 merges: `git -C ~/src/agent-runner pull && git -C ~/src/agent-runner tag v0.1.0 origin/main && git -C ~/src/agent-runner push origin v0.1.0`, then `~/src/agent-runner/install.sh v0.1.0`. Check: `agent-runner version` → `v0.1.0`. Repeat with the next version after later merges you want live. Done 2026-10-19: `v0.1.0` failed its own validation (fixed in #49), and the installed tags went v0.1.1 → v0.1.4. From v0.2.3 on: `agent-runner release --install` (README, "Install and rollback"). | ☑ |
| AM1a | Add agent-runner's CI: commit `.github/workflows/ci.yml` from [runner-2-install](plans/done/runner-2-install.md) "Left for you" (the agents' token has no `workflow` scope, so it can't push workflow files here). Until then PRs here have no checks and merge on a green local `scripts/validate.sh`. Done 2026-10-19. | ☑ |
| AM2 | After A4 merges and is installed (AM1): read `agent-runner health clean --dry-run --pressure` once, then add `*/15 * * * * $HOME/.local/bin/agent-runner health tick` to the `dev` crontab and check `~/.local/state/agent-runner/health.log` after a tick. Done 2026-10-19: the health cron line is in. | ☑ |
| AM3 | After engineerfamily E2 ([preprod-open](https://github.com/SiddharthEngineer/engineerfamily/blob/main/agents/plans/preprod-open.md), preprod without basic auth) is on preprod: save the dev test account's preprod session for A10's signed-in checks at `~/.config/agent-runner/preprod-storage-state.json` (mode 600). Step-by-step, with a fallback if Google blocks the Playwright window and a check: README, "The signed-in session". Redo it every 30 days. Without it, signed-in steps come to you as "left for you". | ☐ |
| AM4 | Cutover config (design Rollout step 4): copy `~/.config/strelitzia-agent/{env,preprod.netrc}` to `~/.config/agent-runner/`, write `repos/{strelitzia,engineerfamily,agent-runner}.env` (`AGENT_REPO=~/src/<repo>`, `PREPROD_NETRC=…`), add `machine preprod.engineerfamily.net` to the netrc, and copy `queue.json` and `runs.jsonl` to `~/.local/state/agent-runner/`. Done 2026-10-19. | ☑ |
| AM5 | Cutover (design Rollout steps 2, 3, 5, 6): the dry run, draining the old queue, installing the latest tag, swapping the crontab line to `*/5 * * * * $HOME/.local/bin/agent-runner run`, and the smoke tests in each repo. Rollback: restore the old crontab line. Done 2026-10-19: smoke tests passed in all three repos (engineerfamily #45 and Strelitzia #84 merged by the runner; agent-runner by [smoke-roadmap-cutover](plans/done/smoke-roadmap-cutover.md)), and the review path ran to preprod on engineerfamily #49 and the second review smoke. | ☑ |
| AM6 | A week after AM5 with no trouble: queue S2, then delete `~/.config/strelitzia-agent` and `~/.local/state/strelitzia-agent`. Done 2026-10-21, two days early (user: no live users): S2 queued now, both dirs deleted after a backup to `~/backups/strelitzia-agent-am6-20261021.tgz`. | ☑ |

## Decisions (current)
- 2026-10-21 (user): close AM6 now instead of waiting the full week; there are no live users, so a break is acceptable.
- 2026-10-19 (user): Everything left in the queue builds with no manual approval: the implementer and validator merge to the base
  and put preprod on it. S2 and A14 are queued (S2 not before 2026-10-26), A14 no longer waits for AM6. Prod deploys and installs stay manual.
- 2026-10-19 (user): All programme PRs are `low` and auto-merge after green CI. Nothing touches prod. Go-live steps (tags, installs,
  crontab, `~/.config/agent-runner`, the cutover) stay the user's.
- 2026-10-19 (user): Plan everything now. Plans after wave 1 cite planned interfaces, and each checks its predecessor in Task 0.
- 2026-10-19 (user): The old inbox repo is renamed `agent-runner` and holds the runner code too; inbox issues stay in it.
- 2026-10-19 (user): VPS-cleanup agent: fixed cleanup rules in code, plus a read-only Sonnet diagnosis when cleanup isn't enough.
  Clean up at 80% disk, under 400 MB of available RAM, or over 2 GB of swap; alert after cleanup or at 90% disk; load alerts only.
  Backups don't need to be kept (only dumps under 24 h old are spared).
- 2026-10-19: Strelitzia's switch is split: S1 only adds files before the cutover (so the live runner keeps working and can build it),
  and S2 moves the commands to the CLI after it.

## Backlog (not yet planned)
- Implementer ↔ validator back-and-forth (user, 2026-10-19; not pressing): let the validator's findings go back to the
  implementer and its fix go back to the validator within one item, for a few rounds, instead of each "changes" costing an
  attempt; also re-queue on a "changes requested" PR review. Moved here from Strelitzia's backlog.
