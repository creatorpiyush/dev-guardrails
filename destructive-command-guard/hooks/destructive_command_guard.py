#!/usr/bin/env python3
"""PreToolUse hook that catches irreversible shell commands.

Parses each Bash/PowerShell command into simple commands, unwraps sudo, env,
xargs, `bash -c`, eval, $(...), backticks, and heredocs, and checks every
inner command against the rule table in rules.py. Exits 2 to block a "deny"
match, prints a JSON "ask" decision for an "ask" match, and allows everything
else. Any unexpected error fails open (exit 0).

Git commands are out of scope: git-workflow-guard owns them.

Standard library only. Python 3.9+.
"""
import json
import os
import re
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True  # don't litter the plugin cache with __pycache__
import rules as R  # noqa: E402  sibling file; the script's directory is on sys.path

SHELL_TOOLS = {"Bash", "PowerShell"}
MAX_DEPTH = 5
CONFIG_NAME = "destructive-guard.json"
DISABLE_ENV = "DESTRUCTIVE_GUARD_DISABLE"

SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
PWSH = {"pwsh", "powershell"}
KEYWORDS = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}", "(", ")"}
# wrapper -> its options that take a separate value
WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S"},
    "nohup": set(), "command": set(), "builtin": set(), "exec": {"-a"},
    "time": {"-f", "-o"},
    "nice": {"-n"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e"},
    "xargs": {"-n", "-I", "-P", "-L", "-d", "-E", "-s", "-a",
              "--max-args", "--replace", "--delimiter", "--max-procs", "--arg-file"},
}
HEREDOC_OP = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
HEREDOC_TOKEN = re.compile(r"^__HEREDOC_(\d+)__$")
REDIRECT = re.compile(r"^(\d*|&)(>>?|>\||<|<&|>&)(?!\()(.*)$")  # not <(...) / >(...)

