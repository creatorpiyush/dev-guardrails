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


# -- Task 3: logs ---------------------------------------------------------------

LOGWIN = ("--since", "2026-11-01T09:00Z", "--until", "2026-11-01T12:00Z")
EPOCH_10_00_05 = int(datetime(2026, 11, 1, 10, 0, 5, tzinfo=timezone.utc).timestamp())
# Built at runtime from a split prefix so this file never holds a token-shaped literal.
GH_TOKEN = "gh" + "p_" + "".join("abcdefghijkmnopqrstuvwyz0123456789"[(i * 7) % 34] for i in range(36))


def write_log(tmp_path, *lines, name="app.log"):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def log_entries(out):
    return [e for e in out["timeline"] if e["source"] == "log"]


@pytest.mark.parametrize("line", [
    "2026-11-01T10:00:05Z ERROR db timeout",
    "2026-11-01T15:30:05+05:30 ERROR db timeout",
    "Nov  1 10:00:05 web1 app[12]: ERROR db timeout",
    "%d ERROR db timeout" % EPOCH_10_00_05,
    '{"ts": %d123, "level": "error", "msg": "db timeout"}' % EPOCH_10_00_05,
])
def test_each_timestamp_format(repo, tmp_path, line):
    out = run(repo, *LOGWIN, "--logs", write_log(tmp_path, line))
    entries = log_entries(out)
    assert len(entries) == 1 and entries[0]["ts"] == "2026-11-01T10:00:05Z"
    assert "db timeout" in entries[0]["signature"]


def test_mixed_zones_normalise_to_utc(repo, tmp_path):
    log = write_log(tmp_path, "2026-11-01T10:00:05Z ERROR a", "2026-11-01T15:30:20+05:30 ERROR a")
    out = run(repo, *LOGWIN, "--logs", log)
    assert out["logs"]["buckets"] == [{"minute": "2026-11-01T10:00:00Z", "errors": 2}]


def test_naive_log_time_is_approximate(repo, tmp_path):
    write_config(repo, {"timezone": "+01:00"})
    out = run(repo, *LOGWIN, "--logs", write_log(tmp_path, "2026-11-01 11:00:05 ERROR x"))
    entry = log_entries(out)[0]
    assert entry["ts"] == "2026-11-01T10:00:05Z" and entry["confidence"] == "approximate"
    assert out["logs"]["files"][0]["assumed_timezone"] == "+01:00"


def test_zoned_log_time_is_exact(repo, tmp_path):
    out = run(repo, *LOGWIN, "--logs", write_log(tmp_path, "2026-11-01T10:00:05Z ERROR x"))
    assert log_entries(out)[0]["confidence"] == "exact"
    assert out["logs"]["files"][0]["assumed_timezone"] is None


def test_error_signature_bucketing(repo, tmp_path):
    log = write_log(tmp_path, "2026-11-01T10:00:05Z ERROR timeout after 30s for user 4411",
                    "2026-11-01T10:01:05Z ERROR timeout after 45s for user 9",
                    "2026-11-01T10:01:09Z INFO all good")
    out = run(repo, *LOGWIN, "--logs", log)
    entries = log_entries(out)
    assert len(entries) == 1
    assert entries[0]["signature"] == "timeout after <n>s for user <n>" and entries[0]["count"] == 2
    assert entries[0]["ts"] == "2026-11-01T10:00:05Z" and entries[0]["line"] == 1
    assert out["logs"]["buckets"] == [{"minute": "2026-11-01T10:00:00Z", "errors": 1},
                                      {"minute": "2026-11-01T10:01:00Z", "errors": 1}]
    f = out["logs"]["files"][0]
    assert (f["lines"], f["parsed"], f["errors"]) == (3, 3, 2)
    assert (f["first"], f["last"]) == ("2026-11-01T10:00:05Z", "2026-11-01T10:01:09Z")


