#!/usr/bin/env bash
# Single-occupant preprod for review PRs. One PR at a time holds the lock; the rest wait as `awaiting-preprod`.
#
#   preprod.sh status        print the holder; release the lock if its PR was merged or closed
#   preprod.sh deploy <pr>   take the lock (must be free or held by <pr>), deploy the PR head, health-check it
#   preprod.sh release       drop the lock and redeploy origin/trunk
#   preprod.sh tick          status, then deploy the oldest awaiting-preprod item if the lock is free (cron, no Claude)
#
# Config (~/.config/strelitzia-agent/env): PREPROD_APP_DIR (the checkout that
# preprod builds from), PREPROD_DEPLOY_CMD (rebuilds only the preprod dashboard service), PREPROD_HEALTH_URL, AGENT_REPO,
# PREPROD_SEED_CMD (optional: wipes and reseeds the preprod DB with scripts/seed_preprod.py after each deploy).
# If the required ones are unset, deploy exits 3 ("not configured"); see agents/DEPLOY.md.
# Sourcing this file (tests) defines the functions without running a command.
set -uo pipefail

CONFIG_DIR=$HOME/.config/strelitzia-agent
[ -f "$CONFIG_DIR/env" ] && . "$CONFIG_DIR/env"
HERE=$(cd "$(dirname "$0")" && pwd)
STATE=${STRELITZIA_AGENT_STATE:-$HOME/.local/state/strelitzia-agent}
LOCK=$STATE/preprod.lock
GH_REPO=${GH_REPO:-SiddharthEngineer/Strelitzia}
Q=(python3 "$HERE/buildqueue.py")
mkdir -p "$STATE"

configured() {
    [ -n "${PREPROD_APP_DIR:-}" ] && [ -n "${PREPROD_DEPLOY_CMD:-}" ] && [ -n "${PREPROD_HEALTH_URL:-}" ]
}

holder() { [ -f "$LOCK" ] && cut -f1 "$LOCK"; }

deploy_ref() {  # deploy_ref <git ref fetched into PREPROD_APP_DIR>
    git -C "$PREPROD_APP_DIR" fetch -q origin "$1" || return 1
    git -C "$PREPROD_APP_DIR" checkout -q --detach FETCH_HEAD || return 1
    bash -c "$PREPROD_DEPLOY_CMD" || return 1
    # Seed output goes to stderr so it reaches runs.log even when cmd_release silences stdout.
    if [ -n "${PREPROD_SEED_CMD:-}" ]; then
        if [ -f "$PREPROD_APP_DIR/scripts/seed_preprod.py" ]; then
            bash -c "$PREPROD_SEED_CMD" >&2 || return 1
        else
            echo "seed skipped: no scripts/seed_preprod.py at $(git -C "$PREPROD_APP_DIR" rev-parse --short HEAD)" >&2
        fi
    fi
    sleep 10
    health_check
}

health_check() { "$HERE/../validate.sh" --preprod; }

slug_for_pr() {
    python3 - "$1" "$STATE/queue.json" <<'EOF'
import json, sys
pr, path = int(sys.argv[1]), sys.argv[2]
items = json.load(open(path))["items"] if __import__("os").path.exists(path) else {}
print(next((s for s, it in items.items() if it.get("pr") == pr), ""))
EOF
}

cmd_status() {
    local pr state slug
    pr=$(holder) || { echo "preprod: free"; return 0; }
    state=$(gh pr view "$pr" --repo "$GH_REPO" --json state -q .state 2>/dev/null)
    if [ "$state" = MERGED ] || [ "$state" = CLOSED ]; then
        slug=$(slug_for_pr "$pr")
        [ -n "$slug" ] && "${Q[@]}" set "$slug" "status=$([ "$state" = MERGED ] && echo done || echo cancelled)"
        echo "preprod: PR #$pr is $state, releasing"
        cmd_release
        return 0
    fi
    echo "preprod: held by PR #$pr since $(cut -f3 "$LOCK")"
}

cmd_deploy() {
    local pr=$1 cur slug
    configured || { echo "preprod not configured (agents/DEPLOY.md, Preprod section)"; return 3; }
    cur=$(holder)
    if [ -n "$cur" ] && [ "$cur" != "$pr" ]; then echo "preprod busy: PR #$cur"; return 4; fi
    printf '%s\t%s\t%s\n' "$pr" "$(gh pr view "$pr" --repo "$GH_REPO" --json headRefOid -q .headRefOid)" "$(date -u +%FT%TZ)" > "$LOCK"
    if ! deploy_ref "pull/$pr/head"; then
        echo "deploy of PR #$pr failed; restoring trunk"
        cmd_release
        return 1
    fi
    gh pr edit "$pr" --repo "$GH_REPO" --add-label on-preprod --remove-label awaiting-preprod >/dev/null 2>&1
    gh pr comment "$pr" --repo "$GH_REPO" --body "Deployed to preprod ($PREPROD_HEALTH_URL is healthy). Test steps are in the PR description." >/dev/null
    slug=$(slug_for_pr "$pr")
    [ -n "$slug" ] && "${Q[@]}" set "$slug" status=on-preprod
    python3 "$HERE/inbox.py" notify --title "PR #$pr is on preprod, ready for you to test" \
        --body "https://github.com/$GH_REPO/pull/$pr. Test steps are in the PR description." ${slug:+--slug "$slug"} >/dev/null
    echo "PR #$pr deployed to preprod"
}

cmd_release() {
    local pr
    pr=$(holder) && gh pr edit "$pr" --repo "$GH_REPO" --remove-label on-preprod >/dev/null 2>&1
    rm -f "$LOCK"
    configured && deploy_ref trunk >/dev/null && echo "preprod reset to trunk"
}

cmd_tick() {
    cmd_status
    holder >/dev/null && return 0
    configured || return 0
    local next
    next=$(python3 - "$STATE/queue.json" <<'EOF'
import json, os, sys
path = sys.argv[1]
items = json.load(open(path))["items"] if os.path.exists(path) else {}
waiting = sorted((it["updated"], it["pr"]) for it in items.values() if it["status"] == "awaiting-preprod" and it.get("pr"))
print(waiting[0][1] if waiting else "")
EOF
)
    [ -n "$next" ] && cmd_deploy "$next"
}

[ "${BASH_SOURCE[0]}" = "$0" ] || return 0

case "${1:-}" in
    status) cmd_status ;;
    deploy) [ -n "${2:-}" ] || { echo "usage: preprod.sh deploy <pr>"; exit 2; }; cmd_deploy "$2" ;;
    release) cmd_release ;;
    tick) cmd_tick ;;
    *) sed -n '2,12p' "$0"; exit 2 ;;
esac
