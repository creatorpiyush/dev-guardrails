"""Tests for refactor-safely/scripts/refactor_gate.py.

Each test writes a small project into a throwaway unborn git repo and runs the
script as a subprocess. The "test command" is a tiny Python script that prints
canned output and exits with a chosen code, so every case is exact and fast.
"""
import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "refactor-safely" / "scripts" / "refactor_gate.py"
PY = sys.executable.replace("\\", "/")
RUNNER = shlex.quote(PY) + " run.py"

PYTEST_OK = "========== 2 passed in 0.10s ==========\n"
PYTEST_ONE_FAILED = ("FAILED tests/test_calc.py::test_add - assert 1 == 2\n"
                     "========== 1 failed, 1 passed in 0.10s ==========\n")


def gate(cwd, *args):
    r = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def write(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
TEST_CALC = ("from calc import add, sub\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
             "def test_sub():\n    assert sub(3, 1) == 2\n")


@pytest.fixture
def covered(repo):
    """calc.py with a test that names every public symbol, and a fake test runner."""
    write(repo, {"calc.py": CALC, "tests/test_calc.py": TEST_CALC, "pytest.ini": "[pytest]\n",
                 "run.py": "import sys\nprint(open('out.txt').read(), end='')\n"
                           "sys.exit(int(open('code.txt').read()))\n",
                 "out.txt": PYTEST_OK, "code.txt": "0"})
    return repo


def set_run(repo, output, code):
    write(repo, {"out.txt": output, "code.txt": str(code)})


def baseline(repo, *extra):
    return gate(repo, "baseline", "--command", RUNNER, *extra)


def ready(repo):
    assert gate(repo, "check", "calc.py")["verdict"] == "covered"
    out = baseline(repo)
    assert out["ok"] and out["verdict"] == "green", out
    return out


# ---------------------------------------------------------------- check

def test_check_covered(covered):
    out = gate(covered, "check", "calc.py")
    assert out["ok"] and out["verdict"] == "covered"
    target = out["targets"][0]
    assert target["existing_tests"] == ["tests/test_calc.py"]
    assert target["untested_symbols"] == [] and target["reasons"] == []
    assert {e["test"] for e in target["evidence"]} == {"tests/test_calc.py"}
    assert out["command"]["run"] == "pytest" and out["command"]["framework"] == "pytest"
    assert out["state_file"].endswith("refactor-safely-state.json")


def test_check_partial_lists_untested_symbols(covered):
    write(covered, {"tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"})
    out = gate(covered, "check", "calc.py")
    assert out["verdict"] == "partial"
    assert out["targets"][0]["untested_symbols"] == ["sub"]
    assert "sub" in out["targets"][0]["reasons"][0]


def test_check_uncovered(covered):
    write(covered, {"other.py": "def lonely():\n    return 1\n"})
    out = gate(covered, "check", "other.py")
    assert out["verdict"] == "uncovered"
    assert out["targets"][0]["reasons"] == ["no test refers to this file"]


def test_check_mixed_targets_are_partial(covered):
    write(covered, {"other.py": "def lonely():\n    return 1\n"})
    out = gate(covered, "check", "calc.py", "other.py")
    assert out["verdict"] == "partial"
    assert [t["verdict"] for t in out["targets"]] == ["covered", "uncovered"]


def test_check_symbol_target(covered):
    out = gate(covered, "check", "sub")
    assert out["verdict"] == "covered"
    assert (out["targets"][0]["symbol"], out["targets"][0]["path"]) == ("sub", "calc.py")


def test_check_symbol_without_a_test_is_partial(covered):
    write(covered, {"calc.py": CALC + "\n\ndef mul(a, b):\n    return a * b\n"})
    assert gate(covered, "check", "mul")["verdict"] == "partial"


def test_check_directory_expands(covered):
    write(covered, {"pkg/a.py": "def one():\n    pass\n", "pkg/b.py": "def two():\n    pass\n"})
    out = gate(covered, "check", "pkg")
    assert sorted(t["path"] for t in out["targets"]) == ["pkg/a.py", "pkg/b.py"]
    assert out["verdict"] == "uncovered"


@pytest.mark.parametrize("args,code", [
    (("check", "missing.py"), "target-not-found"),
    (("check", "nothing_called_this"), "target-not-found"),
    (("check", ".env"), "bad-arguments"),
    (("check", "../outside.py"), "bad-arguments"),
    (("check",), "bad-arguments"),
    ((), "bad-arguments"),
    (("frobnicate",), "bad-arguments"),
])
def test_check_errors(covered, args, code):
    write(covered, {".env": "TOKEN=1\n"})
    (covered.parent / "outside.py").write_text("def f():\n    pass\n", encoding="utf-8")
    out = gate(covered, *args)
    assert out["ok"] is False and out["error"]["code"] == code


def test_not_a_repo(tmp_path):
    assert gate(tmp_path, "step")["error"]["code"] == "not-a-repo"


# ---------------------------------------------------------------- coverage report and config

def test_coverage_report_below_threshold_is_partial(covered):
    write(covered, {"lcov.info": "SF:calc.py\nLF:10\nLH:4\nend_of_record\n",
                    ".claude/refactor-safely.json": '{"requireCoverageOver": 60}'})
    out = gate(covered, "check", "calc.py")
    assert out["verdict"] == "partial" and out["targets"][0]["coverage_percent"] == 40.0
    assert "below requireCoverageOver 60" in out["targets"][0]["reasons"][0]
    assert out["coverage"]["report"] == "lcov.info"


def test_coverage_report_above_threshold_is_covered(covered):
    write(covered, {"lcov.info": "SF:/abs/path/calc.py\nLF:10\nLH:9\nend_of_record\n",
                    ".claude/refactor-safely.json": '{"requireCoverageOver": 60}'})
    out = gate(covered, "check", "calc.py")
    assert out["verdict"] == "covered" and out["targets"][0]["coverage_percent"] == 90.0


def test_cobertura_report(covered):
    xml = ('<coverage><packages><package><classes><class name="calc" filename="calc.py">'
           '<lines><line number="1" hits="1"/><line number="2" hits="0"/><line number="3" hits="1"/>'
           '<line number="4" hits="1"/></lines></class></classes></package></packages></coverage>')
    write(covered, {"coverage.xml": xml})
    assert gate(covered, "check", "calc.py")["targets"][0]["coverage_percent"] == 75.0


def test_istanbul_report(covered):
    data = {"/work/calc.py": {"s": {"0": 1, "1": 0}}}
    write(covered, {"coverage/coverage-final.json": json.dumps(data)})
    assert gate(covered, "check", "calc.py")["targets"][0]["coverage_percent"] == 50.0


def test_threshold_without_a_report_fails_closed(covered):
    write(covered, {".claude/refactor-safely.json": '{"requireCoverageOver": 50}'})
    out = gate(covered, "check", "calc.py")
    assert out["verdict"] == "partial"
    assert any("no readable coverage report" in w for w in out["warnings"])


def test_report_without_threshold_is_informational(covered):
    write(covered, {"lcov.info": "SF:calc.py\nLF:10\nLH:1\nend_of_record\n"})
    out = gate(covered, "check", "calc.py")
    assert out["verdict"] == "covered" and out["targets"][0]["coverage_percent"] == 10.0


def test_unreadable_report_format_warns(covered):
    write(covered, {"coverage.out": "mode: set\n"})
    out = gate(covered, "check", "calc.py")
    assert out["verdict"] == "covered"
    assert any("coverage.out" in w for w in out["warnings"])


def test_configured_command_wins(covered):
    write(covered, {".claude/refactor-safely.json": '{"testCommand": "make check", "stepTimeout": 9}'})
    out = gate(covered, "check", "calc.py")
    assert out["command"]["run"] == "make check" and out["command"]["confidence"] == "configured"
    assert out["config"]["stepTimeout"] == 9


@pytest.mark.parametrize("text", [
    "not json", "[]", '{"testCommand": ""}', '{"testCommand": 5}', '{"stepTimeout": 0}',
    '{"stepTimeout": "9"}', '{"stepTimeout": true}', '{"requireCoverageOver": 101}',
    '{"requireCoverageOver": "x"}',
])
def test_invalid_config_falls_back_with_warning(covered, text):
    write(covered, {".claude/refactor-safely.json": text})
    out = gate(covered, "check", "calc.py")
    assert out["ok"] and out["config"] == {"testCommand": None, "stepTimeout": 300, "requireCoverageOver": 0}
    assert any("refactor-safely.json" in w for w in out["warnings"])


def test_unknown_config_key_warns(covered):
    write(covered, {".claude/refactor-safely.json": '{"stepTimeout": 5, "bogus": 1}'})
    out = gate(covered, "check", "calc.py")
    assert out["config"]["stepTimeout"] == 5 and any("bogus" in w for w in out["warnings"])


def test_no_framework_warns_and_has_no_command(repo):
    write(repo, {"main.py": "def f():\n    pass\n"})
    out = gate(repo, "check", "main.py")
    assert out["command"] is None and any("no test command" in w for w in out["warnings"])


# ---------------------------------------------------------------- baseline

def test_baseline_needs_a_check_first(covered):
    assert baseline(covered)["error"]["code"] == "no-check"


def test_baseline_refuses_uncovered(covered):
    write(covered, {"other.py": "def lonely():\n    return 1\n"})
    gate(covered, "check", "other.py")
    assert baseline(covered)["error"]["code"] == "gate-failed"
    assert baseline(covered, "--acknowledge-partial")["error"]["code"] == "gate-failed"


def test_baseline_refuses_partial_unless_acknowledged(covered):
    write(covered, {"tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"})
    assert gate(covered, "check", "calc.py")["verdict"] == "partial"
    assert baseline(covered)["error"]["code"] == "gate-failed"
    out = baseline(covered, "--acknowledge-partial")
    assert out["ok"] and out["acknowledged_partial"] is True


def test_baseline_green(covered):
    gate(covered, "check", "calc.py")
    out = baseline(covered)
    b = out["baseline"]
    assert out["verdict"] == "green" and out["acknowledged_partial"] is False
    assert (b["framework"], b["passed"], b["failed"], b["total"], b["failing"]) == ("pytest", 2, 0, 2, [])
    assert b["exit_code"] == 0 and out["test_files"] == 1 and "output_tail" not in out


def test_baseline_red_records_failures(covered):
    set_run(covered, PYTEST_ONE_FAILED, 1)
    gate(covered, "check", "calc.py")
    out = baseline(covered)
    assert out["ok"] and out["verdict"] == "red"
    assert out["baseline"]["failing"] == ["tests/test_calc.py::test_add"]
    assert "1 failed" in out["output_tail"]


def test_baseline_uses_the_command_from_check(covered):
    write(covered, {".claude/refactor-safely.json": json.dumps({"testCommand": RUNNER})})
    gate(covered, "check", "calc.py")
    out = gate(covered, "baseline")
    assert out["ok"] and out["command"] == RUNNER


def test_baseline_without_any_command(repo):
    write(repo, {"main.py": "def f():\n    pass\n", "test_main.py": "from main import f\n\n\ndef test_f():\n    f()\n"})
    gate(repo, "check", "main.py")
    assert gate(repo, "baseline")["error"]["code"] in ("no-test-command", "gate-failed")


def test_baseline_command_that_cannot_start(covered):
    gate(covered, "check", "calc.py")
    out = gate(covered, "baseline", "--command", "definitely-not-a-program-xyz")
    assert out["error"]["code"] == "command-failed"


def test_baseline_unterminated_quote(covered):
    gate(covered, "check", "calc.py")
    assert gate(covered, "baseline", "--command", "python 'oops")["error"]["code"] == "command-failed"


def test_baseline_timeout_is_stopped(covered):
    write(covered, {"sleepy.py": "import time\ntime.sleep(60)\n",
                    ".claude/refactor-safely.json": '{"stepTimeout": 1}'})
    gate(covered, "check", "calc.py")
    out = gate(covered, "baseline", "--command", shlex.quote(PY) + " sleepy.py")
    assert out["error"]["code"] == "timeout" and "1s" in out["error"]["message"]


def test_unrecognised_output_warns_and_uses_exit_code(covered):
    set_run(covered, "all good\n", 0)
    gate(covered, "check", "calc.py")
    out = baseline(covered)
    assert out["verdict"] == "green" and out["baseline"]["framework"] is None
    assert any("wasn't recognised" in w for w in out["warnings"])


# ---------------------------------------------------------------- step

def test_step_needs_a_baseline(covered):
    assert gate(covered, "step")["error"]["code"] == "no-baseline"
    gate(covered, "check", "calc.py")
    assert gate(covered, "step")["error"]["code"] == "no-baseline"


def test_step_green(covered):
    ready(covered)
    out = gate(covered, "step")
    assert out["ok"] and out["verdict"] == "green" and out["regressed"] is False
    assert out["step"] == 1 and out["new_failures"] == [] and out["fixed"] == []
    assert out["tests_changed"] == {"modified": [], "deleted": [], "added": []}
    assert gate(covered, "step")["step"] == 2


def test_step_new_failure(covered):
    ready(covered)
    set_run(covered, PYTEST_ONE_FAILED, 1)
    out = gate(covered, "step")
    assert out["verdict"] == "red" and out["regressed"] is True
    assert out["new_failures"] == ["tests/test_calc.py::test_add"]
    assert out["reasons"] == ["1 new failing test(s)"]
    assert "1 failed" in out["output_tail"]


def test_step_fixed_test_against_red_baseline(covered):
    set_run(covered, PYTEST_ONE_FAILED, 1)
    gate(covered, "check", "calc.py")
    assert baseline(covered)["verdict"] == "red"
    set_run(covered, PYTEST_OK, 0)
    out = gate(covered, "step")
    assert out["fixed"] == ["tests/test_calc.py::test_add"] and out["new_failures"] == []
    assert out["regressed"] is False and out["verdict"] == "green"


def test_step_same_failures_as_red_baseline_is_not_a_regression(covered):
    set_run(covered, PYTEST_ONE_FAILED, 1)
    gate(covered, "check", "calc.py")
    baseline(covered)
    out = gate(covered, "step")
    assert out["verdict"] == "red" and out["regressed"] is False and out["new_failures"] == []


def test_step_test_count_falling_is_a_regression(covered):
    ready(covered)
    set_run(covered, "========== 1 passed in 0.10s ==========\n", 0)
    out = gate(covered, "step")
    assert out["regressed"] is True and out["reasons"] == ["test count fell from 2 to 1"]


def test_step_exit_code_only_when_output_unrecognised(covered):
    set_run(covered, "fine\n", 0)
    gate(covered, "check", "calc.py")
    baseline(covered)
    set_run(covered, "boom\n", 3)
    out = gate(covered, "step")
    assert out["regressed"] is True and out["reasons"] == ["exit code 3 after a green baseline"]
    assert out["new_failures"] is None


def test_step_reports_changed_tests(covered):
    ready(covered)
    write(covered, {"tests/test_calc.py": TEST_CALC.replace("== 3", "== 4"),
                    "tests/test_new.py": "def test_new():\n    pass\n"})
    out = gate(covered, "step")
    assert out["tests_changed"] == {"modified": ["tests/test_calc.py"], "deleted": [],
                                    "added": ["tests/test_new.py"]}
    (covered / "tests" / "test_calc.py").unlink()
    assert gate(covered, "step")["tests_changed"]["deleted"] == ["tests/test_calc.py"]


def test_step_timeout(covered):
    write(covered, {"sleepy.py": "import time\ntime.sleep(60)\n"})
    ready(covered)
    state_file = covered / ".git" / "refactor-safely-state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["baseline"]["command"] = shlex.quote(PY) + " sleepy.py"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    write(covered, {".claude/refactor-safely.json": '{"stepTimeout": 1}'})
    assert gate(covered, "step")["error"]["code"] == "timeout"


def test_step_diff_stat_in_a_repo_with_a_commit(covered):
    g = ["git", "-c", "user.name=T", "-c", "user.email=t@example.com"]
    subprocess.run(g + ["add", "-A"], cwd=covered, check=True, capture_output=True)
    subprocess.run(g + ["commit", "-q", "-m", "init"], cwd=covered, check=True, capture_output=True)
    ready(covered)
    write(covered, {"calc.py": CALC + "\n\ndef mul(a, b):\n    return a * b\n", "new.py": "x = 1\n"})
    out = gate(covered, "step")
    assert out["diff"] == {"files": 1, "added": 4, "deleted": 0, "untracked": 1}
    assert out["diff_at_baseline"] == {"files": 0, "added": 0, "deleted": 0, "untracked": 0}


# ---------------------------------------------------------------- state

def test_state_lives_in_git_and_reset_clears_it(covered):
    gate(covered, "check", "calc.py")
    state_file = covered / ".git" / "refactor-safely-state.json"
    assert state_file.is_file()
    assert gate(covered, "reset") == {"ok": True, "error": None, "warnings": [], "removed": True}
    assert not state_file.exists()
    assert gate(covered, "reset")["removed"] is False


def test_check_starts_a_fresh_state(covered):
    ready(covered)
    gate(covered, "check", "calc.py")
    assert gate(covered, "step")["error"]["code"] == "no-baseline"


def test_corrupt_state_is_treated_as_missing(covered):
    gate(covered, "check", "calc.py")
    (covered / ".git" / "refactor-safely-state.json").write_text("{nope", encoding="utf-8")
    assert baseline(covered)["error"]["code"] == "no-check"


# ---------------------------------------------------------------- reading test output

def load():
    spec = importlib.util.spec_from_file_location("refactor_gate_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OUTPUTS = [
    ("pytest", "FAILED tests/test_a.py::test_x - assert 1 == 2\nERROR tests/test_b.py - ImportError\n"
               "===== 1 failed, 2 passed, 1 skipped, 1 error in 0.12s =====\n",
     (2, 2, 1, 5, ["tests/test_a.py::test_x", "tests/test_b.py"])),
    ("pytest", "3 passed in 0.05s\n", (3, 0, 0, 3, [])),
    ("pytest", "===== no tests ran in 0.01s =====\n", (0, 0, 0, 0, [])),
    ("unittest", "FAIL: test_x (tests.test_a.T.test_x)\nERROR: test_y (tests.test_a.T)\n\n"
                 "Ran 5 tests in 0.002s\n\nFAILED (failures=1, errors=1, skipped=1)\n",
     (2, 2, 1, 5, ["tests.test_a.T.test_x", "tests.test_a.T.test_y"])),
    ("unittest", "Ran 3 tests in 0.001s\n\nOK\n", (3, 0, 0, 3, [])),
    ("jest", "FAIL src/a.test.js\n  ● Suite › does x\n  ● Console\n\nTests:       1 failed, 1 skipped, 2 passed, 4 total\n",
     (2, 1, 1, 4, ["Suite › does x"])),
    ("vitest", " FAIL  src/a.test.ts > suite > t\n\n Tests  1 failed | 2 passed (3)\n",
     (2, 1, 0, 3, ["src/a.test.ts > suite > t"])),
    ("dart test", "00:01 +0: loading test/a_test.dart\n00:02 +3 ~1 -1: test/a_test.dart: Cart adds [E]\n"
                  "00:03 +3 ~1 -1: Some tests failed.\n",
     (3, 1, 1, 5, ["test/a_test.dart: Cart adds"])),
    ("dart test", "00:05 +12: All tests passed!\n", (12, 0, 0, 12, [])),
    ("dart test", '00:01 +0 -1: loading test/b_test.dart [E]\n  Failed to load "test/b_test.dart": Compilation failed\n'
                  "00:01 +0 -1: Some tests failed.\n",
     (0, 1, 0, 1, ["loading test/b_test.dart", "load: test/b_test.dart"])),
    ("go test", "--- FAIL: TestX (0.00s)\n    --- FAIL: TestX/sub (0.00s)\nFAIL\nFAIL\texample.com/pkg\t0.01s\n",
     (None, 2, None, None, ["TestX", "TestX/sub"])),
    ("go test", "FAIL\texample.com/pkg [build failed]\n", (None, 1, None, None, ["example.com/pkg [build failed]"])),
    ("cargo test", "test a::t ... FAILED\ntest result: FAILED. 2 passed; 1 failed; 1 ignored; 0 measured\n"
                   "test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured\n",
     (5, 1, 1, 7, ["a::t"])),
    ("mocha", "  2 passing (5ms)\n  1 pending\n  1 failing\n\n  1) Suite\n       does x:\n",
     (2, 1, 1, 4, ["Suite"])),
    ("rspec", "3 examples, 1 failure, 1 pending\n\nFailed examples:\n\nrspec ./spec/a_spec.rb:3 # x\n",
     (1, 1, 1, 3, ["./spec/a_spec.rb:3"])),
    ("junit", "[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 1 s <<< FAILURE! - in FooTest\n"
              "[ERROR]   FooTest.testBar:12 expected: <1> but was: <2>\n[ERROR] Tests run: 5, Failures: 1, Errors: 0, Skipped: 1\n",
     (3, 1, 1, 5, ["FooTest.testBar"])),
    ("junit", "FooTest > testBar() FAILED\n\n3 tests completed, 1 failed\n", (2, 1, 0, 3, ["FooTest > testBar()"])),
    ("phpunit", "1) FooTest::testBar\n\nTests: 3, Assertions: 4, Failures: 1.\n", (2, 1, 0, 3, ["FooTest::testBar"])),
    ("phpunit", "OK (3 tests, 4 assertions)\n", (3, 0, 0, 3, [])),
    ("dotnet test", "  Failed Foo.Bar.Test [5 ms]\nFailed!  - Failed:     1, Passed:     2, Skipped:     0, Total:     3, Duration: 1 s\n",
     (2, 1, 0, 3, ["Foo.Bar.Test"])),
]


@pytest.mark.parametrize("framework,text,expected", OUTPUTS)
def test_reads_runner_output(framework, text, expected):
    got = load().read_output(text)
    assert got["framework"] == framework
    assert (got["passed"], got["failed"], got["skipped"], got["total"], got["failing"]) == expected


def test_unknown_output_has_no_counts():
    got = load().read_output("hello\nworld\n")
    assert got["framework"] is None and got["failing"] is None and got["total"] is None


def test_output_is_ansi_stripped_before_reading(covered):
    set_run(covered, "\x1b[32m========== 2 passed in 0.10s ==========\x1b[0m\n", 0)
    gate(covered, "check", "calc.py")
    assert baseline(covered)["baseline"]["total"] == 2


@pytest.mark.parametrize("text", ["\n" * 200000, " \n" * 100000, "\t" * 300000, "a." * 200000])
def test_pathological_output_is_read_quickly(text):
    import time
    start = time.time()
    assert load().read_output(text)["framework"] is None
    assert time.time() - start < 5


def test_unclosed_cobertura_classes_are_read_quickly():
    import time
    text = '<class name="a" filename="a.py"><line number="1" hits="1"/>' * 20000
    start = time.time()
    entries = load().parse_cobertura(text)
    assert len(entries) == 20000 and time.time() - start < 5


def test_check_on_a_flutter_package(repo):
    write(repo, {"pubspec.yaml": "dependencies:\n  flutter:\n    sdk: flutter\ndev_dependencies:\n"
                                 "  flutter_test:\n    sdk: flutter\n",
                 "lib/src/cart.dart": "class Cart {\n  void add(int x) {}\n}\n\nint total(List<int> xs) => 0;\n",
                 "lib/src/tax.dart": "double tax(double x) {\n  return x;\n}\n",
                 "test/cart_test.dart": "import 'package:shop/src/cart.dart';\nvoid main() {\n  Cart();\n  total([]);\n}\n"})
    covered = gate(repo, "check", "lib/src/cart.dart")
    assert covered["verdict"] == "covered" and covered["command"]["run"] == "flutter test"
    assert gate(repo, "check", "lib/src/tax.dart")["verdict"] == "uncovered"
