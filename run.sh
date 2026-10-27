#!/usr/bin/env bash
# Cron entry point (the manager) for unattended builds of every registered repo. After the cutover, in the `dev` crontab:
#   */5 * * * * $HOME/.local/bin/agent-runner run
# Everything before `claude -p` is plain bash/Python, so an empty queue costs no Claude usage.
#
# 1. Upkeep: pick up your inbox replies, close notices of finished items, tag merged PRs (versions.py), move and merge
#    components.toml pins (pins.py), release/advance the global preprod lock (preprod.sh tick).
# 2. Health gate: while disk is critical (health.py gate), no Claude run starts; upkeep above still ran.
#    Then one stage with work to do: preprod checks, validator review, or the resolver (one Opus try on an item that
#    would have been blocked: `awaiting-resolve`), before any new implementer run.
# 3. `buildqueue.py next` picks a due `<repo>/<slug>` (honours pause, optional daily cap, attempt limit) or we exit.
#    Then usage.py checks Claude usage (one tiny Haiku probe; 5-hour and 7-day windows) against the session cap, daytime
#    reserve, weekly cap and weekly pacing (`agent-runner usage status`); when one refuses, the queue pauses as long as it says.
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
# CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`), AGENT_INBOX_REPO, NTFY_TOPIC: ~/.config/agent-runner/env.
env_file=$("${CFG[@]}" env-file) && . "$env_file"

STATE=${AGENT_RUNNER_STATE:-$HOME/.local/state/agent-runner}
MAX_RUNTIME=${AGENT_MAX_RUNTIME:-3h}
DRY=${AGENT_DRY_RUN:-}
# Opus has a 1M-token window, so long runs never auto-compacted and re-sent 250-380k tokens of context every turn.
# Compact at 200k instead (AGENT_COMPACT_WINDOW in the env file overrides it).
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=${AGENT_COMPACT_WINDOW:-200000}
export AGENT_RUNNER_STATE=$STATE
mkdir -p "$STATE/logs" "$STATE/results" "$STATE/worktrees" "$STATE/configs"
Q=(python3 "$HERE/buildqueue.py")
stamp() { date -u +%FT%TZ; }
say() { echo "$(stamp) $*" >> "$STATE/runs.log"; }
# cost <log> <role> <model> <bytes>: a `cost` line in runs.log for the Claude run that wrote <log> past its first <bytes>
# (`$(size <log>)` before the run; the logs are appended to): turns, tokens, API-equivalent USD. None if it wrote no result.
size() { stat -c %s "$1" 2>/dev/null || echo 0; }
cost() {
    local line
    line=$(python3 "$HERE/usage.py" cost "$1" --role "$2" --key "$key" --model "$3" --after "$4" 2>/dev/null) \
        && [ -n "$line" ] && say "$line"
    return 0
}
dry() {  # dry <what>: in a dry run, log "would <what>" and stop here
    [ -n "$DRY" ] || return 0
    say "dry run: would $*"
    exit 0
}

exec 9>"$STATE/run.lock"
flock -n 9 || exit 0

# An unreadable queue (e.g. the old runner's bare keys) would otherwise read as "nothing due".
if ! qerr=$("${Q[@]}" list 2>&1 >/dev/null); then
    say "queue unreadable, no runs: $qerr"
    echo "$qerr" >&2
    exit 1
fi

# versions_sync <repo>: tag the PRs merged since the newest tag and move the minor's pre-release (versions.py; it exits 4,
# quietly, for a repo without [release]). A failure or exit 3 (waiting for the user's promote) sends one notice per cause
# ($STATE/versions/<repo>.notice holds the last one); it never stops the tick or a preprod redeploy.
versions_sync() {
    local out rc cause seen=$STATE/versions/$1.notice what
    out=$(python3 "$HERE/versions.py" sync "$1" 2>&1)
    rc=$?
    case $rc in
        0|4) [ -n "$out" ] && [ "$rc" = 0 ] && say "versions $1: ${out//$'\n'/ · }"
             rm -f "$seen"
             return 0 ;;
    esac
    cause=$(printf '%s\n' "$out" | tail -n1)
    [ -n "$cause" ] || cause="exit $rc"
    [ -f "$seen" ] && [ "$(cat "$seen")" = "$cause" ] && return 0  # logged and notified when it first happened
    say "versions $1: exit $rc${out:+: ${out//$'\n'/ · }}"
    [ -n "${AGENT_INBOX_REPO:-}" ] || return 0
    what="failed"
    [ "$rc" = 3 ] && what="waits for a promote"
    mkdir -p "$STATE/versions"
    python3 "$HERE/inbox.py" notify --project "$1" --title "versions sync for $1 $what" --body "\`agent-runner versions sync $1\` exited $rc:

$cause

