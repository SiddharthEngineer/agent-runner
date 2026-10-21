"""Tests for the agent-runner CLI and install.sh. Temp HOME and temp repos only; never the real install."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def run(*argv, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, **kwargs)


def git(cwd, *args):
    out = run("git", "-C", cwd, "-c", "user.email=t@e", "-c", "user.name=t", *args)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_cli_through_symlink(tmp_path):
    (tmp_path / "bin").mkdir()
    link = tmp_path / "bin" / "agent-runner"
    link.symlink_to(ROOT / "agent-runner")
    out = run(link, "gate", "--help")
    assert out.returncode == 0 and "usage: gate.py" in out.stdout
    assert run(link, "config", "check", ROOT / "tests/fixtures/strelitzia.runner.toml").stdout == "ok\n"


def test_cli_unknown_and_missing_command():
    out = run(ROOT / "agent-runner", "nope")
    assert out.returncode == 2 and "queue    buildqueue.py" in out.stderr
    assert run(ROOT / "agent-runner").returncode == 2


def test_cli_version_from_git_tag(tmp_path):
    clone = tmp_path / "clone"
    # Leave out VERSION: install.sh writes it into a release before running this suite there, and it would win over the tag.
    shutil.copytree(ROOT, clone, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "VERSION"))
    git(clone, "init", "-q")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "x")
    git(clone, "tag", "v0.0.1")
    assert run(clone / "agent-runner", "version").stdout == "v0.0.1\n"
    (clone / "VERSION").write_text("v9.9.9\n")
    assert run(clone / "agent-runner", "version").stdout == "v9.9.9\n"


@pytest.fixture
def remote(tmp_path):
    """A bare repo with tags v0.0.1..v0.0.5; each release's validate.sh is a stub (v0.0.4's fails)."""
    work, bare = tmp_path / "work", tmp_path / "remote.git"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "agent-runner", work / "agent-runner")
    git(work, "init", "-q", "-b", "main")
    for n in range(1, 6):
        validate = work / "scripts/validate.sh"
        validate.write_text('#!/bin/sh\necho "validate [$*]"\nexit %d\n' % (1 if n == 4 else 0))
        validate.chmod(0o755)
        git(work, "add", "-A")
        git(work, "commit", "-qm", f"release {n}", "--allow-empty")
        git(work, "tag", f"v0.0.{n}")
    run("git", "clone", "-q", "--bare", work, bare)
    return bare


@pytest.fixture
def install(tmp_path, remote):
    """install.sh against `remote`, with a stub gh first on PATH: it prints gh.json, or fails when there is none."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "gh").write_text('#!/bin/sh\necho "$*" >> "$0.log"\ncat "$0.json" 2>/dev/null || exit 1\n')
    (bin_ / "gh").chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path / "home"), "AGENT_RUNNER_REMOTE": str(remote),
           "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}"}

    def go(*args):
        return run(ROOT / "install.sh", *args, env=env)

    go.home = tmp_path / "home"
    go.share = go.home / ".local/share/agent-runner"
    go.current = lambda: os.path.realpath(go.share / "current")
    go.gh_json = bin_ / "gh.json"
    go.gh_log = bin_ / "gh.log"
    go.remote = str(remote)
    return go


def test_install_and_switch(install):
    assert install("v0.0.1").returncode == 0
    assert install.current() == str(install.share / "releases/v0.0.1")
    assert run(install.home / ".local/bin/agent-runner", "version").stdout == "v0.0.1\n"
    assert install("v0.0.2").returncode == 0
    assert install.current() == str(install.share / "releases/v0.0.2")
    assert run(install.home / ".local/bin/agent-runner", "version").stdout == "v0.0.2\n"
    assert install("--list").stdout == "  v0.0.1\n* v0.0.2\n"


def test_install_refuses_branch_and_unknown_tag(install):
    install("v0.0.1")
    assert install("main").returncode == 2
    assert install("v9.9.9").returncode == 2
    assert install.current() == str(install.share / "releases/v0.0.1")


