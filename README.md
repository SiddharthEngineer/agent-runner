# agent-runner

The unattended build runner and VPS upkeep for SiddharthEngineer's repos (Strelitzia, engineerfamily, and this one). A
deterministic manager (`run.sh`, bash and Python, no Claude on a normal tick) picks queued plans, starts a Claude implementer in a
worktree, gates the PR, and merges it or sends it to preprod. Prod is never deployed by the runner. This repo's issues are also
the agent inbox. Design: [agents/plans/shared-agent-runner.md](agents/plans/shared-agent-runner.md).

## Install and rollback (you)
One copy is installed on the VPS, and cron and every repo call it, so runner changes go live only when you install a tag:
```bash
agent-runner release --dry-run          # what the next release would tag, and what it ships
agent-runner release --install          # tag origin/main as the next patch version, push it, install it
agent-runner release v0.3.0             # a given version instead (must be newer than the newest tag); no install
agent-runner install                    # install the newest tag (says so and stops if it's already installed)
agent-runner install v0.2.2             # install any existing tag; an older one is a rollback and says so
agent-runner install --full v0.2.3      # same, but always run the full test suite
agent-runner version                    # the installed tag
~/src/agent-runner/install.sh --list    # installed releases; * is active
```
`release` works in `~/src/agent-runner` (`AGENT_RUNNER_SRC` to change it): it fetches, then tags `origin/main` without
touching the working tree. It refuses a version that isn't `vX.Y.Z` or isn't newer than the newest tag, a commit that
already has a tag, and a commit whose CI isn't green yet (wait for the push run on `main` after a merge).
`agent-runner install` runs the installed copy's `install.sh`. If the installed CLI itself is broken, or for the very
first install, run the clone's copy instead: `~/src/agent-runner/install.sh [<tag>]` (same arguments).

`install.sh` keeps a bare mirror at `~/.local/share/agent-runner/mirror.git`, one detached worktree per release under
`releases/<tag>`, and `current -> releases/<tag>` (switched atomically, only after the release's own `scripts/validate.sh`
passes). `~/.local/bin/agent-runner` points at `current/agent-runner`. The newest 3 releases and the active one are kept.
If CI's run on the tag's exact commit succeeded (`gh run list`), install runs only `validate.sh --quick` (`bash -n`,
`py_compile`) and takes seconds; with no `gh`, no run, a run still going or failed, or any `gh` error, it runs the full suite
and says why. `--full` always runs the full suite.
Agents never push tags or run `install.sh`.

## CLI
`agent-runner <command> [args…]`; the arguments pass straight through to the script.

| Command | Script | What |
|---|---|---|
| `queue` | `buildqueue.py` | Queue state, attempts, pause |
| `gate` | `gate.py` | Risk gate: the plan's `**Risk:**` line plus diff checks decide auto-merge vs review; `--plan` checks a plan's size budget; `--config` takes a repo's `runner.toml` |
| `inbox` | `inbox.py` | Questions and notices as issues in this repo, plus an ntfy push; `sweep` closes notices of finished items |
| `intake` | `intake.py` | `scan [--repo N] [--dry-run]`: queue plans pushed with `**Queue:** yes` (see Queue from anywhere) |
| `usage` | `usage.py` | Claude usage gate and status |
| `config` | `config.py` | `check`/`get` a repo's `agents/runner.toml` (refuses prod commands) |
| `health` | `health.py` | VPS health `check` and fixed-rule `clean` |
| `preprod` | `preprod.sh` | Single-occupant preprod lock: deploy a PR, health-check it, release it |
| `versions` | `versions.py` | Tags and GitHub releases for repos with `[release]`: `sync`, `promote`, `status` (see Versions and releases) |
| `run` | `run.sh` | The cron entry (manager) |
| `check-commands` | `commands_diff.py` | How the repos' `.claude/commands` differ once repo names, base branches and validate commands are swapped for placeholders |
| `version` | | The installed tag |

## Config and state
- `~/.config/agent-runner/` (yours; agents and the runner only read it): `env` for shared secrets and settings (Claude token,
  `AGENT_INBOX_REPO`, `NTFY_TOPIC`), `repos/<name>.env` for machine-local per-repo values (`AGENT_REPO=<dev clone>`,
  `PREPROD_NETRC=…`), and `health.toml` for health threshold overrides. The old `~/.config/strelitzia-agent/env` is not read.
- Each repo describes itself in its own `agents/runner.toml` (base branch, merge method, validate commands, risk patterns,
  preprod commands), read from `origin/<base>`. Check one with `agent-runner config check agents/runner.toml`.
