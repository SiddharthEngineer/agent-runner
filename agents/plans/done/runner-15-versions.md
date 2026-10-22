# Plan: the runner tags every merge and keeps a GitHub pre-release per minor version
**Status:** ✅ Done 2026-10-22
**Risk:** low (no prod impact; runner programme PRs auto-merge on green CI, user 2026-10-19)
**Depends on:** A20 [runner-14-pr-checklist](runner-14-pr-checklist.md) (`prstatus.py`); goes live only with AM8

## Goal
GitHub Releases show what is on prod and preprod (user, 2026-10-22). For a repo whose `runner.toml` has `[release]`
(engineerfamily, from E11):
1. **Every PR merged to the base gets the next patch tag.** Newest tag `v1.4.3` → `v1.4.4` on that PR's merge commit.
2. **One pre-release per minor version**, named `v1.4`, always on the newest patch tag, its notes listing every patch
   newest first: `- v1.4.4 · #71 Wordle: nginx /wordle/ (2026-10-23)`. Each merge updates it.
3. **Prod deploy promotes it** (`versions promote`, run by the user's `make prod-release`): `v1.4` becomes a full,
   Latest release on its last tag with "Deployed to prod <date>" added. The next merge then starts `v1.5.0` and a `v1.5`
   pre-release. Major bumps stay by hand (`versions promote … --next-major`).

## Context
- `config.py`: `_parse` reads tables with `_Reader.table/get`; `RunnerConfig` is frozen dataclasses. Add optional
  `[release]` → `Release(tags: bool)`, `None` when absent.
- `run.sh`: `preprod_follow` (≈239) runs after every runner merge and in `closed_by_hand`; it then redeploys preprod on the
  base. The tag has to exist before that redeploy so preprod shows it (E11 reads the tag on deploy).
- `ci.sh`: `latest_tag`, `version_gt` (reuse the version compare). `release.sh` is agent-runner's own release flow: unchanged.
- `prstatus.py` (A20): `render` stage 5.
- Merges can also happen outside the runner (by hand in the GitHub app), so a sync each tick catches those too.

## Tasks
### Task 0: check access
**Runs:** unattended
**Files:** -
**What:** A20 is merged (`git log origin/main -- prstatus.py`). `gh api repos/SiddharthEngineer/engineerfamily/rulesets`
shows no rule blocking tag pushes or release creation by the runner's token. If one does, stop with `question`.
**Validate:** both checks noted in the PR description.
**Status:** ✅ 2026-10-22: A20 is a7e63ce on origin/main; `gh ruleset list -R SiddharthEngineer/engineerfamily` lists none

### Task 1: `versions.py` sync and promote
**Runs:** unattended
**Files:** versions.py (create), config.py (modify), tests/test_versions.py (create)
**What:** `versions.py sync <repo>`: fetch tags; PRs merged to the base whose merge commit isn't in the newest `vX.Y.Z`
tag (`gh pr list --state merged --base <base> --json number,title,mergeCommit,mergedAt`, `git merge-base --is-ancestor`).
None → exit 0. Else tag the newest such merge commit with the next version (one tag per sync, listing every PR since the
last tag; normally one, since the runner syncs right after each merge), push it, and create or edit the pre-release
(`gh release create/edit`, `--prerelease`, `--tag` moved to the new tag, notes regenerated from the minor's tags and their
PRs). Next version: release `vX.Y` exists and is full → `vX.(Y+1).0`, new pre-release; else `vX.Y.(Z+1)`. If the
newest tag's minor has no release at all, push nothing and print "no release for vX.Y yet: run
`agent-runner versions promote <repo> <tag>` for what prod runs" (exit 3). `versions.py promote <repo> [<tag>]`
(default: newest tag): create or edit release `vX.Y` on `<tag>` as full and Latest, notes plus "Deployed to prod <date> at
<tag>". `--next-major` makes the next sync start `v(X+1).0.0` (a `next_major` marker in the release notes). `versions.py
status <repo>`: newest tag, open pre-release, newest full release. `--dry-run` everywhere. Tests use a temp git repo and a
stub `gh` (record calls): first patch, second patch updates the same pre-release, after promote starts the next minor,
two PRs in one sync → one tag listing both, untagged minor → exit 3, direct non-PR commits don't tag; `config.load` with and without `[release]` (in this file too).
**Validate:** `uv run --with pytest pytest -q tests/test_versions.py tests/test_config.py` passes (existing config tests unchanged).
**Status:** ✅ 2026-10-22 (this commit)

### Task 2: the runner calls it; the PR block shows the tag
**Runs:** unattended
**Files:** run.sh (modify), agent-runner (modify), prstatus.py (modify), tests/test_run.py (modify)
**What:** CLI: `agent-runner versions sync|promote|status`. `run.sh`: when the repo has `[release]`, `preprod_follow`
runs `versions.py sync "$repo"` before the preprod redeploy, and every tick runs it for each such repo after the inbox
poll. A failure (or exit 3) sends one notice per cause (dedupe like the other `[vps]` notices) and never blocks the tick
or the redeploy. `prstatus.py` stage 5 adds "released in v1.4.4 (pre-release v1.4)" from the tag containing the merge commit (its render test goes in `tests/test_run.py`'s prstatus calls or a new case there).
**Validate:** `scripts/validate.sh` passes.
**Status:** ✅ 2026-10-22 (this commit)

### Task 3: docs and tracking
**Runs:** unattended
**Files:** README.md (modify), AGENTS.md (modify), agents/ROADMAP.md (modify), agents/plans/runner-15-versions.md (modify)
**What:** README "Versions and releases": `[release]`, the patch/minor rules, promote, the exit-3 bootstrap. AGENTS.md:
map row for `versions.py`; the rule "never push tags" becomes "agents never push tags or edit releases; only the
runner's `versions sync` does, for repos with `[release]`". ROADMAP: A21 ✅ with the PR.
**Validate:** `scripts/validate.sh` passes.
**Status:** ✅ 2026-10-22 (this commit)

## Validation
- `scripts/validate.sh` passes; CI is green.

## Decisions
1. 2026-10-22 (user): the runner tags (no GitHub Action, no workflow file); a pre-release per minor, promoted on prod deploy.
2. Tags are pushed by deterministic runner code, never by a Claude session. Promote is the user's (through `make prod-release`).
3. Sync never guesses a starting point: a minor with no release waits for the user's promote (engineerfamily: AM9).
