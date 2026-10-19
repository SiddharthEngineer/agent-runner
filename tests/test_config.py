"""Tests for config.py: the runner.toml loader, the prod guard, and the config/state dirs."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

MINIMAL = """
[repo]
name = "demo"
github = "o/demo"
base = "main"
merge = "squash"
[validate]
fast = "v"
full = "v --full"
[gate]
high_risk = ["x"]
doc_or_test = ["*.md"]
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "runner.toml"
    path.write_text(text)
    return path


def test_fixtures_load():
    s = config.load(FIXTURES / "strelitzia.runner.toml")
    assert (s.repo.name, s.repo.base, s.repo.merge, s.repo.plans) == ("strelitzia", "trunk", "rebase", "agents/plans")
    assert "scripts/agent/*" in s.gate.high_risk
    assert s.preprod.health[0].json_ok
    e = config.load(FIXTURES / "engineerfamily.runner.toml")
    assert (e.repo.base, e.repo.merge, e.gate.code_line_budget, e.gate.tests_dir) == ("main", "merge", 250, "tests/")
    assert [h.status for h in e.preprod.health] == [200, 200, None]
    assert "{sha}" in e.preprod.deploy


def test_minimal_has_no_preprod(tmp_path):
    assert config.load(write(tmp_path, MINIMAL)).preprod is None


def test_every_problem_reported_at_once(tmp_path):
    text = MINIMAL.replace('base = "main"\n', "").replace('"squash"', '"ff"')
    with pytest.raises(config.ConfigError) as err:
        config.load(write(tmp_path, text))
    problems = err.value.problems
    assert "missing repo.base" in problems
    assert any("repo.merge 'ff'" in p for p in problems)


def test_wrong_types_reported(tmp_path):
    text = MINIMAL.replace('high_risk = ["x"]', 'high_risk = "x"\ncode_line_budget = "big"')
    with pytest.raises(config.ConfigError) as err:
        config.load(write(tmp_path, text))
    assert "gate.high_risk must be a list of strings" in err.value.problems
    assert "gate.code_line_budget must be an integer" in err.value.problems


def test_bad_toml_is_a_config_error(tmp_path):
    with pytest.raises(config.ConfigError):
        config.load(write(tmp_path, "[repo\n"))


@pytest.mark.parametrize("command", [
    "make deploy-prod",
    "make tag-prod",
    "make strelitzia-deploy",
    "make -C /srv/engineerfamily-prod preprod-deploy",
    "docker compose -p engineerfamily up -d",
    "docker compose -p engineerfamily",
    "docker compose -p engineerfamily-prod up -d",
])
def test_prod_commands_refused(tmp_path, command):
    assert len(config.PROD_PATTERNS) == 6
    text = MINIMAL + f'[preprod]\ndeploy = "{command}"\n'
    with pytest.raises(config.ConfigError) as err:
        config.load(write(tmp_path, text))
    assert any("preprod.deploy looks like a prod command" in p for p in err.value.problems)


def test_prod_guard_checks_release_and_seed(tmp_path):
    text = MINIMAL + '[preprod]\ndeploy = "ok"\nrelease = "make tag-prod"\nseed = "make deploy-prod"\n'
    with pytest.raises(config.ConfigError) as err:
        config.load(write(tmp_path, text))
    assert {p.split()[0] for p in err.value.problems} == {"preprod.release", "preprod.seed"}


def test_preprod_commands_accepted(tmp_path):
    text = MINIMAL + '[preprod]\ndeploy = "make -C /srv/engineerfamily-preprod preprod-deploy ref={sha}"\n'
    assert config.load(write(tmp_path, text)).preprod.deploy.endswith("ref={sha}")


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "config.py"), *args], capture_output=True, text=True)


def test_cli_check_and_get(tmp_path):
    assert cli("check", str(FIXTURES / "strelitzia.runner.toml")).stdout == "ok\n"
    assert cli("get", str(FIXTURES / "engineerfamily.runner.toml"), "repo.base").stdout == "main\n"
    out = cli("get", str(FIXTURES / "engineerfamily.runner.toml"), "gate.high_risk").stdout.splitlines()
    assert out[0] == "services/nginx/*"
    assert cli("get", str(FIXTURES / "engineerfamily.runner.toml"), "repo.nope").returncode == 2
    bad = cli("check", str(write(tmp_path, MINIMAL.replace('base = "main"\n', ""))))
    assert (bad.returncode, bad.stdout) == (1, "missing repo.base\n")
