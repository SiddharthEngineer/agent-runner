"""Tests for versions.py: patch tags, the pre-release per minor, promote. A temp origin + clone and a stub `gh`."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

TOML = """
[repo]
name = "demo"
github = "o/demo"
base = "main"
merge = "squash"
[validate]
fast = "v"
full = "v --full"
[gate]
high_risk = ["x"]
doc_or_test = ["*.md"]
"""

# Answers `pr list`, `release list/view` from $GH_STATE, applies `release create/edit` to it, logs every call to $GH_LOG.
STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
state_path = os.environ["GH_STATE"]
state = json.load(open(state_path))
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default
def flag(name):
    for a in args:
        if a == name or a.startswith(name + "="):
            return a != name + "=false"
    return None
if args[:2] == ["pr", "list"]:
    print(json.dumps(state["prs"]))
elif args[:2] == ["release", "list"]:
    print(json.dumps([{k: r[k] for k in ("name", "tagName", "isPrerelease", "isLatest")} for r in state["releases"]]))
elif args[:2] == ["release", "view"]:
    rel = next((r for r in state["releases"] if r["tagName"] == args[2]), None)
    if rel is None:
        sys.exit("release not found")
    print(json.dumps({"body": rel["body"]}))
elif args[:2] == ["release", "create"]:
    if flag("--latest"):
        for r in state["releases"]:
            r["isLatest"] = False
    state["releases"].insert(0, {"name": opt("--title"), "tagName": args[2], "isPrerelease": bool(flag("--prerelease")),
                                 "isLatest": bool(flag("--latest")), "body": opt("--notes")})
elif args[:2] == ["release", "edit"]:
    rel = next(r for r in state["releases"] if r["tagName"] == args[2])
    rel.update(tagName=opt("--tag", rel["tagName"]), name=opt("--title", rel["name"]), body=opt("--notes", rel["body"]))
    if flag("--prerelease") is not None:
        rel["isPrerelease"] = flag("--prerelease")
    if flag("--latest"):
        for r in state["releases"]:
            r["isLatest"] = r is rel
else:
    sys.exit("stub gh: unexpected " + " ".join(args))
json.dump(state, open(state_path, "w"))
'''


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class World:
    def __init__(self, tmp: Path, release: bool = True):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.clone = tmp / "clone"
        self.work = tmp / "work"  # where "GitHub" merges PRs (pushes to origin)
        self.state = tmp / "gh.json"
        self.log = tmp / "gh.log"
        self.state.write_text(json.dumps({"prs": [], "releases": []}))
        self.log.write_text("")
        bindir = tmp / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(STUB)
        (bindir / "gh").chmod(0o755)
        home = tmp / "home"
        (home / ".config/agent-runner/repos").mkdir(parents=True)
        (home / ".config/agent-runner/repos/demo.env").write_text(f"AGENT_REPO={self.clone}\n")
        self.env = {**os.environ, "HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}",
                    "GH_STATE": str(self.state), "GH_LOG": str(self.log), "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        sh("git", "clone", "-q", str(self.origin), str(self.work))
        (self.work / "agents").mkdir()
        (self.work / "agents/runner.toml").write_text(TOML + ("[release]\ntags = true\n" if release else ""))
        self.commit("init")
        sh("git", "clone", "-q", str(self.origin), str(self.clone))
        self.n = 0

    def git(self, *args, where=None):
        return subprocess.run(["git", "-C", str(where or self.work), *args], check=True, capture_output=True,
                              text=True, env=self.env).stdout.strip()

    def commit(self, msg: str) -> str:
        (self.work / "f.txt").write_text(msg)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        self.git("push", "-q", "origin", "HEAD:main")
        return self.git("rev-parse", "HEAD")

    def merge_pr(self, number: int, title: str) -> str:
        sha = self.commit(title)
        self.n += 1
        data = json.loads(self.state.read_text())
        data["prs"].insert(0, {"number": number, "title": title, "mergeCommit": {"oid": sha},
                               "mergedAt": f"2026-10-23T10:00:{self.n:02d}Z"})
        self.state.write_text(json.dumps(data))
        return sha

    def tag_by_hand(self, tag: str):
        self.git("tag", tag)
        self.git("push", "-q", "origin", tag)

    def run(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "versions.py"), *args], env=self.env, capture_output=True,
                              text=True)

    def remote_tags(self) -> dict[str, str]:
        out = sh("git", "ls-remote", "--tags", str(self.origin))
        return {ref.split("refs/tags/")[1]: sha for sha, ref in (line.split("\t") for line in out.splitlines())
                if not ref.endswith("^{}")}

    def tag_commit(self, tag: str) -> str:
        return sh("git", "-C", str(self.origin), "rev-parse", f"{tag}^{{commit}}")

    def releases(self) -> list[dict]:
        return json.loads(self.state.read_text())["releases"]

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def writes(self) -> list[list[str]]:
        return [c[:3] for c in self.calls() if c[:2] in (["release", "create"], ["release", "edit"])]


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.tag_by_hand("v1.4.3")
    assert w.run("promote", "demo", "v1.4.3").returncode == 0  # what prod runs, by the user
    w.log.write_text("")
    return w


def test_first_patch_creates_the_minor_prerelease_after_promote(world):
    sha = world.merge_pr(71, "Wordle: nginx /wordle/")
    out = world.run("sync", "demo")
    assert out.returncode == 0, out.stderr
    assert world.tag_commit("v1.5.0") == sha
    rel = world.releases()[0]
    assert (rel["name"], rel["tagName"], rel["isPrerelease"]) == ("v1.5", "v1.5.0", True)
    assert rel["body"].startswith("- v1.5.0 · #71 Wordle: nginx /wordle/ (")
    assert world.writes() == [["release", "create", "v1.5.0"]]
    assert world.run("sync", "demo").returncode == 0  # nothing new: no tag, no write
    assert world.writes() == [["release", "create", "v1.5.0"]]


def test_second_patch_moves_the_same_prerelease(world):
    world.merge_pr(71, "One")
    world.run("sync", "demo")
    sha = world.merge_pr(72, "Two")
    out = world.run("sync", "demo")
    assert out.returncode == 0, out.stderr
    assert world.tag_commit("v1.5.1") == sha
    pre = [r for r in world.releases() if r["name"] == "v1.5"]
    assert len(pre) == 1 and pre[0]["tagName"] == "v1.5.1" and pre[0]["isPrerelease"]
    lines = pre[0]["body"].splitlines()
    assert lines[0].startswith("- v1.5.1 · #72 Two (") and lines[1].startswith("- v1.5.0 · #71 One (")
    assert world.writes()[-1] == ["release", "edit", "v1.5.0"]


def test_promote_then_next_minor(world):
    world.merge_pr(71, "One")
    world.run("sync", "demo")
    out = world.run("promote", "demo")
    assert out.returncode == 0, out.stderr
    rel = next(r for r in world.releases() if r["name"] == "v1.5")
    assert (rel["tagName"], rel["isPrerelease"], rel["isLatest"]) == ("v1.5.0", False, True)
    assert "Deployed to prod " in rel["body"] and rel["body"].rstrip().endswith("at v1.5.0")
    assert not next(r for r in world.releases() if r["name"] == "v1.4")["isLatest"]
    world.merge_pr(72, "Two")
    world.run("sync", "demo")
    assert "v1.6.0" in world.remote_tags()
    assert next(r for r in world.releases() if r["name"] == "v1.6")["isPrerelease"]


def test_promote_next_major(world):
    world.merge_pr(71, "One")
    world.run("sync", "demo")
    assert world.run("promote", "demo", "--next-major").returncode == 0
    world.merge_pr(72, "Two")
    world.run("sync", "demo")
    assert "v2.0.0" in world.remote_tags()
    assert world.releases()[0]["name"] == "v2.0"


def test_two_prs_in_one_sync_make_one_tag(world):
    world.merge_pr(71, "One")
    sha = world.merge_pr(72, "Two")
    assert world.run("sync", "demo").returncode == 0
    assert sorted(world.remote_tags()) == ["v1.4.3", "v1.5.0"]
    assert world.tag_commit("v1.5.0") == sha
    assert world.releases()[0]["body"].startswith("- v1.5.0 · #72 Two, #71 One (")
    message = sh("git", "-C", str(world.clone), "tag", "-l", "--format=%(contents)", "v1.5.0")
    assert "#71 One" in message and "#72 Two" in message


def test_untagged_minor_waits_for_promote(tmp_path):
    w = World(tmp_path)
    out = w.run("sync", "demo")
    assert out.returncode == 3 and "no vX.Y.Z tag yet" in out.stderr
    w.tag_by_hand("v1.4.3")
    w.merge_pr(71, "One")
    out = w.run("sync", "demo")
    assert out.returncode == 3
    assert "no release for v1.4 yet: run `agent-runner versions promote demo <tag>` for what prod runs" in out.stderr
    assert sorted(w.remote_tags()) == ["v1.4.3"] and w.writes() == []


def test_direct_commits_do_not_tag(world):
    world.commit("hotfix straight to main")
    assert world.run("sync", "demo").returncode == 0
    assert sorted(world.remote_tags()) == ["v1.4.3"] and world.writes() == []


def test_dry_run_pushes_nothing(world):
    world.merge_pr(71, "One")
    out = world.run("sync", "demo", "--dry-run")
    assert out.returncode == 0 and out.stdout.startswith("would tag v1.5.0 on ")
    assert sorted(world.remote_tags()) == ["v1.4.3"] and world.writes() == []


def test_release_step_failed_is_repaired_next_sync(world):
    world.merge_pr(71, "One")
    world.run("sync", "demo")
    world.merge_pr(72, "Two")
    data = json.loads(world.state.read_text())
    data["releases"] = [r for r in data["releases"] if r["name"] != "v1.5"]  # as if `release create` had failed
    world.state.write_text(json.dumps(data))
    assert world.run("sync", "demo").returncode == 0
    rel = world.releases()[0]
    assert (rel["name"], rel["tagName"], rel["isPrerelease"]) == ("v1.5", "v1.5.1", True)


def test_without_release_section_sync_is_off(tmp_path):
    w = World(tmp_path, release=False)
    w.tag_by_hand("v1.0.0")
    w.merge_pr(1, "One")
    out = w.run("sync", "demo")
    assert out.returncode == 4 and w.remote_tags() == {"v1.0.0": w.remote_tags()["v1.0.0"]}
    assert w.run("status", "demo").stdout.startswith("newest tag: v1.0.0\n")


def test_status(world):
    world.merge_pr(71, "One")
    world.run("sync", "demo")
    out = world.run("status", "demo").stdout.splitlines()
    assert out == ["newest tag: v1.5.0", "pre-release: v1.5 on v1.5.0", "full release: v1.4 on v1.4.3"]


def test_unregistered_repo_is_off(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    out = subprocess.run([sys.executable, str(ROOT / "versions.py"), "sync", "nope"], env=env, capture_output=True,
                         text=True)
    assert out.returncode == 4


def test_config_release_section(tmp_path):
    path = tmp_path / "runner.toml"
    path.write_text(TOML)
    assert config.load(path).release is None
    path.write_text(TOML + "[release]\n")
    assert config.load(path).release == config.Release(tags=True)
    path.write_text(TOML + "[release]\ntags = false\n")
    assert config.load(path).release.tags is False
    path.write_text(TOML + '[release]\ntags = "yes"\n')
    with pytest.raises(config.ConfigError, match="release.tags must be true or false"):
        config.load(path)
