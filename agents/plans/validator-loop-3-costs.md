# Plan: Validator loop, part 3: cost per run in runs.log, text-first preprod checks
**Status:** ❌ Not started
**Risk:** low (logging and a prompt; goes live only on install; runner programme PRs auto-merge on green CI, user 2026-10-19)
**Depends on:** validator-loop-2-rereview

## Goal
Usage is the runner's tightest limit, but nobody can see what each run costs without parsing logs by hand. After this plan every
Claude run (implementer, review, re-review, preprod, triage) adds one `runs.log` line with its role, model, turns, tokens and
API-equivalent cost, read from the `--output-format json` result. `agent-runner usage report [--days 7]` sums them by role and repo.
The preprod validator reads pages as text first (Playwright accessibility snapshots) and takes a screenshot only for a step about
how something looks (layout, colour, an image), so screenshots don't fill its context.

## Context
- Task 0: check part 2 merged: `ls prompts/rereview-pr.md`.
- The result line of a run log: `{"type": "result", "num_turns", "total_cost_usd", "usage": {"input_tokens",
  "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"}, …}`. Measured 2026-10-26: preprod (Sonnet) $0.06–0.19,
  review (Opus) $1.00–1.50, implementer $0.20–2.80.
- `run.sh`: claude calls in `notify_blocked` (≈213, triage), `preprod_stage` (≈378), `review_stage` (≈465), `run_claude` (≈560);
  each writes to its own log file. `say` writes a `runs.log` line.
- `usage.py:1-91`: the session-usage gate; add the `report` subcommand here.
- `prompts/preprod-check.md` (26 lines), step 2 "Pages and clicks". `mcp_config.py:20` `PLAYWRIGHT_MCP` args (screenshots stay
  allowed: `--image-responses` is not set).
- `tests/test_run.py` stubs `claude`; make the stub print a result line so the new log line can be asserted.

## Tasks
### Task 1: one cost line per run, and a report
**Runs:** unattended
**Files:** usage.py (modify), run.sh (modify), tests/test_run.py (modify), tests/test_usage.py (new)
**What:** `usage.py cost <log> --role R --key K --model M` prints `cost <key> <role> <model> turns=N in=… cache_read=… cache_write=…
out=… usd=0.00` from the last result line in the log (prints nothing and exits 0 if none). run.sh calls it after each of the 4 claude
calls and appends the line to `runs.log` with a timestamp. `usage.py report [--days N]` sums `cost` lines from `runs.log` by role
and by repo: runs, turns, USD. Note in the help text that USD is the API-equivalent figure, not a subscription charge.
**Validate:** `uv run --with pytest pytest tests/test_usage.py tests/test_run.py -q` passes; a run test asserts one `cost … review`
line; a report test sums two fixture lines.
**Status:** ✅ 2026-10-27

### Task 2: preprod validator reads text first
**Runs:** unattended
**Files:** prompts/preprod-check.md (modify), tests/test_review.py (modify)
**What:** step 2: prefer `curl` for anything an HTTP response proves. In the browser, use `browser_snapshot` (the accessibility tree,
text) to read pages and find elements. Take `browser_take_screenshot` only when a step is about appearance, at most once per step,
and never full-page unless the step needs it. Keep every placeholder.
**Validate:** `uv run --with pytest pytest tests/test_review.py -q` passes; assert the prompt mentions `browser_snapshot`.
**Status:** ❌

### Task 3: docs and tracking
**Runs:** unattended
**Files:** README.md (modify), agents/ROADMAP.md (modify)
**What:** README: the `cost` line in "How a tick works", `agent-runner usage report` in the CLI table, the text-first rule in
"Validator, preprod stage". ROADMAP: A26 ✅ with the PR.
**Validate:** `scripts/validate.sh` passes.
**Status:** ❌

## Validation
- `scripts/validate.sh` passes; CI is green.
- `python3 usage.py cost <any existing ~/.local/state/agent-runner/logs/*.review.log> --role review --key x --model opus` prints one line.

## Human test steps
After `agent-runner release --install` and a day of runs: `agent-runner usage report --days 1` lists runs by role with USD; the newest
`*.preprod.log` run used fewer turns or tokens than before, or the same if its steps were visual.

## Decisions
1. 2026-10-26: the preprod validator stays Sonnet. It's already the cheapest stage, and Haiku is too weak to judge a page.
2. Screenshots stay possible (no `--image-responses omit`): visual steps need them; the prompt limits when they're used.
3. Cost lines go in `runs.log` (already the one-line event log), not a new file.
