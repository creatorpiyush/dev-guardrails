#!/usr/bin/env python3
"""Hook: append one JSON line per tool event to a per-session activity log.

The log lives under ~/.claude/session-recap/<repo hash>/<session>.jsonl and
holds paths, edit counts, commands, exit codes, and durations. It never holds
file contents or tool output: record() reads only the fields it names.
/session-recap:summary reads it back. Old logs are pruned on every event.

Never blocks, never prints. Any error, bad input, or write failure exits 0.
SESSION_RECAP_DISABLE=1 turns it off.

Standard library only. Python 3.9+.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECT_DIR = os.path.join(HERE, "..", "scripts")

DEFAULT_RETENTION_DAYS = 14  # other repos' logs always use this; also the import fallback
COMMAND_CAP = 500
REDACT_CAP = 8192  # redact() looks at no more than this; must stay well above COMMAND_CAP
RETENTION_MARKER = "retention"
DEFAULT_TEST_KINDS = ["pytest", "jest", "go test"]
SHELL_TOOLS = {"Bash", "PowerShell"}
EXIT_CODE = re.compile(r"Exit code (\d+)")

# -- shell parsing -----------------------------------------------------------
# Copied from git-workflow-guard/hooks/git_guard.py (git_invocations is left out).
# Plugins install separately and can't import each other;
# tests/test_copies_in_sync.py keeps them identical.

# Tokens made only of these split commands: operators, newlines, ( ) of
# subshells and $(...), and backticks.
SEPARATOR_CHARS = set(";&|\n()`")
KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}"}
# wrapper -> its options that take a separate value
WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "--user", "--group"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S"},
    "command": set(), "builtin": set(), "nohup": set(), "exec": {"-a"},
    "time": {"-f", "-o"},
    "nice": {"-n"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "xargs": {"-n", "-I", "-P", "-L", "-d", "-E", "-s", "-a"},
}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
SHELL_OPTS_WITH_VALUE = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
MAX_DEPTH = 3  # nested bash -c / eval


def split_segments(command):
    """Split a shell command into simple-command token lists. None if unparseable.

    Subshells, $(...), and backticks split commands too, so `(git push)` and
    `echo $(git push)` are seen. Each ( and ) is also kept as its own
    one-token segment, so callers can undo a `cd` made inside a subshell.
    Quoted text stays one token."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()`")
    lex.whitespace = " \t\r"  # keep newlines as command separators
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        return None
    segments, cur = [], []
    for tok in tokens:
        if tok and set(tok) <= SEPARATOR_CHARS:
            if cur:
                segments.append(cur)
            cur = []
            segments += [[c] for c in tok if c in "()"]
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def takes_separate_value(opt, opts):
    """True when wrapper option `opt` (or the last option in a cluster like
    -iu) takes the next argument as its value. -uroot carries its own."""
    if opt in opts:
        return True
    if opt.startswith("--") or len(opt) < 3:
        return False
    for k, c in enumerate(opt[1:], 1):
        if "-" + c in opts:
            return k == len(opt) - 1
    return False


def unwrap(seg):
    """Strip leading VAR=value, shell keywords, and wrappers such as sudo or env."""
    seg = list(seg)
    while seg:
        if seg[0] in KEYWORDS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0]):
            seg.pop(0)
            continue
        name = os.path.basename(seg[0])
        if name not in WRAPPERS:
            break
        seg.pop(0)
        while seg and seg[0].startswith("-") and seg[0] != "--":
            if takes_separate_value(seg.pop(0), WRAPPERS[name]) and seg:
                seg.pop(0)
        if seg and seg[0] == "--":
            seg.pop(0)
        if name == "timeout" and seg:
            seg.pop(0)  # the duration
    return seg


def nested_script(seg):
    """The script `bash -c '...'` or `eval ...` runs, else None."""
    name = os.path.basename(seg[0]).lower()
    if name == "eval":
        return " ".join(seg[1:])
    if name in SHELLS:
        k = 1  # only options before the script name: `bash run.sh -c x` passes -c to run.sh
        while k < len(seg) - 1 and seg[k].startswith(("-", "+")):
            if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", seg[k]):
                return seg[k + 1]
            k += 2 if seg[k] in SHELL_OPTS_WITH_VALUE else 1
    return None


# -- redaction ---------------------------------------------------------------
# Copied from pr-writer/scripts/collect_context.py, which copied the high-confidence
# rules from secret-scan-guard/hooks/patterns.py. Keep in sync (see CLAUDE.md).

