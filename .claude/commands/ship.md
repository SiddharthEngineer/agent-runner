---
description: Ship finished work in an interactive session, through the risk gate (merge, or PR for the validator)
argument-hint: [agents/plans/<slug>.md]
---

Use this when you've finished a plan interactively. Unattended runs ship through `agent-runner run` instead.

1. Make sure the work is on a branch (not `main`): `git switch -c agent/<slug>` if needed.
2. `scripts/validate.sh`. Stop if anything fails.
3. Push the branch and open a PR to `main` on `SiddharthEngineer/agent-runner`, with a summary, the plan link and "Left for you" steps.
   Copy the plan's **Human test steps** verbatim, and include the real validation output.
4. `agent-runner gate <plan> --config agents/runner.toml --base origin/main`. Show the user the JSON verdict.
   - `low`: ask the user whether to merge now: wait for green checks (`gh pr checks <n> --watch`), then
     `gh pr merge <n> --squash --delete-branch`.
   - `review`: no preprod here. Queue it for the validator: `agent-runner queue add <slug> --repo agent-runner`, then
     `agent-runner queue set agent-runner/<slug> status=awaiting-review pr=<n>`. The next cron tick reviews it.
5. Tell the user the PR link and exactly what to test.
