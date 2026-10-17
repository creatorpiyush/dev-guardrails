#!/usr/bin/env python3
"""PreToolUse hook that stops silent edits to files that should change only on purpose.

Reads the hook payload as JSON on stdin. Exits 2 (with a reason on stderr) to
block a tool call, prints a JSON "ask" decision to request confirmation, or
exits 0 to allow. Any unexpected error fails open (exit 0).

File tools (Edit, Write, MultiEdit, NotebookEdit) are checked exactly. Shell
commands are checked heuristically: redirects, tee, sed -i, mv, cp, ln,
install, rm, truncate, git checkout -- <path>, git restore, and a few
PowerShell cmdlets.
Anything else (python -c, dd, variables, scripts) gets past it.

Standard library only. Python 3.9+.
"""
import glob
import json
import os
import re
import shlex
import subprocess
import sys

DISABLE_ENV = "PROTECTED_FILES_GUARD_DISABLE"

# Optional per-repo config, relative to the repo root:
#   {"rules": [{"pattern": "infra/**", "action": "deny", "reason": "..."}],
#    "allow": ["docs/migrations/**"], "replaceDefaults": false}
CONFIG_FILE = ".claude/protected-files.json"

LOCK_REASON = (
    "lockfiles are generated. Change dependencies through the package manager "
    "(npm install <pkg>, yarn add, pnpm add, poetry add, uv add, cargo add, bundle add, "
    "composer require, go get) and let it rewrite the lockfile, not by hand"
)
CI_REASON = "CI/CD config runs with repository secrets and gates merges, so it changes only on purpose"
MIGRATION_REASON = (
    "migrations change the schema in every environment and may already have run. "
    "Generate new ones with the framework's migration tool, and don't edit applied ones"
)
POLICY_REASON = "license, code ownership, and security policy files change only on purpose"
SECRET_REASON = "it holds secrets or private keys. Ask the user to change it themselves"

# (patterns, action, reason). A pattern without "/" matches the file name at
# any depth, like .gitignore. One with "/" is anchored at the repo root.
DEFAULT_RULES = [
    (["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
      "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum"], "ask", LOCK_REASON),
    ([".github/workflows/**", ".gitlab-ci.yml", "Jenkinsfile", ".circleci/**"], "ask", CI_REASON),
    (["**/migrations/**", "**/alembic/versions/**", "**/db/migrate/**"], "ask", MIGRATION_REASON),
    (["LICENSE*", "CODEOWNERS", "SECURITY.md"], "ask", POLICY_REASON),
    ([".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.p12", "*.keystore",
      "credentials.json"], "deny", SECRET_REASON),
]
DEFAULT_ALLOW = [".env.example", ".env.sample", ".env.template"]

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}

REDIRECT_WRITE = {">", ">>", ">|", "&>", "&>>", ">&"}
REDIRECT_READ = {"<", "<<", "<<-", "<<<", "<&"}
# wrapper -> its options that take a separate value
WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "--user", "--group"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S"},
    "command": set(), "builtin": set(), "nohup": set(), "time": {"-f", "-o"},
}
PUNCTUATION = set(";&|<>()")
# Every non-option argument may be written or deleted.
ALL_ARGS = {
    "rm", "unlink", "shred", "truncate", "tee", "mv",
    "remove-item", "ri", "del", "erase", "rd", "rmdir", "clear-content", "clc",
    "set-content", "sc", "add-content", "ac", "out-file", "move-item", "mi", "move",
}
# Only the destination is written.
COPY = {"cp", "copy-item", "cpi", "copy", "ln", "install"}
DEST_OPTS = {"-t", "--target-directory", "-destination"}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

_warnings = []
_cache = {}


def git(cwd, *args):
    """Run git in cwd; return stdout, or None on any failure."""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


# ---------------------------------------------------------------- globs and rules


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


def compile_rules(rules):
    return [(p, glob_re(p), action, reason) for patterns, action, reason in rules for p in patterns]


