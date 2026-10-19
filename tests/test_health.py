"""Tests for health.py: metrics, thresholds and the fixed cleanup rules. Never touches the real VPS state."""

import sys
from collections import namedtuple
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import health  # noqa: E402

Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNNER_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("STRELITZIA_AGENT_STATE", str(tmp_path / "old-state"))
    return tmp_path


@pytest.fixture
def meminfo(tmp_path):
    def write(available_mb=1700, swap_total_mb=4096, swap_free_mb=3500):
        path = tmp_path / "meminfo"
        path.write_text(
            f"MemTotal:        3900000 kB\nMemAvailable:    {available_mb * 1024} kB\n"
            f"SwapTotal:       {swap_total_mb * 1024} kB\nSwapFree:        {swap_free_mb * 1024} kB\n"
        )
        return path

    return write


@pytest.fixture
def fake_machine(monkeypatch):
    def setup(disk_pct=71, load15=0.5):
        monkeypatch.setattr(health.shutil, "disk_usage", lambda root: Usage(100, disk_pct, 100 - disk_pct))
        monkeypatch.setattr(health.os, "getloadavg", lambda: (0.0, 0.0, load15))
        monkeypatch.setattr(health.os, "cpu_count", lambda: 2)
        monkeypatch.setattr(health, "build_cache_bytes", lambda: 4.8e9)

    return setup


def keys(found):
    return {(p.key, p.cleanable) for p in found}


def test_healthy(home, meminfo, fake_machine):
    fake_machine()
    m = health.measure(meminfo=meminfo())
    assert (m.disk_pct, m.mem_available_mb, m.swap_used_mb, m.load15_per_cpu, m.build_cache_gb) == (71.0, 1700, 596, 0.25, 4.8)
    assert health.problems(m, health.thresholds()) == []


def test_disk_over_cleanup_threshold(home, meminfo, fake_machine):
    fake_machine(disk_pct=85)
    assert keys(health.problems(health.measure(meminfo=meminfo()), health.thresholds())) == {("disk", True)}


def test_disk_critical(home, meminfo, fake_machine):
    fake_machine(disk_pct=92)
    found = health.problems(health.measure(meminfo=meminfo()), health.thresholds())
    assert keys(found) == {("disk", True), ("disk-critical", True)}


def test_memory_swap_load_not_cleanable(home, meminfo, fake_machine):
    fake_machine(load15=4.4)
    m = health.measure(meminfo=meminfo(available_mb=350, swap_free_mb=1000))
    assert keys(health.problems(m, health.thresholds())) == {("memory", False), ("swap", False), ("load", False)}


def test_thresholds_override_and_unknown_key(home):
    toml = home / ".config/agent-runner/health.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text("[thresholds]\ndisk_clean_pct = 70\n")
    assert health.thresholds()["disk_clean_pct"] == 70
    toml.write_text("[thresholds]\ndisk_clean = 70\n")
    with pytest.raises(health.HealthError, match="unknown threshold disk_clean"):
        health.thresholds()


def test_parse_size():
    assert health.parse_size("4.6GB") == 4.6e9
    assert health.parse_size("86.02kB") == 86020
    assert health.parse_size("0B") == 0


