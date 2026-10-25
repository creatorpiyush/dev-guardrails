"""Tests for secret-scan-guard/hooks/secret_scan.py.

Fake secrets are assembled at runtime from split prefixes and seeded random
bodies, so this file never contains a literal token that this guard or GitHub
push protection would flag. Each test gets a fake HOME, so the real
~/.claude/secret-scan.json never applies.
"""
import json
import os
import random
import string
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "secret-scan-guard" / "hooks" / "secret_scan.py"

_rng = random.Random(1234)
ALNUM = "".join(c for c in string.ascii_letters + string.digits if c not in "xX")


def rand(n, alphabet=ALNUM):
    return "".join(_rng.choice(alphabet) for _ in range(n))


HIGH = {
    "aws-access-key": "AK" + "IA" + rand(16, string.ascii_uppercase.replace("X", "") + string.digits),
    "github-token": "gh" + "p_" + rand(36),
    "github-fine-grained-token": "github" + "_pat_" + rand(60),
    "gitlab-token": "gl" + "pat-" + rand(20),
    "slack-token": "xo" + "xb-" + rand(12, string.digits) + "-" + rand(24),
    "slack-webhook": "https://hooks." + "slack.com/services/T" + rand(8) + "/B" + rand(8) + "/" + rand(24),
    "anthropic-api-key": "sk-" + "ant-" + rand(40),
    "openai-api-key": "sk-" + "proj-" + rand(40),
    "stripe-live-key": "sk_" + "live_" + rand(24),
    "sendgrid-api-key": "S" + "G." + rand(22) + "." + rand(43),
    "npm-token": "np" + "m_" + rand(36),
    "twilio-api-key": "S" + "K" + rand(32, "0123456789abcdef"),
    "private-key": "-----BEGIN " + "RSA PRIVATE KEY-----",
}
AWS_KEY = HIGH["aws-access-key"]
GH_TOKEN = HIGH["github-token"]
PASSWORD = rand(24)

# (rule, line to write, the secret part that must never be echoed)
MEDIUM = [
    ("google-api-key", "key = " + "AI" + "za" + rand(35), None),
    ("url-credentials", "DSN = postgres://admin:" + PASSWORD[:14] + "@db.internal/app", PASSWORD[:14]),
    ("jwt", "t = ey" + "J" + rand(20) + ".ey" + "J" + rand(30) + "." + rand(30), None),
    ("generic-assignment", 'db_password = "' + PASSWORD + '"', PASSWORD),
    ("aws-secret-key", "# aws credentials\nvalue: " + rand(40, ALNUM + "/+"), None),
]


def run_hook(payload, home, **env):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    full_env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), **env)
    full_env.pop("SECRET_SCAN_GUARD_DISABLE", None)
    full_env.update(env)
    return subprocess.run([sys.executable, str(SCRIPT)], input=data, capture_output=True,
                          text=True, timeout=30, env=full_env)


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    (path / ".gitignore").write_text(".env\n")
    return path


@pytest.fixture
def hook(home):
    return lambda payload, **env: run_hook(payload, home, **env)


def write(path, content):
    return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}}


def edit(path, old, new):
    return {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": old, "new_string": new}}


def multi(path, *news):
    return {"tool_name": "MultiEdit", "tool_input": {"file_path": str(path), "edits": [
        {"old_string": "o{}".format(i), "new_string": n} for i, n in enumerate(news)]}}


def bash(repo, command):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}


def config(base, data):
    (base / ".claude").mkdir(exist_ok=True)
    (base / ".claude" / "secret-scan.json").write_text(data if isinstance(data, str) else json.dumps(data))


def decision(r):
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] if r.stdout else None


def allowed(r):
    return r.returncode == 0 and r.stdout == ""


# ---------------------------------------------------------------- edits


@pytest.mark.parametrize("rule", sorted(HIGH))
def test_high_rules_block_write_edit_multiedit(repo, hook, rule):
    secret = HIGH[rule]
    for payload in (write(repo / "a.py", "x = '{}'\n".format(secret)),
                    edit(repo / "a.py", "old", "x = '{}'".format(secret)),
                    multi(repo / "a.py", "fine", "x = '{}'".format(secret))):
        r = hook(payload)
        assert r.returncode == 2, (payload["tool_name"], r.stdout)
        assert rule in r.stderr
        assert secret not in r.stderr + r.stdout


