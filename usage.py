#!/usr/bin/env python3
"""Usage gate for unattended runs: may a Claude run start now, given the 5-hour and 7-day windows and the strategy?

The runner's token (`claude setup-token`) is inference-only, so it can't read /usage. Instead this sends one tiny
Haiku prompt and reads the `rate_limit_event` that `claude -p --output-format stream-json` emits
(`unifiedWindows.five_hour` and `.seven_day`: `utilization` 0-1, `resetsAt`). run.sh calls it only when a queue item is due.

Rules, each set in ~/.config/agent-runner/env (a cap of 0 turns its rule off):
  session cap      AGENT_MAX_SESSION_USAGE (70; 0 turns the whole gate off)   5-hour use >= cap
  daytime reserve  AGENT_DAY_HOURS (empty = off, e.g. 08-22, local time), AGENT_DAY_SESSION_USAGE (40)
                   inside day hours and 5-hour use >= day cap
                   AGENT_DAY_PAUSE=1 refuses every start inside day hours, whatever the usage (a cap of 0 is "off")
  weekly cap       AGENT_MAX_WEEKLY_USAGE (90)                                 7-day use >= cap
  weekly pacing    AGENT_WEEKLY_PACE (even | off), AGENT_PACE_SLACK (10)
                   7-day use > cap x (fraction of the week gone) + slack

Usage:
  usage.py check     exit 0: every rule allows a start
                     exit 10: a rule refuses; stdout's first line names it, the last line is the hours to pause
                     exit 11: usage unknown (probe failed) or bad settings; skip this tick
  usage.py status    both windows, every rule's verdict and which one holds the queue
  usage.py cost <log> --role R --key K --model M [--after BYTES]
                     one `cost` line from the last `--output-format json` result in the log, past its first BYTES
                     (what the log held before this run; nothing if it has none); run.sh appends it to runs.log
  usage.py report [--days N]
                     the `cost` lines of runs.log over the last N days (7), summed by role and by repo: runs, turns, USD.
                     USD is the API-equivalent cost Claude reports, not a subscription charge.
Standard library only.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

PROBE_MODEL = os.environ.get("AGENT_USAGE_PROBE_MODEL", "claude-haiku-5-5")
WEEK = 7 * 24 * 3600
MIN_PAUSE_HOURS = 1 / 12  # one cron tick
RULES = ("session cap", "daytime reserve", "weekly cap", "weekly pacing")


@dataclass(frozen=True)
class Window:
    utilization: float  # 0-1
    resets_at: int | None  # epoch seconds


@dataclass(frozen=True)
class Strategy:
    """Caps in percent; a cap of 0 turns its rule off. `pace` is "even" or "off"."""

    session_cap: float
    day_hours: tuple[int, int] | None
    day_cap: float
    weekly_cap: float
    pace: str
    pace_slack: float
    day_pause: bool = False  # inside day hours, refuse every start regardless of usage


@dataclass(frozen=True)
class Verdict:
    rule: str
    allowed: bool
    message: str
    pause_hours: float | None  # set when refused
    skipped: bool = False  # active, but its window is missing from the probe


def five_hour_window(lines: list[str]) -> tuple[float, int | None] | None:
    """(utilization 0-1, resetsAt epoch seconds) from stream-json lines, or None if no rate_limit_event has it."""
    window = windows(lines).get("five_hour")
    return None if window is None else (window.utilization, window.resets_at)


def _window(raw: object) -> Window | None:
    """A Window from {"utilization", "resetsAt"}, or None if it isn't one (a missing or odd resetsAt reads as None)."""
    if not isinstance(raw, dict):
        return None
    try:
        utilization = float(raw["utilization"])
    except (KeyError, TypeError, ValueError):
        return None
    resets = raw.get("resetsAt")
    return Window(utilization, int(resets) if isinstance(resets, (int, float)) and not isinstance(resets, bool) else None)