- `~/.local/state/agent-runner/` (or `$AGENT_RUNNER_STATE`): `queue.json`, locks, `runs.log`, `logs/`, `results/`,
  `worktrees/<repo>/<slug>`, and `health.json`/`health.log`.

## Cron (added at the cutover)
Nothing here edits the crontab. The lines you add (ROADMAP manual steps AM2 and AM5):
```
*/15 * * * * $HOME/.local/bin/agent-runner health tick
*/5 * * * * $HOME/.local/bin/agent-runner run
```
Since the cutover (2026-10-19) these two lines are the live cron; Strelitzia's old `scripts/agent/run.sh` line is commented out
until S2 deletes it.

## Limits (so it can't run away)
- Claude starts only if an item is due, the queue isn't paused, and the item has used fewer than 2 attempts (`AGENT_MAX_ATTEMPTS`).
  There's no daily run cap by default (`AGENT_MAX_RUNS_PER_DAY=0`; set a number to bring one back).
- Fix rounds: a validator's `changes` (review or preprod stage) starts a **fix round**, not an attempt: up to 3 per item
  (`AGENT_MAX_FIX_ROUNDS`), shared by both stages. Attempts count only runs that crash, time out, write no result or fail
  validation (a failed fix run too).
- Usage gate: before each run, `usage.py` sends one tiny Haiku prompt and reads both usage windows (5-hour and 7-day) from
  its `rate_limit_event`. Four rules, each set in `~/.config/agent-runner/env` (percents; a cap of `0` turns its rule off):

  | Rule | Setting (default) | Refuses when | Pauses until |
  |---|---|---|---|
  | Session cap | `AGENT_MAX_SESSION_USAGE` (70; `0` turns the whole gate off) | 5-hour use ≥ cap | the 5-hour reset |
  | Daytime reserve | `AGENT_DAY_HOURS` (empty = off, e.g. `08-22`), `AGENT_DAY_SESSION_USAGE` (40) | inside day hours (VPS local time; `22-06` wraps past midnight) and 5-hour use ≥ day cap | the earlier of the 5-hour reset and the end of day hours |
  | Weekly cap | `AGENT_MAX_WEEKLY_USAGE` (90) | 7-day use ≥ cap | the 7-day reset |
  | Weekly pacing | `AGENT_WEEKLY_PACE` (`even`; or `off`), `AGENT_PACE_SLACK` (10) | 7-day use > cap × (fraction of the week gone) + slack | the time that allowance catches up with use (never after the 7-day reset) |

  ```
  AGENT_MAX_SESSION_USAGE=70
  AGENT_DAY_HOURS=08-22
  AGENT_DAY_SESSION_USAGE=40
  AGENT_MAX_WEEKLY_USAGE=90
  AGENT_WEEKLY_PACE=even
  AGENT_PACE_SLACK=10
  ```
  When a rule refuses, the run doesn't start and the queue pauses for the longest pause among the refusing rules; the log
  line names them. A probe with no 7-day window skips the weekly rules; if the usage can't be read, or a setting is bad, that
  tick is skipped. `agent-runner usage status` shows both windows, every rule's verdict and which one holds the queue,
  with the same env file cron reads. The gate only gates starts: a run already going finishes even if it pushes usage past a limit.
- Merging: `low` plans that pass `gate.py` merge after green CI; the rest get a validator review, then preprod checks
  if the repo has `[preprod]`. A `low` PR whose CI is red or times out, whose repo reports no CI checks at all, or whose
  merge fails stays open, labelled `ci-failed`; the item is `blocked` and you get a notice with the PR link. Nothing
  merges without checks. A PR that conflicts with its base is never reported as "no CI checks" (GitHub runs no CI on it):
  the notice and PR comment say "conflicts with main in <files>" (see Conflicts).
- Conflicts: before the gate, and before every CI wait, the runner asks GitHub whether the PR conflicts with its base
  (`gh pr view --json mergeable`; `UNKNOWN` is polled for a minute, then treated as not conflicting). If it does, the runner
  merges `origin/<base>` into the PR branch (a merge, never a rebase or a forced push); a clean merge is pushed and CI runs on the
  new head. Real conflicts send the item back to `queued` for a **conflict round** (`requeue --conflict`, a "Conflict" queue
  note with the files): the next run starts `git merge origin/<base>` in the item's worktree and Claude gets a short prompt to
  resolve just those files, validate, commit and push (no `/implement`); if the base now merges cleanly, it's pushed with no
  Claude run. The PR then goes through the gate (and review) again. A conflict round uses no attempt, but an item gets at most
  2 (`AGENT_MAX_CONFLICT_ROUNDS`); the next conflict blocks it with a notice. No `ci-failed` label for a conflict, and a PR
  sent back from preprod releases the preprod lock.
