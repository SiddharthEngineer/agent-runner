# Plan: ROADMAP records the cutover (smoke test, low, agent-runner)
**Status:** ✅ 2026-10-19
**Risk:** low (docs only; auto-merges on green CI, user 2026-10-19)
**Depends on:** the cutover (done 2026-10-19, runner v0.1.4 installed)

## Goal
Record in `agents/ROADMAP.md` what the 2026-10-19 cutover did. This is also the last `low` smoke test for this repo: now that CI
exists here, the implementer PR should merge on green CI with no validator run and no manual merge.

## Tasks
### Task 1: ROADMAP status
**Runs:** unattended
**Files:** agents/ROADMAP.md (modify)
**What:** Edit only these rows and the "Run of 2026-10-19" line:
- A2 → `✅` (CI added by the user, AM1a).
- The "Cutover (AM3-AM5)" row → `✅ 2026-10-19 (AM3 still open: no signed-in preprod checks until it's done)`.
- Manual steps: AM1, AM1a, AM2, AM4 and AM5 → `☑`, each with "Done 2026-10-19." added to the step text. AM1: note that `v0.1.0` failed its
  own validation (fixed in #49) and the installed tags went v0.1.1 → v0.1.4. AM2: the health cron line is in. AM5: smoke tests passed in
  all three repos (engineerfamily #45 and Strelitzia #84 merged by the runner; agent-runner by this plan), and the review path ran to
  preprod on engineerfamily #49 and #<this plan's sibling review PR, leave as "the second review smoke">. AM3 and AM6 stay `☐`.
- Replace the "Run of 2026-10-19" line with: "**Cutover 2026-10-19:** cron runs `agent-runner run` (v0.1.4). Fixes found by the smoke
  tests: #49, #51, #55, #56, #57, #61. Next: AM3 (signed-in preprod checks), then AM6 and S2 from 2026-10-26."
**Validate:** `scripts/validate.sh` → all PASS. `grep -c '☐' agents/ROADMAP.md` → 2 (AM3, AM6).
**Status:** ❌

## Validation
- `scripts/validate.sh` passes.

## Human test steps (preprod)
None (low risk).
