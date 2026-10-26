# Plan: Queue intake from GitHub, part 1: `[intake]` config and `intake.py scan`
**Status:** ✅ Done 2026-10-26
**Risk:** low (new script and an optional config table; nothing calls it until intake-2; runner programme PRs auto-merge on green CI, user 2026-10-19)

## Goal
A plan pushed to a repo's base branch from anywhere (a Claude cloud session, the GitHub app, a laptop) gets queued on the
VPS without anyone running `agent-runner queue add`. A plan opts in with a header line
`**Queue:** yes [at 2026-10-27T02:00Z] [after <repo>/<slug>]`. A repo opts in with `[intake] enabled = true` in its
`agents/runner.toml` (off by default, so in an open-source install, push access alone never starts Claude runs).
This part adds the config table and `intake.py scan`. intake-2 wires it into each tick and the docs.

What lives where (the design this follows): **what to build** is in the app repo (the plan file on `origin/<base>`),
**how the build is going** stays machine-local (`~/.local/state/agent-runner/queue.json`), and **which repos to watch** is
the user's `~/.config/agent-runner/repos/*.env`. Nothing machine- or user-specific goes into this repo.

## Context
- `config.py:87-98`: `Release` dataclass and `RunnerConfig`; add `Intake(enabled: bool = False)` and `intake: Intake | None` the same way.
- `config.py:190-196`: how `_parse` reads the optional `[release]` table (`r.table(..., required=False)`, `r.get(..., bool, default=…)`); mirror it for `[intake]`.
- `config.py:233-270`: `config_dir()`, `state_dir()`, `repo_env(name)` (the repo's `AGENT_REPO` dev clone).
- `buildqueue.py:74-158`: `load()`, `save()`, `parse_when()`, `cmd_add()` (item shape). Intake writes items with the same
  shape plus `"source": "intake"` and `"plan_blob": <git blob sha>`.
- `buildqueue.py:172-188`: `cmd_next` treats an `after` key that isn't in the queue as never done, so the item would wait forever.
- `buildqueue.py:299-304`: `cmd_cancel` (status `cancelled`).
- `gate.py:78-137`: `declared_risk`, `planned_files`, `split_reasons`, `evaluate_plan(Path, rules)`. Write the plan blob to a
  temp file and call `evaluate_plan` with `rules_from_config` rules.
- `inbox.py:105-114`: `cmd_notify`; call it as a subprocess (`inbox.py notify --slug <key> --project <repo> --title … --body …`)
  only when `AGENT_INBOX_REPO` is set, as run.sh's `notify()` does (`run.sh:108-116`).
- `tests/test_versions.py:73-158`: the `world` fixture (temp bare origin + dev clone + `repos/<name>.env` in a temp `HOME`); mirror it.
- Gotchas: `**Queue:**` counts only at the start of a line in the plan header (before the first `## `). The design doc has a
  bullet `- **Queue:** one queue.json…`, which must not match. Plans in `<plans>/done/` and plans whose `**Status:**` starts
  with ✅ are never queued. Read plans with `git ls-tree`/`git show origin/<base>:…`, never from the working tree or a PR branch.

## Tasks
### Task 1: `[intake]` table in runner.toml
**Runs:** unattended
**Files:** config.py (modify), tests/test_config.py (modify)
**What:** `@dataclass(frozen=True) class Intake: enabled: bool = False`; `RunnerConfig.intake: Intake | None = None`;
`_parse` reads `[intake] enabled` (bool, default `False`; any other key is a problem, like other tables). `config get … intake.enabled` works.
**Validate:** `uv run --with pytest pytest tests/test_config.py -q` → passes, with new tests: no table → `intake is None`;
`enabled = true` → `Intake(True)`; `enabled = "yes"` → ConfigError `intake.enabled must be bool`.
**Status:** ✅ 2026-10-26

### Task 2: `intake.py scan`
**Runs:** unattended
**Files:** intake.py (new), tests/test_intake.py (new)
**What:** `python3 intake.py scan [--repo NAME] [--dry-run]`. For each `repos/*.env` (or just `--repo`):
1. `git -C $AGENT_REPO fetch -q origin`; load `agents/runner.toml` from `origin/<base>` (skip quietly when `intake` is missing or off,
   or the config fails to load: run.sh already reports that).
2. List `origin/<base>:<plans>/*.md` (top level only). For each, parse the header:
   `parse_queue_line(text) -> QueueLine | None` with `@dataclass class QueueLine: queue: bool; at: str | None; after: str | None`.
   Grammar: `**Queue:** yes|no` then optional `at <ISO time with Z or offset>` and `after <repo>/<slug>|<slug>` (bare slug = same repo).
3. Key `<repo>/<slug>`. **Intake once, local wins:** if the key is already in the queue, the only thing intake does is
   `**Queue:** no` on an item that is `queued`, `source=intake`, 0 attempts → `cancelled`. Anything else is left alone.
4. A new `yes` key is checked: status not ✅; a `**Risk:**` line; at least one `**Runs:** unattended` task not ✅; `gate.evaluate_plan`
   `split` empty; `at` has a timezone; `after` is in the queue or is queued in this same scan (process `after`-free plans first).
   Pass → add the item (`source=intake`, `plan_blob`, `notes: ["queued by intake from <sha>"]`) and print `intake: queued <key> …`.
   Fail → print `intake: refused <key>: <reasons>` and send one notice `[<key>] not queued: <reasons>`; record
   `{key: blob}` in `$STATE/intake.json` so the same blob is never re-noticed. A new blob (the plan was fixed and pushed) is checked again.
5. `--dry-run` prints the same lines and writes nothing (no queue, no `intake.json`, no notice).
Exit 0 unless the queue file is unreadable. A failed `git fetch` for one repo prints one line and goes on to the next.
**Validate:** `uv run --with pytest pytest tests/test_intake.py -q` → passes, covering: intake off → nothing; `yes` → queued with
`source=intake`; second scan → no duplicate; `no` before start → cancelled; `no` after start → untouched; done/ and ✅ plans skipped;
bullet `- **Queue:**` ignored; oversize plan → refused, one notice, none on the next scan, re-checked after a new commit;
naive `at` → refused; `after` to an unknown key → refused; two plans pushed together with `after` → both queued; `--dry-run` writes nothing.
**Status:** ✅ 2026-10-26

## Validation
- `scripts/validate.sh` passes.
- In a temp `HOME`/`AGENT_RUNNER_STATE` with the test fixture repos: `python3 intake.py scan --dry-run` prints one
  `intake: queued …` line per opted-in plan and `cat $AGENT_RUNNER_STATE/queue.json` is unchanged.

## Human test steps
None (low risk). Nothing calls `intake.py` until intake-2 is installed.

## Decisions
1. The plan file on the base branch is the only queue signal (user, 2026-10-26): one file, works from any session that can push.
2. Intake once, local wins (user): after intake the CLI owns the item; `**Queue:** no` only cancels an item that hasn't started.
3. Opt-in per repo with `[intake] enabled` (user): open-source installs don't turn push access into Claude runs.
4. Bad plans are refused with one notice per plan version (user), so cloud sessions that can't run `gate --plan` still get the size check.
5. `at` must carry a timezone: a cloud session doesn't know the VPS's local time.
6. Only `origin/<base>` is read, so forks and PR branches can never queue anything.
