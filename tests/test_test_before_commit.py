"""Tests for test-before-commit/hooks/test_before_commit.py.

Each test builds a throwaway repo with staged files, pipes a PreToolUse
payload into the script with a fake HOME, and checks the exit code and output.
"""
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "test-before-commit" / "hooks" / "test_before_commit.py"


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    e = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    e.pop("TEST_BEFORE_COMMIT_DISABLE", None)
    return e


@pytest.fixture
def repo(tmp_path, env):
    """An unborn repo with app.py staged. Helpers write configs and command scripts."""
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = Path(env["HOME"])

    class Repo:
        root = path
        counter = tmp_path / "count.txt"  # outside the repo, so it doesn't change the diff

        def stage(self, name, text="x = 1\n"):
            f = path / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
            git(path, "add", name)

        def script(self, name, body):
            """A config "run" string that runs a Python script."""
            f = bindir / (name + ".py")
            f.write_text(body)
            return "{} {}".format(shlex.quote(sys.executable), shlex.quote(str(f)))

        def passing(self):
            return self.script("count", "import sys\nopen(sys.argv[1], 'a').write('x')\n") + \
                " " + shlex.quote(str(self.counter))

        def runs(self):
            return len(self.counter.read_text()) if self.counter.exists() else 0

        def repo_config(self, cfg):
            (path / ".claude").mkdir(exist_ok=True)
            (path / ".claude" / "test-before-commit.json").write_text(
                cfg if isinstance(cfg, str) else json.dumps(cfg))

        def user_config(self, cfg):
            (home / ".claude" / "test-before-commit.json").write_text(
                cfg if isinstance(cfg, str) else json.dumps(cfg))

    r = Repo()
    r.stage("app.py")
    return r


def run_hook(env, payload):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(SCRIPT)], input=data, capture_output=True,
                          text=True, timeout=60, env=env)


def bash(repo, command="git commit -m 'feat: x'"):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo.root)}


def trust(env, repo):
    return subprocess.run([sys.executable, str(SCRIPT), "--trust"], cwd=str(repo.root),
                          capture_output=True, text=True, timeout=30, env=env)


def decision(result):
    out = json.loads(result.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


def commands(*items):
    return {"commands": [{"name": n, "run": r, **extra} for n, r, extra in items]}


def test_no_config_allows(env, repo):
    r = run_hook(env, bash(repo))
    assert r.returncode == 0 and r.stdout == ""


def test_user_config_command_runs_without_approval(env, repo):
    repo.user_config(commands(("unit", repo.passing(), {})))
    r = run_hook(env, bash(repo))
    assert r.returncode == 0 and r.stdout == ""
    assert repo.runs() == 1


def test_failing_command_blocks_with_trimmed_output(env, repo):
    run = repo.script("fail", "for i in range(200):\n    print('line', i)\nraise SystemExit(3)\n")
    repo.user_config(commands(("unit tests", run, {})))
    r = run_hook(env, bash(repo))
    assert r.returncode == 2
    assert "'unit tests' failed with exit code 3" in r.stderr
    assert "line 199" in r.stderr and "line 140\n" in r.stderr
    assert "line 139\n" not in r.stderr
    assert "fix the cause" in r.stderr


def test_long_lines_are_capped(env, repo):
    run = repo.script("wide", "print('y' * 100000)\nraise SystemExit(1)\n")
    repo.user_config(commands(("wide", run, {})))
    r = run_hook(env, bash(repo))
    assert r.returncode == 2 and len(r.stderr) < 5000


def test_stops_at_first_failure(env, repo):
    repo.user_config(commands(("lint", repo.script("bad", "raise SystemExit(1)\n"), {}),
                              ("unit", repo.passing(), {})))
    r = run_hook(env, bash(repo))
    assert r.returncode == 2 and repo.runs() == 0


def test_timeout_asks(env, repo):
    run = repo.script("slow", "import time\nprint('starting', flush=True)\ntime.sleep(30)\n")
    repo.user_config(commands(("slow suite", run, {"timeout": 1})))
    start = time.monotonic()
    r = run_hook(env, bash(repo))
    assert time.monotonic() - start < 20
    kind, reason = decision(r)
    assert kind == "ask" and "'slow suite'" in reason and "didn't finish within 1s" in reason
    assert "starting" in reason


def test_missing_executable_asks(env, repo):
    repo.user_config(commands(("lint", "no-such-tool-xyz --check", {})))
    kind, reason = decision(run_hook(env, bash(repo)))
    assert kind == "ask" and "couldn't start" in reason


def test_shell_command(env, repo):
    repo.user_config(commands(("sh", "exit 4", {"shell": True})))
    r = run_hook(env, bash(repo))
    assert r.returncode == 2 and "exit code 4" in r.stderr


def test_docs_only_commit_skipped(env, tmp_path):
    path = tmp_path / "docs-repo"
    path.mkdir()
    git(path, "init", "-q")
    for name in ("README.md", "docs/guide.txt", "LICENSE"):
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text("text\n")
        git(path, "add", name)
    (Path(env["HOME"]) / ".claude" / "test-before-commit.json").write_text(
        json.dumps(commands(("t", "no-such-tool-xyz", {}))))
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m 'docs: x'"},
               "cwd": str(path)}
    r = run_hook(env, payload)
    assert r.returncode == 0 and r.stdout == ""