class FakeDocker:
    """Records every subprocess argv; `docker system df` reports a 5 GB build cache."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        from subprocess import CompletedProcess

        if argv[:3] == ["docker", "system", "df"]:
            out = '{"Type":"Build Cache","Size":"5GB","Reclaimable":"5GB"}\n{"Type":"Images","Size":"9GB","Reclaimable":"1GB"}\n'
            return CompletedProcess(argv, 0, out, "")
        if argv[1] in self.fail:
            return CompletedProcess(argv, 1, "", "daemon down")
        return CompletedProcess(argv, 0, "Total reclaimed space: 1.5GB\n", "")


@pytest.fixture
def fake_docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(health.subprocess, "run", fake)
    monkeypatch.setattr(health, "all_rules", health.docker_rules)  # Docker rules only
    return fake


def metrics(disk_pct):
    return health.Metrics(disk_pct, 10, 2000, 100, 0.2, 5.0)


def docker_calls(fake):
    return [c for c in fake.calls if c[:3] != ["docker", "system", "df"]]


def test_docker_argv_exact(home, fake_docker):
    health.clean(force_pressure=True, measure_fn=lambda: metrics(85))
    assert docker_calls(fake_docker) == [
        ["docker", "builder", "prune", "-f", "--keep-storage", "3GB"],
        ["docker", "container", "prune", "-f", "--filter", "until=24h"],
        ["docker", "builder", "prune", "-af"],
        ["docker", "image", "prune", "-af", "--filter", "until=168h"],
    ]
    flat = [arg for call in fake_docker.calls for arg in call]
    assert "volume" not in flat and "system" not in [c[1] for c in docker_calls(fake_docker)]


def test_pressure_stops_once_under_threshold(home, fake_docker):
    readings = iter([85, 79])  # over before the first pressure rule, under after it
    results = health.clean(measure_fn=lambda: metrics(next(readings)))
    assert [r["rule"] for r in results if r["tier"] == "pressure"] == ["docker build cache (all)"]
    assert results[-1]["freed_bytes"] == 1_500_000_000


def test_no_pressure_when_disk_is_fine(home, fake_docker):
    results = health.clean(measure_fn=lambda: metrics(70))
    assert {r["tier"] for r in results} == {"routine"}


def test_routine_runs_once_per_23h(home, fake_docker):
    assert len(health.clean(measure_fn=lambda: metrics(70))) == 2
    assert health.clean(measure_fn=lambda: metrics(70)) == []
    log = (home / "state/health.log").read_text().splitlines()
    assert len(log) == 2 and '"rule": "docker build cache over 3GB"' in log[0]


def test_dry_run_runs_nothing_and_estimates(home, fake_docker):
    results = health.clean(dry_run=True, force_pressure=True, measure_fn=lambda: metrics(70))
    assert docker_calls(fake_docker) == []
    assert [r["freed_bytes"] for r in results][:1] == [2_000_000_000]  # 5 GB cache minus the 3 GB kept
    assert not (home / "state/health.json").exists()


def test_docker_error_is_logged_and_next_rule_runs(home, monkeypatch):
    fake = FakeDocker(fail={"builder"})
    monkeypatch.setattr(health.subprocess, "run", fake)
    monkeypatch.setattr(health, "all_rules", health.docker_rules)
    results = health.clean(measure_fn=lambda: metrics(70))
    assert "daemon down" in results[0]["error"]
    assert results[1]["freed_bytes"] == 1_500_000_000


def aged(path: Path, days: float, content: str = "x" * 100) -> Path:
    import os
    import time

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content)
    t = time.time() - days * 86400
    os.utime(path, (t, t), follow_symlinks=False)
    return path


def test_symlink_and_outside_paths_refused(home, tmp_path):
    root = tmp_path / "root"
    outside = aged(tmp_path / "outside/keep.txt", 30)
    root.mkdir()
    link = root / "link"
    link.symlink_to(outside)
    dir_link = root / "dirlink"
    dir_link.symlink_to(outside.parent)
    assert health.delete_under(root, [link, dir_link, dir_link / "keep.txt", outside, root], dry_run=False) == 0
    assert link.is_symlink() and outside.exists()
    victim = aged(root / "old.txt", 30)
    assert health.delete_under(root, [victim], dry_run=True) == 100 and victim.exists()
    assert health.delete_under(root, [victim], dry_run=False) == 100 and not victim.exists()


def test_backups_by_tier(home, monkeypatch):
    backups = home / "backups"
    monkeypatch.setattr(health, "backup_dirs", lambda: [backups])
    two_days = aged(backups / "two-days.sql.gz", 2)
    two_hours = aged(backups / "two-hours.sql.gz", 2 / 24)
    old = aged(backups / "ten-days.sql.gz", 10)
    rules = {r.name: r for r in health.file_rules()}
    rules["backups over 7d"].apply()
    assert not old.exists() and two_days.exists()
    rules["backups over 24h"].apply()
    assert not two_days.exists() and two_hours.exists()


def test_tmp_skips_claude_and_recent(home, tmp_path):
    tmp = tmp_path / "tmp"
    aged(tmp / "claude-1000/scratch.txt", 10)
    aged(tmp / "claude-1000", 10)
    old = aged(tmp / "pytest-of-dev/x.txt", 10)
    aged(tmp / "pytest-of-dev", 10)
    recent_dir = tmp / "recent"
    aged(recent_dir / "old.txt", 10)
    aged(recent_dir / "new.txt", 0)
    [(root, paths)] = health.tmp_targets(tmp)
    assert paths == [old.parent]


def test_logs_and_results_ages(home):
    state = home / "state"
    old_log, new_log = aged(state / "logs/a.log", 15), aged(state / "logs/b.log", 13)
    old_res, new_res = aged(state / "results/a.json", 31), aged(state / "results/b.json", 20)
    for rule in health.file_rules()[:2]:
        rule.apply()
    assert (old_log.exists(), new_log.exists(), old_res.exists(), new_res.exists()) == (False, True, False, True)


def test_playwright_keeps_newest_build(tmp_path):
    cache = tmp_path / "ms-playwright"
    for name in ("chromium-1100", "chromium-1248", "chromium_headless_shell-1248", "ffmpeg-1013", ".links"):
        (cache / name).mkdir(parents=True)
    [(root, old)] = health.playwright_old_builds(cache)
    assert [p.name for p in old] == ["chromium-1100"]


def git(cwd, *args):
    import subprocess

    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def test_worktree_with_live_branch_survives(home):
    import os
    import time

    origin, clone = home / "origin.git", home / "clone"
    git(home, "init", "-q", "--bare", str(origin))
    git(home, "clone", "-q", str(origin), str(clone))
    git(clone, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    git(clone, "push", "-q", "origin", "HEAD:refs/heads/main")
    wts = home / "state/worktrees"
    for slug in ("alive", "gone"):
        git(clone, "worktree", "add", "-q", "-b", f"agent/{slug}", str(wts / slug))
        old = time.time() - 8 * 86400
        os.utime(wts / slug, (old, old))
    git(clone, "push", "-q", "origin", "agent/alive")
    assert [wt.name for _, wt in health.finished_worktrees()] == ["gone"]
    health.worktree_rule().apply()
    assert (wts / "alive").exists() and not (wts / "gone").exists()


@pytest.fixture
def ticker(home, monkeypatch):
    """tick() with measure/clean/notify stubbed; `t.disk` and `t.load` set the next reading, `t.at` the clock."""
    from datetime import datetime, timezone

    class T:
        disk, load = 70, 0.2
        at = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        calls: list = []

    t = T()
    t.calls = []
    monkeypatch.setattr(health, "measure", lambda *a, **k: health.Metrics(t.disk, 10, 2000, 100, t.load, 1.0))
    monkeypatch.setattr(health, "clean", lambda *a, **k: t.calls.append("clean") or [])
    monkeypatch.setattr(health, "now", lambda: t.at)
    monkeypatch.setattr(health, "notify_new", lambda p, *a: t.calls.append(("new", p.key)) or 7)
    monkeypatch.setattr(health, "notify_again", lambda e, p, *a: t.calls.append(("again", p.key)) or True)
    monkeypatch.setattr(health, "notify_recovered", lambda e, key, m: t.calls.append(("recovered", key)) or True)
    monkeypatch.setattr(health.config, "load_env", lambda: None)
    return t


def notices(t):
    return [c for c in t.calls if c != "clean"]


def test_tick_one_notice_per_problem(ticker):
    from datetime import timedelta

    ticker.disk = 85
    health.tick()
    ticker.at += timedelta(minutes=15)
    health.tick()
    assert notices(ticker) == [("new", "disk")]
    ticker.at += timedelta(hours=24)
    health.tick()
    assert notices(ticker)[-1] == ("again", "disk")
    ticker.disk = 70
    health.tick()
    assert notices(ticker)[-1] == ("recovered", "disk")
    assert health.load_state()["open"] == {}


def test_tick_load_needs_two_ticks_15_min_apart(ticker):
    from datetime import timedelta

    ticker.load = 3.0
    health.tick()
    ticker.at += timedelta(minutes=5)
    health.tick()
    assert notices(ticker) == []
    ticker.at += timedelta(minutes=10)
    health.tick()
    assert notices(ticker) == [("new", "load")]


def test_tick_failed_notice_is_retried(ticker, monkeypatch):
    ticker.disk = 85
    monkeypatch.setattr(health, "notify_new", lambda *a: None)
    health.tick()
    assert health.load_state()["open"] == {}


def test_tick_lock_held_exits_quietly(ticker):
    import fcntl

    (ticker_state := health.config.state_dir()).mkdir(parents=True, exist_ok=True)
    with open(ticker_state / "health.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ticker.disk = 95
        assert health.tick() == 0
    assert ticker.calls == []


def test_gate(ticker):
    ticker.disk = 89
    assert health.gate() == 0
    ticker.disk = 90
    assert health.gate() == 1
    assert ticker.calls == []


def test_notices_call_inbox(home, monkeypatch):
    calls = []

    def fake_inbox(*args):
        calls.append(args)
        return '{"number": 9, "url": "u"}' if args[0] == "notify" else "u"

    monkeypatch.setattr(health, "inbox", fake_inbox)
    m = health.Metrics(91, 3, 2000, 100, 0.2, 1.0)
    p = health.Problem("disk-critical", "disk 91% used (alert at 90%)", True)
    cleaned = [{"rule": "docker build cache (all)", "freed_bytes": 2_000_000_000}]
    assert health.notify_new(p, m, cleaned, "uv cache is 1 GB") == 9
    args = calls[0]
    assert args[:4] == ("notify", "--project", "vps-health", "--json")
    assert args[args.index("--title") + 1] == "[vps] disk 91% used (alert at 90%) after cleanup"
    body = args[args.index("--body") + 1]
    assert "freed 2.0 GB" in body and "uv cache is 1 GB" in body
    assert health.notify_recovered({"issue": 9}, "disk", m)
    assert calls[1][:2] == ("comment", "9") and calls[1][-1] == "--close"


def test_failed_inbox_call_returns_none(home, monkeypatch):
    monkeypatch.setattr(health, "inbox", lambda *a: None)
    m = health.Metrics(91, 3, 2000, 100, 0.2, 1.0)
    assert health.notify_new(health.Problem("disk", "d", True), m, [], None) is None
    assert health.notify_again({"issue": 1, "since": "x"}, health.Problem("disk", "d", True), m, []) is False
