#!/usr/bin/env bash
# Install a tagged release of the runner as the one copy cron and every repo call (design D2). The user's step (AM1).
#
#   ./install.sh <tag>     e.g. v0.2.0; rollback is ./install.sh <older tag>
#   ./install.sh --list    installed releases; * marks the active one
#
# Layout under ~/.local/share/agent-runner: mirror.git (bare mirror of $AGENT_RUNNER_REMOTE), releases/<tag> (detached
# worktrees of it), current -> releases/<tag>. ~/.local/bin/agent-runner -> current/agent-runner. The release's own
# scripts/validate.sh must pass before current moves. The newest 3 releases and the active one are kept.
set -euo pipefail

SHARE=$HOME/.local/share/agent-runner
MIRROR=$SHARE/mirror.git
RELEASES=$SHARE/releases
REMOTE=${AGENT_RUNNER_REMOTE:-https://github.com/SiddharthEngineer/agent-runner.git}
KEEP=3

usage() { sed -n '4,5p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
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

[ $# -eq 1 ] || usage
case $1 in
    --list) list; exit 0 ;;
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
if ! mg rev-parse -q --verify "refs/tags/$tag^{commit}" >/dev/null; then
    echo "install.sh: no tag $tag in $REMOTE" >&2
    exit 2
fi

dir=$RELEASES/$tag
created=
if [ ! -d "$dir" ]; then
    mg worktree add -q --detach "$dir" "$tag"
    created=1
fi
echo "$tag" > "$dir/VERSION"

if ! "$dir/scripts/validate.sh"; then
    echo "install.sh: $tag failed its own validation; current is unchanged ($(active || true))" >&2
    [ -n "$created" ] && mg worktree remove --force "$dir"
    exit 1
fi

relink "releases/$tag" "$SHARE/current"
relink "$SHARE/current/agent-runner" "$HOME/.local/bin/agent-runner"
echo "installed $tag: $SHARE/current -> releases/$tag"
prune
