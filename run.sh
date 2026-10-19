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
usage_gate() {  # exits unless a Claude run may start now
    if [ -n "$DRY" ]; then
        say "dry run: $key is due; would check session usage"
        return 0
    fi
    local gate
    gate=$(python3 "$HERE/usage.py" check 2>&1)
    case $? in
        0) ;;
        10) "${Q[@]}" pause "$(echo "$gate" | tail -n1)" >> "$STATE/runs.log"
            say "$key: not started, $(echo "$gate" | head -n1); queue paused until the window resets"
            exit 0 ;;
        *) say "$key: not started, $(echo "$gate" | head -n1)"
           exit 0 ;;
    esac
}

# load_item: the item's repo, its config and the plan, all from origin/<base>. Blocks the item if any is missing or refused.
load_item() {
    repo=${key%%/*}
    slug=${key#*/}
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
}
cfg() { "${CFG[@]}" get "$toml" "$1" 2>/dev/null; }

merge_pr() {  # merge_pr <pr>: green CI, then merge with the repo's method. 1 = CI red or timed out, 2 = no checks, 3 = merge failed
    local checks
    if ! checks=$(timeout 30m gh pr checks "$1" --repo "$GH_REPO" --watch --fail-fast 2>&1); then
        [[ $checks == *"no checks reported"* ]] && return 2
        return 1
    fi
    gh pr merge "$1" --repo "$GH_REPO" "--$MERGE" --delete-branch >/dev/null 2>&1 || return 3
}
merge_failure() {  # merge_failure <merge_pr exit code>: why it didn't merge, for a notice
    case $1 in
        2) echo "no CI checks on this repo, so it isn't merged without them" ;;
        3) echo "CI is green but gh pr merge failed" ;;
        *) echo "CI is red or timed out" ;;
    esac
}

# 3a. Preprod stage (design D3): an approved `review` PR now on preprod gets its Human test steps run by the validator.
STORAGE_STATE=$HOME/.config/agent-runner/preprod-storage-state.json  # the dev test account's session (ROADMAP AM3)
preprod_stage() {
    local pr url host base_url netrc_env netrc signed tmpd pwt presult plog vsettings vprompt verdict summary left tries
    pr=$("${Q[@]}" get "$key" pr)
    url=$(cfg preprod.health | head -n1 | cut -f1)
    host=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.argv[1]).hostname or "")' "$url")
    base_url=$(python3 -c 'import sys, urllib.parse; u = urllib.parse.urlsplit(sys.argv[1]); print(f"{u.scheme}://{u.netloc}/")' "$url")
    netrc_env=$(cfg preprod.netrc_env)
    netrc=${!netrc_env:-}
    signed=false
    [ -f "$STORAGE_STATE" ] && signed=true
    dry "run validator (preprod) for $key PR #$pr signed_in=$signed"
    usage_gate
    tmpd=$(mktemp -d)
    pwt=$STATE/worktrees/$repo/preprod-$pr
    presult=$STATE/results/$repo/$slug.preprod.json
    plog=$STATE/logs/$(date -u +%Y%m%dT%H%MZ)-$repo-$slug.preprod.log
    mkdir -p "$(dirname "$pwt")" "$(dirname "$presult")"
    rm -f "$presult"
    # The validator settings deny reading ~/.config/agent-runner, so its curl gets a private copy of the netrc in $tmpd.
    if [ -n "$netrc" ] && [ -f "$netrc" ]; then
        install -m 600 "$netrc" "$tmpd/preprod.netrc"
        netrc=$tmpd/preprod.netrc
    fi
    signed=$(python3 "$HERE/mcp_config.py" write "$tmpd/mcp.json" ${netrc:+--netrc "$netrc"} --host "$host" \
        --storage-state "$STORAGE_STATE" | sed -n 's/^signed_in=//p')
    if ! { git -C "$AGENT_REPO" fetch -q origin "pull/$pr/head" && git -C "$AGENT_REPO" worktree add -q --detach "$pwt" FETCH_HEAD; }; then
        rm -rf "$tmpd"
        say "$key: can't check out PR #$pr for the preprod check"
        return 0
    fi
    vsettings=$pwt/agents/settings/validate.json
    [ -f "$vsettings" ] || vsettings=$HERE/settings/validate.json
    python3 -c 'import json, sys
