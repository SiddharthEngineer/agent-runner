"""prcomments.py scan: a queue in a temp state dir, the repo's runner.toml in configs/, gh a stub that logs and replays a PR."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

GH = """#!/usr/bin/env bash
echo "gh $*" >> "$STUB_LOG"
if [[ "$*" == "pr view"* ]]; then
  [ -n "${STUB_GH_FAIL:-}" ] && { echo "HTTP 502" >&2; exit 1; }
  cat "$STUB_VIEW"
fi
"""
ADDED = "2026-10-20T00:00:00+00:00"


@pytest.fixture
def cli(tmp_path):
    state = tmp_path / "state"
    (state / "configs").mkdir(parents=True)
    (state / "configs/engineerfamily.toml").write_text(
        '[repo]\nname = "engineerfamily"\ngithub = "o/engineerfamily"\nbase = "main"\nmerge = "merge"\n'
        '[validate]\nfast = "true"\nfull = "true"\n[gate]\nhigh_risk = []\ndoc_or_test = []\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(GH)
    (bin_dir / "gh").chmod(0o755)
    log, view = tmp_path / "gh.log", tmp_path / "view.json"

    def queue(items: dict) -> None:
        (state / "queue.json").write_text(json.dumps({"paused_until": None, "items": items}))

    def run(*args, comments=(), state_="OPEN", **env):
        view.write_text(json.dumps({"state": state_, "comments": list(comments)}))
        e = {**os.environ, "AGENT_RUNNER_STATE": str(state), "HOME": str(tmp_path), "STUB_LOG": str(log),
             "STUB_VIEW": str(view), "PATH": f"{bin_dir}:{os.environ['PATH']}", **env}
        return subprocess.run([sys.executable, ROOT / "prcomments.py", *args], env=e, capture_output=True, text=True)

    run.queue = queue
    run.calls = lambda: log.read_text().splitlines() if log.exists() else []
    run.items = lambda: json.loads((state / "queue.json").read_text())["items"]
    return run


def item(**fields):
    base = {"plan": "agents/plans/x.md", "status": "awaiting-review", "not_before": ADDED, "added": ADDED,
            "attempts": 1, "notes": [], "updated": ADDED, "pr": 9}
    return {**base, **fields}


def comment(body, at="2026-10-21T10:00:00Z", mine=True):
    return {"body": body, "createdAt": at, "viewerDidAuthor": mine}


def replies(cli):
    return [c for c in cli.calls() if c.startswith("gh pr comment")]


def test_new_fix_requeues_once(cli):
    cli.queue({"engineerfamily/x": item()})
    fix = [comment("/fix the Users table should sort by email")]
    out = cli("scan", comments=fix)
    assert out.returncode == 0, out.stderr
    it = cli.items()["engineerfamily/x"]
    assert it["status"] == "queued" and it["fix_pending"] == 1 and it["resume"] == 1
    assert it["fix_comment_at"] == "2026-10-21T10:00:00Z"
    assert it["notes"][-1].startswith("User /fix (") and it["notes"][-1].endswith("): the Users table should sort by email")
    assert replies(cli) == ["gh pr comment 9 --repo o/engineerfamily --body Queued: the implementer picks this up on the next free tick."]
    assert "engineerfamily/x: /fix on o/engineerfamily#9, re-queued from awaiting-review" in out.stdout
    cli("scan", comments=fix)  # the same comment again: nothing new
    assert len(replies(cli)) == 1 and len(cli.items()["engineerfamily/x"]["notes"]) == 1


def test_comments_that_dont_count_are_ignored(cli):
    cli.queue({"engineerfamily/x": item()})
    out = cli("scan", comments=[comment("please /fix this"), comment(" /fix indented"), comment("/fixed it"),
                                comment("/fix not mine", mine=False), comment("/fix before the item", at="2026-10-19T00:00:00Z")])
    assert out.returncode == 0, out.stderr
    assert cli.items()["engineerfamily/x"]["status"] == "awaiting-review" and not replies(cli)


def test_blocked_item_goes_back_to_queued_with_rounds_unchanged(cli):
    cli.queue({"engineerfamily/x": item(status="blocked", fix_rounds=3, blocked_why="rounds", last_unresolved=["a"])})
    cli("scan", comments=[comment("/fix do it this way")])
    it = cli.items()["engineerfamily/x"]
    assert it["status"] == "queued" and it["fix_rounds"] == 3 and it["attempts"] == 1
    assert "blocked_why" not in it and "last_unresolved" not in it


def test_several_new_fixes_are_one_requeue_oldest_first(cli):
    cli.queue({"engineerfamily/x": item(status="awaiting-preprod")})
    cli("scan", comments=[comment("/fix second", at="2026-10-21T11:00:00Z"), comment("/fix first")])
    it = cli.items()["engineerfamily/x"]
    assert it["notes"][-1].endswith("): first\n\nsecond") and it["fix_comment_at"] == "2026-10-21T11:00:00Z"
    assert "gh pr edit 9 --repo o/engineerfamily --remove-label awaiting-preprod" in cli.calls()
    assert len(replies(cli)) == 1


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_merged_or_closed_pr_gets_one_reply(cli, state):
    cli.queue({"engineerfamily/x": item()})
    fix = [comment("/fix too late")]
    cli("scan", comments=fix, state_=state)
    cli("scan", comments=fix, state_=state)
    assert replies(cli) == [f"gh pr comment 9 --repo o/engineerfamily --body This PR is {state.lower()}; plan a follow-up instead."]
    it = cli.items()["engineerfamily/x"]
    assert it["status"] == "awaiting-review" and it["fix_comment_at"] == "2026-10-21T10:00:00Z" and not it["notes"]


def test_running_done_and_prless_items_are_skipped(cli):
    cli.queue({"engineerfamily/r": item(status="running"), "engineerfamily/d": item(status="done"),
               "engineerfamily/n": item(pr=None, status="queued")})
    cli("scan", comments=[comment("/fix x")])
    assert cli.calls() == []
    assert cli.items()["engineerfamily/r"]["status"] == "running"


def test_a_fix_made_during_a_run_that_merged_gets_the_reply(cli):
    recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cli.queue({"engineerfamily/x": item(status="done", updated=recent),
               "engineerfamily/o": item(status="cancelled", updated=recent, pr=10)})
    cli("scan", comments=[comment("/fix x")], state_="MERGED")
    assert "gh pr comment 9 --repo o/engineerfamily --body This PR is merged; plan a follow-up instead." in replies(cli)
    assert cli.items()["engineerfamily/x"]["status"] == "done"
    cli.queue({"engineerfamily/o": item(status="cancelled", updated=recent, pr=10)})
    cli("scan", comments=[comment("/fix x")])  # a cancelled item whose PR is still open: nothing to send back
    assert len(replies(cli)) == 2 and cli.items()["engineerfamily/o"]["status"] == "cancelled"


def test_gh_failure_logs_one_line_and_leaves_the_item(cli):
    cli.queue({"engineerfamily/x": item()})
    out = cli("scan", comments=[comment("/fix x")], STUB_GH_FAIL="1")
    assert out.returncode == 0
    assert out.stdout.count("gh pr view o/engineerfamily#9 failed") == 1
    assert cli.items()["engineerfamily/x"] == item()


def test_dry_run_writes_nothing(cli):
    cli.queue({"engineerfamily/x": item()})
    out = cli("scan", "--dry-run", comments=[comment("/fix x")])
    assert "would re-queue for 1 /fix comment(s) on o/engineerfamily#9: x" in out.stdout
    assert cli.items()["engineerfamily/x"] == item() and not replies(cli)
