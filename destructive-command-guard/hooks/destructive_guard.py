#!/usr/bin/env python3
"""PreToolUse hook that stops destructive shell commands.

Blocks (exit 2) commands that are never right for an agent to run, such as
`rm -rf ~` or `mkfs`, and asks before commands that destroy data or
infrastructure but are sometimes intended, such as `git reset --hard` or
`terraform destroy`. Any unexpected error fails open (exit 0).

Standard library only. Python 3.9+.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

SHELL_TOOLS = {"Bash", "PowerShell"}
SHELL_OPERATORS = {"&&", "||", ";", "|", "&", "\n"}

# Prefixes stripped before matching. Options after them are skipped too.
WRAPPERS = {"sudo", "doas", "env", "nohup", "time", "nice", "command", "exec", "builtin"}
WRAPPER_OPTS_WITH_VALUE = {"-u", "-g", "-n", "-C"}

RM_COMMANDS = {"rm", "remove-item", "ri"}
# Per platform: on Windows a POSIX path like /usr takes the current drive.
if os.name == "nt":
    SYSTEM_DIRS = {"C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
                   "C:\\ProgramData", "C:\\Users"}
    TEMP_DIRS = [tempfile.gettempdir()]
else:
    SYSTEM_DIRS = {
        "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/opt", "/private", "/proc",
        "/root", "/sbin", "/sys", "/usr", "/var", "/System", "/Library", "/Applications",
        "/Users", "/home",
    }
    TEMP_DIRS = [tempfile.gettempdir(), "/tmp"]
DB_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "mongosh", "mongo", "sqlcmd",
              "duckdb", "clickhouse-client", "cockroach", "redis-cli"}

# (segment regex, reason) checked against each command after wrappers are stripped.
BLOCK_RULES = [
    (r"^mkfs(\.\w+)?\b", "formats a filesystem"),
    (r"^dd\b.*\bof=/dev/(sd|hd|xvd|nvme|disk|rdisk|mmcblk)", "overwrites a disk device"),
    (r"^format-volume\b", "formats a volume"),
    (r"^diskutil\s+(erase\w*|zeroDisk|randomDisk|secureErase)\b", "erases a disk"),
    (r"--no-preserve-root\b", "disables rm's protection for /"),
]
ASK_RULES = [
    # git data loss
    (r"^git\b.*\breset\b.*--hard\b", "discards uncommitted changes (git reset --hard)"),
    (r"^git\b.*\bclean\b(?!.*(\s-n\b|--dry-run)).*(\s-[a-zA-Z]*f|--force)", "deletes untracked files (git clean -f)"),
    (r"^git\b.*\bcheckout\b.*(\s--\s+\.|\s\.$)", "discards working-tree changes (git checkout .)"),
    (r"^git\b.*\brestore\b(?!.*--staged(?!.*--worktree)).*\s\.$", "discards working-tree changes (git restore .)"),
    (r"^git\b.*\bstash\s+(drop|clear)\b", "deletes stashed changes"),
    (r"^git\b.*\bbranch\b.*(\s(?-i:-[a-zA-Z]*D)\b|--delete\b.*--force|--force\b.*--delete)", "force-deletes a branch"),
    (r"^git\b.*\bpush\b.*(\s-f\b|--force|\s\+\S)", "force push rewrites remote history"),
    # framework database resets
    (r"^dropdb\b", "drops a database"),
    (r"\b(rails|rake)\s+db:(drop|reset|schema:load|purge)\b", "drops or resets the database"),
    (r"\bprisma\s+migrate\s+reset\b", "resets the database"),
    (r"--accept-data-loss\b|--force-reset\b", "accepts data loss"),
    (r"\bmanage\.py\s+(flush|reset_db)\b", "empties the database"),
    (r"\bartisan\s+(migrate:fresh|migrate:reset|db:wipe)\b", "drops all tables"),
    (r"\bsequelize(-cli)?\s+db:drop\b", "drops the database"),
    # infrastructure
    (r"^(terraform|tofu)\b.*\bdestroy\b|^(terraform|tofu)\b.*\bapply\b.*-destroy\b", "destroys infrastructure"),
    (r"^pulumi\b.*\bdestroy\b", "destroys infrastructure"),
    (r"^kubectl\b.*\bdelete\b", "deletes Kubernetes resources"),
    (r"^helm\b.*\b(uninstall|delete)\b", "uninstalls a Helm release"),
    (r"^docker\s+(system|volume|image|container|builder|network)\s+prune\b", "prunes Docker data"),
    (r"^docker\s+volume\s+rm\b", "deletes Docker volumes"),
    (r"^aws\s+s3\s+rm\b.*--recursive\b", "deletes S3 objects recursively"),
    (r"^aws\s+s3\s+rb\b", "deletes an S3 bucket"),
    (r"^gsutil\b.*\brm\b.*\s-[a-zA-Z]*r", "deletes Cloud Storage objects recursively"),
    (r"^(gcloud|az)\b.*\bdelete\b", "deletes cloud resources"),
    # files and permissions
    (r"^find\b.*(\s-delete\b|-exec\s+rm\b)", "deletes files found by find"),
    (r"^xargs\b.*\brm\b", "deletes files from piped input"),
    (r"^(chmod|chown)\b.*\s(0?777|a\+rwx|ugo\+rwx)\b", "makes files world-writable"),
]
BLOCK_RULES = [(re.compile(rx, re.I), why) for rx, why in BLOCK_RULES]
ASK_RULES = [(re.compile(rx, re.I), why) for rx, why in ASK_RULES]

# Checked against the whole command, since pipes split segments.
FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")
FETCH = r"(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b"
PIPE_TO_SHELL = re.compile(
    FETCH + r"[^|;&]*\|\s*(sudo\s+)?((ba|z|da|k|fi)?sh|python\d?|perl|ruby|node|iex|invoke-expression)\b"
    r"|(sh|bash|zsh)\s+(-c\s+)?[\"']?\$\(\s*" + FETCH +
    r"|(sh|bash|zsh|source|\.)\s+<\(\s*" + FETCH +
    r"|eval\s+[\"']?\$\(\s*" + FETCH,
    re.I,
)
SQL_RULES = [
    (re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA|COLLECTION|INDEX)\b", re.I), "drops database objects"),
    (re.compile(r"\bTRUNCATE\b", re.I), "empties a table (TRUNCATE)"),
    (re.compile(r"\.(drop|dropDatabase)\(\s*\)|\bdeleteMany\(\s*\{\s*\}\s*\)|\bFLUSHALL\b|\bFLUSHDB\b", re.I),
     "deletes a whole collection or database"),
]
DELETE_FROM = re.compile(r"\bDELETE\s+FROM\b([^;]*)", re.I)

# Optional per-repo config, relative to the repo root:
#   {"allow": ["^docker system prune"]}
# Regexes searched in each command segment. They turn an "ask" into an allow;
# blocks always stay.
CONFIG_FILE = os.path.join(".claude", "destructive-commands.json")


def git(cwd, *args):
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def load_allow(root):
    try:
        with open(os.path.join(root, CONFIG_FILE), encoding="utf-8") as f:
            allow = json.load(f).get("allow", [])
        if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
            raise ValueError("allow must be a list of strings")
        return [re.compile(a, re.I) for a in allow]
    except (OSError, ValueError, AttributeError, re.error):
        return []  # missing or invalid config: allow nothing extra


# ---------------------------------------------------------------- shell parsing
# ponytail: adapted from git-workflow-guard's split_segments; plugins install
# separately and can't import each other.


def split_segments(command, escape="\\"):
    """Split a shell command into simple-command token lists. None if unparseable."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    lex.escape = escape
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


