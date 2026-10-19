#!/usr/bin/env bash
# Cron entry point (the manager) for unattended builds of every registered repo. After the cutover, in the `dev` crontab:
#   */5 * * * * $HOME/.local/bin/agent-runner run
# Everything before `claude -p` is plain bash/Python, so an empty queue costs no Claude usage.
#
# 1. Upkeep: pick up your inbox replies, release/advance the global preprod lock (preprod.sh tick).
# 2. Health gate: while disk is critical (health.py gate), no Claude run starts; upkeep above still ran.
# 3. `buildqueue.py next` picks a due `<repo>/<slug>` (honours pause, optional daily cap, attempt limit) or we exit.
#    Then usage.py checks the Claude "Current session" usage (one tiny Haiku probe): at or above
#    AGENT_MAX_SESSION_USAGE (default 70%) the queue pauses until the 5-hour window resets.
# 4. The repo comes from ~/.config/agent-runner/repos/<repo>.env (AGENT_REPO = its dev clone). Its agents/runner.toml and
#    the plan are read from origin/<base>, never from a branch, and the config must pass config.py's checks.
# 5. The agent runs `/implement <plans>/<slug>.md --unattended` in worktrees/<repo>/<slug> on branch agent/<slug>, with the
#    repo's agents/settings/implement.json and agents/mcp.json. It validates, pushes, opens a PR and writes a result file.
#    It cannot merge. After your answer, or after a run stopped at the usage limit, the next run resumes that session.
# 6. This script decides the merge: the repo's full validation again, gate.py --config (plan risk + diff checks), and green
#    CI → merge with the repo's merge method; otherwise the PR waits for preprod (preprod.sh) and you.
#
# AGENT_DRY_RUN=1 logs each decision ("would …") to runs.log and stops before anything with side effects.
set -uo pipefail
export HOME=${HOME:-/home/dev}
export PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin

HERE=$(dirname "$(readlink -f "$0")")
CFG=(python3 "$HERE/config.py")
# CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`), AGENT_INBOX_REPO, NTFY_TOPIC: ~/.config/agent-runner/env
# (or, for one release, the old ~/.config/strelitzia-agent/env).
env_file=$("${CFG[@]}" env-file) && . "$env_file"
LEGACY_AGENT_REPO=${AGENT_REPO:-}  # the old env named Strelitzia's clone; repos/strelitzia.env replaces it

STATE=${AGENT_RUNNER_STATE:-${STRELITZIA_AGENT_STATE:-$HOME/.local/state/agent-runner}}
MAX_RUNTIME=${AGENT_MAX_RUNTIME:-3h}
DRY=${AGENT_DRY_RUN:-}
# Opus has a 1M-token window, so long runs never auto-compacted and re-sent 250-380k tokens of context every turn.
# Compact at 200k instead (AGENT_COMPACT_WINDOW in the env file overrides it).
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=${AGENT_COMPACT_WINDOW:-200000}
export AGENT_RUNNER_STATE=$STATE
unset STRELITZIA_AGENT_STATE
mkdir -p "$STATE/logs" "$STATE/results" "$STATE/worktrees" "$STATE/configs"
Q=(python3 "$HERE/buildqueue.py")
stamp() { date -u +%FT%TZ; }
say() { echo "$(stamp) $*" >> "$STATE/runs.log"; }
dry() {  # dry <what>: in a dry run, log "would <what>" and stop here
    [ -n "$DRY" ] || return 0
    say "dry run: would $*"
    exit 0
}

exec 9>"$STATE/run.lock"
flock -n 9 || exit 0

# 1. Upkeep
if [ -n "$DRY" ]; then
    say "dry run: would poll the inbox and tick preprod"
else
    [ -n "${AGENT_INBOX_REPO:-}" ] && python3 "$HERE/inbox.py" poll >> "$STATE/runs.log" 2>&1
    "$HERE/preprod.sh" tick >> "$STATE/runs.log" 2>&1
fi

# 2. Health gate
if ! python3 "$HERE/health.py" gate >/dev/null 2>&1; then
    say "disk critical, no runs (health.py gate)"
    exit 0
fi

# 3. Next item
key=$("${Q[@]}" next) || { [ -n "$DRY" ] && say "dry run: nothing due"; exit 0; }
repo=${key%%/*}
slug=${key#*/}

if [ -n "$DRY" ]; then
    say "dry run: $key is due; would check session usage"
else
    gate=$(python3 "$HERE/usage.py" check 2>&1)
    case $? in
        0) ;;
        10) "${Q[@]}" pause "$(echo "$gate" | tail -n1)" >> "$STATE/runs.log"
            say "$key: not started, $(echo "$gate" | head -n1); queue paused until the window resets"
            exit 0 ;;
        *) say "$key: not started, $(echo "$gate" | head -n1)"
           exit 0 ;;
    esac
fi

