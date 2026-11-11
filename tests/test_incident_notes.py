"""Tests for incident-notes/scripts/gather_timeline.py.

Each test builds a throwaway repo and runs the script as a subprocess.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "incident-notes" / "scripts" / "gather_timeline.py"

DEFAULTS = {
    "directory": "docs/postmortems", "timezone": "UTC", "includeAuthors": False,
    "redactHostnames": False, "severityScale": ["SEV1", "SEV2", "SEV3", "SEV4"],
}
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
_T = int(datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc).timestamp())
EPOCH_S, EPOCH_MS = str(_T), str(_T * 1000)

# No subprocess test may ever find a real gh.
_NO_CLI = tempfile.mkdtemp(prefix="incident-notes-nocli-")


def load():
    spec = importlib.util.spec_from_file_location("gather_timeline", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(cwd, *args, env=None):
    full = dict(os.environ, INCIDENT_NOTES_CLI_PATH=_NO_CLI)
    full.update(env or {})
    r = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, env=full,
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)  # exactly one JSON object, or this raises


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    return path


def write_config(root, data):
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "incident-notes.json").write_text(json.dumps(data))


WINDOW = ("--since", "2026-11-01T00:00Z", "--until", "2026-11-01T06:00Z")


# -- Task 1: contract, config, window -----------------------------------------

def test_not_a_repo_is_valid_json(tmp_path):
    out = run(tmp_path)
    assert out["ok"] is False and out["error"]["code"] == "not-a-repo"


def test_unknown_flag_is_bad_arguments(repo):
    assert run(repo, "--bogus")["error"]["code"] == "bad-arguments"


def test_bad_time_is_bad_arguments(repo):
    assert run(repo, "--since", "yesterday-ish")["error"]["code"] == "bad-arguments"


def test_until_before_since_is_bad_arguments(repo):
    out = run(repo, "--since", "2026-11-02T00:00Z", "--until", "2026-11-01T00:00Z")
    assert out["error"]["code"] == "bad-arguments"


@pytest.mark.parametrize("text", [
    "2026-11-01T10:00:00+05:30", "2026-11-01 04:30:00Z", "2026-11-01T04:30:00.250Z",
    "2026-11-01T10:00:00+0530", EPOCH_S, EPOCH_MS,
])
def test_parse_time_utc(text):
    m = load()
    assert m.iso(m.parse_time(text, timezone.utc, NOW)) == "2026-11-01T04:30:00Z"


def test_parse_time_relative():
    m = load()
    assert m.iso(m.parse_time("3 hours ago", timezone.utc, NOW)) == "2026-11-01T21:00:00Z"


def test_parse_time_rejects_garbage():
    assert load().parse_time("soon", timezone.utc, NOW) is None


def test_success_shape(repo):
    out = run(repo, *WINDOW)
    assert out["ok"] is True
    assert out["config"] == DEFAULTS and out["warnings"] == []
    assert out["window"] == {"since": "2026-11-01T00:00:00Z",
                             "until": "2026-11-01T06:00:00Z", "source": "args"}
    assert out["title"] is None and out["slug"] is None and out["output_path"] is None


def test_naive_time_uses_config_timezone(repo):
    write_config(repo, {"timezone": "+02:00"})
    out = run(repo, "--since", "2026-11-01 12:00", "--until", "2026-11-01 13:00")
    assert out["window"]["since"] == "2026-11-01T10:00:00Z"


def test_iana_timezone(repo):
    write_config(repo, {"timezone": "Asia/Kolkata"})
    out = run(repo, "--since", "2026-11-01 10:00", "--until", "2026-11-01 11:00")
    if out["warnings"]:  # no tzdata on this machine: falls back to UTC with a warning
        assert out["window"]["since"] == "2026-11-01T10:00:00Z"
    else:
        assert out["window"]["since"] == "2026-11-01T04:30:00Z"


def test_default_window_is_last_24h(repo):
    w = run(repo)["window"]
    since = datetime.strptime(w["since"], "%Y-%m-%dT%H:%M:%SZ")
    until = datetime.strptime(w["until"], "%Y-%m-%dT%H:%M:%SZ")
    assert until - since == timedelta(hours=24) and w["source"] == "default"


@pytest.mark.parametrize("data", [
    {"includeAuthors": "yes"}, {"redactHostnames": 1}, {"directory": ""},
    {"timezone": "Not a zone!"}, {"severityScale": []}, {"severityScale": ["SEV1", 2]},
    # The postmortem is written under this directory, so it must stay inside the repo.
    {"directory": "../outside"}, {"directory": "docs/../../x"}, {"directory": "/etc"},
    {"directory": "C:\\Users\\x"}, {"directory": "\\\\server\\share"},
])
def test_invalid_config_falls_back_with_warning(repo, data):
    write_config(repo, data)
    out = run(repo, *WINDOW)
    assert out["config"] == DEFAULTS and len(out["warnings"]) == 1


def test_unknown_config_key_warns(repo):
    write_config(repo, {"includeAuthors": True, "colour": "red"})
    out = run(repo, *WINDOW)
    assert out["config"]["includeAuthors"] is True
    assert any("colour" in w for w in out["warnings"])


def test_slug_and_output_path(repo):
    out = run(repo, "DB pool exhausted!", *WINDOW)
    assert out["title"] == "DB pool exhausted!"
    assert out["slug"] == "db-pool-exhausted"
    assert out["output_path"] == "docs/postmortems/2026-11-01-db-pool-exhausted.md"


def test_slug_capped():
    assert len(load().slugify("word " * 40)) <= 60


# -- Task 2: commits, tags, hotspots ------------------------------------------

def git(root, *args, date=None):
    env = dict(os.environ)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    r = subprocess.run(["git", "-c", "user.name=Ann", "-c", "user.email=ann@example.com",
                        *args], cwd=root, env=env, check=True, capture_output=True, text=True)
    return r.stdout.strip()


def commit(root, subject, date, files=("f.txt",), body=None):
    for name in files:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(p.read_text() + "x\n" if p.exists() else "x\n")
        git(root, "add", name)
    msg = ["-m", subject] + (["-m", body] if body else [])
    git(root, "commit", "-q", *msg, date=date)
    return git(root, "rev-parse", "HEAD")


def commits_of(out):
    return [e for e in out["timeline"] if e["source"] == "commit"]


def test_commit_window_selection(repo):
    commit(repo, "feat: early", "2026-11-01T09:00:00Z")
    commit(repo, "feat: inside", "2026-11-01T11:00:00Z")
    commit(repo, "feat: late", "2026-11-01T13:00:00Z")
    out = run(repo, "--since", "2026-11-01T10:00Z", "--until", "2026-11-01T12:00Z")
    entries = commits_of(out)
    assert [e["subject"] for e in entries] == ["feat: inside"]
    assert entries[0]["ts"] == "2026-11-01T11:00:00Z" and entries[0]["confidence"] == "exact"
    assert len(entries[0]["hash"]) == 12
    assert out["commits"] == {"count": 1, "truncated": False}


def test_revert_and_hotfix_detected(repo):
    commit(repo, 'Revert "feat: x"', "2026-11-01T01:00:00Z", body="This reverts commit abc.")
    commit(repo, "fix: hotfix for login", "2026-11-01T02:00:00Z")
    commit(repo, "fix: plain", "2026-11-01T03:00:00Z")
    kinds = {e["subject"]: e["kinds"] for e in commits_of(run(repo, *WINDOW))}
    assert kinds == {'Revert "feat: x"': ["revert"], "fix: hotfix for login": ["hotfix"],
                     "fix: plain": []}


def test_merge_detected(repo):
    commit(repo, "feat: base", "2026-11-01T01:00:00Z")
    git(repo, "switch", "-q", "-c", "side")
    commit(repo, "feat: side", "2026-11-01T02:00:00Z", files=("s.txt",))
    git(repo, "switch", "-q", "-")
    commit(repo, "feat: main", "2026-11-01T02:30:00Z", files=("m.txt",))
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch side", "side",
        date="2026-11-01T03:00:00Z")
    kinds = {e["subject"]: e["kinds"] for e in commits_of(run(repo, *WINDOW))}
    assert kinds["Merge branch side"] == ["merge"]


def test_files_touched_count(repo):
    commit(repo, "feat: three", "2026-11-01T01:00:00Z", files=("a", "b/c", "d"))
    assert commits_of(run(repo, *WINDOW))[0]["files"] == 3


def test_authors_excluded_by_default(repo):
    commit(repo, "feat: x", "2026-11-01T01:00:00Z")
    out = run(repo, *WINDOW)
    assert "author" not in commits_of(out)[0] and "Ann" not in json.dumps(out)


def test_authors_included_with_config(repo):
    write_config(repo, {"includeAuthors": True})
    commit(repo, "feat: x", "2026-11-01T01:00:00Z")
    assert commits_of(run(repo, *WINDOW))[0]["author"] == "Ann"


def test_tag_detection(repo):
    commit(repo, "feat: x", "2026-11-01T01:00:00Z")
    git(repo, "tag", "-a", "v1.2.0", "-m", "release", date="2026-11-01T02:00:00Z")
    git(repo, "tag", "deploy-prod-42")
    tags = {e["name"]: e for e in run(repo, *WINDOW)["timeline"] if e["source"] == "tag"}
    assert tags["v1.2.0"]["kind"] == "release" and tags["v1.2.0"]["confidence"] == "exact"
    assert tags["v1.2.0"]["ts"] == "2026-11-01T02:00:00Z"
    assert tags["deploy-prod-42"]["kind"] == "deploy"
    assert tags["deploy-prod-42"]["confidence"] == "approximate"


def test_tag_outside_window_excluded(repo):
    commit(repo, "feat: x", "2026-10-01T01:00:00Z")
    git(repo, "tag", "old-tag")
    assert not [e for e in run(repo, *WINDOW)["timeline"] if e["source"] == "tag"]


def test_hotspots_ranked(repo):
    commit(repo, "a", "2026-11-01T01:00:00Z", files=("hot.py", "warm.py"))
    commit(repo, "b", "2026-11-01T02:00:00Z", files=("hot.py",))
    out = run(repo, *WINDOW)
    assert out["hotspots"] == [{"path": "hot.py", "commits": 2}, {"path": "warm.py", "commits": 1}]


def test_timeline_sorted(repo):
    commit(repo, "b", "2026-11-01T02:00:00Z")
    commit(repo, "a", "2026-11-01T01:00:00Z")
    ts = [e["ts"] for e in run(repo, *WINDOW)["timeline"]]
    assert ts == sorted(ts)


def test_commit_cap_truncates(repo):
    for i in range(3):
        commit(repo, "c%d" % i, "2026-11-01T0%d:00:00Z" % (i + 1))
    m = load()
    since = datetime(2026, 11, 1, tzinfo=timezone.utc)
    entries, truncated, _ = m.commit_events(str(repo), since, since + timedelta(hours=6),
                                            False, {}, cap=2)
    assert len(entries) == 2 and truncated is True


def test_unborn_repo_has_no_commits(repo):
    out = run(repo, *WINDOW)
    assert out["ok"] is True and out["commits"] == {"count": 0, "truncated": False}
