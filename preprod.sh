#!/usr/bin/env bash
# One preprod for every repo, held by one PR at a time; the rest wait as `awaiting-preprod`.
#
#   preprod.sh status              print the holder; release the lock if its PR was merged or closed
#   preprod.sh deploy <repo> <pr>  take the lock (free, or already held by <repo> <pr>), deploy the PR head, health-check it
#   preprod.sh release [<repo>]    drop the lock and put preprod back on <repo>'s base (default: the holder's repo)
#   preprod.sh tick                status, then deploy the oldest awaiting-preprod item of any repo if the lock is free
#
# Preprod is one environment: the engineerfamily-preprod compose project also builds Strelitzia, so the lock is global.
# Lock file: repo<TAB>pr<TAB>sha<TAB>time (a 3-column line from the old runner is read as strelitzia).
# Each repo's commands come from the [preprod] section of its agents/runner.toml on origin/<base>, never from a PR branch,
# and pass config.py's prod guard first. ~/.config/agent-runner/repos/<repo>.env gives AGENT_REPO (the dev clone) and the
# netrc for the health checks. If a repo has no usable config, deploy exits 3.
# Sourcing this file (tests) defines the functions without running a command.
set -uo pipefail

HERE=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
STATE=${AGENT_RUNNER_STATE:-${STRELITZIA_AGENT_STATE:-$HOME/.local/state/agent-runner}}
LOCK=$STATE/preprod.lock
Q=(python3 "$HERE/buildqueue.py")
CFG=(python3 "$HERE/config.py")
mkdir -p "$STATE"
trap 'rm -f "${RTOML:-}"' EXIT

holder() {  # prints "<repo> <pr>"; fails when preprod is free
    [ -s "$LOCK" ] || return 1
    awk -F'\t' 'NF >= 4 { print $1, $2; next } { print "strelitzia", $1 }' "$LOCK"
}

lock_since() { awk -F'\t' '{ print $NF }' "$LOCK"; }

# load_repo <repo>: read the repo's runner.toml from origin/<base> and set GH_REPO, BASE, DEPLOY, RELEASE, SEED, APP_DIR,
# HEALTH (url<TAB>status<TAB>json_ok lines) and NETRC. Returns 3 (and says why) if anything is missing or refused.
load_repo() {
    local repo=$1 env base_ref problems
    env=$("${CFG[@]}" repo-env "$repo") || { echo "bad repo name: $repo"; return 3; }
    unset AGENT_REPO PREPROD_NETRC  # never carry one repo's values into another's
    eval "$env"
    if [ -z "${AGENT_REPO:-}" ] || [ ! -d "$AGENT_REPO" ]; then
        echo "preprod not configured for $repo: no AGENT_REPO in ~/.config/agent-runner/repos/$repo.env"
        return 3
    fi
    git -C "$AGENT_REPO" fetch -q origin || { echo "can't fetch $AGENT_REPO"; return 3; }
    base_ref=$(git -C "$AGENT_REPO" rev-parse --abbrev-ref origin/HEAD 2>/dev/null) || base_ref=origin/${AGENT_BASE:-main}
    rm -f "${RTOML:-}"
    RTOML=$(mktemp)
    if ! git -C "$AGENT_REPO" show "$base_ref:agents/runner.toml" > "$RTOML" 2>/dev/null; then
        echo "preprod not configured for $repo: no agents/runner.toml on $base_ref"
        return 3
    fi
    if ! problems=$("${CFG[@]}" check "$RTOML"); then
        echo "refusing $repo's runner.toml:"
        echo "$problems"
        return 3
    fi
    cfg() { "${CFG[@]}" get "$RTOML" "$1" 2>/dev/null; }
    DEPLOY=$(cfg preprod.deploy)
    [ -n "$DEPLOY" ] || { echo "preprod not configured for $repo: no [preprod] deploy"; return 3; }
    GH_REPO=$(cfg repo.github)
    BASE=$(cfg repo.base)
    RELEASE=$(cfg preprod.release)
    SEED=$(cfg preprod.seed)
    APP_DIR=$(cfg preprod.app_dir)
    HEALTH=$(cfg preprod.health)
    local netrc_env
    netrc_env=$(cfg preprod.netrc_env)
    NETRC=${!netrc_env:-}
    LOADED=$repo
}

with_sha() { echo "${1//\{sha\}/$2}"; }  # with_sha <command> <sha>

deploy_ref() {  # deploy_ref <ref to fetch into APP_DIR> <sha>
    local ref=$1 sha=$2
    if [ -n "$APP_DIR" ]; then
        git -C "$APP_DIR" fetch -q origin "$ref" || return 1
        git -C "$APP_DIR" checkout -q --detach "$sha" || return 1
    fi
    bash -c "$(with_sha "$DEPLOY" "$sha")" || return 1
    # Seed output goes to stderr so it reaches runs.log even when cmd_release silences stdout.
    if [ -n "$SEED" ]; then
        bash -c "$SEED" >&2 || return 1
    fi
    sleep 10
    health_check
}

