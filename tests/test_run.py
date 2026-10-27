"""Tests for run.sh (the manager) across repos. The runner is copied to a temp dir with health.py, inbox.py and
preprod.sh stubbed; claude, gh and docker are stubs on PATH. Never the real state, config or GitHub."""

import json
import os
from datetime import datetime, timezone
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

TOML = """
[repo]
name = "{name}"
github = "o/{name}"
base = "{base}"
merge = "{merge}"
[validate]
fast = "true"
full = "true"
[gate]
high_risk = ["danger/*"]
doc_or_test = ["*.md", "agents/*"]
{preprod}"""

STUB_LOGGER = "#!/usr/bin/env bash\nargs=\"$*\"\necho \"{name} ${{args//$'\\n'/ }}\" >> \"$STUB_LOG\"\n{extra}"


PY_STUB = """import os, sys
open(os.environ["STUB_LOG"], "a").write("{name} " + " ".join(a.replace("\\n", " ") for a in sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("{exit_env}", "0")))
"""


def write_stub(path: Path, name: str, extra: str = "") -> None:
    path.write_text(STUB_LOGGER.format(name=name, extra=extra))
    path.chmod(0o755)


def write_py_stub(path: Path, name: str, exit_env: str = "STUB_NO_EXIT") -> None:
    """A stub for a script run as `python3 <path>`: logs its argv, exits with $exit_env (default 0)."""
    path.write_text(PY_STUB.format(name=name, exit_env=exit_env))