def test_code_change_with_docs_runs(env, repo):
    repo.stage("README.md", "hi\n")
    repo.user_config(commands(("unit", repo.passing(), {})))
    run_hook(env, bash(repo))
    assert repo.runs() == 1


def test_cache_hit_skips_rerun_and_invalidates(env, repo):
    repo.user_config(commands(("unit", repo.passing(), {})))
    run_hook(env, bash(repo))
    run_hook(env, bash(repo))
    assert repo.runs() == 1
    assert (repo.root / ".git" / "test-before-commit-cache.json").exists()
    repo.stage("app.py", "x = 2\n")
    run_hook(env, bash(repo))
    assert repo.runs() == 2


def test_unstaged_change_invalidates_cache(env, repo):
    repo.user_config(commands(("unit", repo.passing(), {})))
    run_hook(env, bash(repo))
    (repo.root / "app.py").write_text("x = 3\n")  # `git commit -a` would commit this
    run_hook(env, bash(repo))
    assert repo.runs() == 2


def test_failure_is_not_cached(env, repo):
    repo.user_config(commands(("lint", repo.script("bad", "raise SystemExit(1)\n"), {})))
    assert run_hook(env, bash(repo)).returncode == 2
    assert run_hook(env, bash(repo)).returncode == 2


def test_repo_config_needs_approval(env, repo):
    repo.repo_config(commands(("unit", repo.passing(), {})))
    kind, reason = decision(run_hook(env, bash(repo)))
    assert kind == "ask" and "/test-before-commit:trust" in reason
    assert "count.py" in reason  # shows the exact command
    assert repo.runs() == 0

    t = trust(env, repo)
    assert t.returncode == 0, t.stdout
    r = run_hook(env, bash(repo))
    assert r.returncode == 0 and r.stdout == ""
    assert repo.runs() == 1


def test_changed_repo_config_needs_approval_again(env, repo):
    repo.repo_config(commands(("unit", repo.passing(), {})))
    trust(env, repo)
    repo.repo_config(commands(("unit", repo.passing(), {}), ("extra", "echo hi", {})))
    kind, reason = decision(run_hook(env, bash(repo)))
    assert kind == "ask" and "it changed" in reason and "echo hi" in reason
    assert repo.runs() == 0


def test_repo_config_without_commands_needs_no_approval(env, repo):
    repo.repo_config({"skipPatterns": []})
    r = run_hook(env, bash(repo))
    assert r.returncode == 0 and r.stdout == ""


def test_trust_without_repo_config_fails(env, repo):
    assert trust(env, repo).returncode == 1


def test_autodetect_off_by_default(env, repo):
    (repo.root / "package.json").write_text(json.dumps({"scripts": {"test": "exit 1"}}))
    (repo.root / "Cargo.toml").write_text("")
    repo.user_config({"skipPatterns": []})
    r = run_hook(env, bash(repo))
    assert r.returncode == 0 and r.stdout == ""


def test_autodetect_infers_commands(tmp_path):
    spec = importlib.util.spec_from_file_location("tbc", str(SCRIPT))
    tbc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tbc)
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {
        "lint": "eslint .", "test": "echo \"Error: no test specified\" && exit 1"}}))
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "go.mod").write_text("module x\n")
    assert [c["run"] for c in tbc.autodetect(str(tmp_path))] == [
        "npm run lint", "pytest -q", "go test ./..."]


@pytest.mark.parametrize("pubspec,run", [
    ("dependencies:\n  flutter:\n    sdk: flutter\ndev_dependencies:\n  flutter_test:\n    sdk: flutter\n",
     "flutter test"),
    ("name: x\ndev_dependencies:\n  test: ^1.24.0\n", "dart test"),
])
def test_autodetect_dart_and_flutter(tmp_path, pubspec, run):
    spec = importlib.util.spec_from_file_location("tbc", str(SCRIPT))
    tbc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tbc)
    (tmp_path / "pubspec.yaml").write_text(pubspec)
    assert [c["run"] for c in tbc.autodetect(str(tmp_path))] == [run]


