"""usage.py: the strategy rules (pure), `check` exit codes and pauses, `status`."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import usage  # noqa: E402

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
T = NOW.timestamp()
H = 3600


def strategy(**env):
    return usage.strategy_from_env(env)


def win(five=0.15, five_reset=T + 2 * H, week=0.32, week_reset=T + 3.5 * 86400):
    w = {"five_hour": usage.Window(five, five_reset)}
    if week is not None:
        w["seven_day"] = usage.Window(week, week_reset)
    return w


def verdicts(w, s=None, now=NOW):
    return {v.rule: v for v in usage.evaluate(w, s or strategy(), now)}


def event(five=0.15, five_reset=T + 2 * H, week=0.32, week_reset=T + 3.5 * 86400):
    unified = {"five_hour": {"utilization": five, "resetsAt": five_reset}}
    if week is not None:
        unified["seven_day"] = {"utilization": week, "resetsAt": week_reset}
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": unified}})


@pytest.fixture
def env(monkeypatch):
    """A private os.environ with none of the strategy variables set, so `load_env()` can't leak into other tests."""
    clean = {k: v for k, v in usage.os.environ.items() if not k.startswith("AGENT_")}
    monkeypatch.setattr(usage.os, "environ", clean)
    monkeypatch.setattr(usage, "datetime", type("FixedNow", (datetime,), {"now": classmethod(lambda cls, tz=None: NOW)}))
    return clean


# windows / strategy_from_env

def test_windows_reads_both_and_skips_noise():
    lines = ["not json", '{"type": "result"}', event(0.15, 100, 0.32, 200)]
    assert usage.windows(lines) == {"five_hour": usage.Window(0.15, 100), "seven_day": usage.Window(0.32, 200)}
    assert usage.windows([event(week=None)]).keys() == {"five_hour"}


def test_strategy_defaults():
    assert strategy() == usage.Strategy(70, None, 40, 90, "even", 10)
    assert strategy(AGENT_DAY_HOURS="22-06", AGENT_WEEKLY_PACE="off").day_hours == (22, 6)


@pytest.mark.parametrize(("name", "value"), [
    ("AGENT_WEEKLY_PACE", "fast"), ("AGENT_DAY_HOURS", "8"), ("AGENT_DAY_HOURS", "08-08"), ("AGENT_DAY_HOURS", "25-03"),
    ("AGENT_MAX_WEEKLY_USAGE", "lots"), ("AGENT_PACE_SLACK", "-5"), ("AGENT_MAX_SESSION_USAGE", "120"),
])
def test_strategy_bad_values_name_the_variable(name, value):
    with pytest.raises(ValueError, match=name):
        strategy(**{name: value})


# Each rule at its edge, with its pause

@pytest.mark.parametrize(("five", "allowed"), [(0.69, True), (0.70, False)])
def test_session_cap_edge_pauses_until_5h_reset(five, allowed):
    v = verdicts(win(five=five))["session cap"]
    assert v.allowed is allowed
    assert v.pause_hours == (None if allowed else pytest.approx(2))


def test_session_cap_missing_reset_pauses_one_hour():
    assert verdicts(win(five=0.9, five_reset=None))["session cap"].pause_hours == 1.0


@pytest.mark.parametrize(("week", "allowed"), [(0.89, True), (0.90, False)])
def test_weekly_cap_edge_pauses_until_7d_reset(week, allowed):
    v = verdicts(win(week=week), strategy(AGENT_WEEKLY_PACE="off"))["weekly cap"]
    assert v.allowed is allowed
    assert v.pause_hours == (None if allowed else pytest.approx(3.5 * 24))


def test_weekly_pacing_edge_and_catch_up():
    # 3.5 of 7 days gone: allowance 90 x 0.5 + 10 = 55%.
    s = strategy()
    assert verdicts(win(week=0.55), s)["weekly pacing"].allowed
    v = verdicts(win(week=0.64), s)["weekly pacing"]
    assert not v.allowed
    assert "64% > 55% allowed (cap 90%, slack 10, 3.5 of 7 days gone)" == v.message
    # Allowance reaches 64% when (64 - 10) / 90 = 0.6 of the week is gone: 4.2 days, 0.7 days from now.
    assert v.pause_hours == pytest.approx(0.7 * 24)