@pytest.mark.parametrize("rule,line,hidden", MEDIUM, ids=[m[0] for m in MEDIUM])
def test_medium_rules_ask_and_redact(repo, hook, rule, line, hidden):
    r = hook(write(repo / "a.py", line + "\n"))
    assert decision(r) == "ask"
    assert rule in r.stdout
    assert (hidden or line.split()[-1]) not in r.stdout + r.stderr


@pytest.mark.parametrize("content", [
    "Example: " + "AK" + "IA" + "IOSFODNN7EXAMPLE",
    'api_key = "your_api_key_goes_here_123"',
    'password = "${DB_PASSWORD_FROM_ENV}"',
    "token = process.env.GITHUB_TOKEN_VALUE_1",
    "url = https://<user>:<password>@example.org",
    '"integrity": "sha512-' + rand(86, ALNUM + "+/") + '=="',
    'secret = "' + "a" * 32 + '"',
    "print('hello')",
])
def test_placeholders_and_clean_content_allowed(repo, hook, content):
    assert allowed(hook(write(repo / "a.py", content)))


@pytest.mark.parametrize("name", ["package-lock.json", "yarn.lock", "go.sum", "pubspec.lock", "dist/app.min.js", "app.js.map"])
def test_lockfiles_and_minified_skipped(repo, hook, name):
    assert allowed(hook(write(repo / name, AWS_KEY)))


def test_binary_content_skipped(repo, hook):
    assert allowed(hook(write(repo / "blob.dat", "\0\x01" + AWS_KEY)))


def test_gitignored_and_outside_repo_skipped(repo, hook, tmp_path):
    assert allowed(hook(write(repo / ".env", "AWS_KEY=" + AWS_KEY)))
    assert allowed(hook(write(tmp_path / "creds.txt", AWS_KEY)))


def test_only_new_content_is_scanned(repo, hook):
    (repo / "a.py").write_text("KEY = '{}'\nx = 1\n".format(AWS_KEY))
    assert allowed(hook(edit(repo / "a.py", "x = 1", "x = 2")))


def test_edit_reports_file_line(repo, hook):
    (repo / "a.py").write_text("a = 1\nb = 2\nc = 3\n")
    r = hook(edit(repo / "a.py", "c = 3", "c = '{}'".format(AWS_KEY)))
    assert r.returncode == 2 and "a.py:3" in r.stderr


def test_new_allow_marker_asks_instead_of_allowing(repo, hook):
    line = "KEY = '{}'  # secret-scan:allow".format(AWS_KEY)
    r = hook(write(repo / "a.py", line + "\n"))
    assert decision(r) == "ask" and "marker" in r.stdout
    (repo / "a.py").write_text(line + "\n")  # marker already present: honoured
    assert allowed(hook(write(repo / "a.py", line + "\nx = 1\n")))


def test_notebook_edit_scanned(repo, hook):
    payload = {"tool_name": "NotebookEdit", "tool_input": {
        "notebook_path": str(repo / "n.ipynb"), "new_source": "k = '{}'".format(GH_TOKEN)}}
    assert hook(payload).returncode == 2


# ---------------------------------------------------------------- git commit


def test_staged_secret_blocks_commit(repo, hook):
    (repo / "settings.py").write_text("TOKEN = '{}'\n".format(GH_TOKEN))
    git(repo, "add", "settings.py")
    r = hook(bash(repo, 'git commit -m "feat: add settings"'))
    assert r.returncode == 2
    assert "settings.py:1" in r.stderr and GH_TOKEN not in r.stderr


@pytest.mark.parametrize("command", [
    'env git commit -m "feat: x"',
    '(git commit -m "feat: x")',
    'bash -c \'git commit -m "feat: x"\'',
])
def test_wrapped_commit_is_scanned(repo, hook, command):
    (repo / "settings.py").write_text("TOKEN = '{}'\n".format(GH_TOKEN))
    git(repo, "add", "settings.py")
    assert hook(bash(repo, command)).returncode == 2, command