# Conservative checks for commands shlex can't tokenise. Anything else is allowed.
FALLBACK = [
    ("fs.rm-catastrophic", re.compile(
        r"(^|[;&|(]\s*)(sudo\s+)?rm\s+(-\S+\s+)*-[a-zA-Z]*[rRf][a-zA-Z]*\s+(-\S+\s+)*"
        r"(/|/\*|~/?|\$HOME/?|\.|\.\.|\*)(\s|;|&|\||\)|$)")),
    ("disk.overwrite", re.compile(
        r"(^|[;&|(]\s*)(sudo\s+)?(mkfs(\.\w+)?\s|dd\s[^;&|]*\bof=/dev/(sd|hd|vd|xvd|nvme|disk|rdisk|mmcblk))")),
    ("remote.pipe-to-shell", re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b")),
]


class Finding:
    def __init__(self, rule_id, detail, display, sudo, conn=(), action=None, reason=None, safer=None):
        self.rule_id, self.detail, self.display, self.sudo = rule_id, detail, display, sudo
        self.conn = list(conn)
        rule = R.RULES_BY_ID.get(rule_id)
        self.action = action
        self.reason = reason or (rule.reason if rule else "")
        self.safer = safer if safer is not None else (rule.safer if rule else "")
        self.notes = []


class State:
    def __init__(self, ctx, tool, extra):
        self.ctx, self.tool, self.extra, self.heredocs = ctx, tool, extra, []


# ---------------------------------------------------------------- parsing

def extract_heredocs(command, bodies):
    """Replace heredoc operators with placeholder tokens and remove their bodies."""
    lines, out, i = command.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        i += 1
        ops = list(HEREDOC_OP.finditer(line))
        if not ops:
            out.append(line)
            continue
        base = len(bodies)
        new_bodies = []
        for m in ops:
            body = []
            while i < len(lines) and lines[i].strip() != m.group(3):
                body.append(lines[i])
                i += 1
            if i >= len(lines):
                return command  # no terminator: not really a heredoc
            i += 1
            new_bodies.append("\n".join(body))
        for k, m in reversed(list(enumerate(ops))):
            line = line[:m.start()] + " __HEREDOC_{}__ ".format(base + k) + line[m.end():]
        bodies.extend(new_bodies)
        out.append(line)
    return "\n".join(out)


def substitutions(command, backticks=True):
    """Inner text of $(...), <(...), >(...), and `...` outside single quotes."""
    found, i, n, single, double = [], 0, len(command), False, False
    while i < n:
        c = command[i]
        if c == "\\" and not single:
            i += 2
            continue
        if c == "'" and not double:
            single = not single
        elif c == '"' and not single:
            double = not double
        elif not single and c in "$<>" and command[i + 1:i + 2] == "(" and command[i + 2:i + 3] != "(":
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(command[j], 0)
                j += 1
            found.append(command[i + 2:j - 1])
            i = j
            continue
        elif not single and backticks and c == "`":
            j = command.find("`", i + 1)
            if j == -1:
                break
            found.append(command[i + 1:j])
            i = j + 1
            continue
        i += 1
    return found


def split_simple(command, escape):
    """Return [(argv, operator before it)], or None if shlex can't tokenise it."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lex.whitespace = " \t\r"  # keep newlines as command separators
    lex.whitespace_split = True
    lex.commenters = ""
    lex.escape = escape
    try:
        tokens = list(lex)
    except ValueError:
        return None
    segments, cur, op = [], [], None
    for tok in tokens:
        if tok == "\n" or set(tok) <= set(";&|"):
            if cur:
                segments.append((cur, op))
            cur, op = [], ("|" if tok in ("|", "|&") else tok)
        else:
            cur.append(tok)
    if cur:
        segments.append((cur, op))
    return segments


def unwrap(argv):
    """Strip keywords, VAR=value, and wrappers. Return (argv, sudo, xargs, env)."""
    argv, sudo, xargs, env = list(argv), False, False, {}
    while argv:
        t = argv[0]
        stripped = t.lstrip("({")
        if stripped != t and not t.startswith("$("):
            argv[0] = stripped
            if not stripped:
                argv.pop(0)
            continue
        if t in KEYWORDS:
            argv.pop(0)
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", t, re.S)
        if m:
            env[m.group(1)] = m.group(2)
            argv.pop(0)
            continue
        name = os.path.basename(t).lower()
        if name not in WRAPPERS:
            break
        sudo = sudo or name in ("sudo", "doas")
        xargs = xargs or name == "xargs"
        argv.pop(0)
        while argv and argv[0].startswith("-") and argv[0] != "--":
            opt = argv.pop(0)
            if opt in WRAPPERS[name] and argv:
                argv.pop(0)
        if argv and argv[0] == "--":
            argv.pop(0)
        if name == "timeout" and argv:
            argv.pop(0)  # the duration
    return argv, sudo, xargs, env


def split_io(argv, heredocs):
    """Pull redirections, heredocs, and here-strings out of argv."""
    out, stdin, redirects, i = [], [], [], 0
    while i < len(argv):
        a = argv[i]
        h = HEREDOC_TOKEN.match(a)
        r = REDIRECT.match(a)
        if h:
            idx = int(h.group(1))
            if idx < len(heredocs):
                stdin.append(heredocs[idx])
        elif a == "<<<":
            if i + 1 < len(argv):
                stdin.append(argv[i + 1])
            i += 1
        elif r and i > 0:
            target = r.group(3)
            if not target and i + 1 < len(argv):
                target = argv[i + 1]
                i += 1
            if ">" in r.group(2):
                redirects.append(target)
        else:
            out.append(a)
        i += 1
    return out, stdin, redirects


def nested_scripts(cmd):
    """Scripts this command runs: bash -c '...', eval ..., pwsh -Command ..., bash <<EOF."""
    args = cmd.args
    if cmd.name in SHELLS:
        for i, a in enumerate(args):
            if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", a) and i + 1 < len(args):
                return [args[i + 1]]
        if not [a for a in args if not a.startswith("-")]:
            return list(cmd.stdin)  # script read from stdin
    if cmd.name in PWSH:
        for i, a in enumerate(args):
            if a.lower() in ("-c", "-command") and i + 1 < len(args):
                return [" ".join(args[i + 1:])]
    if cmd.name == "eval" and args:
        return [" ".join(args)]
    return []


def analyze(command, cwd, depth, sudo, state):
    findings = []
    if depth > MAX_DEPTH:
        return findings
    if R.fork_bomb(command):
        findings.append(Finding("proc.fork-bomb", ":(){ :|:& };:", command, sudo))
    command = extract_heredocs(command, state.heredocs)
    is_pwsh = state.tool == "PowerShell"
    for inner in substitutions(command, backticks=not is_pwsh):
        findings += analyze(inner, cwd, depth + 1, sudo, state)

    segments = split_simple(command, "" if is_pwsh else "\\")
    if segments is None:
        return findings + fallback(command, sudo)

    fetch_in_pipeline = False
    for raw_argv, op in segments:
        if op != "|":
            fetch_in_pipeline = False
        argv, wrapped_sudo, xargs, env = unwrap(raw_argv)
        argv, stdin, redirects = split_io(argv, state.heredocs)
        if not argv:
            continue
        if argv[0] == "cd":
            cwd = os.path.join(cwd, os.path.expanduser(argv[1])) if len(argv) > 1 else os.path.expanduser("~")
            continue
        cmd = R.Cmd(argv=argv, cwd=cwd, display=" ".join(raw_argv), sudo=sudo or wrapped_sudo,
                    xargs=xargs, piped_from_fetcher=fetch_in_pipeline, env=env,
                    stdin=stdin, redirects=redirects)
        for rule in R.RULES:
            if rule.matcher is None:
                continue
            detail = rule.matcher(cmd, state.ctx)
            if detail:
                conn = R.db_parts(cmd)[1] if rule.id.startswith("db.") else ()
                display = command if rule.id == "remote.pipe-to-shell" else cmd.display
                findings.append(Finding(rule.id, detail, display, cmd.sudo, conn))
        for script in nested_scripts(cmd):
            findings += analyze(script, cwd, depth + 1, cmd.sudo, state)
        if cmd.name in R.FETCHERS:
            fetch_in_pipeline = True
        findings += [Finding(e["id"], cmd.display, cmd.display, cmd.sudo, action=e["action"],
                             reason=e.get("reason", "matches a custom rule"), safer=e.get("safer", ""))
                     for e in state.extra if e["compiled"].search(" ".join(argv))]
    return findings


def fallback(command, sudo):
    out = []
    for rule_id, rx in FALLBACK:
        m = rx.search(command)
        if m:
            out.append(Finding(rule_id, m.group(0).strip(), command, sudo or "sudo" in m.group(0)))
    return out


# ---------------------------------------------------------------- configuration

def load_config(path, label, warnings):
    """Return a validated config dict, or None if missing or invalid."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        warnings.append("{}: can't read {} ({}); ignoring it".format(label, path, e))
        return None
    try:
        if not isinstance(data, dict):
            raise ValueError("top level must be an object")
        rules = data.get("rules", {})
        if not isinstance(rules, dict) or not all(v in R.STRICTNESS for v in rules.values()):
            raise ValueError('"rules" must map rule ids to "deny", "ask", or "allow"')
        for rid in [k for k in rules if k not in R.RULES_BY_ID]:
            warnings.append("{}: unknown rule id {!r}; ignoring it".format(label, rid))
            del rules[rid]
        allow = data.get("allow", [])
        if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
            raise ValueError('"allow" must be a list of regexes')
        allow = [re.compile(a) for a in allow]
        extra = data.get("extraRules", [])
        if not isinstance(extra, list):
            raise ValueError('"extraRules" must be a list')
        for e in extra:
            if not (isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("regex"), str)
                    and e.get("action") in R.STRICTNESS):
                raise ValueError('each extraRules entry needs "id", "regex", and "action"')
            e["compiled"] = re.compile(e["regex"])
        prod = data.get("prodPattern")
        if prod is not None:
            if not isinstance(prod, str):
                raise ValueError('"prodPattern" must be a string')
            re.compile(prod)
    except (ValueError, re.error) as e:
        warnings.append("{}: invalid config {} ({}); ignoring it".format(label, path, e))
        return None
    return {"rules": rules, "allow": allow, "extra": extra, "prod": prod}


