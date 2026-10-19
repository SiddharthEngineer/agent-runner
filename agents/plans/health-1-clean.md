# Plan: VPS health check and fixed-rule cleanup (`health.py check`, `clean`)
**Status:** ❌ Not started
**Risk:** review (deletes files and Docker data on the shared VPS)
**Depends on:** - (independent of the runner Parts; design [shared-agent-runner](shared-agent-runner.md) D6)

## Goal
`agent-runner health check` reports in a few milliseconds whether the VPS is healthy (disk, memory, swap, load), and
`agent-runner health clean` frees disk with fixed, audited rules. Each rule can only delete under its own root, and
`--dry-run` shows what each rule would free without deleting anything. Alerts, the Claude diagnosis and cron are health-2-alert.

## Context
- Measured 2026-10-19: `/` 38 GB, 71% used; 3.8 GB RAM, 1.7 GB available; 4 GB swap, 605 MB used; 2 CPUs. Docker build cache
  4.8 GB (4.6 GB reclaimable), images 8.5 GB (0.9 GB reclaimable), 2 volumes (never touched). `~/.cache/uv` 1 GB,
  `~/.cache/ms-playwright` 278 MB, `/tmp/pytest-of-dev` 232 MB, `~/backups` 308 KB, `/srv/backups` absent.
- `run.sh:243` already runs `docker builder prune -f --keep-storage "${AGENT_BUILD_CACHE_KEEP:-3GB}"` after each run; the routine
  rule is the same call, so they agree.
- Runner state today: `~/.local/state/strelitzia-agent/{logs,results,worktrees}`. After the cutover: `~/.local/state/agent-runner/`.
  Clean both (`config.state_dir()` from runner-1-config if it has merged; otherwise define the two paths here and switch later).
- `/tmp/claude-<uid>` holds live desktop sessions' scratchpads: never a cleanup target. Only top-level `/tmp` entries owned by the
  user, judged by the newest mtime anywhere inside, are eligible.
- Thresholds and rules: design D6. Backups are ordinary cleanup targets (user, 2026-10-19), but a dump under 24 h old is always kept.

## Tasks
### Task 1: measure and thresholds
**Runs:** interactive (reads the real VPS for its check)
**Files:** health.py (new), tests/test_health.py (new)
**What:** `@dataclass Metrics(disk_pct: float, disk_free_gb: float, mem_available_mb: int, swap_used_mb: int, load15_per_cpu: float, build_cache_gb: float | None)`.
`measure(root="/", meminfo=Path("/proc/meminfo")) -> Metrics` (`shutil.disk_usage`, `os.getloadavg`, `os.cpu_count`;
`docker system df --format '{{json .}}'` with a 20 s timeout, `None` if Docker is unavailable). `DEFAULTS = {"disk_clean_pct": 80,
"disk_alert_pct": 90, "mem_available_mb": 400, "swap_used_mb": 2048, "load_per_cpu": 2.0}`, merged with
`~/.config/agent-runner/health.toml` (`[thresholds]`; unknown keys are an error). `problems(m, th) -> list[Problem]`, where
`Problem(key: str, summary: str, cleanable: bool)` and `key` is one of `disk`, `disk-critical`, `memory`, `swap`, `load`.
`health.py check` prints `{"metrics": …, "problems": […]}` and exits 0 when there are no problems, else 1.
**Validate:** tests with a fake meminfo file and monkeypatched `disk_usage`/`getloadavg`: 85% disk → `disk` cleanable; 92% →
also `disk-critical`; 350 MB available → `memory` not cleanable; a bad key in a temp `health.toml` → error naming it.
`python3 health.py check` on the VPS → JSON with `disk_pct` near 71.
**Status:** ❌

### Task 2: rule engine and Docker rules
**Runs:** interactive
**Files:** health.py (modify), tests/test_health.py (modify)
**What:** `@dataclass Rule(name: str, tier: Literal["routine", "pressure"], estimate: Callable[[], int], apply: Callable[[], int])`
(bytes). `clean(dry_run, force_pressure) -> list[dict]`: routine rules run if `health.json` (in the state dir) says they last ran
more than 23 h ago. Pressure rules run in order only while `disk_pct >= disk_clean_pct` (or with `--pressure`), re-measuring
after each and stopping once below. Every action appends `{time, rule, freed_bytes, dry_run}` to `health.log` as one JSON line.
Docker rules: routine `builder prune -f --keep-storage 3GB` and `container prune -f --filter until=24h`; pressure
`builder prune -af` and `image prune -af --filter until=168h`. Never `volume`, never `system prune`. A Docker error is logged
and the next rule runs. CLI: `health.py clean [--dry-run] [--pressure]` prints one line per rule: name, estimate or freed size.
**Validate:** tests stub `subprocess.run` and assert the exact Docker argv lists (no `volume`, no `system`), that pressure stops
after the first rule that gets disk under threshold, and that a second routine run within 23 h is skipped.
**Status:** ❌

### Task 3: file rules with root guards
**Runs:** interactive
**Files:** health.py (modify), tests/test_health.py (modify)
**What:** `delete_under(root: Path, paths: Iterable[Path], dry_run) -> int`: resolves each path with `os.path.realpath`, refuses
(logs and skips) anything not strictly inside `root` and anything that is a symlink, then removes it (`shutil.rmtree` for dirs)
and returns the bytes freed. Rules: routine — runner `logs/*` older than 14 d, `results/*` older than 30 d (both state dirs);
worktrees older than 7 d whose `agent/<slug>` branch is gone from origin (`git ls-remote --exit-code`), removed with
`git worktree remove --force` from the owning repo, then `git worktree prune`; `/tmp` entries per Context older than 3 d;
`~/backups/*` and `/srv/backups/*` older than 7 d. Pressure — backups older than 24 h, `uv cache prune`, `pip cache purge`,
`~/.cache/ms-playwright`: all but the highest-numbered dir per `<browser>-<build>` prefix.
**Validate:** tests in `tmp_path`: a symlink pointing outside the root survives, and so does its target; a 2-day-old backup survives
routine but not pressure; a 2-hour-old one survives both; `/tmp/claude-*` is never selected; a worktree whose branch still exists
survives. `python3 health.py clean --dry-run --pressure` on the VPS → lists every rule with an estimate, and `docker system df`
and `df /` are unchanged afterwards.
**Status:** ❌

### Task 4: docs
**Runs:** interactive
**Files:** README.md (modify), AGENTS.md (modify)
**What:** README section "VPS health": the thresholds, both rule tiers, the never-list (design D6), `health.toml` override example,
and how to read `health.log`. AGENTS.md map row for `health.py`.
**Validate:** `scripts/validate.sh` → all PASS.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes.
- On the VPS: `python3 health.py check` exits 0 (71% disk is under 80); `python3 health.py clean --dry-run --pressure` deletes nothing.
- Interactive only, with the user's go-ahead: `python3 health.py clean` (routine) → `docker system df` build cache ≤ 3 GB, and
  `health.log` has one line per rule.

## Human test steps (preprod)
None on preprod. The user reads the `--dry-run --pressure` output once and confirms that nothing on it should be kept.

## Decisions
1. Rules are code, not config: a new deletion target needs a reviewed PR.
2. Memory and load never trigger deletions; health-2-alert reports them.
3. Worktrees count as finished only when their branch is gone from origin, so the rule needs no access to the queue.