def windows(lines: list[str]) -> dict[str, Window]:
    """The last rate_limit_event's "five_hour" and "seven_day" windows, those present."""
    found: dict[str, Window] = {}
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
            continue
        info = event.get("rate_limit_info")
        info = info if isinstance(info, dict) else {}
        unified = info.get("unifiedWindows")
        unified = unified if isinstance(unified, dict) else {}
        current: dict[str, Window] = {}
        for name in ("five_hour", "seven_day"):
            window = _window(unified.get(name))
            if window is None and info.get("rateLimitType") == name:
                window = _window(info)
            if window is not None:
                current[name] = window
        if current:
            found = current
    return found


def _percent(env: Mapping[str, str], name: str, default: str) -> float:
    raw = env.get(name, default).strip() or default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None
    if not 0 <= value <= 100:
        raise ValueError(f"{name}={raw!r} is not a percent (0-100)")
    return value


def session_cap_from_env(env: Mapping[str, str]) -> float:
    """AGENT_MAX_SESSION_USAGE; empty has always meant 0 (the whole gate off), so keep that."""
    return _percent(env, "AGENT_MAX_SESSION_USAGE", "70") if env.get("AGENT_MAX_SESSION_USAGE", "70").strip() else 0.0


def strategy_from_env(env: Mapping[str, str]) -> Strategy:
    """The rules' settings from the environment; ValueError naming the bad variable."""
    session_cap = session_cap_from_env(env)
    day_hours = None
    raw_hours = env.get("AGENT_DAY_HOURS", "").strip()
    if raw_hours:
        m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", raw_hours)
        if not m or int(m[1]) > 23 or int(m[2]) > 24 or int(m[1]) == int(m[2]) % 24:
            raise ValueError(f"AGENT_DAY_HOURS={raw_hours!r} is not HH-HH (e.g. 08-22, or 22-06 across midnight)")
        day_hours = (int(m[1]), int(m[2]))
    pace = env.get("AGENT_WEEKLY_PACE", "even").strip() or "even"
    if pace not in ("even", "off"):
        raise ValueError(f"AGENT_WEEKLY_PACE={pace!r} is not even or off")
    return Strategy(
        session_cap=session_cap,
        day_hours=day_hours,
        day_cap=_percent(env, "AGENT_DAY_SESSION_USAGE", "40"),
        weekly_cap=_percent(env, "AGENT_MAX_WEEKLY_USAGE", "90"),
        pace=pace,
        pace_slack=_percent(env, "AGENT_PACE_SLACK", "10"),
        day_pause=_day_pause(env),
    )


def _day_pause(env: Mapping[str, str]) -> bool:
    raw = env.get("AGENT_DAY_PAUSE", "0").strip().lower() or "0"
    if raw not in ("0", "1"):
        raise ValueError(f"AGENT_DAY_PAUSE={raw!r} is not 0 or 1")
    return raw == "1"


def _hours_until(epoch: float, now: datetime) -> float:
    return max((epoch - now.timestamp()) / 3600, 0.0)


def _in_day(hours: tuple[int, int], now: datetime) -> bool:
    start, end = hours
    if start < end:
        return start <= now.hour < end
    return now.hour >= start or now.hour < end  # wraps past midnight


def _day_end(hours: tuple[int, int], now: datetime) -> datetime:
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=hours[1])
    while end <= now:
        end += timedelta(days=1)
    return end


def _days_gone(week: Window, now: datetime) -> float:
    return min(max(7 - (week.resets_at - now.timestamp()) / 86400, 0.0), 7.0)


def pace_allowance(week: Window, s: Strategy, now: datetime) -> float:
    """Percent of the week the pacing rule allows now: cap x fraction of the week gone + slack."""
    return (s.weekly_cap or 100) * _days_gone(week, now) / 7 + s.pace_slack


def _pct(window: Window) -> float:
    return round(window.utilization * 100, 6)  # 0.55 * 100 is 55.00000000000001; edges compare as written