# (the high-confidence rules, which block there)
SECRET_RULES = [
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("slack-webhook", re.compile(
        r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]+")),
    ("anthropic-api-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{32,}")),
    ("openai-api-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("stripe-live-key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}")),
    ("sendgrid-api-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("twilio-api-key", re.compile(r"\bSK[0-9a-f]{32}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")),
]

# Medium-confidence secrets that secret-scan-guard only asks about, but that never
# belong in a PR body. Only the secret part is replaced; ${VAR} and <placeholder> stay.
EXTRA_REDACTIONS = [
    ("url-credentials", re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@'\"]+:)(?![$<])[^/\s'\"]+@"),
     r"\1<redacted:url-credentials>@"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
     "<redacted:jwt>"),
]

INJECTION = re.compile(
    r"(?i)\b(ignore|disregard|forget)\b.{0,30}\b(previous|prior|above|earlier)\b.{0,20}"
    r"\b(instructions?|prompts?|rules)\b|\byou are now\b|<\s*/?\s*system\s*>")
EXCERPT_FILES, EXCERPT_LINES = 5, 80


# A private key's header rule only matches one line; cover the whole block. Without an END
# line, take the run of base64-looking lines (optionally with a diff +/-/space prefix).
KEY_BLOCK = re.compile(
    SECRET_RULES[-1][1].pattern
    + r"(?:.*?-----END (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----|(?:\r?\n[+\- ]?[A-Za-z0-9+/=]+)*)",
    re.S)


# Local rules below (not copied). A value is "double quoted", 'single quoted', or one word.
VALUE = r"(\"[^\"]*\"|'[^']*'|[^\s\"']+)"
NOT_VAR = r"(?![\"']?[$<])"  # $VAR, ${VAR}, <placeholder> stay
AUTH_HEADER = re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)?\s*)\S+")
PASSWORD_FLAG = re.compile(
    r"(?i)(--?(?:password|passwd|passphrase|pass|token|secret|api[-_]?key)(?:=|\s+))"
    + NOT_VAR + VALUE)
USER_FLAG = re.compile(r"(?i)((?:^|\s)(?:-u|--user)(?:=|\s+)[^\s:$<]+:)" + NOT_VAR + VALUE)
KEY_HEADER = re.compile(
    r"(?i)((?:x-)?(?:api[-_]?key|auth[-_]?token|access[-_]?token)\s*:\s*)" + NOT_VAR + VALUE)
# a secret-looking NAME in the name: GITHUB_TOKEN, DB_PASSWORD, OPENAI_KEY, aws_secret_access_key
SENSITIVE = (r"[\w.-]*(?:secret|token|passw(?:or)?d|pass|api[-_]?key|auth(?!orization\b)"
             r"|credential|private[-_]?key|[-_]key)[\w.-]*")
ASSIGNMENT = re.compile(r"(?i)(?<![\w.-])(" + SENSITIVE + r"(?:=|:[ \t])[ \t]*)" + NOT_VAR + VALUE)
CONFIG_SET = re.compile(r"(?i)(\bconfigure\s+set\s+" + SENSITIVE + r"\s+)" + NOT_VAR + VALUE)


def redact(command):
    """Replace secrets in a raw command string; ${VAR} and <placeholder> stay.

    Pattern-based: it can miss a secret in a shape no rule knows."""
    # ponytail: ASSIGNMENT backtracks quadratically on long name runs; the cap keeps every rule
    # well under the hook timeout. Callers keep only COMMAND_CAP chars, far below REDACT_CAP.
    command = command[:REDACT_CAP]
    command = KEY_BLOCK.sub("<redacted:private-key>", command)
    for name, rx in SECRET_RULES:
        command = rx.sub("<redacted:%s>" % name, command)
    for _name, rx, repl in EXTRA_REDACTIONS:
        command = rx.sub(repl, command)
    command = AUTH_HEADER.sub(r"\1<redacted>", command)
    for rx in (KEY_HEADER, USER_FLAG, PASSWORD_FLAG, ASSIGNMENT, CONFIG_SET):
        command = rx.sub(r"\1<redacted>", command)
    return command


# -- classification ----------------------------------------------------------

PRECEDENCE = ["test", "typecheck", "lint", "build", "install", "git", "other"]


def _rules(*rows):
    return [tuple(r.split()) for r in rows]


KIND_RULES = [
    ("test", tuple(_rules(
        "pytest", "py.test", "tox", "nox", "jest", "vitest", "mocha", "go test",
        "cargo test", "npm test", "npm run test", "yarn test", "yarn run test",
        "pnpm test", "pnpm run test", "bun test", "rspec", "bundle exec rspec",
        "bundle exec rake test", "phpunit", "dotnet test", "make test", "unittest"))),
    ("typecheck", tuple(_rules(
        "mypy", "pyright", "tsc", "npm run typecheck", "npm run type-check"))),
    ("lint", tuple(_rules(
        "ruff", "flake8", "pylint", "black --check", "eslint", "prettier --check",
        "golangci-lint", "cargo clippy", "rubocop", "npm run lint", "make lint"))),
    ("build", tuple(_rules(
        "make", "npm run build", "yarn build", "pnpm build", "cargo build", "go build",
        "docker build", "gradle", "./gradlew", "gradlew", "mvn", "python -m build"))),
    ("install", tuple(_rules(
        "pip install", "pip3 install", "uv pip install", "uv add", "uv sync",
        "poetry install", "poetry add", "npm install", "npm i", "npm ci", "yarn add",
        "yarn install", "pnpm install", "pnpm add", "go get", "cargo add",
        "bundle install", "gem install", "brew install", "apt install",
        "apt-get install"))),
    ("git", (("git",),)),
]


