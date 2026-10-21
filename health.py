#!/usr/bin/env python3
"""VPS health: measure disk, memory, swap and load, and free disk with fixed cleanup rules (design D6).

Usage: python3 health.py check                          (JSON metrics and problems; exit 1 when there are problems)
       python3 health.py clean [--dry-run] [--pressure]  (one line per rule: what it would free, or freed)
       python3 health.py tick                           (cron, every 15 min: measure, clean, measure, notify)
       python3 health.py gate                           (exit 1 while disk is at or over disk_alert_pct)
       python3 health.py diagnose [--force]             (the read-only Sonnet diagnosis; --force ignores the 6 h spacing)

Thresholds default to DEFAULTS and can be overridden in ~/.config/agent-runner/health.toml under [thresholds].
Rules are code, not config: each one deletes only under its own root, and never follows symlinks.
Standard library only.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

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


@dataclass
class Rule:
    name: str
    tier: Literal["routine", "pressure"]
    estimate: Callable[[], int]  # bytes it would free
    apply: Callable[[], int]  # bytes freed


ROUTINE_EVERY = timedelta(hours=23)


def now() -> datetime:
    return datetime.now(timezone.utc)


def state_file() -> Path:
    return config.state_dir() / "health.json"


def load_state() -> dict:
    try:
        return json.loads(state_file().read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    state_file().parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(state_file())


def log(entry: dict) -> None:
    path = config.state_dir() / "health.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"time": now().strftime("%Y-%m-%dT%H:%M:%SZ"), **entry}) + "\n")


# --- Docker rules. Never `volume`, never `system prune`: volumes hold the databases.

def docker(*args: str) -> int:
    """Run `docker <args>` and return the bytes it reports reclaimed. Raises on failure."""
    out = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=600)
    if out.returncode != 0:
        raise HealthError(f"docker {' '.join(args)}: {(out.stderr or out.stdout).strip()[-200:]}")
    m = re.search(r"Total(?: reclaimed space)?:\s*(\S+)", out.stdout)
    return int(parse_size(m.group(1))) if m else 0


def docker_reclaimable(kind: str) -> int:
    rows = docker_df() or []
    return int(sum(parse_size(r.get("Reclaimable", "0B")) for r in rows if r.get("Type") == kind))


def docker_rules() -> list[Rule]:
    def cache_over(keep: float) -> int:
        return int(max(0.0, (build_cache_bytes() or 0) - keep))

    return [
        Rule("docker build cache over 3GB", "routine", lambda: cache_over(3e9),
             lambda: docker("builder", "prune", "-f", "--keep-storage", "3GB")),
        Rule("docker containers exited over 24h", "routine", lambda: docker_reclaimable("Containers"),
             lambda: docker("container", "prune", "-f", "--filter", "until=24h")),
        Rule("docker build cache (all)", "pressure", lambda: cache_over(0),
             lambda: docker("builder", "prune", "-af")),
        Rule("docker images unused 7d", "pressure", lambda: docker_reclaimable("Images"),
             lambda: docker("image", "prune", "-af", "--filter", "until=168h")),
    ]


# --- File rules. Each deletes only strictly inside its root, and never a symlink or anything reached through one.

DAY = 86400.0


def newest_mtime(path: Path) -> float:
    """The newest mtime of `path` or anything under it, without following symlinks."""
    newest = path.lstat().st_mtime
    if path.is_dir() and not path.is_symlink():
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            for name in dirnames + filenames:
                try:
                    newest = max(newest, os.lstat(os.path.join(dirpath, name)).st_mtime)
                except OSError:
                    pass
    return newest


def size_of(path: Path) -> int:
    if path.is_symlink() or not path.is_dir():
        return path.lstat().st_size
    total = 0
    for dirpath, _, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def inside(root: Path, path: Path) -> bool:
    """True when `path`, resolved, is strictly inside `root`, resolved."""
    real_root, real = os.path.realpath(root), os.path.realpath(path)
    return real != real_root and real.startswith(real_root.rstrip(os.sep) + os.sep)


def delete_under(root: Path, paths, dry_run: bool) -> int:
    """Delete each path that is strictly inside `root` and not a symlink; return the bytes freed (or that would be)."""
    freed = 0
    for path in paths:
        path = Path(path)
        if path.is_symlink() or not inside(root, path):
            log({"rule": "refused", "path": str(path), "root": str(root), "dry_run": dry_run})
            continue
        try:
            size = size_of(path)
            if not dry_run:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        except OSError as exc:
            log({"rule": "error", "path": str(path), "error": str(exc), "dry_run": dry_run})
            continue
        freed += size
    return freed


def older_than(root: Path, days: float, pattern: str = "*") -> list[Path]:
    if not root.is_dir():
        return []
    cutoff = time.time() - days * DAY
    return sorted(p for p in root.glob(pattern) if newest_mtime(p) < cutoff)


def state_dirs() -> list[Path]:
    """The runner's state dir (a list, so rules can iterate it)."""
    return [config.state_dir()]


