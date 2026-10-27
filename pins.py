#!/usr/bin/env python3
"""Component pins: a parent repo's components.toml pins each child repo at a tag; the runner moves the pins forward.

Usage:
  pins.py sync [--dry-run]   open or update a "Pin <name> <tag>" PR per component whose child has a newer tag
  pins.py tick [--dry-run]   merge the open pin-bump PRs on green CI (prints `merged <parent>#<n> <name> <tag>`)

Rules (plan runner-16-pins):
- A parent is a registered repo with components.toml at the root of origin/<base> (no runner.toml opt-in). Each table
  with a `tag` is a component; its name is the child's registered repo name. An unregistered name is skipped.
- Sync only ever moves a pin forward, to the child's newest vX.Y.Z tag on origin. The PR is on branch `pin/<name>`,
  labelled `pin-bump`, one commit on origin/<base> changing only that component's `tag` line (comments and order kept).
  An open PR at an older tag is updated in place (force-push to `pin/<name>` only); a pin that is already newest closes it.
- `hold = "reason"` on a component stops its bumps (an open PR is closed); runs.log says so once.
- Tick merges with the parent's merge method, with no review, only a PR whose diff is nothing but `tag` lines in
  components.toml. Red CI or a refused diff sends one notice per PR (and a refused PR gets one comment); pending waits.
- Stateless from origin's tags and the open PRs, so hand merges and missed ticks catch up. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config  # noqa: E402
import versions  # noqa: E402
from versions import Fail, Repo, gh, say  # noqa: E402

COMPONENTS = "components.toml"
LABEL = "pin-bump"
HEADER_RE = re.compile(r'^\s*\[\s*("?)([A-Za-z0-9_-]+)\1\s*\]\s*(#.*)?$')
TABLE_RE = re.compile(r"^\s*\[")
TAG_LINE_RE = re.compile(r'^(\s*tag\s*=\s*)"([^"]*)"(.*)$')
DIFF_TAG_RE = re.compile(r'^[+-]\s*tag\s*=\s*"(v\d+\.\d+\.\d+)"\s*(#.*)?$')
DIFF_FILE_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
DIFF_META = ("old mode", "new mode", "deleted file", "new file", "similarity", "rename", "copy ", "Binary")
GREEN = {"SUCCESS", "SKIPPED", "NEUTRAL"}
RED = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR", "STALE"}


def git(repo: Repo, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    if env is None:
        return versions.git(repo, *args, check=check)
    out = subprocess.run(["git", "-C", str(repo.dir), *args], capture_output=True, text=True, timeout=120,
                         env={**os.environ, **env})
    if check and out.returncode != 0:
        err = (out.stderr or out.stdout).strip().splitlines()
        raise Fail(f"git {args[0]} failed: {err[-1] if err else f'exit {out.returncode}'}")
    return out


def set_pin(text: str, name: str, tag: str) -> str:
    """<text> with only the `tag = "…"` line of table [<name>] changed to <tag>."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if (m := HEADER_RE.match(line)) and m[2] == name), None)
    if start is None:
        raise Fail(f"no [{name}] table in {COMPONENTS}")
    for i in range(start + 1, len(lines)):
        if TABLE_RE.match(lines[i]):
            break
        if m := TAG_LINE_RE.match(lines[i].rstrip("\r\n")):
            lines[i] = f'{m[1]}"{tag}"{m[3]}' + lines[i][len(lines[i].rstrip("\r\n")):]
            new = "".join(lines)
            old_data, new_data = tomllib.loads(text), tomllib.loads(new)
            old_data[name]["tag"] = tag
            if new_data != old_data:
                raise Fail(f"editing [{name}] tag in {COMPONENTS} changed more than the tag")
            return new
    raise Fail(f'no tag = "…" line in [{name}] of {COMPONENTS}')


def components(data: dict) -> list[tuple[str, dict]]:
    return [(k, v) for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("tag"), str)]


def open_parent(name: str) -> tuple[Repo, str] | None:
    """The registered repo and its components.toml text on origin/<base>, or None if it isn't a parent."""
    try:
        repo = versions.open_repo(name)
    except Fail as exc:
        if exc.code == versions.OFF:
            return None
        raise
    shown = git(repo, "show", f"origin/{repo.base}:{COMPONENTS}", check=False)
    if shown.returncode != 0:
        return None
    return repo, shown.stdout