@pytest.mark.parametrize("marker,run", [
    (".fvmrc", "fvm flutter test"), (".fvm/fvm_config.json", "fvm flutter test"), (None, "flutter test"),
])
def test_autodetect_flutter_uses_fvm_when_the_repo_pins_it(tmp_path, marker, run):
    spec = importlib.util.spec_from_file_location("tbc", str(SCRIPT))
    tbc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tbc)
    (tmp_path / "pubspec.yaml").write_text("dependencies:\n  flutter:\n    sdk: flutter\n")
    if marker:
        (tmp_path / marker).parent.mkdir(exist_ok=True)
        (tmp_path / marker).write_text("{}")
    assert [c["run"] for c in tbc.autodetect(str(tmp_path))] == [run]


def test_autodetect_pubspec_without_a_test_dependency(tmp_path):
    spec = importlib.util.spec_from_file_location("tbc", str(SCRIPT))
    tbc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tbc)
    (tmp_path / "pubspec.yaml").write_text("name: x\n")
    assert tbc.autodetect(str(tmp_path)) == []


def test_autodetected_repo_commands_need_approval(env, repo):
    (repo.root / "go.mod").write_text("module x\n")
    repo.repo_config({"autodetect": True})
    kind, reason = decision(run_hook(env, bash(repo)))
    assert kind == "ask" and "go test ./..." in reason


def test_non_commit_git_command_ignored(env, repo):
    repo.user_config(commands(("lint", "no-such-tool-xyz", {})))
    r = run_hook(env, bash(repo, "git status && git log --oneline"))
    assert r.returncode == 0 and r.stdout == ""


@pytest.mark.parametrize("command", [
    "git commit --amend --no-edit",
    "echo commit",
])
def test_skipped_commands(env, repo, command):
    repo.user_config(commands(("lint", repo.script("bad", "raise SystemExit(1)\n"), {})))
    r = run_hook(env, bash(repo, command))
    assert r.returncode == 0 and r.stdout == ""


def test_merge_commit_skipped(env, repo):
    repo.user_config(commands(("lint", repo.script("bad", "raise SystemExit(1)\n"), {})))
    git(repo.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo.root, capture_output=True,
                          text=True).stdout
    (repo.root / ".git" / "MERGE_HEAD").write_text(head)
    r = run_hook(env, bash(repo, "git commit --no-edit"))
    assert r.returncode == 0 and r.stdout == ""


@pytest.mark.parametrize("command", [
    "cd sub && git commit -m 'feat: x'",
    "(git commit -m 'feat: x')",
    "bash -c \"git commit -m 'feat: x'\"",
    "env GIT_EDITOR=true git -C . commit",
])
def test_wrapped_commits_checked(env, repo, command):
    (repo.root / "sub").mkdir()
    repo.user_config(commands(("lint", repo.script("bad", "raise SystemExit(1)\n"), {})))
    assert run_hook(env, bash(repo, command)).returncode == 2


def test_outside_repo_allowed(env, tmp_path, repo):
    repo.user_config(commands(("lint", "no-such-tool-xyz", {})))
    outside = tmp_path / "plain"
    outside.mkdir()
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"},
               "cwd": str(outside)}
    r = run_hook(env, payload)
    assert r.returncode == 0 and r.stdout == ""


@pytest.mark.parametrize("payload", ["not json", "[]", "{}", json.dumps({"tool_name": "Bash"})])
def test_malformed_input_fails_open(env, payload):
    r = run_hook(env, payload)
    assert r.returncode == 0 and r.stdout == ""


def test_disable_env_var(env, repo):
    repo.user_config(commands(("lint", repo.script("bad", "raise SystemExit(1)\n"), {})))
    r = run_hook(dict(env, TEST_BEFORE_COMMIT_DISABLE="1"), bash(repo))
    assert r.returncode == 0 and r.stdout == ""


@pytest.mark.parametrize("cfg", ["{bad json", json.dumps({"commands": [{"name": "x"}]}),
                                 json.dumps({"autodetect": "yes"})])
def test_invalid_config_allows_and_warns(env, repo, cfg):
    repo.repo_config(cfg)
    r = run_hook(env, bash(repo))
    assert r.returncode == 0
    assert "ignoring invalid" in r.stderr
    assert "ignoring invalid" in json.loads(r.stdout)["systemMessage"]


def test_trust_command_in_shell_asks(env, repo):
    repo.repo_config(commands(("unit", "pytest -q", {})))
    kind, reason = decision(run_hook(env, bash(
        repo, "python3 /x/hooks/test_before_commit.py --trust")))
    assert kind == "ask" and "pytest -q" in reason


@pytest.mark.parametrize("name", ["test-before-commit.json", "test-before-commit-trust.json"])
def test_editing_user_files_asks(env, name):
    path = Path(env["HOME"]) / ".claude" / name
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": "{}"}}
    kind, _ = decision(run_hook(env, payload))
    assert kind == "ask"


def test_editing_other_files_allowed(env, repo):
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(repo.root / "app.py")}}
    r = run_hook(env, payload)
    assert r.returncode == 0 and r.stdout == ""