def _normalize(argv):
    """Drop npx / uv run / poetry run, and turn `python -m X` into X."""
    argv = list(argv)
    if argv:  # path-qualified tools: /usr/bin/python3.11, .venv/bin/pytest, tool.exe
        name = re.split(r"[\\/]", argv[0])[-1]
        name = re.sub(r"(?i)\.exe$", "", name)
        argv[0] = re.sub(r"^(python\d*)\.\d+$", r"\1", name) or argv[0]
    while argv:
        if argv[0] == "npx" or argv[:2] in (["uv", "run"], ["poetry", "run"]):
            argv = argv[1:] if argv[0] == "npx" else argv[2:]
        elif (argv[0].startswith("python") and len(argv) > 2 and argv[1] == "-m"
              and argv[2] != "build"):
            argv = argv[2:]
        else:
            break
    if argv and argv[0] in ("npm", "pnpm", "yarn", "bun"):  # test:unit, test:e2e are tests
        i = 2 if argv[1:2] == ["run"] else 1
        if len(argv) > i and argv[i].startswith("test:"):
            argv[i] = "test"
    return argv


def _kinds(command, rules, depth=0):
    """Yield (kind, integration) for every simple command in `command`."""
    segments = split_segments(command)
    if segments is None:
        segments = [command.split()]
    if depth > MAX_DEPTH:
        return
    for seg in segments:
        seg = unwrap(seg)
        if not seg:
            continue
        script = nested_script(seg)
        if script is not None:
            yield from _kinds(script, rules, depth + 1)
        argv = _normalize(seg)
        for kind, prefixes in rules:
            if any(tuple(argv[:len(p)]) == p for p in prefixes):
                integ = kind == "test" and any(
                    "integration" in a or "e2e" in a for a in seg[1:])
                yield kind, integ


def classify(command, extra_test):
    """(kind, integration) for a shell command; the highest-precedence kind wins."""
    rules = [(k, p + tuple(tuple(e.split()) for e in extra_test if e.split())
              if k == "test" else p) for k, p in KIND_RULES]
    best, integration = "other", False
    for kind, integ in _kinds(command, rules):
        if PRECEDENCE.index(kind) < PRECEDENCE.index(best):
            best, integration = kind, False
        if kind == best == "test":
            integration = integration or integ
    return best, integration



# -- paths -------------------------------------------------------------------

def repo_root(cwd):
    """Realpath of the repo's top level, or None outside a repo."""
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                           capture_output=True, text=True, timeout=5,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return os.path.realpath(out) if r.returncode == 0 and out else None


def log_dir(home, root):
    digest = hashlib.sha256(os.path.realpath(root).encode("utf-8")).hexdigest()[:16]
    return Path(home) / ".claude" / "session-recap" / digest


def safe_session(sid):
    name = re.sub(r"[^A-Za-z0-9_-]", "_", str(sid or ""))[:128]
    return name or "unknown"


def rel_path(path, root, cwd):
    """Path relative to the repo root, or absolute when outside it."""
    if not os.path.isabs(path):
        path = os.path.join(cwd or root, path)
    real = os.path.realpath(path)
    rel = os.path.relpath(real, root) if _inside(real, root) else real
    return rel.replace(os.sep, "/")


def _inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


# -- record ------------------------------------------------------------------

