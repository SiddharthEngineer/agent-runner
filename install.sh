#!/usr/bin/env bash
# Install a tagged release of the runner as the one copy cron and every repo call (design D2). The user's step (AM1).
#
#   ./install.sh [<tag>]          e.g. v0.2.0; no tag: the newest vX.Y.Z tag. Rollback is ./install.sh <older tag>
#   ./install.sh --full [<tag>]   same, but always run the full test suite
#   ./install.sh --list           installed releases; * marks the active one
#   (agent-runner install [--full] [<tag>] runs the installed copy's install.sh)
#
# Layout under ~/.local/share/agent-runner: mirror.git (bare mirror of $AGENT_RUNNER_REMOTE), releases/<tag> (detached
# worktrees of it), current -> releases/<tag>. ~/.local/bin/agent-runner -> current/agent-runner. The release's own
# scripts/validate.sh must pass before current moves: only `validate.sh --quick` if CI's run on the tag's exact commit
# succeeded (asked with gh), the full suite otherwise. The newest 3 releases and the active one are kept.
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/ci.sh"  # ci_verdict, latest_tag, version_gt

SHARE=$HOME/.local/share/agent-runner
MIRROR=$SHARE/mirror.git
RELEASES=$SHARE/releases
REMOTE=${AGENT_RUNNER_REMOTE:-https://github.com/SiddharthEngineer/agent-runner.git}
KEEP=3

usage() { sed -n '4,6p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
mg() { git --git-dir="$MIRROR" "$@"; }
active() { [ -L "$SHARE/current" ] && basename "$(readlink -f "$SHARE/current")" || true; }

relink() {  # relink <target> <link>: make the new link beside the old one, then rename it over (atomic)
    ln -sfn "$1" "$2.new"
    mv -T "$2.new" "$2"
}

list() {
    local on tag
    on=$(active)
    for tag in $(ls "$RELEASES" 2>/dev/null | sort -V); do
        if [ "$tag" = "$on" ]; then echo "* $tag"; else echo "  $tag"; fi
    done
}

prune() {  # keep the newest $KEEP releases and the active one
    local on tag
    on=$(active)
    for tag in $(ls "$RELEASES" | sort -V | head -n -"$KEEP"); do
        [ "$tag" = "$on" ] && continue
        mg worktree remove --force "$RELEASES/$tag"
        echo "removed $tag"
    done
    mg worktree prune
}

full=
if [ "${1:-}" = --full ]; then full=1; shift; fi
[ $# -le 1 ] || usage
tag=
case ${1:-} in
    "") ;;
    --list) [ -z "$full" ] || usage; list; exit 0 ;;
    -h|--help) usage ;;
    v*) tag=$1 ;;
    *) echo "install.sh: $1 is not a v* tag" >&2; exit 2 ;;
esac

mkdir -p "$SHARE" "$RELEASES" "$HOME/.local/bin"
if [ -d "$MIRROR" ]; then
    mg fetch -q --prune --tags origin
else
    git clone -q --mirror "$REMOTE" "$MIRROR"
fi
was=$(active)
if [ -z "$tag" ]; then
    tag=$(latest_tag --git-dir="$MIRROR")
    [ -n "$tag" ] || { echo "install.sh: no vX.Y.Z tag in $REMOTE" >&2; exit 2; }
    if [ "$tag" = "$was" ] && [ -z "$full" ]; then
        echo "$tag is the newest tag and already installed"
        exit 0
    fi
fi
if ! mg rev-parse -q --verify "refs/tags/$tag^{commit}" >/dev/null; then
    echo "install.sh: no tag $tag in $REMOTE" >&2
    exit 2
fi
if [ -n "$was" ] && version_gt "$was" "$tag"; then echo "rolling back $was -> $tag"; fi

dir=$RELEASES/$tag
created=
if [ ! -d "$dir" ]; then
    mg worktree add -q --detach "$dir" "$tag"
    created=1
fi
echo "$tag" > "$dir/VERSION"

sha=$(git -C "$dir" rev-parse HEAD)  # what is checked out: a reused release may predate a moved tag
quick=
if [ -n "$full" ]; then
    echo "--full: running the full suite"
elif [ "$sha" != "$(mg rev-parse "refs/tags/$tag^{commit}")" ]; then
    echo "releases/$tag is at ${sha:0:7}, not the tag's commit; running the full suite"
elif [ -n "$(git -C "$dir" status --porcelain --untracked-files=no)" ]; then
    echo "releases/$tag has local changes; running the full suite"
else
    verdict=$(ci_verdict "$REMOTE" "$sha")
    if [ "$verdict" = green ]; then quick=--quick; else echo "$verdict (${sha:0:7}); running the full suite"; fi
fi

if ! "$dir/scripts/validate.sh" $quick; then
    echo "install.sh: $tag failed its own validation; current is unchanged ($(active || true))" >&2
    [ -n "$created" ] && mg worktree remove --force "$dir"
    exit 1
fi
[ -z "$quick" ] || echo "CI passed on ${sha:0:7}; ran the quick checks only"

relink "releases/$tag" "$SHARE/current"
relink "$SHARE/current/agent-runner" "$HOME/.local/bin/agent-runner"
echo "installed $tag: $SHARE/current -> releases/$tag"
prune
