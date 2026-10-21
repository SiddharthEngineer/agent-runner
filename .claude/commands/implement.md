---
description: Implement a plan task by task, validating after each (PIV steps 2–3)
argument-hint: <agents/plans/<slug>.md> [--unattended]
---

# Implement $ARGUMENTS

## 0. Orient (keep it cheap)
- Read the plan in full. Read only the files its **Context** and **Files** name, plus whatever you discover you need.
- Everything you read stays in context and is re-sent on every later turn, so read narrowly:
  - Locate with `grep -n`, then read the line range (Read with `offset`/`limit`, or `sed -n 120,180p`). Read a whole file
    only when it's under ~150 lines or you're about to rewrite most of it. Never `cat` several source files in one command.
  - Don't re-read a file you've already read or just edited; the Edit result is enough.
  - Trim command output: `pytest -q -x … 2>&1 | tail -n 40`, `git diff --stat` before a full `git diff`, `| head` on listings.
  - For a broad question ("where is X used", "what calls this function"), use the Agent tool with
    `subagent_type: "Explore"` and `model: "opus"`, and ask for file:line answers. It reads in its own context and
    returns only the summary.
- `git status && git log --oneline -10`. If commits for some tasks already exist (a previous attempt), continue from
  the first task that isn't done, and don't redo work.
- Validate with `scripts/validate.sh` (pytest comes from `uv`, so there is no venv to set up).

## 1. Each task, in order
1. Implement exactly what the task says, mirroring the patterns it cites. If the plan is wrong or ambiguous, see §3.
2. Run the task's **Validate** command, then `scripts/validate.sh`. Fix until both pass. Show the real output.
   Never weaken a test, add a skip, or delete an assert to get to green.
3. Commit (`<type>(<slug>): Task N <title>`), and set the task's **Status** to ✅ with the date and commit in the same commit.
- Skip `Runs: interactive` tasks when unattended, and list them in the PR under "Left for you".

## 2. Finish
1. `scripts/validate.sh`, plus every command in the plan's **Validation** section. All must pass.
2. Self-review: run the `/code-review` skill on the branch diff and fix anything real it finds, then re-validate.
3. Set the plan's **Status** (✅ if every task is done) and the ROADMAP row. Commit.
   If every task is ✅ (nothing left for the user), move the plan: `git mv agents/plans/<slug>.md agents/plans/done/<slug>.md`,
   add one `../` to the relative links in the moved file, fix every link to it (`grep -rn "<slug>.md" agents README.md AGENTS.md`),
   and point the ROADMAP row at `plans/done/<slug>.md`. A plan with "Left for you" steps stays put until the user finishes them.
4. Ship:
   - **Interactive:** run `/ship`.
   - **Unattended** (`--unattended`): `git push -u origin HEAD`, then `gh pr create --repo SiddharthEngineer/agent-runner --base main`
     with a body that gives a summary, the plan link, the validation output and the plan's Human test steps verbatim. Don't merge;
     the runner decides that with `agent-runner gate`. Then write the result file (§4).

## 3. When you're stuck (unattended)
You get at most 2 attempts per plan, and each one is time-boxed. Don't loop on the same failure.
- **Need a decision from the user:** write the question (context, options, your recommendation) to a temp file, then run
  `agent-runner inbox ask agent-runner/<slug> --title "<one line>" --body-file <file>`. Commit and push the work done so far
  (`git push -u origin HEAD`), write the result file with `"status": "question"`, and stop.
- **Blocked by the environment** (a broken dependency, missing access, or the same validation failure after 3 genuinely different fixes):
  push what you have, write `"status": "blocked"` with a precise summary, and stop.
- Never put secrets, personal data or job data in an issue, PR, commit or the result file.

## 4. Result file (unattended only)
Write JSON to the path given in the prompt (`$AGENT_RESULT_FILE`) as your **last** action:
`{"status": "ready" | "question" | "blocked", "pr": <number or null>, "branch": "<branch>", "summary": "<2–3 sentences>"}`
`ready` means: all runnable tasks are done, `scripts/validate.sh` passes, and the PR is open. The runner re-runs validation
itself, so a false `ready` just costs an attempt.