- CI fallback: green CI is `gh pr checks --watch` first (it also sees non-Actions checks). When the token can't read the
  check rollup ("Resource not accessible", as on agent-runner), the runner watches the Actions runs of the PR head instead
  (`gh run list --commit <sha>`, then `gh run watch --exit-status` each): all green merges, one red is "CI is red", and no
  run within 30 min (`AGENT_CI_WAIT`, seconds) is "no CI checks".
- Preprod follows each merge: after any merge (low, or after the validator), a repo with `[preprod]` gets
  `preprod.sh release <repo>`, so preprod runs the new base, unless another PR holds the lock (its release puts the base back
  later). One `runs.log` line either way; a failed redeploy sends a notice and leaves the item `done`.
- Failed preprod deploy: when a PR's deploy or its health checks fail, preprod goes back on the base and the item is
  `blocked` (`deploy_failed` says why), with one notice. Tick doesn't retry it; re-queue with
  `agent-runner queue set <key> status=awaiting-preprod`.
- Hand merges: before the review or preprod stage acts on an item, the runner reads its PR's state. Merged by hand → `done`
  ("merged by hand"), preprod put on the base; closed → `cancelled` (preprod released if it held it). No validator run.
- After each run the Docker build cache is pruned down to `AGENT_BUILD_CACHE_KEEP` (default `3GB`), so a long chain can't fill the disk.
- Each run is killed after `AGENT_MAX_RUNTIME` (3h). A `flock` allows one Claude run at a time, whatever the repo or role.
- Context cap: runs auto-compact at 200k tokens (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`; override with `AGENT_COMPACT_WINDOW`).
- A usage-limit exit pauses the queue for as long as the usage gate says (a weekly-limit stop waits for the weekly reset;
  5h if the gate allows a start or can't tell), gives the attempt back, and the next run resumes that session (`resume=limit`).
- A run that ends without a result file, or whose `ready` fails the repo's full validation, counts as a failed attempt. After 2
  failures the item is `blocked` and you get a notice.
- **Triage** (once per item): when an item turns `blocked` (2 failed attempts, the review validator's `blocked` or no verdict
  twice, or fix rounds used up or stuck), one read-only
  Sonnet run (15 min at most, [prompts/triage.md](prompts/triage.md), [settings/triage.json](settings/triage.json): reading,
  `tail`/`grep`/`ls`, `gh pr view/checks/diff`, `gh run view`; no writes) reads its last two logs, last result and PR, and
  classifies the cause: `transient` | `fixed-elsewhere` | `plan-problem` | `code-problem` | `environment`. The first two
  re-queue the item with fresh attempts and a note (a validator block goes back to the validator), and you get a short
  "re-queued after triage" notice; anything else gives
  the usual blocked notice with the diagnosis appended. It never edits code or merges. The item gets `triaged=1`, so a second
  block always goes straight to you. If the usage gate says no, you get the plain notice. Turn it off with `AGENT_TRIAGE=0`
  in `~/.config/agent-runner/env`. Its log is `logs/<time>-<repo>-<slug>.triage.log`.

## Preprod (`preprod.sh`)
Preprod is one environment for every repo (the `engineerfamily-preprod` compose project also builds Strelitzia), so one PR from
any repo holds it at a time:
```
agent-runner preprod status                     # the holder; releases the lock when its PR was merged or closed
agent-runner preprod deploy engineerfamily 41   # take the lock, deploy the PR head, health-check it (exit 4 if busy)
agent-runner preprod release [<repo>]           # drop the lock, put preprod back on the repo's base branch
agent-runner preprod tick                       # status, then deploy the oldest awaiting-preprod item of any repo
```
The lock (`preprod.lock` in the state dir) is `repo<TAB>pr<TAB>sha<TAB>time`; a 3-column line from the old runner is read as
`strelitzia`. Each repo's commands come from the `[preprod]` section of its `agents/runner.toml`, read from `origin/<base>` of
the dev clone named in `~/.config/agent-runner/repos/<repo>.env` (`AGENT_REPO`), never from the PR branch, and checked by the prod
guard before anything runs:
```toml
[preprod]
app_dir = "/srv/Strelitzia-preprod"   # optional: fetched and checked out at the PR sha before deploy
deploy = "make -C /srv/engineerfamily-preprod preprod-deploy ref={sha}"   # {sha} = the PR head (or the base, on release)
release = "make -C /srv/engineerfamily-preprod preprod-deploy"           # optional; default: deploy again at origin/<base>
seed = "…"                            # optional: runs after every deploy
netrc_env = "PREPROD_NETRC"           # the repos/<repo>.env variable with the netrc for basic auth (the default)
health = [{ url = "https://preprod.engineerfamily.net/", status = 200 },
          { url = "https://preprod.engineerfamily.net/strelitzia/dashboard/api/health", json_ok = true }]
```
Health checks go through Cloudflare with the netrc, so they prove the public host routes to the right nginx. Each URL gets
6 tries, 10 s apart; a failure prints the URL, the HTTP code and at most 300 characters of the body, and the deploy is rolled back.
The deploy and seed commands' output (docker build logs) goes to `logs/<time>-<repo>.preprod-deploy.log` in the state
dir, not `runs.log`, which gets one line naming that file, plus its last 15 lines when a command fails. `preprod tick` logs
nothing when preprod is free or still held; `agent-runner preprod status` still says which.

## How a tick works (`run.sh`)
Every 5 minutes, for every registered repo, with at most one Claude run at a time (a `flock` on `run.lock`):
1. **Upkeep:** inbox replies (`inbox.py poll`), closing the notices of items that are `done` or `cancelled`
   (`inbox.py sweep`), queue intake for opted-in repos (`intake.py scan`, before the next-item pick so a plan pushed now
   can start this tick), and the global preprod lock (`preprod.sh tick`).
2. **Health gate:** while `health.py gate` says disk is critical, no Claude run starts (upkeep above still ran).
3. **Next item:** `buildqueue.py next` gives a due `<repo>/<slug>`; then the usage gate.
4. **The repo:** `AGENT_REPO` from `~/.config/agent-runner/repos/<repo>.env`. Its `agents/runner.toml` and the plan are read from
   `origin/<base>`; a missing plan, a missing config or one the prod guard refuses blocks the item with a notice.
5. **Implementer:** `/implement <plans>/<slug>.md --unattended` in `worktrees/<repo>/<slug>` on `agent/<slug>`, with the worktree's
   `agents/settings/implement.json` and `agents/mcp.json` (or `scripts/agent/settings.json`/`mcp.json` for a repo not yet on that layout).
6. **Merge or review:** the repo's `validate.full` again, `gate.py --config`, then `low` + green CI → `gh pr merge --<merge>`
   (`rebase`, `merge` or `squash`, from the config). A `review` tier goes to `awaiting-review` for the validator; a `low` PR
   whose CI isn't green gets `awaiting-preprod` and waits for you.

**Item lifecycle:** `queued → running → done` for `low` plans; `running → awaiting-review → (validator) → awaiting-preprod →
on-preprod → done` for `review` plans (a repo with no `[preprod]` merges straight after the validator approves). Also
`waiting-answer` (a question in the inbox), `blocked` and `cancelled`. A PR that conflicts with its base goes back to `queued`
for a conflict round, from any stage (see Limits, Conflicts).
**Notices close themselves.** Each tick, an open `agent-notice` issue titled `[<key>] …` is closed (with a comment) once that
item is `done` or `cancelled`. A "merged; N steps left for you" notice with steps stays open for you to close. Health notices
(`[vps]`) are closed by `health.py` on recovery. If you finish a blocked item by hand, `agent-runner queue set <key> status=done`
closes its notices.

**Validator, review stage.** Each tick, before any new implementer run, the oldest `awaiting-review` item gets one
fresh-context Opus run (1 h at most) in a detached checkout of the PR head (`worktrees/<repo>/review-<pr>`, removed after). It
never sees the implementer's session: its prompt ([prompts/review-pr.md](prompts/review-pr.md)) is the plan, the PR diff and the
repo's full validation. It runs `/code-review`, posts one `gh pr review --comment` (GitHub won't let the runner's account
approve its own PR) and writes `results/<repo>/<slug>.review.json` (`approve | changes | blocked`, a summary, findings).
- `approve` → `awaiting-preprod`, or merged after green CI when the repo has no preprod.
- `changes` → a **fix round**: re-queued for the implementer with the review as a note ("Review validator, round N/3"),
  resuming its session, and the run is told to fix exactly those findings. It uses no attempt (only a failed fix run does). An
  item gets 3 fix rounds (`AGENT_MAX_FIX_ROUNDS`), shared with the preprod stage; after that, or when the validator's
  `unresolved` list (previous findings still not fixed) is non-empty two rounds running, it is `blocked` and you get a notice.
- `blocked`, or no verdict twice → `blocked`, with a notice.
After a fix round (from either stage) the next review is a **re-review**: a Sonnet run (30 min at most; `AGENT_REREVIEW_MODEL`
overrides the model) with [prompts/rereview-pr.md](prompts/rereview-pr.md), scoped to the newest round's findings (or the
preprod stage's failed steps) and the fix diff since the sha the last verdict was about (`reviewed_sha`). It runs the full
validation but not `/code-review`, says for each previous finding whether it is fixed, raises nothing on code the fix didn't
touch (unless validation fails), posts one "Re-review (round N of 3)" review and reports the unfixed ones as `unresolved`.
The first review, a force-pushed head (no longer a descendant of `reviewed_sha`) and a conflict round get the full Opus
review. `runs.log` says `validator (review, opus)` or `validator (re-review, round N/3, sonnet)`.
Every `**Runs:** unattended` task must be `✅`, except one that can only run against preprod and is `⏭️ skipped` for that
reason: the PR reaches preprod only after this review approves, so the review notes it as "left for the preprod stage". Plans
put such checks in their Human test steps, which the preprod validator runs.
**Validator, preprod stage.** Once an approved `review` PR is `on-preprod` (`preprod.sh tick` deployed it), the next tick runs
one Sonnet validator (45 min at most) with [prompts/preprod-check.md](prompts/preprod-check.md). It works through the plan's
Human test steps on the public preprod host: `curl` with the repo's netrc, and the Playwright MCP, whose browser gets the
basic-auth credentials and, if `~/.config/agent-runner/preprod-storage-state.json` exists, the dev test account's session
(ROADMAP AM3). Both go in a 0600 temp config (`mcp_config.py`) that is deleted after the run. It never signs in itself and never
types a password. Steps that need your own account, prod, email, money, a device, or a sign-in it doesn't have are
"left for you". It comments a checklist on the PR and writes `results/<repo>/<slug>.preprod.json`.
- `approve` → merged after green CI, preprod on the new base branch, and one notice "`[<key>] merged; N steps left for you`"
  listing the steps only you can do (left-for-you steps never block the merge).
- `changes` → a fix round: the item re-queued for the implementer with the failed steps as a note ("Preprod validator,
  round N/3"; no attempt used). The 3 rounds are shared with the review stage; out of rounds it is `blocked`, with a notice.
- `blocked`, or no verdict twice → item `blocked`, notice.
After `changes` or `blocked` the PR stays on preprod (the lock is held) so you can look at it, and the notice says so. The lock
frees itself when the PR is merged or closed, or when the fixed PR is deployed again; `agent-runner preprod release <repo>`
frees it by hand. Until then other PRs wait for preprod.
**The signed-in session (ROADMAP AM3).** Do this after engineerfamily E2 (preprod-open) is on preprod, so no basic-auth
prompt sits in front of Google's sign-in. Redo it when the session expires: 30 days after sign-in, not rolling.
1. On your laptop (it needs a screen), with Node installed:
   `npx playwright@1 open --channel chrome --save-storage=state.json https://preprod.engineerfamily.net/`.
   A Chrome window opens. Click **Sign in** (top right) and sign in as the dev test account, never your own. Open
   `/strelitzia/dashboard/` and check the seeded jobs show. Close the window: Playwright writes `state.json` as it closes.
2. If Google refuses the sign-in in that window ("This browser or app may not be secure"), copy the cookie by hand instead:
   in a Chrome profile that only holds the test account, sign in on `https://preprod.engineerfamily.net/`, then DevTools →
   Application → Cookies → `https://preprod.engineerfamily.net` → copy the **Value** of `ef_session`. On the VPS run the
   snippet below and paste the value at the prompt (it isn't echoed). It writes the storage-state file directly, so skip step 3.
   ```bash
   read -rsp 'ef_session value: ' EF; echo
   EF=$EF python3 -c 'import json,os,time; p=os.path.expanduser("~/.config/agent-runner/preprod-storage-state.json"); fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600); os.write(fd,json.dumps({"cookies":[{"name":"ef_session","value":os.environ["EF"],"domain":"preprod.engineerfamily.net","path":"/","expires":int(time.time())+29*86400,"httpOnly":True,"secure":True,"sameSite":"Lax"}],"origins":[]}).encode()); os.close(fd)'
   unset EF
   ```
3. Copy it to the VPS and lock it down, then delete the laptop copy:
   `scp state.json <vps>:~/.config/agent-runner/preprod-storage-state.json && ssh <vps> chmod 600 ~/.config/agent-runner/preprod-storage-state.json && rm state.json`.
4. Check on the VPS: `curl -s -b "ef_session=$(python3 -c 'import json,os; print(next(c["value"] for c in json.load(open(os.path.expanduser("~/.config/agent-runner/preprod-storage-state.json")))["cookies"] if c["name"]=="ef_session"))')" https://preprod.engineerfamily.net/auth/me`
   → JSON with `"signed_in": true` and the test account's email.
Without the file the validator still runs and leaves signed-in steps to you. The netrc stays harmless once preprod has no
basic auth (curl and Playwright only send it when challenged).

Its permissions ([settings/validate.json](settings/validate.json), or the repo's `agents/settings/validate.json`) allow reading,
the validate commands, `gh pr diff/view/checks` and `gh pr review --comment`, and writing only its result file. They deny
edits, commits, pushes, merges, `make`, `docker compose` and `sudo`. Its log is `logs/<time>-<repo>-<slug>.review.log`.

**Dry run:** `AGENT_DRY_RUN=1 agent-runner run` logs each decision to `runs.log` ("dry run: would run claude for
engineerfamily/x in …", "dry run: nothing due") and exits before the inbox poll, preprod, `claude`, merges and queue changes.
The PR status sync still reads each PR, and only logs the edits it would make.
Rehearse against a copy of the queue: `T=$(mktemp -d); cp ~/.local/state/agent-runner/queue.json $T/;
AGENT_RUNNER_STATE=$T AGENT_DRY_RUN=1 agent-runner run; tail $T/runs.log`.

## PR status block (`prstatus.py`)
Every PR the runner builds, in every registered repo, starts with a status block, so the GitHub app shows where it is:

```
<!-- agent-runner:status -->
**Runner status:** 🔁 Validator review: asked for changes (…); implementer re-run queued, fix round 1/3 · updated 2026-10-22 16:40 UTC
1. ✅ Implementer opened the PR, 7 Human test steps
2. 🔁 Validator reviews the code: asked for changes: …
3. ⏳ Runner deploys the branch to preprod
4. ⏳ Second validator runs the Human test steps on preprod and posts a checklist
5. ⏳ Merge, preprod back on main; steps it couldn't do come to you as a "merged; N steps left for you" issue
<!-- /agent-runner:status -->
```

- **Stages:** the implementer's PR, the validator's review, the preprod deploy, the preprod check (linking the validator's
  checklist comment), the merge (linking the "steps left for you" issue).
- **Marks:** ✅ done · 🔁 in progress or re-running · ⏳ not reached · ❌ failed (with why) · ⏸ blocked or waiting for you
  (with the inbox issue) · ➖ skipped (`low` tier merges on green CI, so 2-4; a repo without `[preprod]`, 3-4).
  Stage 1 warns "⚠️ no Human test steps" in a repo with preprod when the PR body has none.
- **First line** says where it is now, so the PR list preview tells the story.
- **Refresh:** `run.sh` updates it right after each transition (`prstatus.py update <key>`) and every tick
  (`prstatus.py sync`: every item with a PR that isn't cancelled, and a `done` item until its final block is written, up
  to 14 days). It is derived from queue state, so PRs opened before this and stale blocks catch up within one tick. The
  body is edited only when the block changes (the time aside); a `gh` failure logs one line in `runs.log`.
- **Editing:** anything inside the markers is overwritten; the rest of the description is never touched.
- **Release:** in a repo with `[release]`, a merged PR's stage 5 adds "released in v1.4.4 (pre-release v1.4)": the
  oldest tag containing its merge commit. The block isn't final until that tag exists.

## Versions and releases (`versions.py`)
GitHub Releases show what is on prod and preprod, for a repo whose `agents/runner.toml` has a `[release]` table
(`tags = true` is the default; `tags = false` turns it off again):

- **Every PR merged to the base gets the next patch tag.** Newest tag `v1.4.3` → `v1.4.4`, an annotated tag on the PR's
  merge commit, listing the PR. `run.sh` runs `agent-runner versions sync <repo>` right after each runner merge (before the
  preprod redeploy, so preprod shows the tag) and every tick after the inbox poll (to catch PRs merged by hand). One sync
  makes one tag, on the newest merge commit, listing every PR since the last tag. Commits pushed straight to the base
  without a PR aren't tagged.
- **One pre-release per minor version**, titled `v1.4`, always on the minor's newest tag. Its notes list every patch,
  newest first: `- v1.4.4 · #71 Wordle: nginx /wordle/ (2026-10-23)`.
- **Prod deploy promotes it:** `agent-runner versions promote <repo> [<tag>]` (default: the newest tag; your
  `make prod-release` runs it) makes `vX.Y` a full, Latest release on that tag, with "Deployed to prod <date> at <tag>"
  added. The next merge then starts `vX.(Y+1).0` and a `vX.(Y+1)` pre-release. `--next-major` makes it `v(X+1).0.0`
  instead. Major bumps happen only that way.
- **Bootstrap (exit 3):** sync never guesses where to start. With no `vX.Y.Z` tag yet, or a newest tag that the runner
  didn't make and whose minor has no release, it pushes nothing and says
  "no release for vX.Y yet: run `agent-runner versions promote <repo> <tag>` for what prod runs". Tag what prod runs,
  promote it, and the next merge is tagged.
- **Failures:** a failed sync or an exit 3 sends one inbox notice per cause, and never blocks the tick or the redeploy.
  A release step that failed after its tag was pushed is repaired by the next sync. `agent-runner versions status <repo>`
  shows the newest tag, the open pre-release and the newest full release. `--dry-run` works on `sync` and `promote`.
- Only this runner code pushes tags or edits releases. Claude sessions never do.

## Queue from anywhere (`intake.py`)
A plan pushed to a repo's base branch (from a Claude cloud session, the GitHub web editor, anywhere) can queue itself:
- **Opt in:** the repo's `agents/runner.toml` on the base branch has `[intake]` with `enabled = true` (your step, per repo).
- **The line:** in the plan's header, `**Queue:** yes | no [at <ISO time with Z or an offset>] [after <repo>/<slug>]`,
  e.g. `**Queue:** yes at 2026-10-27T02:00Z after agent-runner/x-1-config`. A bare `after` slug means the same repo.
- **Every tick** runs `intake.py scan`: it fetches each registered repo and reads only `origin/<base>`, never a branch
  or the working tree. `agent-runner intake scan [--repo N] [--dry-run]` runs it by hand.
- **Intake once, local wins:** a plan is queued once, as `<repo>/<slug>` (`source: intake`). An existing item with that key,
  queued by hand or earlier, is never touched, except that `**Queue:** no` cancels an intake item that hasn't started.
- **Refusals are notices:** a plan over the size budget, without a `**Risk:**` line or unattended task, with an unreadable
  line, or with an `after` that isn't queued gets one inbox notice per pushed version (`not queued: …`) and `intake: refused`
  in `runs.log`. Fix and push it again, or queue it by hand.

## Day to day
- Plan with Claude (`/plan`), then queue it (`/schedule-build <slug> [at 02:00] [after <slug>]`), or, in a repo with intake,
  push it with `**Queue:** yes`.
- **Queue keys.** One queue serves every repo. Items are keyed `<repo>/<slug>` (`strelitzia/site-sso-3-proxy-mode`,
  `engineerfamily/ef-agents-layout`): `agent-runner queue add <slug> --repo engineerfamily` (`--repo` is required
  unless the slug is a full `<repo>/<slug>` key). `--after` takes `repo/slug`, so a plan can wait for a merge in another repo; a bare slug means the same repo.
  Every other command takes the full key, or a bare slug when only one repo has an item with it (several → it exits and names them).
  Inbox issues are labelled with the item's repo.
- **State.** State is in `$AGENT_RUNNER_STATE` (default `~/.local/state/agent-runner`). A `queue.json` with bare keys
  or a bare `after` (the old runner's format) is an error: migrate it to `<repo>/<slug>` keys first.
- Logs: `runs.log` (one line per event) and `logs/<time>-<slug>.log` (full JSON output) in the state dir.
- When an agent asks a question, reply on the inbox issue. The next tick re-queues the item with your answer and resumes the
  agent's own session.

## Develop
`scripts/validate.sh` (`bash -n`, `py_compile`, hermetic pytest via `uv`) runs locally and in CI (`.github/workflows/ci.yml`, once it is added: ROADMAP AM1a).
Standard library only.

## VPS health (`health.py`)
The VPS runs prod, preprod and every agent run, so a full disk or memory pressure breaks them all at once. `health.py` measures
the box cheaply (no Claude), frees disk with fixed rules, and tells you when that isn't enough.

```
python3 health.py check                        # JSON {"metrics": …, "problems": […]}; exit 1 when there are problems
python3 health.py clean --dry-run --pressure   # every rule and what it would free; deletes nothing
python3 health.py clean                        # routine rules (once a day), plus pressure rules while disk is over 80%
python3 health.py tick                         # cron: clean, re-measure, notify (below)
python3 health.py gate                         # exit 1 while disk is at or over disk_alert_pct (run.sh starts no Claude run then)
python3 health.py diagnose [--force]           # the read-only diagnosis on its own; --force ignores the 6 h spacing
python3 health.py sessions                     # open Claude desktop sessions, most idle first, with memory use
```

**Thresholds** (override any of them in `~/.config/agent-runner/health.toml`; an unknown key is an error):
```toml
[thresholds]
disk_clean_pct = 80      # clean up at or over this disk use
disk_alert_pct = 90      # alert at or over this, whatever cleanup does
mem_available_mb = 400   # MemAvailable under this is a problem (alert only)
swap_used_mb = 2048      # swap used over this is a problem (alert only)
load_per_cpu = 2.0       # 15-minute load per CPU over this is a problem (alert only)
```

**Routine rules** (at most once every 23 h): Docker build cache down to 3 GB (`builder prune --keep-storage 3GB`), exited
containers older than 24 h, runner logs older than 14 days and results older than 30 days (in the `agent-runner` state dir), runner worktrees older than 7 days whose branch is gone from origin, your own top-level `/tmp`
entries untouched for 3 days (never `/tmp/claude-*`), and backups in `~/backups` and `/srv/backups` older than 7 days.

**Pressure rules** (only while disk is over `disk_clean_pct`, or with `--pressure`; in this order, re-measuring after each and
stopping once under): all build cache, images unused for 7 days, backups older than 24 h, `uv cache prune`, `pip cache purge`,
and all but the newest Playwright build of each browser.

**Never touched:** Docker volumes (`volume` and `system prune` are never called), running or compose-managed containers' images,
`/srv/*` checkouts, any `.env` outside the backup dirs, databases, `/var/log` and the journal, and processes. Memory and load
never trigger a deletion. Every rule deletes only strictly inside its own root and skips symlinks; a refused path is logged.

**`health.log`** (in the state dir, `~/.local/state/agent-runner/`) has one JSON line per rule run:
`{"time", "rule", "tier", "dry_run", "freed_bytes"}`, plus `"error"` when the rule failed (the next rule still runs), and
`"rule": "refused"` lines for paths a guard skipped. `health.json` next to it records when the routine rules last ran.

**Alerts (`health tick`).** Each tick (its own lock, so ticks never overlap) runs `clean`, measures again, and compares the problems
with the open ones in `health.json`. A new problem opens one `agent-notice` issue in this repo labelled `vps-health`
(`[vps] disk 91% used (alert at 90%) after cleanup`) plus an ntfy push. The body has the metrics, what this tick's cleanup freed,
and the diagnosis. While it lasts, the issue gets at most one comment a day. When it clears, it gets a "Recovered" comment and is
closed. Load counts only when it was also high on a tick at least 15 minutes earlier. A failed `gh` call is logged and retried on
the next tick.

**Claude sessions.** Each Claude desktop session open on the VPS keeps a `ccd-cli` process of about 200–300 MB until you archive
it in the desktop app. Each tick finds those processes (argv[0] under `~/.claude/remote/ccd-cli/`) and records their CPU time in
`health.json`. A session counts as idle once it has used under 0.02 CPU-seconds per second since the last tick, and a session seen
for the first time counts as active. Memory and swap notices (new, and the daily "still open" comment) list every session with its
memory and idle time, most idle first, so you know which ones to archive. Only the app can archive, so this only reports: it never
signals or stops a process.

**Diagnosis.** Before the first notice of a problem, a short Sonnet run (`claude-sonnet-5-5`, 10 minutes, 15 turns,
`--permission-mode dontAsk` with [settings/health-diagnose.json](settings/health-diagnose.json)) looks around with `df`, `du`, `free`,
`ps`, `ls` and `docker system df/ps/images`, and writes what uses the space or memory and the commands you could run. It can't
edit, delete, kill or sudo. It is skipped (the notice goes out without it) when there is no `CLAUDE_CODE_OAUTH_TOKEN`, the
usage gate refuses, a build run holds a run lock, the last diagnosis was under 6 hours ago, or disk is at 95% or more.
The prompt is [prompts/health-diagnose.md](prompts/health-diagnose.md).

**Cron (you, ROADMAP AM2):** `*/15 * * * * $HOME/.local/bin/agent-runner health tick`, separate from the build runner's line so
upkeep keeps working when the runner is broken or paused.
