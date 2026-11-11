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
