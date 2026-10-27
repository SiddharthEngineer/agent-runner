# Plan: Resolver, part 1: the `awaiting-resolve` status, the prompt and the PR status block
**Status:** ✅ Done 2026-10-27
**Risk:** low (queue bookkeeping, a new prompt, status rendering; nothing routes items to it until resolver-2; runner programme PRs auto-merge on green CI, user 2026-10-19)
**Depends on:** runner-16-pins (last plan in the run.sh chain; this part doesn't edit run.sh, but part 2 does)

## Goal
When a queue item is about to become `blocked`, a **resolver** agent tries once to fix it before the user hears about it (user,
2026-10-26). It replaces today's read-only triage (which only classifies). The user gets a notice only when the resolver fails,
gives up, or can't run; a fixed item carries on through the normal path with no notice. This part adds the queue state, the prompt
and the PR status line. resolver-2 wires it into run.sh.

## Context
- Task 0: check the run.sh chain merged: `git log origin/main --oneline | grep -iE "pins|fix comments|conflict"`.
- `buildqueue.py:56-63` `RESULT_TO_STATUS`; `:202-225` `cmd_finish`; `:268-286` `cmd_requeue`; `:289-296` `cmd_oldest`; argparse at the end.
- Today's triage, to replace: `prompts/triage.md` (inputs, classes `transient | fixed-elsewhere | plan-problem | code-problem |
  environment`, JSON reply) and run.sh `notify_blocked`. Keep the classes: part 2 uses them to decide.
- `prstatus.py` `render` (`≈68-212`): the stage list and first line per status; `blocked` handling at `≈192-205`.
- Tests to mirror: `tests/test_runner.py` (`q` fixture), `tests/test_prstatus.py` (`test_review_changes_rerun` and the blocked tests).

## Tasks
### Task 1: `buildqueue.py hold`
**Runs:** unattended
**Files:** buildqueue.py (modify), tests/test_runner.py (modify)
**What:** `hold <key> --title T --body B --stage implement|merge|review|preprod`: where run.sh used to block an item.
- `resolve_tried` unset and `AGENT_RESOLVE` not `0` (also honour the old `AGENT_TRIAGE=0`) → status `awaiting-resolve`, store
  `block_title`, `block_body`, `block_stage`; print `awaiting-resolve`.
- Otherwise → status `blocked` (same fields stored); print `blocked`.
`requeue --fresh-attempts` keeps working for the resolver's `transient` case and also sets `resolve_tried=1`.
**Validate:** `uv run --with pytest pytest tests/test_runner.py -q` → passes with tests for: first hold → awaiting-resolve; hold with
`resolve_tried=1` → blocked; `AGENT_RESOLVE=0` and `AGENT_TRIAGE=0` → blocked; `oldest awaiting-resolve` finds it.
**Status:** ✅ 2026-10-27 (cafea51)

### Task 2: `prompts/resolve.md`
**Runs:** unattended
**Files:** prompts/resolve.md (new)
**What:** a `str.format` prompt (doubled braces for literal JSON) with `{key} {plan} {pr_url} {stage} {block_title} {block_body}
{result_json} {log_paths} {ci_log} {validate_full} {result_file}`. It says:
1. You run once, in the PR's worktree, with the implementer's permissions. The runner merges and deploys; you never do.
2. Find the cause: read the logs (`tail -n 300`, `grep -n`), the PR (`gh pr view --comments`, `gh pr checks`) and the failed CI log at `{ci_log}`.
3. Classify (the triage classes). `environment` and `plan-problem` → don't change code; `give-up` with a diagnosis for the user.
   `transient` → `requeue` with a note. `code-problem`, `fixed-elsewhere`, a red CI or a validator finding → fix it.
4. A fix stays inside the plan's goal and its **Files:** list where possible; never weaken or delete tests, never edit the plan's
   tasks, `agents/settings/*`, `.github/*` or `runner.toml`. Run `{validate_full}`; commit with a message starting `Resolver:`;
   `git push origin HEAD`; one `gh pr comment` saying what you changed and why.
5. Write `{result_file}`: `{{"action": "fixed|requeue|give-up", "class": "…", "summary": "<one line>", "diagnosis": "<under 15 lines:
   what failed, why, what you did or what the user should do>", "pr": <n or null>}}`.
6. Never quote secrets, tokens, environment variables or personal data.
**Validate:** `python3 -c 'import string,sys; print(sorted({f for _,f,_,_ in string.Formatter().parse(open("prompts/resolve.md").read()) if f}))'`
→ exactly the 11 fields above.
**Status:** ✅ 2026-10-27 (fa80a58)

### Task 3: PR status block
**Runs:** unattended
**Files:** prstatus.py (modify), tests/test_prstatus.py (modify)
**What:** `awaiting-resolve` renders a first line `🔁 Resolver is trying one fix: <block_title>` and marks the stage it stopped at
(`block_stage`) 🔁. A `blocked` item with `resolve_tried=1` adds "the resolver tried once" to the blocked line.
**Validate:** `uv run --with pytest pytest tests/test_prstatus.py -q` → passes with one test per case.
**Status:** ✅ 2026-10-27 (7cf310d)

## Validation
- `scripts/validate.sh` passes.

## Human test steps
None (low risk). Nothing sets `awaiting-resolve` until resolver-2 is installed.

## Decisions
1. One resolver replaces triage: a single run both diagnoses and fixes, so the user gets one message, only on failure (user, 2026-10-26).
2. It uses the repo's own `agents/settings/implement.json`: the same powers the implementer had in that repo, so nothing new to
   allow in app repos. The runner fetches the failed CI log into a file, because implement.json doesn't allow `gh run view`.
3. Once per item (`resolve_tried`): a second block always goes straight to the user.
