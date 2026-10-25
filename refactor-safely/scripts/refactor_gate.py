#!/usr/bin/env python3
"""Gate for the refactor-safely skill.

  refactor_gate.py check <path... | symbol>      are the targets covered by tests?
  refactor_gate.py baseline [--command CMD]      run the tests, record the starting point
                            [--acknowledge-partial]
  refactor_gate.py step                          re-run the tests, compare with the baseline
  refactor_gate.py reset                         clear the state

Prints exactly one JSON object on stdout and always exits 0. Failures are
reported as {"ok": false, "error": {"code", "message"}}.

Never edits project files. It writes only its state file (git's
refactor-safely-state.json, inside .git) and a temp file for test output.
`baseline` and `step` run the test command; nothing else is ever executed.

detect_tests.py sits next to this file (a byte-identical copy of test-writer's)
and supplies the repo scan, test discovery, and the test command.

Standard library only. Python 3.9+. No network.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import detect_tests as dt
except Exception as e:  # reported as JSON by main()
    dt, IMPORT_ERROR = None, e

STATE_NAME = "refactor-safely-state.json"
CONFIG_FILE = os.path.join(".claude", "refactor-safely.json")
DEFAULT_CONFIG = {"testCommand": None, "stepTimeout": 300, "requireCoverageOver": 0}

PARSE_BYTES = 2 * 1024 * 1024   # tail of test output searched for counts and failing names
TAIL_LINES = 40
LINE_CAP = 400
MAX_NAMES = 200
MAX_TARGETS = 50
MAX_HASH_FILES = 5000
MAX_HASH_BYTES = 1024 * 1024
MAX_REPORT = 20 * 1024 * 1024   # bytes of a coverage report that are read

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def fail(code, message, **extra):
    out = {"ok": False, "error": {"code": code, "message": message}, "warnings": []}
    out.update(extra)
    return out


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parse_args(argv):
    p = _Parser(prog="refactor_gate.py", add_help=False)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("check", add_help=False).add_argument("targets", nargs="+")
    b = sub.add_parser("baseline", add_help=False)
    b.add_argument("--command")
    b.add_argument("--acknowledge-partial", action="store_true")
    sub.add_parser("step", add_help=False)
    sub.add_parser("reset", add_help=False)
    a = p.parse_args(argv)
    if not a.cmd:
        raise ValueError("expected one of: check, baseline, step, reset")
    return a


# ---------------------------------------------------------------- config and state

def load_config(root, warnings):
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(root, CONFIG_FILE)
    if not os.path.isfile(path):
        return cfg
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        warnings.append("%s is not valid JSON (%s); using detection and defaults" % (CONFIG_FILE, e))
        return cfg
    problems = []
    if not isinstance(data, dict):
        problems.append("must be a JSON object")
    else:
        cmd, timeout, cover = (data.get(k) for k in ("testCommand", "stepTimeout", "requireCoverageOver"))
        if "testCommand" in data and not (isinstance(cmd, str) and cmd.strip()):
            problems.append('"testCommand" must be a non-empty string')
        if "stepTimeout" in data and (isinstance(timeout, bool) or not isinstance(timeout, int)
                                      or not 1 <= timeout <= 3600):
            problems.append('"stepTimeout" must be a whole number of seconds from 1 to 3600')
        if "requireCoverageOver" in data and (isinstance(cover, bool) or not isinstance(cover, (int, float))
                                              or not 0 <= cover <= 100):
            problems.append('"requireCoverageOver" must be a number from 0 to 100')
    if problems:
        warnings.append("%s: %s; using detection and defaults" % (CONFIG_FILE, "; ".join(problems)))
        return cfg
    for key in sorted(set(data) - set(DEFAULT_CONFIG)):
        warnings.append("%s: unknown key %r ignored" % (CONFIG_FILE, key))
    cfg.update({k: data[k] for k in DEFAULT_CONFIG if k in data})
    return cfg


def state_path(root):
    p = (dt.git(root, "rev-parse", "--git-path", STATE_NAME) or "").strip()
    return os.path.join(root, p) if p else None


def read_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------- coverage reports

def parse_lcov(text):
    out, name, found, hit = [], None, 0, 0
    for line in text.splitlines():
        if line.startswith("SF:"):
            name, found, hit = line[3:].strip(), 0, 0
        elif line.startswith("LF:"):
            found = int(line[3:] or 0)
        elif line.startswith("LH:"):
            hit = int(line[3:] or 0)
        elif line.startswith("end_of_record") and name:
            out.append((name, 100.0 * hit / found if found else None))
            name = None
    return out


def parse_cobertura(text):
    out = []
    for chunk in text.split("<class ")[1:]:  # a split, not a lazy regex: linear even if tags are unclosed
        m = re.match(r'[^>]*\bfilename="([^"]+)"', chunk)
        if not m:
            continue
        body = chunk.split("</class>", 1)[0]
        hits = [int(h) for h in re.findall(r'<line\b[^>]*\bhits="(\d+)"', body)]
        out.append((m.group(1), 100.0 * sum(1 for h in hits if h) / len(hits) if hits else None))
    return out


def parse_istanbul(text):
    out = []
    for name, entry in (json.loads(text) or {}).items():
        statements = list((entry.get("s") or {}).values()) if isinstance(entry, dict) else []
        out.append((name, 100.0 * sum(1 for h in statements if h) / len(statements) if statements else None))
    return out


REPORT_PARSERS = (("lcov.info", parse_lcov), ("coverage.xml", parse_cobertura),
                  ("cobertura.xml", parse_cobertura), ("cobertura-coverage.xml", parse_cobertura),
                  ("coverage-final.json", parse_istanbul))


def read_coverage(root, reports, warnings):
    """(report path, [(file, percent)]) from the first report in a format it can read."""
    for rel in reports:
        parser = next((p for suffix, p in REPORT_PARSERS if rel.endswith(suffix)), None)
        if not parser:
            warnings.append("coverage report %s is in a format this script can't read" % rel)
            continue
        path = os.path.join(root, rel)
        try:
            if os.path.getsize(path) > MAX_REPORT:
                warnings.append("coverage report %s is over %d MB; not read" % (rel, MAX_REPORT >> 20))
                continue
            with open(path, "rb") as f:
                return rel, parser(f.read().decode("utf-8", "replace"))
        except (OSError, ValueError, AttributeError) as e:
            warnings.append("coverage report %s could not be read: %s" % (rel, e))
    return None, []


def percent_for(entries, target):
    """Coverage of the report entry whose path is the longest match for target."""
    best = None
    for name, percent in entries:
        n = name.replace("\\", "/")
        n = n[2:] if n.startswith("./") else n
        if n == target or n.endswith("/" + target) or target.endswith("/" + n):
            if best is None or len(n) > best[0]:
                best = (len(n), percent)
    return best[1] if best else None


# ---------------------------------------------------------------- check

def cmd_check(root, targets, warnings):
    repo = dt.Repo(root)
    try:
        raw = dt.resolve_targets(repo, targets, os.getcwd(), warnings)
    except ValueError as e:
        return fail("bad-arguments", str(e))
    except LookupError as e:
        return fail("target-not-found", str(e))
    if not raw:
        return fail("bad-arguments", "no source files found in the targets")
    if len(raw) > MAX_TARGETS:
        warnings.append("%d targets; only the first %d are checked" % (len(raw), MAX_TARGETS))
        raw = raw[:MAX_TARGETS]

    cfg = load_config(root, warnings)
    threshold = cfg["requireCoverageOver"]
    frameworks = dt.detect_frameworks(repo)
    commands = dt.commands_for(repo, frameworks)
    cov = dt.coverage(repo, frameworks)
    report, entries = read_coverage(root, cov["reports"], warnings) if cov["reports"] else (None, [])
    if threshold and not report:
        warnings.append("requireCoverageOver is %g but there is no readable coverage report" % threshold)

    test_imports = {t: dt.imports(t, repo.read(t) or "") for t in repo.tests[:dt.MAX_SCAN]}
    results, stale = [], []
    for path, symbol, line in raw:
        d = dt.describe_target(repo, path, test_imports, symbol, line)
        evidence = [{"test": t, "reason": reason}
                    for _, reason, t, strong in dt.score_tests(repo, path, test_imports) if strong][:10]
        reasons = []
        if not d["existing_tests"]:
            reasons.append("no test refers to this %s" % ("symbol" if symbol else "file"))
            verdict = "uncovered"
        else:
            verdict = "covered"
            if d["untested_symbols"]:
                reasons.append("no test mentions: " + ", ".join(d["untested_symbols"][:20]))
                verdict = "partial"
        percent = percent_for(entries, path) if report else None
        if percent is not None:
            percent = round(percent, 1)
            try:
                if os.path.getmtime(os.path.join(root, path)) > os.path.getmtime(os.path.join(root, report)):
                    stale.append(path)
            except OSError:
                pass
        if threshold and verdict != "uncovered":
            if percent is None:
                reasons.append("requireCoverageOver is %g but %s has no line coverage in %s"
                               % (threshold, path, report or "any report"))
                verdict = "partial"
            elif percent < threshold:
                reasons.append("line coverage %g%% is below requireCoverageOver %g" % (percent, threshold))
                verdict = "partial"
        item = {"path": path, "verdict": verdict, "reasons": reasons, "evidence": evidence,
                "existing_tests": d["existing_tests"], "public_symbols": d["public_symbols"],
                "untested_symbols": d["untested_symbols"], "coverage_percent": percent}
        if symbol:
            item.update(symbol=symbol, line=line)
        results.append(item)
    if stale:
        warnings.append("coverage report %s is older than: %s" % (report, ", ".join(stale[:5])))

    verdicts = {r["verdict"] for r in results}
    overall = "covered" if verdicts == {"covered"} else "uncovered" if verdicts == {"uncovered"} else "partial"

    command = None
    if cfg["testCommand"]:
        command = {"run": cfg["testCommand"], "source": CONFIG_FILE, "confidence": "configured",
                   "framework": None}
    elif commands:
        command = {k: commands[0][k] for k in ("run", "source", "confidence", "framework")}
    else:
        warnings.append("no test command detected; set testCommand in %s" % CONFIG_FILE)

    path = state_path(root)
    if not path:
        return fail("internal-error", "cannot locate the git directory for the state file")
    write_state(path, {"version": 1, "created": time.time(), "command": command,
                       "check": {"verdict": overall, "targets": [r["path"] for r in results]}})
    return {"ok": True, "error": None, "warnings": warnings, "repo_root": root, "verdict": overall,
            "targets": results, "command": command,
            "coverage": {"tools": cov["tools"], "report": report, "threshold": threshold},
            "config": cfg, "state_file": os.path.relpath(path, root)}


# ---------------------------------------------------------------- running tests

def kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)  # the command and everything it spawned
    except (OSError, subprocess.SubprocessError):
        pass
    proc.kill()


def tail(text):
    lines = text.splitlines()[-TAIL_LINES:]
    return "\n".join(l if len(l) <= LINE_CAP else l[:LINE_CAP] + " [...]" for l in lines)


def run_tests(root, command, limit):
    """{"status": "ok"|"timeout"|"no-start", "exit_code", "text", "seconds"}. No shell is used."""
    started = time.monotonic()
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return {"status": "no-start", "exit_code": None, "text": "bad command: %s" % e, "seconds": 0}
    if not argv:
        return {"status": "no-start", "exit_code": None, "text": "empty command", "seconds": 0}
    exe = os.path.join(root, argv[0]) if os.path.dirname(argv[0]) else shutil.which(argv[0])
    if not exe:
        return {"status": "no-start", "exit_code": None,
                "text": "%r was not found on PATH" % argv[0], "seconds": 0}
    extra = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    with tempfile.TemporaryFile() as out:  # a file, not a pipe: no size limit, no hang on kill
        try:
            proc = subprocess.Popen([exe] + argv[1:], cwd=root, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, **extra)
        except (OSError, ValueError) as e:
            return {"status": "no-start", "exit_code": None, "text": str(e), "seconds": 0}
        try:
            code, status = proc.wait(timeout=limit), "ok"
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.wait()
            code, status = None, "timeout"
        size = out.seek(0, os.SEEK_END)
        out.seek(max(0, size - PARSE_BYTES))
        text = ANSI.sub("", out.read().decode("utf-8", "replace"))
    return {"status": status, "exit_code": code, "text": text,
            "seconds": round(time.monotonic() - started, 2)}


# ---------------------------------------------------------------- reading test output

def unique(items):
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def counts(framework, passed=None, failed=None, skipped=None, total=None, failing=None):
    return {"framework": framework, "passed": passed, "failed": failed, "skipped": skipped,
            "total": total, "failing": None if failing is None else unique(failing)[:MAX_NAMES]}


PYTEST_SUMMARY = re.compile(r"^(?:=+ )?((?:\d+ (?:passed|failed|skipped|errors?|xfailed|xpassed|warnings?"
                            r"|deselected|rerun)(?:, )?)+)\s+in [\d.]+s", re.M)


def read_pytest(t):
    last = None
    for last in PYTEST_SUMMARY.finditer(t):
        pass
    if not last and not re.search(r"^(?:=+ )?no tests ran in [\d.]+s", t, re.M):
        return None
    n = {}
    for number, kind in re.findall(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)",
                                   last.group(1) if last else ""):
        n["error" if kind.startswith("error") else kind] = int(number)
    failed = n.get("failed", 0) + n.get("error", 0)
    skipped = n.get("skipped", 0) + n.get("xfailed", 0)
    return counts("pytest", n.get("passed", 0) + n.get("xpassed", 0), failed, skipped,
                  sum(n.values()), re.findall(r"^(?:FAILED|ERROR) (\S+)", t, re.M))


def read_unittest(t):
    m = re.search(r"^Ran (\d+) tests? in [\d.]+s", t, re.M)
    if not m:
        return None
    total = int(m.group(1))
    status = re.search(r"^(?:OK|FAILED) \(([^)]*)\)", t, re.M)
    n = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", status.group(1))} if status else {}
    failed, skipped = n.get("failures", 0) + n.get("errors", 0), n.get("skipped", 0)
    names = []
    for short, dotted in re.findall(r"^(?:FAIL|ERROR): (\w+) \(([\w.]+)\)", t, re.M):
        names.append(dotted if dotted.endswith("." + short) else dotted + "." + short)
    return counts("unittest", total - failed - skipped, failed, skipped, total, names)


def tally(line):
    return {k: int(v) for v, k in re.findall(r"(\d+) (failed|passed|skipped|todo|total)", line)}


def read_jest(t):
    m = re.search(r"^Tests:\s+(.*\d+ total.*)$", t, re.M)  # PHPUnit also prints "Tests:"
    if not m:
        return None
    n = tally(m.group(1))
    names = [x for x in re.findall(r"^[ \t]*● (.+?)\s*$", t, re.M) if not x.startswith("Console")]
    names = names or ["FAIL " + x for x in re.findall(r"^FAIL (\S+)", t, re.M)]
    return counts("jest", n.get("passed", 0), n.get("failed", 0), n.get("skipped", 0) + n.get("todo", 0),
                  n.get("total"), names)


def read_vitest(t):
    m = re.search(r"^[ \t]*Tests\s{2,}(.*)$", t, re.M)
    if not m:
        return None
    n = tally(m.group(1))
    total = re.search(r"\((\d+)\)", m.group(1))
    return counts("vitest", n.get("passed", 0), n.get("failed", 0), n.get("skipped", 0) + n.get("todo", 0),
                  int(total.group(1)) if total else None,
                  [x.strip() for x in re.findall(r"^[ \t]*(?:FAIL|×)\s+(.+?)\s*$", t, re.M)])


def read_go(t):
    if not re.search(r"^(?:ok|FAIL)\s+\S+\s+(?:[\d.]+s|\(cached\)|\[)|^[ \t]*--- (?:FAIL|PASS):", t, re.M):
        return None
    names = re.findall(r"^[ \t]*--- FAIL: (\S+)", t, re.M)
    names += [pkg + " [build failed]"
              for pkg in re.findall(r"^FAIL\s+(\S+)\s+\[(?:build|setup) failed\]", t, re.M)]
    passed = len(re.findall(r"^[ \t]*--- PASS: ", t, re.M)) or None  # only printed with -v
    return counts("go test", passed, len(names), None, None, names)


def read_cargo(t):
    results = re.findall(r"^test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", t, re.M)
    if not results:
        return None
    passed, failed, ignored = (sum(int(r[i]) for r in results) for i in range(3))
    return counts("cargo test", passed, failed, ignored, passed + failed + ignored,
                  re.findall(r"^test (\S+) \.\.\. FAILED", t, re.M))


def read_mocha(t):
    passing = re.search(r"^[ \t]*(\d+) passing", t, re.M)
    failing = re.search(r"^[ \t]*(\d+) failing", t, re.M)
    if not passing and not failing:
        return None
    pending = re.search(r"^[ \t]*(\d+) pending", t, re.M)
    p, f, s = (int(m.group(1)) if m else 0 for m in (passing, failing, pending))
    return counts("mocha", p, f, s, p + f + s,
                  [x.strip() for x in re.findall(r"^[ \t]+\d+\) (.+?):?\s*$", t, re.M)] if f else [])


def read_rspec(t):
    m = re.search(r"^(\d+) examples?, (\d+) failures?(?:, (\d+) pending)?", t, re.M)
    if not m:
        return None
    total, failed, pending = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    return counts("rspec", total - failed - pending, failed, pending, total,
                  re.findall(r"^rspec (\S+)", t, re.M))


def read_maven(t):
    rows = re.findall(r"^\[(?:INFO|WARNING|ERROR)\] Tests run: (\d+), Failures: (\d+), Errors: (\d+), "
                      r"Skipped: (\d+)\s*$", t, re.M)
    if not rows:
        return None
    total, failures, errors, skipped = (int(x) for x in rows[-1])
    failed = failures + errors
    return counts("junit", total - failed - skipped, failed, skipped, total,
                  re.findall(r"^\[ERROR\]\s{2,}([\w$.]+)", t, re.M))


def read_gradle(t):
    m = re.search(r"(\d+) tests? completed, (\d+) failed(?:, (\d+) skipped)?", t)
    names = re.findall(r"^(\S+ > .+?) FAILED\s*$", t, re.M)
    if not m and not names:
        return None
    total = int(m.group(1)) if m else None
    failed = int(m.group(2)) if m else len(names)
    skipped = int(m.group(3) or 0) if m else None
    return counts("junit", None if total is None else total - failed - (skipped or 0), failed, skipped,
                  total, names)


def read_phpunit(t):
    m = re.search(r"^Tests: (\d+), Assertions: \d+(.*)$", t, re.M)
    ok = re.search(r"^OK \((\d+) tests?,", t, re.M)
    if not m and not ok:
        return None
    if ok:
        return counts("phpunit", int(ok.group(1)), 0, 0, int(ok.group(1)), [])
    n = {k: int(v) for k, v in re.findall(r"(\w+): (\d+)", m.group(2))}
    total, failed, skipped = int(m.group(1)), n.get("Failures", 0) + n.get("Errors", 0), n.get("Skipped", 0)
    return counts("phpunit", total - failed - skipped, failed, skipped, total,
                  re.findall(r"^\d+\) (\S+)", t, re.M))


def read_dotnet(t):
    m = re.search(r"^(?:Passed|Failed)!\s+-\s+Failed:\s+(\d+), Passed:\s+(\d+), Skipped:\s+(\d+), "
                  r"Total:\s+(\d+)", t, re.M)
    if not m:
        return None
    failed, passed, skipped, total = (int(x) for x in m.groups())
    return counts("dotnet test", passed, failed, skipped, total, re.findall(r"^[ \t]+Failed (\S+)", t, re.M))


READERS = (read_pytest, read_unittest, read_jest, read_vitest, read_go, read_cargo, read_mocha,
           read_rspec, read_maven, read_gradle, read_phpunit, read_dotnet)


def read_output(text):
    """Counts and failing test names, or all None if no known runner's summary is in the output."""
    for reader in READERS:
        found = reader(text)
        if found:
            return found
    return counts(None)


