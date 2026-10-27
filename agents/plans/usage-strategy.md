# Plan: Usage strategy: weekly cap, weekly pacing, daytime reserve
**Status:** 🔄 In progress
**Risk:** low (the usage gate and its docs; goes live only on install; runner programme PRs auto-merge on green CI, user 2026-10-19)

## Goal
The runner can use the subscription around the clock without running into the weekly limit or crowding out the user's own
sessions. `usage.py`'s probe already returns both windows (`unifiedWindows.five_hour` and `.seven_day`, each with
`utilization` 0-1 and `resetsAt`), but the gate reads only the 5-hour one. After this plan the gate applies four rules, each
set in `~/.config/agent-runner/env`, and pauses the queue for exactly as long as the rule that refused needs:

| Rule | Setting (default) | Refuses when | Pauses until |
|---|---|---|---|
| Session cap | `AGENT_MAX_SESSION_USAGE` (70) | 5-hour use ≥ cap | the 5-hour reset |
| Daytime reserve | `AGENT_DAY_HOURS` (empty = off, e.g. `08-22`), `AGENT_DAY_SESSION_USAGE` (40) | inside day hours (VPS local time) and 5-hour use ≥ day cap | the earlier of the 5-hour reset and the end of day hours |
| Weekly cap | `AGENT_MAX_WEEKLY_USAGE` (90; 0 = off) | 7-day use ≥ cap | the 7-day reset |
| Weekly pacing | `AGENT_WEEKLY_PACE` (`even`; or `off`), `AGENT_PACE_SLACK` (10) | 7-day use > cap × (fraction of the week gone) + slack | the time that allowance catches up with use (never after the 7-day reset) |

`agent-runner usage status` shows both windows, every rule's verdict and which one holds the queue, read with the same env
file cron uses. A run that's already going always finishes; the rules only gate starts, as today.

## Context
- `usage.py:1-91` (whole file, 91 lines): `five_hour_window(lines)` (27-43), `probe()` (46-55), `cmd_check()` (58-80, exit 0/10/11;
  on 10 the last stdout line is the pause in hours), `main()` (83-87, accepts only `check`). If the validator-loop-3-costs
  plan (A26) has merged first, `usage.py` also has `cost`/`report` subcommands and `tests/test_usage.py` exists: keep them and add to them.
- `config.py:242-264`: `env_file()`, `parse_env()`, `load_env()` (sets `os.environ` defaults from `~/.config/agent-runner/env`;
  variables already set win). `health.py:682` calls `config.load_env()` the same way; usage.py's `main()` should too, so a
  plain shell gets the runner's settings.
- `run.sh:128-143`: `usage_gate()`; on exit 10 it pauses for the last line's hours and logs the first line with "queue paused
  until the window resets". `run.sh:618-627`: a usage-limit exit pauses a fixed 5 h.
- `run.sh:189-193`: triage also calls `usage.py check`; `health.py` diagnosis checks the gate too. Both keep working unchanged.
- `tests/test_runner.py:259-306`: `_usage()` loader, `_rate_event(utilization, resets_at)` (already includes a `seven_day`
  window at 0.9), and the existing gate tests. Keep `five_hour_window()` and these tests passing.
- `tests/test_run.py:639-660`: how run tests stub `usage.py` (write a fake `usage.py` into the runner copy).
- The live probe on 2026-10-26 returned `{"five_hour": {"utilization": 0.15, "resetsAt": 1793044200}, "seven_day":
  {"utilization": 0.32, "resetsAt": 1793246400}}` inside `rate_limit_info.unifiedWindows`.
- Gotchas: fraction of the week gone = `1 - (seven_day.resetsAt - now) / 604800`, clamped to 0..1. Day hours are VPS local
  time (`datetime.now().astimezone()`), the same clock `/schedule-build` uses; `22-06` wraps past midnight. An event with no
  `seven_day` window skips the two weekly rules with a note; it is not "unknown". Missing `resetsAt` → pause 1 h, as today.

