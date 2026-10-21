#!/usr/bin/env bash
# Tag origin/main as the next release and push the tag; the user's step (agents never run it).
#
#   agent-runner release [--dry-run] [--install] [vX.Y.Z]
#
# With no version, the newest vX.Y.Z tag's patch number goes up by one. A given version must be newer than the newest
# tag. It refuses when origin/main already has a tag or CI on it isn't green. --dry-run shows what it would tag and
# push; --install then installs the new tag (install.sh). Works in $AGENT_RUNNER_SRC (default ~/src/agent-runner):
# it only fetches there and never touches the working tree.
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
. "$HERE/ci.sh"
SRC=${AGENT_RUNNER_SRC:-$HOME/src/agent-runner}
INSTALL=${AGENT_RUNNER_INSTALL:-$HERE/install.sh}

usage() { sed -n '4p' "$0" | sed 's/^# \{0,1\}/usage: /' >&2; exit 2; }
die() { echo "release: $*" >&2; exit 1; }
g() { git -C "$SRC" "$@"; }

dry= inst= want=
while [ $# -gt 0 ]; do
    case $1 in
        --dry-run) dry=1 ;;
        --install) inst=1 ;;
        -h|--help) usage ;;
        v*) [ -z "$want" ] || usage; want=$1 ;;
        *) usage ;;
    esac
    shift
done

[ -d "$SRC/.git" ] || [ -f "$SRC/.git" ] || die "$SRC is not a git clone (set AGENT_RUNNER_SRC)"
g fetch -q --tags origin
sha=$(g rev-parse --verify -q origin/main^{commit}) || die "no origin/main in $SRC"
latest=$(latest_tag -C "$SRC")

if [ -n "$want" ]; then
    [[ $want =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "$want is not a vX.Y.Z version"
    if [ -n "$latest" ] && ! version_gt "$want" "$latest"; then die "$want is not newer than the newest tag, $latest"; fi
    tag=$want
elif [ -n "$latest" ]; then
    IFS=. read -r major minor patch <<<"${latest#v}"
    tag=v$major.$minor.$((patch + 1))
else
    die "no vX.Y.Z tag yet: give the first version, e.g. release v0.1.0"
fi
g rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "tag $tag already exists"

on=$(g tag --points-at "$sha" | grep -E '^v' | tr '\n' ' ' || true)
[ -z "$on" ] || die "origin/main (${sha:0:7}) is already tagged ${on% }: nothing new to release"

verdict=$(ci_verdict "$(g remote get-url origin)" "$sha")
[ "$verdict" = green ] || die "$verdict (origin/main ${sha:0:7}); not tagging"

echo "$tag at origin/main ${sha:0:7}; changes since ${latest:-the start}:"
g log --oneline --no-decorate ${latest:+"$latest.."}"$sha" | sed 's/^/  /'
if [ -n "$dry" ]; then
    echo "dry run: nothing tagged or pushed"
    exit 0
fi

g tag "$tag" "$sha"
if ! g push -q origin "refs/tags/$tag"; then
    g tag -d "$tag" >/dev/null
    die "push failed; the local tag $tag was removed"
fi
echo "pushed $tag"
[ -z "$inst" ] || exec "$INSTALL" "$tag"
echo "install it with: agent-runner install $tag"
