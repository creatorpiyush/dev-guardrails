"""Tests for session-recap/scripts/collect_recap.py.

Each test builds a throwaway repo and runs the script as a subprocess.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COLLECT = ROOT / "session-recap" / "scripts" / "collect_recap.py"

spec = importlib.util.spec_from_file_location("collect_recap", str(COLLECT))
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)


def run_collect(cwd, *args):
    r = subprocess.run([sys.executable, str(COLLECT), *args], cwd=cwd,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "init", "-q"], cwd=path, check=True, capture_output=True)
    return path


def write_config(root, text):
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "session-recap.json").write_text(text)


def test_not_a_repo_is_json_error(tmp_path):
    out = run_collect(tmp_path)
    assert out["ok"] is False
    assert out["error"]["code"] == "not-a-repo"


def test_bad_arguments_is_json_error(repo):
    out = run_collect(repo, "--bogus")
    assert out["ok"] is False
    assert out["error"]["code"] == "bad-arguments"


def test_success_echoes_save_and_config(repo):
    out = run_collect(repo, "--save")
    assert out["ok"] is True
    assert out["save_requested"] is True
    assert out["config"] == collect.DEFAULT_CONFIG
    assert out["warnings"] == []


def test_config_defaults_when_missing(repo):
    assert collect.load_config(str(repo)) == (collect.DEFAULT_CONFIG, [])


def test_valid_config_overrides(repo):
    write_config(repo, '{"retentionDays": 3, "autoRecap": true}')
    cfg, warnings = collect.load_config(str(repo))
    assert cfg["retentionDays"] == 3 and cfg["autoRecap"] is True
    assert cfg["maxWords"] == 250 and warnings == []


@pytest.mark.parametrize("text", [
    '{"retentionDays": "x"}', '{"retentionDays": true}', '{"retentionDays": 0}',
    '{"maxWords": 10}', '{"autoRecap": 1}', '{"baseBranch": ""}',
    '{"testCommandKinds": ["pytest", ""]}', '{"testCommandKinds": "pytest"}',
])
def test_invalid_config_falls_back_with_warning(repo, text):
    write_config(repo, text)
    cfg, warnings = collect.load_config(str(repo))
    assert cfg == collect.DEFAULT_CONFIG
    assert len(warnings) == 1
    key = text.split('"')[1]
    assert key in warnings[0]


def test_invalid_json_falls_back_with_warning(repo):
    write_config(repo, "{nope")
    cfg, warnings = collect.load_config(str(repo))
    assert cfg == collect.DEFAULT_CONFIG
    assert len(warnings) == 1


def test_unknown_config_key_warns(repo):
    write_config(repo, '{"colour": 1}')
    cfg, warnings = collect.load_config(str(repo))
    assert cfg == collect.DEFAULT_CONFIG
    assert len(warnings) == 1 and "colour" in warnings[0]


# -- log_activity hook -------------------------------------------------------

HOOK = ROOT / "session-recap" / "hooks" / "log_activity.py"
hook_spec = importlib.util.spec_from_file_location("log_activity", str(HOOK))
log_activity = importlib.util.module_from_spec(hook_spec)
hook_spec.loader.exec_module(log_activity)

SENTINEL = "S3NTINEL"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def hook_env(home):
    e = dict(os.environ)
    e.pop("SESSION_RECAP_DISABLE", None)
    e["HOME"] = e["USERPROFILE"] = str(home)
    return e


def run_hook(payload, env):
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    r = subprocess.run([sys.executable, str(HOOK)], input=stdin, env=env,
                        capture_output=True, text=True)
    rows = []
    base = Path(env["HOME"]) / ".claude" / "session-recap"
    if base.is_dir():
        for f in sorted(base.glob("*/*.jsonl")):
            rows += [json.loads(line) for line in f.read_text().splitlines()]
    return r.returncode, r.stdout, rows


def post(repo, tool, tool_input, **extra):
    return {"session_id": "s1", "cwd": str(repo), "hook_event_name": "PostToolUse",
            "tool_name": tool, "tool_input": tool_input, "tool_use_id": "t1", **extra}


def all_text(home):
    return "".join(f.read_text() for f in home.rglob("*") if f.is_file())


def test_edit_event_logs_path_and_count_not_content(repo, home, hook_env):
    p = post(repo, "Write", {"file_path": str(repo / "a.py"), "content": SENTINEL})
    rc, out, rows = run_hook(p, hook_env)
    assert rc == 0 and out == ""
    assert rows[0]["event"] == "post" and rows[0]["path"] == "a.py" and rows[0]["edits"] == 1
    assert SENTINEL not in all_text(home)


def test_multiedit_counts_edits(repo, hook_env):
    edits = [{"old_string": "a", "new_string": "b"}] * 3
    _, _, rows = run_hook(post(repo, "MultiEdit",
                               {"file_path": str(repo / "a.py"), "edits": edits}), hook_env)
    assert rows[0]["edits"] == 3


def test_bash_post_logs_exit_zero_and_duration(repo, home, hook_env):
    p = post(repo, "Bash", {"command": "ls"}, duration_ms=1234,
             tool_response={"stdout": SENTINEL, "interrupted": False})
    _, _, rows = run_hook(p, hook_env)
    assert rows[0]["exit"] == 0 and rows[0]["duration_ms"] == 1234
    assert SENTINEL not in all_text(home)


def test_failure_event_logs_exit_code(repo, home, hook_env):
    p = post(repo, "Bash", {"command": "false"}, hook_event_name="PostToolUseFailure",
             error="Exit code 3\nboom " + SENTINEL)
    _, _, rows = run_hook(p, hook_env)
    assert rows[0]["event"] == "fail" and rows[0]["exit"] == 3
    assert SENTINEL not in all_text(home)


def test_session_id_cannot_escape_log_dir(repo, home, hook_env):
    p = post(repo, "Bash", {"command": "ls"}, session_id="../../evil")
    run_hook(p, hook_env)
    files = [f for f in home.rglob("*.jsonl")]
    assert len(files) == 1
    assert files[0].name == "______evil.jsonl"
    assert files[0].parent == log_activity.log_dir(home, str(repo.resolve()))


def test_edit_path_relative_to_root_from_subdir(repo, hook_env):
    (repo / "sub").mkdir()
    p = post(repo, "Edit", {"file_path": str(repo.resolve() / "sub" / "x.py")}, cwd=str(repo / "sub"))
    _, _, rows = run_hook(p, hook_env)
    assert rows[0]["path"] == "sub/x.py"


def test_outside_repo_logs_nothing(tmp_path, home, hook_env):
    plain = tmp_path / "plain"
    plain.mkdir()
    rc, _, rows = run_hook(post(plain, "Bash", {"command": "ls"}), hook_env)
    assert rc == 0 and rows == []


def test_disable_env_var(repo, home, hook_env):
    hook_env["SESSION_RECAP_DISABLE"] = "1"
    rc, _, _ = run_hook(post(repo, "Bash", {"command": "ls"}), hook_env)
    assert rc == 0 and not (home / ".claude" / "session-recap").exists()


def test_malformed_stdin_fails_open(hook_env):
    rc, out, _ = run_hook("{not json", hook_env)
    assert rc == 0 and out == ""


def test_log_write_error_fails_open(repo, home, hook_env):
    (home / ".claude").mkdir()
    (home / ".claude" / "session-recap").write_text("not a dir")
    rc, out, _ = run_hook(post(repo, "Bash", {"command": "ls"}), hook_env)
    assert rc == 0 and out == ""


def test_pre_stop_denied_write_nothing_yet(repo, home, hook_env):
    for ev in ("PreToolUse", "PermissionDenied", "Stop"):
        rc, out, rows = run_hook(post(repo, "Bash", {"command": "ls"}, hook_event_name=ev), hook_env)
        assert rc == 0 and out == "" and rows == []


def test_retention_prunes_old_logs(repo, home, hook_env):
    other = home / ".claude" / "session-recap" / "otherhash"
    other.mkdir(parents=True)
    old, mid = other / "old.jsonl", other / "mid.jsonl"
    old.write_text("{}\n")
    mid.write_text("{}\n")
    now = time.time()
    os.utime(old, (now - 15 * 86400,) * 2)
    os.utime(mid, (now - 13 * 86400,) * 2)
    run_hook(post(repo, "Bash", {"command": "ls"}), hook_env)
    assert not old.exists() and mid.exists()
    assert not (home / ".claude" / "session-recap" / "otherhash" / "old.jsonl").exists()


def test_repo_retention_only_shortens_its_own_logs(repo, home, hook_env):
    base = home / ".claude" / "session-recap"
    own = log_activity.log_dir(home, str(repo.resolve()))
    other = base / "otherhash"
    own.mkdir(parents=True)
    other.mkdir(parents=True)
    mine, theirs = own / "old.jsonl", other / "keep.jsonl"
    for f in (mine, theirs):
        f.write_text("{}\n")
        os.utime(f, (time.time() - 13 * 86400,) * 2)
    write_config(repo, '{"retentionDays": 1}')
    run_hook(post(repo, "Bash", {"command": "ls"}), hook_env)
    assert not mine.exists() and theirs.exists()


# -- classify / redact ---------------------------------------------------------

import random
import string

_rng = random.Random(4321)


def _rand(n):
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "xX")
    return "".join(_rng.choice(alphabet) for _ in range(n))


DEFAULT_TESTS = ["pytest", "jest", "go test"]


@pytest.mark.parametrize("command,kind,integration", [
    ("pytest -q", "test", False),
    ("python -m pytest tests/x.py", "test", False),
    ("cd a && npm test", "test", False),
    ("sudo env X=1 pytest", "test", False),
    ("bash -c 'cargo test'", "test", False),
    ("pytest tests/integration", "test", True),
    ("npx jest e2e/", "test", True),
    ("ruff check .", "lint", False),
    ("mypy src", "typecheck", False),
    ("npm run build", "build", False),
    ("pip install -r r.txt", "install", False),
    ("git status", "git", False),
    ("ls", "other", False),
    ("pip install x && pytest", "test", False),
    ("go test ./...", "test", False),
    ("uv run pytest", "test", False),
    ("echo 'unbalanced", "other", False),
    ("/usr/bin/python3 -m pytest", "test", False),
    ("./.venv/bin/pytest -q", "test", False),
    (".venv/bin/python -m pytest", "test", False),
    ("python3.11 -m pytest", "test", False),
    ("node_modules/.bin/jest", "test", False),
    ("/opt/homebrew/bin/ruff check", "lint", False),
    ("C:/Python/python.exe -m pytest", "test", False),
])
def test_classify_kinds(command, kind, integration):
    assert log_activity.classify(command, DEFAULT_TESTS) == (kind, integration)


def test_classify_uses_config_test_commands():
    assert log_activity.classify("bats t/", ["bats"])[0] == "test"
    assert log_activity.classify("bats t/", DEFAULT_TESTS)[0] == "other"


def test_redacts_tokens_and_url_credentials():
    token = "gh" + "p_" + _rand(36)
    out = log_activity.redact("curl -H x:%s https://user:hunter2@host/x" % token)
    assert token not in out and "hunter2" not in out and "host/x" in out


def test_redacts_header_and_multiline():
    token = "gh" + "p_" + _rand(36)
    out = log_activity.redact('curl -H "Authorization: Bearer abc.def" x')
    assert "abc.def" not in out
    assert "s3cr3t" not in log_activity.redact("login --password=s3cr3t")
    assert "s3cr3t" not in log_activity.redact("login --password s3cr3t")
    assert "--password=$PW" in log_activity.redact("login --password=$PW")
    out = log_activity.redact("cat <<EOF\nline\n%s\nEOF" % token)
    assert token not in out and "line" in out


def test_logged_command_is_redacted(repo, home, hook_env):
    token = "gh" + "p_" + _rand(36)
    # the token straddles the 500-char cap: redaction must come before the cut
    cmd = "echo " + "a" * (log_activity.COMMAND_CAP - 12) + " " + token
    _, _, rows = run_hook(post(repo, "Bash", {"command": cmd}), hook_env)
    assert token not in all_text(home) and token[:10] not in all_text(home)
    assert rows[0]["kind"] == "other"
    _, _, rows = run_hook(post(repo, "Bash", {"command": "pytest tests/e2e"}), hook_env)
    assert rows[-1]["kind"] == "test" and rows[-1]["integration"] is True


def test_redacts_user_flag_and_key_headers():
    for cmd, secret in [
        ("curl -u alice:s3cr3t https://x", "s3cr3t"),
        ("curl --user alice:s3cr3t x", "s3cr3t"),
        ("curl --user=alice:s3cr3t x", "s3cr3t"),
        ('curl -H "X-Api-Key: k9secret" x', "k9secret"),
        ("curl -H 'X-Auth-Token: abc123' x", "abc123"),
        ('curl -H "Api-Key: zzz" x', "zzz"),
        ("gpg --passphrase=hunter2", "hunter2"),
    ]:
        assert secret not in log_activity.redact(cmd), cmd
    assert "alice:" in log_activity.redact("curl -u alice:s3cr3t x")
    for cmd in ["curl -u $USER:$PASS x", "curl -u alice https://x", "curl -u alice:$PW x"]:
        assert log_activity.redact(cmd) == cmd
