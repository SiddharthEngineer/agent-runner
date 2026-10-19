#!/usr/bin/env bash
# Cron entry point for unattended builds. Installed every 5 minutes in the `dev` crontab:
#   */5 * * * * <repo>/scripts/agent/run.sh   (the dev clone, e.g. ~/src/Strelitzia)
# Everything before `claude -p` is plain bash/Python, so an empty queue costs no Claude usage.
#
# 1. Upkeep: pick up your inbox replies, release/advance the preprod lock.
# 2. `buildqueue.py next` picks a due item (honours pause, optional daily cap, attempt limit) or we exit.
#    Then usage.py checks the Claude "Current session" usage (one tiny Haiku probe): at or above
#    AGENT_MAX_SESSION_USAGE (default 70%) the queue pauses until the 5-hour window resets.
# 3. The agent runs `/implement <plan> --unattended` in its own worktree on branch agent/<slug>, validates,
#    pushes and opens a PR, then writes a result file. It cannot merge.
#    After you answer its question, or after a run stopped at the usage limit, the next run resumes that agent's
#    session (`--resume <session_id>`), falling back to a fresh run if Claude can't find the session.
# 4. This script decides the merge: gate.py (plan risk + diff checks) and green CI → rebase-merge;
#    otherwise the PR waits for preprod (preprod.sh) and you.
#    AGENT_MERGE_POLICY=all (env file) merges every ready PR after green CI, whatever its tier, merges PRs already
#    waiting for review once their CI is green, and redeploys preprod from trunk after each merge. Default: gate.
set -uo pipefail
export HOME=${HOME:-/home/dev}
export PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
# CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`), AGENT_REPO, AGENT_INBOX_REPO, NTFY_TOPIC, PREPROD_*.
CONFIG_DIR=$HOME/.config/strelitzia-agent
[ -f "$CONFIG_DIR/env" ] && . "$CONFIG_DIR/env"

HERE=$(cd "$(dirname "$0")" && pwd)
AGENT_REPO=${AGENT_REPO:-$(cd "$HERE/../.." && pwd)}
STATE=${STRELITZIA_AGENT_STATE:-$HOME/.local/state/strelitzia-agent}
GH_REPO=${GH_REPO:-SiddharthEngineer/Strelitzia}
MAX_RUNTIME=${AGENT_MAX_RUNTIME:-3h}
MERGE_POLICY=${AGENT_MERGE_POLICY:-gate}
# Opus has a 1M-token window, so long runs never auto-compacted and re-sent 250-380k tokens of context every turn.
# Compact at 200k instead (AGENT_COMPACT_WINDOW in the env file overrides it).
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=${AGENT_COMPACT_WINDOW:-200000}
export STRELITZIA_AGENT_STATE=$STATE GH_REPO
mkdir -p "$STATE/logs" "$STATE/results" "$STATE/worktrees"
Q=(python3 "$HERE/buildqueue.py")
stamp() { date -u +%FT%TZ; }
say() { echo "$(stamp) $*" >> "$STATE/runs.log"; }

exec 9>"$STATE/run.lock"
flock -n 9 || exit 0

[ -n "${AGENT_INBOX_REPO:-}" ] && python3 "$HERE/inbox.py" poll >> "$STATE/runs.log" 2>&1

# Policy `all`: preprod follows trunk. `status` releases (and redeploys trunk) when its PR was merged; else redeploy if free.
redeploy_preprod() {
    [ "$MERGE_POLICY" = all ] || return 0
    local out
    out=$("$HERE/preprod.sh" status 2>&1)
    echo "$out" >> "$STATE/runs.log"
    case $out in *"held by"*|*releasing*) return 0 ;; esac
    "$HERE/preprod.sh" release >> "$STATE/runs.log" 2>&1
}