def test_install_aborts_when_validation_fails(install):
    install("v0.0.3")
    out = install("v0.0.4")
    assert out.returncode == 1 and "failed its own validation" in out.stderr
    assert install.current() == str(install.share / "releases/v0.0.3")
    assert not (install.share / "releases/v0.0.4").exists()


def test_install_keeps_three_plus_active(install):
    for tag in ("v0.0.1", "v0.0.2", "v0.0.3", "v0.0.5"):
        assert install(tag).returncode == 0
    assert sorted(p.name for p in (install.share / "releases").iterdir()) == ["v0.0.2", "v0.0.3", "v0.0.5"]
    install("v0.0.1")  # rollback to an old tag: it is active, so it stays beside the newest 3
    assert sorted(p.name for p in (install.share / "releases").iterdir()) == ["v0.0.1", "v0.0.2", "v0.0.3", "v0.0.5"]
    assert install.current() == str(install.share / "releases/v0.0.1")


def tag_sha(install, tag):
    return git(install.share / f"releases/{tag}", "rev-parse", "HEAD").strip()


@pytest.mark.parametrize("runs", [
    [{"status": "completed", "conclusion": "failure"}],
    [{"status": "completed", "conclusion": "failure"}, {"status": "completed", "conclusion": "success"}],
    [],
    [{"status": "in_progress", "conclusion": ""}],
])
def test_install_full_suite_unless_ci_passed(install, runs):
    install.gh_json.write_text(json.dumps(runs))
    out = install("v0.0.1")
    assert out.returncode == 0 and "validate []" in out.stdout and "running the full suite" in out.stdout, out.stdout


def test_install_full_suite_when_gh_fails(install):
    out = install("v0.0.1")  # no gh.json: the stub exits 1
    assert out.returncode == 0 and "gh run list failed" in out.stdout and "validate []" in out.stdout


def test_install_quick_when_ci_passed_on_the_commit(install):
    install.gh_json.write_text('[{"status": "completed", "conclusion": "success"},'
                               ' {"status": "completed", "conclusion": "success"}]')
    out = install("v0.0.2")
    sha = tag_sha(install, "v0.0.2")
    assert out.returncode == 0 and "validate [--quick]" in out.stdout
    assert f"CI passed on {sha[:7]}; ran the quick checks only" in out.stdout
    assert install.current() == str(install.share / "releases/v0.0.2")
    args = install.gh_log.read_text()
    assert f"--commit {sha} --workflow CI" in args and "--repo " in args and "/remote " in args


def test_install_full_flag_ignores_green_ci(install):
    install.gh_json.write_text('[{"status": "completed", "conclusion": "success"}]')
    out = install("--full", "v0.0.1")
    assert out.returncode == 0 and "validate []" in out.stdout and "quick" not in out.stdout
    assert not install.gh_log.exists()
    assert install("--full", "--list").returncode == 2


def test_install_full_suite_when_reused_release_is_not_the_tag(install, remote):
    install.gh_json.write_text('[{"status": "completed", "conclusion": "success"}]')
    assert "validate [--quick]" in install("v0.0.1").stdout
    git(remote, "tag", "-f", "v0.0.1", "v0.0.2")  # the tag moves; releases/v0.0.1 still holds the old commit
    out = install("v0.0.1")
    assert out.returncode == 0 and "not the tag's commit" in out.stdout and "validate []" in out.stdout, out.stdout


def test_install_full_suite_when_release_has_local_changes(install):
    install.gh_json.write_text('[{"status": "completed", "conclusion": "success"}]')
    install("v0.0.1")
    with open(install.share / "releases/v0.0.1/agent-runner", "a") as f:
        f.write("# edited\n")
    out = install("v0.0.1")
    assert out.returncode == 0 and "has local changes" in out.stdout and "validate []" in out.stdout, out.stdout


def test_install_quick_still_aborts_on_failure(install):
    install("v0.0.3")
    install.gh_json.write_text('[{"status": "completed", "conclusion": "success"}]')
    out = install("v0.0.4")
    assert out.returncode == 1 and "validate [--quick]" in out.stdout and "failed its own validation" in out.stderr
    assert install.current() == str(install.share / "releases/v0.0.3")