def test_clean_commit_allowed(repo, hook):
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "a.py")
    assert allowed(hook(bash(repo, 'git commit -m "feat: x"')))


def test_staged_allow_marker_honoured(repo, hook):
    (repo / "a.py").write_text("KEY = '{}'  # secret-scan:allow\n".format(AWS_KEY))
    git(repo, "add", "a.py")
    assert allowed(hook(bash(repo, 'git commit -m "test: x"')))


def test_commit_all_scans_unstaged_tracked_changes(repo, hook):
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-q", "-m", "chore: init")
    (repo / "a.py").write_text("x = '{}'\n".format(AWS_KEY))
    assert allowed(hook(bash(repo, 'git commit -m "fix: x"')))  # not staged
    assert hook(bash(repo, 'git commit -am "fix: x"')).returncode == 2
    assert hook(bash(repo, 'git commit --all -m "fix: x"')).returncode == 2


def test_commit_pathspec_on_unborn_repo(repo, hook):
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "a.py")
    (repo / "a.py").write_text("x = '{}'\n".format(AWS_KEY))
    assert hook(bash(repo, 'git commit -m "fix: x" a.py')).returncode == 2


def test_commit_with_secret_file_name_asks(repo, hook):
    (repo / "certs").mkdir()
    (repo / "certs" / "server.pem").write_text("not a key\n")
    git(repo, "add", "certs")
    r = hook(bash(repo, 'git commit -m "chore: certs"'))
    assert decision(r) == "ask" and "server.pem" in r.stdout


# ---------------------------------------------------------------- git push


def commit_file(repo, name, content, msg):
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", msg)


def test_push_scans_earlier_local_commits(repo, hook):
    commit_file(repo, "a.py", "k = '{}'\n".format(AWS_KEY), "feat: a")
    commit_file(repo, "b.py", "x = 1\n", "feat: b")
    for command in ("git push", "git push -u origin HEAD"):
        r = hook(bash(repo, command))
        assert r.returncode == 2 and "a.py:1" in r.stderr and AWS_KEY not in r.stderr


def test_push_refspec_scans_source_branch(repo, hook):
    commit_file(repo, "a.py", "x = 1\n", "chore: init")
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
                            capture_output=True, text=True).stdout.strip()
    git(repo, "branch", "clean")
    commit_file(repo, "b.py", "k = '{}'\n".format(GH_TOKEN), "feat: b")
    git(repo, "checkout", "-q", "clean")
    assert allowed(hook(bash(repo, "git push origin HEAD")))
    assert hook(bash(repo, "git push origin {}:main".format(branch))).returncode == 2


def test_push_skips_commits_already_on_remote(repo, hook, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    commit_file(repo, "a.py", "k = '{}'\n".format(AWS_KEY), "feat: a")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "HEAD")
    commit_file(repo, "b.py", "x = 1\n", "feat: b")
    assert allowed(hook(bash(repo, "git push")))


# ---------------------------------------------------------------- shell commands


@pytest.mark.parametrize("command", [
    'curl -H "Authorization: Bearer {}" https://api.github.com'.format(GH_TOKEN),
    "export AWS_SECRET_ACCESS_KEY=" + PASSWORD + "/" + PASSWORD[:15],
])
def test_command_embedding_secret_asks(repo, hook, command):
    r = hook(bash(repo, command))
    assert decision(r) == "ask"
    assert GH_TOKEN not in r.stdout and PASSWORD not in r.stdout


@pytest.mark.parametrize("command", ["git add .env", "git add -f .env.local", "git add certs/server.pem"])
def test_add_sensitive_file_asks(repo, hook, command):
    assert decision(hook(bash(repo, command))) == "ask"


@pytest.mark.parametrize("command", ["git add .env.example", "git add src/app.py", "ls && echo AKIA",
                                     "python -m pytest tests/test_secret_scan.py"])
def test_harmless_commands_allowed(repo, hook, command):
    assert allowed(hook(bash(repo, command)))


# ---------------------------------------------------------------- config and trust