notify() {  # notify <title> <body>: an agent-notice issue labelled with the repo
    [ -n "$DRY" ] && { say "dry run: would notify: $1"; return 0; }
    [ -n "${AGENT_INBOX_REPO:-}" ] && python3 "$HERE/inbox.py" notify --slug "$key" --project "$repo" --title "$1" --body "$2" >/dev/null
}
block() {  # block <log line> <notice title> <notice body>
    say "$key: $1, blocked"
    notify "$2" "$3"
    [ -n "$DRY" ] && exit 0
    "${Q[@]}" set "$key" status=blocked
    exit 0
}

# 4. The repo, its config and the plan, all from origin/<base>
unset AGENT_REPO
repo_env=$("${CFG[@]}" repo-env "$repo") && eval "$repo_env"
[ -z "${AGENT_REPO:-}" ] && [ "$repo" = strelitzia ] && AGENT_REPO=$LEGACY_AGENT_REPO
if [ -z "${AGENT_REPO:-}" ] || [ ! -d "$AGENT_REPO" ]; then
    block "no AGENT_REPO for $repo" "repo not registered" \
        "Add AGENT_REPO=<dev clone> to ~/.config/agent-runner/repos/$repo.env, then re-queue $key."
fi
git -C "$AGENT_REPO" fetch -q origin
base_ref=$(git -C "$AGENT_REPO" rev-parse --abbrev-ref origin/HEAD 2>/dev/null) || base_ref=origin/${AGENT_BASE:-main}
toml=$STATE/configs/$repo.toml
if ! git -C "$AGENT_REPO" show "$base_ref:agents/runner.toml" > "$toml" 2>/dev/null; then
    block "no agents/runner.toml on $base_ref" "no runner.toml" "Commit agents/runner.toml to $base_ref, then re-queue $key."
fi
if ! problems=$("${CFG[@]}" check "$toml"); then
    block "agents/runner.toml refused" "runner.toml refused" "config.py check on $base_ref:agents/runner.toml says:
$problems"
fi
cfg() { "${CFG[@]}" get "$toml" "$1" 2>/dev/null; }
GH_REPO=$(cfg repo.github)
BASE=$(cfg repo.base)
MERGE=$(cfg repo.merge)
PLANS=$(cfg repo.plans)
VALIDATE_FULL=$(cfg validate.full)
export GH_REPO
plan=$PLANS/$slug.md
if ! git -C "$AGENT_REPO" cat-file -e "origin/$BASE:$plan" 2>/dev/null; then
    block "$plan is not on origin/$BASE" "plan not on $BASE" \
        "Commit and push $plan, then re-queue it with /schedule-build $slug."
fi

# 5. Implementer
wt=$STATE/worktrees/$repo/$slug
branch=agent/$slug
dry "run claude for $key in $wt (branch $branch from origin/$BASE)"
if [ ! -d "$wt" ]; then
    mkdir -p "$(dirname "$wt")"
    if git -C "$AGENT_REPO" ls-remote --exit-code -q origin "$branch" >/dev/null; then
        git -C "$AGENT_REPO" worktree add -q -B "$branch" "$wt" "origin/$branch"
    else
        git -C "$AGENT_REPO" worktree add -q -B "$branch" "$wt" "origin/$BASE"
    fi
fi
settings=$wt/agents/settings/implement.json
[ -f "$settings" ] || settings=$wt/scripts/agent/settings.json  # repos not yet on the agents/ layout (Strelitzia today)
mcp=$wt/agents/mcp.json
[ -f "$mcp" ] || mcp=$wt/scripts/agent/mcp.json

"${Q[@]}" start "$key"
result=$STATE/results/$repo-$slug.json
log=$STATE/logs/$(date -u +%Y%m%dT%H%MZ)-$repo-$slug.log
rm -f "$result"
notes=$("${Q[@]}" get "$key" notes 2>/dev/null || echo "[]")

prompt="/implement $plan --unattended

Result file: $result
Notes from the queue (including the user's answers to earlier questions): $notes"

# After an answer (`resume=1`) or a usage-limit stop (`resume=limit`), resume that session instead of rebuilding its context.
resume_id=
resume_why=$("${Q[@]}" get "$key" resume 2>/dev/null)
case $resume_why in 1|limit) resume_id=$("${Q[@]}" get "$key" session_id 2>/dev/null) ;; esac

# run_claude <prompt> [extra claude args]: one agent run in the worktree, appended to $log.
run_claude() {
    local p=$1
    shift
    (
        cd "$wt" || exit 1
        AGENT_SLUG=$key AGENT_RESULT_FILE=$result timeout --kill-after=2m "$MAX_RUNTIME" claude -p "$@" "$p" \
            --model claude-opus-5-5 \
            --settings "$settings" \
            --mcp-config "$mcp" \
            --permission-mode auto --permission-prompts none \
            --output-format json
    ) >> "$log" 2>&1
}

: > "$log"
if [ -n "$resume_id" ]; then
    if [ "$resume_why" = limit ]; then
        opener="Your previous run was stopped by the Claude usage limit. The work you committed or left in the worktree is still there."
    else
        opener="The user answered your question. $(echo "$notes" | python3 -c 'import json,sys; n=json.load(sys.stdin); print(n[-1] if n else "")' 2>/dev/null)"
    fi
    say "$key: run start, resuming session $resume_id ($resume_why) (log $log)"
    echo "claude -p --resume $resume_id" >> "$log"
    run_claude "$opener