def parents(only: str | None = None) -> list[str]:
    names = sorted(p.stem for p in (config.config_dir() / "repos").glob("*.env"))
    return [n for n in names if config.NAME_RE.match(n) and (only is None or n == only)]


class Skip(Exception):
    pass


CHILDREN: dict[str, tuple[Repo, list[str]] | Exception] = {}  # opened once per run, however many parents pin it


def open_child(name: str) -> tuple[Repo, list[str]]:
    """The child's clone (tags fetched) and its vX.Y.Z tags on origin, oldest first. Skip if it isn't registered."""
    if name not in CHILDREN:
        try:
            CHILDREN[name] = _open_child(name)
        except (Skip, Fail) as exc:
            CHILDREN[name] = exc
    found = CHILDREN[name]
    if isinstance(found, Exception):
        raise found
    return found


def _open_child(name: str) -> tuple[Repo, list[str]]:
    if not config.NAME_RE.match(name) or not config.repo_env(name).get("AGENT_REPO"):
        raise Skip("not a registered repo")
    try:
        repo = versions.open_repo(name)
    except Fail as exc:
        if exc.code == versions.OFF:
            raise Skip(str(exc)) from exc
        raise
    if repo.remote is None:  # no [release]: open_repo didn't list origin's tags
        repo.remote = {line.rpartition("refs/tags/")[2] for line in
                       git(repo, "ls-remote", "--tags", "--refs", "origin", "v*").stdout.splitlines()}
    return repo, versions.tags(repo)


def changes(child: Repo, tags: list[str], pin: str, newest: str) -> list[str]:
    """One line per child tag after <pin> up to <newest>, newest first, with the PRs from its tag message."""
    out = []
    for tag in reversed(tags):
        if versions.version(pin) < versions.version(tag) <= versions.version(newest):
            prs = [re.sub(r"^#", f"{child.github}#", line) if child.github else line
                   for line in versions.tag_info(child, tag)[1]]
            out.append(f"- {tag}" + (f" · {', '.join(prs)}" if prs else ""))
    return out


class State:
    """Things said once (holds, skips, notices), in $AGENT_RUNNER_STATE/pins.json, so a quiet tick stays quiet."""

    def __init__(self, dry_run: bool):
        self.path = config.state_dir() / "pins.json"
        self.dry_run = dry_run
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("notes", {})
        self.data.setdefault("notices", {})

    def note(self, key: str, text: str | None) -> bool:
        """Print <text> when it differs from what was printed for <key> last time (True); None forgets <key>."""
        if text is None:
            self.data["notes"].pop(key, None)
            return False
        new = self.dry_run or self.data["notes"].get(key) != text
        if new:
            print(text)
        self.data["notes"][key] = text
        return new

    def save(self) -> None:
        if self.dry_run:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True) + "\n")


def pin_prs(repo: Repo) -> dict[str, dict]:
    """Open PRs to the base on a pin/<name> branch, by branch."""
    prs = json.loads(gh("pr", "list", "--repo", repo.github, "--state", "open", "--base", repo.base, "--limit", "100",
                        "--json", "number,headRefName,title,labels") or "[]")
    return {pr["headRefName"]: pr for pr in prs if pr.get("headRefName", "").startswith("pin/")}


def pin_commit_tree(repo: Repo, text: str) -> str:
    """The tree of origin/<base> with components.toml replaced by <text>."""
    base = f"origin/{repo.base}"
    mode = git(repo, "ls-tree", base, COMPONENTS).stdout.split()[0]
    blob = subprocess.run(["git", "-C", str(repo.dir), "hash-object", "-w", "--stdin"], input=text,
                          capture_output=True, text=True, check=True).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(repo, "read-tree", base, env=env)
        git(repo, "update-index", "--cacheinfo", f"{mode},{blob},{COMPONENTS}", env=env)
        return git(repo, "write-tree", env=env).stdout.strip()