def backup_dirs() -> list[Path]:
    return [Path.home() / "backups", Path("/srv/backups")]


def files_rule(name: str, tier: str, targets: Callable[[], list[tuple[Path, list[Path]]]]) -> Rule:
    """A rule over (root, paths) pairs; `targets` is re-evaluated for the estimate and for the run."""

    def run(dry_run: bool) -> int:
        return sum(delete_under(root, paths, dry_run) for root, paths in targets())

    return Rule(name, tier, lambda: run(True), lambda: run(False))


def tmp_targets(tmp: Path = Path("/tmp"), days: float = 3) -> list[tuple[Path, list[Path]]]:
    """Top-level /tmp entries the user owns and nobody touched for `days`. `claude-*` holds live sessions' scratchpads."""
    uid, cutoff, paths = os.getuid(), time.time() - days * DAY, []
    for entry in sorted(tmp.iterdir()) if tmp.is_dir() else []:
        try:
            if entry.name.startswith("claude-") or entry.lstat().st_uid != uid or entry.is_symlink():
                continue
            if newest_mtime(entry) < cutoff:
                paths.append(entry)
        except OSError:
            continue
    return [(tmp, paths)]


def git_out(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=60)


def finished_worktrees(days: float = 7) -> list[tuple[Path, Path]]:
    """(owning repo, worktree) for runner worktrees older than `days` whose branch is gone from origin.

    Layouts: worktrees/<slug> (old runner) and worktrees/<repo>/<slug>.
    """
    found = []
    for state in state_dirs():
        root = state / "worktrees"
        candidates = [p for p in root.glob("*") if (p / ".git").is_file()]
        candidates += [p for p in root.glob("*/*") if (p / ".git").is_file()]
        cutoff = time.time() - days * DAY
        for wt in sorted(candidates):
            if wt.is_symlink() or not inside(root, wt) or wt.lstat().st_mtime >= cutoff:
                continue
            common = git_out("-C", str(wt), "rev-parse", "--path-format=absolute", "--git-common-dir")
            branch = git_out("-C", str(wt), "branch", "--show-current").stdout.strip()
            if common.returncode != 0 or not branch:
                continue
            remote = git_out("-C", str(wt), "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}")
            if remote.returncode == 2:  # 2 = no such ref; anything else (network, auth) keeps the worktree
                found.append((Path(common.stdout.strip()).parent, wt))
    return found


def worktree_rule() -> Rule:
    def estimate() -> int:
        return sum(size_of(wt) for _, wt in finished_worktrees())

    def apply() -> int:
        freed = 0
        for owner, wt in finished_worktrees():
            size = size_of(wt)
            if git_out("-C", str(owner), "worktree", "remove", "--force", str(wt)).returncode == 0:
                freed += size
            git_out("-C", str(owner), "worktree", "prune")
        return freed

    return Rule("runner worktrees whose branch is gone (7d)", "routine", estimate, apply)


def playwright_old_builds(cache: Path | None = None) -> list[tuple[Path, list[Path]]]:
    """All but the highest-numbered `<browser>-<build>` dir of each browser."""
    cache = cache or Path.home() / ".cache/ms-playwright"
    newest: dict[str, tuple[int, Path]] = {}
    old = []
    for d in sorted(cache.iterdir()) if cache.is_dir() else []:
        m = re.fullmatch(r"(.+)-(\d+)", d.name)
        if not m or not d.is_dir():
            continue
        prefix, build = m.group(1), int(m.group(2))
        if prefix in newest:
            keep_build, keep = newest[prefix]
            if build > keep_build:
                old.append(keep)
                newest[prefix] = (build, d)
            else:
                old.append(d)
        else:
            newest[prefix] = (build, d)
    return [(cache, old)]