def load_config(root):
    """Return (rules, allow regexes) for a repo. Invalid config: defaults plus a warning."""
    if root in _cache:
        return _cache[root]
    rules, allow = compile_rules(DEFAULT_RULES), [glob_re(p) for p in DEFAULT_ALLOW]
    path = os.path.join(root, CONFIG_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("top level must be an object")
        user_rules = cfg.get("rules", [])
        user_allow = cfg.get("allow", [])
        replace = cfg.get("replaceDefaults", False)
        if not isinstance(user_rules, list):
            raise ValueError("'rules' must be a list")
        if not isinstance(user_allow, list) or not all(isinstance(a, str) for a in user_allow):
            raise ValueError("'allow' must be a list of strings")
        if not isinstance(replace, bool):
            raise ValueError("'replaceDefaults' must be true or false")
        extra = []
        for r in user_rules:
            if not isinstance(r, dict) or not isinstance(r.get("pattern"), str):
                raise ValueError("each rule needs a string 'pattern'")
            if r.get("action") not in ("ask", "deny"):
                raise ValueError("rule action must be 'ask' or 'deny'")
            reason = r.get("reason", "protected by " + CONFIG_FILE)
            if not isinstance(reason, str):
                raise ValueError("rule reason must be a string")
            extra.append(([r["pattern"]], r["action"], reason))
        extra_allow = [glob_re(p) for p in user_allow]
        if replace:
            rules, allow = compile_rules(extra), extra_allow
        else:
            rules, allow = rules + compile_rules(extra), allow + extra_allow
    except FileNotFoundError:
        pass
    except (OSError, ValueError, re.error) as e:
        _warnings.append("protected-files-guard: ignoring invalid {} ({}); using the default "
                         "rules.".format(path, e))
    _cache[root] = rules, allow
    return rules, allow


def classify(root, rel):
    """Return (action, reason, pattern) for a repo-relative path, or None."""
    if rel.lower() == CONFIG_FILE:  # the guard's own config always asks
        return ("ask", "it controls protected-files-guard's rules", CONFIG_FILE)
    rules, allow = load_config(root)
    if any(a.fullmatch(rel) for a in allow):
        return None
    hits = [(action, reason, p) for p, rx, action, reason in rules if rx.fullmatch(rel)]
    hits.sort(key=lambda h: h[0] != "deny")  # deny beats ask
    return hits[0] if hits else None


# ---------------------------------------------------------------- paths


def toplevel(d):
    key = ("root", d)
    if key not in _cache:
        out = git(d, "rev-parse", "--show-toplevel")
        _cache[key] = os.path.realpath(out.strip()) if out else None
    return _cache[key]


def locate(path):
    """Return (repo root, repo-relative path with "/") for an absolute path, or None."""
    d = os.path.dirname(path)
    while not os.path.isdir(d):  # Write may target a new directory
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    root = toplevel(d)
    if not root:
        return None
    try:
        rel = os.path.relpath(path, root)
    except ValueError:  # another Windows drive
        return None
    if rel == os.curdir or rel.startswith(os.pardir) or os.path.isabs(rel):
        return None
    return root, rel.replace(os.sep, "/")


def targets_for(path):
    """Yield (root, rel) for the path as named and, if a symlink, where it points."""
    path = os.path.abspath(path)
    named = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    seen = set()
    for p in (named, os.path.realpath(path)):
        loc = locate(p)
        if loc and loc not in seen:
            seen.add(loc)
            yield loc


def check_path(path, verb):
    """Return [(action, message)] for one path a tool call writes."""
    results = []
    for root, rel in targets_for(path):
        hit = classify(root, rel)
        if not hit:
            continue
        action, reason, pattern = hit
        what = "editing '{}'".format(rel) if verb is None else "this command ({}) may write '{}'".format(verb, rel)
        if action == "deny":
            msg = ("protected-files-guard: {w} is blocked: {r}. (rule: {p})\n"
                   "Do not work around this with Bash, another tool, or a config change. Tell the "
                   "user why the file is protected and how they can change it.").format(
                       w=what, r=reason, p=pattern)
        else:
            msg = "protected-files-guard: {w}, a protected file: {r}. (rule: {p}) Allow it?".format(
                w=what, r=reason, p=pattern)
        results.append((action, msg))
    return results


def expand_dir(path):
    """A directory target stands for the tracked files under it."""
    path = os.path.abspath(path)
    if not os.path.isdir(path) or os.path.islink(path):
        return [path]
    out = git(path, "ls-files", "-z", "--full-name", "--", ".")
    root = toplevel(path)
    if out is None or not root:
        return []
    return [os.path.join(root, f) for f in out.split("\0") if f]


# ---------------------------------------------------------------- shell parsing


def split_segments(command):
    """Split a shell command into simple-command token lists. None if unparseable.

    Adapted from git-workflow-guard: `<` and `>` are punctuation too, so
    redirects come out as their own tokens. Each ( and ) of a subshell is
    kept as its own one-token segment, so a `cd` inside it can be undone."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
    lex.whitespace = " \t\r"  # keep newlines as command separators
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        return None
    segments, cur = [], []
    for raw in tokens:
        # shlex joins neighbouring punctuation, as in `)>out` or `)&&`; split off the parens.
        parts = re.findall(r"[()]|[^()]+", raw) if raw and set(raw) <= PUNCTUATION else [raw]
        for tok in parts:
            if tok in ("(", ")") or (tok and set(tok) <= set(";&|\n")):
                if cur:
                    segments.append(cur)
                cur = []
                if tok in ("(", ")"):
                    segments.append([tok])
            else:
                cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def sed_files(args):
    """Files `sed -i` edits in place; [] without -i."""
    inplace, script_given, files, i = False, False, [], 0
    while i < len(args):
        a = args[i]
        if a in ("-e", "--expression", "-f", "--file"):
            script_given, i = True, i + 2
            continue
        if a.startswith(("--expression=", "--file=")):
            script_given = True
        elif a == "--in-place" or a.startswith("--in-place="):
            inplace = True
        elif re.match(r"^-[a-zA-Z]*i", a):
            inplace = True
            if a == "-i" and i + 1 < len(args) and args[i + 1] == "":
                i += 1  # BSD sed: -i ''
        elif not a.startswith("-"):
            files.append(a)
        i += 1
    if not inplace:
        return []
    return files if script_given else files[1:]


def git_files(args):
    """Paths `git checkout -- <path>` / `git restore <path>` overwrite."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in GIT_OPTS_WITH_VALUE:
            i += 1
        i += 1
    if i >= len(args):
        return None, []
    sub, rest = args[i], args[i + 1:]
    if sub == "checkout" and "--" in rest:
        return "git checkout --", rest[rest.index("--") + 1:]
    if sub == "restore":
        if ("--staged" in rest or "-S" in rest) and not ("--worktree" in rest or "-W" in rest):
            return None, []  # index only; the file on disk is untouched
        paths, j = [], 0
        while j < len(rest):
            if rest[j] in ("-s", "--source"):
                j += 1
            elif rest[j] != "--" and not rest[j].startswith("-"):
                paths.append(rest[j])
            j += 1
        return "git restore", paths
    return None, []


def git_cwd(args, cwd):
    """Follow `git -C <dir>`."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C" and i + 1 < len(args):
            cwd = os.path.join(cwd, os.path.expanduser(args[i + 1]))
        if args[i] in GIT_OPTS_WITH_VALUE:
            i += 1
        i += 1
    return cwd


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


def strip_wrappers(words):
    """Drop leading VAR=value and wrappers with their options, such as `sudo -u root`."""
    while words:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
            continue
        name = os.path.basename(words[0].replace("\\", "/")).lower()
        if name not in WRAPPERS:
            break
        words = words[1:]
        while words and words[0].startswith("-") and words[0] != "--":
            opt, words = words[0], words[1:]
            if takes_separate_value(opt, WRAPPERS[name]) and words:
                words = words[1:]
        if words and words[0] == "--":
            words = words[1:]
    return words


def shell_writes(command, cwd):
    """Yield (path token, verb, cwd) for each path the command may write. Heuristic."""
    saved = []  # cwd at each open subshell
    for seg in split_segments(command) or []:
        if seg == ["("]:
            saved.append(cwd)
            continue
        if seg == [")"]:
            cwd = saved.pop() if saved else cwd
            continue
        words, i = [], 0
        while i < len(seg):  # pull out redirects
            tok = seg[i]
            if tok in REDIRECT_WRITE or tok in REDIRECT_READ:
                if words and words[-1].isdigit():
                    words.pop()  # the fd in 2>file
                if tok in REDIRECT_WRITE and i + 1 < len(seg) and not seg[i + 1].isdigit() \
                        and seg[i + 1] != "-":
                    yield seg[i + 1], "redirect " + tok, cwd
                i += 2
                continue
            words.append(tok)
            i += 1
        words = strip_wrappers(words)
        if not words:
            continue
        name, args = os.path.basename(words[0]).lower(), words[1:]
        if name.endswith(".exe"):
            name = name[:-4]
        if name in ("cd", "set-location", "sl", "pushd") and args:
            cwd = os.path.join(cwd, os.path.expanduser(args[-1]))
        elif name == "sed":
            for f in sed_files(args):
                yield f, "sed -i", cwd
        elif name in ALL_ARGS:
            for a in args:
                if not a.startswith("-"):
                    yield a, name, cwd
        elif name in COPY:
            positional = [a for a in args if not a.startswith("-")]
            dest = next((args[j + 1] for j, a in enumerate(args[:-1]) if a.lower() in DEST_OPTS), None)
            sources = [p for p in positional if p != dest]
            if dest is None and len(positional) >= 2:
                dest, sources = positional[-1], positional[:-1]
            if dest is None:
                continue
            full = os.path.join(cwd, os.path.expanduser(dest))
            if os.path.isdir(full) or dest.endswith(("/", "\\")):
                for s in sources:
                    yield os.path.join(dest, os.path.basename(s.rstrip("/\\"))), name, cwd
            else:
                yield dest, name, cwd
        elif name == "git":
            verb, paths = git_files(args)
            here = git_cwd(args, cwd)
            for p in paths:
                yield p, verb, here


def resolve_shell_paths(command, cwd):
    """Yield (absolute path, verb), expanding ~, $VARS, globs, and directories."""
    for token, verb, here in shell_writes(command, cwd):
        token = os.path.expandvars(os.path.expanduser(token))
        if "$" in token or "`" in token or not token:
            continue  # can't evaluate
        full = os.path.join(here, token)
        matches = glob.glob(full) if any(c in token for c in "*?[") else []
        for p in matches or [full]:
            for f in expand_dir(p):
                yield f, verb


# ---------------------------------------------------------------- main


def evaluate(payload):
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    if tool in EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        return check_path(os.path.join(cwd, path), None) if path else []
    if tool in SHELL_TOOLS:
        results = []
        for path, verb in resolve_shell_paths(tool_input.get("command") or "", cwd):
            for r in check_path(path, verb):
                if r not in results:
                    results.append(r)
        return results
    return []


def main():
    if os.environ.get(DISABLE_ENV) == "1":
        return 0
    payload = json.loads(sys.stdin.read())
    results = evaluate(payload)
    if _warnings:
        sys.stderr.write("\n".join(_warnings) + "\n")
    denies = [msg for kind, msg in results if kind == "deny"]
    if denies:
        sys.stderr.write("\n\n".join(denies) + "\n")
        return 2
    asks = [msg for kind, msg in results if kind == "ask"]
    out = {}
    if asks:
        out["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "\n\n".join(asks),
        }
    if _warnings:  # stderr is never shown on exit 0
        out["systemMessage"] = "\n".join(_warnings)
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # fail open: never break the user's session
        code = 0
    sys.exit(code)
