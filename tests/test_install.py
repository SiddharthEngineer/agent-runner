"""Tests for the agent-runner CLI and install.sh. Temp HOME and temp repos only; never the real install."""

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
    shutil.copytree(ROOT, clone, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache"))
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
        validate.write_text("#!/bin/sh\nexit %d\n" % (1 if n == 4 else 0))
        validate.chmod(0o755)
        git(work, "add", "-A")
        git(work, "commit", "-qm", f"release {n}", "--allow-empty")
        git(work, "tag", f"v0.0.{n}")
    run("git", "clone", "-q", "--bare", work, bare)
    return bare


@pytest.fixture
def install(tmp_path, remote):
    env = {**os.environ, "HOME": str(tmp_path / "home"), "AGENT_RUNNER_REMOTE": str(remote)}

    def go(*args):
        return run(ROOT / "install.sh", *args, env=env)

    go.home = tmp_path / "home"
    go.share = go.home / ".local/share/agent-runner"
    go.current = lambda: os.path.realpath(go.share / "current")
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