s = json.load(open(sys.argv[1])); s["permissions"]["allow"] += ["mcp__playwright", "Bash(curl:*)", "Bash(gh pr comment:*)"]
json.dump(s, open(sys.argv[2], "w"))' "$vsettings" "$tmpd/settings.json"
    vprompt=$(python3 -c 'import sys; print(open(sys.argv[1]).read().format(repo=sys.argv[2], pr=sys.argv[3], plan_path=sys.argv[4],
base_url=sys.argv[5], result_file=sys.argv[6], signed_in=sys.argv[7]))' \
        "$HERE/prompts/preprod-check.md" "$GH_REPO" "$pr" "$plan" "$base_url" "$presult" "$signed")
    say "$key: validator (preprod) start for PR #$pr, signed_in=$signed (log $plog)"
    (
        cd "$pwt" || exit 1
        PREPROD_NETRC=$netrc AGENT_SLUG=$key AGENT_RESULT_FILE=$presult timeout --kill-after=2m 45m claude -p "$vprompt" \
            --model claude-sonnet-5-5 --settings "$tmpd/settings.json" --mcp-config "$tmpd/mcp.json" \
            --permission-mode dontAsk --output-format json
    ) >> "$plog" 2>&1
    say "$key: validator (preprod) exit $?"
    rm -rf "$tmpd"  # it holds the basic-auth password
    git -C "$AGENT_REPO" worktree remove --force "$pwt"
    field() { python3 -c 'import json,sys; v = json.load(open(sys.argv[1])).get(sys.argv[2], ""); print("\n".join(f"- {x}" for x in v) if isinstance(v, list) else v)' "$presult" "$1" 2>/dev/null; }
    verdict=$(field verdict)
    summary=$(field summary)
    if [ -z "$verdict" ]; then  # no result: a failed validator attempt
        tries=$(( $("${Q[@]}" get "$key" preprod_attempts 2>/dev/null || echo 0) + 1 ))
        "${Q[@]}" set "$key" "preprod_attempts=$tries"
        say "$key: preprod validator wrote no verdict (attempt $tries)"
        [ "$tries" -lt "${AGENT_MAX_ATTEMPTS:-2}" ] && return 0
        verdict=blocked
        summary="The preprod validator wrote no verdict $tries times. Log: $plog (on the VPS)."
    fi
    "${Q[@]}" set "$key" "preprod_verdict=$verdict"
    case $verdict in
        approve)
            left=$(field left_for_you)
            if merge_pr "$pr"; then
                "$HERE/preprod.sh" release "$repo" >> "$STATE/runs.log" 2>&1
                "${Q[@]}" set "$key" status=done
                say "$key: preprod checks passed, PR #$pr merged"
                notify "merged; $(printf '%s' "$left" | grep -c '^- ') steps left for you" \
                    "https://github.com/$GH_REPO/pull/$pr passed review and its preprod checks and is merged.
Left for you:
${left:-nothing left}"
            else
                notify "PR #$pr passed preprod but not merged" "CI isn't green (or the merge failed): https://github.com/$GH_REPO/pull/$pr. It stays on preprod."
            fi ;;
        changes)
            "$HERE/preprod.sh" release "$repo" >> "$STATE/runs.log" 2>&1
            "${Q[@]}" requeue "$key" --note "Preprod checks of PR #$pr failed: $summary
Failed steps:
$(field failed)" >/dev/null
            "${Q[@]}" unset "$key" preprod_verdict preprod_attempts review_attempts
            say "$key: preprod checks failed, back to the implementer"
            [ "$("${Q[@]}" get "$key" status)" = blocked ] && notify "blocked: preprod checks still failing" \
                "PR https://github.com/$GH_REPO/pull/$pr, out of attempts. $summary" ;;
        *)
            "$HERE/preprod.sh" release "$repo" >> "$STATE/runs.log" 2>&1
            "${Q[@]}" set "$key" status=blocked
            notify "preprod validator blocked on PR #$pr" "$summary https://github.com/$GH_REPO/pull/$pr" ;;
    esac
}

