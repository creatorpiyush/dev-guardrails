#!/usr/bin/env python3
"""PreToolUse hook that keeps secrets out of tracked files, commits, and pushes.

- File edits: scans only the new text. High-confidence findings block (exit 2),
  medium ones ask. Gitignored files and files outside a repo are skipped.
- `git commit`: scans lines added in the staged diff (plus unstaged tracked
  changes for -a, and the named paths for `git commit <path>`). Asks when a
  secret-looking file such as .env is staged.
- `git push`: scans lines added in the local commits about to be pushed.
- Any shell command: asks when the command line itself embeds a secret.
- `git add` of .env files or private keys asks first.

Rules live in patterns.py. Findings are always redacted, because stderr goes
into Claude's context. Any unexpected error fails open (exit 0).

`python secret_scan.py --baseline [--output PATH]` writes fingerprints of the
current findings in tracked files, so an existing repo can adopt the hook.

Standard library only. Python 3.9+.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patterns as P  # noqa: E402

DISABLE_ENV = "SECRET_SCAN_GUARD_DISABLE"
CONFIG_NAME = "secret-scan.json"
DEFAULT_BASELINE = os.path.join(".claude", "secret-scan-baseline.json")
# Shell commands that may rewrite the config or baseline. Best effort: ask.
CONFIG_MENTION = re.compile(r"secret-scan(?:-baseline)?\.json|secret_scan\.py\b.*--baseline")
MAX_READ = 5 * 1024 * 1024
DENY, ASK = "deny", "ask"

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
SHELL_OPERATORS = {"&&", "||", ";", "|", "&", "\n"}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
COMMIT_OPTS_WITH_VALUE = {
    "-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c", "--reedit-message",
    "--author", "--date", "--fixup", "--squash", "--cleanup", "-t", "--template", "--trailer",
    "--pathspec-from-file",
}
COMMIT_SHORT_WITH_VALUE = "mFCct"
PUSH_OPTS_WITH_VALUE = {"--repo", "-o", "--push-option", "--receive-pack", "--exec"}

Settings = namedtuple("Settings", "rules allow_paths allow_patterns baseline_path baseline")
Finding = namedtuple("Finding", "rule action path line preview fingerprint note")


def git(cwd, *args):
    """Run git in cwd; return stdout (unstripped), or None on any failure."""
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def repo_root(cwd):
    root = git(cwd, "rev-parse", "--show-toplevel")
    return root.strip() if root else None


def real(path):
    return os.path.normcase(os.path.realpath(path))


def user_config_path():
    return os.path.join(os.path.expanduser("~"), ".claude", CONFIG_NAME)


# ---------------------------------------------------------------- config


def string_list(cfg, key):
    val = cfg.get(key, [])
    if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
        raise ValueError("{} must be a list of strings".format(key))
    return val


def load_config(path, label, warnings):
    """Parsed config dict, or None if missing or invalid (invalid also warns)."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        warnings.append("{}: can't read {} ({}); ignoring it".format(label, path, e))
        return None
    try:
        if not isinstance(cfg, dict):
            raise ValueError("not a JSON object")
        extra = cfg.get("extraPatterns", [])
        if not isinstance(extra, list):
            raise ValueError("extraPatterns must be a list")
        rules = []
        for e in extra:
            if not (isinstance(e, dict) and isinstance(e.get("id"), str)
                    and isinstance(e.get("regex"), str) and e.get("action") in (DENY, ASK)):
                raise ValueError('each extraPatterns entry needs "id", "regex", and "action" (deny|ask)')
            rules.append(P.Rule(e["id"], re.compile(e["regex"]), P.HIGH if e["action"] == DENY else P.MEDIUM))
        baseline = cfg.get("baseline")
        if baseline is not None and not isinstance(baseline, str):
            raise ValueError("baseline must be a string")
        return {
            "allow_paths": string_list(cfg, "allowPaths"),
            "allow_patterns": [re.compile(p) for p in string_list(cfg, "allowPatterns")],
            "extra": rules,
            "baseline": baseline,
        }
    except (ValueError, TypeError, re.error) as e:
        warnings.append("{}: invalid config {} ({}); ignoring it".format(label, path, e))
        return None


