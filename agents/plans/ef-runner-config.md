# Plan: engineerfamily registers with the runner (runner.toml, agent settings, PIV commands)
**Status:** ❌ Not started
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19; nothing runs it until the cutover registers the repo)
**Repo:** SiddharthEngineer/engineerfamily (`~/src/engineerfamily`, base `main`, merge commits)
**Depends on:** ef-agents-layout, runner-7-preprod-check (the config schema and validator settings are final by then)

## Goal
engineerfamily becomes buildable by the runner: `agents/runner.toml` (design D5), per-role agent settings, a Playwright MCP config,
and the PIV commands. After the cutover registers it (`~/.config/agent-runner/repos/engineerfamily.env`), `/plan` →
`/schedule-build` → the cron implements, validates on preprod and merges engineerfamily changes. Prod stays manual.

## Context
- Task 0 check: `AGENTS.md` and `agents/` exist on engineerfamily `origin/main` (ef-agents-layout), and agent-runner `origin/main` has
  `prompts/preprod-check.md` (runner-7). If not, `blocked`.
- Design D5: `base`, `merge`, `validate`, the `high_risk` list, and the preprod `deploy`/`release`/`health` values (three Cloudflare
  URLs, `netrc_env`). The schema is `config.py` in agent-runner (`[preprod] app_dir`/`netrc_env` from runner-4-preprod).
- agent-runner `tests/fixtures/engineerfamily.runner.toml` (runner-1-config) is a near-final draft of the file to write; agent-runner's
  `agents/settings/*.json` and `.claude/commands/*` (runner-8-self-host) are the templates to adapt. Read them with
  `git -C ~/src/agent-runner show origin/main:<path>`.
- Agents must never run `make` here, except that the **manager** (not an agent) runs `make preprod-deploy`. The agent settings deny `Bash(make:*)` outright.

## Tasks
### Task 1: `agents/runner.toml`
**Runs:** unattended
**Files:** agents/runner.toml (new)
**What:** Per design D5, with `[preprod] deploy = "make -C /srv/engineerfamily-preprod preprod-deploy ref={sha}"`,
`release = "make -C /srv/engineerfamily-preprod preprod-deploy"`, and health on `https://preprod.engineerfamily.net/` (200),
`/strelitzia/` (200) and `/strelitzia/dashboard/api/health` (`json_ok`). `high_risk` adds `agents/runner.toml`, `agents/settings/*`,
`.claude/settings.json` to D5's list.
**Validate:** `python3 ~/src/agent-runner/config.py check agents/runner.toml` → `ok` (run against agent-runner `origin/main`'s `config.py`).
**Status:** ❌

### Task 2: agent settings and MCP
**Runs:** unattended
**Files:** agents/settings/implement.json (new), agents/settings/validate.json (new), agents/mcp.json (new)
**What:** Adapt agent-runner's: allow `scripts/validate.sh`, `docker compose … config -q`, and `docker run --rm … nginx:alpine nginx -t`
(the commands validate.sh uses); deny `Bash(make:*)`, `Bash(docker compose up:*)`, `Bash(docker compose down:*)`, `Bash(gh workflow:*)`,
`Bash(git tag:*)`, `Read(**/.env)`, `Read(services/nginx/.htpasswd*)`, `Edit(agents/runner.toml)`, `Edit(agents/settings/**)`,
`Edit(.github/**)`. `mcp.json` is the same Playwright server as Strelitzia's.
**Validate:** a one-liner that loads each JSON and asserts `Bash(make:*)` is in `deny` and nothing in `allow` starts with `Bash(make`.
**Status:** ❌

### Task 3: PIV commands
**Runs:** unattended
**Files:** .claude/commands/plan.md (new), .claude/commands/implement.md (new), .claude/commands/schedule-build.md (new), .claude/commands/ship.md (new)
**What:** Copy agent-runner's four commands (runner-8-self-host) and change the repo to `SiddharthEngineer/engineerfamily`, the base to `main`,
the merge to merge commits, and validate to `scripts/validate.sh`. `/ship`'s review branch uses `agent-runner preprod deploy engineerfamily <n>`.
**Validate:** `agent-runner check-commands agent-runner engineerfamily` (if the CLI is installed; else `python3 ~/src/agent-runner/commands_diff.py …`)
→ every command `same`, or differences only in lines that mention preprod.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes; CI is green.
- `python3 ~/src/agent-runner/config.py check agents/runner.toml` → `ok`.

## Human test steps (preprod)
None (low risk). The first real build is the cutover smoke test (design Rollout step 6: a `review` plan changing one nginx comment).

## Decisions
1. The agents can't run `make` at all. Only the manager's preprod commands do, and `config.py` refuses prod targets in them.
