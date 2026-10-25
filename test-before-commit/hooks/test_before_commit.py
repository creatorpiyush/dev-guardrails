#!/usr/bin/env python3
"""PreToolUse hook that runs a repo's test and lint commands before `git commit`.

Reads the hook payload as JSON on stdin. Exits 2 (with the failing command's
output on stderr) to block a commit, prints a JSON "ask" decision to request
confirmation, or exits 0 to allow. Any unexpected error fails open (exit 0).

`test_before_commit.py --trust [dir]` approves the current repo config.

Standard library only. Python 3.9+.
"""
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

# Repo config, relative to the repo root. Its commands need one-time approval.
CONFIG_FILE = os.path.join(".claude", "test-before-commit.json")
# User config and approved repo-config hashes. Expanded at use, so tests can set HOME.
USER_CONFIG = os.path.join("~", ".claude", "test-before-commit.json")
TRUST_FILE = os.path.join("~", ".claude", "test-before-commit-trust.json")
CACHE_NAME = "test-before-commit-cache.json"  # inside the git dir

DEFAULTS = {"commands": [], "skipPatterns": ["*.md", "docs/**", "LICENSE*"], "autodetect": False}
DEFAULT_TIMEOUT = 300
BUDGET = 1700  # seconds for all commands together; hooks.json gives the hook 1800
TAIL_LINES = 60
TAIL_BYTES = 64 * 1024
LINE_CAP = 400
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}

# ---------------------------------------------------------------- shell parsing
# Copied from git-workflow-guard/hooks/git_guard.py. Plugins install separately
# and can't import each other; tests/test_copies_in_sync.py keeps them identical.

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

# git global options that take a separate value argument.
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


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


def git_invocations(command, cwd, depth=0):
    """Yield (cwd, subcommand, args, -c values) for each git call.

    Follows `cd` (undone when its subshell ends), `-C`, wrappers like sudo
    and env, and `bash -c` / `eval`."""
    segments = split_segments(command)
    if segments is None or depth > MAX_DEPTH:
        return
    saved = []  # cwd at each open subshell
    for seg in segments:
        if seg == ["("]:
            saved.append(cwd)
            continue
        if seg == [")"]:
            cwd = saved.pop() if saved else cwd
            continue
        seg = unwrap(seg)
        if not seg:
            continue
        if seg[0] == "cd" and len(seg) > 1:
            cwd = os.path.join(cwd, os.path.expanduser(seg[1]))
            continue
        script = nested_script(seg)
        if script:
            yield from git_invocations(script, cwd, depth + 1)
            continue
        if os.path.basename(seg[0]).lower() not in ("git", "git.exe"):
            continue
        i, here, config = 1, cwd, []
        while i < len(seg) and seg[i].startswith("-"):
            opt = seg[i]
            if opt in GIT_OPTS_WITH_VALUE and i + 1 < len(seg):
                if opt == "-C":
                    here = os.path.join(here, os.path.expanduser(seg[i + 1]))
                elif opt == "-c":
                    config.append(seg[i + 1])
                i += 2
            else:
                i += 1
        if i < len(seg):
            yield here, seg[i], seg[i + 1:], config


