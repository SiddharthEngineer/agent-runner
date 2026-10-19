# Plan: VPS health alerts and read-only Claude diagnosis (`health.py tick`, `gate`)
**Status:** ❌ Not started
**Risk:** review (sends notices, starts a Claude run, adds an agent settings file)
**Depends on:** health-1-clean, runner-1-config

## Goal
Every 15 minutes `agent-runner health tick` measures the VPS, cleans up when a threshold is crossed, and measures again. If the
box is still unhealthy, the user gets one `vps-health` inbox issue plus an ntfy push for each problem. Before the first notice,
a short read-only Sonnet run looks around and adds a diagnosis: what is using the space or memory, and what the user could remove.
When the problem clears, the issue gets a "recovered" comment and is closed. `agent-runner health gate` exits non-zero while disk
is critical, for `run.sh` to check later (runner-4-run).

## Context
- `health.py` from health-1-clean: `measure`, `problems`, `clean`, `health.json`/`health.log` in the state dir.
- `config.py` (runner-1-config): `load_env()`, `state_dir()`. The env supplies `AGENT_INBOX_REPO`, `NTFY_TOPIC` and `CLAUDE_CODE_OAUTH_TOKEN`.
- `inbox.py:60-90`: `push`, `open_issue` (always adds `PROJECT_LABEL = "strelitzia"`), `cmd_notify` (prints the URL only).
- `usage.py:45-60`: the Haiku probe; `usage.py check` exits 0 when a run may start (design: the usage gate is global).
- `run.sh:40-41`: the run lock is `flock -n 9` on `$STATE/run.lock`. Before the cutover that is
  `~/.local/state/strelitzia-agent/run.lock`; after it, `~/.local/state/agent-runner/run.lock`. Check both, non-blocking.
- `run.sh:133-140`: how a headless run is started (`timeout`, `--model`, `--settings`, `--max-turns`, `--output-format json`).
- Design D6: dedupe, 6 h diagnosis spacing, skip conditions, the read-only command list.

## Tasks
### Task 1: `tick` and problem state
**Runs:** interactive
**Files:** health.py (modify), tests/test_health.py (modify)
**What:** `tick() -> int`: `load_env()`, measure, `clean()` if any problem is `cleanable` (else routine only), measure again.
Load is a problem only if it was also over the threshold on the previous tick at least 15 min earlier (keep `load_since` in
`health.json`). `health.json["open"][key] = {"since", "issue", "last_notified"}`. A new problem → `notify_new`; still open and
`last_notified` older than 24 h → `notify_again`; gone → `notify_recovered` and drop it. `gate() -> int`: 1 if `disk_pct >= disk_alert_pct`,
else 0, with no cleanup or notices. `tick` holds `flock` on `health.lock` (non-blocking; if another tick holds it, exit 0).
**Validate:** tests with monkeypatched `measure`, `clean` and notify functions: one problem over three ticks → one `new`, no
`again` within 24 h, then `recovered`; load over the threshold on one tick → no problem, on two ticks 15 min apart → `load`; a held `health.lock` → exit 0 with no calls.
**Status:** ❌

### Task 2: notices
**Runs:** interactive
**Files:** inbox.py (modify), health.py (modify), tests/test_runner.py (modify)
**What:** `inbox.py notify` gains `--project` (default `strelitzia`, used in place of `PROJECT_LABEL`) and `--json` (prints
`{"number", "url"}`). New `inbox.py comment <n> --body B [--close]` (adds `MARKER`, pushes ntfy). Health calls them with
`--project vps-health` and the title `[vps] <summary>` (e.g. `[vps] disk 91% used after cleanup`). The body holds metrics, freed
bytes per rule from this tick, and the diagnosis (Task 3), never file contents. `notify_again` and `notify_recovered` use `comment`
(`--close` for recovered). A failed `gh` call is logged and retried next tick (the state isn't updated).
**Validate:** `tests/test_runner.py` stubs `gh`: `notify --project vps-health` creates and uses that label and not `strelitzia`;
`comment --close` makes a comment and a close call. Existing inbox tests pass unchanged.
**Status:** ❌

### Task 3: read-only diagnosis
**Runs:** interactive (starts a real Claude run for its check)
**Files:** settings/health-diagnose.json (new), prompts/health-diagnose.md (new), health.py (modify), tests/test_health.py (modify)
**What:** `diagnose(problems, metrics, clean_log) -> str | None`. It returns `None` without starting Claude if: no
`CLAUDE_CODE_OAUTH_TOKEN`; `usage.py check` exits non-zero; either run lock is held; `health.json["last_diagnosis"]` is under 6 h
old; or `disk-critical` is open with disk ≥ 95%. Otherwise it runs
`timeout 10m claude -p <prompt> --model claude-sonnet-5-5 --max-turns 15 --output-format json --settings settings/health-diagnose.json`
with `cwd` = state dir and returns `result` (max 4,000 chars). Settings: `permissions.allow` only `Bash(df:*)`, `Bash(du:*)`,
`Bash(free:*)`, `Bash(ps:*)`, `Bash(ls:*)`, `Bash(uptime)`, `Bash(docker system df:*)`, `Bash(docker ps:*)`,
`Bash(docker images:*)`, `Bash(journalctl --disk-usage)`, `Read`; `deny` `Edit`, `Write`, `Bash(rm:*)`, `Bash(docker rm:*)`,
`Bash(docker rmi:*)`, `Bash(docker * prune:*)`, `Bash(kill:*)`, `Bash(sudo:*)`, `WebFetch`. The prompt: the problems and metrics,
the rules that already ran, "find what uses the space/memory, list the biggest items with sizes, and suggest what the user could
remove and the exact command; do not change anything; never print file contents or secrets".
**Validate:** tests stub `subprocess.run`: each skip condition returns `None` with no `claude` call; the argv has `--settings`
pointing at the file and `--model claude-sonnet-5-5`. A JSON test asserts that nothing in `allow` matches `rm`, `prune`, `kill`
or `sudo`. Interactive: `python3 health.py diagnose --force` on the VPS → a few lines naming the build cache and `~/.cache/uv`.
**Status:** ❌

### Task 4: docs
**Runs:** interactive
**Files:** README.md (modify), AGENTS.md (modify)
**What:** README "VPS health": the alert flow, how the dedupe works, the diagnosis and its limits, `health gate`, and the cron
line (design D6), added by the user (manual step AM2). AGENTS.md map: `settings/`, `prompts/`.
**Validate:** `scripts/validate.sh` → all PASS.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes.
- On the VPS with a temp `health.toml` setting `disk_clean_pct = 50` and `disk_alert_pct = 60`:
  `AGENT_RUNNER_STATE=$(mktemp -d) python3 health.py tick` → one `[vps] disk …` issue in SiddharthEngineer/agent-runner labelled
  `vps-health`, with a diagnosis section. Remove the override and tick again → a "recovered" comment, and the issue is closed.

## Human test steps (preprod)
None on preprod. After AM2: the user sees the ntfy push from the forced-threshold test above, and on a normal day `health.log` shows one routine pass a day and no issues.

## Decisions
1. The diagnosis can only read: cleanup stays in reviewed code (design D6), so a wrong guess costs a message, not data.
2. Health has its own lock and cron line, and only borrows the run lock to check that no other Claude run is going.
3. One issue per problem key, not per tick, so a full disk sends one push a day instead of 96.