def resolve_settings(project, warnings):
    """Merge defaults, the trusted user config, and the untrusted repo config."""
    actions = {r.id: r.action for r in R.RULES}
    allow, extra, prod = [], [], [R.DEFAULT_PROD_PATTERN]
    user_path = os.path.join(os.path.expanduser("~"), ".claude", CONFIG_NAME)
    repo_path = os.path.join(project, ".claude", CONFIG_NAME)

    user = load_config(user_path, "user config", warnings)
    if user:  # trusted: may change anything
        actions.update(user["rules"])
        allow, extra = user["allow"], list(user["extra"])
        if user["prod"]:
            prod = [user["prod"]]

    repo = None
    if R.real(repo_path) != R.real(user_path):
        repo = load_config(repo_path, "repo config", warnings)
    if repo:  # untrusted: may only add rules or make them stricter
        for rid, action in repo["rules"].items():
            if R.STRICTNESS[action] > R.STRICTNESS[actions[rid]]:
                actions[rid] = action
            elif R.STRICTNESS[action] < R.STRICTNESS[actions[rid]]:
                warnings.append("repo config tried to loosen {} to {!r}; ignored (only {} can)".format(
                    rid, action, user_path))
        if repo["allow"]:
            warnings.append('repo config "allow" is ignored; only {} can allow commands'.format(user_path))
        for e in repo["extra"]:
            if e["action"] == R.ALLOW:
                warnings.append("repo config extra rule {!r} has action allow; ignored".format(e["id"]))
            else:
                extra.append(e)
        if repo["prod"]:
            prod.append(repo["prod"])  # adds to the pattern, never replaces it
    return actions, allow, extra, prod