RUNNER_TOML = """[repo]
name = "{name}"
github = "o/{name}"
base = "{base}"
merge = "merge"
[validate]
fast = "{validate}"
full = "{validate} --full"
[gate]
high_risk = []
doc_or_test = []
"""

PLAN_MD = """Plan for {repo}: run `{validate} --full`, then `gh pr create --repo {github} --base {base}`.
Queue it with `agent-runner queue add <slug> --repo {name}`.
"""


@pytest.fixture
def two_repos(tmp_path):
    home = tmp_path / "home"
    (home / ".config/agent-runner/repos").mkdir(parents=True)

    def make(name, base, validate, extra=""):
        work, bare, clone = tmp_path / f"{name}-w", tmp_path / f"{name}.git", tmp_path / f"{name}-c"
        (work / "agents").mkdir(parents=True)
        (work / ".claude/commands").mkdir(parents=True)
        (work / "agents/runner.toml").write_text(RUNNER_TOML.format(name=name, base=base, validate=validate))
        (work / ".claude/commands/plan.md").write_text(
            PLAN_MD.format(repo=name.capitalize(), name=name, github=f"o/{name}", base=base, validate=validate) + extra)
        git(work, "init", "-q", "-b", base)
        git(work, "add", "-A")
        git(work, "commit", "-qm", "x")
        run("git", "clone", "-q", "--bare", work, bare)
        run("git", "clone", "-q", bare, clone)
        (home / f".config/agent-runner/repos/{name}.env").write_text(f"AGENT_REPO={clone}\n")

    def check(*names):
        return run(ROOT / "agent-runner", "check-commands", *names, env={**os.environ, "HOME": str(home)})

    check.make = make
    return check


def test_check_commands_same_after_placeholders(two_repos):
    two_repos.make("strelitzia", "trunk", "scripts/validate.sh")
    two_repos.make("engineerfamily", "main", "scripts/check.sh")
    out = two_repos("strelitzia", "engineerfamily")
    assert (out.returncode, out.stdout) == (0, "same plan.md\n"), out.stderr


def test_check_commands_shows_extra_line(two_repos):
    two_repos.make("strelitzia", "trunk", "scripts/validate.sh")
    two_repos.make("agent-runner", "main", "scripts/validate.sh", extra="Only here.\n")
    out = two_repos("strelitzia", "agent-runner")
    assert out.returncode == 1
    assert "+Only here." in out.stdout and "--- strelitzia/plan.md" in out.stdout
    assert out.stdout.count("\n+") == 1 + 1  # the +++ header and the one extra line


def test_validate_quick_skips_pytest():
    out = run(ROOT / "scripts/validate.sh", "--quick")
    assert (out.returncode, out.stdout) == (0, "PASS  bash -n\nPASS  py_compile\n"), out.stdout + out.stderr
    assert run(ROOT / "scripts/validate.sh", "--nope").returncode == 2


def test_install_no_tag_takes_the_newest(install):
    out = install()
    assert out.returncode == 0 and "installed v0.0.5" in out.stdout, out.stdout + out.stderr
    assert install.current() == str(install.share / "releases/v0.0.5")
    out = install()
    assert out.returncode == 0 and "v0.0.5 is the newest tag and already installed" in out.stdout
    assert "validate" not in out.stdout


def test_install_older_tag_says_it_rolls_back(install):
    install("v0.0.3")
    assert "rolling back" not in install("v0.0.5").stdout
    out = install("v0.0.2")
    assert out.returncode == 0 and "rolling back v0.0.5 -> v0.0.2" in out.stdout
    assert install.current() == str(install.share / "releases/v0.0.2")


def test_cli_install_runs_install_sh(install, tmp_path):
    env = {"HOME": str(install.home), "AGENT_RUNNER_REMOTE": install.remote,
           "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"}
    out = run(ROOT / "agent-runner", "install", "v0.0.2", env={**os.environ, **env})
    assert out.returncode == 0 and install.current() == str(install.share / "releases/v0.0.2"), out.stderr