def strip_wrappers(seg):
    seg = list(seg)
    while seg:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0]):
            seg.pop(0)
        elif os.path.basename(seg[0]).lower() in WRAPPERS:
            seg.pop(0)
            while seg and seg[0].startswith("-"):
                opt = seg.pop(0)
                if opt in WRAPPER_OPTS_WITH_VALUE and seg:
                    seg.pop(0)
        else:
            break
    return seg


# ---------------------------------------------------------------- rm / chmod targets


def real(path):
    return os.path.normcase(os.path.realpath(path))


def is_within(path, parent):
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def classify_target(target, cwd, project):
    """Return 'block', 'ask', or None for a recursive delete/chmod target."""
    target = re.sub(r"\$\{?(HOME|USERPROFILE)\}?", lambda _: os.path.expanduser("~"), target)
    if "$" in target or "`" in target or target.startswith("%"):
        return "ask"  # shell variable we can't evaluate; may expand to /
    target = os.path.expanduser(target)
    if re.search(r"[*?\[]", target):  # a glob deletes the contents of its directory
        target = re.split(r"[*?\[]", target, 1)[0] or "."
    p = real(os.path.join(cwd, target))
    home = real(os.path.expanduser("~"))
    if os.path.dirname(p) == p:
        return "block"  # filesystem root
    if any(p == real(d) for d in SYSTEM_DIRS):
        return "block"
    if is_within(home, p) or is_within(project, p):
        return "block"  # home, the project root, or one of their ancestors
    if is_within(p, project) or any(is_within(p, real(t)) for t in TEMP_DIRS):
        return None
    return "ask"  # outside the project


