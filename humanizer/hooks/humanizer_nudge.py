#!/usr/bin/env python3
"""PreToolUse hook: an optional nudge before `git commit` when staged comments
and docs read like generic boilerplate.

Off unless `"commitNudge": true` in .claude/humanizer.json. Runs
`scripts/scan.py --staged` with a short time limit. When enough findings come
back, it asks the user to confirm the commit and suggests /humanizer:review.
It never blocks and never edits. Any error, timeout, or bad input allows.

Standard library only. Python 3.9+.
"""
import json
import os
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCAN = os.path.join(HERE, "..", "scripts", "scan.py")

SHELL_TOOLS = {"Bash", "PowerShell"}
SCAN_TIMEOUT = 3.0  # seconds; HUMANIZER_SCAN_TIMEOUT overrides it (a test seam)
TOP = 3

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




# ---------------------------------------------------------------- nudge


def git(cwd, *args):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def run_scan(root):
    """scan.py --staged output, or None on timeout or any failure."""
    try:
        limit = float(os.environ.get("HUMANIZER_SCAN_TIMEOUT", SCAN_TIMEOUT))
        r = subprocess.run([sys.executable, SCAN, "--staged"], cwd=root, capture_output=True,
                           text=True, encoding="utf-8", timeout=limit)
        result = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return result if isinstance(result, dict) and result.get("ok") else None


def nudge(cwd):
    """An ask message when the staged text has enough findings, else None."""
    root = git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return None
    sys.path.insert(0, os.path.dirname(SCAN))
    import scan  # the plugin's own scanner; imported here so a failure fails open
    cfg, _ = scan.load_config(root)
    if not cfg["commitNudge"]:
        return None
    result = run_scan(root)
    if not result:
        return None
    hits = [(r["file"], f) for r in result["regions"] for f in r["findings"]
            if f["severity"] in (scan.MEDIUM, scan.HIGH)]
    if len(hits) < cfg["nudgeThreshold"]:
        return None
    hits.sort(key=lambda h: -scan.WEIGHT[h[1]["severity"]])
    sev = result["summary"]["by_severity"]
    top = "\n".join("  - {}:{} {} ({}): \"{}\"".format(
        path, f["line"], f["id"], f["severity"], f["excerpt"]) for path, f in hits[:TOP])
    return (
        "humanizer: the staged comments and docs have {n} findings that read like generic "
        "boilerplate ({h} high, {m} medium, {l} low). Top {t}:\n{top}\n"
        "These are heuristics for review, not a verdict on who wrote the text.\n"
        "Allow to commit as is. To review them first, deny and run /humanizer:review --staged."
    ).format(n=len(hits) + sev["low"], h=sev["high"], m=sev["medium"], l=sev["low"],
             t=min(TOP, len(hits)), top=top)


def evaluate(payload):
    if os.environ.get("HUMANIZER_DISABLE") == "1":
        return None
    if payload.get("tool_name") not in SHELL_TOOLS:
        return None
    command = (payload.get("tool_input") or {}).get("command") or ""
    if "commit" not in command:
        return None
    cwd = payload.get("cwd") or os.getcwd()
    for here, sub, _, _ in git_invocations(command, cwd):
        if sub == "commit":
            return nudge(here)
    return None


def main():
    message = evaluate(json.loads(sys.stdin.read()))
    if message:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": message,
            "additionalContext": "humanizer flagged the staged comments and docs. The user can "
                                 "run /humanizer:review --staged. Don't rewrite anything unless "
                                 "the user asks.",
        }}))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # fail open: never break the user's session
        code = 0
    sys.exit(code)
