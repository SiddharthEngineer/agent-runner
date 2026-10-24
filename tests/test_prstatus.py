"""Tests for prstatus.py: the status block per stage, `apply`, and the gh calls of `update`/`sync` (gh is a stub)."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import prstatus  # noqa: E402

NOW = datetime(2026, 10, 22, 16, 40, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def two_attempts(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("AGENT_INBOX_REPO", "o/inbox")


def block(steps=7, preprod=True, **item):
    item.setdefault("pr", 9)
    item.setdefault("attempts", 1)
    return prstatus.render(item, has_preprod=preprod, human_steps=steps, now=NOW)


def lines(text):
    return text.splitlines()


def test_shape_and_markers():
    out = lines(block(status="awaiting-review", tier="review"))
    assert out[0] == prstatus.START and out[-1] == prstatus.END
    assert out[1].startswith("**Runner status:** 🔁 Validator review: queued")
    assert out[1].endswith("· updated 2026-10-22 16:40 UTC")
    assert out[2] == "1. ✅ Implementer opened the PR, 7 Human test steps"
    assert out[3].startswith("2. 🔁 Validator reviews the code")
    assert [line[:4] for line in out[4:7]] == ["3. ⏳", "4. ⏳", "5. ⏳"]
    assert '"merged; N steps left for you" issue' in out[6]


def test_low_tier_merged():
    out = lines(block(status="done", tier="low"))
    assert out[1].startswith("**Runner status:** ✅ Merged ·")
    assert all("➖" in line and "`low` tier merges on green CI" in line for line in out[3:6])
    assert out[6].startswith("5. ✅")


def test_low_tier_not_merged_holds_at_merge():
    out = lines(block(status="blocked", tier="low", notice="https://github.com/o/inbox/issues/5",
                      last_result={"summary": "PR #9 not merged: CI is red or timed out"}))
    assert out[1].startswith("**Runner status:** ⏸ Merge: blocked: PR #9 not merged: CI is red or timed out "
                             "(https://github.com/o/inbox/issues/5)")
    assert out[6].startswith("5. ⏸")


def test_review_changes_rerun():
    item = dict(status="queued", tier="review", rerun="review", review_verdict="changes",
                review_summary="Task 3 permissions are too wide. Also a nit.",
                notes=["Validator (x): Validator review of PR #9 asks for changes: Task 3 permissions are too wide. Also"])
    out = lines(block(**item))
    assert out[1].startswith("**Runner status:** 🔁 Validator review: asked for changes (Task 3 permissions are too wide); "
                             "implementer re-run queued, attempt 2/2")
    assert out[2].startswith("1. ✅")
    assert out[3].startswith("2. 🔁 Validator reviews the code: asked for changes: Task 3 permissions are too wide;")
    running = lines(block(**{**item, "status": "running", "attempts": 2}))
    assert "implementer fixing it now, attempt 2/2" in running[1]


def test_rerun_note_is_cut_to_140():
    out = block(status="queued", rerun="review", notes=["Validator review of PR #9 asks for changes: " + "x" * 300])
    assert "x" * 139 + "…" in out and "x" * 140 not in out


def test_review_approved_waits_for_preprod():
    out = lines(block(status="awaiting-preprod", tier="review", review_verdict="approve"))
    assert out[1].startswith("**Runner status:** 🔁 Approved; waiting for preprod")
    assert out[3].startswith("2. ✅") and out[4].startswith("3. 🔁") and out[5].startswith("4. ⏳")


def test_on_preprod():
    out = lines(block(status="on-preprod", tier="review", review_verdict="approve"))
    assert "🔁 On preprod" in out[1]
    assert out[4].startswith("3. ✅") and out[5].startswith("4. 🔁")


def test_preprod_approved_not_merged():
    out = lines(block(status="on-preprod", review_verdict="approve", preprod_verdict="approve"))
    assert out[1].startswith("**Runner status:** ❌ Passed preprod, not merged")
    assert out[5].startswith("4. ✅") and out[6].startswith("5. ❌")


def test_preprod_changes_rerun():
    out = lines(block(status="queued", tier="review", review_verdict="approve", rerun="preprod",
                      notes=["Validator (x): Preprod checks of PR #9 failed: the page is blank.\nFailed steps:\n- 3"]))
    assert out[1].startswith("**Runner status:** 🔁 Preprod checks failed (the page is blank); implementer re-run queued,"
                             " attempt 2/2")
    assert out[3].startswith("2. ✅") and out[4].startswith("3. ✅") and out[5].startswith("4. 🔁")


def test_merged_with_steps_left():
    out = lines(block(status="done", tier="review", review_verdict="approve", preprod_verdict="approve",
                      preprod_comment="https://github.com/o/r/pull/9#issuecomment-1", left_count=2,
                      left_issue="https://github.com/o/inbox/issues/7"))
    assert out[1].startswith("**Runner status:** ✅ Merged; 2 steps left for you (https://github.com/o/inbox/issues/7)")
    assert all(line[3] == "✅" for line in out[2:7])
    assert "passed (https://github.com/o/r/pull/9#issuecomment-1)" in out[5]


def test_merged_nothing_left():
    out = lines(block(status="done", tier="review", review_verdict="approve", left_count=0))
    assert "✅ Merged; nothing left for you" in out[1]


def test_summary_mentioning_by_hand_is_not_merged_by_hand():
    out = lines(block(status="done", tier="low", last_result={"summary": "Steps 3-4 must be done by hand."}))
    assert out[1].startswith("**Runner status:** ✅ Merged ·")


def test_merged_by_hand():
    out = lines(block(status="done", tier="review", last_result={"summary": "PR #9 merged by hand"}))
    assert "✅ Merged by hand" in out[1]
    assert out[3].startswith("2. ➖")


@pytest.mark.parametrize("fields, at, why", [
    (dict(review_verdict="blocked", review_summary="Can't run the tests."), 3, "Validator review: blocked: Can't run the tests"),
    (dict(review_verdict="approve", preprod_verdict="blocked", preprod_summary="Login 500s."), 5,
     "Preprod checks: blocked: Login 500s"),
    (dict(review_verdict="approve", rerun="preprod", review_attempts=2), 3, "Validator review: blocked"),
    (dict(last_result={"summary": "Missing access."}), 2, "Implementer: blocked: Missing access"),
    (dict(review_verdict="approve", deploy_failed="health failed: https://x/ returned 401"), 4,
     "Preprod deploy: blocked: health failed: https://x/ returned 401"),
])
def test_blocked_shows_where_and_the_issue(fields, at, why):
    out = lines(block(status="blocked", tier="review", notice="https://github.com/o/inbox/issues/3", issue=1, **fields))
    assert out[1].startswith(f"**Runner status:** ⏸ {why}"), out[1]
    assert "(https://github.com/o/inbox/issues/3)" in out[1]
    assert out[at].startswith(f"{at - 1}. ⏸")
    assert all(line[3] == "✅" for line in out[2:at])


def test_question_links_the_inbox_issue_by_number():
    out = lines(block(status="waiting-answer", issue=12))
    assert "⏸ Implementer: waiting for your answer (https://github.com/o/inbox/issues/12)" in out[1]


def test_no_preprod_repo():
    out = lines(block(preprod=False, status="awaiting-review", tier="review"))
    assert out[4].startswith("3. ➖") and out[5].startswith("4. ➖") and "no `[preprod]`" in out[4]
    assert out[6] == "5. ⏳ Merge to main"
    done = lines(block(preprod=False, status="done", tier="review", review_verdict="approve", steps=0))
    assert done[2] == "1. ✅ Implementer opened the PR, 0 Human test steps"
    assert done[3].startswith("2. ✅") and done[6].startswith("5. ✅")


def test_no_human_test_steps_warns_with_preprod():
    assert "1. ✅ Implementer opened the PR, ⚠️ no Human test steps" in block(steps=0, status="awaiting-review")


def test_human_steps_counts_the_heading_only():
    body = "## Summary\n1. one\n2. two\n\n## Human test steps\n1. open it\n2. click\n   - detail\n3. done\n\n## Validation\n1. x\n"
    assert prstatus.human_steps(body) == 3
    assert prstatus.human_steps("**Human test steps**\n1. a\n2) b\n") == 2
    assert prstatus.human_steps("no steps") == 0


def test_apply_puts_the_block_first_then_replaces_it():
    body = "## Summary\nhello\n\n<!-- a comment -->\n"
    one = prstatus.apply(body, block(status="awaiting-review"))
    assert one.startswith(prstatus.START) and one.endswith("\n\n" + body)
    two = prstatus.apply(one, block(status="awaiting-preprod", review_verdict="approve"))
    assert two.count(prstatus.START) == 1 and two.endswith("\n\n" + body) and "waiting for preprod" in two
    assert prstatus.apply(two, block(status="awaiting-preprod", review_verdict="approve")) == two
    assert prstatus.apply("", "B") == "B\n"


def test_same_ignores_the_updated_time():
    a = block(status="awaiting-review")
    b = prstatus.render({"pr": 9, "attempts": 1, "status": "awaiting-review"}, has_preprod=True, human_steps=7,
                        now=datetime(2026, 10, 23, tzinfo=timezone.utc))
    assert a != b and prstatus.same(a, b)
    assert not prstatus.same(a, block(status="awaiting-preprod"))


# The CLI: a queue in the temp state dir, the repo's runner.toml in configs/, gh a stub that logs and replays a PR body.

GH = """#!/usr/bin/env bash
echo "gh $*" >> "$STUB_LOG"
if [[ "$*" == "pr view"* ]]; then
  [ -n "${STUB_GH_FAIL:-}" ] && { echo "HTTP 502" >&2; exit 1; }
  cat "$STUB_VIEW"
