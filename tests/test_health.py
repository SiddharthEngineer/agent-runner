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
