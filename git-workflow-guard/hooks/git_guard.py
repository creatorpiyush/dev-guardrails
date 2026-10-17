#!/usr/bin/env python3
"""PreToolUse hook that enforces a branch / commit / push workflow.

Reads the hook payload as JSON on stdin. Exits 2 (with a reason on stderr) to
block a tool call, prints a JSON "ask" decision to request confirmation, or
exits 0 to allow. Any unexpected error fails open (exit 0).

Standard library only. Python 3.9+.
"""
import json
import os
import re
import shlex
import subprocess
import sys

# Branches that must never be edited, committed to, or pushed to directly.
# The remote default branch (origin/HEAD) is always added at runtime.
PROTECTED = ["main", "master", "develop", "dev", "trunk", "staging", "production"]
PROTECTED_PREFIXES = ["release/", "hotfix/"]

# Optional per-repo override, relative to the repo root:
#   {"protected": ["main", ...], "protectedPrefixes": ["release/", ...]}
# A key that is present replaces the matching default list.
CONFIG_FILE = os.path.join(".claude", "git-guard.json")

TYPES = "feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert"
CONVENTIONAL = re.compile(r"^(" + TYPES + r")(\([^()\s]+\))?!?: \S.*$")
MAX_SUBJECT = 72  # longer subjects ask instead of block: the user may want one
BRANCH_NAME = re.compile(r"^(" + TYPES + r")/[a-z0-9][a-z0-9.-]*$")

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
# Tokens made only of these split commands: operators, newlines, ( ) of
# subshells and $(...), and backticks.
SEPARATOR_CHARS = set(";&|\n()`")
KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}"}
# wrapper -> its options that take a separate value
WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S"},
    "command": set(), "builtin": set(), "nohup": set(), "exec": {"-a"},
    "time": {"-f", "-o"},
    "nice": {"-n"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "xargs": {"-n", "-I", "-P", "-L", "-d", "-E", "-s", "-a"},
}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
MAX_DEPTH = 3  # nested bash -c / eval

# git global options that take a separate value argument.
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
PUSH_OPTS_WITH_VALUE = {"--repo", "-o", "--push-option", "--receive-pack", "--exec"}
# git commit short options: these take a value (-m msg, -F file, -C/-c commit,
# -t template), and -S[keyid] / -u[mode] take only an attached one.
COMMIT_VALUE_LETTERS = "mFCct"
COMMIT_ATTACHED_LETTERS = "Su"
COMMIT_LONG_WITH_VALUE = {
    "--message", "--file", "--author", "--date", "--template", "--reuse-message",
    "--reedit-message", "--fixup", "--squash", "--cleanup", "--trailer",
}


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


def current_branch(cwd):
    """Branch name, or None when detached or not a repo."""
    return git(cwd, "symbolic-ref", "--short", "-q", "HEAD") or None