elif [[ "$*" == "pr edit"* ]]; then
  cp "${@: -1}" "$STUB_LOG.body"
fi
"""


@pytest.fixture
def cli(tmp_path):
    state = tmp_path / "state"
    (state / "configs").mkdir(parents=True)
    (state / "configs/engineerfamily.toml").write_text(
        '[repo]\nname = "engineerfamily"\ngithub = "o/engineerfamily"\nbase = "main"\nmerge = "merge"\n'
        '[validate]\nfast = "true"\nfull = "true"\n[gate]\nhigh_risk = []\ndoc_or_test = []\n'
        '[preprod]\ndeploy = "true"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(GH)
    (bin_dir / "gh").chmod(0o755)
    log, view = tmp_path / "gh.log", tmp_path / "view.json"

    def queue(items: dict) -> None:
        (state / "queue.json").write_text(json.dumps({"paused_until": None, "items": items}))

    def run(*args, body="## Human test steps\n1. a\n2. b\n", state_="OPEN", merge_commit=None, **env):
        view.write_text(json.dumps({"body": body, "state": state_, "mergeCommit": merge_commit and {"oid": merge_commit}}))
        e = {**os.environ, "AGENT_RUNNER_STATE": str(state), "HOME": str(tmp_path), "STUB_LOG": str(log),
             "STUB_VIEW": str(view), "PATH": f"{bin_dir}:{os.environ['PATH']}", **env}
        return subprocess.run([sys.executable, ROOT / "prstatus.py", *args], env=e, capture_output=True, text=True)

    run.queue = queue
    run.calls = lambda: log.read_text().splitlines() if log.exists() else []
    run.body = lambda: Path(f"{log}.body").read_text()
    run.items = lambda: json.loads((state / "queue.json").read_text())["items"]
    run.state, run.home = state, tmp_path
    return run


def item(**fields):
    base = {"plan": "agents/plans/x.md", "status": "awaiting-review", "not_before": "2026-10-22T00:00:00+00:00",
            "attempts": 1, "notes": [], "updated": prstatus.buildqueue.iso(prstatus.buildqueue.now()), "pr": 9}
    return {**base, **fields}


def test_update_edits_once_then_not_again(cli):
    cli.queue({"engineerfamily/x": item(tier="review")})
    out = cli("update", "engineerfamily/x")
    assert out.returncode == 0, out.stderr
    assert cli.calls() == ["gh pr view 9 --repo o/engineerfamily --json body,state,mergeCommit",
                           f"gh pr edit 9 --repo o/engineerfamily --body-file {cli.calls()[1].rsplit(' ', 1)[1]}"]
    body = cli.body()
    assert body.startswith(prstatus.START) and "2 Human test steps" in body and body.endswith("## Human test steps\n1. a\n2. b\n")
    out = cli("update", "engineerfamily/x", body=body)  # the PR now carries the block: nothing to edit
    assert [c for c in cli.calls() if c.startswith("gh pr edit")].__len__() == 1


def test_gh_failure_logs_one_line_and_exits_0(cli):
    cli.queue({"engineerfamily/x": item(), "engineerfamily/y": item()})
    out = cli("sync", STUB_GH_FAIL="1")
    assert out.returncode == 0
    assert out.stdout.count("gh pr view o/engineerfamily#9 failed") == 2


def test_sync_picks_items_and_finalises_done(cli):
    old = "2026-09-01T00:00:00+00:00"
    cli.queue({
        "engineerfamily/a": item(),
        "engineerfamily/b": item(status="cancelled"),
        "engineerfamily/c": item(pr=None),
        "engineerfamily/d": item(status="done", tier="low"),
        "engineerfamily/e": item(status="done", tier="low", updated=old),
        "engineerfamily/f": item(status="done", tier="low", pr_status_final=1),
        "engineerfamily/h": item(status="blocked", updated=old),
        "engineerfamily/i": item(updated="not a date"),  # one bad item doesn't stop the rest
        "other/g": item(),  # no runner.toml for this repo yet
    })
    out = cli("sync")
    assert out.returncode == 0, out.stderr
    assert len([c for c in cli.calls() if c.startswith("gh pr view")]) == 2  # a and d
    assert "other/g: no runner.toml" in out.stdout and "engineerfamily/i: skipped (ValueError" in out.stdout
    items = cli.items()
    assert items["engineerfamily/d"].get("pr_status_final") == 1 and items["engineerfamily/d"]["updated"] != old
    assert "pr_status_final" not in items["engineerfamily/a"]


def test_sync_dry_run_only_prints(cli):
    cli.queue({"engineerfamily/x": item()})
    out = cli("sync", "--dry-run")
    assert "engineerfamily/x: would edit o/engineerfamily#9: **Runner status:** 🔁" in out.stdout
    assert not any(c.startswith("gh pr edit") for c in cli.calls())


def test_closed_pr_is_left_alone(cli):
    cli.queue({"engineerfamily/x": item()})
    cli("update", "engineerfamily/x", state_="CLOSED")
    assert not any(c.startswith("gh pr edit") for c in cli.calls())


def test_merged_shows_its_release():
    out = lines(prstatus.render({"pr": 9, "attempts": 1, "status": "done", "tier": "low"}, has_preprod=True,
                                human_steps=7, now=NOW, released="v1.4.4 (pre-release v1.4)"))
    assert out[1].startswith("**Runner status:** ✅ Merged; released in v1.4.4 (pre-release v1.4) ·")
    assert out[6].startswith("5. ✅") and out[6].endswith(": merged; released in v1.4.4 (pre-release v1.4)")


def test_release_repo_block_is_final_once_tagged(cli):
    toml = cli.state / "configs/engineerfamily.toml"
    toml.write_text(toml.read_text() + "[release]\n")
    clone = cli.home / "clone"
    git = ["git", "-C", str(clone), "-c", "user.email=t@e", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "pr 9"], check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    (cli.home / ".config/agent-runner/repos").mkdir(parents=True)
    (cli.home / ".config/agent-runner/repos/engineerfamily.env").write_text(f"AGENT_REPO={clone}\n")
    cli.queue({"engineerfamily/x": item(status="done", tier="low")})
    assert cli("update", "engineerfamily/x", merge_commit=sha).returncode == 0
    assert "released in" not in cli.body() and "pr_status_final" not in cli.items()["engineerfamily/x"]
    subprocess.run([*git, "tag", "-a", "v1.4.4", "-m", "v1.4.4"], check=True)
    subprocess.run([*git, "tag", "v1.4.5"], check=True)
    assert cli("update", "engineerfamily/x", merge_commit=sha).returncode == 0
    assert "Merged; released in v1.4.4 (pre-release v1.4)" in cli.body()
    assert cli.items()["engineerfamily/x"]["pr_status_final"] == 1