# Copied from protected-files-guard/hooks/protected_files_guard.py.
def glob_re(pattern):
    """Compile a gitignore-style glob. `**/` is zero or more directories, `/**`
    everything below, `*` and `?` never cross `/`. Case-insensitive, because
    macOS and Windows file systems are, so `.ENV` is the same file as `.env`."""
    pat = pattern.strip()
    if pat.endswith("/"):
        pat += "**"
    anchored = "/" in pat
    pat = pat.lstrip("/")
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pat.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pat[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pat[i] == "?":
            out, i = out + "[^/]", i + 1
        elif pat[i] == "[" and "]" in pat[i + 2:]:
            end = pat.index("]", i + 2)
            body = pat[i + 1:end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out, i = out + "[" + body.replace("\\", "\\\\") + "]", end + 1
        else:
            out, i = out + re.escape(pat[i]), i + 1
    if not anchored:
        out = "(?:.*/)?" + out
    return re.compile(out, re.IGNORECASE)


# ---------------------------------------------------------------- git


def git(cwd, *args):
    """Run git in cwd; return stripped stdout, or None on any failure."""
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def repo_root(cwd):
    return git(cwd, "rev-parse", "--show-toplevel")


def changed_files(root):
    """Staged and unstaged tracked changes, or None if git fails. Unstaged ones
    count because the commands run against the working tree."""
    files = set()
    for extra in (["--cached"], []):
        out = git(root, "diff", *extra, "--name-only", "-z", "--no-renames")
        if out is None:
            return None
        files.update(f for f in out.split("\0") if f)
    return files


# ---------------------------------------------------------------- config


def check_command(c):
    if not isinstance(c, dict):
        raise ValueError("each command must be an object")
    if not isinstance(c.get("name"), str) or not c["name"].strip():
        raise ValueError("each command needs a non-empty \"name\"")
    run = c.get("run")
    if not (isinstance(run, str) and run.strip()) and not (
            isinstance(run, list) and run and all(isinstance(a, str) for a in run)):
        raise ValueError("{!r}: \"run\" must be a string or a list of strings".format(c["name"]))
    timeout = c.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("{!r}: \"timeout\" must be a positive number of seconds".format(c["name"]))
    if not isinstance(c.get("shell", False), bool):
        raise ValueError("{!r}: \"shell\" must be true or false".format(c["name"]))
    if c.get("shell") and not isinstance(run, str):
        raise ValueError("{!r}: \"run\" must be a string when \"shell\" is true".format(c["name"]))


def validate(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("the config must be a JSON object")
    if "commands" in cfg:
        if not isinstance(cfg["commands"], list):
            raise ValueError("\"commands\" must be a list")
        for c in cfg["commands"]:
            check_command(c)
    pats = cfg.get("skipPatterns", [])
    if not isinstance(pats, list) or not all(isinstance(p, str) for p in pats):
        raise ValueError("\"skipPatterns\" must be a list of strings")
    if not isinstance(cfg.get("autodetect", False), bool):
        raise ValueError("\"autodetect\" must be true or false")


def read_config(path):
    """Return (config, raw text, warning). Config is None if missing or invalid."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return None, None, None
    except (OSError, ValueError) as e:
        return None, None, "test-before-commit: can't read {}: {}".format(path, e)
    try:
        cfg = json.loads(text)
        validate(cfg)
    except ValueError as e:
        return None, None, "test-before-commit: ignoring invalid {}: {}".format(path, e)
    return cfg, text, None


def autodetect(root):
    """Commands inferred from well-known project files. Conservative on purpose."""
    found = []
    try:
        with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
            scripts = json.load(f).get("scripts")
    except (OSError, ValueError, AttributeError):
        scripts = None
    if isinstance(scripts, dict):
        if isinstance(scripts.get("lint"), str):
            found.append({"name": "lint", "run": "npm run lint"})
        test = scripts.get("test")
        if isinstance(test, str) and "no test specified" not in test:  # npm init's stub
            found.append({"name": "test", "run": "npm test"})
    try:
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as f:
            pytest_section = "[tool.pytest" in f.read()
    except (OSError, ValueError):
        pytest_section = False
    if pytest_section or os.path.isfile(os.path.join(root, "pytest.ini")):
        found.append({"name": "pytest", "run": "pytest -q"})
    try:
        with open(os.path.join(root, "pubspec.yaml"), encoding="utf-8") as f:
            pubspec = f.read()
    except (OSError, ValueError):
        pubspec = None
    if pubspec is not None:
        fvm = "fvm " if any(os.path.isfile(os.path.join(root, f))
                            for f in (".fvmrc", os.path.join(".fvm", "fvm_config.json"))) else ""
        if re.search(r"^\s*(?:flutter_test:|sdk:\s*flutter\b)", pubspec, re.M):
            found.append({"name": "flutter test", "run": fvm + "flutter test"})
        elif re.search(r"^\s*test:", pubspec, re.M):
            found.append({"name": "dart test", "run": fvm + "dart test"})
    if os.path.isfile(os.path.join(root, "Cargo.toml")):
        found.append({"name": "cargo test", "run": "cargo test"})
    if os.path.isfile(os.path.join(root, "go.mod")):
        found.append({"name": "go test", "run": "go test ./..."})
    return found


def load_settings(root):
    """Return (settings, warnings); settings is None when no config exists.

    Repo keys override user keys. "commands" wins over "autodetect"."""
    user, _, user_warn = read_config(os.path.expanduser(USER_CONFIG))
    repo, repo_text, repo_warn = read_config(os.path.join(root, CONFIG_FILE))
    warnings = [w for w in (user_warn, repo_warn) if w]
    if user is None and repo is None:
        return None, warnings
    merged = dict(DEFAULTS)
    merged.update(user or {})
    merged.update(repo or {})
    if "commands" in (user or {}) or "commands" in (repo or {}):
        raw = merged["commands"]
    else:
        raw = autodetect(root) if merged["autodetect"] else []
    commands = [{"name": c["name"], "run": c["run"], "shell": c.get("shell", False),
                 "timeout": c.get("timeout", DEFAULT_TIMEOUT)} for c in raw]
    digest = None
    if repo_text is not None:  # what the user approves: the file plus what it resolves to
        digest = sha256(repo_text.encode("utf-8"), json.dumps(commands, sort_keys=True).encode())
    return {"commands": commands, "skipPatterns": merged["skipPatterns"],
            "repo": repo_text is not None, "digest": digest}, warnings


def sha256(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(p + b"\0")
    return h.hexdigest()


def run_text(c):
    return c["run"] if isinstance(c["run"], str) else " ".join(shlex.quote(a) for a in c["run"])


def describe(commands):
    return "\n".join("  - {}: {}{} (timeout {}s)".format(
        c["name"], run_text(c), " [shell]" if c["shell"] else "", c["timeout"]) for c in commands)


# ---------------------------------------------------------------- trust


def trust_key(root):
    return os.path.normcase(os.path.realpath(root))


def load_trust():
    try:
        with open(os.path.expanduser(TRUST_FILE), encoding="utf-8") as f:
            store = json.load(f)
    except (OSError, ValueError):
        return {}
    return store if isinstance(store, dict) else {}


def trust_cli(path):
    """Record the current repo config's hash as approved. Returns an exit code."""
    root = repo_root(path)
    if not root:
        print("test-before-commit: {} is not inside a git repository.".format(path))
        return 1
    settings, warnings = load_settings(root)
    for w in warnings:
        print(w)
    if not settings or not settings["repo"]:
        print("test-before-commit: no valid {} in {}; nothing to trust.".format(CONFIG_FILE, root))
        return 1
    store = load_trust()
    store[trust_key(root)] = settings["digest"]
    path = os.path.expanduser(TRUST_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    print("test-before-commit: approved {} for {}. These run before each commit:\n{}".format(
        CONFIG_FILE, root, describe(settings["commands"]) or "  (no commands)"))
    return 0


# ---------------------------------------------------------------- running


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


def tail(data, truncated):
    lines = ANSI.sub("", data.decode("utf-8", "replace")).splitlines()
    if truncated and lines:
        lines = lines[1:]  # the first line was cut mid-way
    lines = lines[-TAIL_LINES:]
    return "\n".join(l if len(l) <= LINE_CAP else l[:LINE_CAP] + " [...]" for l in lines)


def run_command(root, c, limit):
    """Return (exit code, output tail, timed out). Exit code is None if it didn't start."""
    shell = c["shell"]
    argv = c["run"]
    if not shell:
        argv = shlex.split(argv) if isinstance(argv, str) else list(argv)
        exe = os.path.join(root, argv[0]) if os.path.dirname(argv[0]) else shutil.which(argv[0])
        if not exe:
            return None, "{!r} was not found on PATH".format(argv[0]), False
        argv = [exe] + argv[1:]
    extra = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    with tempfile.TemporaryFile() as out:  # a file, not a pipe: no size limit, no hang on kill
        try:
            proc = subprocess.Popen(argv, cwd=root, shell=shell, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, **extra)
        except (OSError, ValueError) as e:
            return None, str(e), False
        try:
            code, timed_out = proc.wait(timeout=limit), False
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.wait()
            code, timed_out = None, True
        size = out.seek(0, os.SEEK_END)
        out.seek(max(0, size - TAIL_BYTES))
        return code, tail(out.read(), size > TAIL_BYTES), timed_out


def run_all(root, commands):
    """Run commands in order. None if all pass, else a (kind, message) result."""
    deadline = time.monotonic() + BUDGET
    for c in commands:
        limit = min(c["timeout"], deadline - time.monotonic())
        if limit <= 0:
            return ("ask", "test-before-commit: ran out of time before {!r}; all checks together "
                           "get {}s. Allow to commit without it?".format(c["name"], BUDGET))
        code, output, timed_out = run_command(root, c, limit)
        if timed_out:
            return ("ask", (
                "test-before-commit: {n!r} ({r}) didn't finish within {t:g}s and was stopped.\n"
                "Last output:\n{o}\n\n"
                "Allow to commit without the remaining checks. Deny to look into the slow command "
                "first, or raise its \"timeout\" in the config."
            ).format(n=c["name"], r=run_text(c), t=limit, o=output or "(none)"))
        if code is None:
            return ("ask", "test-before-commit: {!r} ({}) couldn't start: {}\n"
                           "Allow to commit without it?".format(c["name"], run_text(c), output))
        if code != 0:
            return ("block", (
                "test-before-commit: {n!r} failed with exit code {c}, so this commit is blocked.\n"
                "Command: {r}\n"
                "Last {k} lines of output:\n{o}\n\n"
                "Read the output, fix the cause in the code, stage the fix with `git add`, and "
                "retry the same commit. Don't skip, weaken, or delete tests, use --no-verify, or "
                "edit {f} to get past this. If the check itself looks wrong, ask the user."
            ).format(n=c["name"], c=code, r=run_text(c), k=TAIL_LINES, o=output or "(none)",
                     f=CONFIG_FILE))
    return None


# ---------------------------------------------------------------- cache


def cache_path(root):
    p = git(root, "rev-parse", "--git-path", CACHE_NAME)
    return os.path.join(root, p) if p else None


def cache_key(root, settings):
    """Hash of the index tree, unstaged tracked changes, and the commands."""
    tree = git(root, "write-tree")
    if not tree:
        return None
    try:
        diff = subprocess.run(["git", "diff", "--binary", "--no-ext-diff", "--no-textconv",
                               "--no-color"], cwd=root, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if diff.returncode != 0:
        return None
    return sha256(tree.encode(), json.dumps(settings["commands"], sort_keys=True).encode(),
                  diff.stdout)


def cache_hit(root, key):
    try:
        with open(cache_path(root), encoding="utf-8") as f:
            return json.load(f).get("pass") == key
    except (OSError, TypeError, ValueError, AttributeError):
        return False


def cache_store(root, key):
    try:
        with open(cache_path(root), "w", encoding="utf-8") as f:
            json.dump({"pass": key}, f)
    except (OSError, TypeError):
        pass


# ---------------------------------------------------------------- checks


def approval_ask(root, settings, changed):
    return ("test-before-commit: {f} in {root} wants to run these commands before each commit, "
            "and you haven't approved {what}:\n{cmds}\n"
            "Commands from a repo config can run anything on your machine.\n"
            "Allow: commit this once without running them. To approve them, deny, run "
            "/test-before-commit:trust, and retry the commit."
            ).format(f=CONFIG_FILE, root=root, cmds=describe(settings["commands"]),
                     what="this version (it changed)" if changed else "them yet")


def check_commit(cwd, args):
    root = repo_root(cwd)
    if not root or ("--amend" in args and "--no-edit" in args):
        return []
    if git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD"):
        return []  # concluding a merge
    settings, warnings = load_settings(root)
    results = [("warn", w) for w in warnings]
    if not settings or not settings["commands"]:
        return results
    if settings["repo"]:
        approved = load_trust().get(trust_key(root))
        if approved != settings["digest"]:
            return results + [("ask", approval_ask(root, settings, approved is not None))]
    files = changed_files(root)
    patterns = [glob_re(p) for p in settings["skipPatterns"]]
    if files is not None and all(any(p.fullmatch(f) for p in patterns) for f in files):
        return results  # docs-only (or nothing to commit)
    key = cache_key(root, settings)
    if key and cache_hit(root, key):
        return results
    outcome = run_all(root, settings["commands"])
    if outcome:
        return results + [outcome]
    if key:
        cache_store(root, key)
    return results


def mentions_config(command):
    return ("test-before-commit-trust" in command or "test-before-commit.json" in command
            or ("test_before_commit" in command and "--trust" in command))


def config_ask(cwd):
    msg = ("test-before-commit: this command touches the approval store or a config that "
           "decides which commands run before each commit.")
    root = repo_root(cwd)
    settings = load_settings(root)[0] if root else None
    if settings and settings["repo"]:
        msg += " {} in {} currently runs:\n{}".format(
            CONFIG_FILE, root, describe(settings["commands"]) or "  (no commands)")
    return msg + "\nAllow only if you want these commands approved or changed."


def check_edit(tool_input):
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return []
    target = os.path.normcase(os.path.realpath(path))
    for name in (USER_CONFIG, TRUST_FILE):
        if target == os.path.normcase(os.path.realpath(os.path.expanduser(name))):
            return [("ask", "test-before-commit: {} decides which commands run before each "
                            "commit without approval. Allow this edit?".format(name))]
    return []


# ---------------------------------------------------------------- main


def evaluate(payload):
    if os.environ.get("TEST_BEFORE_COMMIT_DISABLE") == "1":
        return []
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if tool in EDIT_TOOLS:
        return check_edit(tool_input)
    if tool not in SHELL_TOOLS:
        return []
    command = tool_input.get("command") or ""
    cwd = payload.get("cwd") or os.getcwd()
    results = [("ask", config_ask(cwd))] if mentions_config(command) else []
    if "commit" in command:
        for here, sub, args, _ in git_invocations(command, cwd):
            if sub == "commit":
                results += check_commit(here, args)
    return results


def main():
    payload = json.loads(sys.stdin.read())
    results = evaluate(payload)
    warns = [msg for kind, msg in results if kind == "warn"]
    blocks = [msg for kind, msg in results if kind == "block"]
    if blocks:
        sys.stderr.write("\n\n".join(blocks + warns) + "\n")
        return 2
    asks = [msg for kind, msg in results if kind == "ask"]
    out = {}
    if asks:
        out["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "\n\n".join(asks),
        }
    if warns:  # stderr isn't shown on exit 0, so warnings also go to systemMessage
        sys.stderr.write("\n".join(warns) + "\n")
        out["systemMessage"] = "\n".join(warns)
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--trust"]:
        sys.exit(trust_cli(sys.argv[2] if len(sys.argv) > 2 else os.getcwd()))
    try:
        code = main()
    except Exception:  # fail open: never break the user's session
        code = 0
    sys.exit(code)