# Policy `all`: merge PRs that were left for review (awaiting-preprod / on-preprod) once their CI is green.
if [ "$MERGE_POLICY" = all ]; then
    merged_any=
    while read -r wslug wpr; do
        [ -n "$wpr" ] || continue
        gh pr checks "$wpr" --repo "$GH_REPO" >/dev/null 2>&1 || continue
        if gh pr merge "$wpr" --repo "$GH_REPO" --rebase --delete-branch >/dev/null 2>&1; then
            "${Q[@]}" set "$wslug" status=done
            say "$wslug: PR #$wpr merged (policy all, CI green)"
            [ -n "${AGENT_INBOX_REPO:-}" ] && python3 "$HERE/inbox.py" notify --slug "$wslug" --title "merged PR #$wpr" \
                --body "Merged to trunk without review (AGENT_MERGE_POLICY=all): https://github.com/$GH_REPO/pull/$wpr" >/dev/null
            merged_any=1
        fi
    done < <(python3 -c '
import json, os, sys
items = json.load(open(sys.argv[1]))["items"] if os.path.exists(sys.argv[1]) else {}
for slug, it in items.items():
    if it.get("status") in ("awaiting-preprod", "on-preprod") and it.get("pr"):
        print(slug, it["pr"])
' "$STATE/queue.json")
    [ -n "$merged_any" ] && redeploy_preprod
fi
"$HERE/preprod.sh" tick >> "$STATE/runs.log" 2>&1

slug=$("${Q[@]}" next) || exit 0

gate=$(python3 "$HERE/usage.py" check 2>&1)
case $? in
    0) ;;
    10) "${Q[@]}" pause "$(echo "$gate" | tail -n1)" >> "$STATE/runs.log"
        say "$slug: not started, $(echo "$gate" | head -n1); queue paused until the window resets"
        exit 0 ;;
    *) say "$slug: not started, $(echo "$gate" | head -n1)"
       exit 0 ;;
esac

notify() { [ -n "${AGENT_INBOX_REPO:-}" ] && python3 "$HERE/inbox.py" notify --slug "$slug" --title "$1" --body "$2" >/dev/null; }

git -C "$AGENT_REPO" fetch -q origin
if ! git -C "$AGENT_REPO" cat-file -e "origin/trunk:agents/plans/$slug.md" 2>/dev/null; then
    "${Q[@]}" set "$slug" status=blocked
    say "$slug: agents/plans/$slug.md is not on origin/trunk, blocked"
    notify "plan not on trunk" "Commit and push agents/plans/$slug.md, then re-queue it with /schedule-build $slug."
    exit 0
fi

wt=$STATE/worktrees/$slug
branch=agent/$slug
if [ ! -d "$wt" ]; then
    if git -C "$AGENT_REPO" ls-remote --exit-code -q origin "$branch" >/dev/null; then
        git -C "$AGENT_REPO" worktree add -q -B "$branch" "$wt" "origin/$branch"
    else
        git -C "$AGENT_REPO" worktree add -q -B "$branch" "$wt" origin/trunk
    fi
fi

"${Q[@]}" start "$slug"
result=$STATE/results/$slug.json
log=$STATE/logs/$(date -u +%Y%m%dT%H%MZ)-$slug.log
rm -f "$result"
notes=$("${Q[@]}" get "$slug" notes 2>/dev/null || echo "[]")

prompt="/implement agents/plans/$slug.md --unattended

Result file: $result
Notes from the queue (including the user's answers to earlier questions): $notes"

# After an answer (`resume=1`) or a usage-limit stop (`resume=limit`), resume that session instead of rebuilding its context.
resume_id=
resume_why=$("${Q[@]}" get "$slug" resume 2>/dev/null)
case $resume_why in 1|limit) resume_id=$("${Q[@]}" get "$slug" session_id 2>/dev/null) ;; esac

