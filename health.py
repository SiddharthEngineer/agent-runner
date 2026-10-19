#!/usr/bin/env python3
"""VPS health: measure disk, memory, swap and load, and free disk with fixed cleanup rules (design D6).

Usage: python3 health.py check                          (JSON metrics and problems; exit 1 when there are problems)
       python3 health.py clean [--dry-run] [--pressure]  (one line per rule: what it would free, or freed)

Thresholds default to DEFAULTS and can be overridden in ~/.config/agent-runner/health.toml under [thresholds].
Rules are code, not config: each one deletes only under its own root, and never follows symlinks.
Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

DEFAULTS = {"disk_clean_pct": 80, "disk_alert_pct": 90, "mem_available_mb": 400, "swap_used_mb": 2048, "load_per_cpu": 2.0}
UNITS = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "KIB": 2**10, "MIB": 2**20, "GIB": 2**30}


@dataclass
class Metrics:
    disk_pct: float
    disk_free_gb: float
    mem_available_mb: int
    swap_used_mb: int
    load15_per_cpu: float
    build_cache_gb: float | None


@dataclass
class Problem:
    key: str  # disk, disk-critical, memory, swap, load
    summary: str
    cleanable: bool


class HealthError(Exception):
    pass


def parse_size(text: str) -> float:
    """Bytes from Docker's sizes ("4.6GB", "86.02kB", "0B")."""
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]*)", text)
    if not m:
        return 0.0
    return float(m.group(1)) * UNITS.get(m.group(2).upper() or "B", 1)


def docker_df() -> list[dict] | None:
    try:
        out = subprocess.run(["docker", "system", "df", "--format", "{{json .}}"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def build_cache_bytes() -> float | None:
    rows = docker_df()
    if rows is None:
        return None
    return sum(parse_size(r.get("Size", "0B")) for r in rows if r.get("Type") == "Build Cache")


def read_meminfo(path: Path) -> dict[str, int]:
    """kB values from /proc/meminfo."""
    values = {}
    for line in path.read_text().splitlines():
        key, _, rest = line.partition(":")
        if rest.split():
            values[key] = int(rest.split()[0])
    return values


def measure(root: str = "/", meminfo: Path = Path("/proc/meminfo")) -> Metrics:
    usage = shutil.disk_usage(root)
    mem = read_meminfo(meminfo)
    cache = build_cache_bytes()
    return Metrics(
        disk_pct=round(100 * usage.used / usage.total, 1),
        disk_free_gb=round(usage.free / 1e9, 2),
        mem_available_mb=mem.get("MemAvailable", 0) // 1024,
        swap_used_mb=(mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) // 1024,
        load15_per_cpu=round(os.getloadavg()[2] / (os.cpu_count() or 1), 2),
        build_cache_gb=None if cache is None else round(cache / 1e9, 2),
    )


def thresholds(path: Path | None = None) -> dict:
    """DEFAULTS merged with health.toml's [thresholds]. An unknown key is an error."""
    path = path or config.config_dir() / "health.toml"
    merged = dict(DEFAULTS)
    if path.is_file():
        try:
            table = tomllib.loads(path.read_text()).get("thresholds", {})
        except tomllib.TOMLDecodeError as exc:
            raise HealthError(f"{path}: {exc}") from exc
        unknown = sorted(set(table) - set(DEFAULTS))
        if unknown:
            raise HealthError(f"{path}: unknown threshold {', '.join(unknown)}")
        merged.update(table)
    return merged


def problems(m: Metrics, th: dict) -> list[Problem]:
    found = []
    if m.disk_pct >= th["disk_clean_pct"]:
        found.append(Problem("disk", f"disk {m.disk_pct}% used (cleanup at {th['disk_clean_pct']}%)", True))
    if m.disk_pct >= th["disk_alert_pct"]:
        found.append(Problem("disk-critical", f"disk {m.disk_pct}% used (alert at {th['disk_alert_pct']}%)", True))
    if m.mem_available_mb < th["mem_available_mb"]:
        found.append(Problem("memory", f"{m.mem_available_mb} MB available (under {th['mem_available_mb']} MB)", False))
    if m.swap_used_mb > th["swap_used_mb"]:
        found.append(Problem("swap", f"{m.swap_used_mb} MB swap used (over {th['swap_used_mb']} MB)", False))
    if m.load15_per_cpu > th["load_per_cpu"]:
        found.append(Problem("load", f"15-min load {m.load15_per_cpu} per CPU (over {th['load_per_cpu']})", False))
    return found


def cmd_check() -> int:
    m = measure()
    found = problems(m, thresholds())
    print(json.dumps({"metrics": asdict(m), "problems": [asdict(p) for p in found]}, indent=2))
    return 1 if found else 0


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["check"]:
            return cmd_check()
    except HealthError as exc:
        print(exc, file=sys.stderr)
        return 2
    print("usage: health.py check | clean [--dry-run] [--pressure]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