@pytest.fixture
def release(tmp_path, remote, install):
    """release.sh with a src clone of `remote`, one untagged commit on main, the same stub gh, and a stub installer."""
    src, work = tmp_path / "src", tmp_path / "work"
    run("git", "clone", "-q", remote, src)
    (work / "new.txt").write_text("new\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "an untagged change")
    git(work, "push", "-q", remote, "main")
    stub = tmp_path / "install-stub"
    stub.write_text('#!/bin/sh\necho "install-stub $*"\n')
    stub.chmod(0o755)
    env = {**os.environ, "HOME": str(install.home), "AGENT_RUNNER_SRC": str(src), "AGENT_RUNNER_INSTALL": str(stub),
           "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"}
    install.gh_json.write_text('[{"status": "completed", "conclusion": "success"}]')

    def go(*args):
        return run(ROOT / "agent-runner", "release", *args, env=env)

    go.remote_tags = lambda: run("git", "--git-dir", remote, "tag", "-l").stdout.split()
    go.gh_json = install.gh_json
    go.work = work
    return go


def test_release_bumps_patch_and_pushes(release):
    out = release()
    assert out.returncode == 0, out.stderr
    assert "v0.0.6 at origin/main" in out.stdout and "an untagged change" in out.stdout and "release 5" not in out.stdout
    assert "pushed v0.0.6" in out.stdout and "agent-runner install v0.0.6" in out.stdout
    assert "v0.0.6" in release.remote_tags()


def test_release_given_version_must_be_newer(release):
    for bad in ("v0.0.5", "v0.0.4", "v0.0.10.1", "v1.0", "1.0.0"):
        out = release(bad)
        assert out.returncode != 0 and "pushed" not in out.stdout, bad
    assert "not newer than the newest tag, v0.0.5" in release("v0.0.5").stderr
    out = release("v0.1.0")
    assert out.returncode == 0 and "v0.1.0" in release.remote_tags(), out.stderr
    assert "not newer than the newest tag, v0.1.0" in release("v0.0.9").stderr


def test_release_version_order_is_numeric(release, remote):
    git(release.work, "tag", "v0.0.9")
    git(release.work, "push", "-q", remote, "v0.0.9")
    git(release.work, "commit", "-qm", "another", "--allow-empty")
    git(release.work, "push", "-q", remote, "main")
    out = release()
    assert out.returncode == 0 and "pushed v0.0.10" in out.stdout, out.stdout + out.stderr


def test_release_refuses_unless_ci_green(release):
    for runs, why in (([], "no CI run"), ([{"status": "in_progress", "conclusion": ""}], "still running"),
                      ([{"status": "completed", "conclusion": "failure"}], "did not pass")):
        release.gh_json.write_text(json.dumps(runs))
        out = release()
        assert out.returncode == 1 and why in out.stderr and "not tagging" in out.stderr
    assert release.remote_tags() == [f"v0.0.{n}" for n in range(1, 6)]


def test_release_refuses_when_main_already_tagged(release):
    assert release().returncode == 0
    out = release()
    assert out.returncode == 1 and "already tagged v0.0.6" in out.stderr


def test_release_dry_run_pushes_nothing(release):
    out = release("--dry-run")
    assert out.returncode == 0 and "v0.0.6 at origin/main" in out.stdout and "dry run" in out.stdout
    assert "v0.0.6" not in release.remote_tags()


def test_release_install_hands_the_tag_to_install(release):
    out = release("--install", "v0.2.0")
    assert out.returncode == 0 and "install-stub v0.2.0" in out.stdout, out.stderr


def test_release_and_install_denied_to_agents():
    for f in ("agents/settings/implement.json", "agents/settings/validate.json", "settings/validate.json",
              "settings/triage.json", "settings/health-diagnose.json"):
        deny = json.loads((ROOT / f).read_text())["permissions"]["deny"]
        for cmd in ("agent-runner release", "agent-runner install", "release.sh"):
            assert f"Bash({cmd}:*)" in deny, (f, cmd)
