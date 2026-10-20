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


def run_collect(cwd, *args, env=None):
    r = subprocess.run([sys.executable, str(COLLECT), *args], cwd=cwd, env=env,
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


def test_pre_event_logged(repo, hook_env):
    p = post(repo, "Bash", {"command": "pytest -q"}, hook_event_name="PreToolUse")
    rc, out, rows = run_hook(p, hook_env)
    assert rc == 0 and out == ""
    assert rows[0]["event"] == "pre" and rows[0]["id"] == "t1"
    assert rows[0]["kind"] == "test" and "exit" not in rows[0]
    p = post(repo, "Edit", {"file_path": str(repo / "a.py")}, hook_event_name="PreToolUse")
    assert run_hook(p, hook_env)[2][-1]["path"] == "a.py"


def test_permission_denied_logged_redacted(repo, home, hook_env):
    secret = "ghp_" + "A1b2" * 9
    p = post(repo, "Bash", {"command": "curl -H 'Authorization: Bearer %s' x" % secret},
             hook_event_name="PermissionDenied", reason="blocked token " + secret)
    rc, out, rows = run_hook(p, hook_env)
    assert rc == 0 and out == "" and rows[0]["event"] == "denied"
    assert rows[0]["reason"].startswith("blocked token ") and secret not in all_text(home)


def edits(repo, hook_env, n):
    for i in range(n):
        p = post(repo, "Edit", {"file_path": str(repo / ("f%d.py" % i))}, tool_use_id="e%d" % i)
        run_hook(p, hook_env)


def stop(repo, hook_env, **extra):
    return run_hook({"session_id": "s1", "cwd": str(repo), "hook_event_name": "Stop",
                     "stop_hook_active": False, **extra}, hook_env)


def test_stop_silent_by_default(repo, hook_env):
    edits(repo, hook_env, 3)
    rc, out, rows = stop(repo, hook_env)
    assert rc == 0 and out == "" and not any(r["event"] == "recap_suggested" for r in rows)


def test_stop_suggests_once_when_enabled(repo, hook_env):
    write_config(repo, '{"autoRecap": true}')
    edits(repo, hook_env, 3)
    rc, out, rows = stop(repo, hook_env)
    msg = json.loads(out)["systemMessage"]
    assert rc == 0 and "/session-recap:summary" in msg and "3 files" in msg
    assert "decision" not in json.loads(out)
    assert [r["event"] for r in rows].count("recap_suggested") == 1
    assert stop(repo, hook_env)[1] == ""


def test_stop_needs_three_files(repo, hook_env):
    write_config(repo, '{"autoRecap": true}')
    edits(repo, hook_env, 2)
    assert stop(repo, hook_env)[1] == ""


def test_stop_hook_active_is_silent(repo, hook_env):
    write_config(repo, '{"autoRecap": true}')
    edits(repo, hook_env, 3)
    assert stop(repo, hook_env, stop_hook_active=True)[1] == ""


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


# -- change facts ------------------------------------------------------------

def g(repo, *args, date=None):
    e = dict(os.environ)
    if date:
        e["GIT_COMMITTER_DATE"] = e["GIT_AUTHOR_DATE"] = date
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                        *args], cwd=repo, env=e, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def commit(repo, files, msg, date="2026-10-20T09:00:00Z"):
    for name, text in files.items():
        f = repo / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "-m", msg, date=date)
    return g(repo, "rev-parse", "HEAD")


def by_path(out):
    return {f["path"]: f for f in out["changes"]["files"]}


def write_log(home, repo, rows, sid="s1"):
    d = collect.log_dir(home, str(repo))
    d.mkdir(parents=True, exist_ok=True)
    (d / (sid + ".jsonl")).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return d / (sid + ".jsonl")


def test_change_facts_add_modify_delete_rename(repo):
    body = "".join("line %d of a long enough file\n" % i for i in range(20))
    base = commit(repo, {"a.py": "x\n", "b.py": "y\n", "old.txt": body, "keep.py": "k\n"}, "base")
    (repo / "a.py").write_text("x\nz\n")
    (repo / "b.py").unlink()
    (repo / "new.py").write_text("n\n")
    g(repo, "mv", "old.txt", "moved.txt")
    (repo / "moved.txt").write_text(body + "extra\n")
    g(repo, "add", "-A")
    out = run_collect(repo, "--since", base)
    f = by_path(out)
    assert f["a.py"]["status"] == "modified" and f["a.py"]["insertions"] == 1
    assert f["b.py"]["status"] == "deleted" and f["b.py"]["deletions"] == 1
    assert f["new.py"]["status"] == "added"
    assert f["moved.txt"]["status"] == "renamed" and f["moved.txt"]["old_path"] == "old.txt"
    assert out["changes"]["totals"]["files"] == 4
    assert out["changes"]["by_type"]["py"] == 3
    assert out["changes"]["by_area"]["."] == 4
    assert out["empty"] is False


