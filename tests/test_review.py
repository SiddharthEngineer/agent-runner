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
    assert {"Edit", "Bash(git push:*)", "Bash(gh pr merge:*)", "Bash(git commit:*)", "Bash(make:*)", "Bash(sudo:*)",
            "Bash(docker compose:*)"} <= set(perms["deny"])
    for rule in perms["allow"]:
        assert not re.search(r"push|merge|commit", rule), rule
    writes = [r for r in perms["allow"] if r.startswith("Write")]
    assert writes == ["Write(~/.local/state/agent-runner/results/**)"]