def _hours_label(hours: tuple[int, int]) -> str:
    return f"{hours[0]:02d}-{hours[1]:02d}"


def evaluate(win: dict[str, Window], s: Strategy, now: datetime) -> list[Verdict]:
    """One verdict per active rule (off rules are left out), in RULES order. `now` must be timezone-aware."""
    verdicts: list[Verdict] = []
    session, week = win.get("five_hour"), win.get("seven_day")
    if session is not None:
        used = _pct(session)
        session_pause = _hours_until(session.resets_at, now) if session.resets_at else 1.0
        if s.session_cap > 0:
            ok = used < s.session_cap
            verdicts.append(Verdict("session cap", ok, f"{used:.0f}% {'<' if ok else '≥'} {s.session_cap:.0f}%",
                                    None if ok else session_pause))
        if s.day_hours is not None and (s.day_cap > 0 or s.day_pause):
            label = _hours_label(s.day_hours)
            if not _in_day(s.day_hours, now):
                verdicts.append(Verdict("daytime reserve", True, f"outside day hours {label}", None))
            elif s.day_pause:
                end = _hours_until(_day_end(s.day_hours, now).timestamp(), now)
                verdicts.append(Verdict("daytime reserve", False, f"paused during {label} (AGENT_DAY_PAUSE=1)", end))
            else:
                ok = used < s.day_cap
                end = _hours_until(_day_end(s.day_hours, now).timestamp(), now)
                pause = min(end, session_pause) if session.resets_at else end
                verdicts.append(Verdict("daytime reserve", ok,
                                        f"{used:.0f}% {'<' if ok else '≥'} {s.day_cap:.0f}% during {label}",
                                        None if ok else pause))
    weekly_rules = [r for r, on in (("weekly cap", s.weekly_cap > 0), ("weekly pacing", s.pace == "even")) if on]
    if week is None:
        verdicts += [Verdict(r, True, "skipped: no 7-day window in the probe", None, skipped=True) for r in weekly_rules]
        return verdicts
    used = _pct(week)
    if "weekly cap" in weekly_rules:
        ok = used < s.weekly_cap
        pause = _hours_until(week.resets_at, now) if week.resets_at else 1.0
        verdicts.append(Verdict("weekly cap", ok, f"{used:.0f}% {'<' if ok else '≥'} {s.weekly_cap:.0f}%",
                                None if ok else pause))
    if "weekly pacing" in weekly_rules:
        if not week.resets_at:
            verdicts.append(Verdict("weekly pacing", True, "skipped: no 7-day reset time in the probe", None,
                                    skipped=True))
            return verdicts
        cap = s.weekly_cap or 100
        allowed = pace_allowance(week, s, now)
        ok = used <= allowed
        detail = f"(cap {cap:.0f}%, slack {s.pace_slack:.0f}, {_days_gone(week, now):.1f} of 7 days gone)"
        pause = None
        if not ok:
            # The allowance reaches `used` when the week is (used - slack) / cap gone; never later than the reset.
            catch_up = week.resets_at - WEEK * (1 - min((used - s.pace_slack) / cap, 1.0))
            pause = min(_hours_until(catch_up, now), _hours_until(week.resets_at, now))
        verdicts.append(Verdict("weekly pacing", ok,
                                f"{used:.0f}% {'≤' if ok else '>'} {allowed:.0f}% allowed {detail}", pause))
    return verdicts


def _summary(win: dict[str, Window], s: Strategy, now: datetime) -> str:
    text = f"5h {_pct(win['five_hour']):.0f}%"
    week = win.get("seven_day")
    if week is None:
        return text + ", 7d unknown"
    text += f", 7d {_pct(week):.0f}%"
    if s.pace == "even" and week.resets_at:
        text += f" (pace allows {pace_allowance(week, s, now):.0f}%)"
    return text


