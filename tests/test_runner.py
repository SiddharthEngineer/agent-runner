"""Tests for the unattended runner's queue (buildqueue.py), risk gate (gate.py),
session-usage gate (usage.py) and preprod lock (preprod.sh)."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent


@pytest.fixture
def q(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_RUNNER_STATE", str(tmp_path / "state"))

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(AGENT / "buildqueue.py"), *args], capture_output=True, text=True)

    def result(data: dict) -> str:
        path = tmp_path / "r.json"
        path.write_text(json.dumps(data))
        return str(path)

    run.result = result
    return run


def test_queue_uses_agent_runner_state_dir(tmp_path):
    """Without $AGENT_RUNNER_STATE the queue lives in ~/.local/state/agent-runner, even with the old $STRELITZIA_AGENT_STATE set."""
    env = {k: v for k, v in os.environ.items() if not k.endswith("_AGENT_STATE") and k != "AGENT_RUNNER_STATE"}
    env["HOME"] = str(tmp_path)
    env["STRELITZIA_AGENT_STATE"] = str(tmp_path / "old-state")

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(AGENT / "buildqueue.py"), *args], env=env, capture_output=True, text=True)

    assert run("add", "an-item", "--repo", "strelitzia").returncode == 0
    listed = run("list")
    assert listed.returncode == 0 and "an-item" in listed.stdout
    assert (tmp_path / ".local/state/agent-runner/queue.json").exists()
    assert not (tmp_path / "old-state").exists()


def test_empty_queue_has_nothing_next(q):
    assert q("next").returncode == 1


def test_future_item_is_not_due(q):
    q("add", "later", "--in", "2h", "--repo", "strelitzia")
    assert q("next").returncode == 1


def test_failed_attempts_end_in_blocked(q):
    q("add", "plan-a", "--repo", "strelitzia")
    for expected in ("queued", "blocked"):
        assert q("next").stdout.strip() == "strelitzia/plan-a"
        q("start", "plan-a")
        assert q("finish", "plan-a", "/nonexistent").stdout.strip() == expected
    assert q("next").returncode == 1


def test_question_then_answer_requeues_with_fresh_attempts(q):
    q("add", "plan-a", "--repo", "strelitzia")
    q("start", "plan-a")
    assert q("finish", "plan-a", q.result({"status": "question"})).stdout.strip() == "waiting-answer"
    q("answer", "plan-a", "use option B")
    assert q("get", "plan-a", "attempts").stdout.strip() == "0"
    assert "use option B" in q("get", "plan-a", "notes").stdout
    assert q("next").stdout.strip() == "strelitzia/plan-a"


def test_answer_sets_resume_and_failed_attempt_clears_it(q):
    q("add", "plan-a", "--repo", "strelitzia")
    q("start", "plan-a")
    q("set", "plan-a", "session_id=abc-123")
    q("finish", "plan-a", q.result({"status": "question"}))
    q("answer", "plan-a", "use option B")
    assert q("get", "plan-a", "resume").stdout.strip() == "1"
    assert q("get", "plan-a", "session_id").stdout.strip() == "abc-123"
    q("start", "plan-a")
    assert q("finish", "plan-a", "/nonexistent").stdout.strip() == "queued"
    assert q("get", "plan-a", "resume").returncode == 1
    assert q("get", "plan-a", "session_id").returncode == 1


def test_add_starts_without_resume(q):
    q("add", "plan-a", "--repo", "strelitzia")
    q("start", "plan-a")
    q("set", "plan-a", "session_id=abc-123", "resume=1")
    q("finish", "plan-a", q.result({"status": "blocked"}))
    q("add", "plan-a", "--repo", "strelitzia")
    assert q("get", "plan-a", "resume").returncode == 1
    assert q("get", "plan-a", "session_id").returncode == 1


def test_unset_removes_the_key(q):
    q("add", "plan-a", "--repo", "strelitzia")
    q("set", "plan-a", "resume=1")
    q("unset", "plan-a", "resume")
    assert q("get", "plan-a", "resume").returncode == 1
    assert q("get", "plan-a", "status").stdout.strip() == "queued"


def test_no_daily_cap_by_default(q, monkeypatch):
    monkeypatch.delenv("AGENT_MAX_RUNS_PER_DAY", raising=False)
    for slug in ("plan-a", "plan-b", "plan-c", "plan-d", "plan-e", "plan-f"):
        q("add", slug, "--repo", "strelitzia")
    for slug in ("plan-a", "plan-b", "plan-c", "plan-d", "plan-e"):
        q("start", slug)
    assert q("next").stdout.strip() == "strelitzia/plan-f"
    assert "no daily cap" in q("list").stdout


def test_daily_cap_and_pause_stop_runs(q, monkeypatch):
    monkeypatch.setenv("AGENT_MAX_RUNS_PER_DAY", "1")
    q("add", "plan-a", "--repo", "strelitzia")
    q("add", "plan-b", "--repo", "strelitzia")
    q("start", "plan-a")
    assert q("next").returncode == 1  # cap reached
    monkeypatch.setenv("AGENT_MAX_RUNS_PER_DAY", "5")
    q("pause", "1")
    assert q("next").returncode == 1


def test_review_result_waits_for_preprod(q):
    q("add", "plan-a", "--repo", "strelitzia")
    q("start", "plan-a")
    assert q("finish", "plan-a", q.result({"status": "review", "pr": 12})).stdout.strip() == "awaiting-preprod"
    assert q("get", "plan-a", "pr").stdout.strip() == "12"



def test_after_waits_until_the_dependency_merged(q):
    q("add", "first", "--repo", "strelitzia")
    q("add", "second", "--after", "first", "--repo", "strelitzia")
    assert q("next").stdout.strip() == "strelitzia/first"
    q("start", "first")
    q("finish", "first", q.result({"status": "review", "pr": 1}))
    assert q("next").returncode == 1  # an open PR isn't enough: the dependency must be merged
    q("set", "first", "status=done")
    assert q("next").stdout.strip() == "strelitzia/second"


@pytest.fixture
def repo(tmp_path):
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "trunk")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_a():\n    assert 1\n")
    (tmp_path / "plan.md").write_text("# Plan\n**Risk:** low (docs)\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("branch", "base")

    def commit(path: str, content: str) -> None:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(content)
        git("add", "-A")
        git("commit", "-qm", f"change {path}")

    commit.path = tmp_path
    return commit


STRELITZIA_CONFIG = AGENT / "tests" / "fixtures" / "strelitzia.runner.toml"


def gate(repo) -> dict:
    out = subprocess.run(
        [sys.executable, str(AGENT / "gate.py"), "plan.md", "--base", "base", "--config", str(STRELITZIA_CONFIG)],
        cwd=repo.path, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def test_gate_requires_config(repo):
    out = subprocess.run([sys.executable, str(AGENT / "gate.py"), "plan.md"], cwd=repo.path, capture_output=True, text=True)
    assert out.returncode == 2
    assert "usage: gate.py" in out.stderr and "--config is required" in out.stderr


def test_gate_low_for_docs(repo):
    repo("docs/notes.md", "hello\n")
    assert gate(repo)["tier"] == "low"


def test_gate_high_risk_path_needs_review(repo):
    repo("Dockerfile", "FROM python\n")
    assert gate(repo)["tier"] == "review"


def test_gate_weakened_test_needs_review(repo):
    repo("tests/test_x.py", "def test_a():\n    pass\n")
    assert any("assert" in r for r in gate(repo)["reasons"])


def test_gate_over_budget_needs_review(repo):
    repo("src/pkg/big.py", "x = 1\n" * 300)
    assert gate(repo)["tier"] == "review"


def test_gate_respects_declared_review(repo):
    (repo.path / "plan.md").write_text("# Plan\n**Risk:** review (schema)\n")
    repo("docs/notes.md", "hello\n")
    assert gate(repo)["tier"] == "review"


def plan_gate(tmp_path, files: str, *config: str) -> tuple[int, dict]:
    plan = tmp_path / "p.md"
    plan.write_text(f"# Plan\n**Risk:** low (small)\n\n### Task 1: x\n**Files:** {files}\n")
    out = subprocess.run([sys.executable, str(AGENT / "gate.py"), str(plan), "--plan", *config], capture_output=True, text=True)
    return out.returncode, json.loads(out.stdout)


def test_gate_plan_low_listing_high_risk_file_needs_review(tmp_path):
    files = "src/strelitzia/web/tasks.py (modify), tests/test_web.py (modify)"
    code, verdict = plan_gate(tmp_path, files, "--config", str(STRELITZIA_CONFIG))
    assert (code, verdict["tier"]) == (1, "review")
    assert verdict["reasons"] == ["touches high-risk path src/strelitzia/web/tasks.py"]


def test_gate_plan_without_config_has_no_high_risk_paths(tmp_path):
    code, verdict = plan_gate(tmp_path, "src/strelitzia/web/tasks.py (modify), Dockerfile (modify)")
    assert (code, verdict) == (0, {"tier": "low", "reasons": [], "split": []})


def test_gate_plan_low_listing_tests_and_docs_stays_low(tmp_path):
    code, verdict = plan_gate(tmp_path, "tests/test_x.py (new), docs/notes.md (modify), `README.md` (modify)")
    assert (code, verdict) == (0, {"tier": "low", "reasons": [], "split": []})


def test_gate_plan_over_size_budget_says_split_without_changing_tier(tmp_path):
    files = ", ".join(f"tests/test_{i}.py (new)" for i in range(13))
    code, verdict = plan_gate(tmp_path, files)
    assert (code, verdict["tier"]) == (0, "low")
    assert verdict["split"] == ["13 planned files > budget 12"]


def test_planned_files_strips_suffixes_across_tasks(tmp_path):
    spec = importlib.util.spec_from_file_location("gate", AGENT / "gate.py")
    gate_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate_mod)
    plan = tmp_path / "p.md"
    plan.write_text(
        "**Files:** a.py (new), b/c.py (modify)\n**What:** x, y\n\n**Files:** a.py (modify), `d.md` (modify); e.py,\n"
        "f.py (new, user data)\n**Status:** x\n**Files:**\n- g.py (new)\n- `h.py` (modify)\n\nother, text\n"
    )
    assert gate_mod.planned_files(plan) == ["a.py", "b/c.py", "d.md", "e.py", "f.py", "g.py", "h.py"]


def _usage():
    spec = importlib.util.spec_from_file_location("agent_usage", AGENT / "usage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rate_event(utilization, resets_at=2_000_000_000):
    info = {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": resets_at,
            "unifiedWindows": {"five_hour": {"utilization": utilization, "resetsAt": resets_at},
                               "seven_day": {"utilization": 0.9, "resetsAt": resets_at}}}
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


def test_usage_reads_five_hour_window_not_seven_day():
    lines = ['{"type": "system", "subtype": "init"}', "not json", _rate_event(0.11), '{"type": "result"}']
    assert _usage().five_hour_window(lines) == (0.11, 2_000_000_000)


def test_usage_missing_event_is_unknown():
    assert _usage().five_hour_window(['{"type": "result"}']) is None


@pytest.mark.parametrize(("utilization", "code"), [(0.29, 0), (0.30, 10), (0.95, 10)])
def test_usage_check_against_limit(monkeypatch, capsys, utilization, code):
    usage = _usage()
    monkeypatch.setenv("AGENT_MAX_SESSION_USAGE", "30")
    monkeypatch.setattr(usage, "probe", lambda: [_rate_event(utilization)])
    assert usage.cmd_check() == code
    if code == 10:
        assert float(capsys.readouterr().out.splitlines()[-1]) > 0  # hours until reset, for buildqueue.py pause


@pytest.mark.parametrize(("utilization", "code"), [(0.69, 0), (0.70, 10)])
def test_usage_check_default_limit_is_70(monkeypatch, utilization, code):
    usage = _usage()
    monkeypatch.delenv("AGENT_MAX_SESSION_USAGE", raising=False)
    monkeypatch.setattr(usage, "probe", lambda: [_rate_event(utilization)])
    assert usage.cmd_check() == code


def test_usage_check_unknown_and_disabled(monkeypatch):
    usage = _usage()
    monkeypatch.setattr(usage, "probe", lambda: [])
    monkeypatch.setenv("AGENT_MAX_SESSION_USAGE", "30")
    assert usage.cmd_check() == 11
    monkeypatch.setenv("AGENT_MAX_SESSION_USAGE", "0")
    monkeypatch.setattr(usage, "probe", lambda: pytest.fail("probe must not run when the check is disabled"))
    assert usage.cmd_check() == 0



PREPROD_TOML = """
[repo]
name = "{name}"
github = "o/{name}"
base = "trunk"
merge = "rebase"
[validate]
fast = "true"
full = "true"
[gate]
high_risk = []
doc_or_test = []
[preprod]
deploy = "{deploy}"
{extra}
"""


@pytest.fixture
def preprod(tmp_path):
    """preprod.sh against temp repos: an origin with agents/runner.toml on trunk, a dev clone (AGENT_REPO), a stub gh.

    pp.config(name, deploy, extra) writes that repo's runner.toml and repos/<name>.env; pp(script) sources preprod.sh with
    sleep and health_check stubbed, then runs the script. Commands write to pp.log.
    """
    log = tmp_path / "log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        f"open({str(log)!r}, 'a').write('gh ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "if 'headRefOid' in sys.argv: print('abc123')\n"
        "if '.state' in sys.argv: print('OPEN')\n"
    )
    gh.chmod(0o755)

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()

    def config(name: str, deploy: str, extra: str = "") -> Path:
        origin, clone = tmp_path / f"{name}-origin", tmp_path / f"{name}-clone"
        if not origin.exists():
            git("init", "-q", "-b", "trunk", str(origin))
            git("-C", str(origin), "config", "user.email", "t@example.com")
            git("-C", str(origin), "config", "user.name", "t")
        (origin / "agents").mkdir(exist_ok=True)
        text = PREPROD_TOML.format(name=name, deploy=deploy.replace("LOG", str(log)), extra=extra.replace("LOG", str(log)))
        (origin / "agents" / "runner.toml").write_text(text)
        git("-C", str(origin), "add", "-A")
        git("-C", str(origin), "commit", "-qm", "config")
        if not clone.exists():
            git("clone", "-q", str(origin), str(clone))
        repos = tmp_path / ".config/agent-runner/repos"
        repos.mkdir(parents=True, exist_ok=True)
        (repos / f"{name}.env").write_text(f"AGENT_REPO={clone}\n")
        return origin

    def run(script: str, stub_health: bool = True) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PREPROD_", "STRELITZIA_AGENT"))}
        env.update(HOME=str(tmp_path), AGENT_RUNNER_STATE=str(tmp_path / "state"), PATH=f"{bin_dir}:{os.environ['PATH']}",
                   AGENT_INBOX_REPO="")
        stub = "health_check() { echo healthy; }; " if stub_health else ""
        full = f'. "{AGENT / "preprod.sh"}"; sleep() {{ echo slept >> "{log}"; }}; {stub}{script}'
        return subprocess.run(["bash", "-c", full], env=env, capture_output=True, text=True)

    run.config = config
    run.git = git
    run.log = lambda: log.read_text() if log.exists() else ""
    run.lock = tmp_path / "state" / "preprod.lock"
    return run


def test_preprod_three_column_lock_reads_as_strelitzia(preprod):
    preprod.lock.parent.mkdir(parents=True, exist_ok=True)
    preprod.lock.write_text("12\tdeadbeef\t2026-10-19T00:00:00Z\n")
    assert preprod("holder").stdout == "strelitzia 12\n"
    preprod.lock.write_text("engineerfamily\t5\tdeadbeef\t2026-10-19T00:00:00Z\n")
    assert preprod("holder").stdout == "engineerfamily 5\n"


def test_preprod_busy_names_the_holder(preprod):
    preprod.lock.parent.mkdir(parents=True, exist_ok=True)
    preprod.lock.write_text("strelitzia\t3\tdeadbeef\t2026-10-19T00:00:00Z\n")
    out = preprod("cmd_deploy engineerfamily 5")
    assert out.returncode == 4
    assert out.stdout.strip() == "preprod busy: strelitzia#3"
    assert preprod.log() == ""


def test_preprod_deploy_uses_runner_toml_from_base(preprod):
    preprod.config("strelitzia", "echo deploy {sha} >> LOG", 'seed = "echo seeded >> LOG"')
    out = preprod("cmd_deploy strelitzia 7")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "deploy abc123" in preprod.log() and "seeded" in preprod.log()
    assert preprod.lock.read_text().startswith("strelitzia\t7\tabc123\t")
    assert "gh pr comment 7 --repo o/strelitzia" in preprod.log()


def test_preprod_app_dir_checks_out_the_sha(preprod, tmp_path):
    origin = preprod.config("strelitzia", "true")
    app = tmp_path / "app"
    preprod.git("clone", "-q", str(origin), str(app))
    head = preprod.git("-C", str(origin), "rev-parse", "HEAD")
    preprod.config("strelitzia", f"git -C {app} rev-parse HEAD >> LOG", f'app_dir = "{app}"')
    (origin / "x").write_text("pr change\n")
    preprod.git("-C", str(origin), "add", "-A")
    preprod.git("-C", str(origin), "commit", "-qm", "pr")
    pr_sha = preprod.git("-C", str(origin), "rev-parse", "HEAD")
    preprod.git("-C", str(origin), "update-ref", "refs/pull/7/head", pr_sha)
    preprod.git("-C", str(origin), "reset", "-q", "--hard", "HEAD~1")
    assert head != pr_sha
    out = preprod(f'load_repo strelitzia && deploy_ref pull/7/head {pr_sha}')
    assert out.returncode == 0, out.stdout + out.stderr
    assert [line for line in preprod.log().split() if line != "slept"] == [pr_sha]


def test_preprod_refuses_prod_command(preprod):
    preprod.config("strelitzia", "make -C /srv/engineerfamily-prod deploy-prod")
    out = preprod("cmd_deploy strelitzia 7")
    assert out.returncode == 3
    assert "looks like a prod command" in out.stdout
    assert "headRefOid" not in preprod.log() and not preprod.lock.exists()


def test_preprod_release_defaults_to_deploy_at_base(preprod):
    origin = preprod.config("engineerfamily", "echo deploy {sha} >> LOG")
    base = preprod.git("-C", str(origin), "rev-parse", "HEAD")
    preprod.lock.parent.mkdir(parents=True, exist_ok=True)
    preprod.lock.write_text("engineerfamily\t5\tabc123\t2026-10-19T00:00:00Z\n")
    out = preprod("cmd_release")
    assert out.returncode == 0 and "preprod reset to engineerfamily trunk" in out.stdout
    assert f"deploy {base}" in preprod.log() and not preprod.lock.exists()


def test_preprod_tick_picks_oldest_across_repos(preprod, tmp_path):
    preprod.config("engineerfamily", "echo ef {sha} >> LOG")
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    items = {
        "strelitzia/a": {"repo": "strelitzia", "status": "awaiting-preprod", "pr": 3, "updated": "2026-10-19T02:00:00+00:00"},
        "engineerfamily/b": {"repo": "engineerfamily", "status": "awaiting-preprod", "pr": 5, "updated": "2026-10-19T01:00:00+00:00"},
    }
    (state / "queue.json").write_text(json.dumps({"paused_until": None, "items": items}))
    out = preprod("cmd_tick")
    assert "engineerfamily#5 deployed to preprod" in out.stdout, out.stdout + out.stderr
    assert json.loads((state / "queue.json").read_text())["items"]["engineerfamily/b"]["status"] == "on-preprod"


@pytest.fixture
def health_server():
    """A local HTTP server: /ok 200, /bad 502, /json-true and /json-false bodies. Yields its base URL."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code, body = {"/ok": (200, b"fine"), "/bad": (502, b"Bad Gateway" * 100), "/json-true": (200, b'{"ok": true}'),
                          "/json-false": (200, b'{"ok": false}')}.get(self.path, (404, b""))
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def health(preprod, lines: list[str]) -> subprocess.CompletedProcess:
    checks = "\\n".join(lines)
    return preprod(f"NETRC=; HEALTH=$(printf '{checks}'); health_check", stub_health=False)