def is_recursive(name, args):
    if name in RM_COMMANDS:  # rm -r/-rf/-R/--recursive, Remove-Item -Recurse
        return any(a.lower() in ("-recurse", "--recursive") or
                   (re.fullmatch(r"-[rRfivdI]+", a) and "r" in a.lower()) for a in args)
    return any(a == "--recursive" or re.fullmatch(r"-[a-zA-Z]*R[a-zA-Z]*", a) for a in args)


def check_targets(seg, cwd, project):
    """Check recursive rm/chmod/chown targets; return (kind, reason) or None."""
    name = os.path.basename(seg[0]).lower()
    args = seg[1:]
    if not is_recursive(name, args):
        return None
    targets, after_dashdash = [], False
    for a in args:
        if a == "--":
            after_dashdash = True
        elif after_dashdash or not a.startswith("-"):
            targets.append(a)
    deleting = name in RM_COMMANDS
    if not deleting:
        targets = targets[1:]  # chmod/chown: first operand is the mode or owner
    result = None
    for t in targets:
        verdict = classify_target(t, cwd, project)
        if verdict == "block":
            return ("block", "recursively {} `{}`, which is the filesystem root, a system "
                             "directory, your home directory, or the project itself".format(
                                 "deletes" if deleting else "changes ownership or permissions of", t))
        if verdict == "ask" and deleting:
            result = ("ask", "recursively deletes `{}`, outside the project or through a "
                             "variable".format(t))
    return result


# ---------------------------------------------------------------- main


def evaluate(payload):
    if payload.get("tool_name") not in SHELL_TOOLS:
        return []
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command.strip():
        return []
    cwd = payload.get("cwd") or os.getcwd()
    project = real(git(cwd, "rev-parse", "--show-toplevel") or cwd)
    allow = load_allow(project)
    results = []

    if FORK_BOMB.search(command):
        return [("block", "fork bomb", command)]
    if PIPE_TO_SHELL.search(command) and not any(a.search(command) for a in allow):
        results.append(("ask", "runs a script downloaded from the internet", command))

    escape = "" if payload.get("tool_name") == "PowerShell" else "\\"
    for raw_seg in split_segments(command, escape) or []:
        if raw_seg[0] == "cd" and len(raw_seg) > 1:
            cwd = os.path.join(cwd, os.path.expanduser(raw_seg[1]))
            continue
        seg = strip_wrappers(raw_seg)
        if not seg:
            continue
        text = " ".join(seg)
        name = os.path.basename(seg[0]).lower()
        allowed = any(a.search(text) for a in allow)

        for rx, why in BLOCK_RULES:
            if rx.search(text):
                results.append(("block", why, text))
        if name in RM_COMMANDS or name in ("chmod", "chown"):
            r = check_targets(seg, cwd, project)
            if r and not (r[0] == "ask" and allowed):
                results.append((r[0], r[1], text))
        if allowed:
            continue
        for rx, why in ASK_RULES:
            if rx.search(text):
                results.append(("ask", why, text))
                break
        if name in DB_CLIENTS:
            for rx, why in SQL_RULES:
                if rx.search(command):
                    results.append(("ask", why, text))
            for m in DELETE_FROM.finditer(command):
                if not re.search(r"\bWHERE\b", m.group(1), re.I):
                    results.append(("ask", "deletes every row (DELETE without WHERE)", text))
    return results


def short(text, limit=200):
    return text if len(text) <= limit else text[:limit] + "..."


def main():
    payload = json.loads(sys.stdin.read())
    results = evaluate(payload)
    blocks = [r for r in results if r[0] == "block"]
    if blocks:
        lines = ["destructive-command-guard: blocked."]
        lines += ["- `{}`: {}".format(short(t), why) for _, why, t in blocks]
        lines.append("This is never safe for an agent to run. If the user really wants it, ask them "
                     "to run it themselves in a terminal. Do not try another way to do the same thing.")
        sys.stderr.write("\n".join(lines) + "\n")
        return 2
    if results:
        lines = ["destructive-command-guard: this command may destroy data."]
        seen = set()
        for _, why, t in results:
            if (why, t) not in seen:
                seen.add((why, t))
                lines.append("- `{}`: {}".format(short(t), why))
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "\n".join(lines),
        }}))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # fail open: never break the user's session
        code = 0
    sys.exit(code)
