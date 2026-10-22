#!/usr/bin/env python3
"""The runner's status block at the top of each PR description, so the GitHub app shows where a PR is.

Usage:
  prstatus.py update <key> [--dry-run]   refresh one item's PR (run.sh, right after a transition)
  prstatus.py sync [--dry-run]           refresh every item with a PR that isn't cancelled and changed in the last 14 days
                                         (a `done` item until its final block is written); run.sh, once per tick

The block is derived from queue fields only (`render`), between two HTML-comment markers (`apply`); the rest of the body
is never touched. The PR's repo, base and `[preprod]` come from the copy of its runner.toml that run.sh keeps in
$AGENT_RUNNER_STATE/configs/<repo>.toml. `gh pr edit` runs only when the block changes (the "updated" time aside), and a
`gh` failure logs one line and moves on. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buildqueue  # noqa: E402
import config  # noqa: E402

START = "<!-- agent-runner:status -->"
END = "<!-- /agent-runner:status -->"
BLOCK_RE = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
UPDATED_RE = re.compile(r" · updated [^\n]*")
FINAL_DAYS = 14
DONE, RUN, WAIT, FAIL, HOLD, SKIP = "✅", "🔁", "⏳", "❌", "⏸", "➖"
NAMES = ("Implementer", "Validator review", "Preprod deploy", "Preprod checks", "Merge")
NOTE_PREFIX = re.compile(r"^(Validator \([^)]*\): )?(Validator review|Preprod checks) of PR #\d+ [^:]*: ")


def first_sentence(text: str | None, limit: int = 140) -> str:
    text = NOTE_PREFIX.sub("", (text or "").strip()).split("\n", 1)[0].strip()
    text = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].rstrip(".")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def human_steps(body: str) -> int:
    """Numbered lines under the body's "Human test steps" heading (up to the next heading)."""
    count, inside = 0, False
    for line in body.splitlines():
        if re.match(r"\s*#{1,6}\s", line) or re.match(r"\s*\*\*[^*]+\*\*:?\s*$", line):
            inside = "human test steps" in line.lower()
        elif inside and re.match(r"\s*\d+[.)]\s", line):
            count += 1
    return count


def issue_link(issue) -> str:
    if issue in (None, ""):
        return ""
    if str(issue).isdigit():
        inbox = os.environ.get("AGENT_INBOX_REPO")
        return f"https://github.com/{inbox}/issues/{issue}" if inbox else f"inbox issue #{issue}"
    return str(issue)