@pytest.fixture
def runner(tmp_path):
    home, state, log = tmp_path / "home", tmp_path / "state", tmp_path / "stub.log"
    home.mkdir()
    code = tmp_path / "runner"
    shutil.copytree(ROOT, code, ignore=shutil.ignore_patterns(".git", "tests", "agents", "__pycache__", ".pytest_cache"))
    write_py_stub(code / "inbox.py", "inbox")
    stub = (code / "inbox.py").read_text()  # `notify` prints the new issue's URL, as inbox.py does
    (code / "inbox.py").write_text(stub.replace("sys.exit(", 'sys.argv[1] == "notify" and print("https://github.com/o/inbox/issues/5")\nsys.exit('))
    write_stub(code / "preprod.sh", "preprod", '[ "$1" = release ] && exit "${STUB_RELEASE_EXIT:-0}"\nexit 0\n')
    write_py_stub(code / "health.py", "health", "STUB_HEALTH_EXIT")
    # versions.py: the real functions (prstatus.py imports them), but running it only logs and exits $STUB_VERSIONS_EXIT.
    real = (code / "versions.py").read_text()
    main = 'if __name__ == "__main__":\n    sys.exit(main(sys.argv[1:]))\n'
    assert real.endswith(main)
    (code / "versions.py").write_text(real.removesuffix(main) + 'if __name__ == "__main__":\n'
                                      + "".join("    " + line + "\n" for line in
                                                PY_STUB.format(name="versions", exit_env="STUB_VERSIONS_EXIT").splitlines()))
    bin_dir = home / ".local/bin"  # run.sh resets PATH, with $HOME/.local/bin first
    bin_dir.mkdir(parents=True)
    # claude: writes $STUB_RESULT (default ready, PR 9) to the result file (or with $STUB_LIMIT, prints a usage-limit
    # message instead) and prints a session id; triage prints $STUB_TRIAGE.
    write_stub(bin_dir / "claude", "claude",
               'if [[ "$*" == *", preprod stage"* ]]; then\n'
               '  ls "$(dirname "$(echo "$*" | grep -o -- "--mcp-config [^ ]*" | cut -d" " -f2)")" >> "$STUB_LOG.mcpdir"\n'
               '  echo "${PREPROD_NETRC:-}" >> "$STUB_LOG.netrc"\n'
               '  cat "$(echo "$*" | grep -o -- "--settings [^ ]*" | cut -d" " -f2)" >> "$STUB_LOG.settings"\n'
               '  [ -n "${STUB_PREPROD:-}" ] && printf "%s" "$STUB_PREPROD" > "$AGENT_RESULT_FILE"\n'
               'elif [[ "$*" == *"You are the validator"* ]]; then\n'
               '  [ -n "${STUB_REVIEW:-}" ] && printf "%s" "$STUB_REVIEW" > "$AGENT_RESULT_FILE"\n'
               'elif [[ "$*" == *"You are triaging"* ]]; then\n'
               '  [ -n "${STUB_TRIAGE:-}" ] && echo "$STUB_TRIAGE"\n'
               'else\n'
               '  git rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1 && echo merging >> "$STUB_LOG.merge"\n'
               '  ready=\'{"status": "ready", "pr": 9}\'\n'
               '  if [ -n "${STUB_LIMIT:-}" ]; then echo "Claude usage limit reached"\n'
               '  else printf "%s" "${STUB_RESULT:-$ready}" > "$AGENT_RESULT_FILE"; fi\n'
               'fi\n'
               'echo \'{"session_id": "s1"}\'\n')
    # gh: `pr checks` fails with $STUB_CHECKS (red; none: "no checks reported"; denied: no access to the check rollup);
    # `pr merge` exits $STUB_MERGE_EXIT; `run list` prints the run ids in $STUB_RUNS, and `run watch` fails for those in
    # $STUB_RUNS_RED; `pr view --json state` prints $STUB_PR_STATE (default OPEN). prstatus.py's `pr view --json body,state`
    # gets an open PR with two Human test steps, `pr edit --body-file` is logged with the body, and the comments query
    # prints the validator's comment URL. `pr view --json mergeable` prints the next word of $STUB_MERGEABLE on each call
    # (the last one repeats; default MERGEABLE), and `--json headRefName` prints agent/x.
    write_stub(bin_dir / "gh", "gh",
               'if [[ "$*" == "pr checks"* ]]; then\n'
               '  [ "${STUB_CHECKS:-}" = red ] && { echo "build  fail  1m  https://ci.example/1"; exit 1; }\n'
               '  [ "${STUB_CHECKS:-}" = none ] && { echo "no checks reported on the \'x\' branch" >&2; exit 1; }\n'
               '  [ "${STUB_CHECKS:-}" = denied ] && { echo "GraphQL: Resource not accessible by personal access token'
               ' (repository.pullRequest.statusCheckRollup)" >&2; exit 1; }\n'
               'elif [[ "$*" == "pr view"*mergeable* ]]; then\n'
               '  n=$(( $(cat "$STUB_LOG.mergeable" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$STUB_LOG.mergeable"\n'
               '  set -- ${STUB_MERGEABLE:-MERGEABLE}; [ "$n" -gt $# ] && n=$#; echo "${!n}"\n'
               'elif [[ "$*" == "pr view"*headRefName* ]]; then\n'
               '  echo agent/x\n'
               'elif [[ "$*" == "pr merge"* ]]; then\n'
               '  exit "${STUB_MERGE_EXIT:-0}"\n'
               'elif [[ "$*" == "pr view"*body,state* ]]; then\n'
               '  echo \'{"body": "## Human test steps\\n1. a\\n2. b\\n", "state": "OPEN"}\'\n'
               'elif [[ "$*" == "pr view"*viewerDidAuthor* ]]; then\n'
               '  echo https://github.com/o/x/pull/9#issuecomment-1\n'
               'elif [[ "$*" == "pr edit"*--body-file* ]]; then\n'
               '  cat "${@: -1}" >> "$STUB_LOG.body"\n'
               'elif [[ "$*" == "pr view"*headRefOid* ]]; then\n'
               '  echo abc123\n'
               'elif [[ "$*" == "pr view"*state* ]]; then\n'
               '  echo "${STUB_PR_STATE:-OPEN}"\n'
               'elif [[ "$*" == "run list"* ]]; then\n'
               '  for id in ${STUB_RUNS:-}; do echo "$id"; done\n'
               'elif [[ "$*" == "run watch"* ]]; then\n'
               '  for id in ${STUB_RUNS_RED:-}; do [ "$3" = "$id" ] && exit 1; done\n'
               'fi\n'
               'exit 0\n')
    write_stub(bin_dir / "docker", "docker")

    def git(*args) -> str:
        return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()

    def register(name: str, base: str = "main", merge: str = "merge", deploy: str | None = "true",
                 plan: str | None = "low", mcp: bool = False, release: bool = False, intake: bool = False,
                 plans: dict[str, str] | None = None) -> None:
        origin, clone = tmp_path / f"{name}.git", tmp_path / f"{name}-clone"
        work = tmp_path / f"{name}-work"
        git("init", "-q", "-b", base, work)
        (work / "agents/plans").mkdir(parents=True)
        health = 'health = [{ url = "https://preprod.example/", status = 200 }]\n'
        preprod = f'[preprod]\ndeploy = "{deploy}"\n{health}' if deploy else ""
        (work / "agents/runner.toml").write_text(TOML.format(name=name, base=base, merge=merge, preprod=preprod)
                                                 + ("[release]\n" if release else "")
                                                 + ("[intake]\nenabled = true\n" if intake else ""))
        if plan:
            (work / "agents/plans/x.md").write_text(f"# Plan: x\n**Risk:** {plan}\n")
        for slug, text in (plans or {}).items():
            (work / f"agents/plans/{slug}.md").write_text(text)
        (work / "README.md").write_text("x\n")
        if mcp:
            (work / "agents/mcp.json").write_text('{"mcpServers": {}}\n')
        git("-C", work, "add", "-A")
        git("-C", work, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base")
        git("clone", "-q", "--bare", work, origin)
        git("-C", origin, "update-ref", "refs/pull/9/head", "HEAD")  # PR #9's head, for the validator's checkout
        git("clone", "-q", origin, clone)
        repos = home / ".config/agent-runner/repos"
        repos.mkdir(parents=True, exist_ok=True)
        (repos / f"{name}.env").write_text(f"AGENT_REPO={clone}\n")

    def queue(*args) -> subprocess.CompletedProcess:
        return subprocess.run(["python3", code / "buildqueue.py", *args], env=env(), capture_output=True, text=True)

    def env(**extra) -> dict:
        e = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_", "STRELITZIA_", "PREPROD_"))}
        e.update(HOME=str(home), AGENT_RUNNER_STATE=str(state), STUB_LOG=str(log), AGENT_MAX_SESSION_USAGE="0",
                 AGENT_INBOX_REPO="o/inbox", AGENT_MERGEABLE_POLL="0", AGENT_CI_SETTLE="0",
                 GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e", PATH=f"{bin_dir}:{os.environ['PATH']}", **extra)
        return e

    def run(**extra) -> subprocess.CompletedProcess:
        out = subprocess.run(["bash", code / "run.sh"], env=env(**extra), capture_output=True, text=True,
                             cwd=tmp_path)
        run.out = out
        return out

    def pr_branch(name: str, conflict: bool) -> None:
        """PR #9's branch agent/x on origin edits README.md; then main edits README.md too (conflict) or another file."""
        origin, work = tmp_path / f"{name}.git", tmp_path / f"{name}-work"
        commit = ["-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qam"]
        git("-C", work, "checkout", "-qb", "agent/x")
        (work / "README.md").write_text("pr\n")
        git("-C", work, *commit, "pr")
        git("-C", work, "checkout", "-q", "main")
        if conflict:
            (work / "README.md").write_text("main\n")
        else:
            (work / "agents/plans/x.md").write_text("# Plan: x\n**Risk:** low\nmore\n")
        git("-C", work, *commit, "main moves on")
        git("-C", work, "push", "-q", origin, "agent/x", "main")

    run.register, run.queue, run.pr_branch, run.git = register, queue, pr_branch, git
    run.calls = lambda: log.read_text().splitlines() if log.exists() else []
    run.runs_log = lambda: (state / "runs.log").read_text() if (state / "runs.log").exists() else ""
    run.status = lambda key: queue("get", key, "status").stdout.strip()
    run.state, run.code = state, code
    return run


def claude_calls(run):
    return [c for c in run.calls() if c.startswith("claude ")]


def test_dry_run_logs_and_stops(runner):
    runner.register("engineerfamily")
    runner.queue("add", "x", "--repo", "engineerfamily")
    runner(AGENT_DRY_RUN="1")
    log = runner.runs_log()
    assert f"would run claude for engineerfamily/x in {runner.state}/worktrees/engineerfamily/x" in log
    assert claude_calls(runner) == [] and not any(c.startswith(("gh ", "inbox poll", "preprod", "versions"))
                                                  for c in runner.calls())
    assert runner.status("engineerfamily/x") == "queued"
    assert not (runner.state / "worktrees/engineerfamily/x").exists()


def test_dry_run_nothing_due(runner):
    runner(AGENT_DRY_RUN="1")
    assert "dry run: nothing due" in runner.runs_log()


def test_unreadable_queue_is_logged_not_nothing_due(runner):
    runner.state.mkdir(parents=True, exist_ok=True)
    item = {"plan": "agents/plans/x.md", "status": "queued", "not_before": "2026-10-01T00:00:00+00:00", "attempts": 0, "notes": []}
    (runner.state / "queue.json").write_text(json.dumps({"paused_until": None, "items": {"x": item}}))
    out = runner(AGENT_DRY_RUN="1")
    assert out.returncode == 1 and "migrate first" in out.stderr
    log = runner.runs_log()
    assert "queue unreadable, no runs" in log and "nothing due" not in log


QUEUED_PLAN = ("# Plan: p\n**Status:** ❌ Not started\n**Risk:** low\n**Queue:** yes\n\n## Tasks\n"
               "### Task 1: t\n**Runs:** unattended\n**Files:** README.md (modify)\n**Status:** ❌\n")


def test_tick_queues_an_opted_in_plan(runner):
    runner.register("engineerfamily", intake=True, plans={"p": QUEUED_PLAN})
    runner()
    assert "intake: queued engineerfamily/p" in runner.runs_log()
    items = json.loads((runner.state / "queue.json").read_text())["items"]
    assert items["engineerfamily/p"]["source"] == "intake"


def test_dry_run_tick_logs_intake_and_writes_nothing(runner):
    runner.register("engineerfamily", intake=True, plans={"p": QUEUED_PLAN})
    runner(AGENT_DRY_RUN="1")
    assert "intake: queued engineerfamily/p" in runner.runs_log()
    assert not (runner.state / "queue.json").exists()


def test_tick_without_intake_queues_nothing(runner):
    runner.register("engineerfamily", plans={"p": QUEUED_PLAN})
    runner()
    assert "intake:" not in runner.runs_log() and runner.status("engineerfamily/p") == ""


def test_plan_missing_on_base_blocks(runner):
    runner.register("engineerfamily", plan=None)
    runner.queue("add", "x", "--repo", "engineerfamily")
    runner()
    assert runner.status("engineerfamily/x") == "blocked"
    assert "agents/plans/x.md is not on origin/main" in runner.runs_log()
    assert claude_calls(runner) == []


def test_prod_command_in_config_blocks(runner):
    runner.register("engineerfamily", deploy="make deploy-prod")
    runner.queue("add", "x", "--repo", "engineerfamily")
    runner()
    assert runner.status("engineerfamily/x") == "blocked"
    assert claude_calls(runner) == []
    assert any(c.startswith("inbox notify") and "runner.toml refused" in c for c in runner.calls())


def test_low_engineerfamily_merges_with_merge_commit(runner):
    runner.register("engineerfamily")
    runner.queue("add", "x", "--repo", "engineerfamily")
    runner()
    calls = runner.calls()
    assert "gh pr merge 9 --repo o/engineerfamily --merge --delete-branch" in calls, calls
    [claude] = claude_calls(runner)
    assert "/implement agents/plans/x.md --unattended" in claude
    assert runner.status("engineerfamily/x") == "done"
    assert not (runner.state / "worktrees/engineerfamily/x").exists()


@pytest.mark.parametrize("env, why", [
    ({"STUB_CHECKS": "red"}, "CI is red or timed out"),
    ({"STUB_CHECKS": "none"}, "no CI checks on this repo"),
    ({"STUB_MERGE_EXIT": "1"}, "gh pr merge failed"),
])
def test_low_not_merged_blocks_with_notice(runner, env, why):
    runner.register("agent-runner", merge="squash", deploy=None)  # no [preprod], like agent-runner itself
    runner.queue("add", "x", "--repo", "agent-runner")
    runner(**env)
    calls = runner.calls()
    assert runner.status("agent-runner/x") == "blocked", runner.runs_log() + runner.out.stderr
    assert "gh pr edit 9 --repo o/agent-runner --add-label ci-failed" in calls
    assert not any("awaiting-preprod" in c for c in calls)
    [notice] = [c for c in calls if c.startswith("inbox notify")]
    assert why in notice and "https://github.com/o/agent-runner/pull/9" in notice
    if "STUB_MERGE_EXIT" not in env:
        assert not any(c.startswith("gh pr merge") for c in calls)
    assert why in json.loads(runner.queue("get", "agent-runner/x", "last_result").stdout)["summary"]


@pytest.mark.parametrize("lock, deploy, released", [
    (None, "true", True),
    ("engineerfamily\t9\tabc\t2026-10-19T00:00:00Z\n", "true", True),  # this PR's own lock
    ("strelitzia\t5\tabc\t2026-10-19T00:00:00Z\n", "true", False),  # another PR holds preprod
    (None, None, False),  # no [preprod]
])
def test_low_merge_puts_preprod_on_the_base(runner, lock, deploy, released):
    runner.register("engineerfamily", deploy=deploy)
    runner.queue("add", "x", "--repo", "engineerfamily")
    if lock:
        runner.state.mkdir(parents=True, exist_ok=True)
        (runner.state / "preprod.lock").write_text(lock)
    runner()
    assert runner.status("engineerfamily/x") == "done", runner.runs_log() + runner.out.stderr
    assert ("preprod release engineerfamily" in runner.calls()) is released
    if deploy:
        assert ("preprod redeployed on engineerfamily's main" if released else "preprod is held by strelitzia#5") in runner.runs_log()


@pytest.mark.parametrize("release", [True, False])
def test_release_repo_is_tagged_before_the_preprod_redeploy(runner, release):
    runner.register("engineerfamily", release=release)
    runner.queue("add", "x", "--repo", "engineerfamily")
    runner()
    calls = runner.calls()
    syncs = [i for i, c in enumerate(calls) if c == "versions sync engineerfamily"]
    assert len(syncs) == (2 if release else 1)  # every tick, after the inbox poll; and after the merge with [release]
    assert calls.index("inbox poll") < syncs[0] < calls.index("preprod tick")
    if release:
        assert syncs[1] == calls.index("preprod release engineerfamily") - 1


def test_versions_sync_failure_notifies_once_per_cause(runner):
    runner.register("engineerfamily")
    notices = lambda: [c for c in runner.calls() if c.startswith("inbox notify") and "versions sync for" in c]
    runner(STUB_VERSIONS_EXIT="3")
    runner(STUB_VERSIONS_EXIT="3")
    assert len(notices()) == 1 and "versions sync for engineerfamily waits for a promote" in notices()[0]
    runner(STUB_VERSIONS_EXIT="4")  # not set up for releases: quiet, and the cause is cleared
    assert len(notices()) == 1 and runner.runs_log().count("versions engineerfamily: exit 3") == 1  # logged once too
    runner(STUB_VERSIONS_EXIT="1")
    assert len(notices()) == 2 and "versions sync for engineerfamily failed" in notices()[1]
    assert runner.out.returncode == 0


def test_failed_preprod_redeploy_notifies(runner):
    runner.register("engineerfamily")
    runner.queue("add", "x", "--repo", "engineerfamily")
    runner(STUB_RELEASE_EXIT="1")
    assert runner.status("engineerfamily/x") == "done"
    assert any(c.startswith("inbox notify") and "preprod redeploy failed after PR #9" in c for c in runner.calls())


@pytest.mark.parametrize("env, why", [
    ({"STUB_RUNS": "1 2"}, None),
    ({"STUB_RUNS": "1 2", "STUB_RUNS_RED": "2"}, "CI is red or timed out"),
    ({"AGENT_CI_WAIT": "0"}, "no CI checks on this repo"),
])
def test_ci_falls_back_to_actions_runs(runner, env, why):
    runner.register("agent-runner", merge="squash", deploy=None)
    runner.queue("add", "x", "--repo", "agent-runner")
    runner(STUB_CHECKS="denied", **env)
    calls = runner.calls()
    assert "gh run list --repo o/agent-runner --commit abc123 --json databaseId -q .[].databaseId" in calls
    merged = "gh pr merge 9 --repo o/agent-runner --squash --delete-branch" in calls
    if why is None:
        assert merged and runner.status("agent-runner/x") == "done", runner.runs_log() + runner.out.stderr
        assert {"gh run watch 1 --repo o/agent-runner --exit-status", "gh run watch 2 --repo o/agent-runner --exit-status"} <= set(calls)
    else:
        assert not merged and runner.status("agent-runner/x") == "blocked"
        [notice] = [c for c in calls if c.startswith("inbox notify")]
        assert why in notice


def test_conflicting_pr_whose_base_merges_cleanly_is_pushed_and_merged(runner, tmp_path):
    runner.register("agent-runner", merge="squash", deploy=None)
    runner.pr_branch("agent-runner", conflict=False)
    runner.queue("add", "x", "--repo", "agent-runner")
    runner(STUB_MERGEABLE="CONFLICTING MERGEABLE")
    assert runner.status("agent-runner/x") == "done", runner.runs_log() + runner.out.stderr
    assert "agent-runner/x: merged main into PR #9 (clean), CI re-runs" in runner.runs_log()
    origin = tmp_path / "agent-runner.git"
    assert runner.git("-C", origin, "merge-base", "--is-ancestor", "main", "agent/x") == ""  # main is in the PR now
    assert "gh pr merge 9 --repo o/agent-runner --squash --delete-branch" in runner.calls()
    assert not any(c.startswith("inbox notify") and "merged PR #9" not in c for c in runner.calls())
    assert runner.queue("get", "agent-runner/x", "conflict").returncode == 1
    assert not (runner.state / "worktrees/agent-runner/sync-9").exists()


def test_real_conflict_gets_conflict_rounds_then_blocks(runner, tmp_path):
    runner.register("agent-runner", merge="squash", deploy=None)
    runner.pr_branch("agent-runner", conflict=True)
    key = "agent-runner/x"
    runner.queue("add", "x", "--repo", "agent-runner")
    get = lambda field: runner.queue("get", key, field).stdout.strip()
    runner(STUB_MERGEABLE="CONFLICTING")
    assert runner.status(key) == "queued", runner.runs_log() + runner.out.stderr
    assert (get("conflict"), get("conflict_rounds"), get("attempts")) == ("1", "1", "0")
    assert "Conflict (" in get("notes") and "PR #9 conflicts with main in README.md" in get("notes")
    calls = runner.calls()
    assert not any("ci-failed" in c for c in calls) and not any(c.startswith("gh pr merge") for c in calls)
    assert not any(c.startswith("inbox notify") for c in calls)
    assert "agent-runner/x: PR #9 conflicts with main in README.md" in runner.runs_log()
    assert not (runner.state / "worktrees/agent-runner/sync-9").exists()
    # Round 1: the runner started the merge, Claude gets the conflict prompt (not /implement).
    runner(STUB_MERGEABLE="CONFLICTING")
    prompt = claude_calls(runner)[-1]
    assert "PR #9 for agents/plans/x.md conflicts with origin/main" in prompt and "conflict markers: README.md" in prompt
    assert "/implement agents/plans" not in prompt
    assert (Path(str(runner.state)).parent / "stub.log.merge").read_text() == "merging\n"
    assert runner.status(key) == "queued" and get("conflict_rounds") == "2" and get("attempts") == "0"
    # Round 2 also ends in a conflict: over the cap, blocked with a conflict notice.
    runner(STUB_MERGEABLE="CONFLICTING")
    assert runner.status(key) == "blocked"
    assert runner.queue("get", key, "conflict").returncode == 1
    [notice] = [c for c in runner.calls() if c.startswith("inbox notify")]
    assert "conflicts with main in README.md" in notice and "no CI checks" not in notice
    assert not any("ci-failed" in c for c in runner.calls())


def test_conflict_round_with_a_clean_merge_skips_claude(runner):
    runner.register("agent-runner", merge="squash", deploy=None)
    runner.pr_branch("agent-runner", conflict=False)
    key = "agent-runner/x"
    runner.queue("add", "x", "--repo", "agent-runner")
    runner.queue("set", key, "pr=9")
    runner.queue("requeue", key, "--conflict", "--note", "PR #9 conflicts with main in README.md")
    runner()
    assert claude_calls(runner) == []
    assert "conflict round: merged main into PR #9 (clean), no Claude run" in runner.runs_log()
    assert runner.status(key) == "done", runner.runs_log() + runner.out.stderr


def test_conflict_round_without_a_result_stays_a_conflict_round(runner):
    runner.register("agent-runner", merge="squash", deploy=None)
    runner.pr_branch("agent-runner", conflict=True)
    key = "agent-runner/x"
    runner.queue("add", "x", "--repo", "agent-runner")
    runner.queue("set", key, "pr=9")
    runner.queue("requeue", key, "--conflict", "--note", "PR #9 conflicts with main in README.md")
    runner(STUB_RESULT=" ")  # Claude writes no usable result: a failed attempt
    assert runner.status(key) == "queued", runner.runs_log() + runner.out.stderr
    assert runner.queue("get", key, "conflict").stdout.strip() == "1"
    runner(STUB_MERGEABLE="CONFLICTING")
    assert all("conflicts with origin/main" in c for c in claude_calls(runner))
    assert len(claude_calls(runner)) == 2


def test_no_checks_on_a_conflicting_pr_says_conflict(runner):
    runner.register("agent-runner", merge="squash", deploy=None)
    runner.queue("add", "x", "--repo", "agent-runner")
    # UNKNOWN through the ready path's and merge_pr's polls; CONFLICTING once "no checks reported" re-checks.
    runner(STUB_CHECKS="none", STUB_MERGEABLE=" ".join(["UNKNOWN"] * 12 + ["CONFLICTING"]))
    assert runner.status("agent-runner/x") == "queued", runner.runs_log() + runner.out.stderr
    notes = runner.queue("get", "agent-runner/x", "notes").stdout
    assert "PR #9 conflicts with main" in notes and "no CI checks" not in notes
    assert "no CI checks" not in runner.runs_log()
    assert not any("ci-failed" in c for c in runner.calls())


def test_preprod_approve_on_a_conflicting_pr_releases_and_requeues(runner):
    key = on_preprod(runner)
    runner.pr_branch("engineerfamily", conflict=True)
    runner(STUB_PREPROD=preprod_verdict("approve"), STUB_MERGEABLE="CONFLICTING")
    calls = runner.calls()
    assert "preprod release engineerfamily" in calls
    assert not any(c.startswith("gh pr merge") for c in calls)
    assert runner.status(key) == "queued" and runner.queue("get", key, "conflict").stdout.strip() == "1"
    assert runner.queue("get", key, "preprod_verdict").returncode == 1


def test_strelitzia_rebases(runner):
    runner.register("strelitzia", base="trunk", merge="rebase")
    runner.queue("add", "strelitzia/x")
    runner()
    assert "gh pr merge 9 --repo o/strelitzia --rebase --delete-branch" in runner.calls(), runner.runs_log() + runner.out.stderr


def test_review_tier_waits_for_the_validator(runner):
    runner.register("engineerfamily", plan="review")
    runner.queue("add", "x", "--repo", "engineerfamily")
    runner()
    calls = runner.calls()
    assert not any(c.startswith("gh pr merge") for c in calls)
    assert runner.status("engineerfamily/x") == "awaiting-review"


def test_settings_fall_back_to_scripts_agent(runner):
    runner.register("engineerfamily")
    runner.queue("add", "x", "--repo", "engineerfamily")
    runner()
    [claude] = claude_calls(runner)
    assert f"--settings {runner.state}/worktrees/engineerfamily/x/scripts/agent/settings.json" in claude


def test_mcp_config_only_when_the_repo_has_one(runner):
    runner.register("agent-runner")
    runner.queue("add", "x", "--repo", "agent-runner")
    runner()
    [claude] = claude_calls(runner)
    assert "--mcp-config" not in claude, runner.runs_log()
    assert runner.status("agent-runner/x") == "done"


def test_mcp_config_from_agents(runner, tmp_path):
    runner.register("engineerfamily", mcp=True)
    runner.queue("add", "x", "--repo", "engineerfamily")
    runner()
    [claude] = claude_calls(runner)
    assert f"--mcp-config {runner.state}/worktrees/engineerfamily/x/agents/mcp.json" in claude


def test_health_gate_stops_runs_not_upkeep(runner):
    runner.register("engineerfamily")
    runner.queue("add", "x", "--repo", "engineerfamily")
    runner(STUB_HEALTH_EXIT="1")
    assert claude_calls(runner) == []
    assert "inbox poll" in runner.calls() and "preprod tick" in runner.calls()
    assert "disk critical, no runs" in runner.runs_log()
    assert runner.status("engineerfamily/x") == "queued"


def awaiting_review(runner, name="engineerfamily", attempts=1, **register):
    runner.register(name, plan="review", **register)
    key = f"{name}/x"
    runner.queue("add", key)
    runner.queue("set", key, "status=awaiting-review", "pr=9", f"attempts={attempts}")
    return key


def verdict(v: str, summary: str = "s") -> str:
    return json.dumps({"verdict": v, "summary": summary, "findings": ["a.py:1: bug"]})


def test_review_dry_run(runner):
    key = awaiting_review(runner)
    runner(AGENT_DRY_RUN="1")
    assert f"would run validator (review) for {key} PR #9" in runner.runs_log()
    assert claude_calls(runner) == []


def test_review_approve_with_preprod_waits_for_preprod(runner):
    key = awaiting_review(runner)
    runner(STUB_REVIEW=verdict("approve"))
    [claude] = claude_calls(runner)
    assert "You are the validator for o/engineerfamily PR #9" in claude
    assert f"--settings {runner.state.parent}/runner/settings/validate.json" in claude
    assert "--permission-mode dontAsk" in claude
    assert runner.status(key) == "awaiting-preprod"
    assert not any(c.startswith("gh pr merge") for c in runner.calls())
    assert not (runner.state / "worktrees/engineerfamily/review-9").exists()


def move_plan_on_pr_head(tmp_path, name="engineerfamily"):
    """PR #9's head moves the plan to agents/plans/done/, as /implement does when a plan is finished."""
    work, origin = tmp_path / f"{name}-work", tmp_path / f"{name}.git"
    git = ["git", "-C", str(work), "-c", "user.email=t@e", "-c", "user.name=t"]
    (work / "agents/plans/done").mkdir()
    subprocess.run([*git, "mv", "agents/plans/x.md", "agents/plans/done/x.md"], check=True)
    subprocess.run([*git, "commit", "-qm", "done"], check=True)
    subprocess.run([*git, "push", "-q", str(origin), "HEAD:refs/pull/9/head"], check=True)


def test_review_prompt_points_at_the_plan_in_place(runner):
    awaiting_review(runner)
    runner(STUB_REVIEW=verdict("approve"))
    [claude] = claude_calls(runner)
    assert "The plan the PR implements: agents/plans/x.md " in claude


def test_review_prompt_finds_a_plan_moved_to_done(runner, tmp_path):
    awaiting_review(runner)
    move_plan_on_pr_head(tmp_path)
    runner(STUB_REVIEW=verdict("approve"))
    [claude] = claude_calls(runner)
    assert "The plan the PR implements: agents/plans/done/x.md " in claude


def test_preprod_prompt_finds_a_plan_moved_to_done(runner, tmp_path):
    on_preprod(runner)
    move_plan_on_pr_head(tmp_path)
    runner(STUB_PREPROD=preprod_verdict("approve"))
    [claude] = claude_calls(runner)
    assert "The plan: agents/plans/done/x.md " in claude


def test_review_approve_without_preprod_merges(runner):
    key = awaiting_review(runner, name="agent-runner", merge="squash", deploy=None)
    runner(STUB_REVIEW=verdict("approve"))
    assert "gh pr merge 9 --repo o/agent-runner --squash --delete-branch" in runner.calls()
    assert runner.status(key) == "done"


def test_review_changes_requeues_with_note(runner):
    key = awaiting_review(runner, attempts=1)
    runner(STUB_REVIEW=verdict("changes", "fix the bug"))
    assert runner.status(key) == "queued"
    assert runner.queue("get", key, "resume").stdout.strip() == "1"
    assert "fix the bug" in runner.queue("get", key, "notes").stdout


def test_review_changes_out_of_attempts_blocks(runner):
    key = awaiting_review(runner, attempts=2)
    runner(STUB_REVIEW=verdict("changes"))
    assert runner.status(key) == "blocked"
    assert any(c.startswith("inbox notify") and "still asks for changes" in c for c in runner.calls())


def test_review_missing_result_twice_blocks(runner):
    key = awaiting_review(runner)
    runner()
    assert runner.status(key) == "awaiting-review"
    runner()
    assert runner.status(key) == "blocked"
    assert any(c.startswith("inbox notify") and "validator failed 2 times" in c for c in runner.calls())


def on_preprod(runner, **fields):
    runner.register("engineerfamily", plan="review")
    key = "engineerfamily/x"
    runner.queue("add", key)
    runner.queue("set", key, "status=on-preprod", "pr=9", "attempts=1", *[f"{k}={v}" for k, v in fields.items()])
    return key


def preprod_verdict(v: str, **extra) -> str:
    return json.dumps({"verdict": v, "passed": ["1"], "failed": extra.get("failed", []),
                       "left_for_you": extra.get("left", []), "summary": "s"})


def test_preprod_dry_run(runner):
    key = on_preprod(runner)
    runner(AGENT_DRY_RUN="1")
    assert f"would run validator (preprod) for {key} PR #9 signed_in=false" in runner.runs_log()


def test_preprod_stage_runs_once_and_cleans_up(runner, tmp_path):
    key = on_preprod(runner)
    runner(STUB_PREPROD=preprod_verdict("approve", left=["2. sign in with your own account"]))
    [claude] = claude_calls(runner)
    assert "--model claude-sonnet-5-5" in claude and "--mcp-config" in claude
    assert "Signed in as the dev test account: false" in claude
    seen = (tmp_path / "stub.log.mcpdir").read_text().split()
    assert {"mcp.json", "mcp.playwright.json", "settings.json"} <= set(seen)
    assert "--settings" in claude
    mcp_dir = claude.split("--mcp-config ")[1].split()[0]
    assert not Path(mcp_dir).exists()  # the temp config (basic-auth password) is gone
    assert runner.queue("get", key, "preprod_verdict").stdout.strip() == "approve"
    runner()  # already has a verdict: no second validator run
    assert len(claude_calls(runner)) == 1


def test_preprod_stage_gets_a_readable_netrc_and_can_comment(runner, tmp_path):
    key = on_preprod(runner)
    netrc = tmp_path / "home/.config/agent-runner/preprod.netrc"  # under the dir the validator may not read
    netrc.write_text("machine preprod.example login u password p\n")
    with open(tmp_path / "home/.config/agent-runner/repos/engineerfamily.env", "a") as f:
        f.write(f"PREPROD_NETRC={netrc}\n")
    runner(STUB_PREPROD=preprod_verdict("approve"))
    [seen] = (tmp_path / "stub.log.netrc").read_text().split()
    assert seen.endswith("/preprod.netrc") and seen != str(netrc), seen  # a private copy, not the config file
    assert not Path(seen).exists()  # removed with the temp dir
    assert "preprod.netrc" in (tmp_path / "stub.log.mcpdir").read_text().split()
    allow = json.loads((tmp_path / "stub.log.settings").read_text())["permissions"]["allow"]
    assert {"Bash(curl:*)", "Bash(gh pr comment:*)", "mcp__playwright"} <= set(allow)


def test_preprod_approve_merges_releases_notifies(runner):
    key = on_preprod(runner)
    runner(STUB_PREPROD=preprod_verdict("approve", left=["2. check the email"]))
    calls = [c for c in runner.calls() if c.startswith(("gh pr merge", "preprod release", "inbox notify"))]
    assert calls[0] == "gh pr merge 9 --repo o/engineerfamily --merge --delete-branch"
    assert calls[1] == "preprod release engineerfamily"
    assert calls[2].startswith("inbox notify") and "merged; 1 steps left for you" in calls[2] and "check the email" in calls[2]
    assert runner.status(key) == "done"


def test_preprod_changes_stays_and_requeues(runner):
    key = on_preprod(runner)
    runner(STUB_PREPROD=preprod_verdict("changes", failed=["3. the page is blank"]))
    calls = runner.calls()
    assert not any(c.startswith("preprod release") for c in calls)  # the PR stays on preprod for the user to look at
    assert not any(c.startswith("gh pr merge") for c in calls)
    assert runner.status(key) == "queued"
    assert "the page is blank" in runner.queue("get", key, "notes").stdout
    assert runner.queue("get", key, "preprod_verdict").returncode == 1  # cleared for the next round


def test_preprod_changes_out_of_attempts_stays_and_notifies(runner):
    key = on_preprod(runner, attempts=2)
    runner(STUB_PREPROD=preprod_verdict("changes", failed=["3. the page is blank"]))
    calls = runner.calls()
    assert not any(c.startswith("preprod release") for c in calls)
    assert runner.status(key) == "blocked"
    [note] = [c for c in calls if c.startswith("inbox notify")]
    assert "preprod checks still failing" in note and "agent-runner preprod release engineerfamily" in note


def test_preprod_blocked_keeps_the_lock_and_notifies(runner):
    key = on_preprod(runner)
    runner(STUB_PREPROD=preprod_verdict("blocked"))
    calls = runner.calls()
    assert not any(c.startswith("preprod release") for c in calls)
    assert runner.status(key) == "blocked"
    [note] = [c for c in calls if c.startswith("inbox notify")]
    assert "preprod validator blocked on PR #9" in note
    assert "It stays on preprod (https://" in note and "agent-runner preprod release engineerfamily" in note


@pytest.mark.parametrize("stage", ["awaiting-review", "on-preprod"])
@pytest.mark.parametrize("state, status", [("MERGED", "done"), ("CLOSED", "cancelled")])
def test_pr_merged_or_closed_by_hand(runner, stage, state, status):
    key = awaiting_review(runner)
    runner.queue("set", key, f"status={stage}")
    runner.state.mkdir(parents=True, exist_ok=True)
    (runner.state / "preprod.lock").write_text("engineerfamily\t9\tabc\t2026-10-19T00:00:00Z\n")  # this PR holds preprod
    runner(STUB_PR_STATE=state, STUB_REVIEW=verdict("approve"), STUB_PREPROD=preprod_verdict("approve"))
    assert runner.status(key) == status, runner.runs_log() + runner.out.stderr
    assert claude_calls(runner) == []
    assert not any(c.startswith("gh pr merge") for c in runner.calls())
    assert "preprod release engineerfamily" in runner.calls()
    summary = json.loads(runner.queue("get", key, "last_result").stdout)["summary"]
    assert summary == f"PR #9 {state.lower()} by hand"


def triage_reply(action, cls, note="", diagnosis="") -> str:
    """The `--output-format json` line of a triage run: its reply is the verdict object."""
    reply = json.dumps({"action": action, "class": cls, "note": note, "diagnosis": diagnosis})
    return json.dumps({"type": "result", "result": f"```json\n{reply}\n```"})


BLOCKED = '{"status": "blocked", "pr": 9, "summary": "CI flaked"}'  # a failed attempt; the second one blocks the item


def test_triage_requeues_once_with_fresh_attempts(runner):
    runner.register("engineerfamily")
    key = "engineerfamily/x"
    runner.queue("add", key)
    runner.queue("set", key, "attempts=1")  # this run is the last attempt
    runner(STUB_RESULT=BLOCKED, STUB_TRIAGE=triage_reply("requeue", "transient", note="CI flaked, try again"))
    assert runner.status(key) == "queued", runner.runs_log() + runner.out.stderr
    assert runner.queue("get", key, "attempts").stdout.strip() == "0"
    assert runner.queue("get", key, "triaged").stdout.strip() == "1"
    assert "CI flaked, try again" in runner.queue("get", key, "notes").stdout
    assert runner.queue("get", key, "resume").returncode != 0
    [triage] = [c for c in claude_calls(runner) if "You are triaging" in c]
    assert "--model claude-sonnet-5-5" in triage and "settings/triage.json" in triage and "pull/9" in triage
    [notice] = [c for c in runner.calls() if c.startswith("inbox notify")]
    assert "re-queued after triage: transient" in notice and "CI flaked, try again" in notice
    # A second block of the same item: no second triage, the plain notice.
    runner.queue("set", key, "attempts=1")
    runner(STUB_RESULT=BLOCKED, STUB_TRIAGE=triage_reply("requeue", "transient"))
    assert runner.status(key) == "blocked"
    assert len([c for c in claude_calls(runner) if "You are triaging" in c]) == 1
    notice = [c for c in runner.calls() if c.startswith("inbox notify")][-1]
    assert "blocked after 2 attempt(s)" in notice and "Triage" not in notice


def test_triage_notify_puts_the_diagnosis_in_the_notice(runner):
    runner.register("engineerfamily")
    key = "engineerfamily/x"
    runner.queue("add", key)
    runner.queue("set", key, "attempts=1")  # this run is the last attempt
    runner(STUB_RESULT=BLOCKED, STUB_TRIAGE=triage_reply("notify", "plan-problem", diagnosis="Task 2 contradicts Task 1"))
    assert runner.status(key) == "blocked"
    [notice] = [c for c in runner.calls() if c.startswith("inbox notify")]
    assert "blocked after 2 attempt(s)" in notice and "Task 2 contradicts Task 1" in notice and "plan-problem" in notice


def test_triage_never_requeues_other_classes(runner):
    runner.register("engineerfamily")
    key = "engineerfamily/x"
    runner.queue("add", key)
    runner.queue("set", key, "attempts=1")  # this run is the last attempt
    runner(STUB_RESULT=BLOCKED, STUB_TRIAGE=triage_reply("requeue", "code-problem", diagnosis="the fix is wrong"))
    assert runner.status(key) == "blocked"
    assert "the fix is wrong" in [c for c in runner.calls() if c.startswith("inbox notify")][0]


def test_no_triage_when_turned_off(runner):
    runner.register("engineerfamily")
    key = "engineerfamily/x"
    runner.queue("add", key)
    runner.queue("set", key, "attempts=1")  # this run is the last attempt
    runner(STUB_RESULT=BLOCKED, STUB_TRIAGE=triage_reply("requeue", "transient"), AGENT_TRIAGE="0")
    assert runner.status(key) == "blocked"
    assert not any("You are triaging" in c for c in claude_calls(runner))
    assert runner.queue("get", key, "triaged").returncode != 0
    assert "blocked after 2 attempt(s)" in [c for c in runner.calls() if c.startswith("inbox notify")][0]


def test_no_triage_when_usage_gate_refuses(runner):
    # usage.py check: the implementer's gate passes, the second check (triage's) is at the limit.
    (runner.code / "usage.py").write_text("import os, sys\nf = os.environ['STUB_LOG'] + '.usage'\n"
                                          "n = len(open(f).read()) if os.path.exists(f) else 0\n"
                                          "open(f, 'a').write('x')\nsys.exit(10 if n else 0)\n")
    runner.register("engineerfamily")
    key = "engineerfamily/x"
    runner.queue("add", key)
    runner.queue("set", key, "attempts=1")  # this run is the last attempt
    runner(STUB_RESULT=BLOCKED, STUB_TRIAGE=triage_reply("requeue", "transient"))
    assert runner.status(key) == "blocked"
    assert not any("You are triaging" in c for c in claude_calls(runner))
    assert runner.queue("get", key, "triaged").returncode != 0
    assert "blocked after 2 attempt(s)" in [c for c in runner.calls() if c.startswith("inbox notify")][0]


def usage_stub(runner, code: int, out: str, then: tuple[int, str] | None = None) -> None:
    """usage.py check: prints `out` and exits `code`; with `then`, only the first call does, and later calls use `then`."""
    later = then or (code, out)
    (runner.code / "usage.py").write_text(
        "import os, sys\nf = os.environ['STUB_LOG'] + '.usage'\nn = os.path.exists(f)\nopen(f, 'a').write('x')\n"
        f"code, out = {later!r} if n else {(code, out)!r}\nprint(out)\nsys.exit(code)\n")


def paused_hours(runner) -> float:
    until = json.loads((runner.state / "queue.json").read_text())["paused_until"]
    return (datetime.fromisoformat(until) - datetime.now(timezone.utc)).total_seconds() / 3600


@pytest.mark.parametrize(("code", "out", "hours", "says"), [
    (10, "weekly cap: 95% ≥ 90%\n30.5", 30.5, "usage limit hit, weekly cap: 95% ≥ 90%; queue paused 30.5h"),
    (11, "usage unknown: probe failed", 5, "usage limit hit, queue paused 5h (usage gate: usage unknown: probe failed)"),
])
def test_usage_limit_exit_pauses_for_the_gate(runner, code, out, hours, says):
    runner.register("engineerfamily")
    key = "engineerfamily/x"
    runner.queue("add", key)
    usage_stub(runner, 0, "usage ok", then=(code, out))  # the implementer starts; the limit exit asks again
    runner(STUB_LIMIT="1")
    assert runner.status(key) == "queued", runner.runs_log()
    assert paused_hours(runner) == pytest.approx(hours, abs=0.05)
    assert says in runner.runs_log()


def test_usage_gate_refusal_logs_rule_and_hours(runner):
    runner.register("engineerfamily")
    key = "engineerfamily/x"
    runner.queue("add", key)
    usage_stub(runner, 10, "weekly pacing: 52% > 46% allowed\n7.25")
    runner()
    assert not claude_calls(runner)
    assert f"{key}: not started, weekly pacing: 52% > 46% allowed; queue paused 7.25h" in runner.runs_log()
    assert paused_hours(runner) == pytest.approx(7.25, abs=0.05)


def test_validator_blocked_is_triaged(runner):
    key = awaiting_review(runner)
    runner(STUB_REVIEW=verdict("blocked", "cannot check out"), STUB_TRIAGE=triage_reply("requeue", "transient", note="retry"))
    assert runner.status(key) == "awaiting-review", runner.runs_log()  # back to the validator: the PR is unchanged
    assert runner.queue("get", key, "triaged").stdout.strip() == "1"
    assert any("re-queued after triage: transient" in c for c in runner.calls())


def test_fresh_attempts_requeue_clears_stage_counters(runner):
    key = on_preprod(runner, preprod_verdict="changes", preprod_attempts=1, review_attempts=1, resume=1)
    runner.queue("set", key, "status=blocked", "attempts=2")
    assert runner.queue("requeue", key, "--note", "flaky", "--fresh-attempts").stdout.strip() == "queued"
    assert runner.queue("get", key, "attempts").stdout.strip() == "0"
    for field in ("resume", "review_attempts", "preprod_verdict", "preprod_attempts"):
        assert runner.queue("get", key, field).returncode == 1, field


def body_edits(runner, tmp_path):
    edits = [c for c in runner.calls() if c.startswith("gh pr edit 9") and "--body-file" in c]
    body = (tmp_path / "stub.log.body").read_text() if (tmp_path / "stub.log.body").exists() else ""
    return edits, body


def test_review_changes_records_fields_and_edits_the_pr_body_once(runner, tmp_path):
    key = awaiting_review(runner, attempts=1)
    runner(STUB_REVIEW=verdict("changes", "Task 3 permissions are too wide. More."))
    get = lambda field: runner.queue("get", key, field).stdout.strip()
    assert (get("review_verdict"), get("review_summary"), get("rerun")) == ("changes", "Task 3 permissions are too wide. More.", "review")
    edits, body = body_edits(runner, tmp_path)
    assert len(edits) == 1, runner.calls()
    assert "**Runner status:** 🔁 Validator review: asked for changes (Task 3 permissions are too wide); " \
           "implementer re-run queued, attempt 2/2" in body
    assert body.rstrip().endswith("## Human test steps\n1. a\n2. b")


def test_preprod_approve_records_fields_and_edits_the_pr_body_once(runner, tmp_path):
    key = on_preprod(runner, review_verdict="approve", tier="review")
    runner(STUB_PREPROD=preprod_verdict("approve", left=["2. check the email"]))
    get = lambda field: runner.queue("get", key, field).stdout.strip()
    assert get("preprod_comment") == "https://github.com/o/x/pull/9#issuecomment-1"
    assert get("left_count") == "1" and get("left_issue") == "https://github.com/o/inbox/issues/5"
    assert get("merged_at").startswith("20")
    edits, body = body_edits(runner, tmp_path)
    assert len(edits) == 1, runner.calls()
    assert "**Runner status:** ✅ Merged; 1 steps left for you (https://github.com/o/inbox/issues/5)" in body
    assert "4. ✅ Second validator runs the Human test steps on preprod and posts a checklist: passed " \
           "(https://github.com/o/x/pull/9#issuecomment-1)" in body
    assert get("pr_status_final") == "1"


def test_blocked_notice_url_is_recorded(runner):
    key = awaiting_review(runner, attempts=2)
    runner(STUB_REVIEW=verdict("changes"))
    assert runner.status(key) == "blocked"
    assert runner.queue("get", key, "notice").stdout.strip() == "https://github.com/o/inbox/issues/5"
    assert runner.queue("get", key, "issue").returncode == 1  # the agent-question field is left alone


def test_dry_run_syncs_without_editing(runner):
    awaiting_review(runner)
    runner(AGENT_DRY_RUN="1")
    assert not any(c.startswith("gh pr edit") for c in runner.calls())
