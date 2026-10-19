"""Keep every test off the real state dir, even when pytest runs under run.sh (which exports $AGENT_RUNNER_STATE)."""

import pytest

RUN_VARS = ("STRELITZIA_AGENT_STATE", "AGENT_RESULT_FILE", "AGENT_SLUG", "AGENT_REPO", "PREPROD_NETRC")


@pytest.fixture(autouse=True)
def hermetic_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_RUNNER_STATE", str(tmp_path / "state"))
    for key in RUN_VARS:
        monkeypatch.delenv(key, raising=False)
