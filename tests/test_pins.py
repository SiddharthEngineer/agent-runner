"""Tests for pins.py: bump PRs for a parent's components.toml. Temp origins + clones and a stub `gh`."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TOML = """
[repo]
name = "{name}"
github = "o/{name}"
base = "main"
merge = "{merge}"
[validate]
fast = "v"
full = "v --full"
[gate]
high_risk = ["x"]
doc_or_test = ["*.md"]
"""

COMPONENTS = """# What prod runs. Edited by hand or by agent-runner pins.
[kid]
tag = "v1.0.0"  # the dashboard
compose = "kid.yml"

# Not a registered repo.
[stranger]
tag = "v2.0.0"
"""

# Open PRs live in $GH_STATE; `pr diff` is the real `git diff main...<branch>` on the parent's origin ($GH_ORIGIN).
# `pr checks` prints the PR's "checks" (default: none reported). Every call is logged to $GH_LOG.
STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
state_path = os.environ["GH_STATE"]
state = json.load(open(state_path))
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default
def find(n):
    return next(pr for pr in state["prs"] if pr["number"] == int(n))
if args[:2] == ["pr", "list"]:
    prs = [pr for pr in state["prs"] if (pr["state"] == "open") == (opt("--state") == "open")]
    if "--label" in args:
        prs = [pr for pr in prs if opt("--label") in pr["labels"]]
    if "--head" in args:
        prs = [pr for pr in prs if pr["headRefName"] == opt("--head")]
    oid = lambda b: subprocess.run(["git", "-C", os.environ["GH_ORIGIN"], "rev-parse", "-q", "--verify", b],
                                   capture_output=True, text=True).stdout.strip()
    print(json.dumps([{**pr, "labels": [{"name": n} for n in pr["labels"]], "state": pr["state"].upper(),
                       "headRefOid": oid(pr["headRefName"])} for pr in prs]))
elif args[:2] == ["pr", "create"]:
    n = 10 + len(state["prs"])
    state["prs"].append({"number": n, "headRefName": opt("--head"), "title": opt("--title"), "body": opt("--body"),
                         "labels": [opt("--label")], "state": "open", "comments": []})
    print(f"https://github.com/{opt('--repo')}/pull/{n}")
elif args[:2] == ["pr", "edit"]:
    pr = find(args[2])
    pr.update(title=opt("--title", pr["title"]), body=opt("--body", pr["body"]))
elif args[:2] == ["pr", "close"]:
    pr = find(args[2])
    pr["state"] = "closed"
    pr["comments"].append(opt("--comment"))
elif args[:2] == ["pr", "comment"]:
    find(args[2])["comments"].append(opt("--body"))
elif args[:2] == ["pr", "diff"]:
    pr = find(args[2])
    print(subprocess.run(["git", "-C", os.environ["GH_ORIGIN"], "diff", "main..." + pr["headRefName"]],
                         capture_output=True, text=True, check=True).stdout, end="")
elif args[:2] == ["pr", "checks"]:
    checks = find(args[2]).get("checks")
    if checks is None:
        sys.exit("no checks reported on the 'x' branch")
    print(json.dumps([{"state": s} for s in checks]))
elif args[:2] == ["pr", "merge"]:
    pr = find(args[2])
    if pr.get("merge_fails"):
        sys.exit("merge failed")
    pr["state"] = "merged"
elif args[:2] == ["label", "create"]:
    pass
else:
    sys.exit("stub gh: unexpected " + " ".join(args))
json.dump(state, open(state_path, "w"))
'''


def sh(*args, env=None):
    return subprocess.run([str(a) for a in args], check=True, capture_output=True, text=True, env=env).stdout.strip()


class World:
    def __init__(self, tmp: Path, components: str = COMPONENTS):
        self.tmp = tmp
        self.state = tmp / "gh.json"
        self.log = tmp / "gh.log"
        self.state.write_text(json.dumps({"prs": []}))
        self.log.write_text("")
        bindir = tmp / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(STUB)
        (bindir / "gh").chmod(0o755)
        self.home = tmp / "home"
        (self.home / ".config/agent-runner/repos").mkdir(parents=True)
        self.env = {**os.environ, "HOME": str(self.home), "PATH": f"{bindir}:{os.environ['PATH']}",
                    "AGENT_RUNNER_STATE": str(tmp / "state"), "GH_STATE": str(self.state), "GH_LOG": str(self.log),
                    "GH_ORIGIN": str(tmp / "parent.git"), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
        for k in ("AGENT_INBOX_REPO", "NTFY_TOPIC"):
            self.env.pop(k, None)
        self.make("parent", {"components.toml": components}, merge="squash")
        self.make("kid", {"README.md": "kid\n"})
        self.tag("kid", "v1.0.0", "#1 First")

    def make(self, name: str, files: dict[str, str], merge: str = "merge"):
        origin, work = self.tmp / f"{name}.git", self.tmp / f"{name}-work"
        sh("git", "init", "-q", "--bare", "-b", "main", origin)
        sh("git", "clone", "-q", origin, work, env=self.env)
        (work / "agents").mkdir()
        (work / "agents/runner.toml").write_text(TOML.format(name=name, merge=merge))
        for path, text in files.items():
            (work / path).write_text(text)
        self.commit(name, "init")
        sh("git", "clone", "-q", origin, self.tmp / f"{name}-clone", env=self.env)
        (self.home / f".config/agent-runner/repos/{name}.env").write_text(f"AGENT_REPO={self.tmp / f'{name}-clone'}\n")

    def git(self, name: str, *args) -> str:
        return sh("git", "-C", self.tmp / f"{name}-work", *args, env=self.env)

    def commit(self, name: str, msg: str, path: str = "f.txt", text: str | None = None):
        (self.tmp / f"{name}-work" / path).write_text(msg if text is None else text)
        self.git(name, "add", "-A")
        self.git(name, "commit", "-q", "-m", msg)
        self.git(name, "push", "-q", "origin", "HEAD:main")

    def tag(self, name: str, tag: str, *prs: str):
        self.commit(name, f"for {tag}")
        self.git(name, "tag", "-a", "--cleanup=whitespace", tag, "-m", "\n".join([tag, "", *prs]))
        self.git(name, "push", "-q", "origin", tag)

    def run(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "pins.py"), *args], env=self.env, capture_output=True,
                              text=True)

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def prs(self) -> list[dict]:
        return json.loads(self.state.read_text())["prs"]

    def origin(self, *args) -> str:
        return sh("git", "-C", self.tmp / "parent.git", *args)


def writes(w: World) -> list[list[str]]:
    return [c for c in w.calls() if c[:2] != ["pr", "list"]]


def test_new_tag_opens_a_pr_changing_one_line(tmp_path):
    w = World(tmp_path)
    w.tag("kid", "v1.0.1", "#7 Fix the chart", "#8 Faster load")
    out = w.run("sync")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "opened parent#10: Pin kid v1.0.1" in out.stdout
    assert "stranger: skipped in parent: not a registered repo" in out.stdout
    [pr] = w.prs()
    assert pr["headRefName"] == "pin/kid" and pr["title"] == "Pin kid v1.0.1" and pr["labels"] == ["pin-bump"]
    assert "- v1.0.1 · o/kid#7 Fix the chart, o/kid#8 Faster load" in pr["body"]
    assert ["label", "create", "pin-bump"] in [c[:3] for c in w.calls()]
    diff = w.origin("diff", "main", "pin/kid")
    changed = [line for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    assert changed == ['-tag = "v1.0.0"  # the dashboard', '+tag = "v1.0.1"  # the dashboard']
    assert w.origin("show", "pin/kid:components.toml") == COMPONENTS.replace('"v1.0.0"', '"v1.0.1"').rstrip("\n")
    assert w.origin("rev-parse", "pin/kid^") == w.origin("rev-parse", "main")
    assert "o/kid#7 Fix the chart" in w.origin("log", "-1", "--format=%B", "pin/kid")


def test_older_open_pr_is_updated_in_place(tmp_path):
    w = World(tmp_path)
    w.tag("kid", "v1.0.1", "#7 A")
    w.run("sync")
    w.tag("kid", "v1.0.2", "#9 B")
    out = w.run("sync")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "updated parent#10: Pin kid v1.0.2" in out.stdout
    [pr] = w.prs()
    assert pr["title"] == "Pin kid v1.0.2" and pr["state"] == "open"
    assert "- v1.0.2 · o/kid#9 B\n- v1.0.1 · o/kid#7 A" in pr["body"]
    assert 'tag = "v1.0.2"' in w.origin("show", "pin/kid:components.toml")
    assert w.origin("rev-parse", "pin/kid^") == w.origin("rev-parse", "main")


def test_current_pr_makes_no_calls(tmp_path):
    w = World(tmp_path)
    w.tag("kid", "v1.0.1")
    w.run("sync")
    before = len(w.calls())
    out = w.run("sync")
    assert out.returncode == 0 and out.stdout == ""
    assert [c[:2] for c in w.calls()[before:]] == [["pr", "list"]]


def test_up_to_date_makes_no_call_and_says_so_in_a_dry_run(tmp_path):
    w = World(tmp_path)
    out = w.run("sync")
    assert out.returncode == 0 and writes(w) == []
    assert "kid" not in out.stdout  # quiet every tick
    out = w.run("sync", "--dry-run")
    assert "kid: v1.0.0 is newest, nothing to do" in out.stdout


def test_dry_run_opens_nothing(tmp_path):
    w = World(tmp_path)
    w.tag("kid", "v1.0.1")
    out = w.run("sync", "--dry-run")
    assert "would open parent PR Pin kid v1.0.1 on pin/kid" in out.stdout
    assert writes(w) == [] and "pin/kid" not in w.origin("branch")


def test_pin_already_newest_closes_the_open_pr(tmp_path):
    w = World(tmp_path)
    w.tag("kid", "v1.0.1")
    w.run("sync")
    w.commit("parent", "pin by hand", "components.toml", COMPONENTS.replace('"v1.0.0"', '"v1.0.1"'))
    out = w.run("sync")
    assert "close parent#10" in out.stdout
    [pr] = w.prs()
    assert pr["state"] == "closed" and "already pins kid at v1.0.1" in pr["comments"][0]


def test_hold_opens_no_pr_and_says_so_once(tmp_path):
    w = World(tmp_path, COMPONENTS.replace('compose = "kid.yml"', 'compose = "kid.yml"\nhold = "testing"'))
    w.tag("kid", "v1.0.1")
    out = w.run("sync")
    assert "kid: held in parent (testing), no bumps" in out.stdout
    assert w.prs() == [] and writes(w) == []
    assert "held" not in w.run("sync").stdout  # one line per change, not per tick


def test_unregistered_component_is_skipped_once(tmp_path):
    w = World(tmp_path)
    assert "stranger: skipped" in w.run("sync").stdout
    assert "stranger" not in w.run("sync").stdout


def test_repo_without_components_toml_is_not_a_parent(tmp_path):
    w = World(tmp_path)
    w.git("parent", "rm", "-q", "components.toml")
    w.git("parent", "commit", "-qm", "no pins")
    w.git("parent", "push", "-q", "origin", "HEAD:main")
    w.tag("kid", "v1.0.1")
    out = w.run("sync")
    assert out.returncode == 0 and out.stdout == "" and w.calls() == []


def test_set_pin_keeps_comments_and_order():
    sys.path.insert(0, str(ROOT))
    import pins
    text = '[a]\ntag = "v1.0.0"\n\n[kid]  # x\n# tag = "v0.0.1"\n  tag = "v1.0.0" # keep\nz = 1\n[b]\ntag = "v1.0.0"\n'
    assert pins.set_pin(text, "kid", "v1.2.0") == text.replace('  tag = "v1.0.0" # keep', '  tag = "v1.2.0" # keep')


def set_pr(w: World, number: int, **fields):
    data = json.loads(w.state.read_text())
    next(pr for pr in data["prs"] if pr["number"] == number).update(fields)
    w.state.write_text(json.dumps(data))


def inbox_stub(w: World, tmp: Path) -> Path:
    """pins.py with a stub inbox.py beside it that logs each notice to <tmp>/notices."""
    code = tmp / "code"
    code.mkdir()
    for f in ("pins.py", "versions.py", "config.py"):
        (code / f).write_text((ROOT / f).read_text())
    (code / "inbox.py").write_text("import sys\nopen(%r, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                                   % str(tmp / "notices"))
    w.env["AGENT_INBOX_REPO"] = "o/inbox"
    return code


def tick(w: World, code: Path, *args):
    return subprocess.run([sys.executable, str(code / "pins.py"), "tick", *args], env=w.env, capture_output=True,
                          text=True)


def notices(tmp: Path) -> list[str]:
    return (tmp / "notices").read_text().splitlines() if (tmp / "notices").exists() else []


def test_green_pr_is_merged_with_the_parents_method(tmp_path):
    w = World(tmp_path)
    code = inbox_stub(w, tmp_path)
    w.tag("kid", "v1.0.1")
    w.run("sync")
    set_pr(w, 10, checks=["SUCCESS", "SKIPPED"])
    out = tick(w, code)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip() == "merged parent#10 kid v1.0.1"
    assert ["pr", "merge", "10", "--repo", "o/parent", "--squash", "--delete-branch"] in [c[:7] for c in w.calls()]
    assert w.prs()[0]["state"] == "merged" and notices(tmp_path) == []


def test_pending_or_no_checks_waits_quietly(tmp_path):
    w = World(tmp_path)
    code = inbox_stub(w, tmp_path)
    w.tag("kid", "v1.0.1")
    w.run("sync")
    assert tick(w, code).stdout == ""  # no checks reported yet
    set_pr(w, 10, checks=["SUCCESS", "PENDING"])
    assert tick(w, code).stdout == ""
    assert not any(c[:2] == ["pr", "merge"] for c in w.calls()) and notices(tmp_path) == []


def test_red_ci_sends_one_notice(tmp_path):
    w = World(tmp_path)
    code = inbox_stub(w, tmp_path)
    w.tag("kid", "v1.0.1")
    w.run("sync")
    set_pr(w, 10, checks=["SUCCESS", "FAILURE"])
    tick(w, code)
    tick(w, code)
    assert len(notices(tmp_path)) == 1 and "--project parent --title pin PR parent#10 CI is red" in notices(tmp_path)[0]
    assert not any(c[:2] == ["pr", "merge"] for c in w.calls())


def test_pr_that_edits_another_file_is_refused_once(tmp_path):
    w = World(tmp_path)
    code = inbox_stub(w, tmp_path)
    w.tag("kid", "v1.0.1")
    w.run("sync")
    w.git("parent", "fetch", "-q", "origin")
    w.git("parent", "checkout", "-q", "-b", "pin/kid", "origin/pin/kid")
    (tmp_path / "parent-work/run.sh").write_text("echo sneak\n")
    w.git("parent", "add", "-A")
    w.git("parent", "commit", "-qm", "sneak")
    w.git("parent", "push", "-q", "--force", "origin", "HEAD:pin/kid")
    set_pr(w, 10, checks=["SUCCESS"])
    out = tick(w, code)
    tick(w, code)
    assert "refuse parent#10" in out.stdout and "run.sh" in out.stdout
    assert not any(c[:2] == ["pr", "merge"] for c in w.calls())
    assert len(notices(tmp_path)) == 1 and len(w.prs()[0]["comments"]) == 1


def test_tick_dry_run_merges_nothing(tmp_path):
    w = World(tmp_path)
    code = inbox_stub(w, tmp_path)
    w.tag("kid", "v1.0.1")
    w.run("sync")
    set_pr(w, 10, checks=["SUCCESS"])
    out = tick(w, code, "--dry-run")
    assert "would merge parent#10 kid v1.0.1" in out.stdout and "merged" not in out.stdout
    assert not any(c[:2] == ["pr", "merge"] for c in w.calls())


def test_diff_problem():
    sys.path.insert(0, str(ROOT))
    import pins
    head = "diff --git a/components.toml b/components.toml\nindex 1..2 100644\n--- a/components.toml\n+++ b/components.toml\n@@ -1 +1 @@\n"
    assert pins.diff_problem(head + '-tag = "v1.0.0"\n+tag = "v1.0.1"  # x\n') is None
    assert "more than tag lines" in pins.diff_problem(head + '-tag = "v1.0.0"\n+tag = "v1.0.1"\n+x = 1\n')
    assert "more than tag lines" in pins.diff_problem(head + '-tag = "v1.0.0"\n+tag = "main"\n')
    assert "adds or removes" in pins.diff_problem(head + '+tag = "v1.0.1"\n')
    assert "mode" in pins.diff_problem("diff --git a/components.toml b/components.toml\nold mode 100644\nnew mode 100755\n")
    assert pins.diff_problem("") == "its diff is empty"


def test_pr_closed_by_hand_is_not_reopened_until_the_next_tag(tmp_path):
    w = World(tmp_path)
    w.tag("kid", "v1.0.1")
    w.run("sync")
    set_pr(w, 10, state="closed")
    out = w.run("sync")
    assert "Pin kid v1.0.1 was closed by hand in parent, not reopened" in out.stdout and len(w.prs()) == 1
    assert w.run("sync").stdout == ""
    w.tag("kid", "v1.0.2")
    assert "opened parent#11: Pin kid v1.0.2" in w.run("sync").stdout


def test_unlabelled_pin_branch_pr_is_left_alone(tmp_path):
    w = World(tmp_path)
    w.tag("kid", "v1.0.1")
    data = json.loads(w.state.read_text())
    data["prs"].append({"number": 3, "headRefName": "pin/kid", "title": "mine", "body": "", "labels": [],
                        "state": "open", "comments": []})
    w.state.write_text(json.dumps(data))
    out = w.run("sync")
    assert "isn't labelled pin-bump, so sync leaves it alone" in out.stdout
    assert writes(w) == [] and "pin/kid" not in w.origin("branch")


def test_merge_is_pinned_to_the_checked_head(tmp_path):
    w = World(tmp_path)
    code = inbox_stub(w, tmp_path)
    w.tag("kid", "v1.0.1")
    w.run("sync")
    set_pr(w, 10, checks=["SUCCESS"])
    tick(w, code)
    merge = next(c for c in w.calls() if c[:2] == ["pr", "merge"])
    assert merge[-2:] == ["--match-head-commit", w.origin("rev-parse", "pin/kid")]


def test_failing_merge_is_logged_and_notified_once(tmp_path):
    w = World(tmp_path)
    code = inbox_stub(w, tmp_path)
    w.tag("kid", "v1.0.1")
    w.run("sync")
    set_pr(w, 10, checks=["SUCCESS"], merge_fails=True)
    first, second = tick(w, code), tick(w, code)
    assert first.returncode == 1 and "parent#10: gh pr merge failed" in first.stdout
    assert second.returncode == 0 and second.stdout == ""
    assert len(notices(tmp_path)) == 1 and "merge failed" in notices(tmp_path)[0]


def test_backward_pin_is_refused():
    sys.path.insert(0, str(ROOT))
    import pins
    head = "diff --git a/components.toml b/components.toml\n--- a/components.toml\n+++ b/components.toml\n"
    assert pins.diff_problem(head + '-tag = "v1.2.0"\n+tag = "v1.1.9"\n') == "it doesn't move every pin forward"