def test_uncommitted_and_untracked_counted(repo):
    base = commit(repo, {"a.py": "x\n"}, "base")
    (repo / "a.py").write_text("x\ny\n")
    (repo / "src").mkdir()
    (repo / "src" / "u.py").write_text("1\n2\n3\n")
    out = run_collect(repo, "--since", base)
    f = by_path(out)
    assert f["a.py"]["status"] == "modified"
    assert f["src/u.py"]["status"] == "added" and f["src/u.py"]["insertions"] == 3
    assert len(out["changes"]["files"]) == 2
    assert out["changes"]["by_area"]["src"] == 1


def test_unborn_repo_change_facts(repo):
    (repo / "u.py").write_text("1\n2\n")
    out = run_collect(repo)
    assert out["ok"] is True
    assert out["since"] == {"commit": collect.EMPTY_TREE, "source": "empty-tree"}
    assert out["changes"]["commits"] == []
    assert by_path(out)["u.py"]["status"] == "added"
    assert out["empty"] is False


def test_since_ref_and_time(repo):
    a = commit(repo, {"a.py": "1\n"}, "first", "2026-10-20T08:00:00Z")
    commit(repo, {"b.py": "2\n"}, "second", "2026-10-20T10:00:00Z")
    out = run_collect(repo, "--since", a)
    assert out["since"] == {"commit": a, "source": "ref"}
    assert [c["subject"] for c in out["changes"]["commits"]] == ["second"]
    assert len(out["changes"]["commits"][0]["sha"]) == 12
    for since in ("2026-10-20T09:00:00Z", "2026-10-20T09:00:00+00:00"):
        out = run_collect(repo, "--since", since)
        assert out["since"] == {"commit": a, "source": "time"}, since
    out = run_collect(repo, "--since", "2026-10-19")
    assert out["since"] == {"commit": collect.EMPTY_TREE, "source": "time"}
    out = run_collect(repo, "--since", "2.hours.ago")
    assert out["since"]["source"] == "time"


@pytest.mark.parametrize("since", ["nonsense", "yesterday-ish", "not a time"])
def test_since_unknown_is_error(repo, since):
    commit(repo, {"a.py": "1\n"}, "first")
    out = run_collect(repo, "--since", since)
    assert out["ok"] is False and out["error"]["code"] == "since-not-found"


def test_default_since_is_session_start(repo, home, hook_env):
    a = commit(repo, {"a.py": "1\n"}, "first", "2026-10-20T08:00:00Z")
    commit(repo, {"b.py": "2\n"}, "second", "2026-10-20T10:00:00Z")
    path = write_log(home, repo, [
        {"ts": "2026-10-20T09:30:00Z", "event": "post", "tool": "Edit", "path": "b.py"},
        {"ts": "2026-10-20T09:00:00Z", "event": "pre", "tool": "Edit"},
    ])
    out = run_collect(repo, env=hook_env)
    assert out["since"] == {"commit": a, "source": "session-start"}
    assert out["session"]["id"] == "s1"
    assert out["session"]["events"] == 2
    assert out["session"]["log_path"] == str(path)


def test_session_flag_selects_log(repo, home, hook_env):
    commit(repo, {"a.py": "1\n"}, "first")
    write_log(home, repo, [{"ts": "2026-10-20T09:00:00Z", "event": "pre"}], sid="old")
    write_log(home, repo, [{"ts": "2026-10-20T09:00:00Z", "event": "pre"}] * 3, sid="new")
    out = run_collect(repo, "--session", "old", env=hook_env)
    assert out["session"]["id"] == "old" and out["session"]["events"] == 1


def test_default_since_merge_base_without_log(repo, hook_env):
    a = commit(repo, {"a.py": "1\n"}, "first")
    g(repo, "branch", "-M", "main")
    g(repo, "checkout", "-q", "-b", "feat/x")
    commit(repo, {"b.py": "2\n"}, "second")
    out = run_collect(repo, env=hook_env)
    assert out["since"] == {"commit": a, "source": "merge-base"}
    assert out["session"] == {"id": None, "log_path": None, "events": 0}


def test_base_not_found(repo, hook_env):
    commit(repo, {"a.py": "1\n"}, "first")
    out = run_collect(repo, "--base", "nope", env=hook_env)
    assert out["ok"] is False and out["error"]["code"] == "base-not-found"


def test_error_precedence(tmp_path, repo):
    assert run_collect(tmp_path, "--bogus")["error"]["code"] == "not-a-repo"
    commit(repo, {"a.py": "1\n"}, "first")
    out = run_collect(repo, "--bogus")
    assert out["error"]["code"] == "bad-arguments"
    out = run_collect(repo, "--since", "nonsense", "--base", "nope")
    assert out["error"]["code"] == "since-not-found"


def test_no_changes_is_empty(repo):
    base = commit(repo, {"a.py": "1\n"}, "first")
    out = run_collect(repo, "--since", base)
    assert out["changes"]["files"] == [] and out["changes"]["commits"] == []
    assert out["empty"] is True


def test_malformed_log_lines_skipped(tmp_path):
    f = tmp_path / "x.jsonl"
    f.write_text('garbage\n[1, 2]\n{"ts": "2026-10-20T09:00:00Z", "ev\n'
                 '{"ts": "2026-10-20T09:00:00Z", "event": "pre"}\n')
    rows = collect.read_log(f)
    assert rows == [{"ts": "2026-10-20T09:00:00Z", "event": "pre"}]
    assert collect.read_log(None) == []