def probe() -> list[str]:
    from buildqueue import STATE as state  # same directory

    state.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(
        ["claude", "-p", "Reply with just: ok", "--model", PROBE_MODEL, "--max-turns", "1",
         "--output-format", "stream-json", "--verbose"],
        cwd=state, capture_output=True, text=True, timeout=120,
    )
    return out.stdout.splitlines()


def _read() -> tuple[dict[str, Window], datetime] | str:
    """Probe once: (windows, now), or why the usage is unknown."""
    try:
        win = windows(probe())
    except (OSError, subprocess.SubprocessError) as exc:
        return f"usage unknown: {exc}"
    if "five_hour" not in win:
        return "usage unknown: no rate_limit_event in the probe output"
    return win, datetime.now().astimezone()


def _hold(verdicts: list[Verdict]) -> tuple[Verdict, float] | None:
    """The refusing rule with the longest pause, and that pause (at least one cron tick), or None if every rule allows.
    The queue pauses for the longest refusal, so the next tick doesn't start straight into another one."""
    refused = [v for v in verdicts if not v.allowed]
    if not refused:
        return None
    holder = max(refused, key=lambda v: v.pause_hours or 0)
    return holder, max(holder.pause_hours or 0, MIN_PAUSE_HOURS)


def cmd_check() -> int:
    try:
        if session_cap_from_env(os.environ) <= 0:
            print("session usage check disabled")
            return 0
        strategy = strategy_from_env(os.environ)
    except ValueError as exc:
        print(f"usage settings: {exc}")
        return 11
    read = _read()
    if isinstance(read, str):
        print(read)
        return 11
    win, now = read
    verdicts = evaluate(win, strategy, now)
    hold = _hold(verdicts)
    if hold is None:
        print(f"usage ok: {_summary(win, strategy, now)}")
        return 0
    print("; ".join(f"{v.rule}: {v.message}" for v in verdicts if not v.allowed))
    print(f"{math.ceil(hold[1] * 100) / 100}")
    return 10


OFF = {
    "session cap": "off (AGENT_MAX_SESSION_USAGE=0: the whole gate is off)",
    "daytime reserve": "off (AGENT_DAY_HOURS not set)",
    "weekly cap": "off (AGENT_MAX_WEEKLY_USAGE=0)",
    "weekly pacing": "off (AGENT_WEEKLY_PACE=off)",
}


def _utc(epoch: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch)) if epoch else "unknown"


def cmd_status() -> int:
    try:
        strategy = strategy_from_env(os.environ)
    except ValueError as exc:
        print(f"usage settings: {exc}")
        return 11
    read = _read()
    if isinstance(read, str):
        print(read)
        return 11
    win, now = read
    session, week = win["five_hour"], win.get("seven_day")
    print(f"5-hour  {_pct(session):.0f}% used, resets {_utc(session.resets_at)}")
    if week is None:
        print("7-day   unknown (no 7-day window in the probe)")
    else:
        gone = f" ({_days_gone(week, now):.1f} of 7 days gone)" if week.resets_at else ""
        print(f"7-day   {_pct(week):.0f}% used, resets {_utc(week.resets_at)}{gone}")
    verdicts = evaluate(win, strategy, now)
    by_rule = {v.rule: v for v in verdicts}
    off = dict(OFF)
    if strategy.day_hours is not None and not strategy.day_pause:
        off["daytime reserve"] = "off (AGENT_DAY_SESSION_USAGE=0)"
    for rule in RULES:
        v = by_rule.get(rule)
        icon = "➖" if v is None or v.skipped else "✅" if v.allowed else "❌"
        print(f"{icon} {rule:<16} {off[rule] if v is None else v.message}")
    hold = _hold(verdicts)
    if strategy.session_cap <= 0:
        print("Runs may start (the gate is off).")
    elif hold is None:
        print("Runs may start.")
    else:
        print(f"Held by {hold[0].rule} until ~{_utc(now.timestamp() + hold[1] * 3600)}.")
    return 0