# health_check: every [preprod] health URL through Cloudflare with the repo's netrc (a 127.0.0.1 check doesn't prove the
# public host routes to the right nginx). `status` entries compare the HTTP code; `json_ok` entries need "ok": true.
# Each URL gets 6 tries, 10 s apart.
health_check() {
    local line url rest want json_ok try out code body netrc_args=()
    [ -n "$NETRC" ] && netrc_args=(--netrc-file "$NETRC")
    [ -n "$HEALTH" ] || { echo "health: no checks configured"; return 0; }
    while IFS= read -r line; do
        # url<TAB>status<TAB>json_ok, status may be empty (read with IFS=tab would merge the two tabs)
        url=${line%%$'\t'*}
        rest=${line#*$'\t'}
        want=${rest%%$'\t'*}
        json_ok=${rest#*$'\t'}
        for ((try = 1; ; try++)); do
            out=$(curl -sS --max-time 20 "${netrc_args[@]}" -w '\n%{http_code}' "$url" 2>&1)
            code=${out##*$'\n'}
            body=${out%$'\n'*}
            if [ "$json_ok" = true ]; then
                [ "$code" = 200 ] && grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' <<<"$body" && break
            else
                [ "$code" = "${want:-200}" ] && break
            fi
            if [ "$try" -ge 6 ]; then
                echo "health failed: $url returned $code: ${body:0:300}"
                return 1
            fi
            sleep 10
        done
    done <<<"$HEALTH"
    echo "healthy"
}

key_for_pr() {  # key_for_pr <repo> <pr>: the queue key of the repo's item with that PR
    python3 - "$1" "$2" "$STATE/queue.json" <<'EOF'
import json, os, sys
repo, pr, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
items = json.load(open(path))["items"] if os.path.exists(path) else {}
print(next((k for k, it in items.items() if it.get("pr") == pr and (it.get("repo") or "strelitzia") == repo), ""))
EOF
}

cmd_status() {
    local cur repo pr state key
    cur=$(holder) || { echo "preprod: free"; return 0; }
    read -r repo pr <<<"$cur"
    load_repo "$repo" >/dev/null || { echo "preprod: held by $repo#$pr since $(lock_since) (its config can't be read)"; return 0; }
    state=$(gh pr view "$pr" --repo "$GH_REPO" --json state -q .state 2>/dev/null)
    if [ "$state" = MERGED ] || [ "$state" = CLOSED ]; then
        key=$(key_for_pr "$repo" "$pr")
        [ -n "$key" ] && "${Q[@]}" set "$key" "status=$([ "$state" = MERGED ] && echo done || echo cancelled)"
        echo "preprod: $repo#$pr is $state, releasing"
        cmd_release "$repo"
        return 0
    fi
    echo "preprod: held by $repo#$pr since $(lock_since)"
}

cmd_deploy() {
    local repo=$1 pr=$2 cur sha key
    if cur=$(holder) && [ "$cur" != "$repo $pr" ]; then
        echo "preprod busy: ${cur/ /#}"
        return 4
    fi
    load_repo "$repo" || return 3
    sha=$(gh pr view "$pr" --repo "$GH_REPO" --json headRefOid -q .headRefOid) || { echo "can't read $repo#$pr"; return 1; }
    printf '%s\t%s\t%s\t%s\n' "$repo" "$pr" "$sha" "$(date -u +%FT%TZ)" > "$LOCK"
    if ! deploy_ref "pull/$pr/head" "$sha"; then
        echo "deploy of $repo#$pr failed; restoring $BASE"
        cmd_release "$repo"
        return 1
    fi
    gh pr edit "$pr" --repo "$GH_REPO" --add-label on-preprod --remove-label awaiting-preprod >/dev/null 2>&1
    gh pr comment "$pr" --repo "$GH_REPO" --body "Deployed to preprod (health checks passed). Test steps are in the PR description." >/dev/null
    key=$(key_for_pr "$repo" "$pr")
    [ -n "$key" ] && "${Q[@]}" set "$key" status=on-preprod
    python3 "$HERE/inbox.py" notify --title "$repo#$pr is on preprod, ready for you to test" --project "$repo" \
        --body "https://github.com/$GH_REPO/pull/$pr. Test steps are in the PR description." ${key:+--slug "$key"} >/dev/null
    echo "$repo#$pr deployed to preprod"
}

cmd_release() {
    local repo=${1:-} cur pr sha
    if cur=$(holder); then
        [ -n "$repo" ] || repo=${cur%% *}
        pr=${cur##* }
    fi
    [ -n "$repo" ] || { echo "preprod: free"; return 0; }
    if ! load_repo "$repo" >/dev/null; then
        rm -f "$LOCK"
        echo "lock released; $repo's config can't be read, so preprod was not redeployed"
        return 0
    fi
    [ -n "${pr:-}" ] && gh pr edit "$pr" --repo "$GH_REPO" --remove-label on-preprod >/dev/null 2>&1
    rm -f "$LOCK"
    sha=$(git -C "$AGENT_REPO" rev-parse "origin/$BASE")
    if [ -n "$RELEASE" ]; then
        bash -c "$(with_sha "$RELEASE" "$sha")" >/dev/null && echo "preprod reset to $repo $BASE"
    else
        deploy_ref "$BASE" "$sha" >/dev/null && echo "preprod reset to $repo $BASE"
    fi
}

cmd_tick() {
    cmd_status
    holder >/dev/null && return 0
    local next
    next=$(python3 - "$STATE/queue.json" <<'EOF'
import json, os, sys
path = sys.argv[1]
items = json.load(open(path))["items"] if os.path.exists(path) else {}
waiting = sorted((it["updated"], it.get("repo") or "strelitzia", it["pr"]) for it in items.values()
                 if it["status"] == "awaiting-preprod" and it.get("pr"))
print(f"{waiting[0][1]} {waiting[0][2]}" if waiting else "")
EOF
)
    [ -n "$next" ] && cmd_deploy ${next}
}

[ "${BASH_SOURCE[0]}" = "$0" ] || return 0

case "${1:-}" in
    status) cmd_status ;;
    deploy) [ -n "${3:-}" ] || { echo "usage: preprod.sh deploy <repo> <pr>"; exit 2; }; cmd_deploy "$2" "$3" ;;
    release) cmd_release "${2:-}" ;;
    tick) cmd_tick ;;
    *) sed -n '2,7p' "$0"; exit 2 ;;
esac
