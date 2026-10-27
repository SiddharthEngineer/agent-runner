#!/usr/bin/env python3
"""Build queue for unattended agent runs (machine-local, never committed).

State lives in $AGENT_RUNNER_STATE (default ~/.local/state/agent-runner):
  queue.json   items keyed `<repo>/<slug>` (a bare `<slug>` key from the old runner is an error)
  runs.jsonl   one line per started run (drives the optional daily cap)

Every <slug> below may be a full `<repo>/<slug>` key, or a bare slug when exactly one repo has an item with it.

Usage:
  buildqueue.py add <slug> [--repo NAME] [--at 2026-10-12T02:00 | --in 3h] [--after <repo/slug>] [--note TEXT]
                                     --repo: required unless <slug> is a `<repo>/<slug>` key
                                     --after: not runnable until that item is `done` (merged); any repo
  buildqueue.py list
  buildqueue.py cancel <slug>
  buildqueue.py next                 print the next runnable key, or exit 1 (cron pre-check; no Claude cost)
  buildqueue.py start <slug>         mark running, count an attempt, log the run
  buildqueue.py finish <slug> <result.json>   apply the agent's result (missing file = failed attempt)
  buildqueue.py set <slug> key=value [...]    used by inbox.py / preprod.sh / run.sh
  buildqueue.py unset <slug> key [...]        used by run.sh (clears `resume`)
  buildqueue.py answer <slug> TEXT   attach the user's answer and re-queue with fresh attempts, resuming the session
  buildqueue.py requeue <slug> --note TEXT   re-queue with a note, resuming the session, keeping the attempts used
                                     (the validator's `changes`); out of attempts → blocked
                                     --fresh-attempts: attempts back to 0, a fresh session (triage's re-queue)
                                     --conflict: a conflict round (run.sh, the PR conflicts with its base): sets `conflict=1`,
                                     gives the attempt back; over AGENT_MAX_CONFLICT_ROUNDS (default 2) → blocked
  buildqueue.py oldest <status> [--without KEY]   print the key of the least recently updated item with that status
                                     (and no KEY field), or exit 1
  buildqueue.py pause <hours>        stop starting runs (e.g. after a usage-limit exit)
  buildqueue.py get <slug> <key>

Item statuses: queued → running → done | awaiting-review | awaiting-preprod | on-preprod | waiting-answer | blocked | cancelled.
run.sh stores the agent's `session_id` after each run; `resume=1` (set by `answer`) or `resume=limit` (set by run.sh after a
usage-limit stop) makes the next run resume that session.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

STATE = config.state_dir()
QUEUE = STATE / "queue.json"
RUNS = STATE / "runs.jsonl"
MAX_ATTEMPTS = int(os.environ.get("AGENT_MAX_ATTEMPTS", "2"))
# 0 (the default) = no daily cap: runs are bounded by MAX_ATTEMPTS per item and by usage.py's session-usage gate.
MAX_RUNS_PER_DAY = int(os.environ.get("AGENT_MAX_RUNS_PER_DAY", "0") or 0)
# Conflict rounds per item (the PR conflicts with its base); they cost no attempt, so they have their own cap.
MAX_CONFLICT_ROUNDS = int(os.environ.get("AGENT_MAX_CONFLICT_ROUNDS", "2") or 2)

# Result statuses the agent may write, mapped to the item status they leave behind.
RESULT_TO_STATUS = {
    "merged": "done",
    "review": "awaiting-preprod",  # PR opened for a human; preprod.sh deploys it when the lock is free
    "needs-review": "awaiting-review",  # gate tier `review`: the validator reviews it first (run.sh review stage)
    "question": "waiting-answer",
    "blocked": "blocked",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def load() -> dict:
    if not QUEUE.exists():
        return {"paused_until": None, "items": {}}
    data = json.loads(QUEUE.read_text())
    bare = sorted(k for k, it in data["items"].items() if "/" not in k or "/" not in (it.get("after") or "/"))
    if bare:
        sys.exit(f"{QUEUE}: items with a bare key or `after` from the old runner ({', '.join(bare)}): "
                 "migrate first (both must be `<repo>/<slug>`)")
    return data


def resolve(data: dict, ref: str) -> str:
    """The item key for `ref`: an exact key, or a bare slug that exactly one repo has."""
    if ref in data["items"]:
        return ref
    if "/" in ref:
        sys.exit(f"not queued: {ref}")
    matches = sorted(k for k in data["items"] if k.partition("/")[2] == ref)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"not queued: {ref}")
    sys.exit(f"{ref} is ambiguous: {', '.join(matches)} (use the full key)")


def save(data: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(QUEUE)


def item(data: dict, ref: str) -> dict:
    return data["items"][resolve(data, ref)]


def parse_when(at: str | None, delay: str | None) -> str:
    if at:
        dt = datetime.fromisoformat(at)
        return iso(dt if dt.tzinfo else dt.astimezone())  # naive = VPS local time
    if delay:
        m = re.fullmatch(r"(\d+)([mhd])", delay)
        if not m:
            sys.exit("--in takes e.g. 30m, 3h, 1d")
        unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
        return iso(now() + timedelta(**{unit: int(m.group(1))}))
    return iso(now())


def runs_last_day() -> int:
    if not RUNS.exists():
        return 0
    cutoff = now() - timedelta(days=1)
    return sum(1 for line in RUNS.read_text().splitlines() if datetime.fromisoformat(json.loads(line)["at"]) > cutoff)


def cmd_add(args: argparse.Namespace) -> int:
    data = load()
    repo, slug = args.slug.split("/", 1) if "/" in args.slug else (args.repo, args.slug)
    if not repo:
        sys.exit(f"add {args.slug}: --repo is required (or give a <repo>/<slug> key)")
    key = f"{repo}/{slug}"
    after = None
    if args.after:
        after = args.after if "/" in args.after else f"{repo}/{args.after}"
    plan = Path("agents/plans") / f"{slug}.md"
    existing = data["items"].get(key)
    if existing and existing["status"] in {"queued", "running"}:
        sys.exit(f"{key} is already {existing['status']}")
    data["items"][key] = {
        "repo": repo,
        "slug": slug,
        "plan": str(plan),
        "status": "queued",
        "not_before": parse_when(args.at, args.delay),
        "attempts": 0,
        "notes": [args.note] if args.note else [],
        **({"after": after} if after else {}),
        "added": iso(now()),
        "updated": iso(now()),
    }
    save(data)
    after_text = f", after {after}" if after else ""
    print(f"queued {key} (not before {data['items'][key]['not_before']}{after_text})")
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    data = load()
    if data.get("paused_until"):
        print(f"paused until {data['paused_until']}")
    print(f"runs in last 24h: {runs_last_day()}" + (f"/{MAX_RUNS_PER_DAY}" if MAX_RUNS_PER_DAY else " (no daily cap)"))
    for slug, it in sorted(data["items"].items(), key=lambda kv: kv[1]["not_before"]):
        extra = " ".join(f"{k}={it[k]}" for k in ("after", "pr", "issue") if it.get(k))
        print(f"{it['status']:<17} {slug:<50} attempts={it['attempts']}/{MAX_ATTEMPTS} not_before={it['not_before']} {extra}")
    return 0


def cmd_next(_: argparse.Namespace) -> int:
    data = load()
    if data.get("paused_until") and datetime.fromisoformat(data["paused_until"]) > now():
        return 1
    if MAX_RUNS_PER_DAY and runs_last_day() >= MAX_RUNS_PER_DAY:
        return 1
    due = [
        (it["not_before"], slug)
        for slug, it in data["items"].items()
        if it["status"] == "queued" and it["attempts"] < MAX_ATTEMPTS and datetime.fromisoformat(it["not_before"]) <= now()
        and (not it.get("after") or data["items"].get(it["after"], {}).get("status") == "done")
    ]
    if not due:
        return 1
    print(min(due)[1])
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    data = load()
    key = resolve(data, args.slug)
    it = data["items"][key]
    it.update(status="running", attempts=it["attempts"] + 1, updated=iso(now()))
    save(data)
    STATE.mkdir(parents=True, exist_ok=True)
    with RUNS.open("a") as f:
        f.write(json.dumps({"at": iso(now()), "slug": key, "attempt": it["attempts"]}) + "\n")
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    data = load()
    it = item(data, args.slug)
    result: dict = {}
    if args.result.exists():
        try:
            result = json.loads(args.result.read_text())
        except json.JSONDecodeError:
            result = {}
    status = RESULT_TO_STATUS.get(result.get("status", ""))
    if status is None:  # crashed, timed out or wrote nothing: a failed attempt
        status = "queued" if it["attempts"] < MAX_ATTEMPTS else "blocked"
        result.setdefault("summary", "run ended without a result file")
        # A failed attempt starts fresh: its context is the thing that went wrong.
        it.pop("resume", None)
        it.pop("session_id", None)
    it.update(status=status, updated=iso(now()), last_result=result)
    for key in ("pr", "issue", "branch"):
        if result.get(key):
            it[key] = result[key]
    save(data)
    print(status)
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    data = load()
    it = item(data, args.slug)
    for pair in args.pairs:
        key, _, value = pair.partition("=")
        it[key] = int(value) if value.isdigit() else value
    it["updated"] = iso(now())
    save(data)
    return 0


def cmd_unset(args: argparse.Namespace) -> int:
    data = load()
    it = item(data, args.slug)
    for key in args.keys:
        it.pop(key, None)
    it["updated"] = iso(now())
    save(data)
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    value = item(load(), args.slug).get(args.key)
    if value is None:
        return 1
    print(value if not isinstance(value, (list, dict)) else json.dumps(value))
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    data = load()
    it = item(data, args.slug)
    it["notes"].append(f"User answer ({iso(now())}): {args.text}")
    # The answer is new information, so the item gets a fresh set of attempts, and the next run resumes the
    # session that asked (run.sh falls back to a fresh run if there is no session_id or Claude can't find it).
    it.update(status="queued", attempts=0, resume=1, not_before=iso(now()), updated=iso(now()))
    it.pop("issue", None)
    save(data)
    return 0


def cmd_requeue(args: argparse.Namespace) -> int:
    data = load()
    it = item(data, args.slug)
    if args.fresh_attempts:
        # Triage found a transient cause: one fresh set of attempts, and a fresh session (the old one is what went wrong).
        it["notes"].append(f"Triage ({iso(now())}): {args.note}")
        it.update(status="queued", attempts=0, not_before=iso(now()), updated=iso(now()))
        for stale in ("resume", "review_attempts", "preprod_verdict", "preprod_attempts"):
            it.pop(stale, None)
        save(data)
        print("queued")
        return 0
    if args.conflict:
        # The PR conflicts with its base: not the implementer's fault, so the attempt comes back, but the rounds are capped.
        it["notes"].append(f"Conflict ({iso(now())}): {args.note}")
        rounds = int(it.get("conflict_rounds", 0)) + 1
        it.update(conflict_rounds=rounds, updated=iso(now()))
        it.pop("resume", None)
        if rounds > MAX_CONFLICT_ROUNDS:
            it.update(status="blocked")
            it.pop("conflict", None)
        else:
            it.update(status="queued", conflict=1, attempts=max(0, it["attempts"] - 1), not_before=iso(now()))
        save(data)
        print(it["status"])
        return 0
    it["notes"].append(f"Validator ({iso(now())}): {args.note}")
    # Like `answer`, but the attempts already used still count: a third failed try ends in `blocked`.
    status = "queued" if it["attempts"] < MAX_ATTEMPTS else "blocked"
    it.update(status=status, resume=1, not_before=iso(now()), updated=iso(now()))
    save(data)
    print(status)
    return 0


def cmd_oldest(args: argparse.Namespace) -> int:
    items = load()["items"]
    found = sorted((it["updated"], key) for key, it in items.items()
                   if it["status"] == args.status and not (args.without and it.get(args.without)))
    if not found:
        return 1
    print(found[0][1])
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    data = load()
    item(data, args.slug).update(status="cancelled", updated=iso(now()))
    save(data)
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    data = load()
    data["paused_until"] = iso(now() + timedelta(hours=args.hours))
    save(data)
    print(f"paused until {data['paused_until']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Strelitzia agent build queue")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("slug")
    when = p.add_mutually_exclusive_group()
    when.add_argument("--at")
    when.add_argument("--in", dest="delay")
    p.add_argument("--repo", help="the plan's repo (required unless <slug> is a <repo>/<slug> key)")
    p.add_argument("--after", help="repo/slug (or a slug in the same repo) that must be done (merged) first")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_add)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("next").set_defaults(fn=cmd_next)
    for name, fn in (("start", cmd_start), ("cancel", cmd_cancel)):
        p = sub.add_parser(name)
        p.add_argument("slug")
        p.set_defaults(fn=fn)
    p = sub.add_parser("finish")
    p.add_argument("slug")
    p.add_argument("result", type=Path)
    p.set_defaults(fn=cmd_finish)
    p = sub.add_parser("set")
    p.add_argument("slug")
    p.add_argument("pairs", nargs="+")
    p.set_defaults(fn=cmd_set)
    p = sub.add_parser("unset")
    p.add_argument("slug")
    p.add_argument("keys", nargs="+")
    p.set_defaults(fn=cmd_unset)
    p = sub.add_parser("get")
    p.add_argument("slug")
    p.add_argument("key")
    p.set_defaults(fn=cmd_get)
    p = sub.add_parser("answer")
    p.add_argument("slug")
    p.add_argument("text")
    p.set_defaults(fn=cmd_answer)
    p = sub.add_parser("requeue")
    p.add_argument("slug")
    p.add_argument("--note", required=True)
    how = p.add_mutually_exclusive_group()
    how.add_argument("--fresh-attempts", action="store_true")
    how.add_argument("--conflict", action="store_true")
    p.set_defaults(fn=cmd_requeue)
    p = sub.add_parser("oldest")
    p.add_argument("status")
    p.add_argument("--without", help="skip items that have this field set")
    p.set_defaults(fn=cmd_oldest)
    p = sub.add_parser("pause")
    p.add_argument("hours", type=float)
    p.set_defaults(fn=cmd_pause)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