def test_user_allow_paths_and_patterns(repo, hook, home):
    config(home, {"allowPaths": ["tests/fixtures/**"], "allowPatterns": ["^" + GH_TOKEN[:8]]})
    assert allowed(hook(write(repo / "tests" / "fixtures" / "k.txt", AWS_KEY)))
    assert allowed(hook(write(repo / "a.py", GH_TOKEN)))
    assert hook(write(repo / "a.py", AWS_KEY)).returncode == 2


def test_user_allow_paths_apply_to_commits(repo, hook, home):
    config(home, {"allowPaths": ["fixtures/*"]})
    (repo / "fixtures").mkdir()
    (repo / "fixtures" / "key.txt").write_text(HIGH["private-key"])
    git(repo, "add", "fixtures")
    assert allowed(hook(bash(repo, 'git commit -m "test: add fixture"')))


def test_repo_config_cannot_allowlist(repo, hook):
    config(repo, {"allowPaths": ["**"], "allowPatterns": [".*"]})
    r = hook(write(repo / "a.py", AWS_KEY))
    assert r.returncode == 2 and "ignored" in r.stderr
    r = hook(write(repo / "b.py", "x = 1"))
    assert "ignored" in json.loads(r.stdout)["systemMessage"]


@pytest.mark.parametrize("action,expected", [("deny", 2), ("ask", 0)])
def test_repo_extra_patterns(repo, hook, action, expected):
    config(repo, {"extraPatterns": [{"id": "internal-token", "regex": "int_[A-Za-z0-9]{32}", "action": action}]})
    r = hook(write(repo / "a.py", "t = int" + "_" + rand(32)))
    assert r.returncode == expected and "internal-token" in r.stderr + r.stdout


def test_editing_guard_config_or_baseline_asks(repo, hook, home):
    for path in (repo / ".claude" / "secret-scan.json", home / ".claude" / "secret-scan.json",
                 repo / ".claude" / "secret-scan-baseline.json"):
        assert decision(hook(write(path, "{}"))) == "ask", path
    assert decision(hook(bash(repo, "echo '{}' > .claude/secret-scan.json"))) == "ask"
    assert decision(hook(bash(repo, "python secret_scan.py --baseline"))) == "ask"


@pytest.mark.parametrize("text", ["{bad", "[]", '{"allowPaths": "x"}', '{"allowPatterns": ["("]}',
                                  '{"extraPatterns": [{"id": "x", "regex": "y", "action": "allow"}]}'])
def test_invalid_config_falls_back_with_warning(repo, hook, home, text):
    config(home, text)
    r = hook(write(repo / "a.py", AWS_KEY))
    assert r.returncode == 2
    assert "ignoring it" in r.stderr
    r = hook(write(repo / "b.py", "x = 1"))
    assert r.returncode == 0 and "secret-scan-guard" in json.loads(r.stdout)["systemMessage"]


# ---------------------------------------------------------------- baseline


def make_baseline(repo, home):
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    return subprocess.run([sys.executable, str(SCRIPT), "--baseline"], cwd=repo,
                          capture_output=True, text=True, env=env, timeout=30)


def test_baseline_suppresses_known_findings(repo, hook, home):
    (repo / "a.py").write_text("KEY = '{}'\n".format(AWS_KEY))
    git(repo, "add", "a.py")
    r = make_baseline(repo, home)
    assert r.returncode == 0 and "1 fingerprint" in r.stdout
    baseline = (repo / ".claude" / "secret-scan-baseline.json").read_text()
    assert AWS_KEY not in baseline and AWS_KEY not in r.stdout + r.stderr
    assert allowed(hook(bash(repo, 'git commit -m "chore: adopt"')))
    assert allowed(hook(write(repo / "a.py", "KEY = '{}'\n".format(AWS_KEY))))
    assert hook(write(repo / "a.py", GH_TOKEN)).returncode == 2  # a new secret still blocks
    assert hook(write(repo / "b.py", "KEY = '{}'\n".format(AWS_KEY))).returncode == 2  # other file


# ---------------------------------------------------------------- robustness


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"tool_name": "Write", "tool_input": null}'])
def test_malformed_stdin_fails_open(hook, stdin):
    assert allowed(hook(stdin))


def test_disable_env_var(repo, hook):
    assert allowed(hook(write(repo / "a.py", AWS_KEY), SECRET_SCAN_GUARD_DISABLE="1"))