def record(payload, root, cfg):
    """One log row for a tool event payload, else None.

    Reads only tool_name, tool_use_id, duration_ms, tool_input.file_path /
    notebook_path / edits / command, tool_response.interrupted, error (first
    line), is_interrupt, reason (PermissionDenied, redacted). Nothing else.
    """
    name = payload.get("hook_event_name")
    event = {"PreToolUse": "pre", "PostToolUse": "post", "PostToolUseFailure": "fail",
             "PermissionDenied": "denied"}.get(name)
    if event is None:
        return None
    tool = payload.get("tool_name")
    inp = payload.get("tool_input")
    inp = inp if isinstance(inp, dict) else {}
    row = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "event": event, "tool": tool}
    if payload.get("tool_use_id"):
        row["id"] = payload["tool_use_id"]

    path = inp.get("file_path") or inp.get("notebook_path")
    if isinstance(path, str) and path:
        row["path"] = rel_path(path, root, payload.get("cwd"))
        edits = inp.get("edits")
        row["edits"] = len(edits) if isinstance(edits, list) else 1
    command = inp.get("command")
    if tool in SHELL_TOOLS and isinstance(command, str):
        row["command"] = redact(command)[:COMMAND_CAP]  # redact first: a cap can't leak a prefix
        row["kind"], integration = classify(command, cfg["testCommandKinds"])
        if integration:
            row["integration"] = True

    if event == "denied" and isinstance(payload.get("reason"), str):
        row["reason"] = redact(payload["reason"])[:COMMAND_CAP]
    if event in ("pre", "denied"):
        return row
    error = payload.get("error")
    if event == "post":
        row["exit"] = 0
    elif isinstance(error, str):
        m = EXIT_CODE.match(error.split("\n", 1)[0])
        if m:
            row["exit"] = int(m.group(1))
    if isinstance(payload.get("duration_ms"), (int, float)):
        row["duration_ms"] = payload["duration_ms"]
    resp = payload.get("tool_response")
    if payload.get("is_interrupt") is True or (
            isinstance(resp, dict) and resp.get("interrupted") is True):
        row["interrupted"] = True
    return row


# -- Stop nudge --------------------------------------------------------------

NUDGE_MIN_FILES = 3


def stop_message(rows, cfg, stop_hook_active):
    """One-time suggestion to run the recap, or None."""
    if not cfg.get("autoRecap") or stop_hook_active:
        return None
    if any(r.get("event") == "recap_suggested" for r in rows):
        return None
    n = len({r["path"] for r in rows if r.get("event") == "post" and r.get("path")})
    if n < NUDGE_MIN_FILES:
        return None
    return "This session changed %d files. Run /session-recap:summary for a recap." % n


def read_rows(f):
    rows = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                rows.append(r)
    except OSError:
        pass
    return rows


# -- retention ---------------------------------------------------------------

def dir_days(d, default):
    """Retention in days for a hash dir: its `retention` marker (1..365), else `default`."""
    try:
        n = int((Path(d) / RETENTION_MARKER).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return default
    return max(1, min(365, n))


def write_private(path, text, flags):
    fd = os.open(str(path), flags | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def prune(base, days, now, own=None, own_days=None):
    """Delete *.jsonl older than the dir's limit under base/*/, then empty hash dirs.

    Each dir's limit is its `retention` marker (written by the hook working in that
    repo), else `days`. The `own` dir (this repo's) uses `own_days` from the live
    config, so a repo's checked-in retentionDays only ever affects its own dir."""
    for d in Path(base).iterdir():
        if not d.is_dir():
            continue
        limit = own_days if own_days is not None and d == own else dir_days(d, days)
        cutoff = now - limit * 86400
        for f in d.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
        try:
            if not any(d.glob("*.jsonl")):
                (d / RETENTION_MARKER).unlink(missing_ok=True)
            d.rmdir()  # fails unless empty
        except OSError:
            pass


# -- main --------------------------------------------------------------------

def load_cfg(root):
    try:
        sys.path.insert(0, COLLECT_DIR)
        import collect_recap  # the plugin's own collector; lazy so a failure fails open
        cfg = collect_recap.load_config(root)[0]
        return {"retentionDays": cfg["retentionDays"],
                "autoRecap": cfg["autoRecap"] is True,
                "testCommandKinds": list(cfg["testCommandKinds"])}
    except Exception:
        return {"retentionDays": DEFAULT_RETENTION_DAYS, "autoRecap": False,
                "testCommandKinds": list(DEFAULT_TEST_KINDS)}


def main():
    if os.environ.get("SESSION_RECAP_DISABLE") == "1":
        return
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        return
    root = repo_root(payload.get("cwd") or os.getcwd())
    if not root:
        return
    cfg = load_cfg(root)
    days = cfg["retentionDays"]
    home = Path.home()
    d = log_dir(home, root)
    logfile = d / (safe_session(payload.get("session_id")) + ".jsonl")
    row = record(payload, root, cfg)
    msg = None
    if payload.get("hook_event_name") == "Stop":
        msg = stop_message(read_rows(logfile), cfg, payload.get("stop_hook_active") is True)
        if msg:
            row = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "event": "recap_suggested"}
    if row:
        d.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        d.mkdir(mode=0o700, exist_ok=True)
        write_private(logfile, json.dumps(row, separators=(",", ":")) + "\n", os.O_APPEND)
        if dir_days(d, None) != days:  # let other repos' hooks honor this repo's setting
            write_private(d / RETENTION_MARKER, "%d\n" % days, os.O_TRUNC)
    if msg:
        print(json.dumps({"systemMessage": msg}))
    prune(home / ".claude" / "session-recap", DEFAULT_RETENTION_DAYS, time.time(),
          log_dir(home, root), days)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # fail open: never break the user's session
        pass
    sys.exit(0)
