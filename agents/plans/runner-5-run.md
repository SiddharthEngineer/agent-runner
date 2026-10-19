# Plan: `run.sh` builds every registered repo
**Status:** ❌ Not started
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19; nothing installed runs this until the cutover)
**Depends on:** runner-4-preprod

## Goal
The cron entry builds plans from any registered repo, so engineerfamily and agent-runner get the same continuous development as
Strelitzia. Each tick: `health gate`, upkeep, take the next due `<repo>/<slug>`, run the implementer in
`worktrees/<repo>/<slug>` with that repo's settings, re-validate with that repo's command, gate with its `runner.toml`, then
merge with its merge method or send the PR to preprod. `AGENT_DRY_RUN=1` logs every decision and stops before anything with side
effects, so the cutover can be rehearsed.

## Context
Line ranges are as of agent-runner `d361143`; `grep -n` if they moved.
- Task 0 check: `preprod.sh` takes `<repo> <pr>` (runner-4-preprod) and `health.py gate` exists (health-2-alert). If not, `blocked`.
- `run.sh:19-41` (env, `CONFIG_DIR`, `AGENT_REPO` from `$HERE/../..`, `GH_REPO`, `MERGE_POLICY`, `STATE`, lock), `:43-77` (inbox poll,
  `AGENT_MERGE_POLICY=all` merge loop, `preprod.sh tick`), `:79-99` (`next`, usage gate, plan must be on `origin/trunk`),
  `:101-109` (worktree on `agent/<slug>` from `origin/trunk`), `:117-140` (prompt, `run_claude` with `$wt/scripts/agent/settings.json`
  and `mcp.json`), `:170-195` (session id, usage-limit handling), `:198-230` (`validate.sh --full`, `gate.py`, `gh pr checks --watch`,
  `gh pr merge --rebase`), `:232-245` (finish, worktree removal, build-cache prune, preprod tick).
- `config.py`: `repo_env(name)` (`AGENT_REPO`), `RunnerConfig` (`repo.github`, `base`, `merge`, `plans`, `validate.full`), `config.py get`.
- Per-repo agent files read from the worktree: `agents/settings/implement.json` and `agents/mcp.json` (design D4). Until a
  repo has them, fall back to `scripts/agent/settings.json` and `mcp.json` (Strelitzia today).
- Design D3 merge rule. Until runner-6-review lands, a `review` PR goes to preprod and waits for the user, as today.
- Gotcha: `run.sh` is sourced by nothing; tests run it as a subprocess with stub `claude`, `gh` and `git push` on `PATH`.

## Tasks
### Task 1: repos, config and worktrees
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (new)
**What:** Load the env with `config.py` (shared env, then `repos/<repo>.env` for the picked item). Take `key=$(buildqueue.py next)` and split it into
`repo`/`slug`. `git -C "$AGENT_REPO" fetch`, then `git show origin/<base>:agents/runner.toml` to a temp file and `config.py check` it.
A bad config blocks the item with a notice listing the problems. The plan must exist on `origin/<base>` at `<plans>/<slug>.md`.
Worktree `$STATE/worktrees/<repo>/<slug>` on `agent/<slug>` from `origin/<base>`. `run_claude` uses the worktree's
`agents/settings/implement.json` and `agents/mcp.json` (falling back as in Context). `GH_REPO` comes from `repo.github`.
Drop `AGENT_MERGE_POLICY` and its merge loop (`:55-76`). Add `AGENT_DRY_RUN=1`: log "would run claude / merge / deploy" and exit 0 before each side effect.
**Validate:** `uv run --no-project --with pytest pytest -q tests/test_run.py` → with a temp state dir, a temp origin repo holding
`agents/runner.toml` and a plan, and stub `claude`/`gh` scripts on `PATH`: a dry run logs `would run claude for engineerfamily/x`
in `worktrees/engineerfamily/x`; a plan missing on `origin/main` → item `blocked`; a config with `deploy-prod` → `blocked`, no claude call.
**Status:** ✅ 2026-10-19

### Task 2: validate, gate and merge per repo
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify)
**What:** After a `ready` result: run `validate.full` in the worktree; `gate.py <plan> --config <toml>`; `low` + `gh pr checks --watch`
green → `gh pr merge --<merge> --delete-branch` (no `--rebase` hard-code), notice, `preprod.sh release` only if this repo's PR
held preprod. Anything else → label `awaiting-preprod`, risk-gate comment, as today. The stub `claude` writes a result file, so the
full path runs in tests.
**Validate:** tests: a `low` engineerfamily item with green stub checks → `gh pr merge 9 --merge --delete-branch`; a Strelitzia
item → `--rebase`; `gate` returning review → `awaiting-preprod` label and no merge call.
**Status:** ✅ 2026-10-19 (committed with Task 1: one rewrite of run.sh)

### Task 3: health gate and upkeep order
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify), README.md (modify)
**What:** First thing after taking the lock: `health.py gate` non-zero → log "disk critical, no runs" and exit 0 (upkeep still
runs: inbox poll and `preprod.sh tick` come before the gate). Keep the per-run `docker builder prune --keep-storage` (cheap and
it bounds long chains). Update the header comment and README "How a tick works" for multi-repo and the dry run.
**Validate:** test: a stub `health.py gate` exiting 1 → no claude call, inbox poll still called. `scripts/validate.sh` → all PASS.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes.
- Design Rollout step 2 rehearsal: `T=$(mktemp -d); cp ~/.local/state/strelitzia-agent/queue.json $T/; AGENT_RUNNER_STATE=$T AGENT_DRY_RUN=1 ./run.sh; tail $T/runs.log`
  → "nothing due". It doesn't touch `~/.local/state/strelitzia-agent`.

## Human test steps (preprod)
None (low risk). The live check is the cutover (design Rollout steps 3-6).

## Decisions
1. Config and plans are read from `origin/<base>`, so a branch can't change its own gate or deploy commands.
2. `AGENT_MERGE_POLICY` goes away: one merge rule for every repo (design D3).
