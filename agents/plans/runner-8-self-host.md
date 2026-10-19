# Plan: agent-runner builds itself (its own `runner.toml`, agent settings and PIV commands)
**Status:** ❌ Not started
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19)
**Depends on:** runner-7-preprod-check

## Goal
agent-runner becomes a repo the runner can build, like Strelitzia and engineerfamily: plan with `/plan`, queue with
`/schedule-build`, and the cron implements and merges it. It gets `agents/runner.toml` (no `[preprod]`: a merge goes live
only when the user installs a tag), per-role agent settings, and the PIV commands. `agent-runner check-commands <repo>…` shows
how the repos' copies of the PIV commands differ from each other (design D4).

## Context
- Task 0 check: `prompts/preprod-check.md` exists (runner-7-preprod-check). If not, `blocked`.
- Design D4 (layout), D3 (roles and their permissions). Strelitzia's versions to adapt: `.claude/commands/{plan,implement,validate,ship,rca,schedule-build}.md`
  (in `~/src/Strelitzia`, read-only here), `scripts/agent/settings.json` (implementer permissions), `.claude/settings.json`.
- `settings/validate.json` (runner-6-review) is the default validator settings; this repo's `agents/settings/validate.json` may just copy it.
- `config.py` `RunnerConfig` (runner-1-config); `agent-runner` CLI map (runner-2-install).
- Gotcha: agent-runner has no Docker build or dashboard, so `validate.full` = `validate.fast` = `scripts/validate.sh`.

## Tasks
### Task 1: `runner.toml` and agent settings
**Runs:** unattended
**Files:** agents/runner.toml (new), agents/settings/implement.json (new), agents/settings/validate.json (new)
**What:** `[repo] name="agent-runner", github="SiddharthEngineer/agent-runner", base="main", merge="squash"`; validate both
`scripts/validate.sh`; `[gate] high_risk = ["agents/runner.toml", "agents/settings/*", ".claude/settings.json", ".github/*",
"install.sh", "settings/*"]`, `doc_or_test = ["*.md", "agents/*", ".claude/commands/*", "tests/*", "prompts/*"]`; no `[preprod]`.
Implementer settings: the Strelitzia ones without the `.venv`/npm/dashboard lines, plus `Bash(uv run:*)` and
`Bash(agent-runner inbox ask:*)`, `Bash(agent-runner gate:*)`; deny `Edit(agents/settings/**)`, `Edit(agents/runner.toml)`,
`Edit(.github/**)`, `Edit(install.sh)`, `Bash(git tag:*)`, `Bash(git push --tags:*)`, `Bash(crontab:*)`, `Read(//home/dev/.config/agent-runner/**)`.
**Validate:** `python3 config.py check agents/runner.toml` → `ok`; `pytest -q tests` (add a test that every `agents/settings/*.json` parses and denies `Bash(crontab:*)` and `Bash(git tag:*)`).
**Status:** ✅ 2026-10-19

### Task 2: PIV commands
**Runs:** unattended
**Files:** .claude/commands/plan.md (new), .claude/commands/implement.md (new), .claude/commands/schedule-build.md (new), .claude/commands/ship.md (new)
**What:** Copy Strelitzia's four commands and change only the repo-specific parts: repo `SiddharthEngineer/agent-runner`, base
`main`, `scripts/validate.sh` (no `--full`/`--setup`; `uv` needs no venv), and the runner calls `agent-runner gate|queue|inbox|preprod` instead of
`python3 scripts/agent/*.py`. `/schedule-build` passes `--repo agent-runner`. `/ship` has no preprod branch (a `review` PR waits for the validator).
**Validate:** `grep -rn 'scripts/agent\|Strelitzia\|trunk' .claude/commands` → no matches.
**Status:** ✅ 2026-10-19

### Task 3: `check-commands`
**Runs:** unattended
**Files:** commands_diff.py (new), agent-runner (modify), tests/test_install.py (modify)
**What:** `commands_diff.py <repo>…` loads each repo's `.claude/commands/*.md` from `origin/<base>` (via `repo_env` and `runner.toml`),
replaces the repo-specific tokens (repo name, base branch, validate command) with placeholders, and prints a unified diff per command
name that differs, or `same` per command. CLI subcommand `check-commands`.
**Validate:** test with two temp repos whose `plan.md` differ only in the repo name → `same`; and one with an extra line → a diff showing that line.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes.
- `python3 config.py check agents/runner.toml` → `ok`.

## Human test steps (preprod)
None (low risk). After the cutover, `agent-runner queue add <slug> --repo agent-runner` builds a plan from this repo.

## Decisions
1. Squash merges: runner history reads as one commit per Part.
2. No `[preprod]`: the user's tag + `install.sh` is this repo's deploy and stays manual.
