"""Tests for session-recap/scripts/collect_recap.py.

Each test builds a throwaway repo and runs the script as a subprocess.
"""
import importlib.util
import json
import subprocess
import sys
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