# ---------------------------------------------------------------- decisions

def decide(findings, actions, allow, prod, command):
    kept = []
    for f in findings:
        action = f.action or actions.get(f.rule_id, R.ASK)
        if action == R.ALLOW:
            continue
        if any(rx.fullmatch(f.display.strip()) or rx.fullmatch(command.strip()) for rx in allow):
            continue
        if action == R.ASK and f.sudo:
            action = R.DENY
            f.notes.append("escalated to deny: runs under sudo")
        if action == R.ASK and f.rule_id.startswith("db.") and any(
                re.search(p, c, re.I) for p in prod for c in f.conn):
            action = R.DENY
            f.notes.append("escalated to deny: the connection looks like production")
        f.action = action
        kept.append(f)
    return kept


def short(text, limit=160):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def describe(f):
    lines = ["[{}] `{}`".format(f.rule_id, short(f.display)), "  Why: " + f.reason]
    if f.safer:
        lines.append("  Safer: " + f.safer)
    lines += ["  Note: " + n for n in f.notes]
    return "\n".join(lines)


def project_root(cwd):
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return cwd


def main():
    if os.environ.get(DISABLE_ENV) == "1":
        return 0
    payload = json.loads(sys.stdin.read())
    tool = payload.get("tool_name")
    if tool not in SHELL_TOOLS:
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command.strip():
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    project = project_root(cwd)

    warnings = []
    actions, allow, extra, prod = resolve_settings(project, warnings)
    state = State(R.Context(project=R.real(project), home=R.real(os.path.expanduser("~"))), tool, extra)
    findings = decide(analyze(command, cwd, 0, False, state), actions, allow, prod, command)

    # One entry per rule and command.
    unique = {}
    for f in findings:
        key = (f.rule_id, f.display)
        if key not in unique or R.STRICTNESS[f.action] > R.STRICTNESS[unique[key].action]:
            unique[key] = f
    findings = list(unique.values())

    for w in warnings:
        sys.stderr.write("destructive-command-guard warning: " + w + "\n")
    denies = [f for f in findings if f.action == R.DENY]
    if denies:
        sys.stderr.write("\n".join(
            ["destructive-command-guard blocked this command."]
            + [describe(f) for f in findings]
            + ["Explain the risk to the user in plain words and offer the safer alternative. "
               "Do not rephrase, split, encode, or wrap the command to get past this check. "
               "If the user really wants it, they can run it themselves in a terminal."]) + "\n")
        return 2
    if findings:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "\n".join(
                ["destructive-command-guard: this command may be irreversible."]
                + [describe(f) for f in findings]),
        }}))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # fail open: never break the user's session
        code = 0
    sys.exit(code)
