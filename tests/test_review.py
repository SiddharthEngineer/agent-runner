"""Tests for the validator's prompt template and its settings (read-only except its result file)."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_prompt_formats_with_every_placeholder():
    template = (ROOT / "prompts/review-pr.md").read_text()
    assert set(re.findall(r"(?<!\{)\{(\w+)\}(?!\})", template)) == {"repo", "pr", "plan_path", "result_file", "validate_full"}
    text = template.format(repo="o/r", pr=9, plan_path="agents/plans/x.md", result_file="/s/x.review.json",
                           validate_full="scripts/validate.sh --full")
    assert "gh pr diff 9 --repo o/r" in text and "/s/x.review.json" in text and "scripts/validate.sh --full" in text
    assert '{"verdict": "approve"' in text
    assert "{" + "repo" + "}" not in text


def test_settings_are_read_only():
    perms = json.loads((ROOT / "settings/validate.json").read_text())["permissions"]
    assert {"Edit(./**)", "Bash(git push:*)", "Bash(gh pr merge:*)", "Bash(git commit:*)", "Bash(make:*)", "Bash(sudo:*)",
            "Bash(docker compose:*)"} <= set(perms["deny"])
    for rule in perms["allow"]:
        assert not re.search(r"push|merge|commit", rule), rule
    writes = [r for r in perms["allow"] if r.startswith(("Write", "Edit"))]
    assert writes == ["Edit(~/.local/state/agent-runner/results/**)"]


def test_preprod_prompt_formats():
    template = (ROOT / "prompts/preprod-check.md").read_text()
    names = set(re.findall(r"(?<!\{)\{(\w+)\}(?!\})", template))
    assert names == {"repo", "pr", "plan_path", "base_url", "result_file", "signed_in"}
    text = template.format(repo="o/r", pr=9, plan_path="p.md", base_url="https://preprod.example/", result_file="/r.json",
                           signed_in="false")
    assert "gh pr comment 9 --repo o/r" in text and '"left_for_you"' in text


def mcp(tmp_path, *extra):
    import subprocess
    import sys

    out = tmp_path / "mcp.json"
    res = subprocess.run([sys.executable, str(ROOT / "mcp_config.py"), "write", str(out), "--host", "preprod.example", *extra],
                         capture_output=True, text=True)
    return res, out


def test_mcp_config_has_credentials_and_is_private(tmp_path):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine preprod.example login bot password s3cret\n")
    netrc.chmod(0o600)
    state = tmp_path / "state.json"
    state.write_text("{}")
    res, out = mcp(tmp_path, "--netrc", str(netrc), "--storage-state", str(state))
    assert res.stdout.strip() == "signed_in=true" and "s3cret" not in res.stdout + res.stderr
    pw = out.with_suffix(".playwright.json")
    for path in (out, pw):
        assert path.stat().st_mode & 0o777 == 0o600
    context = json.loads(pw.read_text())["browser"]["contextOptions"]
    assert context["httpCredentials"] == {"username": "bot", "password": "s3cret", "origin": "https://preprod.example"}
    assert context["storageState"] == str(state)
    args = json.loads(out.read_text())["mcpServers"]["playwright"]["args"]
    assert args[-2:] == ["--config", str(pw)]


def test_mcp_config_without_storage_state(tmp_path):
    res, out = mcp(tmp_path, "--storage-state", str(tmp_path / "missing.json"))
    assert res.stdout.strip() == "signed_in=false"
    assert "storageState" not in json.loads(out.with_suffix(".playwright.json").read_text())["browser"]["contextOptions"]
