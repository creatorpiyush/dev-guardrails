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
    feature(repo, "feat: a")
    out = run_script(repo.root)
    assert out["ok"] is True
    assert out["branch"] == "feat/x"
    assert out["config"] == DEFAULTS


def feature(repo, *msgs):
    repo.on("feat/x")
    for m in msgs:
        repo.commit(m)


def test_base_from_argument(repo):
    feature(repo, "feat: a")
    out = run_script(repo.root, "--base", "main")
    assert out["base"] == {"name": "main", "ref": "main", "source": "argument"}


def test_base_from_origin_head(repo):
    repo.remote()
    feature(repo, "feat: a")
    out = run_script(repo.root)
    assert out["base"] == {"name": "main", "ref": "origin/main", "source": "origin-head"}


def test_base_fallback_without_remote(repo):
    feature(repo, "feat: a")
    out = run_script(repo.root)
    assert out["base"]["source"] == "fallback"
    assert out["base"]["ref"] == "main"


def test_missing_base_errors(repo):
    feature(repo, "feat: a")
    assert run_script(repo.root, "--base", "nope")["error"]["code"] == "base-not-found"


def test_no_commits_ahead_errors(repo):
    repo.on("feat/x")
    assert run_script(repo.root)["error"]["code"] == "no-commits-ahead"


def test_merge_base_and_ahead_behind(repo):
    feature(repo, "feat: a", "feat: b")
    git(repo.root, "switch", "-q", "main")
    repo.commit("chore: m")
    git(repo.root, "switch", "-q", "feat/x")
    out = run_script(repo.root)
    assert (out["ahead"], out["behind"]) == (2, 1)
    expected = subprocess.run(["git", "rev-parse", "main~1"], cwd=repo.root,
                              capture_output=True, text=True).stdout.strip()
    assert out["merge_base"] == expected


def test_pushed_vs_unpushed(repo):
    repo.remote()
    feature(repo, "feat: a")
    assert run_script(repo.root)["pushed"] is False
    git(repo.root, "push", "-q", "-u", "origin", "HEAD")
    assert run_script(repo.root)["pushed"] is True
    repo.commit("feat: b")
    assert run_script(repo.root)["pushed"] is False


def test_merge_commits_excluded(repo):
    feature(repo, "feat: a")
    git(repo.root, "switch", "-q", "main")
    repo.commit("chore: m", {"m.txt": "m"})
    git(repo.root, "switch", "-q", "feat/x")
    git(repo.root, "merge", "-q", "--no-ff", "-m", "Merge main", "main")
    out = run_script(repo.root)
    assert [c["subject"] for c in out["commits"]] == ["feat: a"]


def test_commit_cap(repo):
    repo.on("feat/x")
    for i in range(101):
        git(repo.root, "commit", "--allow-empty", "-q", "-m", "chore: %d" % i)
    out = run_script(repo.root)
    assert len(out["commits"]) == 100
    assert out["commits_truncated"] is True


@pytest.mark.parametrize("msg,note", [
    ("feat!: x", None),
    ("feat(api): x\n\nBREAKING CHANGE: y", "y"),
])
def test_breaking_change_detected(repo, msg, note):
    feature(repo, msg)
    out = run_script(repo.root)
    assert out["commits"][0]["breaking"] is True
    assert out["commits"][0]["type"] == "feat"
    assert [b["note"] for b in out["breaking_changes"]] == [note]


def test_non_conventional_commit(repo):
    feature(repo, "Fix the thing")
    c = run_script(repo.root)["commits"][0]
    assert (c["type"], c["scope"], c["breaking"]) == (None, None, False)
    assert out_empty(run_script(repo.root))


def out_empty(out):
    return out["breaking_changes"] == [] and out["commits_truncated"] is False


def test_dirty_warns(repo):
    feature(repo, "feat: a")
    assert run_script(repo.root)["dirty"] is False
    (repo.root / "new.txt").write_text("x")
    out = run_script(repo.root)
    assert out["dirty"] is True
    assert "uncommitted changes are not included" in out["warnings"]
