"""Tests for pr-writer/scripts/collect_context.py.

Each test builds a throwaway git repo, runs the script, and checks the JSON.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "pr-writer" / "scripts" / "collect_context.py"

ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="T",
    GIT_AUTHOR_EMAIL="t@example.com",
    GIT_COMMITTER_NAME="T",
    GIT_COMMITTER_EMAIL="t@example.com",
)

DEFAULTS = {
    "draftByDefault": True,
    "titleFormat": "conventional",
    "maxTitleLength": 72,
    "language": "en",
    "labels": {},
    "extraChecklist": [],
    "diffSizeCap": 60000,
}


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=ENV)


def run_script(cwd, *args):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd, capture_output=True, text=True, timeout=30, env=ENV,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    try:
        git(path, "init", "-q", "-b", "main")
    except subprocess.CalledProcessError:
        git(path, "init", "-q")
        git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    git(path, "config", "core.autocrlf", "false")
    git(path, "config", "commit.gpgsign", "false")

    class Repo:
        root = path

        def commit(self, msg, files=None):
            for name, content in (files or {"f.txt": msg}).items():
                p = path / name
                p.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    p.write_bytes(content)
                else:
                    p.write_text(content, encoding="utf-8")
                git(path, "add", name)
            git(path, "commit", "-q", "-m", msg)

        def on(self, branch):
            git(path, "switch", "-q", "-c", branch)

        def remote(self):
            bare = path.parent / "origin.git"
            git(path.parent, "init", "-q", "--bare", str(bare))
            git(path, "remote", "add", "origin", str(bare))
            git(path, "push", "-q", "origin", "main")
            git(path, "remote", "set-head", "origin", "main")

    r = Repo()
    r.commit("chore: init")
    return r


def test_non_git_directory_errors(tmp_path):
    out = run_script(tmp_path)
    assert out["ok"] is False
    assert out["error"]["code"] == "not-a-repo"


@pytest.mark.parametrize("branch", ["main", "release/1.0"])
def test_protected_branch_errors(repo, branch):
    if branch != "main":
        repo.on(branch)
    assert run_script(repo.root)["error"]["code"] == "protected-branch"


def test_git_guard_config_protects_custom_branch(repo):
    (repo.root / ".claude").mkdir()
    (repo.root / ".claude" / "git-guard.json").write_text('{"protected": ["trunk2"]}')
    repo.on("trunk2")
    assert run_script(repo.root)["error"]["code"] == "protected-branch"


def test_detached_head_errors(repo):
    git(repo.root, "checkout", "-q", "--detach")
    assert run_script(repo.root)["error"]["code"] == "detached-head"


def test_bad_arguments_error(repo):
    out = run_script(repo.root, "--ready", "--draft")
    assert out["error"]["code"] == "bad-arguments"


def write_config(repo, text):
    (repo.root / ".claude").mkdir(exist_ok=True)
    (repo.root / ".claude" / "pr-writer.json").write_text(text)


@pytest.mark.parametrize("text", ["{", "[]", '{"maxTitleLength": "x"}'])
def test_invalid_config_falls_back(repo, text):
    write_config(repo, text)
    repo.on("feat/x")
    out = run_script(repo.root)
    assert out["config"] == DEFAULTS
    assert out["warnings"]


def test_unknown_config_key_warns(repo):
    write_config(repo, '{"bogus": 1, "language": "fr"}')
    repo.on("feat/x")
    out = run_script(repo.root)
    assert out["config"]["language"] == "fr"
    assert any("bogus" in w for w in out["warnings"])


def test_valid_branch_has_no_error(repo):
    repo.on("feat/x")
    out = run_script(repo.root)
    assert out["ok"] is True
    assert out["branch"] == "feat/x"
    assert out["config"] == DEFAULTS