COST_RE = re.compile(r"^(\S+) cost (\S+) (\S+) (\S+) turns=(\d+) .*\busd=([0-9.]+)\s*$")


def cost_line(lines: list[str], role: str, key: str, model: str) -> str | None:
    """The `cost` line for the last result line in a run log, or None when the log has none."""
    for line in reversed(lines):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("type") != "result":
            continue
        u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        tokens = {name: int(u.get(field) or 0) for name, field in (
            ("in", "input_tokens"), ("cache_read", "cache_read_input_tokens"),
            ("cache_write", "cache_creation_input_tokens"), ("out", "output_tokens"))}
        return (f"cost {key} {role} {model} turns={int(data.get('num_turns') or 0)} "
                + " ".join(f"{k}={v}" for k, v in tokens.items())
                + f" usd={float(data.get('total_cost_usd') or 0):.2f}")
    return None


def cmd_cost(args: list[str]) -> int:
    if len(args) not in (7, 9) or args[1::2] != ["--role", "--key", "--model", "--after"][:len(args) // 2]:
        return _usage()
    try:
        after = int(args[8]) if len(args) == 9 else 0
    except ValueError:
        return _usage()
    try:
        with open(args[0], "rb") as f:
            f.seek(max(after, 0))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return 0
    line = cost_line(lines, role=args[2], key=args[4], model=args[6])
    if line:
        print(line)
    return 0


def report(lines: list[str], since: datetime) -> str:
    """Sums the `cost` lines stamped at or after `since` by role and by repo."""
    by: dict[str, dict[str, list[float]]] = {"role": {}, "repo": {}}
    for line in lines:
        m = COST_RE.match(line)
        if not m:
            continue
        try:
            when = datetime.fromisoformat(m[1].replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < since:
            continue
        for kind, name in (("role", m[3]), ("repo", m[2].split("/")[0])):
            row = by[kind].setdefault(name, [0, 0, 0.0])
            row[0] += 1
            row[1] += int(m[5])
            row[2] += float(m[6])
    out = [f"Claude runs since {since:%Y-%m-%d %H:%M UTC} (USD: the API-equivalent cost, not a subscription charge)"]
    if not by["role"]:
        return out[0] + "\nNo runs."
    for kind in ("role", "repo"):
        out.append(f"By {kind}:")
        rows = sorted(by[kind].items(), key=lambda kv: -kv[1][2])
        out += [f"  {name:<14} {n:>4} runs {t:>6} turns  ${usd:,.2f}" for name, (n, t, usd) in rows]
    n, t, usd = (sum(r[i] for r in by["role"].values()) for i in range(3))
    out.append(f"  {'total':<14} {n:>4} runs {t:>6} turns  ${usd:,.2f}")
    return "\n".join(out)


def cmd_report(args: list[str]) -> int:
    days = 7.0
    if args:
        if len(args) != 2 or args[0] != "--days":
            return _usage()
        try:
            days = float(args[1])
        except ValueError:
            return _usage()
    log = config.state_dir() / "runs.log"
    lines = log.read_text(errors="replace").splitlines() if log.exists() else []
    print(report(lines, datetime.now().astimezone() - timedelta(days=days)))
    return 0


def _usage() -> int:
    print(__doc__.split("Usage:")[1].split("Standard")[0].rstrip(), file=sys.stderr)
    return 2


def main() -> int:
    config.load_env()  # ~/.config/agent-runner/env, so a plain shell gets the same settings as cron
    args = sys.argv[1:]
    if args and args[0] == "cost":
        return cmd_cost(args[1:])
    if args and args[0] == "report":
        return cmd_report(args[1:])
    commands = {"check": cmd_check, "status": cmd_status}
    if len(args) != 1 or args[0] not in commands:
        return _usage()
    return commands[args[0]]()


if __name__ == "__main__":
    sys.exit(main())
