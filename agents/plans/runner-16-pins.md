# Plan: the runner opens and merges "Pin <child> <tag>" PRs in a parent repo's components.toml
**Status:** ❌ Not started
**Risk:** review (new merge path in `run.sh`: PRs merged without a Claude review)
**Depends on:** engineerfamily E12 [components-1-pins](https://github.com/SiddharthEngineer/engineerfamily/blob/main/agents/plans/components-1-pins.md)
(`components.toml` and `scripts/components.py`); queue after A23 and A24 (they edit `run.sh` too)

## Goal
After a child repo's merge is tagged (Strelitzia `v1.1.3`), the parent that pins it (engineerfamily's `components.toml`)
gets a small PR moving that pin, and the runner merges it on green CI (user, 2026-10-26). Then preprod follows the parent's
new base like after any merge, so preprod runs the pinned set and the next `make prod-release` ships it. No Claude run:
plain code, every tick, stateless (catches hand merges and missed ticks). A `hold = "reason"` on a component stops bumps
for it. Claude can still move pins in an ordinary plan; the runner only ever moves a pin forward to the child's newest tag.

## Context
- `run.sh:56-106`: `versions_sync` (one notice per cause in `$STATE/versions/<repo>.notice`) and the upkeep loop over
  `~/.config/agent-runner/repos/*.env`, where the pin step goes, right after the versions loop.
- `run.sh:257-297`: `merge_pr` (watches CI up to 30 min) and `preprod_follow`. The pin step must not block a tick: read
  CI once (`gh pr checks <n> --json state` / the `ci_runs` fallback's run list without `watch`) and act on the next tick
  if it's still pending.
- `versions.py`: `Repo`, `run`, `git`, `gh` helpers and the "newest `vX.Y.Z` tag on origin" logic to reuse;
  `tests/test_versions.py` has the temp-origin + stub-`gh` pattern to copy.
- `config.py:267` `repo_env(name)` → the child's dev clone (`AGENT_REPO`); runner.toml from `origin/<base>`.
- Parent opt-in is the file: a registered repo with `components.toml` at the root of `origin/<base>`. A component's name is
  the child's registered repo name (`strelitzia`, `wordle`); an unregistered name is skipped with one log line.
- Edit the pin as text (only the `tag = "…"` line inside that component's table), so comments and order survive;
  standard library only (`tomllib` to read).

## Tasks
### Task 1: `pins.py sync`
**Runs:** unattended
**Files:** pins.py (create), tests/test_pins.py (create)
**What:** `pins.py sync [--dry-run]`: for each parent (above), for each component without `hold`: newest child tag on
origin > the pin → make sure a PR exists on branch `pin/<name>` in the parent: branch from `origin/<base>` with only the
`tag` line changed, commit `Pin <name> <tag>` (body: the child PRs since the old pin, from the child's tag messages), push
(force only to `pin/<name>`), and `gh pr create` labelled `pin-bump` (create the label if missing). An open `pin-bump` PR on
that branch at an older tag is updated in place (force-push, new title). Pin already newest → close any open `pin/<name>`
PR with a comment. `hold` → one `runs.log` line per tick change, no PR. `pins.py tick` (Task 2) does merges.
**Validate:** `uv run --with pytest pytest tests/test_pins.py -q`: new tag → PR created with exactly one changed line;
older open PR → updated; up to date → no call; `hold` → no PR; unregistered component → skipped; comments in the file kept.
**Status:** ❌

### Task 2: merge on green CI, then preprod follows
**Runs:** unattended
**Files:** pins.py (modify), run.sh (modify), tests/test_pins.py (modify)
**What:** `pins.py tick`: for each open `pin-bump` PR in a parent: refuse (comment once, notice once) if its diff touches
anything but `components.toml` or more than the `tag` lines; CI green → `gh pr merge --<parent merge method> --delete-branch`,
print `merged <parent>#<n> <name> <tag>`; red → one notice per PR (inbox, project = parent); pending → nothing. run.sh upkeep,
after the versions loop: `pins.py sync` then `pins.py tick` (both skipped in a dry run, which logs "would sync pins");
for each `merged` line, `versions_sync <parent>` and the `preprod_follow` logic for the parent (refactor so it takes the
repo name; the lock rule is unchanged). Every failure is one `runs.log` line and never stops the tick.
**Validate:** `uv run --with pytest pytest tests/test_pins.py tests/test_run.py -q`: green → merged and the parent's preprod
redeployed; red → one notice; a PR that also edits another file → not merged; a dry run calls neither.
**Status:** ❌

### Task 3: docs and tracking
**Runs:** unattended
**Files:** README.md (modify), AGENTS.md (modify), agents/ROADMAP.md (modify)
**What:** README: a "Pins (`pins.py`)" section (opt-in by file, branch `pin/<name>`, label `pin-bump`, merges on green CI with
no review, `hold`, only forward to the newest tag, preprod follows); the tick list in "How a tick works" gains the step;
the CLI table gets `pins`. AGENTS.md Map row. ROADMAP: A29 row.
**Validate:** `scripts/validate.sh` passes.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes; CI is green.

## Human test steps
After installing the release (`agent-runner release --install`) and E12 is on engineerfamily `main`:
1. `agent-runner pins sync --dry-run` → `strelitzia: v1.0.0 is newest, nothing to do` (or the PR it would open).
2. After the next Strelitzia merge is tagged: within two ticks an engineerfamily PR `Pin strelitzia v1.0.1` labelled
   `pin-bump` exists, changing one line of `components.toml`.
3. Once its CI is green, the next tick merges it; `runs.log` shows `merged engineerfamily#<n> strelitzia v1.0.1` and a
   preprod redeploy on main; `git -C /srv/Strelitzia-preprod describe --tags` → `v1.0.1`.
4. Add `hold = "testing"` to `[wordle]` on main: the next Wordle tag opens no PR, and `runs.log` says it's held.

## Decisions
1. 2026-10-26 (user): the runner opens low-risk bump PRs after child merges; they merge on green CI with no validator.
2. Opt-in by the presence of `components.toml`, so no `runner.toml` edit (agents can't make one) is needed.
3. Stateless each tick from origin's tags and open PRs, so hand merges and missed ticks catch up.
4. A bump PR whose diff is more than pin lines never auto-merges, whoever pushed to the branch.
