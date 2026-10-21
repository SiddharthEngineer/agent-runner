# Plan: validator, preprod stage (run the Human test steps on preprod)
**Status:** ✅ Done 2026-10-19 (all 4 tasks; AM3 is the user's)
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19; nothing installed runs this until the cutover)
**Depends on:** runner-6-review

## Goal
After the manager deploys an approved `review` PR to preprod, the validator runs every one of the plan's Human test steps that it
can automate: `curl` through Cloudflare, and Playwright signed in as the dev test account. It posts a checklist comment
(passed / failed / left for you). Pass → the manager merges after green CI, puts preprod back on the base branch, and sends
the user one notice with the steps only a human can do. Fail → the PR leaves preprod and goes back to the implementer.

## Context
- Task 0 check: `awaiting-review` and `prompts/review-pr.md` exist (runner-6-review). If not, `blocked`.
- `preprod.sh` (runner-4-preprod): `deploy <repo> <pr>` sets the item to `on-preprod`; `release`; the health list; `netrc_env`.
- Design D3: the preprod stage, the "left for you" steps going to the user, merge after both stages.
- Dev test account (Strelitzia `agents/DEPLOY.md`, "Test account"): the owner on preprod. The user signs it in once in a browser,
  and agents never handle its password or 2FA. The runner's Playwright MCP is `--headless --isolated` (Strelitzia `scripts/agent/mcp.json`), so
  it starts signed out: a signed-in session needs a Playwright storage-state file that the user exports (manual step AM3).
- Preprod has nginx basic auth. Playwright MCP takes a `--config` JSON whose `browser.contextOptions` can carry `httpCredentials`
  and `storageState`. Build it per run from the netrc, in a 0600 temp file, deleted afterwards, and never logged.

## Tasks
### Task 1: preprod-check prompt and MCP config
**Runs:** unattended
**Files:** prompts/preprod-check.md (new), mcp_config.py (new), tests/test_review.py (modify)
**What:** Template with `{repo}`, `{pr}`, `{plan_path}`, `{base_url}`, `{result_file}`, `{signed_in}`. Read the plan's Human test steps;
automate each one it can with `curl` (netrc) or the Playwright MCP; mark anything needing the user's own account, prod, email,
money or a physical device as `left_for_you`; if `{signed_in}` is false, signed-in steps are `left_for_you` too. Post one PR
comment with the checklist, then write `{"verdict": "approve"|"changes"|"blocked", "passed": [], "failed": [], "left_for_you": [], "summary": ""}`.
`mcp_config.py write <out> --netrc <f> --host <h> [--storage-state <f>]` writes the Playwright MCP config (mode 0600) and prints
`signed_in=true|false`.
**Validate:** `pytest -q tests/test_review.py` → the template fills; the MCP config has `httpCredentials` from a temp netrc and
mode `0o600`; a missing storage-state file → `signed_in=false` and no `storageState` key.
**Status:** ✅ 2026-10-19

### Task 2: the preprod stage in `run.sh`
**Runs:** unattended
**Files:** run.sh (modify), tests/test_run.py (modify)
**What:** Tick order step 2 (before the review stage): an `on-preprod` item with no `preprod_verdict` → write the MCP config
(storage state `~/.config/agent-runner/preprod-storage-state.json` if present) → `claude -p` with the filled prompt,
`--mcp-config <temp>`, the validator settings plus `mcp__playwright`, Sonnet, 45 min → read
`results/<repo>/<slug>.preprod.json` → delete the temp config → set `preprod_verdict`.
**Validate:** `pytest -q tests/test_run.py`: with a stub `claude`, an `on-preprod` item gets exactly one validator run and
`preprod_verdict` is set; an item that already has one isn't re-run; the temp MCP config is gone afterwards.
**Status:** ✅ 2026-10-19

### Task 3: after the verdict
**Runs:** unattended
**Files:** run.sh (modify), buildqueue.py (modify), tests/test_run.py (modify)
**What:** `approve` + `gh pr checks` green → merge (the repo's method), `preprod.sh release`, item `done`, and one notice titled
`[<key>] merged; N steps left for you` that lists `left_for_you` (or "nothing left"). `changes` → `preprod.sh release`,
`buildqueue.py requeue <key> --note <failed steps + summary>` (counts an attempt). `blocked` → release, item `blocked`, notice.
**Validate:** tests: `approve` → merge, release and notice calls in that order; `changes` → release, then queued with the failed steps in notes.
**Status:** ✅ 2026-10-19 (committed with Task 2)

### Task 4: docs and the storage-state step
**Runs:** unattended
**Files:** README.md (modify), agents/ROADMAP.md (modify)
**What:** README: the preprod stage, what the validator can and can't test, and how to refresh the storage state. Confirm that ROADMAP
manual step AM3 matches what was built (file path, the export command) and fix it if not.
**Validate:** `scripts/validate.sh` → all PASS.
**Status:** ✅ 2026-10-19

## Validation
- `scripts/validate.sh` passes.
- `AGENT_DRY_RUN=1` with one `on-preprod` item → `would run validator (preprod) for <key> PR #<n> signed_in=<bool>`.

## Human test steps (preprod)
None (low risk). First live use: the cutover's `review` smoke plan (design Rollout step 6).

## Decisions
1. Sonnet for the preprod stage: it follows written steps; the merge judgment was already made in the review stage.
2. Without a valid storage state the validator still runs and leaves the signed-in steps to the user, instead of blocking.
3. "Left for you" steps never block a merge (design D3); they come to the user in one notice.
