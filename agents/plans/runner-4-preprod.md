# Plan: one global preprod lock, driven by each repo's `runner.toml`
**Status:** ❌ Not started
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19; nothing installed runs this until the cutover)
**Depends on:** runner-3-state

## Goal
`preprod.sh` serves every repo. Preprod is one environment (the `engineerfamily-preprod` compose project also builds Strelitzia),
so one PR from any repo holds it at a time. The lock records the repo. Deploy, seed, release and health commands come from
that repo's `[preprod]` section in `runner.toml`, read from `origin/<base>`. Health checks go through Cloudflare with the netrc, so they
prove the public host routes correctly. The prod-command guard in `config.load` still applies to every command it runs.

## Context
Line ranges are as of agent-runner `d361143`; `grep -n` the named function if they moved.
- Task 0 check: `buildqueue.py` has `resolve()` and `<repo>/<slug>` keys (runner-3-state). If not, result `blocked` and stop.
- `preprod.sh:9-27` (env config, `configured`), `:29` (`holder`), `:31-45` (`deploy_ref`: fetch into `PREPROD_APP_DIR`, run
  `PREPROD_DEPLOY_CMD`, optional seed, health), `:47` (`health_check` calls `$HERE/../validate.sh --preprod`: Strelitzia's script,
  **broken in this repo**), `:58-113` (`status`, `deploy`, `release`, `tick`), `:115` (sourcing guard used by tests).
- `config.py` (runner-1-config): `RunnerConfig.preprod` (`deploy` with `{sha}`, `release`, `seed`, `health` list), `repo_env(name)`
  (`AGENT_REPO` = dev clone path, `PREPROD_NETRC`), `prod_problems`.
- Today's Strelitzia values: `~/.config/strelitzia-agent/env` `PREPROD_*` (app dir `/srv/Strelitzia-preprod`, deploy
  `make -C /srv/engineerfamily-preprod preprod-strelitzia-up`, seed via `docker compose … run … seed_preprod.py`, health
  `http://127.0.0.1:8080/strelitzia/dashboard/api/health`). engineerfamily values: design D5 (`preprod-deploy ref={sha}`, release
  without `ref`, three Cloudflare URLs).
- `tests/test_runner.py:305-361`: the `preprod` fixture (temp origin + app checkout, stub deploy and health) to extend.

## Tasks
### Task 1: config additions and the 4-column lock
**Runs:** unattended
**Files:** config.py (modify), preprod.sh (modify), tests/test_config.py (modify), tests/test_runner.py (modify)
**What:** `[preprod]` gains optional `app_dir` (a checkout to `git fetch` + `checkout --detach <sha>` before `deploy`, as
`deploy_ref` does today) and `netrc_env` (default `PREPROD_NETRC`). `release` defaults to "deploy at `origin/<base>`". The lock line
is `repo<TAB>pr<TAB>sha<TAB>time`; `holder` prints `repo pr`, and a 3-column line is read as `strelitzia`. All commands take
`<repo> <pr>`; `status` and `tick` need no arguments. `tick` picks the oldest `awaiting-preprod` item across all repos.
**Validate:** `uv run --no-project --with pytest pytest -q tests` → passes. New tests: a 3-column lock reads as `strelitzia`; `deploy engineerfamily 5`
while `strelitzia 3` holds the lock → exit 4 "preprod busy: strelitzia#3"; `app_dir` + `{sha}` substitution in the stubbed command.
**Status:** ✅ 2026-10-19

### Task 2: commands from `runner.toml`
**Runs:** unattended
**Files:** preprod.sh (modify), tests/test_runner.py (modify)
**What:** `load_repo <repo>`: source `repo_env` (via `agent-runner config` or `python3 "$HERE/config.py"`), `git -C "$AGENT_REPO" fetch -q`,
`git show origin/<base>:agents/runner.toml` to a temp file, then `config.py check` (a failure → exit 3 with the problems, no
deploy). Read `deploy`, `seed`, `release`, `health` and `github` with `config.py get`. `GH_REPO` comes from the config, not the env.
Seed runs only when configured (no more `scripts/seed_preprod.py` existence check: the repo's config decides). The old env
variables are no longer read.
**Validate:** tests with a temp repo whose `origin/trunk` has a fixture `runner.toml`: `deploy strelitzia 7` runs the
fixture's deploy with the PR head sha; a fixture whose `deploy` says `deploy-prod` → exit 3 and nothing runs.
**Status:** ✅ 2026-10-19 (committed with Task 1: the new lock and the config-driven commands are one rewrite of preprod.sh)

### Task 3: health checks through Cloudflare
**Runs:** unattended
**Files:** preprod.sh (modify), tests/test_runner.py (modify), README.md (modify)
**What:** Replace `health_check` with a loop over the `health` list: `curl -sS --max-time 20 --netrc-file "$netrc"` per URL;
`status` entries compare the HTTP code, and `json_ok` entries need `"ok": true` in the body. Retry 6 times, 10 s apart, before
failing. On failure print which URL and what it returned (never the body beyond 300 characters). README: the preprod section
rewritten for multi-repo and `runner.toml`.
**Validate:** tests with a local `python3 -m http.server`-style stub (in the test, on a free port): 200 passes; 502 fails after
the retries (sleep stubbed); `json_ok` with `{"ok": false}` fails. `scripts/validate.sh` → all PASS.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes.
- `bash -n preprod.sh`; `grep -c 'validate.sh --preprod' preprod.sh` → `0`.

## Human test steps (preprod)
None (low risk). The real preprod is first exercised in the cutover smoke test (design Rollout step 6).

## Decisions
1. Commands are read from `origin/<base>`, never from a PR branch, so a PR can't change how it gets deployed.
2. The lock stays global across repos (design D1): both repos share one preprod.
