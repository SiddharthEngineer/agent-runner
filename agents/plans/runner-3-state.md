# Plan: multi-repo queue keys, inbox projects, shared state dir
**Status:** ✅ Done 2026-10-19 (all 3 tasks)
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19; nothing installed runs this until the cutover)
**Depends on:** runner-1-config, health-2-alert (both edit `inbox.py`; build after them to avoid conflicts)

## Goal
One queue for every repo. Items are keyed `<repo>/<slug>` (`strelitzia/site-sso-3-proxy-mode`, `engineerfamily/ef-agents-layout`),
`--after` can point across repos, and an old queue file with bare slugs migrates in place. `buildqueue.py`, `inbox.py` and
`usage.py` keep their state in `config.state_dir()` and load the env with `config.load_env()` (design D1). Inbox issues are labelled
with the item's repo instead of always `strelitzia`.

## Context
Line ranges are as of agent-runner `d361143`. If an earlier Part moved them, `grep -n` the named function.
- Task 0 check: `config.py` has `state_dir()`, `load_env()` (runner-1-config), and `inbox.py notify` has `--project` (health-2-alert).
  If either is missing, write the result file with `blocked` ("predecessor not merged") and stop.
- `buildqueue.py:40-43` (`CONFIG_DIR`, `STATE` from `$STRELITZIA_AGENT_STATE`), `:65-75` (`load`/`save`), `:78-82` (`item`),
  `:105-124` (`cmd_add`), `:138-153` (`cmd_next`, the `after` check), `:248-289` (argparse).
- `inbox.py:26-49` (`MARKER`, `PROJECT_LABEL`, `load_env`, `repo`), `:71-77` (`open_issue`), `:93-107` (`cmd_poll` iterates items by slug).
- `usage.py:46-49`: `probe()` imports `STATE` from buildqueue.
- `tests/test_runner.py:17-30` (the `q` fixture sets `STRELITZIA_AGENT_STATE`), `:32-44` (asserts the `strelitzia-agent` default dir).
- Callers that pass a bare slug and must keep working: `run.sh` (`set`, `get`, `start`, `finish`, `unset`), `preprod.sh:49-56,64,86`, `inbox.py:82`.

## Tasks
### Task 1: `<repo>/<slug>` keys and migration
**Runs:** unattended
**Files:** buildqueue.py (modify), tests/test_runner.py (modify)
**What:** `STATE = Path(os.environ.get("AGENT_RUNNER_STATE") or os.environ.get("STRELITZIA_AGENT_STATE") or config.state_dir())`
(the old variable stays as an alias so `run.sh`'s export keeps working until runner-5-run). `DEFAULT_REPO = os.environ.get("AGENT_DEFAULT_REPO", "strelitzia")`.
`load()`: every key without `/` becomes `f"{DEFAULT_REPO}/{key}"` with `item["repo"]` set, and a bare `after` becomes `<repo>/<after>`.
Save only when something changed. `resolve(data, ref) -> str`: an exact key wins; a bare slug that matches exactly one item's slug
part resolves to it; zero or several matches → `sys.exit` naming the candidates. Every command that takes a slug goes through `resolve`.
`add <slug> --repo <name>` (default `DEFAULT_REPO`) stores `repo`, `slug` and `plan = agents/plans/<slug>.md`; `--after` takes
`repo/slug` or a bare slug in the same repo. `next` prints the full key. `list` shows the key.
**Validate:** `uv run --no-project --with pytest pytest -q tests/test_runner.py` → passes. New tests: a bare-key `queue.json`
loads as `strelitzia/*` and is rewritten once; `add x --repo engineerfamily --after strelitzia/y` waits until `strelitzia/y` is
`done`; `set x status=done` with two `x` in different repos exits naming both; the existing tests pass with bare slugs. The
default-dir test now expects `~/.local/state/agent-runner` (test updated, not weakened: same assertion, new path).
**Status:** ✅ 2026-10-19

### Task 2: inbox and usage on the shared config
**Runs:** unattended
**Files:** inbox.py (modify), usage.py (modify), tests/test_runner.py (modify)
**What:** `inbox.load_env` becomes a call to `config.load_env()`. `ask <key>` resolves the key and labels the issue with the item's
`repo` (the `--project` from health-2-alert); `notify --slug <key>` does the same when `--project` isn't given. `cmd_poll` iterates
full keys. `MARKER` becomes `<!-- agent-runner -->`; `strelitzia-agent` and `applypilot-agent` markers are both read as legacy.
`usage.probe` uses `buildqueue.STATE`, unchanged in behavior.
**Validate:** tests stub `gh`: `ask engineerfamily/x` creates labels `agent-question` and `engineerfamily`; a reply comment with
the old `strelitzia-agent` marker isn't taken as the user's answer; `poll` re-queues `engineerfamily/x` with the answer.
**Status:** ✅ 2026-10-19

### Task 3: docs
**Runs:** unattended
**Files:** README.md (modify)
**What:** README: queue keys, `--repo`, cross-repo `--after`, the bare-slug migration, `AGENT_RUNNER_STATE`/`AGENT_DEFAULT_REPO`.
**Validate:** `scripts/validate.sh` → all PASS.
**Status:** ✅ 2026-10-19

## Validation
- `scripts/validate.sh` passes.
- Migration dry check on a copy: `cp ~/.local/state/strelitzia-agent/queue.json $T/ && AGENT_RUNNER_STATE=$T python3 buildqueue.py list`
  → every item listed as `strelitzia/<slug>`, all `done`. The real old state dir is never written.

## Human test steps (preprod)
None (low risk).

## Decisions
1. Bare slugs keep working everywhere through `resolve`, so `run.sh` and `preprod.sh` don't need to change in the same PR.
2. `STRELITZIA_AGENT_STATE` stays as an alias until runner-5-run rewrites `run.sh`.
