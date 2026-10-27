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
    assert names == {"repo", "pr", "plan_path", "base_url", "result_file", "signed_in", "netrc_file"}
    text = template.format(repo="o/r", pr=9, plan_path="p.md", base_url="https://preprod.example/", result_file="/r.json",
                           signed_in="false", netrc_file="/tmp/x/preprod.netrc")
    assert "gh pr comment 9 --repo o/r" in text and '"left_for_you"' in text
    # a $VAR in the curl command is refused by the validator's permission rules, so the path must be literal
    assert "--netrc-file /tmp/x/preprod.netrc" in text and "$PREPROD_NETRC" not in text
    # text first: the accessibility snapshot reads pages, a screenshot only for a step about appearance
    assert "`browser_snapshot`" in text and "`browser_take_screenshot`" in text and "at most once per step" in text


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


def test_resolve_prompt_formats():
    template = (ROOT / "prompts/resolve.md").read_text()
    names = set(re.findall(r"(?<!\{)\{(\w+)\}(?!\})", template))
    assert names == {"key", "plan", "pr_url", "stage", "block_title", "block_body", "result_json", "log_paths", "ci_log",
                     "validate_full", "result_file"}
    text = template.format(key="r/x", plan="agents/plans/x.md", pr_url="https://github.com/o/r/pull/9", stage="merge",
                           block_title="PR #9 not merged", block_body="CI is red", result_json='{"status": "blocked"}',
                           log_paths="/s/logs/a.log", ci_log="/s/results/r/x.ci-failed.log", validate_full="make check",
                           result_file="/s/results/r/x.resolve.json")
    assert "Stopped at stage: merge" in text and "/s/results/r/x.ci-failed.log" in text and "make check" in text
    assert '{"action": "fixed|requeue|give-up"' in text and "/s/results/r/x.resolve.json" in text


def test_rereview_prompt_formats():
    template = (ROOT / "prompts/rereview-pr.md").read_text()
    names = set(re.findall(r"(?<!\{)\{(\w+)\}(?!\})", template))
    assert names == {"repo", "pr", "plan_path", "result_file", "validate_full", "since_sha", "label", "findings"}
    text = template.format(repo="o/r", pr=9, plan_path="p.md", result_file="/s/x.review.json",
                           validate_full="scripts/validate.sh --full", since_sha="abc123", label="round 1 of 3",
                           findings="a.py:3: off by one\nb.py:7: missing test")
    assert "git diff abc123..HEAD" in text and "Re-review (round 1 of 3)" in text and "b.py:7: missing test" in text
    assert "gh pr review 9 --repo o/r --comment" in text and "/s/x.review.json" in text and "scripts/validate.sh --full" in text
    assert '"unresolved": [' in text and '{"verdict": "approve"' in text
    assert "/code-review` skill" not in text and "{" + "since_sha" + "}" not in text