def tool_rule(name: str, argv: list[str], cache: Path) -> Rule:
    """A pressure rule that runs a tool's own cache cleanup; freed bytes are measured as the cache dir's shrinkage."""

    def apply() -> int:
        if shutil.which(argv[0]) is None:
            return 0
        before = size_of(cache) if cache.is_dir() else 0
        out = subprocess.run(argv, capture_output=True, text=True, timeout=600)
        if out.returncode != 0:
            raise HealthError(f"{' '.join(argv)}: {(out.stderr or out.stdout).strip()[-200:]}")
        return max(0, before - (size_of(cache) if cache.is_dir() else 0))

    # The tools can't say what they'd free without doing it, so the estimate is the cache's whole size (an upper bound).
    return Rule(name, "pressure", lambda: size_of(cache) if cache.is_dir() else 0, apply)


def file_rules() -> list[Rule]:
    home = Path.home()
    return [
        files_rule("runner logs over 14d", "routine", lambda: [(s / "logs", older_than(s / "logs", 14)) for s in state_dirs()]),
        files_rule("runner results over 30d", "routine",
                   lambda: [(s / "results", older_than(s / "results", 30)) for s in state_dirs()]),
        worktree_rule(),
        files_rule("/tmp entries untouched 3d", "routine", tmp_targets),
        files_rule("backups over 7d", "routine", lambda: [(b, older_than(b, 7)) for b in backup_dirs()]),
        files_rule("backups over 24h", "pressure", lambda: [(b, older_than(b, 1)) for b in backup_dirs()]),
        tool_rule("uv cache prune", ["uv", "cache", "prune"], home / ".cache/uv"),
        tool_rule("pip cache purge", ["pip", "cache", "purge"], home / ".cache/pip"),
        files_rule("old Playwright builds", "pressure", playwright_old_builds),
    ]


def all_rules() -> list[Rule]:
    """Routine rules, then pressure rules cheapest-to-lose first (design D6 order)."""
    rules = docker_rules() + file_rules()
    return [r for r in rules if r.tier == "routine"] + [r for r in rules if r.tier == "pressure"]


def run_rule(rule: Rule, dry_run: bool) -> dict:
    result = {"rule": rule.name, "tier": rule.tier, "dry_run": dry_run}
    try:
        result["freed_bytes"] = rule.estimate() if dry_run else rule.apply()
    except (HealthError, OSError, subprocess.SubprocessError) as exc:
        result["freed_bytes"] = 0
        result["error"] = str(exc)
    log(result)
    return result


def clean(dry_run: bool = False, force_pressure: bool = False, rules: list[Rule] | None = None,
          measure_fn: Callable[[], Metrics] = measure) -> list[dict]:
    """Routine rules at most once per ROUTINE_EVERY; pressure rules in order while disk is over the cleanup threshold."""
    rules = all_rules() if rules is None else rules
    th = thresholds()
    state = load_state()
    results = []

    last = state.get("routine_at")
    due = last is None or now() - datetime.fromisoformat(last) > ROUTINE_EVERY
    if due or dry_run:
        results += [run_rule(r, dry_run) for r in rules if r.tier == "routine"]
        if not dry_run:
            state["routine_at"] = now().isoformat()
            save_state(state)

    for rule in (r for r in rules if r.tier == "pressure"):
        if not force_pressure and measure_fn().disk_pct < th["disk_clean_pct"]:
            break
        results.append(run_rule(rule, dry_run))
    return results


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return str(n)


def cmd_clean(argv: list[str]) -> int:
    dry_run, pressure = "--dry-run" in argv, "--pressure" in argv
    results = clean(dry_run=dry_run, force_pressure=pressure)
    if not results:
        print("nothing due (routine ran in the last 23 h; disk under the cleanup threshold)")
    for r in results:
        verb = "would free" if dry_run else "freed"
        line = f"{r['tier']:8}  {r['rule']}: {verb} {human(r['freed_bytes'])}"
        print(line + (f"  (error: {r['error']})" if "error" in r else ""))
    return 1 if any("error" in r for r in results) else 0