def render(item: dict, *, has_preprod: bool, human_steps: int, now: datetime, base: str = "main",
           released: str = "") -> str:
    """The status block for one queue item (no network). `released`: the tag with the merge commit (`release_tag`)."""
    max_attempts = buildqueue.MAX_ATTEMPTS
    status = item.get("status", "")
    rv, pv, rerun = item.get("review_verdict"), item.get("preprod_verdict"), item.get("rerun")
    summary = (item.get("last_result") or {}).get("summary", "")
    notes = item.get("notes") or []
    tier = item.get("tier") or (
        "review" if rv or pv or rerun or status in ("awaiting-review", "awaiting-preprod", "on-preprod")
        else "low" if status == "done" else None)
    attempt = item.get("attempts", 0) + (status == "queued")
    attempt_txt = f"attempt {attempt}/{max_attempts}"
    # A question links its agent-question issue (`issue`, inbox.py ask); a block links the newest notice (`notice`, run.sh).
    issue = issue_link(item.get("issue") if status in ("waiting-answer", "question") else item.get("notice"))

    steps = f"{human_steps} Human test steps"
    if human_steps == 0 and has_preprod:
        steps = "⚠️ no Human test steps"
    texts = [
        "Implementer opened the PR",
        "Validator reviews the code",
        "Runner deploys the branch to preprod",
        "Second validator runs the Human test steps on preprod and posts a checklist",
        f'Merge, preprod back on {base}; steps it couldn\'t do come to you as a "merged; N steps left for you" issue',
    ]
    if not has_preprod:
        texts[4] = f"Merge to {base}"
    stages: list[list[str]] = [[DONE, steps], *([WAIT, ""] for _ in range(4))]
    if tier == "low":
        for i in (1, 2, 3):
            stages[i] = [SKIP, "`low` tier merges on green CI"]
    elif not has_preprod:
        for i in (2, 3):
            stages[i] = [SKIP, "no `[preprod]` in this repo"]
    reviewed = tier == "review" and rv == "approve"
    if reviewed:
        stages[1] = [DONE, "approved"]

    def deployed() -> None:
        stages[1] = [DONE, "approved"]
        stages[2] = [DONE, "on preprod"]

    def checked() -> None:
        deployed()
        link = item.get("preprod_comment")
        stages[3] = [DONE, f"passed ({link})" if link else "passed"]

    def failed_stage() -> int:
        """Where a blocked or waiting item stopped (0-based)."""
        if pv == "approve":
            return 4
        if pv == "blocked" or item.get("preprod_attempts"):
            return 3
        if rv == "blocked" or item.get("review_attempts"):
            return 1
        if rerun:  # `changes` out of attempts (run.sh clears `rerun` when the implementer itself is blocked)
            return 3 if rerun == "preprod" else 1
        if (rv == "approve" and not has_preprod) or (tier == "low" and "not merged" in summary):
            return 4
        return 0

    rerun_note = first_sentence(item.get("review_summary") if rerun == "review" and item.get("review_summary") else
                                notes[-1] if notes else "")
    if status == "done":
        hand = bool(re.fullmatch(r"PR #\d+ merged by hand", summary))  # closed_by_hand's summary
        if tier == "review" and not hand:
            if has_preprod:
                checked()
            else:
                stages[1] = [DONE, "approved"]
        elif hand:
            for i in (1, 2, 3):
                if stages[i][0] == WAIT:
                    stages[i] = [SKIP, "merged by hand"]
        left = item.get("left_count")
        if hand:
            detail = "merged by hand"
        elif left:
            detail = f"merged; {left} steps left for you" + (f" ({item['left_issue']})" if item.get("left_issue") else "")
        elif left == 0:
            detail = "merged; nothing left for you"
        else:
            detail = "merged"
        if released:
            detail += f"; released in {released}"
        stages[4] = [DONE, detail]
        head = f"{DONE} {detail[0].upper()}{detail[1:]}"
    elif status == "cancelled":
        head = f"{SKIP} Cancelled (PR closed)"
    elif status == "awaiting-review":
        tries = item.get("review_attempts")
        detail = "queued" + (f" again: last run wrote no verdict ({tries}/{max_attempts})" if tries else
                             " again after the fix" if rerun else "")
        stages[1] = [RUN, detail]
        head = f"{RUN} Validator review: {detail}"
    elif status == "awaiting-preprod":
        stages[1] = [DONE, "approved"]
        stages[2] = [RUN, "waiting for the preprod lock"]
        head = f"{RUN} Approved; waiting for preprod"
    elif status == "on-preprod":
        deployed()
        if pv == "approve":
            checked()
            stages[4] = [FAIL, "not merged: CI isn't green or the merge failed; it stays on preprod"]
            head = f"{FAIL} Passed preprod, not merged: CI isn't green or the merge failed"
        else:
            tries = item.get("preprod_attempts")
            stages[3] = [RUN, "next" + (f" (last run wrote no verdict, {tries}/{max_attempts})" if tries else "")]
            head = f"{RUN} On preprod; the second validator checks the Human test steps next"
    elif status in ("queued", "running"):
        doing = "re-run queued" if status == "queued" else "fixing it now"
        if rerun == "review":
            stages[1] = [RUN, f"asked for changes: {rerun_note}; implementer {doing}, {attempt_txt}"]
            head = f"{RUN} Validator review: asked for changes ({rerun_note}); implementer {doing}, {attempt_txt}"
        elif rerun == "preprod":
            deployed()
            stages[3] = [RUN, f"checks failed: {rerun_note}; implementer {doing}, {attempt_txt}"]
            head = f"{RUN} Preprod checks failed ({rerun_note}); implementer {doing}, {attempt_txt}"
        else:
            stages[0] = [RUN, f"{steps}; re-run {'queued' if status == 'queued' else 'running'}, {attempt_txt}"]
            head = f"{RUN} Implementer re-run {'queued' if status == 'queued' else 'running'}, {attempt_txt}"
    else:  # blocked, waiting-answer / question, or anything new: on hold at the stage it stopped
        at = failed_stage()
        for i in range(1, at):
            if stages[i][0] == WAIT:
                stages[i] = [DONE, "approved" if i == 1 else "on preprod" if i == 2 else "passed"]
        if status in ("waiting-answer", "question"):
            why = "waiting for your answer"
        else:
            why = {0: summary, 1: item.get("review_summary"),
                   3: item.get("preprod_summary") or (notes[-1] if rerun == "preprod" and notes else ""), 4: summary}[at]
            why = f"blocked: {first_sentence(why)}" if why else "blocked"
        detail = why + (f" ({issue})" if issue else "")
        stages[at] = [HOLD, detail]
        head = f"{HOLD} {NAMES[at]}: {detail}"
    lines = [START, f"**Runner status:** {head} · updated {now.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC"]
    for n, ((mark, detail), text) in enumerate(zip(stages, texts), 1):
        sep = ", " if n == 1 else ": "  # "opened the PR, 7 Human test steps"
        lines.append(f"{n}. {mark} {text}" + (sep + detail if detail else ""))
    lines.append(END)
    return "\n".join(lines)


