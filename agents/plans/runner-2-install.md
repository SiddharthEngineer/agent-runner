# Plan: `agent-runner` CLI, `install.sh <tag>`, CI and README
**Status:** ❌ Not started
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19. Was review: adds `.github/` and decides what code cron runs)
**Depends on:** runner-1-config

## Goal
One installed copy of the runner on the VPS that every repo and cron line calls (design D2). The user runs
`./install.sh v0.2.0`. That makes `~/.local/share/agent-runner/current` a detached checkout of that tag and `~/.local/bin/agent-runner`
a CLI on PATH (`agent-runner queue list`, `agent-runner gate <plan> --plan`, `agent-runner health tick`, …). Runner changes go
live only when the user installs a new tag. Nothing is added to cron here. The repo also gets CI, so PRs show green or red.

## Context
- Entry points to dispatch: `buildqueue.py`, `gate.py`, `inbox.py`, `usage.py`, `config.py` (runner-1-config), `preprod.sh`,
  `run.sh`; `health.py` once health-1-clean lands (dispatch any `<name>.py`/`<name>.sh` in the map, so no CLI change is needed then).
- `run.sh:25` and `preprod.sh:18` find siblings with `HERE=$(cd "$(dirname "$0")" && pwd)`. Through a symlinked CLI,
  resolve the real path first (`readlink -f`), or `HERE` points at `~/.local/bin`.
- `scripts/validate.sh` is the CI command (`uv run --with pytest`); CI needs `astral-sh/setup-uv` and Python 3.12.
- Design D2 (installed copy, not a submodule) and D5 rule: agents never push tags, so tagging is a user manual step.
- Gotcha: `ln -sfn` onto an existing symlink to a directory isn't atomic. Make the new link beside it and `mv -T` it over `current`.

## Tasks
### Task 1: `agent-runner` CLI
**Runs:** unattended
**Files:** agent-runner (new), tests/test_install.py (new)
**What:** A bash script. `HERE=$(dirname "$(readlink -f "$0")")`. Subcommand map: `queue`→`buildqueue.py`, `gate`, `inbox`,
`usage`, `config`, `health` → `<name>.py`; `preprod`, `run` → `<name>.sh`; Python goes through `python3`, and all remaining
arguments pass through with `exec`. `agent-runner version` prints the contents of `$HERE/VERSION` (written by install.sh), else
`git -C "$HERE" describe --tags --always`. An unknown or missing subcommand prints usage (the map) to stderr and exits 2.
**Validate:** `uv run --no-project --with pytest pytest -q tests/test_install.py -k cli` → `agent-runner gate --help` exits 0
through a symlink in `tmp_path/bin`; `agent-runner nope` exits 2; `agent-runner version` in a temp clone with tag `v0.0.1` prints `v0.0.1`.
**Status:** ✅ 2026-10-19

### Task 2: `install.sh <tag>`
**Runs:** unattended
**Files:** install.sh (new), tests/test_install.py (modify)
**What:** `install.sh <tag> | --list`. Source: `$AGENT_RUNNER_REMOTE` (default `https://github.com/SiddharthEngineer/agent-runner.git`).
Keep a bare mirror at `~/.local/share/agent-runner/mirror.git` (`git clone --mirror` the first time, `git fetch --prune --tags` after).
Refuse anything that isn't a `v*` tag in the mirror (exit 2). Check out with `git --git-dir=mirror.git worktree add --detach releases/<tag> <tag>`
(skip if it exists), write `releases/<tag>/VERSION`, run `releases/<tag>/scripts/validate.sh` and abort (exit 1, `current`
untouched) if it fails, then switch `current` atomically and point `~/.local/bin/agent-runner` at `current/agent-runner`. Keep the
newest 3 releases plus the active one; remove older ones with `git worktree remove`. `--list` prints the installed tags and marks the active one.
Rollback is `install.sh <older tag>`.
**Validate:** tests with `HOME=tmp_path` and `AGENT_RUNNER_REMOTE` set to a temp bare repo tagged `v0.0.1`, `v0.0.2`. Expect: install
v0.0.1 → `current` resolves to `releases/v0.0.1`; install v0.0.2 switches it; `main` and an unknown tag are refused with `current`
unchanged; after 5 installs, 3 remain. (Tests stub `scripts/validate.sh` in the temp repo with `exit 0`; one test uses `exit 1` to cover the abort.)
**Status:** ✅ 2026-10-19

### Task 3: CI workflow and README
**Runs:** unattended
**Files:** .github/workflows/ci.yml (new), README.md (modify), AGENTS.md (modify)
**What:** `ci.yml` runs on `pull_request` and on `push` to `main`, on `ubuntu-latest` with `actions/setup-python` 3.12 and
`astral-sh/setup-uv`, and runs `scripts/validate.sh`. Rewrite `README.md` (it's the Strelitzia-era `scripts/agent/README.md` today):
what the runner is, install and rollback (`install.sh`), the CLI map, config (`~/.config/agent-runner/{env,repos/*.env,health.toml}`)
and state (`~/.local/state/agent-runner/`) locations, and the cron lines from design D1/D6 marked "added at the cutover". Keep its
Limits section, but drop the Strelitzia paths. Add `agent-runner`, `install.sh` and `config.py` to the AGENTS.md map.
**Validate:** `scripts/validate.sh` → all PASS; after pushing the branch, `gh pr checks <pr> --repo SiddharthEngineer/agent-runner` → the CI check passes.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes locally and in CI.
- In a temp `HOME`: `AGENT_RUNNER_REMOTE=<local bare repo> ./install.sh v0.0.1 && ~/.local/bin/agent-runner version` → `v0.0.1`.

## Human test steps (preprod)
None on preprod. After merge, the user tags and installs (ROADMAP manual step AM1): `agent-runner version` prints the tag, and
`agent-runner config check <a fixture>` prints `ok`.

## Decisions
1. Releases are git worktrees of one bare mirror: cheap, and `install.sh` never needs network access for a rollback.
2. `install.sh` runs the release's own validation before switching, so a broken tag can't become `current`.
3. No cron lines here: the health line is a manual step after health-2-alert, and the run line is the cutover.