if key=$("${Q[@]}" oldest on-preprod --without preprod_verdict); then
    load_item
    preprod_stage
    exit 0
fi

# 3b. Review stage (design D3): a `review` PR the implementer called ready gets a fresh-context validator run first.
review_stage() {
    local pr rwt rresult rlog vsettings vprompt verdict summary tries
    pr=$("${Q[@]}" get "$key" pr)
    dry "run validator (review) for $key PR #$pr"
    usage_gate
    rwt=$STATE/worktrees/$repo/review-$pr
    rresult=$STATE/results/$repo/$slug.review.json
    rlog=$STATE/logs/$(date -u +%Y%m%dT%H%MZ)-$repo-$slug.review.log
    mkdir -p "$(dirname "$rwt")" "$(dirname "$rresult")"
    rm -f "$rresult"
    git -C "$AGENT_REPO" fetch -q origin "pull/$pr/head" && git -C "$AGENT_REPO" worktree add -q --detach "$rwt" FETCH_HEAD \
        || { say "$key: can't check out PR #$pr for review"; return 0; }
    vsettings=$rwt/agents/settings/validate.json
    [ -f "$vsettings" ] || vsettings=$HERE/settings/validate.json
    vprompt=$(python3 -c 'import sys; print(open(sys.argv[1]).read().format(repo=sys.argv[2], pr=sys.argv[3], plan_path=sys.argv[4],
result_file=sys.argv[5], validate_full=sys.argv[6]))' "$HERE/prompts/review-pr.md" "$GH_REPO" "$pr" "$plan" "$rresult" "$VALIDATE_FULL")
    say "$key: validator (review) start for PR #$pr (log $rlog)"
    (
        cd "$rwt" || exit 1
        AGENT_SLUG=$key AGENT_RESULT_FILE=$rresult timeout --kill-after=2m 1h claude -p "$vprompt" --model claude-opus-5-5 --settings "$vsettings" \
            --permission-mode dontAsk --output-format json
    ) >> "$rlog" 2>&1
    say "$key: validator exit $?"
    git -C "$AGENT_REPO" worktree remove --force "$rwt"
    verdict=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("verdict",""))' "$rresult" 2>/dev/null)
    summary=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("summary",""))' "$rresult" 2>/dev/null)
    case $verdict in
        changes)
            say "$key: validator asks for changes on PR #$pr"
            "${Q[@]}" requeue "$key" --note "Validator review of PR #$pr asks for changes: $summary
Findings: $(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])).get("findings",[])))' "$rresult")" >/dev/null
            [ "$("${Q[@]}" get "$key" status)" = blocked ] && notify "blocked: validator still asks for changes" \
                "PR https://github.com/$GH_REPO/pull/$pr, out of attempts. $summary" ;;
        approve)
            if [ -n "$(cfg preprod.deploy)" ]; then
                "${Q[@]}" set "$key" status=awaiting-preprod
                gh pr edit "$pr" --repo "$GH_REPO" --add-label awaiting-preprod >/dev/null 2>&1
                say "$key: validator approved PR #$pr, awaiting preprod"
            elif merge_pr "$pr"; then
                "${Q[@]}" set "$key" status=done
                say "$key: validator approved PR #$pr, merged"
                notify "merged PR #$pr after review" "The validator approved it and CI is green: https://github.com/$GH_REPO/pull/$pr"
            else
                "${Q[@]}" set "$key" status=blocked
                notify "PR #$pr approved but not merged" "CI isn't green (or the merge failed): https://github.com/$GH_REPO/pull/$pr"
            fi ;;
        blocked)
            "${Q[@]}" set "$key" status=blocked
            notify "validator blocked on PR #$pr" "$summary https://github.com/$GH_REPO/pull/$pr" ;;
        *)  # no result: a failed validator attempt
            tries=$(( $("${Q[@]}" get "$key" review_attempts 2>/dev/null || echo 0) + 1 ))
            "${Q[@]}" set "$key" "review_attempts=$tries"
            say "$key: validator wrote no verdict (attempt $tries)"
            if [ "$tries" -ge "${AGENT_MAX_ATTEMPTS:-2}" ]; then
                "${Q[@]}" set "$key" status=blocked
                notify "validator failed $tries times on PR #$pr" "Log: $rlog (on the VPS)."
            fi ;;
    esac
}

