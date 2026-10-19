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
