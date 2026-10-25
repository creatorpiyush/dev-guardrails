"""Plugins install separately and can't import each other, so some code is
copied between them (see CLAUDE.md). These tests fail when a copy drifts."""
import ast
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GIT_GUARD = ROOT / "git-workflow-guard" / "hooks" / "git_guard.py"
SECRET_SCAN = ROOT / "secret-scan-guard" / "hooks" / "secret_scan.py"
PATTERNS = ROOT / "secret-scan-guard" / "hooks" / "patterns.py"
PROTECTED_FILES = ROOT / "protected-files-guard" / "hooks" / "protected_files_guard.py"
PR_WRITER = ROOT / "pr-writer" / "scripts" / "collect_context.py"
TEST_BEFORE_COMMIT = ROOT / "test-before-commit" / "hooks" / "test_before_commit.py"
HUMANIZER_SCAN = ROOT / "humanizer" / "scripts" / "scan.py"
HUMANIZER_NUDGE = ROOT / "humanizer" / "hooks" / "humanizer_nudge.py"


def definition(path, name):
    """ast.dump of a top-level function (docstring dropped) or assignment."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                node.body = body[1:]
            return ast.dump(node)
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.dump(node.value)
    raise AssertionError("{} not found in {}".format(name, path))


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem + "_sync", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", [
    "SEPARATOR_CHARS", "KEYWORDS", "WRAPPERS", "SHELLS", "SHELL_OPTS_WITH_VALUE", "MAX_DEPTH",
    "GIT_OPTS_WITH_VALUE", "split_segments", "takes_separate_value", "unwrap", "nested_script",
    "git_invocations",
])
def test_shell_parsing_copied_to_secret_scan(name):
    assert definition(GIT_GUARD, name) == definition(SECRET_SCAN, name)


@pytest.mark.parametrize("name", [
    "SEPARATOR_CHARS", "KEYWORDS", "WRAPPERS", "SHELLS", "SHELL_OPTS_WITH_VALUE", "MAX_DEPTH",
    "GIT_OPTS_WITH_VALUE", "split_segments", "takes_separate_value", "unwrap", "nested_script",
    "git_invocations",
])
def test_shell_parsing_copied_to_test_before_commit(name):
    assert definition(GIT_GUARD, name) == definition(TEST_BEFORE_COMMIT, name)


def test_glob_re_copied_to_test_before_commit():
    assert definition(PROTECTED_FILES, "glob_re") == definition(TEST_BEFORE_COMMIT, "glob_re")


@pytest.mark.parametrize("name", [
    "SEPARATOR_CHARS", "KEYWORDS", "WRAPPERS", "SHELLS", "SHELL_OPTS_WITH_VALUE", "MAX_DEPTH",
    "GIT_OPTS_WITH_VALUE", "split_segments", "takes_separate_value", "unwrap", "nested_script",
    "git_invocations",
])
def test_shell_parsing_copied_to_humanizer(name):
    assert definition(GIT_GUARD, name) == definition(HUMANIZER_NUDGE, name)


def test_glob_re_copied_to_humanizer():
    assert definition(PROTECTED_FILES, "glob_re") == definition(HUMANIZER_SCAN, "glob_re")


@pytest.mark.parametrize("name", [
    "PROTECTED", "PROTECTED_PREFIXES", "is_protected", "TYPES", "CONVENTIONAL", "load_rules",
])
def test_branch_rules_copied_to_pr_writer(name):
    copy = definition(PR_WRITER, name).replace("GUARD_CONFIG", "CONFIG_FILE")
    assert definition(GIT_GUARD, name) == copy


def test_secret_rules_copied_to_pr_writer():
    patterns = load(PATTERNS)
    high = [(r.id, r.regex.pattern) for r in patterns.RULES if r.confidence == patterns.HIGH]
    copy = [(name, rx.pattern) for name, rx in load(PR_WRITER).SECRET_RULES]
    assert copy == high


def test_wrapper_options_match_protected_files():
    assert definition(GIT_GUARD, "takes_separate_value") == definition(
        PROTECTED_FILES, "takes_separate_value")


@pytest.mark.parametrize("pattern", [
    ".env", ".env.*", "*.pem", "LICENSE*", ".github/workflows/**", "**/migrations/**",
    "docs/", "/build", "src/*.py", "file?.txt", "[ab].txt", "[!a]x", "a/**/b",
])
@pytest.mark.parametrize("path", [
    ".env", "config/.env.local", "KEY.PEM", "LICENSE.md", ".github/workflows/ci.yml",
    "app/migrations/0001.py", "docs/a/b.md", "build", "src/build", "src/x.py", "src/a/x.py",
    "file1.txt", "a.txt", "bx", "a/b", "a/x/y/b",
])
def test_glob_re_copied_to_pr_writer(pattern, path):
    original = load(PROTECTED_FILES).glob_re(pattern)
    copy = load(PR_WRITER).glob_re(pattern)
    assert bool(original.fullmatch(path)) == bool(copy.fullmatch(path))
    assert copy.flags & re.IGNORECASE


LOG_ACTIVITY = ROOT / "session-recap" / "hooks" / "log_activity.py"


@pytest.mark.parametrize("name", [
    "SEPARATOR_CHARS", "KEYWORDS", "WRAPPERS", "SHELLS", "SHELL_OPTS_WITH_VALUE", "MAX_DEPTH",
    "split_segments", "takes_separate_value", "unwrap", "nested_script",
])
def test_shell_parsing_copied_to_session_recap(name):
    assert definition(GIT_GUARD, name) == definition(LOG_ACTIVITY, name)


@pytest.mark.parametrize("name", ["SECRET_RULES", "EXTRA_REDACTIONS", "KEY_BLOCK"])
def test_redaction_rules_copied_to_session_recap(name):
    assert definition(PR_WRITER, name) == definition(LOG_ACTIVITY, name)


COLLECT_RECAP = ROOT / "session-recap" / "scripts" / "collect_recap.py"


@pytest.mark.parametrize("name", ["log_dir", "safe_session"])
def test_log_paths_copied_to_collect_recap(name):
    assert definition(LOG_ACTIVITY, name) == definition(COLLECT_RECAP, name)


def test_detect_tests_copied_to_bug_reproducer():
    original = ROOT / "test-writer" / "scripts" / "detect_tests.py"
    copy = ROOT / "bug-reproducer" / "scripts" / "detect_tests.py"
    assert original.read_bytes() == copy.read_bytes()
