# Plan: say the cutover happened (smoke test, agent-runner)
**Status:** ❌ Not started
**Risk:** low (docs only; auto-merges on green CI, user 2026-10-19)
**Depends on:** the cutover (ROADMAP AM5, cron swapped 2026-10-19)

## Goal
The cutover moved cron to the installed agent-runner on 2026-10-19, but two docs still say the old Strelitzia runner is live. Fix them.
This is also design Rollout step 6's `low` smoke test for this repo: implementer PR → merged, no validator run.

## Tasks
### Task 1: cutover wording
**Runs:** unattended
**Files:** README.md (modify), AGENTS.md (modify)
**What:** README "Cron" section: replace the last line ("Until the cutover the live cron runs Strelitzia's own `scripts/agent/run.sh`.")
with "Since the cutover (2026-10-19) these two lines are the live cron; Strelitzia's old `scripts/agent/run.sh` line is commented out
until S2 deletes it." AGENTS.md: replace the sentence starting "Until the cutover (design Part "cutover")" with "Since the cutover
(2026-10-19) the live cron runs the installed release, so changes here go live only when the user installs a tag." Change nothing else.
**Validate:** `scripts/validate.sh` → all PASS. `grep -n "Until the cutover" README.md AGENTS.md` → no match.
**Status:** ✅ 2026-10-19 (docs; see PR for the validation caveat)

## Validation
- `scripts/validate.sh` passes.

## Human test steps (preprod)
None (low risk).