def branch_current(repo: Repo, branch: str, tree: str) -> bool:
    """origin/<branch> is one commit on origin/<base> whose tree is <tree>."""
    ref = f"origin/{branch}"
    shown = git(repo, "rev-list", "--parents", "-n", "1", ref, check=False).stdout.split()
    base = git(repo, "rev-parse", f"origin/{repo.base}").stdout.strip()
    return (len(shown) == 2 and shown[1] == base
            and git(repo, "rev-parse", f"{ref}^{{tree}}", check=False).stdout.strip() == tree)


def sync_parent(repo: Repo, text: str, state: State, dry_run: bool) -> bool:
    """Bump PRs for one parent. False if any component failed (the others still ran)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Fail(f"{repo.name}: {COMPONENTS} on origin/{repo.base} doesn't parse: {exc}") from exc
    prs = pin_prs(repo)
    ok, labelled = True, False
    for name, table in components(data):
        key, branch = f"{repo.name}/{name}", f"pin/{name}"
        pr = prs.get(branch)
        try:
            if pr and LABEL not in {label.get("name") for label in pr.get("labels") or []}:
                state.note(key, f"{name}: open PR {repo.name}#{pr['number']} on {branch} isn't labelled {LABEL}, "
                                "so sync leaves it alone")
                continue
            if isinstance(table.get("hold"), str):
                state.note(key, f"{name}: held in {repo.name} ({table['hold']}), no bumps")
                if pr:
                    close(repo, pr, f"{name} is held in {COMPONENTS}: {table['hold']}", dry_run)
                continue
            try:
                child, tags = open_child(name)
            except Skip as exc:
                state.note(key, f"{name}: skipped in {repo.name}: {exc}")
                continue
            pin = table["tag"]
            if not versions.TAG_RE.match(pin):
                state.note(key, f"{name}: pin {pin!r} in {repo.name} isn't a vX.Y.Z tag, skipped")
                continue
            state.note(key, None)
            newest = tags[-1] if tags else None
            if newest is None or versions.version(newest) <= versions.version(pin):
                if dry_run:
                    print(f"{name}: {pin} is newest, nothing to do")
                if pr:
                    close(repo, pr, f"{repo.name} already pins {name} at {pin}, its newest tag.", dry_run)
                continue
            title = f"Pin {name} {newest}"
            body = "\n".join([f"Moves {name} from {pin} to {newest} in `{COMPONENTS}`:", "",
                              *changes(child, tags, pin, newest)])
            tree = pin_commit_tree(repo, set_pin(text, name, newest))
            current = branch_current(repo, branch, tree)
            if pr and current and pr["title"] == title:
                continue
            if not pr and closed_by_hand(repo, branch, title):
                state.note(f"{key} closed", f"{name}: {title} was closed by hand in {repo.name}, not reopened (the next tag opens one)")
                continue
            if dry_run:
                say(True, f"{'update' if pr else 'open'} {repo.name} PR {title} on {branch}")
                continue
            if not current:
                sha = git(repo, "commit-tree", tree, "-p", f"origin/{repo.base}", "-m", title, "-m", body).stdout.strip()
                git(repo, "push", "-q", "--force", "origin", f"{sha}:refs/heads/{branch}")
            pr_body = body + "\n\nOpened by `agent-runner pins sync`; the runner merges it on green CI, with no review."
            if not labelled:
                gh("label", "create", LABEL, "--repo", repo.github, "--color", "0E8A16", "--force",
                   "--description", "A components.toml pin moved by agent-runner (merges on green CI)")
                labelled = True
            if pr:
                gh("pr", "edit", str(pr["number"]), "--repo", repo.github, "--title", title, "--body", pr_body,
                   "--add-label", LABEL)
                print(f"updated {repo.name}#{pr['number']}: {title}")
            else:
                url = gh("pr", "create", "--repo", repo.github, "--base", repo.base, "--head", branch,
                         "--title", title, "--body", pr_body, "--label", LABEL).strip().splitlines()[-1]
                print(f"opened {repo.name}#{url.rstrip('/').rpartition('/')[2]}: {title}")
        except (Fail, subprocess.CalledProcessError, ValueError, KeyError) as exc:
            ok = not state.note(f"{key}!", f"{repo.name} {name}: {exc}") and ok  # a known failure stays quiet
        else:
            state.note(f"{key}!", None)
    return ok


def closed_by_hand(repo: Repo, branch: str, title: str) -> bool:
    """A closed (not merged) PR with this title on <branch>: you turned this bump down, so it isn't reopened."""
    prs = json.loads(gh("pr", "list", "--repo", repo.github, "--state", "closed", "--head", branch, "--limit", "20",
                        "--json", "title,state") or "[]")
    return any(pr.get("title") == title and pr.get("state") != "MERGED" for pr in prs)