# --- tick: measure, clean, measure again, and keep one inbox issue per open problem.

LOAD_CONFIRM = timedelta(minutes=15)  # load is a problem only when it was also high on a tick at least this much earlier
RENOTIFY = timedelta(hours=24)


def confirmed(found: list[Problem], state: dict, at: datetime) -> list[Problem]:
    """Drop `load` unless it was already high at least LOAD_CONFIRM ago (`state["load_since"]`)."""
    if not any(p.key == "load" for p in found):
        state.pop("load_since", None)
        return found
    since = state.setdefault("load_since", at.isoformat())
    if at - datetime.fromisoformat(since) >= LOAD_CONFIRM:
        return found
    return [p for p in found if p.key != "load"]


def inbox(*args: str) -> str | None:
    """Run `inbox.py <args>`; its stdout, or None (logged) when gh or the inbox isn't reachable."""
    out = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "inbox.py"), *args],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        log({"rule": "notice-failed", "args": args[:1], "error": (out.stderr or out.stdout).strip()[-300:]})
        return None
    return out.stdout.strip()


def report(m: Metrics, cleaned: list[dict], diagnosis: str | None = None) -> str:
    """Issue/comment body: metrics, what this tick's cleanup freed, the diagnosis. Never file contents."""
    lines = ["**Metrics**", "```", json.dumps(asdict(m), indent=2), "```"]
    if cleaned:
        lines += ["", "**Cleanup this tick**"]
        lines += [f"- {r['rule']}: freed {human(r['freed_bytes'])}" + (f" (error: {r['error']})" if "error" in r else "")
                  for r in cleaned]
    if diagnosis:
        lines += ["", "**Diagnosis** (read-only Sonnet run; nothing was changed)", "", diagnosis]
    return "\n".join(lines)


def notify_new(problem: Problem, m: Metrics, cleaned: list[dict], diagnosis: str | None) -> int | None:
    """Open the problem's issue; its number, or None if that failed (retried next tick)."""
    after = " after cleanup" if problem.cleanable else ""
    out = inbox("notify", "--project", "vps-health", "--json", "--title", f"[vps] {problem.summary}{after}",
                "--body", report(m, cleaned, diagnosis))
    try:
        return int(json.loads(out)["number"]) if out else None
    except (ValueError, KeyError, TypeError):
        return None


def notify_again(entry: dict, problem: Problem, m: Metrics, cleaned: list[dict]) -> bool:
    body = f"Still open: {problem.summary} (since {entry['since']}).\n\n{report(m, cleaned)}"
    return inbox("comment", str(entry["issue"]), "--body", body) is not None


def notify_recovered(entry: dict, key: str, m: Metrics) -> bool:
    body = f"Recovered: `{key}` is back under its threshold.\n\n{report(m, [])}"
    return inbox("comment", str(entry["issue"]), "--body", body, "--close") is not None


HERE = Path(__file__).resolve().parent
DIAGNOSIS_EVERY = timedelta(hours=6)
DIAGNOSIS_MODEL = "claude-sonnet-5-5"
DIAGNOSIS_MAX_CHARS = 4000


