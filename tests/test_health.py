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