if key=$("${Q[@]}" oldest awaiting-review); then
    load_item
    review_stage
    exit 0
fi

# 4. Next item for the implementer
key=$("${Q[@]}" next) || { [ -n "$DRY" ] && say "dry run: nothing due"; exit 0; }
load_item
usage_gate

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
# The MCP config is optional: a repo without one (agent-runner itself) runs with no MCP servers, since claude refuses a missing file.
mcp_args=()
for mcp in "$wt/agents/mcp.json" "$wt/scripts/agent/mcp.json"; do
    [ -f "$mcp" ] && { mcp_args=(--mcp-config "$mcp"); break; }
done

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
            "${mcp_args[@]}" \
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
    if [ "$tier" = review ]; then
        final=needs-review  # the validator reviews it on a later tick (review stage above)
        gh pr comment "$pr" --repo "$GH_REPO" --body "Risk gate: \`review\`, so a validator reviews it next.
\`\`\`json
$verdict
\`\`\`" >/dev/null
    elif merge_pr "$pr"; then
        final=merged
        notify "merged PR #$pr" "Merged to $BASE after green CI (gate tier low): https://github.com/$GH_REPO/pull/$pr"
        # Only if this PR itself held preprod: put preprod back on the base.
        [ "$(cut -f1,2 "$STATE/preprod.lock" 2>/dev/null)" = "$repo"$'\t'"$pr" ] && "$HERE/preprod.sh" release "$repo" >> "$STATE/runs.log" 2>&1
    else
        # Not merged (gate tier low): the PR stays open, labelled ci-failed, and the item is blocked. Preprod is only
        # for `review` PRs the validator approved, so a repo without [preprod] can't get stuck waiting for it.
        merge_rc=$?
        not_merged=$(merge_failure "$merge_rc")
        final=blocked
        gh pr edit "$pr" --repo "$GH_REPO" --add-label ci-failed >/dev/null 2>&1 \
            || { gh label create ci-failed --repo "$GH_REPO" --force >/dev/null; gh pr edit "$pr" --repo "$GH_REPO" --add-label ci-failed >/dev/null; }
        gh pr comment "$pr" --repo "$GH_REPO" --body "Not merged: $not_merged. Risk gate: \`$tier\`
\`\`\`json
$verdict
\`\`\`" >/dev/null
    fi
    python3 - "$result" "$final" "${not_merged:-}" <<'EOF'
import json, sys
path, final, not_merged = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(path)); data["status"] = final
if not_merged:
    data["summary"] = f"PR #{data['pr']} not merged: {not_merged}"
json.dump(data, open(path, "w"))
EOF
fi

new=$("${Q[@]}" finish "$key" "$result")
say "$key: -> $new"
case "$new" in
    done|awaiting-preprod|awaiting-review)
        git -C "$AGENT_REPO" worktree remove --force "$wt" ;;
    blocked)
        if [ -n "${not_merged:-}" ]; then
            notify "PR #$pr not merged: $not_merged" "https://github.com/$GH_REPO/pull/$pr stays open, labelled ci-failed. Fix it and merge by hand, or re-queue $key. Log: $log (on the VPS)."
        else
            notify "blocked after $("${Q[@]}" get "$key" attempts) attempt(s)" "Log: $log (on the VPS). Last result: $(cat "$result" 2>/dev/null || echo none)"
        fi ;;
esac

# Cap the Docker build cache: a full validation can rebuild an image, and a busted dependency layer adds ~750 MB, so a
# chain of runs could fill the disk. Recent layers stay, so rebuilds stay fast.
docker builder prune -f --keep-storage "${AGENT_BUILD_CACHE_KEEP:-3GB}" >> "$STATE/runs.log" 2>&1 || true
"$HERE/preprod.sh" tick >> "$STATE/runs.log" 2>&1
exit 0
