# Plan: preprod deploys name a tag or a branch; the runner stops opening pin PRs
**Status:** ✅ 2026-10-28 (every task done; AM11 is the user's)
**Risk:** low (runner code only; goes live when the user installs a release)
**Depends on:** A29 [runner-16-pins](runner-16-pins.md) (the code this removes)
**Queue:** after strelitzia/job-edit-delete (queued by hand 2026-10-28; intake isn't on, AM10)

## Goal
User, 2026-10-28: prod is pinned in Git, preprod floats. A pin-bump PR plus an engineerfamily tag after every child merge
(11 "Pin strelitzia …" PRs in a day, v1.6.0 → v1.6.6) is too much churn for a preprod deploy. After this plan:
- **No more pin PRs.** The runner stops running `pins.py sync`/`tick`. engineerfamily's `components.toml` records what prod
  runs, and only `make prod-release` moves it (engineerfamily E15, [components-3-float](https://github.com/SiddharthEngineer/engineerfamily/blob/main/agents/plans/components-3-float.md)).
- **Every preprod deploy names a tag or a branch, never a bare sha** (user, 2026-10-28). A PR goes to preprod as its
  branch (checked against the sha the validator approved). After a merge, a repo with `[release] tags = true` goes to
  preprod at its newest `vX.Y.Z` tag, and a repo without it at its base branch.
- A child merge (Strelitzia, Wordle) puts the child's new tag on preprod directly, so preprod follows without an
  engineerfamily commit.

## Context
- `run.sh:97-122` `preprod_follow` (unchanged apart from callers); `run.sh:131-174` `pins_upkeep` and its call at `:174`:
  remove both, plus the `pins` lines in the header comment (`:6-7`).
- `preprod.sh:75-95` `with_sha` / `deploy_ref`: today `deploy_ref "pull/$pr/head" "$sha"` checks out a detached sha in
  `APP_DIR` and `{sha}` is the only placeholder in `[preprod] deploy`/`release`. `preprod.sh:155-175` `cmd_deploy`,
  `:201-221` `cmd_release`.
- `versions.py`: the "newest `vX.Y.Z` tag on origin" helper (reuse it from shell through a small `versions.py newest <repo>`
  subcommand if there isn't one; check `versions.py --help` first).
- engineerfamily's `agents/runner.toml` has `deploy = "make -C /srv/engineerfamily-preprod preprod-deploy ref={sha}"` and
  `release = "make -C /srv/engineerfamily-preprod preprod-deploy"`. Agents can't edit runner.toml, so `{sha}` must keep
  working until the user switches it to `{branch}` (manual step AM11 below). Strelitzia has `app_dir` and no `release`.
- `pins.py`, `tests/test_pins.py`, the `pins` CLI entry (`agent-runner:22,36`), `README.md:48,181,333-350`,
  `AGENTS.md:10,26`.
- Gotcha: a PR branch can move after review. Deploying "the branch" must still deploy what was approved: fetch
  `refs/heads/<branch>`, compare its tip to the approved sha, and fail the deploy (same path as a failed deploy) if they differ.

## Tasks
### Task 0: check the starting point
**Runs:** unattended
**Files:** none
**What:** `git log --oneline origin/main -- pins.py` shows A29's commits; `grep -n pins_upkeep run.sh` finds it. If either is
missing, stop and report (someone already changed this area).
**Validate:** both found.
**Status:** ✅ 2026-10-28 (no commit: checks only)

### Task 1: preprod deploys by branch or tag
**Runs:** unattended
**Files:** preprod.sh (modify), versions.py (modify, only if a `newest` subcommand is needed), tests/test_preprod.py (modify)
**What:**
- `cmd_deploy`: read `headRefName` with `headRefOid`. `deploy_ref` becomes `deploy_ref <kind> <name> <sha>` where kind is
  `branch` or `tag`: fetch `refs/heads/<name>` (or `refs/tags/<name>`), check the fetched tip equals `<sha>` (else
  `echo "preprod: $name moved since it was approved ($sha → $tip)"` and return 1), then `checkout -q --detach` that ref.
- Placeholders: `with_ref <command> <ref> <sha>` replaces `{branch}` with the branch or tag name (rename-free: the
  placeholder is `{branch}` for both, documented as "the branch, or the tag on a release") and `{sha}` with the sha, so
  engineerfamily's current `ref={sha}` keeps working until AM11.
- `cmd_release`: if the repo's `release.tags` is true and `origin/<base>` is at a `vX.Y.Z` tag, deploy that tag (newest one
  pointing at the base tip); otherwise deploy the base branch. Same for `RELEASE` commands (`{branch}` → the tag or base).
  If the base tip has no tag yet (versions sync failed), deploy the branch and log one line saying so.
- The lock line keeps the sha (column 3); add nothing else to it.
**Validate:** `uv run --with pytest pytest tests/test_preprod.py -q`: a PR deploy fetches `refs/heads/<branch>` and passes the
branch name to `{branch}`; a moved branch fails the deploy and restores the base; a release with a tagged base checks out
the tag; an untagged base checks out the branch; `{sha}` still expands.
**Status:** ✅ 2026-10-28 (124ec96)

### Task 2: remove the pin PRs
**Runs:** unattended
**Files:** run.sh (modify), pins.py (delete), tests/test_pins.py (delete), tests/test_run.py (modify), agent-runner (modify)
**What:** delete `pins_upkeep` and its call, `pins.py`, its tests and the `pins` CLI command. A child merge already calls
`preprod_follow <child> <pr>`, which (after Task 1) puts the child's new tag on preprod; nothing replaces the pin step.
Leave the `pin-bump` label on GitHub alone.
**Validate:** `uv run --with pytest pytest tests -q` passes; `grep -rn "pins.py\|pins_upkeep" --exclude-dir=agents .` → nothing.
**Status:** ✅ 2026-10-28 (b3b55ef)

### Task 3: docs and tracking
**Runs:** unattended
**Files:** README.md (modify), AGENTS.md (modify), agents/ROADMAP.md (modify)
**What:** README: drop the Pins section and CLI row; the `[preprod]` docs list `{branch}` (PR branch, or on release the tag
or base) next to `{sha}`, and say a preprod deploy is always a branch or a tag; "How a tick works" loses the pin step.
AGENTS.md: drop the `pins.py` row and `pins` from the CLI row. ROADMAP: A33 ✅, and AM11 stays for the user.
**Validate:** `scripts/validate.sh` passes.
**Status:** ✅ 2026-10-28 (this commit)

## Validation
- `scripts/validate.sh` passes; CI is green.

## Human test steps
After `agent-runner release --install` (AM11):
1. Edit engineerfamily `agents/runner.toml` on main: `deploy = "… preprod-deploy ref={branch}"`, `release = "… preprod-deploy ref=main"`.
2. On the next Strelitzia merge: no "Pin strelitzia …" PR appears in engineerfamily; `runs.log` shows preprod redeployed on
   strelitzia after the merge, and `git -C /srv/Strelitzia-preprod describe --tags` → the new tag.
3. On the next runner PR that reaches preprod: the PR's deploy log line names its branch, and
   `git -C /srv/<Repo>-preprod log -1` matches the PR head.

## Decisions
1. 2026-10-28 (user): prod pinned in Git, preprod floating; preprod deploys always require a tag or a branch.
2. Deploy by branch, but refuse if the branch moved since approval, so preprod still runs exactly what was reviewed.
3. `{sha}` stays as a placeholder so the live engineerfamily runner.toml keeps working until the user edits it.
