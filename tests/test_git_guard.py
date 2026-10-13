"""Tests for git-workflow-guard/hooks/git_guard.py.

Each test builds a throwaway git repo, pipes a PreToolUse payload into the
script, and checks the exit code and output.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "git-workflow-guard" / "hooks" / "git_guard.py"


def run_hook(payload):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(SCRIPT)], input=data, capture_output=True, text=True, timeout=30
    )


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A repo on an unborn 'main' branch. Call repo.on(name) to switch branch."""
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "symbolic-ref", "HEAD", "refs/heads/main")

    class Repo:
        root = path

        def on(self, branch):
            git(path, "symbolic-ref", "HEAD", "refs/heads/" + branch)
            return self

        def config(self, text):
            (path / ".claude").mkdir(exist_ok=True)
            (path / ".claude" / "git-guard.json").write_text(text)
            return self

    return Repo()


def edit(file_path):
    return {"tool_name": "Edit", "tool_input": {"file_path": str(file_path),
                                                "old_string": "a", "new_string": "b"}}


def bash(repo, command):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo.root)}


def decision(result):
    out = json.loads(result.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


# ---------------------------------------------------------------- edits

def test_edit_on_main_blocked(repo):
    r = run_hook(edit(repo.root / "src" / "app.py"))
    assert r.returncode == 2
    assert "protected branch" in r.stderr
    assert "git switch -c" in r.stderr


def test_edit_on_feature_allowed(repo):
    repo.on("feat/thing")
    r = run_hook(edit(repo.root / "app.py"))
    assert r.returncode == 0 and r.stdout == ""


def test_release_prefix_protected(repo):
    repo.on("release/1.2")
    assert run_hook(edit(repo.root / "app.py")).returncode == 2


def test_remote_default_branch_protected(repo):
    git(repo.root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/mainline")
    repo.on("mainline")
    assert run_hook(edit(repo.root / "app.py")).returncode == 2


def test_detached_head_allowed(repo):
    git(repo.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
        "--allow-empty", "-m", "chore: init")
    git(repo.root, "checkout", "-q", "--detach")
    assert run_hook(edit(repo.root / "app.py")).returncode == 0


# ---------------------------------------------------------------- commits

def test_commit_on_main_blocked(repo):
    r = run_hook(bash(repo, 'git commit -m "feat: add thing"'))
    assert r.returncode == 2
    assert "committing on protected branch 'main'" in r.stderr


def test_bad_commit_message_blocked(repo):
    repo.on("feat/thing")
    r = run_hook(bash(repo, 'git add . && git commit -m "added stuff"'))
    assert r.returncode == 2
    assert "Conventional Commit" in r.stderr


def test_long_subject_blocked(repo):
    repo.on("feat/thing")
    r = run_hook(bash(repo, 'git commit -m "feat: ' + "x" * 80 + '"'))
    assert r.returncode == 2
    assert "max 72" in r.stderr


@pytest.mark.parametrize("message", [
    "feat: add thing",
    "fix(api): handle empty body",
    "feat(auth)!: drop legacy tokens",
    "revert: undo the thing",
])
def test_good_commit_message_allowed(repo, message):
    repo.on("feat/thing")
    r = run_hook(bash(repo, "git commit -am '{}'".format(message)))
    assert r.returncode == 0 and r.stdout == ""


def test_heredoc_message_parsed(repo):
    repo.on("feat/thing")
    good = 'git commit -m "$(cat <<\'EOF\'\nfeat: add parser\n\nBody text.\nEOF\n)"'
    bad = 'git commit -m "$(cat <<\'EOF\'\nAdd parser\n\nBody text.\nEOF\n)"'
    assert run_hook(bash(repo, good)).returncode == 0
    assert run_hook(bash(repo, bad)).returncode == 2


def test_heredoc_file_message_parsed(repo):
    repo.on("feat/thing")
    cmd = "git commit -F - <<'EOF'\nbad message\nEOF"
    assert run_hook(bash(repo, cmd)).returncode == 2


def test_unparseable_message_allowed(repo):
    repo.on("feat/thing")
    assert run_hook(bash(repo, 'git commit -m "$MSG"')).returncode == 0


def test_no_verify_asks(repo):
    repo.on("feat/thing")
    r = run_hook(bash(repo, 'git commit --no-verify -m "fix: thing"'))
    assert r.returncode == 0
    kind, reason = decision(r)
    assert kind == "ask" and "--no-verify" in reason


def test_cd_and_dash_c_followed(repo, tmp_path):
    payload = {"tool_name": "Bash", "cwd": str(tmp_path),
               "tool_input": {"command": 'cd repo && git commit -m "feat: x"'}}
    assert run_hook(payload).returncode == 2
    payload["tool_input"]["command"] = 'git -C repo commit -m "feat: x"'
    assert run_hook(payload).returncode == 2


# ---------------------------------------------------------------- pushes

@pytest.mark.parametrize("command", [
    "git push origin main",
    "git push origin feat/thing:main",
    "git push origin HEAD:refs/heads/master",
    "git push origin --delete release/2.0",
])
def test_push_to_protected_explicit_blocked(repo, command):
    repo.on("feat/thing")
    r = run_hook(bash(repo, command))
    assert r.returncode == 2
    assert "protected branch" in r.stderr


def test_push_head_on_main_blocked(repo):
    assert run_hook(bash(repo, "git push origin HEAD")).returncode == 2


def test_push_implicit_on_main_blocked(repo):
    assert run_hook(bash(repo, "git push")).returncode == 2


def test_push_on_feature_asks(repo):
    repo.on("feat/thing")
    (repo.root / "CONTRIBUTING.md").write_text("x")
    (repo.root / ".github").mkdir()
    (repo.root / ".github" / "pull_request_template.md").write_text("x")
    (repo.root / ".commitlintrc.json").write_text("{}")
    r = run_hook(bash(repo, "git push -u origin feat/thing"))
    assert r.returncode == 0
    kind, reason = decision(r)
    assert kind == "ask"
    assert "CONTRIBUTING.md" in reason
    assert ".github/pull_request_template.md" in reason
    assert "commitlint" in reason
    assert "CODEOWNERS" not in reason


def test_force_push_warns(repo):
    repo.on("feat/thing")
    for cmd in ("git push --force origin feat/thing", "git push origin +feat/thing", "git push --all"):
        kind, reason = decision(run_hook(bash(repo, cmd)))
        assert kind == "ask" and "WARNING" in reason, cmd


# ---------------------------------------------------------------- config

def test_custom_config_respected(repo):
    repo.config(json.dumps({"protected": ["prod"], "protectedPrefixes": ["rel-"]}))
    assert run_hook(edit(repo.root / "a.py")).returncode == 0  # main no longer protected
    repo.on("prod")
    assert run_hook(edit(repo.root / "a.py")).returncode == 2
    repo.on("rel-9")
    assert run_hook(edit(repo.root / "a.py")).returncode == 2


@pytest.mark.parametrize("text", ["{not json", "[]", '{"protected": "main"}', '{"protected": [1]}'])
def test_invalid_config_falls_back(repo, text):
    repo.config(text)
    assert run_hook(edit(repo.root / "a.py")).returncode == 2


# ---------------------------------------------------------------- pass-through

def test_non_git_command_allowed(repo):
    r = run_hook(bash(repo, "ls -la && echo main"))
    assert r.returncode == 0 and r.stdout == ""


def test_non_git_directory_allowed(tmp_path):
    payload = {"tool_name": "Bash", "cwd": str(tmp_path),
               "tool_input": {"command": 'git commit -m "bad"'}}
    assert run_hook(payload).returncode == 0
    assert run_hook(edit(tmp_path / "file.txt")).returncode == 0


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"tool_name": "Edit", "tool_input": null}'])
def test_malformed_stdin_fails_open(stdin):
    r = run_hook(stdin)
    assert r.returncode == 0 and r.stdout == ""
