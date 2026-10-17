#!/usr/bin/env python3
"""Collect read-only git context for the pr-writer skill.

Prints exactly one JSON object on stdout and always exits 0. Failures are
reported as {"ok": false, "error": {"code", "message"}}.

Standard library only. Python 3.9+. No network.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# copied from git-workflow-guard/hooks/git_guard.py; keep in sync (see CLAUDE.md)
PROTECTED = ["main", "master", "develop", "dev", "trunk", "staging", "production"]
PROTECTED_PREFIXES = ["release/", "hotfix/"]
GUARD_CONFIG = os.path.join(".claude", "git-guard.json")

CONFIG_FILE = os.path.join(".claude", "pr-writer.json")
TITLE_FORMATS = ("conventional", "free")
DEFAULT_CONFIG = {
    "draftByDefault": True,
    "titleFormat": "conventional",
    "maxTitleLength": 72,
    "language": "en",
    "labels": {},
    "extraChecklist": [],
    "diffSizeCap": 60000,
}


def git(cwd, *args):
    """Run read-only git in cwd; return stripped stdout, or None on any failure."""
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=10, errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


# copied from git-workflow-guard/hooks/git_guard.py; keep in sync (see CLAUDE.md)
def load_rules(root):
    protected, prefixes = list(PROTECTED), list(PROTECTED_PREFIXES)
    try:
        with open(os.path.join(root, GUARD_CONFIG), encoding="utf-8") as f:
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


# copied from git-workflow-guard/hooks/git_guard.py; keep in sync (see CLAUDE.md)
def is_protected(branch, rules):
    names, prefixes = rules
    return branch in names or branch.startswith(prefixes)


def fail(code, message, **extra):
    out = {"ok": False, "error": {"code": code, "message": message}, "warnings": []}
    out.update(extra)
    return out


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parse_args(argv):
    p = _Parser(prog="collect_context.py", add_help=False)
    p.add_argument("--base")
    p.add_argument("--check-title", dest="check_title")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--ready", action="store_true")
    g.add_argument("--draft", action="store_true")
    a = p.parse_args(argv)
    return a.base, a.ready, a.draft, a.check_title


def _valid(key, val):
    if key == "draftByDefault":
        return isinstance(val, bool)
    if key in ("maxTitleLength", "diffSizeCap"):
        return isinstance(val, int) and not isinstance(val, bool) and val > 0
    if key == "titleFormat":
        return val in TITLE_FORMATS
    if key == "language":
        return isinstance(val, str) and bool(val)
    if key == "labels":
        return isinstance(val, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in val.items())
    if key == "extraChecklist":
        return isinstance(val, list) and all(isinstance(v, str) for v in val)
    return True


def load_config(root, warnings):
    cfg = dict(DEFAULT_CONFIG, labels={}, extraChecklist=[])
    path = os.path.join(root, CONFIG_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as e:
        warnings.append("ignoring %s: %s" % (CONFIG_FILE, e))
        return cfg
    if not isinstance(data, dict):
        warnings.append("ignoring %s: must be a JSON object" % CONFIG_FILE)
        return cfg
    for key, val in data.items():
        if key in DEFAULT_CONFIG and not _valid(key, val):
            warnings.append("ignoring %s: invalid value for %s" % (CONFIG_FILE, key))
            return dict(DEFAULT_CONFIG, labels={}, extraChecklist=[])
    for key, val in data.items():
        if key in DEFAULT_CONFIG:
            cfg[key] = val
        else:
            warnings.append("unknown key in %s: %s" % (CONFIG_FILE, key))
    return cfg


def resolve_base(root, arg):
    """Pick the base ref: argument, origin/HEAD, then main/master."""
    def pick(name, source):
        for ref in ("origin/" + name, name):
            if git(root, "rev-parse", "--verify", "-q", ref + "^{commit}"):
                return {"name": name, "ref": ref, "source": source}
        return None

    if arg:
        return pick(arg, "argument")
    head = git(root, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
    if head and head.startswith("origin/"):
        found = pick(head[len("origin/"):], "origin-head")
        if found:
            return found
    for name in ("main", "master"):
        found = pick(name, "fallback")
        if found:
            return found
    return None


def push_state(root, branch):
    upstream = git(root, "rev-parse", "--abbrev-ref", "-q", "@{u}")
    if not upstream and git(root, "rev-parse", "--verify", "-q", "origin/" + branch):
        upstream = "origin/" + branch
    sha = git(root, "rev-parse", "--verify", "-q", upstream) if upstream else None
    return {"upstream": upstream, "pushed": bool(sha) and sha == git(root, "rev-parse", "HEAD"),
            "note": "based on the last fetch"}


# copied from git-workflow-guard/hooks/git_guard.py; keep in sync (see CLAUDE.md)
TYPES = "feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert"
CONVENTIONAL = re.compile(r"^(" + TYPES + r")(\([^()\s]+\))?!?: \S.*$")
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE: *(.*)$", re.M)


def parse_commit(hash, subject, body):
    m = CONVENTIONAL.match(subject)
    scope = m.group(2)[1:-1] if m and m.group(2) else None
    bang = bool(m) and subject.split(": ", 1)[0].endswith("!")
    return {"hash": hash, "subject": subject, "body": body,
            "type": m.group(1) if m else None, "scope": scope,
            "breaking": bang or bool(BREAKING_FOOTER.search(body))}


COMMIT_CAP = 100


def commits(root, merge_base):
    """Commits on HEAD since merge_base, merges excluded; (list, truncated)."""
    raw = git(root, "log", "--no-merges", "-z", "--format=%H%x1f%s%x1f%b",
              "%s..HEAD" % merge_base, "-n", str(COMMIT_CAP + 1)) or ""
    out = []
    for rec in raw.split("\0"):
        if not rec.strip():
            continue
        h, s, b = (rec.split("\x1f", 2) + ["", ""])[:3]
        out.append(parse_commit(h.strip(), s, b.strip()))
    return out[:COMMIT_CAP], len(out) > COMMIT_CAP

# copied from secret-scan-guard/hooks/patterns.py; keep in sync (see CLAUDE.md)
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


def redact(text, counter):
    """Replace secrets with <redacted:rule>; count hits per rule in counter."""
    text, n = KEY_BLOCK.subn("<redacted:private-key>", text)
    if n:
        counter["private-key"] = counter.get("private-key", 0) + n
    for name, rx in SECRET_RULES:
        text, n = rx.subn("<redacted:%s>" % name, text)
        if n:
            counter[name] = counter.get(name, 0) + n
    return text


def check_injection(text, where, warnings):
    """Warn once per source (e.g. 'commit abc1234', 'diff of f.py') on injection-like text."""
    msg = "possible prompt injection in %s" % where
    if text and INJECTION.search(text) and msg not in warnings:
        warnings.append(msg)


def numstat(root, merge_base):
    raw = git(root, "diff", "--numstat", "-z", "--no-color", "--no-ext-diff",
              "%s..HEAD" % merge_base) or ""
    parts, files, i = raw.split("\0"), [], 0
    while i < len(parts):
        head = parts[i].split("\t", 2)
        i += 1
        if len(head) < 3:
            continue
        path = head[2]
        if not path:  # rename or copy: empty path, then old and new
            path = parts[i + 1] if i + 1 < len(parts) else ""
            i += 2
        binary = head[0] == "-"
        files.append({"path": path, "added": 0 if binary else int(head[0]),
                      "deleted": 0 if binary else int(head[1]), "binary": binary})
    return files


def diff_info(root, merge_base, cap):
    rng = "%s..HEAD" % merge_base
    files = numstat(root, merge_base)
    stat = git(root, "diff", "--stat", "--no-color", "--no-ext-diff", rng) or ""
    text = git(root, "diff", "--no-color", "--no-ext-diff", rng) or ""
    if len(text.encode("utf-8")) <= cap:
        return {"files": files, "stat": stat, "text": text, "truncated": False, "excerpts": []}
    top = sorted((f for f in files if not f["binary"]),
                 key=lambda f: f["added"] + f["deleted"], reverse=True)[:EXCERPT_FILES]
    excerpts = []
    for f in top:
        d = git(root, "diff", "--no-color", "--no-ext-diff", rng, "--", f["path"]) or ""
        excerpts.append({"path": f["path"], "lines": "\n".join(d.splitlines()[:EXCERPT_LINES])})
    return {"files": files, "stat": stat, "text": None, "truncated": True, "excerpts": excerpts}


DIFF_HEADER = re.compile(r"^diff --git a/.* b/(.*)$", re.M)


def sanitize_diff(info, counter, warnings):
    """Redact in place and warn on injection text, per file where the path is known."""
    info["stat"] = redact(info["stat"], counter)
    if info["text"] is not None:
        info["text"] = redact(info["text"], counter)
        marks = list(DIFF_HEADER.finditer(info["text"]))
        for k, m in enumerate(marks):
            end = marks[k + 1].start() if k + 1 < len(marks) else len(info["text"])
            check_injection(info["text"][m.start():end], "diff of " + m.group(1), warnings)
    for e in info["excerpts"]:
        e["lines"] = redact(e["lines"], counter)
        check_injection(e["lines"], "diff of " + e["path"], warnings)


JIRA = re.compile(r"(?i)(?<![a-z0-9])([a-z][a-z0-9]+-\d+)(?![a-z0-9.])")
FOOTER = re.compile(r"(?im)^(close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?)\b:?\s+(.+)$")
ISSUE_URL = re.compile(
    r"https?://\S+/(?:issues|pull|-/merge_requests|merge_requests|-/issues)/\d+"
    r"|https?://\S+/browse/[A-Z][A-Z0-9]+-\d+")
ISSUE_TOKEN = re.compile(r"#\d+|[A-Za-z][A-Za-z0-9]+-\d+")


def issue_refs(branch, commits):
    found = {}

    def add(ref, source, closing):
        if ref in found:
            found[ref]["closing"] = found[ref]["closing"] or closing
        else:
            found[ref] = {"ref": ref, "source": source, "closing": closing}

    name = branch.split("/", 1)[-1]
    for m in JIRA.finditer(name):
        add(m.group(1).upper(), "branch", False)
    m = re.match(r"(\d+)(?:[-_]|$)", name)
    if m:
        add("#" + m.group(1), "branch", False)
    for c in commits:
        for kw, val in FOOTER.findall(c["body"]):
            closing = not kw.lower().startswith("ref")
            for tok in re.split(r"[,\s]+", val):
                if ISSUE_TOKEN.fullmatch(tok):
                    add(tok.upper() if tok[0] != "#" else tok, "footer", closing)
                elif tok.startswith(("http://", "https://")):
                    add(tok.rstrip(".,;)"), "footer", closing)
        for u in ISSUE_URL.findall(c["subject"] + "\n" + c["body"]):
            add(u, "url", False)
    return list(found.values())


def check_title(title, cfg):
    problems = []
    if cfg["titleFormat"] == "conventional" and not CONVENTIONAL.match(title):
        problems.append("not a Conventional Commit: <type>(<scope>)!: <description>")
    if len(title) > cfg["maxTitleLength"]:
        problems.append("%d chars, over the %d-char limit" % (len(title), cfg["maxTitleLength"]))
    return {"valid": not problems, "problems": problems}


def suggest_title(commits, cfg):
    if len(commits) == 1:
        s = commits[0]["subject"]
        return dict(suggested=s, prefix=None, is_suggestion=False, **check_title(s, cfg))
    typed = [c for c in commits if c["type"]]
    prefix = None
    if typed:
        order = TYPES.split("|")
        counts = {}
        for c in typed:
            counts[c["type"]] = counts.get(c["type"], 0) + 1
        top = min(counts, key=lambda t: (-counts[t], order.index(t)))
        scopes = {c["scope"] for c in typed if c["type"] == top}
        scope = "(%s)" % scopes.copy().pop() if len(scopes) == 1 and None not in scopes else ""
        prefix = "%s%s%s: " % (top, scope, "!" if any(c["breaking"] for c in commits) else "")
    return {"suggested": None, "prefix": prefix, "is_suggestion": True,
            "valid": None, "problems": []}


# copied from protected-files-guard/hooks/protected_files_guard.py; keep in sync (see CLAUDE.md)
def glob_re(pattern, ignore_case=True):
    """Compile a gitignore-style glob. `**/` is zero or more directories, `/**`
    everything below, `*` and `?` never cross `/`."""
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
    return re.compile(out, re.IGNORECASE if ignore_case else 0)


TEMPLATE_FILES = (".github/pull_request_template.md", "docs/pull_request_template.md",
                  "pull_request_template.md")
TEMPLATE_DIRS = (".github/PULL_REQUEST_TEMPLATE", "docs/PULL_REQUEST_TEMPLATE",
                 "PULL_REQUEST_TEMPLATE", ".gitlab/merge_request_templates")
TEMPLATE_CAP = 20000
CODEOWNERS_FILES = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS")


def _resolve(root, rel):
    """Resolve rel under root matching each component case-insensitively; None if absent."""
    cur, parts = root, []
    for comp in rel.split("/"):
        try:
            names = {n.lower(): n for n in os.listdir(cur)}
        except OSError:
            return None
        actual = names.get(comp.lower())
        if not actual:
            return None
        cur = os.path.join(cur, actual)
        parts.append(actual)
    return "/".join(parts)


def find_templates(root):
    paths = []
    for rel in TEMPLATE_FILES:
        found = _resolve(root, rel)
        if found and os.path.isfile(os.path.join(root, found)):
            paths.append(found)
    for rel in TEMPLATE_DIRS:
        found = _resolve(root, rel)
        if found and os.path.isdir(os.path.join(root, found)):
            names = sorted(n for n in os.listdir(os.path.join(root, found))
                           if n.lower().endswith(".md")
                           and os.path.isfile(os.path.join(root, found, n)))
            paths.extend(found + "/" + n for n in names)
    out = []
    for p in paths:
        try:
            with open(os.path.join(root, p), encoding="utf-8", errors="replace") as f:
                out.append({"path": p, "text": f.read()})
        except OSError:
            pass
    return {"templates": out, "choose": len(out) > 1}


def codeowners(root, paths):
    rel = next((r for r in map(lambda c: _resolve(root, c), CODEOWNERS_FILES)
                if r and os.path.isfile(os.path.join(root, r))), None)
    out = {"file": rel, "by_file": {}, "owners": []}
    if not rel:
        return out
    rules = []
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = re.sub(r"\s+#.*$", "", line).strip()
        if not line or line.startswith(("#", "[", "^[")):
            continue
        pat, *own = line.split()
        # GitHub: a bare `apps/` matches an apps directory at any depth, which glob_re
        # would anchor because of the slash.
        if pat.endswith("/") and "/" not in pat[:-1]:
            pat = pat[:-1]
        rules.append((glob_re(pat, ignore_case=False), own))
    seen = set()
    for path in paths:
        parts = path.split("/")
        cands = ["/".join(parts[:k]) for k in range(1, len(parts) + 1)]
        match = []
        for rx, own in rules:
            if any(rx.fullmatch(c) for c in cands):
                match = own
        out["by_file"][path] = match
        seen.update(match)
    out["owners"] = sorted(seen)
    return out


def detect_host(root, path=None):
    if path is None:
        path = os.environ.get("PR_WRITER_CLI_PATH")  # test seam: lets tests hide real gh/glab
    url = git(root, "remote", "get-url", "origin") or ""
    m = re.match(r"^[a-z+]+://(?:[^@/]*@)?([^/:]+)", url, re.I) or re.match(r"^(?:[^@/]+@)?([^/:]+):", url)
    remote_host = m.group(1).lower() if m else None
    order = ["glab", "gh"] if remote_host and "gitlab" in remote_host else ["gh", "glab"]
    available, cli, authed = {}, None, False
    for name in order:
        which = shutil.which(name, path=path)
        available[name] = bool(which)
        if which and cli is None:
            try:
                ok = subprocess.run([which, "auth", "status"], capture_output=True,
                                    timeout=10).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            if ok:
                cli, authed = name, True
    return {"remote_host": remote_host, "cli": cli,
            "available": {"gh": available["gh"], "glab": available["glab"]},
            "authenticated": authed}


def main(argv):
    try:
        base, ready, draft, check_title_arg = parse_args(argv)
    except ValueError as e:
        return fail("bad-arguments", str(e))
    cwd = os.getcwd()
    root = git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return fail("not-a-repo", "Not inside a git repository.")
    warnings = []
    config = load_config(root, warnings)
    if check_title_arg is not None:
        return {"ok": True, "title": check_title(check_title_arg, config), "warnings": warnings}
    branch = git(root, "symbolic-ref", "--short", "-q", "HEAD")
    if not branch:
        return fail("detached-head", "HEAD is detached. Switch to a feature branch.",
                    warnings=warnings, repo_root=root, config=config)
    if is_protected(branch, load_rules(root)):
        return fail("protected-branch",
                    "'%s' is a protected branch. Switch to a feature branch." % branch,
                    warnings=warnings, repo_root=root, branch=branch, config=config)
    out = {"ok": True, "error": None, "warnings": warnings, "repo_root": root,
           "branch": branch, "config": config}
    base_info = resolve_base(root, base)
    if not base_info:
        return fail("base-not-found",
                    "Base branch '%s' not found." % base if base
                    else "No base branch found (tried origin/HEAD, main, master). Pass --base.",
                    warnings=warnings, repo_root=root, branch=branch, config=config)
    ref = base_info["ref"]
    mb = git(root, "merge-base", ref, "HEAD")
    if not mb:
        return fail("base-not-found", "No common ancestor with '%s'." % ref,
                    warnings=warnings, repo_root=root, branch=branch, config=config)
    if git(root, "rev-list", "--count", mb + "..HEAD") == "0":
        return fail("no-commits-ahead", "No commits ahead of '%s'." % ref,
                    warnings=warnings, repo_root=root, branch=branch, config=config,
                    base=base_info)
    counts = (git(root, "rev-list", "--left-right", "--count", ref + "...HEAD") or "0\t0").split()
    commit_list, truncated = commits(root, mb)
    redactions = {}
    for c in commit_list:
        c["subject"], c["body"] = redact(c["subject"], redactions), redact(c["body"], redactions)
        check_injection(c["subject"] + "\n" + c["body"], "commit " + c["hash"][:7], warnings)
    dirty = bool(git(root, "status", "--porcelain"))
    if dirty:
        warnings.append("uncommitted changes are not included")
    out.update(base=base_info, merge_base=mb, ahead=int(counts[1]), behind=int(counts[0]),
               dirty=dirty, commits=commit_list, commits_truncated=truncated,
               breaking_changes=[
                   {"hash": c["hash"], "subject": c["subject"],
                    "note": (BREAKING_FOOTER.search(c["body"]) or [None, None])[1] or None}
                   for c in commit_list if c["breaking"]])
    out.update(push_state(root, branch))
    info = diff_info(root, mb, config["diffSizeCap"])
    sanitize_diff(info, redactions, warnings)
    out.update(diff=info, redactions=redactions)
    out.update(issues=issue_refs(branch, commit_list), title=suggest_title(commit_list, config),
               draft=False if ready else True if draft else config["draftByDefault"],
               body_file=os.path.join(tempfile.gettempdir(),
                                      "pr-writer-" + re.sub(r"[^A-Za-z0-9._-]", "-", branch) + ".md"))
    found = find_templates(root)
    for t in found["templates"]:
        t["text"] = redact(t["text"], redactions)  # before the cap, so a cut can't split a secret
        if len(t["text"]) > TEMPLATE_CAP:
            t["text"] = t["text"][:TEMPLATE_CAP]
            warnings.append("template %s truncated to %d chars" % (t["path"], TEMPLATE_CAP))
        check_injection(t["text"], "template " + t["path"], warnings)
    out.update(found, codeowners=codeowners(root, [f["path"] for f in info["files"]]),
               host=detect_host(root))
    return out


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
    except Exception as e:  # always emit one JSON object
        result = {"ok": False, "error": {"code": "internal-error", "message": str(e)},
                  "warnings": []}
    print(json.dumps(result))
    sys.exit(0)