# ---------------------------------------------------------------- baseline, step, reset

def hash_tests(repo):
    hashes = {}
    for rel in (repo.tests + repo.named("conftest.py"))[:MAX_HASH_FILES]:
        try:
            with open(os.path.join(repo.root, rel), "rb") as f:
                hashes[rel] = hashlib.sha256(f.read(MAX_HASH_BYTES)).hexdigest()[:16]
        except OSError:
            continue
    return hashes


def diff_stat(root):
    out = dt.git(root, "diff", "--numstat", "HEAD")
    if out is None:
        return None
    files = added = deleted = 0
    for line in out.splitlines():
        a, d = (line.split("\t") + ["", ""])[:2]
        files += 1
        added += int(a) if a.isdigit() else 0
        deleted += int(d) if d.isdigit() else 0
    untracked = [f for f in (dt.git(root, "ls-files", "-o", "--exclude-standard") or "").splitlines() if f]
    return {"files": files, "added": added, "deleted": deleted, "untracked": len(untracked)}


def summary(run, parsed):
    out = {k: parsed[k] for k in ("framework", "passed", "failed", "skipped", "total", "failing")}
    out.update(exit_code=run["exit_code"], seconds=run["seconds"])
    return out


def run_or_fail(root, command, limit, what):
    run = run_tests(root, command, limit)
    if run["status"] == "timeout":
        return None, fail("timeout", "%s didn't finish within %gs and was stopped. Last output:\n%s"
                          % (what, limit, tail(run["text"]) or "(none)"), command=command)
    if run["status"] == "no-start":
        return None, fail("command-failed", "%s couldn't start: %s" % (what, run["text"]), command=command)
    return run, None


