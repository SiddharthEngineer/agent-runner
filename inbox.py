#!/usr/bin/env python3
"""Agent ↔ user inbox: issues in a private GitHub repo, plus an optional ntfy push.

Usage:
  inbox.py ask <key> --title T --body-file F    open an `agent-question` issue; the item waits for your reply
  inbox.py notify --title T --body B [--slug K] [--project P] [--json]
                                                 open an `agent-notice` issue (PR on preprod, item blocked, …)
  inbox.py comment <n> --body B [--close]        comment on an inbox issue (and close it)
  inbox.py poll                                  re-queue items whose question issue has a reply (cron, no Claude)
  inbox.py sweep [--dry-run]                     close `agent-notice` issues whose item is done or cancelled (cron, no Claude)

<key> is a queue key (`<repo>/<slug>`, or a bare slug only one repo has). Issues are labelled with the item's repo.

Config (~/.config/agent-runner/env, or the environment):
  AGENT_INBOX_REPO  owner/name of the private inbox repo (required)
  NTFY_TOPIC        optional ntfy.sh topic. GitHub doesn't push-notify you about issues your own account
                    opens, so without this you only see them in the app's inbox.
Never put personal data, secrets or job data in a title or body: link to files on the VPS instead.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config  # noqa: E402

MARKER = "<!-- agent-runner -->"  # tags comments the agent wrote, so any other comment is your answer
LEGACY_MARKERS = ("<!-- strelitzia-agent -->", "<!-- applypilot-agent -->")  # on comments written before the renames
PROJECT_LABEL = "strelitzia"
load_env = config.load_env


def by_agent(body: str) -> bool:
    return any(marker in body for marker in (MARKER, *LEGACY_MARKERS))


def queue_item(ref: str) -> tuple[str, dict] | None:
    """(full key, item) for a queue key or bare slug, or None if it isn't queued (or is ambiguous)."""
    import buildqueue  # same directory

    data = buildqueue.load()
    try:
        key = buildqueue.resolve(data, ref)
    except SystemExit:
        return None
    return key, data["items"][key]


def repo() -> str:
    value = os.environ.get("AGENT_INBOX_REPO")
    if not value:
        sys.exit("AGENT_INBOX_REPO is not set (see scripts/agent/README.md)")
    return value


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout.strip()


def buildqueue(*args: str) -> None:
    subprocess.run([sys.executable, str(HERE / "buildqueue.py"), *args], check=True)


def push(title: str, url: str) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=title.encode(), headers={"Click": url, "Title": "Strelitzia agent"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except OSError as exc:  # a missed push must never fail the run
        print(f"ntfy push failed: {exc}", file=sys.stderr)


def open_issue(title: str, body: str, label: str, project: str = PROJECT_LABEL) -> tuple[int, str]:
    for name in (label, project):
        gh("label", "create", name, "--repo", repo(), "--force")
    url = gh("issue", "create", "--repo", repo(), "--title", title, "--body", f"{body}\n\n{MARKER}",
             "--label", label, "--label", project)
    push(title, url)
    return int(url.rstrip("/").rsplit("/", 1)[1]), url


def cmd_ask(args: argparse.Namespace) -> int:
    found = queue_item(args.slug)
    if found is None:
        sys.exit(f"not queued (or ambiguous): {args.slug}")
    key, it = found
    number, url = open_issue(f"[{key}] {args.title}", args.body_file.read_text(), "agent-question",
                             it.get("repo") or PROJECT_LABEL)
    buildqueue("set", key, f"issue={number}", "status=waiting-answer")
    print(url)
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    title = f"[{args.slug}] {args.title}" if args.slug else args.title
    project = args.project
    if project is None:
        found = queue_item(args.slug) if args.slug else None
        project = (found[1].get("repo") if found else None) or PROJECT_LABEL
    number, url = open_issue(title, args.body, "agent-notice", project)
    print(json.dumps({"number": number, "url": url}) if args.json else url)
    return 0


def cmd_comment(args: argparse.Namespace) -> int:
    """Comment on an inbox issue (and close it with --close), with a push."""
    url = gh("issue", "comment", str(args.number), "--repo", repo(), "--body", f"{args.body}\n\n{MARKER}")
    if args.close:
        gh("issue", "close", str(args.number), "--repo", repo())
    push(args.body.splitlines()[0][:120] if args.body else f"issue #{args.number}", url)
    print(url)
    return 0


def cmd_poll(_: argparse.Namespace) -> int:
    from buildqueue import load  # same directory

    for slug, it in load()["items"].items():
        if it["status"] != "waiting-answer" or not it.get("issue"):
            continue
        issue = json.loads(gh("issue", "view", str(it["issue"]), "--repo", repo(), "--json", "comments"))
        replies = [c["body"] for c in issue["comments"] if not by_agent(c["body"])]
        if not replies:
            continue
        buildqueue("answer", slug, "\n\n".join(replies))
        gh("issue", "comment", str(it["issue"]), "--repo", repo(), "--body", f"Got it, re-queued `{slug}`.\n\n{MARKER}")
        gh("issue", "close", str(it["issue"]), "--repo", repo())
        print(f"re-queued {slug} with your answer")
    return 0


FINISHED = {"done", "cancelled"}
STEPS_LEFT = re.compile(r"merged; ([1-9]\d*) steps left for you")  # your to-do after a merge: you close it


def cmd_sweep(args: argparse.Namespace) -> int:
    """Close the notices of finished items: `[<key>] …` titles whose queue item is done or cancelled."""
    from buildqueue import load, resolve  # same directory

    data = load()
    issues = json.loads(gh("issue", "list", "--repo", repo(), "--label", "agent-notice", "--state", "open",
                           "--limit", "500", "--json", "number,title"))
    for issue in issues:
        m = re.match(r"\[([^\]]+)\]", issue["title"])
        if not m or STEPS_LEFT.search(issue["title"]):
            continue
        try:
            key = resolve(data, m.group(1))
        except SystemExit:  # not a queue item ([vps] health notices) or ambiguous
            continue
        status = data["items"][key]["status"]
        if status not in FINISHED:
            continue
        if not args.dry_run:
            gh("issue", "comment", str(issue["number"]), "--repo", repo(),
               "--body", f"`{key}` is {status}, closing.\n\n{MARKER}")
            gh("issue", "close", str(issue["number"]), "--repo", repo())
        print(f"{'would close' if args.dry_run else 'closed'} #{issue['number']} ({key} is {status})")
    return 0


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description="Strelitzia agent inbox")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ask")
    p.add_argument("slug")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", type=Path, required=True)
    p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("notify")
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--slug")
    p.add_argument("--project", help=f"project label (default: the --slug item's repo, else {PROJECT_LABEL})")
    p.add_argument("--json", action="store_true", help='print {"number", "url"} instead of the URL')
    p.set_defaults(fn=cmd_notify)
    p = sub.add_parser("comment")
    p.add_argument("number", type=int)
    p.add_argument("--body", required=True)
    p.add_argument("--close", action="store_true")
    p.set_defaults(fn=cmd_comment)
    sub.add_parser("poll").set_defaults(fn=cmd_poll)
    p = sub.add_parser("sweep")
    p.add_argument("--dry-run", action="store_true", help="print what it would close")
    p.set_defaults(fn=cmd_sweep)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
