"""Tests for secret-scan-guard/hooks/secret_scan.py.

Fake secrets are assembled at runtime so this file never contains a literal
token that this guard or GitHub push protection would flag.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "secret-scan-guard" / "hooks" / "secret_scan.py"

AWS_KEY = "AKIA" + "QX7RT2LMZB4KD9WP"
GH_TOKEN = "ghp" + "_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
AWS_DOC_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


def run_hook(payload):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(SCRIPT)], input=data, capture_output=True, text=True, timeout=30
    )


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    (path / ".gitignore").write_text(".env\n")
    return path


def write(path, content):
    return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}}


def bash(repo, command):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}


def config(repo, data):
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "secret-scan.json").write_text(
        data if isinstance(data, str) else json.dumps(data))


# ---------------------------------------------------------------- edits

def test_write_secret_to_tracked_file_blocked(repo):
    r = run_hook(write(repo / "config.py", "KEY = '{}'\n".format(AWS_KEY)))
    assert r.returncode == 2
    assert "AWS access key ID" in r.stderr
    assert AWS_KEY not in r.stderr  # redacted


def test_edit_and_multiedit_scanned(repo):
    edit = {"tool_name": "Edit", "tool_input": {
        "file_path": str(repo / "a.py"), "old_string": "x", "new_string": GH_TOKEN}}
    assert run_hook(edit).returncode == 2
    multi = {"tool_name": "MultiEdit", "tool_input": {"file_path": str(repo / "a.py"), "edits": [
        {"old_string": "a", "new_string": "fine"},
        {"old_string": "b", "new_string": PRIVATE_KEY}]}}
    assert run_hook(multi).returncode == 2


def test_write_to_gitignored_env_allowed(repo):
    assert run_hook(write(repo / ".env", "AWS_KEY=" + AWS_KEY)).returncode == 0


def test_write_outside_repo_allowed(tmp_path):
    assert run_hook(write(tmp_path / "creds.txt", AWS_KEY)).returncode == 0


def test_clean_write_and_placeholder_allowed(repo):
    assert run_hook(write(repo / "a.py", "print('hello')")).returncode == 0
    assert run_hook(write(repo / "README.md", "Example: " + AWS_DOC_KEY)).returncode == 0


def test_allow_paths_and_patterns(repo):
    config(repo, {"allowPaths": ["tests/fixtures/*"], "allowPatterns": ["^ghp_a1B2"]})
    assert run_hook(write(repo / "tests" / "fixtures" / "k.txt", AWS_KEY)).returncode == 0
    assert run_hook(write(repo / "a.py", GH_TOKEN)).returncode == 0
    assert run_hook(write(repo / "a.py", AWS_KEY)).returncode == 2


@pytest.mark.parametrize("text", ["{bad", "[]", '{"allowPaths": "x"}', '{"allowPatterns": ["("]}'])
def test_invalid_config_falls_back(repo, text):
    config(repo, text)
    assert run_hook(write(repo / "a.py", AWS_KEY)).returncode == 2


# ---------------------------------------------------------------- git add / commit

def test_staged_secret_blocks_commit(repo):
    (repo / "settings.py").write_text("TOKEN = '{}'\n".format(GH_TOKEN))
    git(repo, "add", "settings.py")
    r = run_hook(bash(repo, 'git commit -m "feat: add settings"'))
    assert r.returncode == 2
    assert "settings.py" in r.stderr and GH_TOKEN not in r.stderr


def test_clean_commit_allowed(repo):
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "a.py")
    assert run_hook(bash(repo, 'git commit -m "feat: x"')).returncode == 0


def test_commit_all_scans_unstaged_changes(repo):
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-q", "-m", "chore: init")
    (repo / "a.py").write_text("x = '{}'\n".format(AWS_KEY))
    assert run_hook(bash(repo, 'git commit -m "fix: x"')).returncode == 0  # not staged
    assert run_hook(bash(repo, 'git commit -am "fix: x"')).returncode == 2


def test_allow_paths_apply_to_commits(repo):
    config(repo, {"allowPaths": ["fixtures/*"]})
    (repo / "fixtures").mkdir()
    (repo / "fixtures" / "key.pem").write_text(PRIVATE_KEY)
    git(repo, "add", "fixtures")
    assert run_hook(bash(repo, 'git commit -m "test: add fixture"')).returncode == 0


@pytest.mark.parametrize("command", ["git add .env", "git add -f .env.local", "git add certs/server.pem"])
def test_add_sensitive_file_asks(repo, command):
    r = run_hook(bash(repo, command))
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


@pytest.mark.parametrize("command", ["git add .env.example", "git add src/app.py", "ls && echo AKIA"])
def test_harmless_commands_allowed(repo, command):
    r = run_hook(bash(repo, command))
    assert r.returncode == 0 and r.stdout == ""


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"tool_name": "Write", "tool_input": null}'])
def test_malformed_stdin_fails_open(stdin):
    r = run_hook(stdin)
    assert r.returncode == 0 and r.stdout == ""