def test_health_status_200_passes(preprod, health_server):
    out = health(preprod, [f"{health_server}/ok\\t200\\tfalse", f"{health_server}/json-true\\t\\ttrue"])
    assert (out.returncode, out.stdout.strip()) == (0, "healthy"), out.stderr
    assert "slept" not in preprod.log()


def test_health_502_fails_after_retries(preprod, health_server):
    out = health(preprod, [f"{health_server}/bad\\t200\\tfalse"])
    assert out.returncode == 1
    assert f"health failed: {health_server}/bad returned 502" in out.stdout
    assert len(out.stdout.split(": ", 2)[2].strip()) <= 300
    assert preprod.log().count("slept") == 5


def test_health_json_ok_false_fails(preprod, health_server):
    out = health(preprod, [f"{health_server}/json-false\\t\\ttrue"])
    assert out.returncode == 1 and "json-false returned 200" in out.stdout


EF_CONFIG = AGENT / "tests" / "fixtures" / "engineerfamily.runner.toml"


def gate_with_config(repo) -> dict:
    out = subprocess.run(
        [sys.executable, str(AGENT / "gate.py"), "plan.md", "--base", "base", "--config", str(EF_CONFIG)],
        cwd=repo.path, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def test_gate_config_nginx_change_needs_review(repo):
    repo("services/nginx/x.conf", "server {}\n")
    verdict = gate_with_config(repo)
    assert verdict["tier"] == "review"
    assert verdict["reasons"] == ["touches high-risk path services/nginx/x.conf"]


def test_gate_config_readme_change_is_low(repo):
    repo("README.md", "hello\n")
    assert gate_with_config(repo)["tier"] == "low"


def test_gate_config_patterns_replace_defaults(repo):
    repo("Dockerfile", "FROM python\n")  # high-risk for Strelitzia, not on engineerfamily's list (only services/*/Dockerfile)
    assert gate(repo)["tier"] == "review"
    assert gate_with_config(repo)["tier"] == "low"


def test_gate_plan_with_config_uses_its_high_risk(tmp_path):
    plan = tmp_path / "p.md"
    plan.write_text("# Plan\n**Risk:** low (small)\n\n### Task 1: x\n**Files:** Makefile (modify), src/strelitzia/web/x.py (modify)\n")
    out = subprocess.run(
        [sys.executable, str(AGENT / "gate.py"), str(plan), "--plan", "--config", str(EF_CONFIG)], capture_output=True, text=True
    )
    assert json.loads(out.stdout)["reasons"] == ["touches high-risk path Makefile"]


def test_gate_bad_config_is_a_usage_error(tmp_path):
    plan = tmp_path / "p.md"
    plan.write_text("# Plan\n**Risk:** low\n")
    bad = tmp_path / "runner.toml"
    bad.write_text("[repo]\n")
    out = subprocess.run([sys.executable, str(AGENT / "gate.py"), str(plan), "--plan", "--config", str(bad)], capture_output=True, text=True)
    assert out.returncode == 2
    assert "missing repo.name" in out.stderr


@pytest.fixture
def fake_gh(tmp_path):
    """A `gh` on PATH that logs its argv (one JSON list per line) and answers `issue create` with an issue URL."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\nimport json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['issue', 'create']: print('https://github.com/o/r/issues/42')\n"
        "if sys.argv[1:3] == ['issue', 'comment']: print('https://github.com/o/r/issues/42#issuecomment-1')\n"
        f"if sys.argv[1:3] == ['issue', 'view']: print(open({str(tmp_path / 'view.json')!r}).read())\n"
        f"if sys.argv[1:3] == ['issue', 'list']: print(open({str(tmp_path / 'list.json')!r}).read())\n"
    )
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path), "AGENT_INBOX_REPO": "o/r",
           "AGENT_RUNNER_STATE": str(tmp_path / "state")}
    env.pop("NTFY_TOPIC", None)
    env.pop("STRELITZIA_AGENT_STATE", None)

    def inbox(*args):
        return subprocess.run([sys.executable, str(AGENT / "inbox.py"), *args], env=env, capture_output=True, text=True)

    def queue(*args):
        return subprocess.run([sys.executable, str(AGENT / "buildqueue.py"), *args], env=env, capture_output=True, text=True)

    inbox.queue = queue
    inbox.tmp = tmp_path
    inbox.issues = lambda titles: (tmp_path / "list.json").write_text(
        json.dumps([{"number": n, "title": t} for n, t in titles.items()]))
    inbox.view = lambda comments: (tmp_path / "view.json").write_text(json.dumps({"comments": [{"body": b} for b in comments]}))

    inbox.calls = lambda: [json.loads(line) for line in log.read_text().splitlines()]
    return inbox


def test_inbox_notify_project_label(fake_gh):
    out = fake_gh("notify", "--project", "vps-health", "--json", "--title", "[vps] disk", "--body", "b")
    assert json.loads(out.stdout) == {"number": 42, "url": "https://github.com/o/r/issues/42"}
    calls = fake_gh.calls()
    assert ["label", "create", "vps-health", "--repo", "o/r", "--force"] in calls
    create = next(c for c in calls if c[:2] == ["issue", "create"])
    assert "vps-health" in create and "strelitzia" not in create
    assert not any("strelitzia" in c for c in calls)


def test_inbox_notify_defaults_to_strelitzia(fake_gh):
    out = fake_gh("notify", "--title", "t", "--body", "b")
    assert out.stdout.strip() == "https://github.com/o/r/issues/42"
    assert "strelitzia" in next(c for c in fake_gh.calls() if c[:2] == ["issue", "create"])


def test_inbox_comment_close(fake_gh):
    assert fake_gh("comment", "42", "--body", "Recovered", "--close").returncode == 0
    calls = fake_gh.calls()
    assert calls[0][:3] == ["issue", "comment", "42"] and "<!-- agent-runner -->" in calls[0][-1]
    assert calls[1] == ["issue", "close", "42", "--repo", "o/r"]


def test_queue_bare_keys_are_an_error(tmp_path, q):
    state = tmp_path / "state"
    state.mkdir()
    old = {"paused_until": None, "items": {
        "a": {"plan": "agents/plans/a.md", "status": "done", "not_before": "2026-10-01T00:00:00+00:00", "attempts": 1, "notes": []},
    }}
    (state / "queue.json").write_text(json.dumps(old))
    out = q("next")
    assert out.returncode != 0 and "migrate first" in out.stderr
    assert json.loads((state / "queue.json").read_text()) == old  # not rewritten


def test_queue_bare_after_is_an_error(tmp_path, q):
    state = tmp_path / "state"
    state.mkdir()
    item = {"repo": "strelitzia", "slug": "b", "plan": "agents/plans/b.md", "status": "queued",
            "not_before": "2026-10-01T00:00:00+00:00", "attempts": 0, "notes": [], "after": "a"}
    (state / "queue.json").write_text(json.dumps({"paused_until": None, "items": {"strelitzia/b": item}}))
    out = q("list")
    assert out.returncode != 0 and "migrate first" in out.stderr


def test_queue_add_requires_repo(q):
    out = q("add", "x")
    assert out.returncode != 0 and "--repo is required" in out.stderr
    assert q("add", "strelitzia/x").returncode == 0
    assert q("next").stdout.strip() == "strelitzia/x"


def test_queue_after_across_repos(q):
    q("add", "y", "--repo", "strelitzia")
    q("add", "x", "--repo", "engineerfamily", "--after", "strelitzia/y")
    q("set", "y", "status=running")
    assert q("next").returncode == 1
    q("set", "strelitzia/y", "status=done")
    assert q("next").stdout.strip() == "engineerfamily/x"
    assert q("get", "engineerfamily/x", "repo").stdout.strip() == "engineerfamily"


def test_queue_ambiguous_bare_slug(q):
    q("add", "x", "--repo", "strelitzia")
    q("add", "x", "--repo", "engineerfamily")
    out = q("set", "x", "status=done")
    assert out.returncode != 0
    assert "engineerfamily/x" in out.stderr and "strelitzia/x" in out.stderr


def test_inbox_ask_labels_item_repo(fake_gh):
    fake_gh.queue("add", "x", "--repo", "engineerfamily")
    body = fake_gh.tmp / "q.md"
    body.write_text("which option?")
    assert fake_gh("ask", "engineerfamily/x", "--title", "q", "--body-file", str(body)).returncode == 0
    create = next(c for c in fake_gh.calls() if c[:2] == ["issue", "create"])
    assert create[create.index("--title") + 1] == "[engineerfamily/x] q"
    labels = [create[i + 1] for i, a in enumerate(create) if a == "--label"]
    assert labels == ["agent-question", "engineerfamily"]
    assert fake_gh.queue("get", "engineerfamily/x", "status").stdout.strip() == "waiting-answer"


def test_inbox_poll_ignores_legacy_marker_and_requeues(fake_gh):
    fake_gh.queue("add", "x", "--repo", "engineerfamily")
    fake_gh.queue("set", "engineerfamily/x", "status=waiting-answer", "issue=42")
    fake_gh.view(["question\n\n<!-- strelitzia-agent -->", "old\n<!-- applypilot-agent -->"])
    assert fake_gh("poll").stdout == ""
    assert fake_gh.queue("get", "engineerfamily/x", "status").stdout.strip() == "waiting-answer"
    fake_gh.view(["question\n\n<!-- strelitzia-agent -->", "use option B"])
    assert "re-queued engineerfamily/x" in fake_gh("poll").stdout
    assert fake_gh.queue("get", "engineerfamily/x", "status").stdout.strip() == "queued"
    assert "use option B" in fake_gh.queue("get", "engineerfamily/x", "notes").stdout


def test_inbox_notify_slug_uses_item_repo(fake_gh):
    fake_gh.queue("add", "x", "--repo", "engineerfamily")
    fake_gh("notify", "--slug", "x", "--title", "t", "--body", "b")
    create = next(c for c in fake_gh.calls() if c[:2] == ["issue", "create"])
    assert "engineerfamily" in create and "strelitzia" not in create


def test_inbox_sweep_closes_notices_of_finished_items(fake_gh):
    for slug in ("merged", "gone", "busy", "todo"):
        fake_gh.queue("add", slug, "--repo", "engineerfamily")
    fake_gh.queue("set", "engineerfamily/merged", "status=done")
    fake_gh.queue("set", "engineerfamily/gone", "status=cancelled")
    fake_gh.queue("set", "engineerfamily/busy", "status=blocked")
    fake_gh.queue("set", "engineerfamily/todo", "status=done")
    fake_gh.issues({1: "[engineerfamily/merged] merged PR #4", 2: "[gone] validator blocked on PR #5",
                    3: "[engineerfamily/busy] blocked after 2 attempt(s)", 4: "[vps] disk 70% used",
                    5: "[engineerfamily/todo] merged; 2 steps left for you", 6: "[engineerfamily/todo] merged; 0 steps left for you",
                    7: "[nope] not queued", 8: "no key"})
    assert fake_gh("sweep", "--dry-run").stdout.count("would close") == 3
    assert not any(c[:2] == ["issue", "close"] for c in fake_gh.calls())
    out = fake_gh("sweep")
    assert out.returncode == 0, out.stderr
    closed = [c[2] for c in fake_gh.calls() if c[:2] == ["issue", "close"]]
    assert closed == ["1", "2", "6"]
    assert "engineerfamily/gone` is cancelled" in next(c for c in fake_gh.calls() if c[:3] == ["issue", "comment", "2"])[-1]
