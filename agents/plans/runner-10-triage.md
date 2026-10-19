# Plan: Sonnet triage of blocked items (optional)
**Status:** ✅ Done 2026-10-19
**Risk:** low (no prod impact; auto-merges on green CI, user 2026-10-19)
**Depends on:** runner-8-self-host

## Goal
When an item ends `blocked` (2 failed attempts, or validator `blocked`), a short read-only Sonnet run reads its logs, result files
and PR, and decides between two outcomes. If the cause was transient or environmental and is now fixed (a flaky CI run, a
usage-limit artefact, a since-merged dependency), it re-queues the item once with a note. Otherwise it sends the user a notice
with a diagnosis and a suggested fix. It never edits code, never merges, and re-queues an item at most once.

## Context
- `run.sh` (after runner-5-run): where an item becomes `blocked` and the "blocked after N attempt(s)" notice is sent;
  `grep -n blocked run.sh`.
- `buildqueue.py requeue` (runner-6-review): re-queue with a note, counting attempts. Triage needs `--fresh-attempts` once.
- `health.py` diagnosis (health-2-alert): the pattern to mirror (read-only settings, timeout, `usage.py check` first, run-lock check).
- Design D3: "the one judgment call it delegates is triage".

## Tasks
### Task 1: triage prompt and settings
**Runs:** unattended
**Files:** prompts/triage.md (new), settings/triage.json (new), tests/test_review.py (modify)
**What:** Template with `{key}`, `{log_paths}`, `{result_json}`, `{pr_url}`, `{result_file}`. Read the last two run logs (`tail -n 300`),
the PR checks and comments; classify the cause as `transient` | `fixed-elsewhere` | `plan-problem` | `code-problem` | `environment`;
write `{"action": "requeue"|"notify", "class": "...", "note": "...", "diagnosis": "..."}`. Requeue only `transient` or
`fixed-elsewhere`. Settings: read-only (`Read`, `tail`, `grep`, `ls`, `gh pr view|checks|diff`, `gh run view --log-failed`), deny all writes.
**Validate:** `pytest -q tests/test_review.py` → the template fills; the settings deny `Edit`, `Write` and `Bash(git push:*)`.
**Status:** ✅ 2026-10-19 9dcabe5 (the settings deny all writes, so the agent replies with the JSON and run.sh saves it to the result file)

### Task 2: wire triage into `run.sh`
**Runs:** unattended
**Files:** run.sh (modify), buildqueue.py (modify), tests/test_run.py (modify)
**What:** When an item turns `blocked` and has no `triaged` flag, and the usage gate allows it: run triage (Sonnet, 15 min) and
set `triaged=1`. `requeue` → `buildqueue.py requeue <key> --note <note> --fresh-attempts`, plus a short notice ("re-queued after
triage: …"). `notify` → the blocked notice includes the diagnosis. If triage can't run (usage, lock) → the plain blocked notice as today.
**Validate:** tests with a stub `claude`: `requeue` → item queued, attempts 0, `triaged=1`; a second block of the same item → no
second triage, plain notice; `notify` → the notice body contains the diagnosis.
**Status:** ✅ 2026-10-19 (implementer `blocked after N attempt(s)`; the review validator's `blocked` or no verdict twice, which go back to `awaiting-review`; `AGENT_TRIAGE=0` turns it off. Not triaged: CI-red not-merged PRs and preprod blocks)

### Task 3: docs
**Runs:** unattended
**Files:** README.md (modify)
**What:** README: triage, its once-per-item limit, and how to turn it off (`AGENT_TRIAGE=0`; also implement the flag in Task 2's code path).
**Validate:** `scripts/validate.sh` → all PASS.
**Status:** ✅ 2026-10-19

## Validation
- `scripts/validate.sh` passes.

## Human test steps (preprod)
None (low risk).

## Decisions
1. One triage per item: a second block always goes to the user.
2. Triage only re-queues; any change to the plan or code goes through the user.
