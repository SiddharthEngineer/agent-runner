# Plan: runner.toml loader, prod guard, and shared config/state dirs
**Status:** ❌ Not started
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19. Was review: the runner's config and risk gate; every later Part reads it)
**Depends on:** - (first Part of [shared-agent-runner](shared-agent-runner.md))

## Goal
The runner stops hard-coding Strelitzia. A repo describes itself in `agents/runner.toml` (base branch, merge method, validate
commands, risk patterns, preprod commands), and `config.py` loads and checks it, refusing any preprod command that could touch
prod. `config.py` also owns where the runner's config and state live (`~/.config/agent-runner`, `~/.local/state/agent-runner`,
with a fallback to the old `strelitzia-agent` env file). `gate.py --config <runner.toml>` takes its patterns and base from it;
without `--config` it behaves exactly as today.

## Context
- `gate.py:24-52`: `HIGH_RISK`, `CODE_LINE_BUDGET`, `DOC_OR_TEST` and the `PLAN_*` budgets. The first three become per-repo
  config; `PLAN_*` stay global (design D1).
- `gate.py:117-157`: `evaluate_plan`, `evaluate` and `main` (`--base origin/trunk` default) read the module constants directly.
- `inbox.py:32-42`: `load_env()` parses `KEY=value` lines (strips `export `, quotes, comments). Mirror it in `config.load_env`;
  `inbox.py` moves to it in `runner-3-state`, not here.
- `buildqueue.py:40-43`: `CONFIG_DIR`/`STATE` with `$STRELITZIA_AGENT_STATE`. Unchanged here.
- Values for the Strelitzia fixture: `gate.py:24-46` (patterns), `run.sh:28` (`GH_REPO`), `run.sh:61` (`--rebase`),
  `~/.config/strelitzia-agent/env` `PREPROD_*` lines (read them; copy the commands, never the netrc path's contents).
- Design: [shared-agent-runner](shared-agent-runner.md) D1 (naming), D4 (`runner.toml`), D5 (engineerfamily values, prod guard).
- Gotcha: `tomllib` needs Python 3.11+ (the VPS has 3.14). Stdlib only, no `tomli` fallback.

## Tasks
### Task 1: `config.py` loader and prod guard
**Runs:** unattended
**Files:** config.py (new), tests/test_config.py (new), tests/fixtures/strelitzia.runner.toml (new), tests/fixtures/engineerfamily.runner.toml (new)
**What:**
```toml
[repo]            # name (str, [a-z0-9-]), github ("owner/name"), base (str), merge ("rebase"|"merge"|"squash"), plans (default "agents/plans")
[validate]        # fast, full (shell command strings)
[gate]            # high_risk, doc_or_test (lists of fnmatch patterns), code_line_budget (int, default 250), tests_dir (default "tests/")
[preprod]         # optional: deploy (may contain {sha}), release, seed, health = [{url, status = 200} | {url, json_ok = true}]
```
`@dataclass(frozen=True) class RunnerConfig` with those fields (`preprod: Preprod | None`). `load(path: Path) -> RunnerConfig`
raises `ConfigError(problems: list[str])` that lists every problem at once (missing key, wrong type, bad merge value).
`PROD_PATTERNS = [r"deploy-prod", r"tag-prod", r"strelitzia-deploy", r"/srv/[^\s]*-prod\b", r"-p engineerfamily(\s|$)", r"engineerfamily-prod"]`;
`prod_problems(cfg) -> list[str]` checks every `[preprod]` command and is part of `load`. CLI: `config.py check <toml>`
(prints `ok` or one problem per line, exit 1) and `config.py get <toml> <dotted.key>` (prints the value; lists one per line).
The fixtures hold today's Strelitzia values and design D5's engineerfamily values.
**Validate:** `uv run --no-project --with pytest pytest -q tests/test_config.py` → passes, including: both fixtures load; a missing
`repo.base` and a bad `merge` are both reported in one error; each `PROD_PATTERNS` entry in a `preprod.deploy` is refused;
`preprod-deploy ref={sha}` is accepted. `python3 config.py get tests/fixtures/engineerfamily.runner.toml repo.base` → `main`.
**Status:** ❌

### Task 2: config and state dirs, env loading
**Runs:** unattended
**Files:** config.py (modify), tests/test_config.py (modify)
**What:** `config_dir() -> Path` = `~/.config/agent-runner`. `state_dir() -> Path` = `$AGENT_RUNNER_STATE` or
`~/.local/state/agent-runner`. `env_file() -> Path | None`: `config_dir()/env` if it exists, else `~/.config/strelitzia-agent/env`
if that exists (one-release fallback, design D1), else `None`. `load_env() -> None` sets `os.environ` defaults from it, parsing as
`inbox.py:32-42`. `repo_env(name) -> dict[str, str]` parses `config_dir()/repos/<name>.env` the same way (empty dict if missing).
Nothing calls these yet besides tests and `health.py` (Parts health-1/2).
**Validate:** tests with `HOME` set to `tmp_path` → new env wins over the old one, old one is used alone, neither → `None`;
`AGENT_RUNNER_STATE` overrides `state_dir()`; `export K="v"` and `# comment` lines parse like `inbox.load_env`.
**Status:** ❌

### Task 3: `gate.py --config`
**Runs:** unattended
**Files:** gate.py (modify), tests/test_runner.py (modify)
**What:** Add `--config <runner.toml>`. When given, `HIGH_RISK`, `DOC_OR_TEST`, `CODE_LINE_BUDGET` and the tests dir come from
`cfg.gate`, and `--base` defaults to `origin/<cfg.repo.base>`. Pass them as arguments (`evaluate(plan, base, rules)` with a small
`Rules` dataclass built either from the config or from today's constants), not by mutating module globals. Without `--config`,
output is byte-for-byte what it is today. Update the module docstring's usage lines (drop the `scripts/agent/` prefix).
**Validate:** `scripts/validate.sh` → all PASS. New tests: with the engineerfamily fixture, a diff touching `services/nginx/x.conf`
is `review` and one touching only `README.md` is `low`; `--plan --config` uses the config's `high_risk`. Existing gate tests pass unchanged.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes.
- `python3 config.py check tests/fixtures/strelitzia.runner.toml` → `ok`; same for the engineerfamily fixture.
- `python3 gate.py agents/plans/runner-1-config.md --plan` → `"split": []`.

## Human test steps (preprod)
None (no preprod for agent-runner; nothing is installed or in cron yet).

## Decisions
1. `PLAN_*` budgets stay global constants: they come from Claude's context costs, not from a repo.
2. The prod guard lives in `load`, so no caller can get a `RunnerConfig` with a prod command in it.
3. `gate.py` keeps working without `--config` so the old Strelitzia runner's copy and these tests don't diverge before the cutover.
