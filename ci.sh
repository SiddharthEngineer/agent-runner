# Sourced by install.sh and release.sh, not run on its own.
#
#   ci_verdict <remote-url> <sha>   prints "green" if every CI run on exactly this commit succeeded, else why it can't vouch
#   latest_tag <git-args...>        prints the newest vX.Y.Z tag (version order), or nothing; e.g. latest_tag -C ~/src/agent-runner
#   version_gt <a> <b>              true when vX.Y.Z tag a is newer than b

ci_verdict() {
    local r owner json
    command -v gh >/dev/null || { echo "gh is not installed"; return; }
    r=${1%/}; r=${r%.git}
    owner=${r%/*}; owner=${owner##*[/:]}
    json=$(timeout 60 gh run list --repo "$owner/${r##*/}" --commit "$2" --workflow CI --json conclusion,status 2>/dev/null) \
        || { echo "gh run list failed"; return; }
    printf '%s' "$json" | python3 -c '
import json, sys
runs = json.load(sys.stdin)
if not runs:
    print("no CI run on this commit")
elif all(r.get("status") == "completed" and r.get("conclusion") == "success" for r in runs):
    print("green")
elif any(r.get("status") != "completed" for r in runs):
    print("CI is still running on this commit")
else:
    print("CI did not pass on this commit")
' 2>/dev/null || echo "could not read the gh run list output"
}

latest_tag() {
    git "$@" tag -l 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n 1 || true
}

version_gt() {
    [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n 1)" = "$1" ]
}
