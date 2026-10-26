"""Tests for intake.py: plans on origin/<base> with `**Queue:** yes` get queued. A temp origin + clone and a stub `gh`."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import intake  # noqa: E402

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

# Logs every call to $GH_LOG; `issue create` prints an issue URL.
STUB = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if os.environ.get("GH_FAIL"):
    sys.exit("gh: rate limited")
if sys.argv[1:3] == ["issue", "create"]:
    print("https://github.com/o/inbox/issues/7")
'''


def plan(queue: str = "yes", status: str = "❌ Not started", risk: str = "**Risk:** low\n", tasks: int = 1,
         extra: str = "") -> str:
    body = "".join(f"### Task {i}: t\n**Runs:** unattended\n**Files:** a{i}.py (new)\n**Status:** ❌\n\n"
                   for i in range(1, tasks + 1))
    queue_line = f"**Queue:** {queue}\n" if queue else ""
    return f"# Plan: p\n**Status:** {status}\n{risk}{queue_line}\n## Goal\ng\n{extra}\n## Tasks\n{body}"


def sh(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


class World:
    def __init__(self, tmp: Path):
        self.origin = tmp / "origin.git"
        self.clone = tmp / "clone"
        self.work = tmp / "work"
        self.state = tmp / "state"
        self.log = tmp / "gh.log"
        self.log.write_text("")
        bindir = tmp / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(STUB)
        (bindir / "gh").chmod(0o755)
        home = tmp / "home"
        (home / ".config/agent-runner/repos").mkdir(parents=True)
        (home / ".config/agent-runner/repos/demo.env").write_text(f"AGENT_REPO={self.clone}\n")
        self.env = {**os.environ, "HOME": str(home), "AGENT_RUNNER_STATE": str(self.state),
                    "PATH": f"{bindir}:{os.environ['PATH']}", "GH_LOG": str(self.log), "AGENT_INBOX_REPO": "o/inbox",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@e"}
        self.env.pop("NTFY_TOPIC", None)
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        sh("git", "clone", "-q", str(self.origin), str(self.work))
        (self.work / "agents/plans/done").mkdir(parents=True)
        self.push({"agents/runner.toml": TOML + "[intake]\nenabled = true\n"})
        sh("git", "clone", "-q", str(self.origin), str(self.clone))

    def push(self, files: dict[str, str]) -> str:
        for path, text in files.items():
            (self.work / path).write_text(text)
        subprocess.run(["git", "-C", str(self.work), "add", "-A"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.work), "commit", "-q", "-m", "c"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.work), "push", "-q", "origin", "HEAD:main"], check=True, env=self.env,
                       capture_output=True)
        return sh("git", "-C", str(self.work), "rev-parse", "HEAD")

    def scan(self, *args) -> list[str]:
        out = subprocess.run([sys.executable, str(ROOT / "intake.py"), "scan", *args], env=self.env,
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        return out.stdout.splitlines()

    def queue(self) -> dict:
        path = self.state / "queue.json"
        return json.loads(path.read_text())["items"] if path.exists() else {}

    def set_queue(self, items: dict) -> None:
        self.state.mkdir(exist_ok=True)
        (self.state / "queue.json").write_text(json.dumps({"paused_until": None, "items": items}))

    def notices(self) -> list[str]:
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [c[c.index("--title") + 1] for c in calls if c[:2] == ["issue", "create"]]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_parse_queue_line():
    assert intake.parse_queue_line(plan()) == intake.QueueLine(True, None, None)
    line = intake.parse_queue_line(plan("yes at 2026-10-27T02:00Z after other/p1"))
    assert line == intake.QueueLine(True, "2026-10-27T02:00Z", "other/p1")
    assert intake.parse_queue_line(plan("no after p1")) == intake.QueueLine(False, None, "p1")
    assert intake.parse_queue_line(plan("")) is None
    assert intake.parse_queue_line(plan("", extra="- **Queue:** one queue.json\n**Queue:** yes\n")) is None
    with pytest.raises(ValueError):
        intake.parse_queue_line(plan("maybe"))
    with pytest.raises(ValueError):
        intake.parse_queue_line(plan("yes later"))


def test_intake_off_does_nothing(world):
    world.push({"agents/runner.toml": TOML, "agents/plans/p1.md": plan()})
    assert world.scan() == []
    assert world.queue() == {}


def test_yes_is_queued_once(world):
    sha = world.push({"agents/plans/p1.md": plan("yes at 2026-10-27T02:00Z")})
    out = world.scan()
    assert out == ["intake: queued demo/p1 (not before 2026-10-27T02:00:00+00:00)"]
    it = world.queue()["demo/p1"]
    blob = sh("git", "-C", str(world.work), "rev-parse", "HEAD:agents/plans/p1.md")
    assert (it["source"], it["plan_blob"], it["status"], it["plan"]) == ("intake", blob, "queued", "agents/plans/p1.md")
    assert it["notes"] == [f"queued by intake from {sha[:12]}"]
    assert world.scan() == []
    assert list(world.queue()) == ["demo/p1"]


def test_no_cancels_only_before_start(world):
    world.push({"agents/plans/p1.md": plan(), "agents/plans/p2.md": plan()})
    world.scan()
    items = world.queue()
    items["demo/p2"].update(status="running", attempts=1)
    world.set_queue(items)
    world.push({"agents/plans/p1.md": plan("no"), "agents/plans/p2.md": plan("no")})
    assert world.scan() == ["intake: cancelled demo/p1 (**Queue:** no)"]
    assert world.queue()["demo/p1"]["status"] == "cancelled"
    assert world.queue()["demo/p2"]["status"] == "running"


def test_local_item_wins(world):
    world.set_queue({"demo/p1": {"repo": "demo", "slug": "p1", "status": "queued", "attempts": 0,
                                 "not_before": "2026-10-26T00:00:00+00:00", "notes": []}})
    world.push({"agents/plans/p1.md": plan("no")})
    assert world.scan() == []
    assert world.queue()["demo/p1"]["status"] == "queued"


def test_done_and_finished_plans_skipped(world):
    world.push({"agents/plans/done/p1.md": plan(), "agents/plans/p2.md": plan(status="✅ Done (2026-10-20)"),
                "agents/plans/p3.md": plan(queue="", extra="- **Queue:** one queue.json for every repo\n")})
    assert world.scan() == []
    assert world.queue() == {}
    assert world.notices() == []


def test_oversize_plan_refused_once_per_version(world):
    world.push({"agents/plans/big.md": plan(tasks=5)})
    assert world.scan() == ["intake: refused demo/big: 5 tasks > budget 4"]
    assert world.notices() == ["[demo/big] not queued: 5 tasks > budget 4"]
    assert world.scan() == []
    assert len(world.notices()) == 1
    world.push({"agents/plans/big.md": plan(tasks=5, extra="more")})
    assert world.scan() == ["intake: refused demo/big: 5 tasks > budget 4"]
    assert len(world.notices()) == 2
    world.push({"agents/plans/big.md": plan(tasks=2)})
    assert world.scan()[0].startswith("intake: queued demo/big")


def test_failed_notice_is_retried(world):
    world.push({"agents/plans/big.md": plan(tasks=5)})
    world.env["GH_FAIL"] = "1"
    out = world.scan()
    assert out[0] == "intake: refused demo/big: 5 tasks > budget 4"
    assert out[1].startswith("intake: notice for demo/big failed")
    del world.env["GH_FAIL"]
    assert world.scan() == ["intake: refused demo/big: 5 tasks > budget 4"]
    assert world.notices() == ["[demo/big] not queued: 5 tasks > budget 4"]
    assert world.scan() == []


def test_other_refusals(world):
    world.push({"agents/plans/naive.md": plan("yes at 2026-10-27T02:00"),
                "agents/plans/orphan.md": plan("yes after nope"),
                "agents/plans/norisk.md": plan(risk=""),
                "agents/plans/notasks.md": plan(tasks=0)})
    out = world.scan()
    assert out == [
        "intake: refused demo/naive: at 2026-10-27T02:00 has no timezone (add Z or an offset)",
        "intake: refused demo/norisk: no **Risk:** line",
        "intake: refused demo/notasks: no unattended task left to run",
        "intake: refused demo/orphan: after demo/nope is not queued",
    ]
    assert world.queue() == {}


def test_after_in_the_same_scan(world):
    world.push({"agents/plans/a2.md": plan("yes after a1"), "agents/plans/a1.md": plan()})
    out = world.scan()
    assert [line.split(" (")[0] for line in out] == ["intake: queued demo/a1", "intake: queued demo/a2"]
    assert world.queue()["demo/a2"]["after"] == "demo/a1"


def test_dry_run_writes_nothing(world):
    world.push({"agents/plans/p1.md": plan(), "agents/plans/big.md": plan(tasks=5)})
    out = world.scan("--dry-run")
    assert [line.split(" (")[0].split(":")[1] for line in out] == [" queued demo/p1", " refused demo/big"]
    assert not world.state.exists()
    assert world.notices() == []


def test_failed_fetch_goes_on(world, tmp_path):
    other = tmp_path / "other"
    sh("git", "clone", "-q", str(world.origin), str(other))
    sh("git", "-C", str(other), "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    (tmp_path / "home/.config/agent-runner/repos/aaa.env").write_text(f"AGENT_REPO={other}\n")
    world.push({"agents/plans/p1.md": plan()})
    out = world.scan()
    assert out[0].startswith("intake: aaa: git fetch failed")
    assert out[1].startswith("intake: queued demo/p1")


def test_unreadable_queue_exits_1(world):
    world.state.mkdir()
    (world.state / "queue.json").write_text("{not json")
    out = subprocess.run([sys.executable, str(ROOT / "intake.py"), "scan"], env=world.env, capture_output=True,
                         text=True)
    assert out.returncode == 1
