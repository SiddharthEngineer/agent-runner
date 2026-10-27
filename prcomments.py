#!/usr/bin/env python3
"""`/fix` PR comments: the user sends a runner PR back to the implementer from the GitHub app.

Usage:
  prcomments.py scan [--dry-run]   each tick (run.sh upkeep): re-queue items whose PR has a new `/fix` comment

A comment counts when the runner's own account wrote it (`viewerDidAuthor`: the runner opens PRs as the user) and its body
starts with `/fix` at column 0 (validators post as the same account and never start a comment that way). New ones (after the
item's `fix_comment_at`, default its `added`) re-queue the item as a user fix round (`buildqueue.py requeue --fix --stage
user`: no round, no attempt), with the text after `/fix` as the note, and get a "Queued" reply. On a merged or closed PR they
get one reply saying to plan a follow-up (also for a day after an item is done or cancelled: a /fix made during its run). A `gh` failure logs one line and leaves the item alone.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buildqueue  # noqa: E402
import config  # noqa: E402
import prstatus  # noqa: E402

# Not `running`: a /fix written during a run is picked up once the run has finished (its time is after `fix_comment_at`).
STATUSES = ("awaiting-review", "awaiting-preprod", "on-preprod", "blocked", "queued")
# A run can also end in a merge: for a day after, a /fix on a finished item still gets its "plan a follow-up" reply.
FINISHED_STATUSES = ("done", "cancelled")
FIX = re.compile(r"/fix(?:\s|$)")
QUEUED = "Queued: the implementer picks this up on the next free tick."
FINISHED_REPLY = "This PR is {state}; plan a follow-up instead."


def log(msg: str) -> None:
    print(f"prcomments: {msg}")


def when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def new_fixes(comments: list, since: str) -> list:
    """The viewer's `/fix` comments after <since>, oldest first."""
    found = [c for c in comments if c.get("viewerDidAuthor") and FIX.match(c.get("body") or "")
             and when(c["createdAt"]) > when(since)]
    return sorted(found, key=lambda c: when(c["createdAt"]))


def scan_item(key: str, it: dict, *, dry_run: bool) -> None:
    repo = key.partition("/")[0]
    cfg = prstatus.repo_config(repo)
    if cfg is None:
        return  # prstatus.py sync logs this every tick already
    pr, gh_repo = str(it["pr"]), cfg.repo.github
    try:
        view = json.loads(prstatus.gh("pr", "view", pr, "--repo", gh_repo, "--json", "state,comments"))
        fixes = new_fixes(view.get("comments") or [], it.get("fix_comment_at") or it["added"])
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError) as exc:
        log(f"{key}: gh pr view {gh_repo}#{pr} failed: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}")
        return
    if not fixes:
        return
    newest = fixes[-1]["createdAt"]
    state = view.get("state", "")
    if state in ("MERGED", "CLOSED"):
        reply = FINISHED_REPLY.format(state=state.lower())
        if dry_run:
            log(f"{key}: would reply on {gh_repo}#{pr}: {reply}")
            return
        set_fields(key, fix_comment_at=newest)
        comment(key, gh_repo, pr, reply)
        return
    if it.get("status") not in STATUSES:
        return  # a finished item whose PR is still open: nothing to send back
    note = "\n\n".join(FIX.sub("", c["body"], count=1).strip() or "(no text)" for c in fixes)
    if dry_run:
        log(f"{key}: would re-queue for {len(fixes)} /fix comment(s) on {gh_repo}#{pr}: {note.splitlines()[0][:140]}")
        return
    # Re-read the queue right before the change: the gh call took time, and the item may have moved on.
    data = buildqueue.load()
    it = data["items"].get(key)
    if not it or it.get("status") not in STATUSES:
        return
    was = it["status"]
    it["fix_comment_at"] = newest
    buildqueue.user_fix(it, note)
    buildqueue.save(data)
    log(f"{key}: /fix on {gh_repo}#{pr}, re-queued from {was}")
    if was == "awaiting-preprod":
        try:
            prstatus.gh("pr", "edit", pr, "--repo", gh_repo, "--remove-label", "awaiting-preprod")
        except (subprocess.SubprocessError, OSError):
            pass  # only the label: preprod.sh deploys by queue status
    comment(key, gh_repo, pr, QUEUED)


def set_fields(key: str, **fields: str) -> None:
    data = buildqueue.load()
    data["items"][key].update(fields)
    buildqueue.save(data)


def comment(key: str, gh_repo: str, pr: str, body: str) -> None:
    try:
        prstatus.gh("pr", "comment", pr, "--repo", gh_repo, "--body", body)
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"{key}: gh pr comment {gh_repo}#{pr} failed: {type(exc).__name__}")


def due(it: dict) -> bool:
    if not it.get("pr"):
        return False
    if it.get("status") in FINISHED_STATUSES:
        return when(it["updated"]) > buildqueue.now() - timedelta(days=1)
    return it.get("status") in STATUSES


def cmd_scan(args: argparse.Namespace) -> int:
    for key, it in sorted(buildqueue.load()["items"].items()):
        if not due(it):
            continue
        try:
            scan_item(key, it, dry_run=args.dry_run)
        except Exception as exc:  # one bad item never stops the others, or the tick
            log(f"{key}: skipped ({type(exc).__name__}: {exc})")
    return 0


def main() -> int:
    config.load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_scan)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