def load_baseline(path, warnings):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        fps = data["fingerprints"]
        if not isinstance(fps, list) or not all(isinstance(x, str) for x in fps):
            raise ValueError("fingerprints must be a list of strings")
        return frozenset(fps)
    except FileNotFoundError:
        return frozenset()
    except (OSError, ValueError, TypeError, KeyError) as e:
        warnings.append("invalid baseline {} ({}); ignoring it".format(path, e))
        return frozenset()


def resolve(root, warnings, use_baseline=True):
    """Merge the trusted user config with the untrusted repo config.

    The user file can do anything. The repo file can only add extraPatterns
    and name a baseline; its allowPaths and allowPatterns are ignored, so a
    repo (or the agent editing it) can't allowlist its own secrets.
    """
    user_path = user_config_path()
    user = load_config(user_path, "user config", warnings)
    repo = None
    if root:
        repo_path = os.path.join(root, ".claude", CONFIG_NAME)
        if real(repo_path) != real(user_path):
            repo = load_config(repo_path, "repo config", warnings)
    rules, allow_paths, allow_patterns, baseline = list(P.RULES), [], [], None
    if user:
        rules += user["extra"]
        allow_paths, allow_patterns, baseline = user["allow_paths"], user["allow_patterns"], user["baseline"]
    if repo:
        rules += repo["extra"]
        if repo["allow_paths"] or repo["allow_patterns"]:
            warnings.append("repo config allowPaths/allowPatterns are ignored; only {} can allowlist".format(
                user_path))
        baseline = baseline or repo["baseline"]
    baseline_path = os.path.join(root, baseline or DEFAULT_BASELINE) if root else None
    fps = load_baseline(baseline_path, warnings) if use_baseline and baseline_path else frozenset()
    return Settings(rules, allow_paths, allow_patterns, baseline_path, fps)


def protected_paths(root, settings):
    paths = {real(user_config_path())}
    if root:
        paths.add(real(os.path.join(root, ".claude", CONFIG_NAME)))
        paths.add(real(settings.baseline_path))
    return paths


# ---------------------------------------------------------------- scanning


def skip_file(rel):
    name = os.path.basename(rel)
    return name in P.SKIP_NAMES or name.endswith(P.SKIP_SUFFIXES)


def path_allowed(rel, settings):
    return any(fnmatch.fnmatch(rel, glob) for glob in settings.allow_paths)


def fingerprint(path, rule_id, value):
    return hashlib.sha256("\0".join((path, rule_id, value)).encode("utf-8")).hexdigest()


def scan(lines, settings, path=None, trusted=None):
    """Return findings in the added lines.

    lines: [(line number, text, added)]; unadded lines are context only.
    trusted: stripped lines whose allow marker predates this edit. None trusts
    every marker (committed or staged content).
    """
    found = []
    for i, (n, text, added) in enumerate(lines):
        if not added:
            continue
        marker = P.ALLOW_MARKER in text
        if marker and (trusted is None or text.strip() in trusted):
            continue
        clean = P.INTEGRITY.sub("", text)
        for rule in settings.rules:
            for m in rule.regex.finditer(clean):
                value = rule.value(m, lines, i) if rule.value else m.group(0)
                if not value or P.PLACEHOLDER.search(value):
                    continue
                if any(p.search(value) for p in settings.allow_patterns):
                    continue
                fp = fingerprint(path, rule.id, value) if path else None
                if fp in settings.baseline:
                    continue
                action = DENY if rule.confidence == P.HIGH else ASK
                note = ""
                if marker:
                    action, note = ASK, "the `{}` marker on this line is new".format(P.ALLOW_MARKER)
                preview = "{}*** ({} chars)".format(value[:min(rule.show, 4)], len(value))
                found.append(Finding(rule.id, action, path, n, preview, fp, note))
    return found


def describe(f):
    where = "{}:{}".format(f.path, f.line) if f.path else "line {}".format(f.line)
    line = "  - {} at {}: {}".format(f.rule, where, f.preview)
    return line + " ({})".format(f.note) if f.note else line