def cmd_baseline(root, args, warnings):
    path = state_path(root)
    state = read_state(path) if path else None
    if not state or "check" not in state:
        return fail("no-check", "Run `check` on the targets first.")
    verdict = state["check"]["verdict"]
    if verdict == "uncovered":
        return fail("gate-failed", "The targets are uncovered: no test refers to them. Write "
                                   "characterization tests and run `check` again.")
    if verdict == "partial" and not args.acknowledge_partial:
        return fail("gate-failed", "The targets are only partly covered. Write the missing tests and run "
                                   "`check` again, or pass --acknowledge-partial if the user agreed to go on.")
    command = args.command or (state.get("command") or {}).get("run")
    if not command:
        return fail("no-test-command", "No test command is known. Set testCommand in %s or pass --command."
                    % CONFIG_FILE)
    cfg = load_config(root, warnings)
    run, error = run_or_fail(root, command, cfg["stepTimeout"], "The baseline run")
    if error:
        return error
    parsed = read_output(run["text"])
    green = run["exit_code"] == 0
    baseline = dict(summary(run, parsed), command=command, verdict="green" if green else "red",
                    tests=hash_tests(dt.Repo(root)), diff=diff_stat(root), at=time.time())
    state.update(baseline=baseline, steps=0, acknowledged_partial=verdict == "partial")
    write_state(path, state)
    if parsed["framework"] is None:
        warnings.append("the test output wasn't recognised, so counts and failing names are unknown; "
                        "steps are compared by exit code only")
    out = {"ok": True, "error": None, "warnings": warnings, "command": command,
           "verdict": baseline["verdict"], "acknowledged_partial": state["acknowledged_partial"],
           "baseline": {k: v for k, v in baseline.items() if k != "tests"},
           "test_files": len(baseline["tests"])}
    if not green:
        out["output_tail"] = tail(run["text"])
    return out


