"""Tests for protected-files-guard/hooks/protected_files_guard.py.

Each test builds a throwaway git repo, pipes a PreToolUse payload into the
script, and checks the exit code and output. Repos are unborn (no commits),
so no git identity is needed.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[1] / "protected-files-guard" / "hooks"
          / "protected_files_guard.py")


def run_hook(payload, **env):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    full_env = {k: v for k, v in os.environ.items() if k != "PROTECTED_FILES_GUARD_DISABLE"}
    full_env.update(env)
    return subprocess.run([sys.executable, str(SCRIPT)], input=data, capture_output=True,
                          text=True, timeout=30, env=full_env)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    return path


def config(repo, data):
    (repo / ".claude").mkdir(exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (repo / ".claude" / "protected-files.json").write_text(text)


def edit(path):
    return {"tool_name": "Edit", "cwd": str(Path(path).parent),
            "tool_input": {"file_path": str(path), "old_string": "a", "new_string": "b"}}


def bash(repo, command, tool="Bash"):
    return {"tool_name": tool, "cwd": str(repo), "tool_input": {"command": command}}


def decision(r):
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


def allowed(r):
    return r.returncode == 0 and r.stdout == ""


# ---------------------------------------------------------------- default rules

def test_env_denied(repo):
    r = run_hook(edit(repo / ".env"))
    assert r.returncode == 2
    assert "'.env' is blocked" in r.stderr and "secrets" in r.stderr


def test_nested_env_variant_denied(repo):
    r = run_hook(edit(repo / "api" / ".env.production"))
    assert r.returncode == 2


@pytest.mark.parametrize("name", [".env.example", ".env.sample", ".env.template"])
def test_env_example_allowed(repo, name):
    assert allowed(run_hook(edit(repo / name)))


def test_lockfile_asks_with_package_manager_hint(repo):
    kind, reason = decision(run_hook(edit(repo / "web" / "package-lock.json")))
    assert kind == "ask"
    assert "npm install" in reason


def test_nested_migration_asks(repo):
    kind, reason = decision(run_hook(edit(repo / "app" / "users" / "migrations" / "0002_x.py")))
    assert kind == "ask" and "migration" in reason


def test_ci_file_asks(repo):
    kind, _ = decision(run_hook(edit(repo / ".github" / "workflows" / "ci.yml")))
    assert kind == "ask"


def test_ci_rule_is_anchored(repo):
    assert allowed(run_hook(edit(repo / "docs" / ".github" / "workflows" / "ci.yml")))


def test_ordinary_file_allowed(repo):
    assert allowed(run_hook(edit(repo / "src" / "app.py")))


def test_outside_repo_allowed(tmp_path):
    assert allowed(run_hook(edit(tmp_path / "loose" / ".env")))


def test_symlink_to_protected_file_caught(repo):
    (repo / ".env").write_text("X=1\n")
    link = repo / "settings.txt"
    try:
        link.symlink_to(repo / ".env")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    r = run_hook(edit(link))
    assert r.returncode == 2 and "'.env'" in r.stderr


def test_editing_guard_config_asks_even_when_allowed(repo):
    config(repo, {"allow": [".claude/**"]})
    kind, _ = decision(run_hook(edit(repo / ".claude" / "protected-files.json")))
    assert kind == "ask"


# ---------------------------------------------------------------- config

def test_allow_overrides_rule(repo):
    config(repo, {"allow": ["docs/migrations/**"]})
    assert allowed(run_hook(edit(repo / "docs" / "migrations" / "guide.md")))
    kind, _ = decision(run_hook(edit(repo / "app" / "migrations" / "0001.py")))
    assert kind == "ask"


def test_custom_rule(repo):
    config(repo, {"rules": [{"pattern": "infra/**", "action": "deny",
                             "reason": "Managed by platform team"}]})
    r = run_hook(edit(repo / "infra" / "prod" / "main.tf"))
    assert r.returncode == 2 and "Managed by platform team" in r.stderr
    assert run_hook(edit(repo / ".env")).returncode == 2  # defaults still apply


def test_replace_defaults(repo):
    config(repo, {"replaceDefaults": True,
                  "rules": [{"pattern": "*.lock", "action": "ask", "reason": "custom"}]})
    assert allowed(run_hook(edit(repo / ".env")))
    kind, reason = decision(run_hook(edit(repo / "yarn.lock")))
    assert kind == "ask" and "custom" in reason


@pytest.mark.parametrize("text", [
    "{not json",
    '["a list"]',
    '{"rules": [{"pattern": "x", "action": "allow"}]}',
    '{"allow": "docs/**"}',
])
def test_invalid_config_falls_back(repo, text):
    config(repo, text)
    r = run_hook(edit(repo / ".env"))
    assert r.returncode == 2
    assert "ignoring invalid" in r.stderr


def test_invalid_config_warning_reaches_user_on_allow(repo):
    config(repo, "{oops")
    r = run_hook(edit(repo / "src" / "ok.py"))
    assert r.returncode == 0
    assert "ignoring invalid" in json.loads(r.stdout)["systemMessage"]


@pytest.mark.parametrize("pattern,path,hit", [
    ("**/migrations/**", "migrations/0001.py", True),
    ("**/migrations/**", "a/b/migrations/c/d.py", True),
    ("**/migrations/**", "migrations.py", False),
    ("docs/**", "docs/a/b.md", True),
    ("docs/**", "src/docs/a.md", False),
    ("*.pem", "deep/dir/cert.pem", True),
    ("src/*.py", "src/a/b.py", False),
    ("a/**/b", "a/b", True),
    ("a/**/b", "a/x/y/b", True),
])
def test_glob_semantics(repo, pattern, path, hit):
    config(repo, {"replaceDefaults": True,
                  "rules": [{"pattern": pattern, "action": "deny", "reason": "r"}]})
    r = run_hook(edit(repo / path))
    assert (r.returncode == 2) is hit


# ---------------------------------------------------------------- shell commands

def test_sed_inplace_on_lockfile_caught(repo):
    kind, reason = decision(run_hook(bash(repo, "sed -i 's/1.0/1.1/' package-lock.json")))
    assert kind == "ask" and "sed -i" in reason


def test_bsd_sed_inplace_caught(repo):
    kind, _ = decision(run_hook(bash(repo, "sed -i '' -e 's/a/b/' yarn.lock")))
    assert kind == "ask"


def test_sed_without_inplace_allowed(repo):
    assert allowed(run_hook(bash(repo, "sed 's/a/b/' package-lock.json > out.json")))


@pytest.mark.parametrize("command", [
    "echo SECRET=1 > .env",
    "echo SECRET=1 >> config/.env.local",
    "printf x | tee .env",
    "cp .env.example .env",
    "mv tmp.key server.key",
    "rm -f .env",
    "truncate -s 0 .env",
    "cd config && echo x > .env",
    "printf x | sudo -u root tee .env",
    "env -u HOME tee .env",
    "ln -sf /tmp/other .env",
    "install -m 600 /tmp/other .env",
])
def test_shell_writes_to_secrets_denied(repo, command):
    (repo / "config").mkdir()
    r = run_hook(bash(repo, command))
    assert r.returncode == 2, command


@pytest.mark.parametrize("command", [
    "git checkout -- poetry.lock",
    "git restore Cargo.lock",
    "git -C sub restore --worktree --staged go.sum",
])
def test_git_restore_lockfile_asks(repo, command):
    (repo / "sub").mkdir()
    kind, _ = decision(run_hook(bash(repo, command)))
    assert kind == "ask"


def test_rm_directory_with_tracked_migration_asks(repo):
    mig = repo / "db" / "migrate"
    mig.mkdir(parents=True)
    (mig / "001_init.rb").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    kind, _ = decision(run_hook(bash(repo, "rm -rf db")))
    assert kind == "ask"


def test_powershell_set_content_denied(repo):
    r = run_hook(bash(repo, "Set-Content -Path .env -Value 'X=1'", tool="PowerShell"))
    assert r.returncode == 2


@pytest.mark.parametrize("command", [
    "cat .env",
    "cp package-lock.json backup.json",
    "cat package-lock.json > /dev/null 2>&1",
    "git restore --staged yarn.lock",
    "npm install left-pad",
    "ls -la && echo done",
])
def test_harmless_commands_allowed(repo, command):
    assert allowed(run_hook(bash(repo, command))), command


# ---------------------------------------------------------------- fail open / disable

@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"tool_name": "Edit"}'])
def test_malformed_stdin_fails_open(stdin):
    r = run_hook(stdin)
    assert r.returncode == 0


def test_unparseable_command_allowed(repo):
    assert allowed(run_hook(bash(repo, "echo 'unbalanced > .env")))


def test_disable_env_var(repo):
    r = run_hook(edit(repo / ".env"), PROTECTED_FILES_GUARD_DISABLE="1")
    assert allowed(r)