def close(repo: Repo, pr: dict, why: str, dry_run: bool) -> None:
    say(dry_run, f"close {repo.name}#{pr['number']} ({pr['title']}): {why}")
    if not dry_run:
        gh("pr", "close", str(pr["number"]), "--repo", repo.github, "--comment", f"Closed: {why}", "--delete-branch")


def each_parent(step, only: str | None, dry_run: bool) -> int:
    state, ok = State(dry_run), True
    for name in parents(only):
        try:
            found = open_parent(name)
            if found is not None:
                ok = step(*found, state, dry_run) and ok
        except Fail as exc:
            ok = not state.note(f"{name}!", str(exc)) and ok
        else:
            state.note(f"{name}!", None)
    state.save()
    return 0 if ok else 1


def diff_problem(diff: str) -> str | None:
    """Why a pin PR's diff isn't only `tag = "vX.Y.Z"` lines in components.toml, or None."""
    paths = {p for pair in DIFF_FILE_RE.findall(diff) for p in pair}
    if not paths:
        return "its diff is empty"
    if paths != {COMPONENTS}:
        return f"it changes {', '.join(sorted(paths - {COMPONENTS}))}"
    lines = diff.splitlines()
    if any(line.startswith(DIFF_META) for line in lines):
        return f"it changes {COMPONENTS}'s mode or path"
    changed = [line for line in lines if line[:1] in "+-" and not line.startswith(("+++ ", "--- "))]
    other = next((line for line in changed if not DIFF_TAG_RE.match(line)), None)
    if other is not None:
        return f"it changes more than tag lines in {COMPONENTS}: `{other[:80]}`"
    old = [DIFF_TAG_RE.match(line)[1] for line in changed if line[0] == "-"]
    new = [DIFF_TAG_RE.match(line)[1] for line in changed if line[0] == "+"]
    if len(old) != len(new):
        return f"it adds or removes tag lines in {COMPONENTS}"
    if any(versions.version(b) <= versions.version(a) for a, b in zip(old, new)):
        return "it doesn't move every pin forward"
    return None


def ci(repo: Repo, number: int) -> str:
    """green, red or pending, read once (no waiting). No checks reported yet counts as pending."""
    out = versions.run(["gh", "pr", "checks", str(number), "--repo", repo.github, "--json", "state"], check=False)
    said = out.stdout + out.stderr
    if "no checks reported" in said:
        return "pending"
    if "Resource not accessible" in said:  # a token that can't read the check rollup: the Actions runs of the head
        sha = gh("pr", "view", str(number), "--repo", repo.github, "--json", "headRefOid", "-q", ".headRefOid").strip()
        runs = json.loads(gh("run", "list", "--repo", repo.github, "--commit", sha, "--json", "status,conclusion") or "[]")
        if not runs or any(r.get("status") != "completed" for r in runs):
            return "pending"
        return "green" if all((r.get("conclusion") or "").upper() in GREEN for r in runs) else "red"
    try:
        states = {(c.get("state") or "").upper() for c in json.loads(out.stdout)}
    except ValueError as exc:
        err = said.strip().splitlines()
        raise Fail(f"gh pr checks failed: {err[-1] if err else f'exit {out.returncode}'}") from exc
    if states & RED:
        return "red"
    return "green" if states and states <= GREEN else "pending"


def notify(repo: Repo, title: str, body: str) -> bool:
    """An agent-notice for the parent. False when inbox.py failed (try again next tick); True when sent or no inbox."""
    if not os.environ.get("AGENT_INBOX_REPO"):
        return True
    sent = subprocess.run([sys.executable, str(HERE / "inbox.py"), "notify", "--project", repo.name,
                           "--title", title, "--body", body], capture_output=True, text=True)
    if sent.returncode != 0:
        err = sent.stderr.strip().splitlines()
        print(f"notice for {repo.name} failed: {err[-1] if err else f'exit {sent.returncode}'}")
    return sent.returncode == 0