def cmd_step(root, warnings):
    path = state_path(root)
    state = read_state(path) if path else None
    base = (state or {}).get("baseline")
    if not base:
        return fail("no-baseline", "Run `baseline` first.")
    cfg = load_config(root, warnings)
    run, error = run_or_fail(root, base["command"], cfg["stepTimeout"], "The step run")
    if error:
        return error
    parsed = read_output(run["text"])
    now = summary(run, parsed)

    reasons, new, fixed = [], None, None
    if now["failing"] is not None and base["failing"] is not None:
        new = [n for n in now["failing"] if n not in base["failing"]]
        fixed = [n for n in base["failing"] if n not in now["failing"]]
        if new:
            reasons.append("%d new failing test(s)" % len(new))
    if base["exit_code"] == 0 and run["exit_code"] != 0 and not new:
        reasons.append("exit code %s after a green baseline" % run["exit_code"])
    if now["total"] is not None and base["total"] is not None and now["total"] < base["total"]:
        reasons.append("test count fell from %d to %d" % (base["total"], now["total"]))
    if now["failed"] is not None and base["failed"] is not None and now["failed"] > base["failed"] and not new:
        reasons.append("failures rose from %d to %d" % (base["failed"], now["failed"]))
    if parsed["framework"] is None and base["framework"] is not None:
        warnings.append("the test output wasn't recognised this time, so only the exit code was compared")

    after, before = hash_tests(dt.Repo(root)), base["tests"]
    changed = {"modified": sorted(p for p in after if p in before and after[p] != before[p]),
               "deleted": sorted(p for p in before if p not in after),
               "added": sorted(p for p in after if p not in before)}

    state["steps"] = state.get("steps", 0) + 1
    write_state(path, state)
    out = {"ok": True, "error": None, "warnings": warnings, "step": state["steps"],
           "verdict": "green" if run["exit_code"] == 0 else "red", "regressed": bool(reasons),
           "reasons": reasons, "new_failures": new, "fixed": fixed, "current": now,
           "baseline": {k: base[k] for k in ("passed", "failed", "skipped", "total", "seconds", "exit_code")},
           "tests_changed": changed, "diff": diff_stat(root), "diff_at_baseline": base.get("diff")}
    if reasons or run["exit_code"] != 0:
        out["output_tail"] = tail(run["text"])
    return out


def cmd_reset(root):
    path = state_path(root)
    removed = False
    for p in (path, (path or "") + ".tmp"):
        try:
            os.remove(p)
            removed = True
        except (OSError, TypeError):
            pass
    return {"ok": True, "error": None, "warnings": [], "removed": removed}


def main(argv):
    if dt is None:
        return fail("internal-error", "cannot load detect_tests.py: %s" % IMPORT_ERROR)
    try:
        args = parse_args(argv)
    except ValueError as e:
        return fail("bad-arguments", str(e))
    root = (dt.git(os.getcwd(), "rev-parse", "--show-toplevel") or "").strip()
    if not root:
        return fail("not-a-repo", "Not inside a git repository.")
    warnings = []
    if args.cmd == "check":
        return cmd_check(root, args.targets, warnings)
    if args.cmd == "baseline":
        return cmd_baseline(root, args, warnings)
    if args.cmd == "step":
        return cmd_step(root, warnings)
    return cmd_reset(root)


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
    except Exception as e:  # always emit one JSON object
        result = {"ok": False, "error": {"code": "internal-error", "message": str(e)},
                  "warnings": []}
    print(json.dumps(result))
    sys.exit(0)