def decide(findings, where, deny_advice, ask_advice):
    """Turn findings into at most one block and one ask result."""
    results = []
    for action, kind, advice in ((DENY, "block", deny_advice), (ASK, "ask", ask_advice)):
        hits = [f for f in findings if f.action == action]
        if hits:
            lines = ["secret-scan-guard: possible secret in {}:".format(where)]
            lines += [describe(f) for f in hits[:10]]
            if len(hits) > 10:
                lines.append("  ... and {} more".format(len(hits) - 10))
            results.append((kind, "\n".join(lines + [advice])))
    return results


EDIT_ADVICE = (
    "Do not put secrets in tracked files. Read them from environment variables, a gitignored "
    ".env, or a secret manager, and put a placeholder in .env.example. Do not move, split, encode, "
    "or mark the value to get past this check. If it's a false positive, tell the user; they can "
    "allowlist it in ~/.claude/secret-scan.json.")
ASK_ADVICE = "This may be a real credential. Confirm with the user before going ahead."


# ---------------------------------------------------------------- edits


def read_text(path):
    try:
        if os.path.getsize(path) > MAX_READ:
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def edit_segments(tool, tool_input, old):
    """[(line offset, new text)] for the text this tool call writes."""
    def offset(s):
        idx = old.find(s) if s else -1
        return old[:idx].count("\n") if idx > 0 else 0

    if tool == "Write":
        return [(0, tool_input.get("content") or "")]
    if tool == "Edit":
        return [(offset(tool_input.get("old_string")), tool_input.get("new_string") or "")]
    if tool == "MultiEdit":
        # ponytail: offsets come from the file before any edit applies; later edits may be a few lines off.
        return [(offset(e.get("old_string")), e.get("new_string") or "") for e in tool_input.get("edits") or []]
    return [(0, tool_input.get("new_source") or "")]  # NotebookEdit


def check_edit(tool, tool_input, warnings):
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return []
    path = os.path.abspath(path)
    protect_msg = ("secret-scan-guard: {} controls what this guard allows. "
                   "Confirm with the user before changing it.".format(path))
    if real(path) == real(user_config_path()):
        return [("ask", protect_msg)]
    d = os.path.dirname(path)
    while d and not os.path.isdir(d):  # Write may target a new directory
        parent = os.path.dirname(d)
        if parent == d:
            return []
        d = parent
    root = repo_root(d)
    if not root:
        return []  # outside a repo: nothing gets committed
    settings = resolve(root, warnings)
    if real(path) in protected_paths(root, settings):
        return [("ask", protect_msg)]
    # Exit 0 means ignored (and untracked); `git` returns None otherwise.
    if git(d, "check-ignore", "-q", path) is not None:
        return []
    rel = os.path.relpath(path, root).replace("\\", "/")
    if skip_file(rel) or path_allowed(rel, settings):
        return []
    old = read_text(path)
    trusted = {line.strip() for line in old.splitlines() if P.ALLOW_MARKER in line}
    findings = []
    for base, text in edit_segments(tool, tool_input, old):
        if not text or "\0" in text:
            continue
        lines = [(base + n + 1, t, True) for n, t in enumerate(text.splitlines())]
        findings += scan(lines, settings, rel, trusted)
    return decide(findings, rel, EDIT_ADVICE, ASK_ADVICE)


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


# ---------------------------------------------------------------- git add / commit / push


def is_sensitive(path):
    name = os.path.basename(path.replace("\\", "/"))
    if name.startswith(".env") and name.endswith(P.SAFE_ENV_SUFFIXES):
        return False
    return any(fnmatch.fnmatch(name, glob) for glob in P.SENSITIVE_FILES)


def check_add(args):
    files = [a for a in args if not a.startswith("-") and is_sensitive(a)]
    if not files:
        return []
    return [("ask", "secret-scan-guard: `git add` includes files that usually hold secrets: "
                    "{}. Stage them anyway?".format(", ".join(files)))]