def test_json_log_signature_uses_message(repo, tmp_path):
    log = write_log(tmp_path, '{"time": "2026-11-01T10:00:05Z", "level": "error", "msg": "db down"}',
                    '{"time": "2026-11-01T10:00:06Z", "level": "error", "msg": "cache down"}')
    sigs = sorted(e["signature"] for e in log_entries(run(repo, *LOGWIN, "--logs", log)))
    assert sigs == ["cache down", "db down"]


def test_unparseable_timestamps_reported(repo, tmp_path):
    out = run(repo, *LOGWIN, "--logs", write_log(tmp_path, "11/01/2026 10:00 ERROR x"))
    assert out["logs"]["unparsed"]["count"] == 1
    assert out["logs"]["unparsed"]["samples"] == ["11/01/2026 10:00 ERROR x"]


def test_stack_trace_continuation_lines(repo, tmp_path):
    log = write_log(tmp_path, "2026-11-01T10:00:05Z ERROR request failed",
                    "Traceback (most recent call last):", '  File "app.py", line 3, in <module>',
                    "    boom()", "ValueError: bad")
    out = run(repo, *LOGWIN, "--logs", log)
    assert out["logs"]["files"][0]["errors"] == 1 and out["logs"]["unparsed"]["count"] == 0


def test_traceback_after_info_line_is_one_error(repo, tmp_path):
    log = write_log(tmp_path, "2026-11-01T10:00:05Z INFO handling", "Traceback (most recent call last):",
                    '  File "app.py", line 3, in <module>')
    out = run(repo, *LOGWIN, "--logs", log)
    assert out["logs"]["files"][0]["errors"] == 1
    assert log_entries(out)[0]["confidence"] == "approximate"


