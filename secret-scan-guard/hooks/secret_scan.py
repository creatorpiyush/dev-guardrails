#!/usr/bin/env python3
"""PreToolUse hook that keeps secrets out of tracked files and commits.

Blocks (exit 2) file edits and `git commit`s whose new content matches a
high-confidence secret pattern, and asks before `git add` of .env files or
private keys. Files that git ignores are skipped: a gitignored .env is where
secrets belong. Any unexpected error fails open (exit 0).

Standard library only. Python 3.9+.
"""
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys

# High-confidence patterns only; no entropy guessing.
RULES = [
    ("AWS access key ID", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ("GitHub fine-grained token", r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    ("GitLab token", r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    ("Slack token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("Slack webhook", r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]+"),
    ("OpenAI/Anthropic API key", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    ("Stripe live key", r"\b[rs]k_live_[A-Za-z0-9]{20,}"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("npm token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("Private key", r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----"),
    ("JSON Web Token", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]
RULES = [(name, re.compile(rx)) for name, rx in RULES]

# Documentation placeholders such as AWS's AKIAIOSFODNN7EXAMPLE.
PLACEHOLDER = re.compile(r"example", re.I)

# `git add` of these asks first. Templates like .env.example are fine.
SENSITIVE_FILES = [".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"]
SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist")

# Optional per-repo config, relative to the repo root:
#   {"allowPaths": ["tests/fixtures/*"], "allowPatterns": ["regex", ...]}
# allowPaths are fnmatch globs on repo-relative paths (`*` also matches `/`).
# allowPatterns are regexes searched in the matched secret text.
CONFIG_FILE = os.path.join(".claude", "secret-scan.json")

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
SHELL_OPERATORS = {"&&", "||", ";", "|", "&", "\n"}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def git(cwd, *args):
    """Run git in cwd; return stdout (unstripped), or None on any failure."""
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def load_allow(root):
    try:
        with open(os.path.join(root, CONFIG_FILE), encoding="utf-8") as f:
            cfg = json.load(f)
        paths = cfg.get("allowPaths", [])
        patterns = cfg.get("allowPatterns", [])
        for val in (paths, patterns):
            if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
                raise ValueError("allow lists must be lists of strings")
        return paths, [re.compile(p) for p in patterns]
    except (OSError, ValueError, AttributeError, re.error):
        return [], []  # missing or invalid config: no allowlist


def path_allowed(rel_path, allow):
    rel_path = rel_path.replace("\\", "/")
    return any(fnmatch.fnmatch(rel_path, glob) for glob in allow[0])


def find_secrets(text, allow):
    """Return [(rule name, redacted match)] for secrets in text."""
    found = []
    for name, rx in RULES:
        for m in rx.finditer(text):
            s = m.group(0)
            if PLACEHOLDER.search(s) or any(p.search(s) for p in allow[1]):
                continue
            found.append((name, "{}... ({} chars)".format(s[:6], len(s))))
    return found


def report(findings, where):
    lines = ["secret-scan-guard: possible secret in {}:".format(where)]
    for name, redacted in findings[:10]:
        lines.append("  - {}: {}".format(name, redacted))
    lines.append(
        "Do not put secrets in tracked files. Read them from environment variables, a "
        "gitignored .env file, or a secret manager instead. If this is a false positive "
        "(a test fixture or a revoked key), tell the user; they can allowlist it in "
        ".claude/secret-scan.json. Do not try to bypass this check."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- edits


def edit_text(tool, tool_input):
    if tool == "Write":
        return tool_input.get("content") or ""
    if tool == "Edit":
        return tool_input.get("new_string") or ""
    if tool == "MultiEdit":
        return "\n".join(e.get("new_string") or "" for e in tool_input.get("edits") or [])
    return tool_input.get("new_source") or ""  # NotebookEdit


def check_edit(tool, tool_input):
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    text = edit_text(tool, tool_input)
    if not path or not text:
        return None
    d = os.path.dirname(os.path.abspath(path))
    while d and not os.path.isdir(d):  # Write may target a new directory
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    root = git(d, "rev-parse", "--show-toplevel")
    if not root:
        return None  # outside a repo: nothing gets committed
    root = root.strip()
    # Exit 0 means ignored (and untracked); `git` returns None otherwise.
    if git(d, "check-ignore", "-q", os.path.abspath(path)) is not None:
        return None
    allow = load_allow(root)
    rel = os.path.relpath(os.path.abspath(path), root)
    if path_allowed(rel, allow):
        return None
    findings = find_secrets(text, allow)
    return ("block", report(findings, rel.replace("\\", "/"))) if findings else None


# ---------------------------------------------------------------- shell parsing
# ponytail: copied from git-workflow-guard; plugins install separately and
# can't import each other. Keep the two copies in sync.


def split_segments(command):
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        return None
    segments, cur = [], []
    for tok in tokens:
        if tok in SHELL_OPERATORS or set(tok) <= set(";&|\n"):
            if cur:
                segments.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def git_invocations(command, cwd):
    segments = split_segments(command)
    if segments is None:
        return
    for seg in segments:
        while seg and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0]):
            seg = seg[1:]
        if not seg:
            continue
        if seg[0] == "cd" and len(seg) > 1:
            cwd = os.path.join(cwd, os.path.expanduser(seg[1]))
            continue
        if os.path.basename(seg[0]).lower() not in ("git", "git.exe"):
            continue
        i, here = 1, cwd
        while i < len(seg) and seg[i].startswith("-"):
            opt = seg[i]
            if opt in GIT_OPTS_WITH_VALUE and i + 1 < len(seg):
                if opt == "-C":
                    here = os.path.join(here, os.path.expanduser(seg[i + 1]))
                i += 2
            else:
                i += 1
        if i < len(seg):
            yield here, seg[i], seg[i + 1:]


# ---------------------------------------------------------------- git add / commit


def is_sensitive(path):
    name = os.path.basename(path.replace("\\", "/"))
    if name.startswith(".env") and name.endswith(SAFE_ENV_SUFFIXES):
        return False
    return any(fnmatch.fnmatch(name, glob) for glob in SENSITIVE_FILES)


def check_add(args):
    files = [a for a in args if not a.startswith("-") and is_sensitive(a)]
    if not files:
        return None
    return ("ask", "secret-scan-guard: `git add` includes files that usually hold secrets: "
                   "{}. Stage them anyway?".format(", ".join(files)))


def added_lines_by_file(diff):
    """Yield (path, added text) from a unified diff."""
    path, lines = None, []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            if path and lines:
                yield path, "\n".join(lines)
            target = line[4:]
            path = target[2:] if target.startswith("b/") else None
            lines = []
        elif line.startswith("+") and path:
            lines.append(line[1:])
    if path and lines:
        yield path, "\n".join(lines)


def commits_working_tree(args):
    for a in args:
        if a in ("-a", "--all", "-i", "--include", "-o", "--only"):
            return True
        cluster = re.match(r"^-([a-zA-Z]+)$", a)
        if cluster and "a" in re.split("[mF]", cluster.group(1), 1)[0]:
            return True  # clusters such as -am
    return False


def check_commit(cwd, args):
    root = git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return None
    root = root.strip()
    diffs = [git(cwd, "diff", "--cached", "-U0", "--no-color", "--no-ext-diff")]
    if commits_working_tree(args):
        diffs.append(git(cwd, "diff", "-U0", "--no-color", "--no-ext-diff"))
    allow = load_allow(root)
    findings, files = [], []
    for diff in diffs:
        for path, text in added_lines_by_file(diff or ""):
            if path_allowed(path, allow):
                continue
            hits = find_secrets(text, allow)
            if hits:
                files.append(path)
                findings.extend(hits)
    if not findings:
        return None
    msg = report(findings, "the changes being committed ({})".format(", ".join(sorted(set(files)))))
    return ("block", msg + "\nUnstage the file (`git restore --staged <file>`), remove the secret, "
                           "and if it was ever pushed, tell the user to rotate it.")


# ---------------------------------------------------------------- main


def evaluate(payload):
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if tool in EDIT_TOOLS:
        r = check_edit(tool, tool_input)
        return [r] if r else []
    if tool not in SHELL_TOOLS:
        return []
    command = tool_input.get("command") or ""
    if "git" not in command:
        return []
    cwd = payload.get("cwd") or os.getcwd()
    results = []
    for here, sub, args in git_invocations(command, cwd):
        if sub == "add":
            r = check_add(args)
        elif sub == "commit":
            r = check_commit(here, args)
        else:
            r = None
        if r:
            results.append(r)
    return results


def main():
    payload = json.loads(sys.stdin.read())
    results = evaluate(payload)
    blocks = [msg for kind, msg in results if kind == "block"]
    if blocks:
        sys.stderr.write("\n\n".join(blocks) + "\n")
        return 2
    asks = [msg for kind, msg in results if kind == "ask"]
    if asks:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "\n\n".join(asks),
        }}))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # fail open: never break the user's session
        code = 0
    sys.exit(code)