def apply(body: str, block: str) -> str:
    """`body` with its status block replaced by `block`, or `block` put first; the rest of the body is unchanged."""
    if BLOCK_RE.search(body or ""):
        return BLOCK_RE.sub(lambda _: block, body, count=1)
    return f"{block}\n\n{body}" if body else block + "\n"


def same(a: str, b: str) -> bool:
    """Equal apart from the "updated" time, so a tick with nothing new edits nothing."""
    return UPDATED_RE.sub("", a) == UPDATED_RE.sub("", b)


def log(msg: str) -> None:
    print(f"prstatus: {msg}")


def repo_config(repo: str) -> config.RunnerConfig | None:
    try:
        return config.load(config.state_dir() / "configs" / f"{repo}.toml")
    except config.ConfigError:
        return None


def release_tag(repo: str, sha: str) -> str:
    """"v1.4.4 (pre-release v1.4)": the oldest vX.Y.Z tag containing <sha> in the repo's clone (versions.py tags it), or ""."""
    path = config.repo_env(repo).get("AGENT_REPO")
    if not sha or not path:
        return ""
    out = subprocess.run(["git", "-C", path, "tag", "--contains", sha, "-l", "v*"], capture_output=True, text=True,
                         timeout=30)
    found = sorted((t for t in out.stdout.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)),
                   key=lambda t: tuple(int(n) for n in t[1:].split(".")))
    return f"{found[0]} (pre-release {found[0].rsplit('.', 1)[0]})" if found else ""


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, timeout=30).stdout


def refresh(key: str, it: dict, *, dry_run: bool) -> bool:
    """Bring one item's PR block up to date. True if the block on the PR is (now) the current one, and final if done."""
    repo = key.partition("/")[0]
    cfg = repo_config(repo)
    if cfg is None:
        log(f"{key}: no runner.toml for {repo} in the state dir, skipped")
        return False
    pr, gh_repo = str(it["pr"]), cfg.repo.github
    try:
        view = json.loads(gh("pr", "view", pr, "--repo", gh_repo, "--json", "body,state,mergeCommit"))
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        log(f"{key}: gh pr view {gh_repo}#{pr} failed: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}")
        return False
    body = view.get("body") or ""
    # A [release] repo's merged PR shows its tag; until versions.py has made it, the block isn't final (sync retries).
    tagging = cfg.release is not None and cfg.release.tags and it.get("status") == "done"
    released = release_tag(repo, (view.get("mergeCommit") or {}).get("oid", "")) if tagging else ""
    final = not tagging or bool(released)
    block = render(it, has_preprod=cfg.preprod is not None, human_steps=human_steps(BLOCK_RE.sub("", body)),
                   now=buildqueue.now(), base=cfg.repo.base, released=released)
    old = BLOCK_RE.search(body)
    if old and same(old.group(0), block):
        return final
    if view.get("state") == "CLOSED" and it.get("status") != "done":
        return False
    if dry_run:
        log(f"{key}: would edit {gh_repo}#{pr}: {block.splitlines()[1]}")
        return False
    fd, path = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(apply(body, block))
        gh("pr", "edit", pr, "--repo", gh_repo, "--body-file", path)
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"{key}: gh pr edit {gh_repo}#{pr} failed: {type(exc).__name__}")
        return False
    finally:
        os.unlink(path)
    log(f"{key}: {gh_repo}#{pr}: {block.splitlines()[1]}")
    return final


def mark_final(key: str) -> None:
    """A `done` item's block is final once written: `sync` stops fetching its PR. `updated` is left alone."""
    data = buildqueue.load()
    if key in data["items"]:
        data["items"][key]["pr_status_final"] = 1
        buildqueue.save(data)


def due(it: dict) -> bool:
    if not it.get("pr") or it.get("status") == "cancelled" or it.get("pr_status_final"):
        return False
    updated = it.get("updated")
    return not updated or datetime.fromisoformat(updated) > buildqueue.now() - timedelta(days=FINAL_DAYS)


def cmd_update(args: argparse.Namespace) -> int:
    data = buildqueue.load()
    key = buildqueue.resolve(data, args.key)
    it = data["items"][key]
    if not it.get("pr"):
        return 0
    if refresh(key, it, dry_run=args.dry_run) and it.get("status") == "done":
        mark_final(key)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    for key, it in sorted(buildqueue.load()["items"].items()):
        try:
            if due(it) and refresh(key, it, dry_run=args.dry_run) and it.get("status") == "done":
                mark_final(key)
        except Exception as exc:  # one bad item never stops the others, or the tick
            log(f"{key}: skipped ({type(exc).__name__}: {exc})")
    return 0


def main() -> int:
    config.load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("update")
    p.add_argument("key")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_update)
    p = sub.add_parser("sync")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_sync)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