def load_rules(root):
    protected, prefixes = list(PROTECTED), list(PROTECTED_PREFIXES)
    try:
        with open(os.path.join(root, CONFIG_FILE), encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("config must be an object")
        new_protected, new_prefixes = protected, prefixes
        for key in ("protected", "protectedPrefixes"):
            if key in cfg:
                val = cfg[key]
                if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
                    raise ValueError(key + " must be a list of strings")
                if key == "protected":
                    new_protected = val
                else:
                    new_prefixes = val
        protected, prefixes = new_protected, new_prefixes
    except (OSError, ValueError):
        pass  # missing or invalid config: keep defaults
    default = git(root, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
    if default and default.startswith("origin/"):
        protected = protected + [default[len("origin/"):]]
    return set(protected), tuple(prefixes)


def is_protected(branch, rules):
    names, prefixes = rules
    return branch in names or branch.startswith(prefixes)


def describe(rules):
    names, prefixes = rules
    return ", ".join(sorted(names) + [p + "*" for p in prefixes])


# ---------------------------------------------------------------- edits


CONFIG_ASK = ("git-workflow-guard: {} sets which branches are protected. "
              "Confirm with the user before changing it.")


def real(path):
    return os.path.normcase(os.path.realpath(path))


def check_edit(tool_input):
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return None
    d = os.path.dirname(os.path.abspath(path))
    while d and not os.path.isdir(d):  # Write may target a new directory
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    root = repo_root(d)
    if not root:
        return None
    branch = current_branch(d)
    rules = load_rules(root)
    if not branch or not is_protected(branch, rules):
        if real(path) == real(os.path.join(root, CONFIG_FILE)):
            return ("ask", CONFIG_ASK.format(CONFIG_FILE))
        return None
    return ("block", (
        "git-workflow-guard: '{b}' is a protected branch ({p}); editing files on it is blocked.\n"
        "Do this before retrying:\n"
        "1. Propose a branch name as <type>/<short-kebab-description> (type is one of "
        "feat, fix, docs, refactor, test, chore, ...), following any convention visible in "
        "`git branch -a` or CONTRIBUTING.md, plus one or two alternatives.\n"
        "2. Ask the user to confirm or pick a name, and wait for the answer.\n"
        "3. Run `git switch -c <name>`, then retry this edit.\n"
        "Do not try to bypass this check."
    ).format(b=branch, p=describe(rules)))


# ---------------------------------------------------------------- shell parsing


def split_segments(command):
    """Split a shell command into simple-command token lists. None if unparseable.

    Subshells, $(...), and backticks split commands too, so `(git push)` and
    `echo $(git push)` are seen. Quoted text stays one token."""
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
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


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
            if seg.pop(0) in WRAPPERS[name] and seg:
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
        for k in range(1, len(seg) - 1):
            if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", seg[k]):
                return seg[k + 1]
    return None


def git_invocations(command, cwd, depth=0):
    """Yield (cwd, subcommand, args, -c values) for each git call.

    Follows `cd`, `-C`, wrappers like sudo and env, and `bash -c` / `eval`."""
    segments = split_segments(command)
    if segments is None or depth > MAX_DEPTH:
        return
    for seg in segments:
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


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\s*(?:\n|$|\))", re.S)


def commit_cluster(a):
    """Split a short-option cluster such as -am into (flags, value letter, attached value).

    The value letter is the first option that takes a value (or None), so
    `-nm` is (n, m, "") and `-unormal` is ("", u, "normal"). None for non-clusters."""
    if not re.match(r"^-[a-zA-Z]", a):
        return None
    body = a[1:]
    for k, c in enumerate(body):
        if c in COMMIT_VALUE_LETTERS or c in COMMIT_ATTACHED_LETTERS:
            return body[:k], c, body[k + 1:]
    return body, None, ""


def skips_hooks(args, config):
    """True for --no-verify, -n in any cluster, or -c core.hooksPath=..."""
    if any(c.lower().startswith("core.hookspath") for c in config):
        return True
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a == "--":
            break
        elif a == "--no-verify":
            return True
        elif a in COMMIT_LONG_WITH_VALUE:
            skip = True
        else:
            cluster = commit_cluster(a)
            if cluster and "n" in cluster[0]:
                return True
            skip = bool(cluster and cluster[1] in COMMIT_VALUE_LETTERS and not cluster[2])
    return False


def commit_message(args, raw_command):
    """Return the commit subject source: the message, '' if none, None if unparseable."""
    for i, a in enumerate(args):
        if a == "--":
            break
        nxt = args[i + 1] if i + 1 < len(args) else None
        cluster = commit_cluster(a)  # -m, -am, -mmsg, -F
        if a in ("--message", "--file"):
            val, is_file = nxt, a == "--file"
        elif a.startswith(("--message=", "--file=")):
            val, is_file = a.split("=", 1)[1], a.startswith("--file=")
        elif cluster and cluster[1] in ("m", "F"):
            val, is_file = cluster[2] or nxt, cluster[1] == "F"
        else:
            continue
        if val is None:
            return None
        if is_file and val != "-":
            return None  # message in a file we don't read
        source = raw_command if is_file else val
        if is_file or "<<" in val:
            m = HEREDOC.search(source)
            return m.group(3) if m else None
        if "$" in val or "`" in val:
            return None  # shell expansion we can't evaluate
        return val  # the first -m holds the subject
    return ""


# ---------------------------------------------------------------- commit / push


def check_commit(cwd, args, raw, config=()):
    root = repo_root(cwd)
    branch = current_branch(cwd) if root else None
    if not root:
        return None
    rules = load_rules(root)
    if branch and is_protected(branch, rules):
        return ("block", (
            "git-workflow-guard: committing on protected branch '{b}' is blocked ({p}).\n"
            "Propose a <type>/<short-kebab-description> branch name plus alternatives, ask the "
            "user to confirm, wait, then `git switch -c <name>` (uncommitted changes move with "
            "you) and commit there."
        ).format(b=branch, p=describe(rules)))

    asks = []
    msg = commit_message(args, raw)
    if msg:
        subject = msg.strip().splitlines()[0].strip() if msg.strip() else ""
        if not CONVENTIONAL.match(subject):
            return ("block", (
                "git-workflow-guard: commit message rejected: subject is not a Conventional Commit.\n"
                "Subject was: {s!r}\n"
                "Required: <type>(<optional scope>)!: <description>, where type is {t}. "
                "Keep it to {n} chars unless the user wants it longer.\n"
                "Example: feat(auth): add token refresh on 401\n"
                "Draft a compliant message, show it to the user, and commit after they approve."
            ).format(s=subject, t=TYPES, n=MAX_SUBJECT))
        if len(subject) > MAX_SUBJECT:
            asks.append((
                "git-workflow-guard: the commit subject is {l} chars, over the {n}-char "
                "convention:\n  {s}\n"
                "Allow to keep this subject. Deny to have Claude shorten it and move the "
                "detail into the body."
            ).format(l=len(subject), n=MAX_SUBJECT, s=subject))

    if skips_hooks(args, config):
        asks.append("git-workflow-guard: this commit uses --no-verify (or overrides "
                    "core.hooksPath) and skips the repo's git hooks. Allow it?")
    return ("ask", "\n\n".join(asks)) if asks else None


# ---------------------------------------------------------------- discarded work


def has_short_flag(args, letter):
    return any(re.fullmatch(r"-[a-zA-Z]*" + letter + r"[a-zA-Z]*", a) for a in args)


def check_discard(sub, args):
    """Ask before git commands that throw away uncommitted work or unmerged commits."""
    what = None
    if sub == "reset" and "--hard" in args:
        what = "`git reset --hard` discards every uncommitted change in tracked files"
    elif sub == "clean" and ("--force" in args or has_short_flag(args, "f")) \
            and not ("--dry-run" in args or has_short_flag(args, "n")):
        what = "`git clean` deletes untracked files{}".format(
            ", including ignored ones" if has_short_flag(args, "x") else "")
    elif sub == "branch" and (has_short_flag(args, "D") or (
            ("-d" in args or "--delete" in args) and ("-f" in args or "--force" in args))):
        what = "`git branch -D` deletes a branch even if it isn't merged"
    elif sub == "stash" and args[:1] in (["drop"], ["clear"]):
        what = "`git stash {}` deletes stashed changes".format(args[0])
    elif sub == "checkout" and (("--" in args and args[-1] != "--") or "." in args):
        what = "`git checkout -- <path>` overwrites uncommitted changes in those files"
    elif sub == "restore" and not (("--staged" in args or "-S" in args)
                                   and not ("--worktree" in args or "-W" in args)):
        what = "`git restore` overwrites uncommitted changes in those files"
    if not what:
        return None
    return ("ask", "git-workflow-guard: {}, and git can't bring that work back. "
                   "Check `git status` first. Allow it?".format(what))


# ---------------------------------------------------------------- new branches


CREATE_FLAGS = {"switch": ("-c", "-C", "--create", "--force-create"), "checkout": ("-b", "-B")}


def new_branch_name(sub, args):
    """Name of the branch `git switch -c`, `checkout -b`, or `branch <name>` creates."""
    if sub == "branch":  # any option means list/delete/move/etc., not a plain create
        return args[0] if args and not any(a.startswith("-") for a in args) else None
    flags = CREATE_FLAGS.get(sub, ())
    for i, a in enumerate(args):
        if a in flags and i + 1 < len(args):
            return args[i + 1]
        opt, eq, val = a.partition("=")
        if eq and opt in flags and opt.startswith("--"):
            return val
    return None


def check_branch_name(name):
    if BRANCH_NAME.match(name):
        return None
    return ("ask", (
        "git-workflow-guard: new branch {b!r} doesn't follow <type>/<short-kebab-description> "
        "(type is {t}), e.g. feat/csv-export.\n"
        "Allow to keep this name. Deny to have Claude propose names that fit."
    ).format(b=name, t=TYPES))


def push_targets(cwd, args):
    """Return (target branch names, warnings) for a git push."""
    positionals, warnings, delete = [], [], False
    i = 0
    while i < len(args):
        a = args[i]
        if a in PUSH_OPTS_WITH_VALUE:
            i += 2
            continue
        if a in ("-f", "--force") or a.startswith(("--force-with-lease", "--force-if-includes")):
            warnings.append("force push ({})".format(a))
        elif a in ("--all", "--branches", "--mirror"):
            warnings.append("{} pushes every branch, protected ones included".format(a))
        elif a in ("-d", "--delete"):
            delete = True
        elif not a.startswith("-"):
            positionals.append(a)
        i += 1

    branch = current_branch(cwd)
    targets = set()
    refspecs = positionals[1:]
    for spec in refspecs:
        if spec.startswith("+"):
            warnings.append("force push (+{})".format(spec[1:]))
            spec = spec[1:]
        src, _, dst = spec.partition(":")
        dst = dst or src
        if dst == "HEAD" and branch:
            dst = branch
        if dst.startswith("refs/heads/"):
            dst = dst[len("refs/heads/"):]
        if dst and not dst.startswith("refs/tags/"):
            targets.add(dst)
    if not refspecs and not delete and branch:
        targets.add(branch)  # implicit: the current branch...
        upstream = git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if upstream and "/" in upstream:
            targets.add(upstream.split("/", 1)[1])  # ...and wherever it tracks
    return targets, warnings


GUIDELINES = [
    ("CONTRIBUTING.md", ["CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"]),
    ("PR/MR template", [
        ".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE",
        "pull_request_template.md", "docs/pull_request_template.md",
        ".gitlab/merge_request_templates",
    ]),
    ("CODEOWNERS", ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS"]),
    ("commitlint config", [
        "commitlint.config.js", "commitlint.config.cjs", "commitlint.config.mjs",
        "commitlint.config.ts", ".commitlintrc", ".commitlintrc.json", ".commitlintrc.yml",
        ".commitlintrc.yaml", ".commitlintrc.js", ".commitlintrc.cjs",
    ]),
]


def existing_guidelines(root):
    """Find guideline files, matching names case-insensitively."""
    found = []
    listings = {}
    for label, candidates in GUIDELINES:
        for rel in candidates:
            parent, name = os.path.split(rel)
            if parent not in listings:
                try:
                    listings[parent] = {n.lower(): n for n in os.listdir(os.path.join(root, parent))}
                except OSError:
                    listings[parent] = {}
            actual = listings[parent].get(name.lower())
            if actual:
                found.append("{}: {}".format(label, os.path.join(parent, actual).replace("\\", "/")))
    return found


def check_push(cwd, args):
    root = repo_root(cwd)
    if not root:
        return None
    rules = load_rules(root)
    targets, warnings = push_targets(cwd, args)
    blocked = sorted(t for t in targets if is_protected(t, rules))
    if blocked:
        return ("block", (
            "git-workflow-guard: pushing to protected branch {b} is blocked ({p}).\n"
            "Push a feature branch instead (`git push -u origin <feature-branch>`) and open a "
            "pull request."
        ).format(b=", ".join(repr(t) for t in blocked), p=describe(rules)))

    lines = ["git-workflow-guard: confirm this push."]
    if targets:
        lines.append("Target: " + ", ".join(sorted(targets)))
    for w in warnings:
        lines.append("WARNING: " + w)
    found = existing_guidelines(root)
    if found:
        lines.append("Check these before pushing: " + "; ".join(found))
    else:
        lines.append("No CONTRIBUTING.md, PR template, CODEOWNERS, or commitlint config found.")
    return ("ask", "\n".join(lines))


# ---------------------------------------------------------------- main


def evaluate(payload):
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if tool in EDIT_TOOLS:
        r = check_edit(tool_input)
        return [r] if r else []
    if tool not in SHELL_TOOLS:
        return []
    command = tool_input.get("command") or ""
    if "git" not in command:
        return []
    cwd = payload.get("cwd") or os.getcwd()
    results = []
    if "git-guard.json" in command:  # best effort: a shell write to the config
        results.append(("ask", CONFIG_ASK.format(CONFIG_FILE) + " This command mentions it."))
    for here, sub, args, config in git_invocations(command, cwd):
        if sub == "commit":
            r = check_commit(here, args, command, config)
        elif sub == "push":
            r = check_push(here, args)
        elif sub in ("switch", "checkout", "branch"):
            name = new_branch_name(sub, args)
            r = check_branch_name(name) if name else check_discard(sub, args)
        else:
            r = check_discard(sub, args)
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