# run_claude <prompt> [extra claude args]: one agent run in the worktree, appended to $log.
run_claude() {
    local p=$1
    shift
    (
        cd "$wt" || exit 1
        AGENT_SLUG=$slug AGENT_RESULT_FILE=$result timeout --kill-after=2m "$MAX_RUNTIME" claude -p "$@" "$p" \
            --model claude-opus-5-5 \
            --settings "$wt/scripts/agent/settings.json" \
            --mcp-config "$wt/scripts/agent/mcp.json" \
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
    say "$slug: run start, resuming session $resume_id ($resume_why) (log $log)"
    echo "claude -p --resume $resume_id" >> "$log"
    run_claude "$opener

Result file: $result
All queue notes: $notes
Continue the plan (agents/plans/$slug.md, --unattended) from where you stopped." --resume "$resume_id"
    code=$?
    if [ "$code" -ne 0 ] && [ ! -f "$result" ] && grep -qi 'no conversation found' "$log"; then
        # Keep the failed resume's output apart, so the usage-limit check and session_id below see only the fresh run.
        mv "$log" "$log.resume-failed"
        say "$slug: resume failed, fresh run"
        echo "resume failed, fresh run (resume output: $log.resume-failed)" > "$log"
        run_claude "$prompt"
        code=$?
    fi
else
    say "$slug: run start (log $log)"
    run_claude "$prompt"
    code=$?
fi
# Read session_id now: later steps append validate.sh output to $log. The last JSON line with the key wins.
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
[ -n "$session_id" ] && "${Q[@]}" set "$slug" "session_id=$session_id"
say "$slug: claude exit $code"

# A usage-limit exit isn't the plan's fault: give the attempt back and pause the queue.
if [ ! -f "$result" ] && grep -qiE 'usage limit|rate.limit|limit reached' "$log"; then
    attempts=$("${Q[@]}" get "$slug" attempts)
    "${Q[@]}" set "$slug" status=queued "attempts=$((attempts - 1))"
    # Resume this session next time instead of redoing its work. An answer resume keeps `resume=1` (same session either way).
    [ -n "$session_id" ] && [ "$resume_why" != 1 ] && "${Q[@]}" set "$slug" resume=limit
    "${Q[@]}" pause 5 >> "$STATE/runs.log"
    say "$slug: usage limit hit, queue paused 5h"
    exit 0
fi
"${Q[@]}" unset "$slug" resume

# The agent reports `ready` (PR open, validated) / `question` / `blocked`. Turn `ready` into merged or review.
status=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "$result" 2>/dev/null)
if [ "$status" = ready ] && ! (cd "$wt" && scripts/validate.sh --full >> "$log" 2>&1); then
    say "$slug: agent said ready but validate.sh --full failed; counting a failed attempt"
    rm -f "$result"
    status=""
fi
if [ "$status" = ready ]; then
    pr=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pr"])' "$result")
    verdict=$(cd "$wt" && python3 scripts/agent/gate.py "agents/plans/$slug.md")
    tier=$(echo "$verdict" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tier"])')
    if { [ "$tier" = low ] || [ "$MERGE_POLICY" = all ]; } && timeout 30m gh pr checks "$pr" --repo "$GH_REPO" --watch --fail-fast >/dev/null 2>&1 \
        && gh pr merge "$pr" --repo "$GH_REPO" --rebase --delete-branch >/dev/null; then
        final=merged
        notify "merged PR #$pr" "Merged to trunk after green CI (gate tier $tier, policy $MERGE_POLICY): https://github.com/$GH_REPO/pull/$pr"
        redeploy_preprod
    else
        final=review
        gh pr edit "$pr" --repo "$GH_REPO" --add-label awaiting-preprod >/dev/null 2>&1 \
            || { gh label create awaiting-preprod --repo "$GH_REPO" --force >/dev/null; gh pr edit "$pr" --repo "$GH_REPO" --add-label awaiting-preprod >/dev/null; }
        gh pr comment "$pr" --repo "$GH_REPO" --body "Risk gate: \`$tier\`
\`\`\`json
$verdict
\`\`\`" >/dev/null
        [ "$MERGE_POLICY" = all ] && notify "PR #$pr not merged: CI not green" \
            "Left open for you; it merges on a later tick if CI turns green: https://github.com/$GH_REPO/pull/$pr"
    fi
    python3 - "$result" "$final" <<'EOF'
import json, sys
path, final = sys.argv[1], sys.argv[2]
data = json.load(open(path)); data["status"] = final
json.dump(data, open(path, "w"))
EOF
fi

new=$("${Q[@]}" finish "$slug" "$result")
say "$slug: -> $new"
case "$new" in
    done|awaiting-preprod)
        git -C "$AGENT_REPO" worktree remove --force "$wt" ;;
    blocked)
        notify "blocked after $("${Q[@]}" get "$slug" attempts) attempt(s)" "Log: $log (on the VPS). Last result: $(cat "$result" 2>/dev/null || echo none)" ;;
esac

# Cap the Docker build cache: each `validate.sh --full` rebuilds strelitzia-validate, and a busted dependency layer
# adds ~750 MB, so a chain of runs could fill the disk. Recent layers stay, so rebuilds stay fast.
docker builder prune -f --keep-storage "${AGENT_BUILD_CACHE_KEEP:-3GB}" >> "$STATE/runs.log" 2>&1 || true
"$HERE/preprod.sh" tick >> "$STATE/runs.log" 2>&1
exit 0