Merged PRs are tagged on the next tick after this is fixed. \`agent-runner versions status $1\` shows the tags and releases." \
        >> "$STATE/runs.log" 2>&1 && printf '%s\n' "$cause" > "$seen"
    return 0
}

# preprod_follow <repo> <pr>: after merging <pr> in <repo>, tag it (with [release]) and put preprod on <repo>'s new base,
# unless another PR holds the lock (its release does it). Reads $STATE/configs/<repo>.toml (load_item or repo_toml wrote it).
preprod_follow() {
    local repo=$1 pr=$2 rtoml=$STATE/configs/$1.toml who=${key:-pins} holder rbase rgh title body
    [ "$("${CFG[@]}" get "$rtoml" release.tags 2>/dev/null)" = True ] && versions_sync "$repo"  # the tag first: preprod shows it
    [ -n "$("${CFG[@]}" get "$rtoml" preprod.deploy 2>/dev/null)" ] || return 0
    rbase=$("${CFG[@]}" get "$rtoml" repo.base 2>/dev/null)
    rgh=$("${CFG[@]}" get "$rtoml" repo.github 2>/dev/null)
    holder=$(cut -f1,2 "$STATE/preprod.lock" 2>/dev/null)
    if [ -n "$holder" ] && [ "$holder" != "$repo"$'\t'"$pr" ]; then
        say "$who: preprod is held by ${holder/$'\t'/#}, so it stays; its release puts $repo's $rbase back"
    elif "$HERE/preprod.sh" release "$repo" >> "$STATE/runs.log" 2>&1; then
        say "$who: preprod redeployed on $repo's $rbase after PR #$pr"
    else
        say "$who: preprod redeploy of $repo's $rbase failed after PR #$pr"
        title="preprod redeploy failed after PR #$pr"
        body="PR https://github.com/$rgh/pull/$pr is merged, but putting preprod on $repo's $rbase failed. See runs.log (on the VPS), then run \`agent-runner preprod release $repo\`."
        if [ -n "${key:-}" ]; then
            notify "$title" "$body"
        elif [ -n "${AGENT_INBOX_REPO:-}" ]; then
            python3 "$HERE/inbox.py" notify --project "$repo" --title "$title" --body "$body" >> "$STATE/runs.log" 2>&1
        fi
    fi
    return 0
}
# repo_toml <repo>: its agents/runner.toml from origin/HEAD (as load_item reads it) to $STATE/configs/<repo>.toml.
repo_toml() (
    unset AGENT_REPO AGENT_BASE
    eval "$("${CFG[@]}" repo-env "$1")"
    [ -n "${AGENT_REPO:-}" ] && [ -d "$AGENT_REPO" ] || exit 1
    ref=$(git -C "$AGENT_REPO" rev-parse --abbrev-ref origin/HEAD 2>/dev/null) || ref=origin/${AGENT_BASE:-main}
    git -C "$AGENT_REPO" show "$ref:agents/runner.toml" > "$STATE/configs/$1.toml" 2>/dev/null
)

# pins_upkeep: pins.py sync opens or moves the "Pin <child> <tag>" PRs of every parent repo (components.toml), then
# pins.py tick merges the green ones with no review. After each `merged <parent>#<n> …` line, the parent is tagged and
# preprod follows its new base (preprod_follow). Each failure is one runs.log line; it never stops the tick.
pins_upkeep() {
    local cmd out rc line parent pr
    local -A merged=()  # parent → its last merged pin PR: one redeploy per parent, after all its merges
    for cmd in sync tick; do
        out=$(python3 "$HERE/pins.py" "$cmd" 2>&1)
        rc=$?
        while IFS= read -r line <&3; do
            [ -n "$line" ] && say "pins: $line"
            [ "$cmd" = tick ] && [[ $line =~ ^merged\ ([a-z0-9-]+)#([0-9]+)\  ]] || continue
            merged[${BASH_REMATCH[1]}]=${BASH_REMATCH[2]}
        done 3<<< "$out"
        [ "$rc" = 0 ] || say "pins $cmd: exit $rc"
    done
    for parent in "${!merged[@]}"; do
        pr=${merged[$parent]}
        if repo_toml "$parent"; then
            preprod_follow "$parent" "$pr"
        else
            say "pins: can't read $parent's runner.toml, so preprod doesn't follow its PR #$pr"
        fi
    done
    return 0
}

# 1. Upkeep
if [ -n "$DRY" ]; then
    say "dry run: would poll the inbox, sync versions, sync pins and tick preprod"
    python3 "$HERE/intake.py" scan --dry-run >> "$STATE/runs.log" 2>&1 || true  # writes nothing
    python3 "$HERE/prcomments.py" scan --dry-run >> "$STATE/runs.log" 2>&1 || true
else
    [ -n "${AGENT_INBOX_REPO:-}" ] && python3 "$HERE/inbox.py" poll >> "$STATE/runs.log" 2>&1
    [ -n "${AGENT_INBOX_REPO:-}" ] && python3 "$HERE/inbox.py" sweep >> "$STATE/runs.log" 2>&1
    # Plans pushed with `**Queue:** yes` (opted-in repos), before the next-item pick so they can start this tick.
    python3 "$HERE/intake.py" scan >> "$STATE/runs.log" 2>&1 || true
    # The user's `/fix` PR comments send a PR back to the implementer, before preprod.sh tick and the next-item pick.
    python3 "$HERE/prcomments.py" scan >> "$STATE/runs.log" 2>&1 || true
    # Tags for PRs merged outside the runner too (by hand in the GitHub app), before prstatus.py shows them.
    for repo_file in "$HOME"/.config/agent-runner/repos/*.env; do
        [ -e "$repo_file" ] && versions_sync "$(basename "$repo_file" .env)"
    done
    pins_upkeep  # after the versions loop: a merged pin PR is tagged and preprod follows, before preprod.sh tick
    "$HERE/preprod.sh" tick >> "$STATE/runs.log" 2>&1
fi
# Every runner PR's status block, from queue state (catches up PRs a missed update left stale). A dry run only logs.
python3 "$HERE/prstatus.py" sync ${DRY:+--dry-run} >> "$STATE/runs.log" 2>&1

# 2. Health gate
if ! python3 "$HERE/health.py" gate >/dev/null 2>&1; then
    say "disk critical, no runs (health.py gate)"
    exit 0
fi

# notify <title> <body>: an agent-notice issue labelled with the repo. Its URL lands in $notice_url and in the item's
# `notice` field, which the PR status block links while the item is blocked (`issue` stays the agent-question issue).
notify() {
    notice_url=
    [ -n "$DRY" ] && { say "dry run: would notify: $1"; return 0; }
    [ -n "${AGENT_INBOX_REPO:-}" ] || return 0
    notice_url=$(python3 "$HERE/inbox.py" notify --slug "$key" --project "$repo" --title "$1" --body "$2" | tail -n1)
    [ -n "$notice_url" ] && "${Q[@]}" set "$key" "notice=$notice_url"
    return 0
}
# pr_status: refresh the item's PR status block (prstatus.py), right after a transition. Never fails the tick.
pr_status() {
    python3 "$HERE/prstatus.py" update "$key" ${DRY:+--dry-run} >> "$STATE/runs.log" 2>&1 || say "$key: prstatus update failed"
}
block() {  # block <log line> <notice title> <notice body>
    say "$key: $1, blocked"
    notify "$2" "$3"
    [ -n "$DRY" ] && exit 0
    "${Q[@]}" set "$key" status=blocked
    pr_status
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
            say "$key: not started, $(echo "$gate" | head -n1); queue paused $(echo "$gate" | tail -n1)h"
            exit 0 ;;
        *) say "$key: not started, $(echo "$gate" | head -n1)"
           exit 0 ;;
    esac
}

# limit_pause: after a run stopped at the usage limit, pause the queue for as long as the usage gate says (a weekly-limit
# stop waits for the weekly reset), or 5 h if the gate allows a start or can't tell.
limit_pause() {
    local gate hours
    gate=$(python3 "$HERE/usage.py" check 2>&1)
    if [ $? = 10 ]; then
        hours=$(echo "$gate" | tail -n1)
        "${Q[@]}" pause "$hours" >> "$STATE/runs.log"
        say "$key: usage limit hit, $(echo "$gate" | head -n1); queue paused ${hours}h"
    else
        "${Q[@]}" pause 5 >> "$STATE/runs.log"
        say "$key: usage limit hit, queue paused 5h (usage gate: $(echo "$gate" | head -n1))"
    fi
}

# load_item: the item's repo, its config and the plan, all from origin/<base>. Blocks the item if any is missing or refused.
load_item() {
    repo=${key%%/*}
    slug=${key#*/}
    unset AGENT_REPO
    repo_env=$("${CFG[@]}" repo-env "$repo") && eval "$repo_env"
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
# pr_plan <worktree>: the plan's path in a PR checkout. A finished plan moves to $PLANS/done/ in its own PR.
pr_plan() {
    if [ -f "$1/$plan" ]; then echo "$plan"
    elif [ -f "$1/$PLANS/done/$slug.md" ]; then echo "$PLANS/done/$slug.md"
    else echo "$plan"; fi
}

# hold_or_notify <title> <body> <stage>: where the item would be blocked. `buildqueue.py hold` gives the resolver one try
# first (status awaiting-resolve; the resolve stage runs it on a later tick, and the user hears nothing yet). Once the
# resolver has tried (or AGENT_RESOLVE=0), the item is blocked with the notice, plus the resolver's diagnosis if it had one.
hold_or_notify() {
    local title=$1 body=$2 stage=$3 diagnosis class rlog
    if [ -n "$DRY" ]; then
        say "dry run: would hold $key for the resolver: $title"
        return 0
    fi
    if [ "$("${Q[@]}" hold "$key" --title "$title" --body "$body" --stage "$stage")" = awaiting-resolve ]; then
        say "$key: held for the resolver ($title)"
        return 0
    fi
    say "$key: $title, blocked"
    diagnosis=$("${Q[@]}" get "$key" resolve_diagnosis 2>/dev/null)
    if [ -n "$diagnosis" ]; then
        class=$("${Q[@]}" get "$key" resolve_class 2>/dev/null)
        rlog=$("${Q[@]}" get "$key" resolve_log 2>/dev/null)
        body="$body

Resolver (tried once, class \`${class:-unknown}\`): $diagnosis
Resolver log: ${rlog:-none} (on the VPS)."
    fi
    notify "$title" "$body"
}

# fix_blocked <pr> <text> <stage>: `requeue --fix` blocked the item (fix rounds used up, or the same findings came back
# unfixed), so it is held for the resolver.
fix_blocked() {
    local why unresolved title
    why=$("${Q[@]}" get "$key" blocked_why 2>/dev/null)
    unresolved=$("${Q[@]}" get "$key" last_unresolved 2>/dev/null)
    if [ "$why" = stuck ]; then
        title="blocked: the same findings came back unfixed"
    else
        title="blocked: validator still asks for changes after $("${Q[@]}" get "$key" fix_rounds 2>/dev/null || echo 0) rounds"
    fi
    say "$key: $title (PR #$1)"
    hold_or_notify "$title" "PR https://github.com/$GH_REPO/pull/$1. $2
Unresolved: ${unresolved:-[]}" "$3"
}

# merge_pr <pr>: green CI, then merge with the repo's method. 1 = CI red or timed out, 2 = no checks, 3 = merge failed,
# 4 = conflicts with $BASE (files in $conflict_files). GitHub runs no CI on a conflicting PR, so that comes first: the base
# is merged into the PR when it merges cleanly, and the CI wait is then on the new head.
merge_pr() {
    local checks rc
    conflict_files=
    if [ "$(pr_mergeable "$1")" = CONFLICTING ]; then
        sync_base "$1"
        [ $? = 1 ] && return 4
    fi
    if ! checks=$(timeout 30m gh pr checks "$1" --repo "$GH_REPO" --watch --fail-fast 2>&1); then
        if [[ $checks == *"Resource not accessible"* ]]; then
            ci_runs "$1"
            rc=$?
            [ "$rc" = 2 ] && [ "$(pr_mergeable "$1")" = CONFLICTING ] && return 4
            [ "$rc" = 0 ] || return "$rc"
        else
            if [[ $checks == *"no checks reported"* ]]; then
                [ "$(pr_mergeable "$1")" = CONFLICTING ] && return 4
                return 2
            fi
            return 1
        fi
    fi
    gh pr merge "$1" --repo "$GH_REPO" "--$MERGE" --delete-branch >/dev/null 2>&1 || return 3
}
# pr_mergeable <pr>: MERGEABLE, CONFLICTING or UNKNOWN. GitHub computes it lazily, so UNKNOWN is polled 6 times, 10 s apart.
pr_mergeable() {
    local m i
    for i in 1 2 3 4 5 6; do
        m=$(gh pr view "$1" --repo "$GH_REPO" --json mergeable -q .mergeable 2>/dev/null)
        case $m in MERGEABLE|CONFLICTING) echo "$m"; return 0 ;; esac
        [ "$i" -lt 6 ] && sleep "${AGENT_MERGEABLE_POLL:-10}"
    done
    echo UNKNOWN
}
# sync_base <pr>: merge origin/$BASE into the PR branch in a temp worktree. 0 = merged cleanly and pushed (a normal push),
# 1 = real conflicts (the files in $conflict_files, the merge aborted), 2 = couldn't try (logged; the CI wait goes on as before).
sync_base() {
    local head swt rc
    conflict_files=
    head=$(gh pr view "$1" --repo "$GH_REPO" --json headRefName -q .headRefName 2>/dev/null)
    [ -n "$head" ] || { say "$key: can't read PR #$1's branch to merge $BASE into it"; return 2; }
    swt=$STATE/worktrees/$repo/sync-$1
    mkdir -p "$(dirname "$swt")"
    git -C "$AGENT_REPO" worktree remove --force "$swt" 2>/dev/null
    if ! { git -C "$AGENT_REPO" fetch -q origin && git -C "$AGENT_REPO" worktree add -q --detach "$swt" "origin/$head"; } \
            >> "$STATE/runs.log" 2>&1; then
        say "$key: can't check out PR #$1's branch $head to merge $BASE into it"
        return 2
    fi
    if git -C "$swt" merge -q --no-edit "origin/$BASE" >> "$STATE/runs.log" 2>&1; then
        if git -C "$swt" push -q origin "HEAD:refs/heads/$head" >> "$STATE/runs.log" 2>&1; then
            say "$key: merged $BASE into PR #$1 (clean), CI re-runs"
            sleep "${AGENT_CI_SETTLE:-30}"  # let the new head's checks register before the CI wait
            rc=0
        else
            say "$key: merged $BASE into PR #$1, but the push failed"
            rc=2
        fi
    else
        conflict_files=$(git -C "$swt" diff --name-only --diff-filter=U | paste -sd, - | sed 's/,/, /g')
        git -C "$swt" merge --abort >/dev/null 2>&1
        if [ -n "$conflict_files" ]; then
            say "$key: PR #$1 conflicts with $BASE in $conflict_files"
            rc=1
        else
            say "$key: merging $BASE into PR #$1 failed (no conflicting files, see runs.log)"
            rc=2
        fi
    fi
    git -C "$AGENT_REPO" worktree remove --force "$swt" >/dev/null 2>&1
    return "$rc"
}
# conflict_back <pr>: after merge_pr returned 4 (or sync_base 1), send the item back to the implementer for a conflict round
# (no attempt used); once AGENT_MAX_CONFLICT_ROUNDS are used up it is held for the resolver (hold_or_notify). 1 = held.
conflict_back() {
    local why new
    why=$(merge_failure 4)
    new=$("${Q[@]}" requeue "$key" --conflict --note "PR #$1 $why")
    if [ "$new" = blocked ]; then
        gh pr comment "$1" --repo "$GH_REPO" --body "Not merged: $why, after ${AGENT_MAX_CONFLICT_ROUNDS:-2} conflict round(s)." >/dev/null 2>&1
        say "$key: PR #$1 $why, out of conflict rounds"
        hold_or_notify "blocked: PR #$1 $why" "https://github.com/$GH_REPO/pull/$1 still $why after ${AGENT_MAX_CONFLICT_ROUNDS:-2} conflict round(s). Merge $BASE into it by hand, or re-queue $key." merge
        return 1
    fi
    gh pr comment "$1" --repo "$GH_REPO" --body "Not merged yet: $why. Back to the implementer for a conflict round." >/dev/null 2>&1
    say "$key: PR #$1 $why, back to the implementer for a conflict round"
}
# ci_runs <pr>: CI through the Actions runs of the PR head, for a token that can't read the check rollup (agent-runner).
# Waits up to 30 min for a run to show up. 0 = all green, 1 = one failed (or gh failed), 2 = no run.
ci_runs() {
    local sha ids id waited=0
    sha=$(gh pr view "$1" --repo "$GH_REPO" --json headRefOid -q .headRefOid 2>/dev/null) && [ -n "$sha" ] || return 1
    while ids=$(gh run list --repo "$GH_REPO" --commit "$sha" --json databaseId -q '.[].databaseId' 2>/dev/null) || return 1
          [ -z "$ids" ]; do
        [ "$waited" -ge "${AGENT_CI_WAIT:-1800}" ] && return 2
        sleep 30
        waited=$((waited + 30))
    done
    for id in $ids; do
        timeout 30m gh run watch "$id" --repo "$GH_REPO" --exit-status >/dev/null 2>&1 || return 1
    done
}
merge_failure() {  # merge_failure <merge_pr exit code>: why it didn't merge, for a notice
    case $1 in
        2) echo "no CI checks on this repo, so it isn't merged without them" ;;
        3) echo "CI is green but gh pr merge failed" ;;
        4) echo "conflicts with $BASE${conflict_files:+ in $conflict_files}" ;;
        *) echo "CI is red or timed out" ;;
    esac
}

# closed_by_hand: if the item's PR was merged or closed outside the runner, finish the item (no validator) and succeed.
closed_by_hand() {
    local pr state hand result
    pr=$("${Q[@]}" get "$key" pr)
    state=$(gh pr view "$pr" --repo "$GH_REPO" --json state -q .state 2>/dev/null)
    case $state in
        MERGED) hand="merged by hand" ;;
        CLOSED) hand="closed by hand" ;;
        *) return 1 ;;
    esac
    dry "finish $key: PR #$pr was $hand"
    result=$(mktemp)  # `finish` keeps the summary in last_result; a closed PR is then cancelled below
    printf '{"status": "merged", "pr": %s, "summary": "PR #%s %s"}' "$pr" "$pr" "$hand" > "$result"
    "${Q[@]}" finish "$key" "$result" >/dev/null
    rm -f "$result"
    if [ "$state" = MERGED ]; then
        "${Q[@]}" set "$key" "merged_at=$(stamp)"
        say "$key: PR #$pr was merged by hand, done"
        preprod_follow "$repo" "$pr"
    else
        "${Q[@]}" cancel "$key"
        say "$key: PR #$pr was closed by hand, cancelled"
        [ "$(cut -f1,2 "$STATE/preprod.lock" 2>/dev/null)" = "$repo"$'\t'"$pr" ] && "$HERE/preprod.sh" release "$repo" >> "$STATE/runs.log" 2>&1
    fi
    return 0
}

# 3a. Preprod stage (design D3): an approved `review` PR now on preprod gets its Human test steps run by the validator.
STORAGE_STATE=$HOME/.config/agent-runner/preprod-storage-state.json  # the dev test account's session (ROADMAP AM3)
preprod_stage() {
    local pr url host base_url netrc_env netrc signed tmpd pwt presult plog vsettings vprompt verdict summary left tries held pstart
    local comment left_count since deployed rc
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
base_url=sys.argv[5], result_file=sys.argv[6], signed_in=sys.argv[7], netrc_file=sys.argv[8]))' \
        "$HERE/prompts/preprod-check.md" "$GH_REPO" "$pr" "$(pr_plan "$pwt")" "$base_url" "$presult" "$signed" "${netrc:-/dev/null}")
    say "$key: validator (preprod) start for PR #$pr, signed_in=$signed (log $plog)"
    since=$(stamp)
    pstart=$(size "$plog")
    (
        cd "$pwt" || exit 1
        PREPROD_NETRC=$netrc AGENT_SLUG=$key AGENT_RESULT_FILE=$presult timeout --kill-after=2m 45m claude -p "$vprompt" \
            --model claude-sonnet-5-5 --settings "$tmpd/settings.json" --mcp-config "$tmpd/mcp.json" \
            --permission-mode dontAsk --output-format json
    ) >> "$plog" 2>&1
    say "$key: validator (preprod) exit $?"
    cost "$plog" preprod claude-sonnet-5-5 "$pstart"
    rm -rf "$tmpd"  # it holds the basic-auth password
    deployed=$(git -C "$pwt" rev-parse HEAD)
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
    # The validator's checklist comment: the newest comment by the runner's account since the validator started (not the
    # user's `/fix` or prcomments.py's reply: the user posts as the same account).
    comment=$(gh pr view "$pr" --repo "$GH_REPO" --json comments \
        -q "[.comments[] | select(.viewerDidAuthor and .createdAt >= \"$since\" and (.body | test(\"^(/fix|Queued: )\") | not))] | last | .url // empty" 2>/dev/null)
    "${Q[@]}" unset "$key" preprod_comment
    [ -n "$comment" ] && "${Q[@]}" set "$key" "preprod_comment=$comment"
    "${Q[@]}" set "$key" "preprod_summary=$summary"
    # After changes or blocked the PR keeps preprod, so the user can look at it. The lock frees itself when the PR is merged
    # or closed (preprod.sh status), or when the fixed PR is deployed again over its own hold.
    held="It stays on preprod ($base_url) so you can look. Close the PR, or run \`agent-runner preprod release $repo\`, to free preprod."
    case $verdict in
        approve)
            left=$(field left_for_you)
            if merge_pr "$pr"; then
                preprod_follow "$repo" "$pr"
                left_count=$(printf '%s' "$left" | grep -c '^- ')
                "${Q[@]}" set "$key" status=done "merged_at=$(stamp)" "left_count=$left_count"
                say "$key: preprod checks passed, PR #$pr merged"
                notify "merged; $left_count steps left for you" \
                    "https://github.com/$GH_REPO/pull/$pr passed review and its preprod checks and is merged.
Left for you:
${left:-nothing left}"
                [ -n "$notice_url" ] && "${Q[@]}" set "$key" "left_issue=$notice_url"
            else
                rc=$?
                if [ "$rc" = 4 ]; then
                    # Back to the implementer: the conflict round's PR goes through the gate, review and preprod again.
                    "$HERE/preprod.sh" release "$repo" >> "$STATE/runs.log" 2>&1
                    "${Q[@]}" unset "$key" preprod_verdict preprod_attempts review_attempts review_verdict review_summary rerun reviewed_sha
                    gh pr edit "$pr" --repo "$GH_REPO" --remove-label awaiting-preprod >/dev/null 2>&1
                    conflict_back "$pr"
                else
                    hold_or_notify "PR #$pr passed preprod but not merged" "$(merge_failure "$rc"): https://github.com/$GH_REPO/pull/$pr. $held" merge
                fi
            fi ;;
        changes)
            "${Q[@]}" unset "$key" preprod_verdict preprod_attempts review_attempts
            "${Q[@]}" set "$key" rerun=preprod "reviewed_sha=$deployed"  # the next review is a re-review of the fix
            if [ "$("${Q[@]}" requeue "$key" --fix --stage preprod --note "Preprod checks of PR #$pr failed: $summary
Failed steps:
$(field failed)")" = blocked ]; then
                fix_blocked "$pr" "$summary $held" preprod
            else
                say "$key: preprod checks failed, back to the implementer (fix round)"
            fi ;;
        *)
            hold_or_notify "preprod validator blocked on PR #$pr" "$summary https://github.com/$GH_REPO/pull/$pr $held" preprod ;;
    esac
}

if key=$("${Q[@]}" oldest on-preprod --without preprod_verdict); then
    load_item
    closed_by_hand || preprod_stage
    pr_status
    exit 0
fi

# 3b. Review stage (design D3): a `review` PR the implementer called ready gets a fresh-context validator run first.
# last_round: the newest fix-round note of the item: its round, its max rounds, then its findings one per line
# (a review round's findings list, or a preprod round's failed steps). A user `/fix` note: `user`, `-`, then its text.
# Nothing when there is no round note.
last_round() {
    "${Q[@]}" get "$key" notes 2>/dev/null | python3 -c '
import json, re, sys
ALSO = "\nAlso, the user asked on the PR: "
notes = [n for n in json.load(sys.stdin) if re.match(r"(Review|Preprod) validator, round \d+/\d+|User /fix \(", n)]
if notes and notes[-1].startswith("User /fix ("):
    print("user", "-", notes[-1].split("): ", 1)[-1], sep="\n")
elif notes:
    note = notes[-1]
    m = re.match(r"(Review|Preprod) validator, round (\d+)/(\d+) \([^)]*\): ", note)
    body = note[m.end():] if m else note
    found = []
    body, _, asked = body.partition(ALSO)  # a user `/fix` made while this round was still queued (buildqueue.py)
    if "\nFindings: " in body:
        try:
            found = [str(x) for x in json.loads(body.split("\nFindings: ", 1)[1])]
        except ValueError:
            pass
    elif "\nFailed steps:\n" in body:
        found = [line.removeprefix("- ") for line in body.split("\nFailed steps:\n", 1)[1].splitlines() if line.strip()]
    if asked:
        found = (found or [body]) + [f"The user asked on the PR: {asked}"]
    r = re.match(r"\S+ validator, round (\d+)/(\d+)", note)
    print(r[1], r[2], sep="\n")
    print("\n".join(found or [body]))' 2>/dev/null
}
review_stage() {
    local pr rwt rresult rlog vsettings vprompt verdict summary tries head since round n max findings model model_name mode limit role rstart
    local label rc
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
    head=$(git -C "$rwt" rev-parse HEAD)
    since=$("${Q[@]}" get "$key" reviewed_sha 2>/dev/null)
    round=$(last_round)
    { read -r n; read -r max; findings=$(cat); } <<< "$round"
    # After a fix round, a scoped re-review (on Sonnet) of the fix since the reviewed sha, with the newest round's findings;
    # else (the first review, a force-push, no sha) the full review on Opus.
    # A user `/fix` (no round) gets the same scoped re-review of their ask.
    if { [ "$("${Q[@]}" get "$key" fix_rounds 2>/dev/null || echo 0)" -gt 0 ] || [ "$n" = user ]; } && [ -n "$since" ] \
        && [ -n "$round" ] && git -C "$rwt" merge-base --is-ancestor "$since" HEAD 2>/dev/null; then
        model=${AGENT_REREVIEW_MODEL:-claude-sonnet-5-5}
        model_name=${model#claude-}
        label="round $n of $max"
        [ "$n" = user ] && label="the user's /fix comment"
        mode="re-review, ${label/ of /\/}, ${model_name%%-[0-9]*}"
        role=rereview
        limit=30m
        vprompt=$(python3 -c 'import sys; print(open(sys.argv[1]).read().format(repo=sys.argv[2], pr=sys.argv[3], plan_path=sys.argv[4],
result_file=sys.argv[5], validate_full=sys.argv[6], since_sha=sys.argv[7], label=sys.argv[8], findings=sys.argv[9]))' \
            "$HERE/prompts/rereview-pr.md" "$GH_REPO" "$pr" "$(pr_plan "$rwt")" "$rresult" "$VALIDATE_FULL" "$since" "$label" \
            "$findings")
    else
        model=claude-opus-5-5
        mode="review, opus"
        role=review
        limit=1h
        vprompt=$(python3 -c 'import sys; print(open(sys.argv[1]).read().format(repo=sys.argv[2], pr=sys.argv[3], plan_path=sys.argv[4],
result_file=sys.argv[5], validate_full=sys.argv[6]))' "$HERE/prompts/review-pr.md" "$GH_REPO" "$pr" "$(pr_plan "$rwt")" "$rresult" "$VALIDATE_FULL")
    fi
    say "$key: validator ($mode) start for PR #$pr (log $rlog)"
    rstart=$(size "$rlog")
    (
        cd "$rwt" || exit 1
        AGENT_SLUG=$key AGENT_RESULT_FILE=$rresult timeout --kill-after=2m "$limit" claude -p "$vprompt" --model "$model" \
            --settings "$vsettings" --permission-mode dontAsk --output-format json
    ) >> "$rlog" 2>&1
    say "$key: validator exit $?"
    cost "$rlog" "$role" "$model" "$rstart"
    git -C "$AGENT_REPO" worktree remove --force "$rwt"
    verdict=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("verdict",""))' "$rresult" 2>/dev/null)
    summary=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("summary",""))' "$rresult" 2>/dev/null)
    # A list field of the review result as JSON ([] when missing, empty or not a list).
    rfield() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print(json.dumps(v if isinstance(v, list) else []))' "$rresult" "$1" 2>/dev/null || echo '[]'; }
    case $verdict in
        # reviewed_sha: what the next re-review diffs from (a run without a verdict keeps the previous one)
        changes|approve|blocked) "${Q[@]}" set "$key" "review_verdict=$verdict" "review_summary=$summary" "reviewed_sha=$head" ;;
    esac
    [ "$verdict" = approve ] && "${Q[@]}" unset "$key" rerun
    case $verdict in
        changes)
            "${Q[@]}" set "$key" rerun=review
            say "$key: validator asks for changes on PR #$pr"
            if [ "$("${Q[@]}" requeue "$key" --fix --stage review --note "Validator review of PR #$pr asks for changes: $summary
Findings: $(rfield findings)" --unresolved "$(rfield unresolved)")" = blocked ]; then
                fix_blocked "$pr" "$summary" review
            fi ;;
        approve)
            if [ -n "$(cfg preprod.deploy)" ]; then
                "${Q[@]}" set "$key" status=awaiting-preprod
                gh pr edit "$pr" --repo "$GH_REPO" --add-label awaiting-preprod >/dev/null 2>&1
                say "$key: validator approved PR #$pr, awaiting preprod"
            elif merge_pr "$pr"; then
                "${Q[@]}" set "$key" status=done "merged_at=$(stamp)"
                say "$key: validator approved PR #$pr, merged"
                preprod_follow "$repo" "$pr"
                notify "merged PR #$pr after review" "The validator approved it and CI is green: https://github.com/$GH_REPO/pull/$pr"
            else
                rc=$?
                if [ "$rc" = 4 ]; then
                    "${Q[@]}" unset "$key" review_attempts review_verdict review_summary rerun reviewed_sha
                    conflict_back "$pr"
                else
                    hold_or_notify "PR #$pr approved but not merged" "$(merge_failure "$rc"): https://github.com/$GH_REPO/pull/$pr" merge
                fi
            fi ;;
        blocked)
            hold_or_notify "validator blocked on PR #$pr" "$summary https://github.com/$GH_REPO/pull/$pr" review ;;
        *)  # no result: a failed validator attempt
            tries=$(( $("${Q[@]}" get "$key" review_attempts 2>/dev/null || echo 0) + 1 ))
            "${Q[@]}" set "$key" "review_attempts=$tries"
            say "$key: validator wrote no verdict (attempt $tries)"
            if [ "$tries" -ge "${AGENT_MAX_ATTEMPTS:-2}" ]; then
                hold_or_notify "validator failed $tries times on PR #$pr" "Log: $rlog (on the VPS)." review
            fi ;;
    esac
}

if key=$("${Q[@]}" oldest awaiting-review); then
    load_item
    closed_by_hand || review_stage
    pr_status
    exit 0
fi

# 3c. Resolve stage: the oldest item held for the resolver (hold_or_notify) gets its one try, before new implementer work.
# resolve_logs: this item's logs (`<time>-<repo>-<slug>`): the newest validator and preprod logs, the two newest implementer logs.
resolve_logs() {
    ls -t "$STATE/logs/"????????T????Z-"$repo-$slug.review.log" 2>/dev/null | head -n 1
    ls -t "$STATE/logs/"????????T????Z-"$repo-$slug.preprod.log" 2>/dev/null | head -n 1
    ls -t "$STATE/logs/"????????T????Z-"$repo-$slug.log" 2>/dev/null | head -n 2
}
# resolve_ci_log <pr> <file>: the failed Actions run of the PR head (if one failed), its failing steps' log tail in <file>.
resolve_ci_log() {
    local sha id
    sha=$(gh pr view "$1" --repo "$GH_REPO" --json headRefOid -q .headRefOid 2>/dev/null) && [ -n "$sha" ] || return 1
    id=$(gh run list --repo "$GH_REPO" --commit "$sha" --json databaseId,conclusion \
        -q '.[] | select(.conclusion == "failure") | .databaseId' 2>/dev/null | head -n1)
    [ -n "$id" ] || return 1
    gh run view "$id" --repo "$GH_REPO" --log-failed 2>/dev/null | tail -n 400 > "$2"
    [ -s "$2" ]
}
resolve_stage() {
    local pr stage title body rwt rbranch rresult rlog rstart last ci_log settings prompt code action class summary diagnosis
    local verdict tier rc
    local -a mcp=()
    pr=$("${Q[@]}" get "$key" pr 2>/dev/null)
    stage=$("${Q[@]}" get "$key" block_stage 2>/dev/null) || stage=implement  # set by hand: no hold fields
    title=$("${Q[@]}" get "$key" block_title 2>/dev/null) || title=blocked
    body=$("${Q[@]}" get "$key" block_body 2>/dev/null)
    dry "run the resolver for $key"
    usage_gate
    rbranch=agent/$slug
    rwt=$STATE/worktrees/$repo/$slug
    git -C "$AGENT_REPO" fetch -q origin
    if [ -d "$rwt" ]; then
        git -C "$rwt" merge -q --ff-only "origin/$rbranch" >/dev/null 2>&1  # catch up with fix rounds pushed since
    elif git -C "$AGENT_REPO" ls-remote --exit-code -q origin "$rbranch" >/dev/null; then
        mkdir -p "$(dirname "$rwt")"
        git -C "$AGENT_REPO" worktree add -q -B "$rbranch" "$rwt" "origin/$rbranch" >> "$STATE/runs.log" 2>&1
    fi
    "${Q[@]}" set "$key" resolve_tried=1
    if [ ! -d "$rwt" ] || [ -z "$pr" ]; then
        say "$key: nothing for the resolver to fix (no branch $rbranch or no PR)"
        hold_or_notify "$title" "$body" "$stage"
        return 0
    fi
    rresult=$STATE/results/$repo/$slug.resolve.json
    rlog=$STATE/logs/$(date -u +%Y%m%dT%H%MZ)-$repo-$slug.resolve.log
    mkdir -p "$(dirname "$rresult")"
    rm -f "$rresult"
    case $stage in
        review|preprod) last=$STATE/results/$repo/$slug.$stage.json ;;
        *) last=$STATE/results/$repo-$slug.json ;;
    esac
    ci_log=$STATE/results/$repo/$slug.ci-failed.log
    rm -f "$ci_log"
    resolve_ci_log "$pr" "$ci_log" || { rm -f "$ci_log"; ci_log=none; }
    settings=$rwt/agents/settings/implement.json
    [ -f "$settings" ] || settings=$rwt/scripts/agent/settings.json
    for f in "$rwt/agents/mcp.json" "$rwt/scripts/agent/mcp.json"; do
        [ -f "$f" ] && { mcp=(--mcp-config "$f"); break; }
    done
    prompt=$(python3 -c 'import sys; a = sys.argv; print(open(a[1]).read().format(key=a[2], plan=a[3], pr_url=a[4], stage=a[5],
block_title=a[6], block_body=a[7], result_json=a[8], log_paths=a[9], ci_log=a[10], validate_full=a[11], result_file=a[12]))' \
        "$HERE/prompts/resolve.md" "$key" "$(pr_plan "$rwt")" "https://github.com/$GH_REPO/pull/$pr" "${stage:-implement}" \
        "$title" "$body" "$(cat "$last" 2>/dev/null || echo none)" "$(resolve_logs | grep . || echo none)" "$ci_log" \
        "$VALIDATE_FULL" "$rresult")
    "${Q[@]}" set "$key" "resolve_log=$rlog"
    say "$key: resolver start for PR #$pr (stopped at ${stage:-implement}: $title) (log $rlog)"
    rstart=$(size "$rlog")
    (
        cd "$rwt" || exit 1
        AGENT_SLUG=$key AGENT_RESULT_FILE=$rresult timeout --kill-after=2m 60m claude -p "$prompt" --model claude-opus-5-5 \
            --max-turns 120 --settings "$settings" "${mcp[@]}" --permission-mode auto --permission-prompts none \
            --output-format json
    ) >> "$rlog" 2>&1
    code=$?
    say "$key: resolver exit $code"
    cost "$rlog" resolve claude-opus-5-5 "$rstart"
    # A usage-limit stop isn't the item's fault: the try comes back, and the queue pauses.
    if [ ! -f "$rresult" ] && grep -qiE 'usage limit|rate.limit|limit reached' "$rlog"; then
        "${Q[@]}" unset "$key" resolve_tried
        limit_pause
        return 0
    fi
    rfield() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' "$rresult" "$1" 2>/dev/null; }
    action=$(rfield action)
    class=$(rfield class)
    summary=$(rfield summary)
    diagnosis=$(rfield diagnosis)
    if [ "$action" = fixed ] && ! (cd "$rwt" && bash -c "$VALIDATE_FULL" >> "$rlog" 2>&1); then
        action=give-up
        diagnosis="The resolver said fixed, but \`$VALIDATE_FULL\` fails in its worktree. ${diagnosis:-$summary}"
    fi
    if [ -z "$action" ]; then
        if [ "$code" = 124 ]; then diagnosis="The resolver timed out after 60 minutes."
        else diagnosis="The resolver wrote no result (exit $code)."; fi
    fi
    "${Q[@]}" set "$key" "resolve_class=${class:-unknown}" "resolve_diagnosis=${diagnosis:-${summary:-no diagnosis}}"
    case $action in
        fixed)
            say "$key: resolver fixed (${class:-no class}): $summary"
            if [ "$stage" = review ] || [ "$stage" = preprod ]; then
                tier=review
            else
                verdict=$(cd "$rwt" && python3 "$HERE/gate.py" "$plan" --config "$toml" --base "origin/$BASE")
                tier=$(echo "$verdict" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tier"])' 2>/dev/null)
                "${Q[@]}" set "$key" "tier=${tier:-review}"
            fi
            if [ "$tier" = low ]; then
                gh pr edit "$pr" --repo "$GH_REPO" --remove-label ci-failed >/dev/null 2>&1
                sleep "${AGENT_CI_SETTLE:-30}"  # let the resolver's push register its checks before the CI wait
                if merge_pr "$pr"; then
                    "${Q[@]}" set "$key" status=done "merged_at=$(stamp)"
                    say "$key: PR #$pr merged after the resolver's fix"
                    notify "merged PR #$pr" "Merged to $BASE after green CI (gate tier low, after a resolver fix): https://github.com/$GH_REPO/pull/$pr"
                    preprod_follow "$repo" "$pr"
                    git -C "$AGENT_REPO" worktree remove --force "$rwt"
                else
                    rc=$?
                    "${Q[@]}" set "$key" "resolve_diagnosis=$diagnosis
After the fix: $(merge_failure "$rc")."
                    hold_or_notify "PR #$pr not merged after the resolver's fix: $(merge_failure "$rc")" "$body" merge
                fi
            else
                # Back to the validator (a fresh-context review of the fix); preprod follows an approval.
                "${Q[@]}" unset "$key" review_attempts review_verdict review_summary preprod_verdict preprod_attempts
                "${Q[@]}" set "$key" status=awaiting-review
                say "$key: PR #$pr back to the validator after the resolver's fix"
                git -C "$AGENT_REPO" worktree remove --force "$rwt"
            fi ;;
        requeue)
            if [ "$stage" = review ] || [ "$stage" = preprod ]; then
                # The PR is unchanged, so the validator tries again (a fresh review; preprod follows an approval).
                "${Q[@]}" unset "$key" review_attempts review_verdict review_summary preprod_verdict preprod_attempts
                "${Q[@]}" set "$key" status=awaiting-review
            else
                "${Q[@]}" requeue "$key" --fresh-attempts --note "${summary:-re-queued by the resolver (${class:-transient})}" >/dev/null
            fi
            say "$key: resolver requeue (${class:-no class}): $summary" ;;
        *)
            say "$key: resolver give-up (${class:-no class})${summary:+: $summary}"
            hold_or_notify "$title" "$body" "${stage:-implement}" ;;
    esac
}

if key=$("${Q[@]}" oldest awaiting-resolve); then
    load_item
    closed_by_hand || resolve_stage
    pr_status
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
    local p=$1 start
    shift
    start=$(size "$log")
    (
        cd "$wt" || exit 1
        AGENT_SLUG=$key AGENT_RESULT_FILE=$result timeout --kill-after=2m "$MAX_RUNTIME" claude -p "$@" "$p" \
            --model claude-opus-5-5 \
            --settings "$settings" \
            "${mcp_args[@]}" \
            --permission-mode auto --permission-prompts none \
            --output-format json
    ) >> "$log" 2>&1
    local code=$?
    cost "$log" implement claude-opus-5-5 "$start"
    return $code
}

: > "$log"
# A conflict round (`conflict=1`, set by `requeue --conflict`): the worktree goes to the PR head and the runner starts
# `git merge origin/$BASE` itself (the implementer can't run git merge). Clean: pushed, no Claude run. Conflicts: Claude
# gets a prompt for resolving just those, not /implement.
conflict_round=
conflict_prompt=
if [ "$("${Q[@]}" get "$key" conflict 2>/dev/null)" = 1 ]; then
    conflict_round=1
    conflict_pr=$("${Q[@]}" get "$key" pr)
    {
        git -C "$wt" fetch -q origin
        git -C "$wt" merge -q --ff-only "origin/$branch" 2>/dev/null || git -C "$wt" reset -q --hard "origin/$branch"
    } >> "$log" 2>&1
    if git -C "$wt" merge --no-edit "origin/$BASE" >> "$log" 2>&1; then
        if git -C "$wt" push -q origin HEAD >> "$log" 2>&1; then
            say "$key: conflict round: merged $BASE into PR #$conflict_pr (clean), no Claude run"
            printf '{"status": "ready", "pr": %s, "summary": "Merged %s into PR #%s (clean)."}' \
                "$conflict_pr" "$BASE" "$conflict_pr" > "$result"
        else
            say "$key: conflict round: merged $BASE into PR #$conflict_pr, but the push failed (log $log)"
        fi
    else
        conflict_files=$(git -C "$wt" diff --name-only --diff-filter=U | paste -sd, - | sed 's/,/, /g')
        if [ -n "$conflict_files" ]; then
            conflict_prompt="PR #$conflict_pr for $plan conflicts with origin/$BASE. The runner started \`git merge origin/$BASE\` in this worktree; these files have conflict markers: $conflict_files. Resolve them, keeping both sides' intent; run \`$VALIDATE_FULL\`; \`git add\` them, \`git commit --no-edit\`, \`git push origin HEAD\`. Then write the result file as for /implement (\`ready\` with the PR). Change nothing else.

Result file: $result"
        else
            git -C "$wt" merge --abort >/dev/null 2>&1
            say "$key: conflict round: merging $BASE failed without conflicting files (log $log)"
        fi
    fi
fi
if [ -n "$conflict_round" ]; then
    code=0
    if [ -n "$conflict_prompt" ]; then
        say "$key: conflict round start for PR #$conflict_pr in $conflict_files (log $log)"
        run_claude "$conflict_prompt"
        code=$?
    fi
elif [ -n "$resume_id" ]; then
    if [ "$resume_why" = limit ]; then
        opener="Your previous run was stopped by the Claude usage limit. The work you committed or left in the worktree is still there."
    else
        # A fix round (`requeue --fix` wrote the newest note: a validator's or the user's `/fix`) or the user's answer.
        trailer="Continue the plan ($plan, --unattended) from where you stopped."
        opener=$(echo "$notes" | python3 -c '
import json, re, sys
notes = json.load(sys.stdin)
last = notes[-1] if notes else ""
m = re.match(r"(Review|Preprod) validator, round (\d+)/(\d+)", last)
if m:
    print(f"A validator asked for changes (round {m[2]} of {m[3]}). Fix exactly these findings on the same branch, re-run "
          f"validation, push, and write the result file with status ready. Don\x27t redo finished work. {last}")
elif last.startswith("User /fix"):
    ask = last.split(": ", 1)[-1]
    print(f"The user asked for this change on the PR: {ask}\nMake it on the same branch, re-run "
          f"validation, push, and write the result file with status ready. Don\x27t redo finished work.")
else:
    print(f"The user answered your question. {last}")' 2>/dev/null)
        case $opener in "A validator asked"*|"The user asked"*) trailer="Then finish as /implement does for $plan (--unattended): validate, push, result file." ;; esac
    fi
    say "$key: run start, resuming session $resume_id ($resume_why) (log $log)"
    echo "claude -p --resume $resume_id" >> "$log"
    run_claude "$opener

Result file: $result
All queue notes: $notes
${trailer:-Continue the plan ($plan, --unattended) from where you stopped.}" --resume "$resume_id"
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
# A conflict round that wrote no result (crashed, timed out, usage limit, failed push) stays one: the next run starts the
# merge again rather than running /implement on a finished plan.
if [ -n "$conflict_round" ] && python3 -c 'import json,sys; sys.exit(not json.load(open(sys.argv[1])).get("status"))' "$result" 2>/dev/null; then
    "${Q[@]}" unset "$key" conflict
fi

# A usage-limit exit isn't the plan's fault: give the attempt back and pause the queue, for as long as the usage gate
# says (a weekly-limit stop waits for the weekly reset), or 5 h if the gate allows a start or can't tell.
if [ ! -f "$result" ] && grep -qiE 'usage limit|rate.limit|limit reached' "$log"; then
    if [ "$("${Q[@]}" get "$key" run_kind 2>/dev/null)" = fix ]; then
        "${Q[@]}" set "$key" status=queued fix_pending=1  # a fix run cost no attempt; the next run is the same fix round
    else
        attempts=$("${Q[@]}" get "$key" attempts)
        "${Q[@]}" set "$key" status=queued "attempts=$((attempts - 1))"
    fi
    # Resume this session next time instead of redoing its work. An answer resume keeps `resume=1` (same session either way).
    [ -n "$session_id" ] && [ "$resume_why" != 1 ] && "${Q[@]}" set "$key" resume=limit
    limit_pause
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
conflicted=
if [ "$status" = ready ]; then
    pr=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pr"])' "$result")
    # A conflicting PR gets no CI: merge the base in first, so the gate and the validator see a mergeable PR.
    conflict_files=
    if [ "$(pr_mergeable "$pr")" = CONFLICTING ]; then
        sync_base "$pr"
        [ $? = 1 ] && conflicted=1
    fi
    if [ -n "$conflicted" ]; then
        final=blocked  # `finish` records the PR; conflict_back below re-queues it for a conflict round
    else
        verdict=$(cd "$wt" && python3 "$HERE/gate.py" "$plan" --config "$toml" --base "origin/$BASE")
        tier=$(echo "$verdict" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tier"])')
        "${Q[@]}" set "$key" "tier=$tier"
        if [ "$tier" = review ]; then
            final=needs-review  # the validator reviews it on a later tick (review stage above)
            gh pr comment "$pr" --repo "$GH_REPO" --body "Risk gate: \`review\`, so a validator reviews it next.
\`\`\`json
$verdict
\`\`\`" >/dev/null
        elif merge_pr "$pr"; then
            final=merged
            "${Q[@]}" set "$key" "merged_at=$(stamp)"
            notify "merged PR #$pr" "Merged to $BASE after green CI (gate tier low): https://github.com/$GH_REPO/pull/$pr"
            preprod_follow "$repo" "$pr"
        else
            # Not merged (gate tier low): the PR stays open, labelled ci-failed, and the item is blocked. Preprod is only
            # for `review` PRs the validator approved, so a repo without [preprod] can't get stuck waiting for it.
            # A conflict instead goes back to the implementer (conflict_back below), with no label.
            merge_rc=$?
            final=blocked
            if [ "$merge_rc" = 4 ]; then
                conflicted=1
            else
                not_merged=$(merge_failure "$merge_rc")
                gh pr edit "$pr" --repo "$GH_REPO" --add-label ci-failed >/dev/null 2>&1 \
                    || { gh label create ci-failed --repo "$GH_REPO" --force >/dev/null; gh pr edit "$pr" --repo "$GH_REPO" --add-label ci-failed >/dev/null; }
                gh pr comment "$pr" --repo "$GH_REPO" --body "Not merged: $not_merged. Risk gate: \`$tier\`
\`\`\`json
$verdict
\`\`\`" >/dev/null
            fi
        fi
    fi
    [ -n "$conflicted" ] && not_merged=$(merge_failure 4)
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
if [ -n "$conflicted" ]; then
    conflict_back "$pr"  # queued for a conflict round (the worktree stays), or blocked with its own notice
    new=$("${Q[@]}" get "$key" status)
fi
say "$key: -> $new"
case "$new" in
    done|awaiting-preprod|awaiting-review)
        git -C "$AGENT_REPO" worktree remove --force "$wt" ;;
    blocked)
        # Blocked at the implementer: an earlier stage's verdict no longer says where it is (the PR status block).
        [ -z "${not_merged:-}" ] && "${Q[@]}" unset "$key" rerun review_verdict review_summary
        if [ -n "$conflicted" ]; then
            :  # conflict_back held it (or sent the notice)
        elif [ -n "${not_merged:-}" ]; then
            hold_or_notify "PR #$pr not merged: $not_merged" "https://github.com/$GH_REPO/pull/$pr stays open, labelled ci-failed. Fix it and merge by hand, or re-queue $key. Log: $log (on the VPS)." merge
        else
            hold_or_notify "blocked after $("${Q[@]}" get "$key" attempts) attempt(s)" "Log: $log (on the VPS). Last result: $(cat "$result" 2>/dev/null || echo none)" implement
        fi ;;
esac
pr_status

# Cap the Docker build cache: a full validation can rebuild an image, and a busted dependency layer adds ~750 MB, so a
# chain of runs could fill the disk. Recent layers stay, so rebuilds stay fast.
docker builder prune -f --keep-storage "${AGENT_BUILD_CACHE_KEEP:-3GB}" >> "$STATE/runs.log" 2>&1 || true
"$HERE/preprod.sh" tick >> "$STATE/runs.log" 2>&1
exit 0