Result file: $result
All queue notes: $notes
Continue the plan ($plan, --unattended) from where you stopped." --resume "$resume_id"
    code=$?
    if [ "$code" -ne 0 ] && [ ! -f "$result" ] && grep -qi 'no conversation found' "$log"; then
        # Keep the failed resume's output apart, so the usage-limit check and session_id below see only the fresh run.
        mv "$log" "$log.resume-failed"
        say "$key: resume failed, fresh run"
        echo "resume failed, fresh run (resume output: $log.resume-failed)" > "$log"
        run_claude "$prompt"
        code=$?
    fi
else
    say "$key: run start (log $log)"
    run_claude "$prompt"
    code=$?
fi
# Read session_id now: later steps append validation output to $log. The last JSON line with the key wins.
session_id=$(python3 -c '
import json, sys
for line in reversed(open(sys.argv[1], errors="replace").read().splitlines()):
    try:
        data = json.loads(line)
    except ValueError:
        continue
    if isinstance(data, dict) and data.get("session_id"):
        print(data["session_id"])
        break
' "$log")
[ -n "$session_id" ] && "${Q[@]}" set "$key" "session_id=$session_id"
say "$key: claude exit $code"

# A usage-limit exit isn't the plan's fault: give the attempt back and pause the queue.
if [ ! -f "$result" ] && grep -qiE 'usage limit|rate.limit|limit reached' "$log"; then
    attempts=$("${Q[@]}" get "$key" attempts)
    "${Q[@]}" set "$key" status=queued "attempts=$((attempts - 1))"
    # Resume this session next time instead of redoing its work. An answer resume keeps `resume=1` (same session either way).
    [ -n "$session_id" ] && [ "$resume_why" != 1 ] && "${Q[@]}" set "$key" resume=limit
    "${Q[@]}" pause 5 >> "$STATE/runs.log"
    say "$key: usage limit hit, queue paused 5h"
    exit 0
fi
"${Q[@]}" unset "$key" resume

# 6. The agent reports `ready` (PR open, validated) / `question` / `blocked`. Turn `ready` into merged or review.
status=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "$result" 2>/dev/null)
if [ "$status" = ready ] && ! (cd "$wt" && bash -c "$VALIDATE_FULL" >> "$log" 2>&1); then
    say "$key: agent said ready but '$VALIDATE_FULL' failed; counting a failed attempt"
    rm -f "$result"
    status=""
fi
if [ "$status" = ready ]; then
    pr=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pr"])' "$result")
    verdict=$(cd "$wt" && python3 "$HERE/gate.py" "$plan" --config "$toml" --base "origin/$BASE")
    tier=$(echo "$verdict" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tier"])')
    if [ "$tier" = low ] && timeout 30m gh pr checks "$pr" --repo "$GH_REPO" --watch --fail-fast >/dev/null 2>&1 \
        && gh pr merge "$pr" --repo "$GH_REPO" "--$MERGE" --delete-branch >/dev/null; then
        final=merged
        notify "merged PR #$pr" "Merged to $BASE after green CI (gate tier low): https://github.com/$GH_REPO/pull/$pr"
        # Only if this PR itself held preprod: put preprod back on the base.
        [ "$(cut -f1,2 "$STATE/preprod.lock" 2>/dev/null)" = "$repo"$'\t'"$pr" ] && "$HERE/preprod.sh" release "$repo" >> "$STATE/runs.log" 2>&1
    else
        final=review
        gh pr edit "$pr" --repo "$GH_REPO" --add-label awaiting-preprod >/dev/null 2>&1 \
            || { gh label create awaiting-preprod --repo "$GH_REPO" --force >/dev/null; gh pr edit "$pr" --repo "$GH_REPO" --add-label awaiting-preprod >/dev/null; }
        gh pr comment "$pr" --repo "$GH_REPO" --body "Risk gate: \`$tier\`
\`\`\`json
$verdict
\`\`\`" >/dev/null
    fi
    python3 - "$result" "$final" <<'EOF'
import json, sys
path, final = sys.argv[1], sys.argv[2]
data = json.load(open(path)); data["status"] = final
json.dump(data, open(path, "w"))
EOF
fi

new=$("${Q[@]}" finish "$key" "$result")
say "$key: -> $new"
case "$new" in
    done|awaiting-preprod)
        git -C "$AGENT_REPO" worktree remove --force "$wt" ;;
    blocked)
        notify "blocked after $("${Q[@]}" get "$key" attempts) attempt(s)" "Log: $log (on the VPS). Last result: $(cat "$result" 2>/dev/null || echo none)" ;;
esac

# Cap the Docker build cache: a full validation can rebuild an image, and a busted dependency layer adds ~750 MB, so a
# chain of runs could fill the disk. Recent layers stay, so rebuilds stay fast.
docker builder prune -f --keep-storage "${AGENT_BUILD_CACHE_KEEP:-3GB}" >> "$STATE/runs.log" 2>&1 || true
"$HERE/preprod.sh" tick >> "$STATE/runs.log" 2>&1
exit 0