## Tasks
### Task 1: Strategy and rules in `usage.py`
**Runs:** unattended
**Files:** usage.py (modify), tests/test_usage.py (new, or modify if A26 created it)
**What:** pure functions, no I/O:
```python
@dataclass(frozen=True)
class Window: utilization: float; resets_at: int | None          # utilization 0-1
@dataclass(frozen=True)
class Strategy: session_cap: float; day_hours: tuple[int, int] | None; day_cap: float
                weekly_cap: float; pace: str; pace_slack: float  # percents; pace "even" | "off"
@dataclass(frozen=True)
class Verdict: rule: str; allowed: bool; message: str; pause_hours: float | None
def windows(lines: list[str]) -> dict[str, Window]                 # "five_hour", "seven_day" when present
def strategy_from_env(env: Mapping[str, str]) -> Strategy          # ValueError naming the bad variable
def evaluate(win: dict[str, Window], s: Strategy, now: datetime) -> list[Verdict]  # one per active rule
```
A cap of 0 turns its rule off (session cap 0 keeps today's meaning: the whole check is off). `cmd_check()` uses them: all allowed → exit 0,
first line `usage ok: 5h 15%, 7d 32% (pace allows 67%)`; any refused → exit 10, first line names the refusing rule with its
numbers (`weekly pace: 7d 52% > 46% allowed (cap 90%, 3.1 of 7 days gone)`), last line the **largest** pause of the refusing
rules (minimum 1/12 h); bad settings → exit 11 with the ValueError's message; probe failure → 11 as today.
**Validate:** `uv run --with pytest pytest tests/test_usage.py tests/test_runner.py -q` → passes, with new tests for: each rule
allowing and refusing at its edge; pause hours for each rule (pacing catch-up time computed and capped at the weekly reset);
day hours inside/outside and wrapping `22-06`; the largest pause wins when two rules refuse; no `seven_day` window skips
weekly rules; bad `AGENT_WEEKLY_PACE=fast` / `AGENT_DAY_HOURS=8` → exit 11 naming the variable; the old default (70) still holds.
**Status:** ✅ 2026-10-27 (this commit)

### Task 2: `usage.py status`, env file, CLI help
**Runs:** unattended
**Files:** usage.py (modify), agent-runner (modify), tests/test_usage.py (modify)
**What:** `main()` calls `config.load_env()` first, then accepts `check` and `status`. `status` probes once and prints, then
exits 0 (11 if the probe or settings fail):
```
5-hour  15% used, resets 2026-10-26 19:50 UTC
7-day   32% used, resets 2026-10-29 04:00 UTC (4.5 of 7 days gone)
✅ session cap      15% < 70%
➖ daytime reserve  off (AGENT_DAY_HOURS not set)
✅ weekly cap       32% < 90%
✅ weekly pacing    32% ≤ 67% allowed (even, slack 10)
Runs may start.
```
or `Held by weekly pacing until ~2026-10-27 08:00 UTC.` The `agent-runner` help line for `usage` becomes
"Claude usage gate and status".
**Validate:** `uv run --with pytest pytest tests/test_usage.py -q` → passes; a test sets `HOME` to a temp dir with
`.config/agent-runner/env` holding `AGENT_MAX_WEEKLY_USAGE=20` and a stubbed probe at 7-day 0.32, and asserts `status`
prints the weekly cap as ❌ and the holding rule.
**Status:** ❌

### Task 3: `run.sh` pause messages and usage-limit exits
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify)
**What:** `usage_gate()` logs `"$key: not started, <first line>; queue paused <h>h"` instead of "until the window resets".
After a usage-limit exit (`run.sh:618-627`), run `usage.py check`: on exit 10 pause for its hours (so a weekly-limit stop
pauses until the weekly reset, not 5 h), otherwise pause 5 h as today; log which.
**Validate:** `uv run --with pytest pytest tests/test_run.py -q` → passes, with tests: a stubbed `usage.py` returning exit 10 and
`30.5` after a usage-limit exit pauses the queue ~30.5 h; one returning 11 pauses 5 h.
**Status:** ❌

### Task 4: Docs
**Runs:** unattended
**Files:** README.md (modify), AGENTS.md (modify), agents/ROADMAP.md (modify)
**What:** README "Limits": replace the session-usage bullet with the rule table from the Goal, the env lines, and
`agent-runner usage status`; the usage-limit bullet says it pauses for the gate's time. CLI table: `usage` → "Claude usage gate
and status". AGENTS.md map line for `usage.py`. ROADMAP A28 → ✅ with the PR.
**Validate:** `scripts/validate.sh` → all PASS; `grep -n "AGENT_WEEKLY_PACE" README.md` → at least one line.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes.
- With a stubbed probe (tests), every rule refuses at its edge and the pause matches the table in the Goal.

## Human test steps
After `agent-runner release --install`:
1. `agent-runner usage status` → both windows with reset times and four rule lines; "Runs may start." (or the holding rule).
2. Add to `~/.config/agent-runner/env` the settings you want, e.g.:
   `AGENT_MAX_WEEKLY_USAGE=90`, `AGENT_WEEKLY_PACE=even`, `AGENT_DAY_HOURS=08-22`, `AGENT_DAY_SESSION_USAGE=40`.
   Run `agent-runner usage status` again → the daytime line shows your hours instead of "off".
3. `AGENT_MAX_WEEKLY_USAGE=1 agent-runner usage check; echo $?` → a "weekly cap" line, a number of hours, and `10`. Nothing is paused (only `run.sh` pauses).
4. After the next due item: `grep "not started\|usage" ~/.local/state/agent-runner/runs.log | tail -3` names the rule when a run is held.

## Decisions
1. Defaults turn on the weekly cap (90%) and even pacing (slack 10), and leave the daytime reserve off: the first two only
   protect the week, the third depends on the user's hours.
2. When several rules refuse, the queue pauses for the longest of their pauses, so the next tick doesn't start straight into another refusal.
3. Pacing is linear over the 7-day window plus a slack, so a fresh week can start work at once instead of waiting.
4. Settings stay in `~/.config/agent-runner/env` (the user's), read by `config.load_env()`, so `status` in a shell matches cron.