def test_weekly_pacing_pause_capped_at_reset():
    # 89% is under the cap but needs (89 - 10) / 90 of the week; slack 0 and cap 50 push the catch-up past the reset.
    v = verdicts(win(week=0.89), strategy(AGENT_MAX_WEEKLY_USAGE="0", AGENT_PACE_SLACK="0"))["weekly pacing"]
    assert not v.allowed and v.pause_hours == pytest.approx(0.89 * 7 * 24 - 3.5 * 24)
    v = verdicts(win(week=0.95), strategy(AGENT_MAX_WEEKLY_USAGE="50", AGENT_PACE_SLACK="0"))["weekly pacing"]
    assert not v.allowed and v.pause_hours == pytest.approx(3.5 * 24)


def test_fresh_week_can_start_at_once():
    w = win(week=0.05, week_reset=T + 7 * 86400)
    assert verdicts(w)["weekly pacing"].allowed


@pytest.mark.parametrize(("hour", "inside"), [(7, False), (8, True), (21, True), (22, False)])
def test_daytime_reserve_inside_and_outside(hour, inside):
    now = NOW.replace(hour=hour)
    v = verdicts(win(five=0.5, five_reset=now.timestamp() + 4 * H), strategy(AGENT_DAY_HOURS="08-22"), now)
    assert v["daytime reserve"].allowed is not inside
    assert v["session cap"].allowed


def test_daytime_reserve_edge_and_pause_is_earlier_of_reset_and_day_end():
    s = strategy(AGENT_DAY_HOURS="08-22", AGENT_DAY_SESSION_USAGE="40")  # NOW is 12:00, day ends in 10 h
    assert verdicts(win(five=0.39), s)["daytime reserve"].allowed
    assert verdicts(win(five=0.40), s)["daytime reserve"].pause_hours == pytest.approx(2)
    assert verdicts(win(five=0.40, five_reset=T + 4 * H), strategy(AGENT_DAY_HOURS="08-14"))[
        "daytime reserve"].pause_hours == pytest.approx(2)


@pytest.mark.parametrize(("hour", "inside", "pause"), [(23, True, 7), (3, True, 3), (6, False, None), (12, False, None)])
def test_daytime_reserve_wraps_midnight(hour, inside, pause):
    now = NOW.replace(hour=hour)
    v = verdicts(win(five=0.5, five_reset=now.timestamp() + 20 * H), strategy(AGENT_DAY_HOURS="22-06"), now)
    assert v["daytime reserve"].allowed is not inside
    assert v["daytime reserve"].pause_hours == (pytest.approx(pause) if pause else None)


def test_no_seven_day_window_skips_weekly_rules():
    v = verdicts(win(week=None))
    assert v["weekly cap"].allowed and v["weekly cap"].skipped
    assert v["weekly pacing"].allowed and v["weekly pacing"].skipped


def test_off_rules_are_left_out():
    s = strategy(AGENT_MAX_WEEKLY_USAGE="0", AGENT_WEEKLY_PACE="off", AGENT_DAY_HOURS="08-22", AGENT_DAY_SESSION_USAGE="0")
    assert list(verdicts(win(), s)) == ["session cap"]


# check

def test_check_ok_line(env, monkeypatch, capsys):
    monkeypatch.setattr(usage, "probe", lambda: [event(0.15, week=0.32)])
    assert usage.cmd_check() == 0
    assert capsys.readouterr().out == "usage ok: 5h 15%, 7d 32% (pace allows 55%)\n"


def test_check_largest_pause_wins(env, monkeypatch, capsys):
    env["AGENT_WEEKLY_PACE"] = "off"
    monkeypatch.setattr(usage, "probe", lambda: [event(0.8, T + 2 * H, 0.95, T + 30.5 * H)])
    assert usage.cmd_check() == 10
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "session cap: 80% ≥ 70%; weekly cap: 95% ≥ 90%"
    assert float(out[-1]) == 30.5


def test_check_minimum_pause_one_tick(env, monkeypatch, capsys):
    monkeypatch.setattr(usage, "probe", lambda: [event(0.8, T - 60)])
    assert usage.cmd_check() == 10
    assert float(capsys.readouterr().out.splitlines()[-1]) == pytest.approx(1 / 12, abs=0.01)


@pytest.mark.parametrize(("name", "value"), [("AGENT_WEEKLY_PACE", "fast"), ("AGENT_DAY_HOURS", "8")])
def test_check_bad_settings_exit_11(env, monkeypatch, capsys, name, value):
    env[name] = value
    monkeypatch.setattr(usage, "probe", lambda: pytest.fail("no probe with bad settings"))
    assert usage.cmd_check() == 11
    assert name in capsys.readouterr().out


@pytest.mark.parametrize(("five", "code"), [(0.69, 0), (0.70, 10)])
def test_check_old_default_70_holds(env, monkeypatch, five, code):
    monkeypatch.setattr(usage, "probe", lambda: [event(five, week=0.1)])
    assert usage.cmd_check() == code