def tick_parent(repo: Repo, text: str, state: State, dry_run: bool) -> bool:
    """Merge the green pin-bump PRs of one parent. False if any PR failed (the others still ran)."""
    try:
        held = {name for name, table in components(tomllib.loads(text)) if isinstance(table.get("hold"), str)}
    except tomllib.TOMLDecodeError as exc:
        raise Fail(f"{repo.name}: {COMPONENTS} on origin/{repo.base} doesn't parse: {exc}") from exc
    prs = json.loads(gh("pr", "list", "--repo", repo.github, "--state", "open", "--base", repo.base, "--label", LABEL,
                        "--limit", "100", "--json", "number,headRefName,headRefOid,title") or "[]")
    seen = state.data["notices"]
    for key in [k for k in seen if k.partition("#")[0] == repo.name
                and int(k.partition("#")[2]) not in {pr["number"] for pr in prs}]:
        del seen[key]  # merged or closed: forget it
    ok = True
    for pr in prs:
        n, key = pr["number"], f"{repo.name}#{pr['number']}"
        url = f"https://github.com/{repo.github}/pull/{n}"
        name = pr["headRefName"].removeprefix("pin/")
        if not pr["headRefName"].startswith("pin/"):
            continue  # labelled by hand: not a runner pin PR
        if name in held:
            continue  # sync closes it
        try:
            diff = gh("pr", "diff", str(n), "--repo", repo.github)
            why = diff_problem(diff)
            if why is not None:
                cause = f"refused: {why}"
                if seen.get(key) != cause:
                    say(dry_run, f"refuse {key} ({pr['title']}): {why}")
                    if not dry_run:
                        seen[key] = cause  # the comment once, even if the notice below fails
                        gh("pr", "comment", str(n), "--repo", repo.github, "--body",
                           f"Not merged by the runner: {why}. A pin-bump PR merges with no review only when its "
                           f"diff is nothing but `tag` lines in `{COMPONENTS}`. Merge or close it by hand.")
                        notify(repo, f"pin PR {key} not merged: {why}", f"{url} ({pr['title']}): {why}.")
                continue
            tag = next(m[1] for line in diff.splitlines() if line.startswith("+") and (m := DIFF_TAG_RE.match(line)))
            status = ci(repo, n)
            if status == "pending":
                continue
            if status == "red":
                if seen.get(key) != "red":
                    say(dry_run, f"notify: {key} ({pr['title']}) CI is red")
                    if not dry_run and notify(repo, f"pin PR {key} CI is red", f"{url} ({pr['title']}): CI is red, "
                                              "so the runner doesn't merge it. The next green run merges it."):
                        seen[key] = "red"
                continue
            if dry_run:
                say(True, f"merge {key} {name} {tag}")
                continue
            try:
                gh("pr", "merge", str(n), "--repo", repo.github, f"--{repo.merge}", "--delete-branch",
                   "--match-head-commit", pr["headRefOid"])  # only the head whose diff and CI were checked
            except Fail as exc:
                if seen.get(key) != "merge failed" and notify(
                        repo, f"pin PR {key} merge failed", f"{url} ({pr['title']}): CI is green but {exc}."):
                    seen[key] = "merge failed"
                raise
            seen.pop(key, None)
            print(f"merged {key} {name} {tag}")
        except (Fail, ValueError, KeyError, StopIteration) as exc:
            why = "no tag line in the diff" if isinstance(exc, StopIteration) else exc
            ok = not state.note(f"{key}!", f"{key}: {why}") and ok  # a known failure stays quiet
        else:
            state.note(f"{key}!", None)
    return ok


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pins.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in ("sync", "tick"):
        s = sub.add_parser(cmd)
        s.add_argument("--repo", help="only this parent")
        s.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    config.load_env()
    return each_parent(sync_parent if args.cmd == "sync" else tick_parent, args.repo, args.dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