def diff_files(diff):
    """Yield (path, [(line number, text, added)]) for each file section of a diff."""
    path, lines, n, in_hunk = None, [], 0, False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            if path and lines:
                yield path, lines
            path, lines, in_hunk = None, [], False
        elif raw.startswith("+++ ") and not in_hunk:
            target = raw[4:].strip('"')
            path = target[2:] if target.startswith("b/") else None  # None for /dev/null
        elif raw.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)", raw)
            n, in_hunk = (int(m.group(1)) if m else 0), True
        elif path and in_hunk and raw[:1] in ("+", " "):
            lines.append((n, raw[1:], raw[0] == "+"))
            n += 1
    if path and lines:
        yield path, lines


def scan_diffs(diffs, settings):
    findings = []
    for diff in diffs:
        for path, lines in diff_files(diff or ""):
            if not skip_file(path) and not path_allowed(path, settings):
                findings += scan(lines, settings, path)
    return findings


def commit_scope(args):
    """(commits tracked working-tree changes, explicit pathspecs) for `git commit`."""
    working, paths, i = False, [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            paths += args[i + 1:]
            break
        if a in ("-a", "--all", "-i", "--include", "-o", "--only"):
            working = True
        elif a in COMMIT_OPTS_WITH_VALUE:
            i += 1
        elif re.match(r"^-[a-zA-Z]+", a) and not a.startswith("--"):
            cluster = a[1:]
            cut = next((k for k, c in enumerate(cluster) if c in COMMIT_SHORT_WITH_VALUE), None)
            flags = cluster if cut is None else cluster[:cut]
            if "a" in flags:
                working = True
            if cut is not None and cut == len(cluster) - 1:
                i += 1  # value is the next argument (-am "msg")
        elif not a.startswith("-"):
            paths.append(a)
        i += 1
    return working, paths


DIFF_OPTS = ("-U3", "--no-color", "--no-ext-diff")


def check_commit(cwd, args, warnings):
    root = repo_root(cwd)
    if not root:
        return []
    settings = resolve(root, warnings)
    working, paths = commit_scope(args)
    diffs = [git(cwd, "diff", "--cached", *DIFF_OPTS)]
    names = [git(cwd, "diff", "--cached", "--name-only")]
    if working:
        diffs.append(git(cwd, "diff", *DIFF_OPTS))
        names.append(git(cwd, "diff", "--name-only"))
    if paths:
        diffs.append(git(cwd, "diff", *DIFF_OPTS, "--", *paths))
        names.append(git(cwd, "diff", "--name-only", "--", *paths))
    results = decide(
        scan_diffs(diffs, settings), "the changes being committed",
        "Unstage the file (`git restore --staged <file>`), move the secret to an environment variable "
        "or secret manager, and stage the fix. If it was ever pushed or shared, tell the user to rotate it.",
        ASK_ADVICE)
    staged = {n for out in names for n in (out or "").splitlines() if n}
    sensitive = sorted(n for n in staged if is_sensitive(n))
    if sensitive:
        results.append(("ask", "secret-scan-guard: this commit includes files that usually hold "
                               "secrets: {}. Commit them anyway?".format(", ".join(sensitive))))
    guarded = protected_paths(root, settings)
    if any(real(os.path.join(root, n)) in guarded for n in staged):
        results.append(("ask", "secret-scan-guard: this commit changes the secret-scan config or "
                               "baseline. Confirm with the user before committing it."))
    return results


def push_revisions(args):
    """Revisions whose unpushed commits a `git push` would send, or [] for none."""
    positionals, i = [], 0
    while i < len(args):
        a = args[i]
        if a in PUSH_OPTS_WITH_VALUE:
            i += 2
            continue
        if a in ("-d", "--delete"):
            return []
        if a in ("--all", "--branches", "--mirror"):
            return ["--branches"]
        if not a.startswith("-"):
            positionals.append(a)
        i += 1
    refspecs = positionals[1:]
    if not refspecs:
        return ["HEAD"]
    srcs = [spec.lstrip("+").partition(":")[0] for spec in refspecs]
    return [s for s in srcs if s]  # an empty src (":branch") deletes


def check_push(cwd, args, warnings):
    root = repo_root(cwd)
    revs = push_revisions(args)
    if not root or not revs:
        return []
    settings = resolve(root, warnings)
    # Commits not on any remote-tracking ref: everything local-only when there's no upstream.
    log = git(cwd, "log", "-p", *DIFF_OPTS, "--format=", *revs, "--not", "--remotes")
    return decide(
        scan_diffs([log], settings), "local commits about to be pushed",
        "Don't push. Remove the secret from those commits first (amend, or an interactive rebase "
        "the user runs; ask before rewriting history), and move it to an environment variable or "
        "secret manager. If it already left this machine another way, tell the user to rotate it.",
        ASK_ADVICE)


# ---------------------------------------------------------------- main


def check_command(command, settings):
    """Ask when the command line itself carries a secret. Never block: it's already in the transcript."""
    lines = [(n + 1, t, True) for n, t in enumerate(command.splitlines())]
    findings = [f._replace(action=ASK) for f in scan(lines, settings)]
    return decide(findings, "the command", "", (
        "The command line contains what looks like a credential. Prefer reading it from an "
        "environment variable (\"$TOKEN\") so it isn't logged. Confirm with the user before running it."))


def evaluate(payload, warnings):
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if tool in EDIT_TOOLS:
        return check_edit(tool, tool_input, warnings)
    if tool not in SHELL_TOOLS:
        return []
    command = tool_input.get("command") or ""
    cwd = payload.get("cwd") or os.getcwd()
    results = check_command(command, resolve(repo_root(cwd), warnings, use_baseline=False))
    if CONFIG_MENTION.search(command):
        results.append(("ask", "secret-scan-guard: this command may change the secret-scan config "
                               "or baseline. Confirm with the user before running it."))
    if "git" not in command:
        return results
    for here, sub, args in git_invocations(command, cwd):
        if sub == "add":
            results += check_add(args)
        elif sub == "commit":
            results += check_commit(here, args, warnings)
        elif sub == "push":
            results += check_push(here, args, warnings)
    return results


def write_baseline(argv):
    parser = argparse.ArgumentParser(
        prog="secret_scan.py", description="Record current findings in tracked files as a baseline.")
    parser.add_argument("--baseline", action="store_true", required=True)
    parser.add_argument("--output", help="baseline file (default: the configured baseline path)")
    opts = parser.parse_args(argv)
    root = repo_root(os.getcwd())
    if not root:
        sys.stderr.write("secret-scan-guard: not inside a git repository\n")
        return 1
    warnings = []
    settings = resolve(root, warnings, use_baseline=False)  # rebuild from scratch, dropping stale entries
    fps = set()
    for rel in (git(root, "ls-files", "-z") or "").split("\0"):
        if not rel or skip_file(rel) or path_allowed(rel, settings):
            continue
        text = read_text(os.path.join(root, rel))
        if "\0" in text:
            continue
        lines = [(n + 1, t, True) for n, t in enumerate(text.splitlines())]
        fps.update(f.fingerprint for f in scan(lines, settings, rel))
    out = os.path.abspath(opts.output or settings.baseline_path)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "fingerprints": sorted(fps)}, f, indent=2)
        f.write("\n")
    for w in warnings:
        sys.stderr.write("secret-scan-guard warning: " + w + "\n")
    print("secret-scan-guard: wrote {} fingerprint(s) to {}".format(len(fps), out))
    return 0


def main():
    if len(sys.argv) > 1:
        return write_baseline(sys.argv[1:])
    if os.environ.get(DISABLE_ENV) == "1":
        return 0
    payload = json.loads(sys.stdin.read())
    warnings = []
    results = evaluate(payload, warnings)
    warnings = list(dict.fromkeys(warnings))
    blocks = [msg for kind, msg in results if kind == "block"]
    if blocks:
        for w in warnings:
            sys.stderr.write("secret-scan-guard warning: " + w + "\n")
        sys.stderr.write("\n\n".join(blocks) + "\n")
        return 2
    out = {}
    asks = [msg for kind, msg in results if kind == "ask"]
    if asks:
        out["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "\n\n".join(asks),
        }
    if warnings:
        # stderr on exit 0 is never shown, so surface config problems to the user here.
        out["systemMessage"] = "secret-scan-guard: " + "; ".join(warnings)
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as e:  # argparse errors in --baseline mode
        code = e.code if isinstance(e.code, int) else 1
    except Exception:  # fail open: never break the user's session
        code = 0
    sys.exit(code)