def run_lock_held() -> bool:
    """True if the build runner holds its run lock. Never blocks, never creates the lock."""
    for state in state_dirs():
        path = state / "run.lock"
        if not path.exists():
            continue
        with path.open("a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
    return False


def usage_ok() -> bool:
    out = subprocess.run([sys.executable, str(HERE / "usage.py"), "check"], capture_output=True, text=True, timeout=180)
    return out.returncode == 0


def diagnosis_skip(m: Metrics, state: dict, force: bool = False) -> str | None:
    """Why the diagnosis can't run now, or None if it can."""
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "no CLAUDE_CODE_OAUTH_TOKEN"
    if m.disk_pct >= 95:
        return f"disk {m.disk_pct}% (a Claude run could fill it)"
    last = state.get("last_diagnosis")
    if not force and last and now() - datetime.fromisoformat(last) < DIAGNOSIS_EVERY:
        return "last diagnosis under 6 h ago"
    if run_lock_held():
        return "another Claude run holds the run lock"
    if not usage_ok():
        return "session usage over its limit"
    return None


def diagnose(found: list[Problem], m: Metrics, cleaned: list[dict], state: dict, force: bool = False) -> str | None:
    """A short read-only Sonnet run that says what uses the space or memory. None when skipped or failed."""
    why = diagnosis_skip(m, state, force)
    if why:
        log({"rule": "diagnosis-skipped", "why": why})
        return None
    state["last_diagnosis"] = now().isoformat()
    prompt = (HERE / "prompts/health-diagnose.md").read_text().format(
        problems="\n".join(f"- {p.key}: {p.summary}" for p in found) or "- none",
        metrics=json.dumps(asdict(m)),
        cleaned="\n".join(f"- {r['rule']}: freed {human(r['freed_bytes'])}" for r in cleaned) or "- none",
    )
    argv = ["timeout", "10m", "claude", "-p", prompt, "--model", DIAGNOSIS_MODEL, "--max-turns", "15",
            "--output-format", "json", "--settings", str(HERE / "settings/health-diagnose.json"),
            "--permission-mode", "dontAsk"]
    config.state_dir().mkdir(parents=True, exist_ok=True)
    try:
        out = subprocess.run(argv, cwd=config.state_dir(), capture_output=True, text=True, timeout=660)
        result = json.loads(out.stdout).get("result") if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log({"rule": "diagnosis-failed", "error": str(exc)[:300]})
        return None
    if not result:
        log({"rule": "diagnosis-failed", "error": (out.stderr or out.stdout).strip()[-300:]})
        return None
    return result[:DIAGNOSIS_MAX_CHARS]


def cmd_diagnose(force: bool) -> int:
    config.load_env()
    m = measure()
    state = load_state()
    result = diagnose(problems(m, thresholds()), m, [], state, force=force)
    save_state(state)
    print(result or "skipped (see health.log)")
    return 0 if result else 1


def tick() -> int:
    config.state_dir().mkdir(parents=True, exist_ok=True)
    lock = open(config.state_dir() / "health.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # another tick is running
    try:
        config.load_env()
        th = thresholds()
        cleaned = clean()  # routine once a day; pressure rules only while disk is over disk_clean_pct
        m = measure()
        state = load_state()  # after clean(), which records routine_at
        at = now()
        found = confirmed(problems(m, th), state, at)
        open_ = state.setdefault("open", {})

        new = [p for p in found if p.key not in open_]
        diagnosis = diagnose(new, m, cleaned, state) if new else None
        for p in found:
            entry = open_.get(p.key)
            if entry is None:
                issue = notify_new(p, m, cleaned, diagnosis)
                if issue is not None:
                    open_[p.key] = {"since": at.isoformat(), "issue": issue, "last_notified": at.isoformat()}
            elif at - datetime.fromisoformat(entry["last_notified"]) >= RENOTIFY:
                if notify_again(entry, p, m, cleaned):
                    entry["last_notified"] = at.isoformat()
        for key in [k for k in open_ if k not in {p.key for p in found}]:
            if notify_recovered(open_[key], key, m):
                del open_[key]
        save_state(state)
        return 0
    finally:
        lock.close()


def gate() -> int:
    """1 while disk is at or over disk_alert_pct (run.sh starts no Claude run then), else 0. No cleanup, no notices."""
    m = measure()
    return 1 if m.disk_pct >= thresholds()["disk_alert_pct"] else 0


def cmd_check() -> int:
    m = measure()
    found = problems(m, thresholds())
    print(json.dumps({"metrics": asdict(m), "problems": [asdict(p) for p in found]}, indent=2))
    return 1 if found else 0


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["check"]:
            return cmd_check()
        if argv[:1] == ["clean"] and set(argv[1:]) <= {"--dry-run", "--pressure"}:
            return cmd_clean(argv[1:])
        if argv == ["tick"]:
            return tick()
        if argv == ["gate"]:
            return gate()
        if argv[:1] == ["diagnose"] and set(argv[1:]) <= {"--force"}:
            return cmd_diagnose("--force" in argv)
    except HealthError as exc:
        print(exc, file=sys.stderr)
        return 2
    print("usage: health.py check | clean [--dry-run] [--pressure] | tick | gate | diagnose [--force]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