def test_caps_on_large_logs(repo, tmp_path):
    letters = "abcdefghijklmnopqrstuvwxyz"
    words = ("".join(letters[(i // 26 ** k) % 26] for k in range(4)) for i in range(200000))
    lines = ("2026-11-01T10:%02d:00Z ERROR fault %s" % (i % 60, w) for i, w in enumerate(words))
    log = write_log(tmp_path, *lines)
    out = run(repo, *LOGWIN, "--logs", log)
    assert len(log_entries(out)) <= 50
    assert out["logs"]["files"][0]["errors"] == 200000
    assert len(json.dumps(out)) < 200000


def test_byte_cap_truncates(tmp_path):
    m = load()
    m.MAX_LOG_BYTES = 100
    log = write_log(tmp_path, *["2026-11-01T10:00:05Z ERROR x"] * 50)
    since = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
    got = m.read_log(log, timezone.utc, (since, since + timedelta(hours=3)), DEFAULTS, {})
    assert got["truncated"] is True and got["lines"] < 50


def test_binary_log_skipped(repo, tmp_path):
    p = tmp_path / "bin.log"
    p.write_bytes(b"\x00\x01ERROR\n" * 10)
    out = run(repo, *LOGWIN, "--logs", str(p))
    assert out["logs"]["files"][0]["skipped"] == "binary"


def test_missing_log_skipped_with_note(repo, tmp_path):
    out = run(repo, *LOGWIN, "--logs", str(tmp_path / "nope.log"))
    assert out["ok"] is True and out["logs"]["files"][0]["skipped"] == "not found"


def test_secret_file_never_read(repo, tmp_path):
    out = run(repo, *LOGWIN, "--logs", write_log(tmp_path, "2026-11-01T10:00:05Z ERROR x", name=".env"))
    assert out["logs"]["files"][0]["skipped"] == "secret file"


def test_signature_redacts_secrets_and_pii(repo, tmp_path):
    log = write_log(tmp_path, "2026-11-01T10:00:05Z ERROR auth failed token=%s for bob@example.com "
                    "from 10.1.2.3 req 7c9e6679-7425-40de-944b-e07fc1f90ae7" % GH_TOKEN)
    out = run(repo, *LOGWIN, "--logs", log)
    text = json.dumps(out)
    for leaked in (GH_TOKEN, GH_TOKEN[4:20], "bob@example.com", "10.1.2.3", "7c9e6679"):
        assert leaked not in text
    assert out["redactions"]["github-token"] == 1
    assert log_entries(out)[0]["signature"] == (
        "auth failed token=<redacted:github-token> for <email> from <ip> req <uuid>")


def test_unparsed_sample_redacted(repo, tmp_path):
    out = run(repo, *LOGWIN, "--logs", write_log(tmp_path, "11/01/2026 %s" % GH_TOKEN))
    assert GH_TOKEN[4:20] not in json.dumps(out)  # not even a cut-off piece


def test_redact_hostnames_flag(repo, tmp_path):
    log = write_log(tmp_path, "2026-11-01T10:00:05Z ERROR cannot reach db.prod.internal")
    assert "db.prod.internal" in log_entries(run(repo, *LOGWIN, "--logs", log))[0]["signature"]
    write_config(repo, {"redactHostnames": True})
    sig = log_entries(run(repo, *LOGWIN, "--logs", log))[0]["signature"]
    assert sig == "cannot reach <host>"


def test_window_defaults_to_log_span(repo, tmp_path):  # 24 hours back, 30 minutes forward
    log = write_log(tmp_path, "2026-11-01T10:00:05Z INFO start", "2026-11-01T10:20:00Z ERROR x")
    w = run(repo, "--logs", log)["window"]
    assert w == {"since": "2026-10-31T10:00:05Z", "until": "2026-11-01T10:50:00Z", "source": "logs"}


def test_logs_filtered_to_window(repo, tmp_path):
    log = write_log(tmp_path, "2026-11-01T08:00:00Z ERROR early", "2026-11-01T10:00:00Z ERROR inside")
    out = run(repo, *LOGWIN, "--logs", log)
    assert [e["signature"] for e in log_entries(out)] == ["inside"]
    assert out["logs"]["files"][0]["first"] == "2026-11-01T08:00:00Z"


def test_several_log_files(repo, tmp_path):
    a = write_log(tmp_path, "2026-11-01T10:00:05Z ERROR a", name="a.log")
    b = write_log(tmp_path, "2026-11-01T10:00:09Z ERROR b", name="b.log")
    out = run(repo, *LOGWIN, "--logs", a, b)
    assert len(out["logs"]["files"]) == 2
    assert out["logs"]["buckets"] == [{"minute": "2026-11-01T10:00:00Z", "errors": 2}]


def test_commit_subject_redacted(repo):
    commit(repo, "fix: drop leaked %s" % GH_TOKEN, "2026-11-01T01:00:00Z")
    out = run(repo, *WINDOW)
    assert GH_TOKEN not in json.dumps(out) and out["redactions"] == {"github-token": 1}


# -- Task 4: merged PRs and templates ------------------------------------------

def fake_gh(bin_dir, auth_code=0, prs=None, raw=None):
    """A gh stand-in: `auth status` exits auth_code; `pr list` prints prs as JSON (or raw)."""
    bin_dir.mkdir(exist_ok=True)
    body = raw if raw is not None else json.dumps(prs or [])
    script = bin_dir / "fake_gh.py"
    script.write_text(
        "import sys\n"
        "if sys.argv[1:3] == ['auth', 'status']: sys.exit(%d)\n"
        "if sys.argv[1:3] == ['pr', 'list']: print(%r); sys.exit(0)\n"
        "sys.exit(2)\n" % (auth_code, body))
    if os.name == "nt":
        (bin_dir / "gh.bat").write_text('@"%s" "%s" %%*\n' % (sys.executable, script))
    else:
        p = bin_dir / "gh"
        p.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script))
        p.chmod(0o755)
    return {"INCIDENT_NOTES_CLI_PATH": str(bin_dir)}


PRS = [
    {"number": 7, "title": "fix: pool size", "mergedAt": "2026-11-01T03:00:00Z", "url": "https://x/7"},
    {"number": 8, "title": "feat: later", "mergedAt": "2026-11-01T09:00:00Z", "url": "https://x/8"},
]


def test_gh_missing_handled(repo):
    prs = run(repo, *WINDOW)["prs"]
    assert prs["status"] == "skipped" and "not found" in prs["note"] and prs["count"] == 0


def test_gh_unauthenticated_skipped(repo, tmp_path):
    out = run(repo, *WINDOW, env=fake_gh(tmp_path / "bin", auth_code=1, prs=PRS))
    assert out["prs"]["status"] == "skipped" and "not signed in" in out["prs"]["note"]
    assert not [e for e in out["timeline"] if e["source"] == "pr"]


def test_gh_prs_in_window(repo, tmp_path):
    out = run(repo, *WINDOW, env=fake_gh(tmp_path / "bin", prs=PRS))
    prs = [e for e in out["timeline"] if e["source"] == "pr"]
    assert prs == [{"ts": "2026-11-01T03:00:00Z", "source": "pr", "confidence": "exact",
                    "number": 7, "title": "fix: pool size", "url": "https://x/7"}]
    assert out["prs"] == {"status": "ok", "note": None, "count": 1}


def test_gh_pr_title_redacted(repo, tmp_path):
    prs = [dict(PRS[0], title="fix: rotate %s" % GH_TOKEN)]
    out = run(repo, *WINDOW, env=fake_gh(tmp_path / "bin", prs=prs))
    assert GH_TOKEN not in json.dumps(out)


def test_gh_failure_is_valid_json(repo, tmp_path):
    out = run(repo, *WINDOW, env=fake_gh(tmp_path / "bin", raw="not json {"))
    assert out["ok"] is True and out["prs"]["status"] == "skipped"
    assert "failed" in out["prs"]["note"]


def test_gh_inside_repo_ignored(repo):
    fake_gh(repo / "bin", prs=PRS)
    out = run(repo, *WINDOW, env={"INCIDENT_NOTES_CLI_PATH": str(repo / "bin")})
    assert out["prs"]["status"] == "skipped" and "not found" in out["prs"]["note"]


def test_template_detection_dir(repo):
    (repo / "docs" / "incidents").mkdir(parents=True)
    (repo / "docs" / "incidents" / "TEMPLATE.md").write_text("# Title\n\n## Impact\ntext\n## Timeline\n")
    out = run(repo, "Pool exhausted", *WINDOW)
    assert out["template"] == {"directory": "docs/incidents",
                               "template": "docs/incidents/TEMPLATE.md",
                               "headings": ["# Title", "## Impact", "## Timeline"]}
    assert out["output_path"] == "docs/incidents/2026-11-01-pool-exhausted.md"


def test_template_detection_issue_template(repo):
    (repo / ".github" / "ISSUE_TEMPLATE").mkdir(parents=True)
    (repo / ".github" / "ISSUE_TEMPLATE" / "postmortem.md").write_text("---\nname: pm\n---\n## Summary\n")
    t = run(repo, *WINDOW)["template"]
    assert t["template"] == ".github/ISSUE_TEMPLATE/postmortem.md" and t["headings"] == ["## Summary"]


def test_configured_directory_wins(repo):
    write_config(repo, {"directory": "ops/pm"})
    (repo / "ops" / "pm").mkdir(parents=True)
    (repo / "docs" / "postmortems").mkdir(parents=True)
    out = run(repo, "x", *WINDOW)
    assert out["template"]["directory"] == "ops/pm" and out["output_path"].startswith("ops/pm/")


def test_no_template(repo):
    out = run(repo, "Pool exhausted", *WINDOW)
    assert out["template"] == {"directory": None, "template": None, "headings": []}
    assert out["output_path"] == "docs/postmortems/2026-11-01-pool-exhausted.md"


def test_headings_skip_code_blocks_and_cap(repo):
    (repo / "docs" / "postmortems").mkdir(parents=True)
    text = "```\n# not a heading\n```\n" + "".join("## H%d\n" % i for i in range(60))
    (repo / "docs" / "postmortems" / "template.md").write_text(text)
    headings = run(repo, *WINDOW)["template"]["headings"]
    assert "# not a heading" not in headings and len(headings) == 40


# -- final review fixes ---------------------------------------------------------

def test_old_window_found_behind_newer_commits(repo):
    commit(repo, "fix: the trigger", "2026-11-01T01:00:00Z")
    for i in range(3):
        commit(repo, "later %d" % i, "2026-11-20T0%d:00:00Z" % (i + 1))  # past the landing slack
    m = load()
    m.LOG_SCAN_LIMIT = "2"  # fewer than the newer commits, like a busy repo days later
    since = datetime(2026, 11, 1, tzinfo=timezone.utc)
    entries, _, _ = m.commit_events(str(repo), since, since + timedelta(hours=6), False, {})
    assert [e["subject"] for e in entries] == ["fix: the trigger"]


def test_unparsed_samples_mask_emails_and_ips(repo, tmp_path):
    out = run(repo, *LOGWIN, "--logs", write_log(tmp_path, "2026/11/01 10:00 login jane@x.io from 10.9.8.7"))
    text = json.dumps(out["logs"]["unparsed"])
    assert out["logs"]["unparsed"]["count"] == 1 and "jane@" not in text and "10.9.8" not in text


def test_ip_leading_line_is_not_an_unparsed_timestamp(repo, tmp_path):
    log = write_log(tmp_path, '10.0.0.1 - - [01/Nov/2026:10:00:00 +0000] "GET / HTTP/1.1" 200')
    assert run(repo, *LOGWIN, "--logs", log)["logs"]["unparsed"]["count"] == 0


def test_generic_credentials_masked_in_signature(repo, tmp_path):
    log = write_log(tmp_path, "2026-11-01T10:00:05Z ERROR auth failed Authorization: Bearer "
                    "abcdefGHIJKLmnopQRST password=hunter2 api_key: 'k-77Zq'")
    sig = log_entries(run(repo, *LOGWIN, "--logs", log))[0]["signature"]
    for leaked in ("abcdefGHIJ", "hunter", "77Zq"):
        assert leaked not in sig
    assert sig.startswith("auth failed Authorization:")


def test_plain_words_after_secret_keywords_survive(repo, tmp_path):
    log = write_log(tmp_path, "2026-11-01T10:00:05Z ERROR password incorrect for token refresh")
    sig = log_entries(run(repo, *LOGWIN, "--logs", log))[0]["signature"]
    assert sig == "password incorrect for token refresh"


def test_traceback_signature_uses_exception_line(repo, tmp_path):
    log = write_log(tmp_path,
                    "2026-11-01T10:00:05Z INFO handling a", "Traceback (most recent call last):",
                    '  File "app.py", line 3, in <module>', "KeyError: 'x'",
                    "2026-11-01T10:00:09Z INFO handling b", "Traceback (most recent call last):",
                    '  File "app.py", line 9, in <module>', "ConnectionError: refused")
    sigs = sorted(e["signature"] for e in log_entries(run(repo, *LOGWIN, "--logs", log)))
    assert sigs == ["ConnectionError: refused", "KeyError: <str>"]


def test_word_starting_with_z_is_not_a_zone(repo, tmp_path):
    write_config(repo, {"timezone": "+01:00"})
    log = write_log(tmp_path, "2026-11-01 11:00:05 Zookeeper ERROR session expired")
    entry = log_entries(run(repo, *LOGWIN, "--logs", log))[0]
    assert entry["ts"] == "2026-11-01T10:00:05Z" and entry["confidence"] == "approximate"


def test_log_window_looks_back_for_the_trigger(repo, tmp_path):
    commit(repo, "feat: shrink pool", "2026-11-01T07:00:00Z")  # 3 hours before the first log line
    log = write_log(tmp_path, "2026-11-01T10:00:05Z ERROR pool exhausted")
    out = run(repo, "Pool exhausted", "--logs", log)
    assert [e["subject"] for e in commits_of(out)] == ["feat: shrink pool"]
    assert out["output_path"] == "docs/postmortems/2026-11-01-pool-exhausted.md"
