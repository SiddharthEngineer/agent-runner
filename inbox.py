#!/usr/bin/env python3
"""Agent ↔ user inbox: issues in a private GitHub repo, plus an optional ntfy push.

Usage:
  inbox.py ask <slug> --title T --body-file F   open an `agent-question` issue; the item waits for your reply
  inbox.py notify --title T --body B [--slug S]  open an `agent-notice` issue (PR on preprod, item blocked, …)
  inbox.py poll                                  re-queue items whose question issue has a reply (cron, no Claude)

Config (~/.config/strelitzia-agent/env, or the environment):
  AGENT_INBOX_REPO  owner/name of the private inbox repo (required)
  NTFY_TOPIC        optional ntfy.sh topic. GitHub doesn't push-notify you about issues your own account
                    opens, so without this you only see them in the app's inbox.
Never put personal data, secrets or job data in a title or body: link to files on the VPS instead.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKER = "<!-- strelitzia-agent -->"  # tags comments the agent wrote, so any other comment is your answer
LEGACY_MARKER = "<!-- applypilot-agent -->"  # on comments written before the rename
PROJECT_LABEL = "strelitzia"


def load_env() -> None:
    from buildqueue import CONFIG_DIR  # same directory

    env_file = CONFIG_DIR / "env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip().removeprefix("export ")
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


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


def open_issue(title: str, body: str, label: str) -> tuple[int, str]:
    for name in (label, PROJECT_LABEL):
        gh("label", "create", name, "--repo", repo(), "--force")
    url = gh("issue", "create", "--repo", repo(), "--title", title, "--body", f"{body}\n\n{MARKER}",
             "--label", label, "--label", PROJECT_LABEL)
    push(title, url)
    return int(url.rstrip("/").rsplit("/", 1)[1]), url


def cmd_ask(args: argparse.Namespace) -> int:
    number, url = open_issue(f"[{args.slug}] {args.title}", args.body_file.read_text(), "agent-question")
    buildqueue("set", args.slug, f"issue={number}", "status=waiting-answer")
    print(url)
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    title = f"[{args.slug}] {args.title}" if args.slug else args.title
    print(open_issue(title, args.body, "agent-notice")[1])
    return 0


def cmd_poll(_: argparse.Namespace) -> int:
    from buildqueue import load  # same directory

    for slug, it in load()["items"].items():
        if it["status"] != "waiting-answer" or not it.get("issue"):
            continue
        issue = json.loads(gh("issue", "view", str(it["issue"]), "--repo", repo(), "--json", "comments"))
        replies = [c["body"] for c in issue["comments"] if MARKER not in c["body"] and LEGACY_MARKER not in c["body"]]
        if not replies:
            continue
        buildqueue("answer", slug, "\n\n".join(replies))
        gh("issue", "comment", str(it["issue"]), "--repo", repo(), "--body", f"Got it, re-queued `{slug}`.\n\n{MARKER}")
        gh("issue", "close", str(it["issue"]), "--repo", repo())
        print(f"re-queued {slug} with your answer")
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
    p.set_defaults(fn=cmd_notify)
    sub.add_parser("poll").set_defaults(fn=cmd_poll)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
