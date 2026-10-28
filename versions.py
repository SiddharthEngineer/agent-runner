#!/usr/bin/env python3
"""Version tags and GitHub releases for a repo whose runner.toml has `[release]`.

Usage:
  versions.py sync <repo> [--dry-run]                          tag the PRs merged since the newest tag; update the pre-release
  versions.py promote <repo> [<tag>] [--next-major] [--dry-run] make release vX.Y full and Latest on <tag> (default: newest)
  versions.py status <repo>                                    newest tag, open pre-release, newest full release

Rules (plan runner-15-versions):
- Every PR merged to the base gets the next patch tag (`v1.4.3` → `v1.4.4`) on its merge commit. One sync makes at most
  one tag, on the newest merge commit, and its message lists every PR it covers.
- Each minor version has one GitHub release titled `vX.Y`: a pre-release on the minor's newest tag while it collects
  patches, its notes listing every patch newest first. `promote` (the user's `make prod-release`) makes it a full, Latest
  release; the next merge then starts `vX.(Y+1).0`, or `v(X+1).0.0` after `promote --next-major`.
- Sync never guesses a starting point: no tag yet, or a newest tag the runner didn't make whose minor has no release,
  exits 3 until the user promotes what prod runs.

<repo> is a registered repo name: its dev clone is AGENT_REPO in ~/.config/agent-runner/repos/<repo>.env, and its
runner.toml is read from origin/<base>. Exit codes: 0 ok, 1 failed, 3 waiting for a promote, 4 not set up for releases
(no clone, no runner.toml, or no `[release]`; run.sh skips those quietly). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
MINOR_RE = re.compile(r"^v\d+\.\d+$")
TAGGED_BY = "Tagged-by: agent-runner versions sync"
NEXT_MAJOR = "<!-- agent-runner:next_major -->"
WAITING, OFF = 3, 4


class Fail(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


@dataclass
class Repo:
    name: str
    dir: Path
    github: str
    base: str
    release: config.Release | None
    remote: set[str] | None = None  # the tags on origin; a tag only in the local clone doesn't count
    merge: str = "merge"


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Fail(f"{' '.join(cmd[:3])} failed: {type(exc).__name__}") from exc
    if check and out.returncode != 0:
        err = (out.stderr or out.stdout).strip().splitlines()
        raise Fail(f"{' '.join(cmd[:3])} failed: {err[-1] if err else f'exit {out.returncode}'}")
    return out


def git(repo: Repo, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(repo.dir), *args], check=check)


def gh(*args: str) -> str:
    return run(["gh", *args]).stdout


def version(tag: str) -> tuple[int, int, int]:
    m = TAG_RE.match(tag)
    if not m:
        raise Fail(f"not a vX.Y.Z tag: {tag}")
    return int(m[1]), int(m[2]), int(m[3])


def minor(tag: str) -> str:
    major, mnr, _ = version(tag)
    return f"v{major}.{mnr}"


def open_repo(name: str) -> Repo:
    """The registered clone, fetched (tags too), with the runner.toml from origin/<base>."""
    if not config.NAME_RE.match(name):
        raise Fail(f"bad repo name: {name}")
    path = config.repo_env(name).get("AGENT_REPO")
    if not path or not Path(path).is_dir():
        raise Fail(f"{name}: no AGENT_REPO clone in ~/.config/agent-runner/repos/{name}.env", OFF)
    probe = Repo(name, Path(path), "", "", None)
    fetched = git(probe, "fetch", "-q", "--tags", "origin", check=False)
    head = git(probe, "rev-parse", "--abbrev-ref", "origin/HEAD", check=False).stdout.strip() \
        or f"origin/{config.repo_env(name).get('AGENT_BASE') or 'main'}"  # as run.sh's load_item
    text = git(probe, "show", f"{head}:agents/runner.toml", check=False)
    if text.returncode != 0:
        raise Fail(f"{name}: no agents/runner.toml on {head}", OFF)
    with tempfile.NamedTemporaryFile("w", suffix=".toml") as f:
        f.write(text.stdout)
        f.flush()
        try:
            cfg = config.load(Path(f.name))
        except config.ConfigError as exc:
            raise Fail(f"{name}: runner.toml refused: {exc}", OFF) from exc
    if cfg.release is None:  # not opted in: a failed fetch is none of our business
        return Repo(name, Path(path), cfg.repo.github, cfg.repo.base, None)
    if fetched.returncode != 0:
        err = fetched.stderr.strip().splitlines()
        raise Fail(f"{name}: git fetch failed: {err[-1] if err else f'exit {fetched.returncode}'}")
    remote = {line.rpartition("refs/tags/")[2] for line in
              git(probe, "ls-remote", "--tags", "--refs", "origin", "v*").stdout.splitlines()}
    return Repo(name, Path(path), cfg.repo.github, cfg.repo.base, cfg.release, remote)


def tags(repo: Repo) -> list[str]:
    """Every vX.Y.Z tag (on origin, once known), oldest version first."""
    names = git(repo, "tag", "-l", "v*").stdout.split()
    return sorted((t for t in names if TAG_RE.match(t) and (repo.remote is None or t in repo.remote)), key=version)


def tag_sort(names: list[str]) -> list[str]:
    """The vX.Y.Z names among <names>, oldest version first."""
    return sorted((t for t in names if TAG_RE.match(t)), key=version)


def tag_info(repo: Repo, tag: str) -> tuple[str, list[str], bool]:
    """(date, PR lines, made by sync) of one tag. A lightweight tag has no PR lines."""
    fmt = "%(objecttype)%00%(creatordate:short)%00%(contents)"
    kind, date, contents = (git(repo, "for-each-ref", f"--format={fmt}", f"refs/tags/{tag}").stdout.split("\0", 2)
                            + ["", ""])[:3]
    if kind != "tag":
        return date, [], False
    lines = contents.splitlines()
    return date, [line for line in lines if re.match(r"#\d+ ", line)], TAGGED_BY in lines


def notes(repo: Repo, mnr: str, upto: str) -> str:
    """One line per tag of the minor up to <upto>, newest first: `- v1.4.4 · #71 Title (2026-10-23)`."""
    out = []
    for tag in reversed(tags(repo)):
        if minor(tag) != mnr or version(tag) > version(upto):
            continue
        date, prs, _ = tag_info(repo, tag)
        out.append(f"- {tag}" + (f" · {', '.join(prs)}" if prs else "") + f" ({date})")
    return "\n".join(out)


def releases(repo: Repo) -> list[dict]:
    data = json.loads(gh("release", "list", "--repo", repo.github, "--exclude-drafts", "--limit", "100",
                         "--json", "name,tagName,isPrerelease,isLatest") or "[]")
    return [r for r in data if MINOR_RE.match(r.get("name") or "")]


def find(rels: list[dict], mnr: str) -> dict | None:
    return next((r for r in rels if r["name"] == mnr), None)


def body(repo: Repo, rel: dict) -> str:
    return json.loads(gh("release", "view", rel["tagName"], "--repo", repo.github, "--json", "body")).get("body") or ""


def pending(repo: Repo, newest: str) -> list[dict]:
    """PRs merged to the base whose merge commit is on origin/<base> but not in <newest>, oldest merge first."""
    prs = json.loads(gh("pr", "list", "--repo", repo.github, "--state", "merged", "--base", repo.base, "--limit", "100",
                        "--json", "number,title,mergeCommit,mergedAt") or "[]")
    untagged = commits(repo, newest, f"origin/{repo.base}")
    out = [{**pr, "sha": pr["mergeCommit"]["oid"]} for pr in prs
           if (pr.get("mergeCommit") or {}).get("oid") in untagged]
    return sorted(out, key=lambda pr: pr.get("mergedAt") or "")


def commits(repo: Repo, since: str, until: str) -> set[str]:
    """The commits in <until> but not in <since>."""
    return set(git(repo, "rev-list", f"{since}..{until}").stdout.split())


def next_tag(newest: str, rel: dict, rel_body: str) -> str:
    major, mnr, patch = version(newest)
    if rel["isPrerelease"]:
        return f"v{major}.{mnr}.{patch + 1}"
    if NEXT_MAJOR in rel_body:
        return f"v{major + 1}.0.0"
    return f"v{major}.{mnr + 1}.0"


def waiting(repo: Repo, newest: str | None) -> Fail:
    what = f"no release for {minor(newest)} yet" if newest else "no vX.Y.Z tag yet"
    return Fail(f"{repo.name}: {what}: run `agent-runner versions promote {repo.name} <tag>` for what prod runs", WAITING)


def say(dry_run: bool, text: str) -> None:
    print(("would " if dry_run else "") + text)


def sync(repo: Repo, dry_run: bool) -> int:
    if repo.release is None or not repo.release.tags:
        raise Fail(f"{repo.name}: no [release] in runner.toml", OFF)
    all_tags = tags(repo)
    newest = all_tags[-1] if all_tags else None
    if newest is None:
        raise waiting(repo, newest)
    rels = releases(repo)
    rel = find(rels, minor(newest))
    if rel is None and not tag_info(repo, newest)[2]:
        raise waiting(repo, newest)

    prs = pending(repo, newest)
    if prs:
        if rel is None:  # the runner's own tag whose release failed last time: patch on, the release is made below
            rel = {"isPrerelease": True}
        target = prs[-1]["sha"]
        in_target = commits(repo, newest, target)
        prs = [pr for pr in prs if pr["sha"] in in_target]
        new = next_tag(newest, rel, "" if rel["isPrerelease"] else body(repo, rel))
        listed = [f"#{pr['number']} {pr['title']}" for pr in reversed(prs)]
        say(dry_run, f"tag {new} on {target[:7]} ({', '.join(listed)}) and push it")
        if dry_run:
            return 0
        message = "\n".join([new, "", *listed, "", TAGGED_BY])
        git(repo, "tag", "-a", "--cleanup=whitespace", new, "-m", message, target)  # keep the "#71 …" lines
        try:
            git(repo, "push", "-q", "origin", f"refs/tags/{new}")
        except Fail:
            git(repo, "tag", "-d", new, check=False)  # the next sync tries again from the same state
            raise
        if repo.remote is not None:
            repo.remote.add(new)
        newest = new
        rel = find(rels, minor(newest))

    # The minor's pre-release follows its newest tag (also repairs a release step that failed after a pushed tag).
    mnr = minor(newest)
    text = notes(repo, mnr, newest)
    if rel is None:
        say(dry_run, f"create pre-release {mnr} on {newest}")
        if not dry_run:
            gh("release", "create", newest, "--repo", repo.github, "--verify-tag", "--title", mnr, "--prerelease",
               "--notes", text)
    elif rel["isPrerelease"] and rel["tagName"] != newest:
        say(dry_run, f"move pre-release {mnr} to {newest}")
        if not dry_run:
            gh("release", "edit", rel["tagName"], "--repo", repo.github, "--tag", newest, "--title", mnr,
               "--prerelease", "--notes", text)
    return 0


def promote(repo: Repo, tag: str | None, next_major: bool, dry_run: bool) -> int:
    all_tags = tags(repo)
    tag = tag or (all_tags[-1] if all_tags else None)
    if tag is None:
        raise Fail(f"{repo.name}: no vX.Y.Z tag to promote")
    if tag not in all_tags:
        raise Fail(f"{repo.name}: no tag {tag} (version tags are vX.Y.Z)")
    mnr = minor(tag)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = notes(repo, mnr, tag) + f"\n\nDeployed to prod {today} at {tag}"
    if next_major:
        text += f"\n\n{NEXT_MAJOR}"
    rel = find(releases(repo), mnr)
    say(dry_run, f"{'edit' if rel else 'create'} release {mnr} on {tag} as full and Latest"
        + (", next sync starts the next major" if next_major else ""))
    if dry_run:
        return 0
    if rel:
        gh("release", "edit", rel["tagName"], "--repo", repo.github, "--tag", tag, "--title", mnr,
           "--prerelease=false", "--latest", "--notes", text)
    else:
        gh("release", "create", tag, "--repo", repo.github, "--verify-tag", "--title", mnr, "--latest", "--notes", text)
    return 0


def status(repo: Repo) -> int:
    all_tags = tags(repo)
    rels = releases(repo)
    pre = [r for r in rels if r["isPrerelease"]]
    full = [r for r in rels if not r["isPrerelease"]]
    latest = next((r for r in full if r.get("isLatest")), None) or (
        max(full, key=lambda r: version(r["tagName"]) if TAG_RE.match(r["tagName"]) else (0, 0, 0)) if full else None)
    print(f"newest tag: {all_tags[-1] if all_tags else 'none'}")
    print("pre-release: " + (", ".join(f"{r['name']} on {r['tagName']}" for r in pre) or "none"))
    print("full release: " + (f"{latest['name']} on {latest['tagName']}" if latest else "none"))
    if repo.release is None:
        print("no [release] in runner.toml: sync does nothing")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="versions.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    s.add_argument("repo")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("promote")
    s.add_argument("repo")
    s.add_argument("tag", nargs="?")
    s.add_argument("--next-major", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("repo")
    s.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    config.load_env()
    try:
        repo = open_repo(args.repo)
        if args.cmd == "sync":
            return sync(repo, args.dry_run)
        if args.cmd == "promote":
            return promote(repo, args.tag, args.next_major, args.dry_run)
        return status(repo)
    except Fail as exc:
        print(f"versions: {exc}", file=sys.stderr)
        return exc.code
    except (ValueError, KeyError) as exc:  # unreadable gh output
        print(f"versions: {args.repo}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
