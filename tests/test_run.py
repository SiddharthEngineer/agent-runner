"""Tests for run.sh (the manager) across repos. The runner is copied to a temp dir with health.py, inbox.py and
preprod.sh stubbed; claude, gh and docker are stubs on PATH. Never the real state, config or GitHub."""

import json
import os
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
open(os.environ["STUB_LOG"], "a").write("{name} " + " ".join(sys.argv[1:]) + "\\n")
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
    write_stub(code / "preprod.sh", "preprod")
    write_py_stub(code / "health.py", "health", "STUB_HEALTH_EXIT")
    bin_dir = home / ".local/bin"  # run.sh resets PATH, with $HOME/.local/bin first
    bin_dir.mkdir(parents=True)
    # claude: writes $STUB_RESULT (default ready, PR 9) to the result file and prints a session id.
    write_stub(bin_dir / "claude", "claude",
               'if [[ "$*" == *"You are the validator"* ]]; then\n'
               '  [ -n "${STUB_REVIEW:-}" ] && printf "%s" "$STUB_REVIEW" > "$AGENT_RESULT_FILE"\n'
               'else\n'
               '  printf "%s" "${STUB_RESULT:-{\\"status\\": \\"ready\\", \\"pr\\": 9}}" > "$AGENT_RESULT_FILE"\n'
               'fi\n'
               'echo \'{"session_id": "s1"}\'\n')
    write_stub(bin_dir / "gh", "gh")
    write_stub(bin_dir / "docker", "docker")

    def git(*args) -> str:
        return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()

    def register(name: str, base: str = "main", merge: str = "merge", deploy: str | None = "true",
                 plan: str | None = "low") -> None:
        origin, clone = tmp_path / f"{name}.git", tmp_path / f"{name}-clone"
        work = tmp_path / f"{name}-work"
        git("init", "-q", "-b", base, work)
        (work / "agents/plans").mkdir(parents=True)
        preprod = f'[preprod]\ndeploy = "{deploy}"\n' if deploy else ""
        (work / "agents/runner.toml").write_text(TOML.format(name=name, base=base, merge=merge, preprod=preprod))
        if plan:
            (work / "agents/plans/x.md").write_text(f"# Plan: x\n**Risk:** {plan}\n")
        (work / "README.md").write_text("x\n")
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
                 AGENT_INBOX_REPO="o/inbox", PATH=f"{bin_dir}:{os.environ['PATH']}", **extra)
        return e

    def run(**extra) -> subprocess.CompletedProcess:
        out = subprocess.run(["bash", code / "run.sh"], env=env(**extra), capture_output=True, text=True,
                             cwd=tmp_path)
        run.out = out
        return out

    run.register, run.queue = register, queue
    run.calls = lambda: log.read_text().splitlines() if log.exists() else []
    run.runs_log = lambda: (state / "runs.log").read_text() if (state / "runs.log").exists() else ""
    run.status = lambda key: queue("get", key, "status").stdout.strip()
    run.state = state
    return run


def claude_calls(run):
    return [c for c in run.calls() if c.startswith("claude ")]


def test_dry_run_logs_and_stops(runner):
    runner.register("engineerfamily")
    runner.queue("add", "x", "--repo", "engineerfamily")
    runner(AGENT_DRY_RUN="1")
    log = runner.runs_log()
    assert f"would run claude for engineerfamily/x in {runner.state}/worktrees/engineerfamily/x" in log
    assert claude_calls(runner) == [] and not any(c.startswith(("gh ", "inbox poll", "preprod")) for c in runner.calls())
    assert runner.status("engineerfamily/x") == "queued"
    assert not (runner.state / "worktrees/engineerfamily/x").exists()


def test_dry_run_nothing_due(runner):
    runner(AGENT_DRY_RUN="1")
    assert "dry run: nothing due" in runner.runs_log()


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


def test_strelitzia_rebases(runner):
    runner.register("strelitzia", base="trunk", merge="rebase")
    runner.queue("add", "x")
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
