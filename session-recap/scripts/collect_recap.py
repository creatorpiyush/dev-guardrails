#!/usr/bin/env python3
"""Collect read-only facts for the session-recap skill.

Prints exactly one JSON object on stdout and always exits 0. Failures are
reported as {"ok": false, "error": {"code", "message"}}. Never writes files.

Standard library only. Python 3.9+.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shlex
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# -- config ------------------------------------------------------------------

CONFIG_FILE = os.path.join(".claude", "session-recap.json")
DEFAULT_CONFIG = {
    "baseBranch": "main",
    "retentionDays": 14,
    "maxWords": 250,
    "autoRecap": False,
    "testCommandKinds": ["pytest", "jest", "go test"],
}


def _int_in(lo, hi):
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


VALIDATORS = {
    "baseBranch": lambda v: isinstance(v, str) and bool(v),
    "retentionDays": _int_in(1, 365),
    "maxWords": _int_in(50, 1000),
    "autoRecap": lambda v: isinstance(v, bool),
    "testCommandKinds": lambda v: isinstance(v, list)
    and all(isinstance(x, str) and x for x in v),
}


def load_config(repo_root):
    """Return (config, warnings). Any invalid value drops the whole file."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = os.path.join(repo_root, CONFIG_FILE)
    if not os.path.exists(path):
        return cfg, []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config must be an object")
    except (OSError, ValueError) as e:
        return cfg, ["%s ignored: %s" % (CONFIG_FILE, e)]
    warnings = ["%s: unknown key %s" % (CONFIG_FILE, k)
                for k in data if k not in VALIDATORS]
    for key, value in data.items():
        if key in VALIDATORS and not VALIDATORS[key](value):
            return cfg, ["%s ignored: invalid value for %s" % (CONFIG_FILE, key)]
    cfg.update({k: v for k, v in data.items() if k in VALIDATORS})
    return cfg, warnings


# -- git ---------------------------------------------------------------------

def git(cwd, *args):
    """Run read-only git in cwd; return stripped stdout, or None on any failure."""
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=10, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


# -- log ---------------------------------------------------------------------

# Copied from ../hooks/log_activity.py; keep in sync (tests/test_copies_in_sync.py).
def log_dir(home, root):
    digest = hashlib.sha256(os.path.realpath(root).encode("utf-8")).hexdigest()[:16]
    return Path(home) / ".claude" / "session-recap" / digest


def safe_session(sid):
    name = re.sub(r"[^A-Za-z0-9_-]", "_", str(sid or ""))[:128]
    return name or "unknown"


def find_log(home, root, session):
    """The --session log if it exists, else the newest log by mtime."""
    d = log_dir(home, root)
    if session:
        p = d / (safe_session(session) + ".jsonl")
        return p if p.is_file() else None
    try:
        logs = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return logs[-1] if logs else None


def read_log(path):
    """Rows (JSON objects) sorted by ts; anything else is skipped."""
    rows = []
    if path is None:
        return rows
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    rows.sort(key=lambda r: str(r.get("ts", "")))
    return rows


# -- since / changes ---------------------------------------------------------

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?(Z|[+-]\d{2}:\d{2})?$")
REL_TIME = re.compile(r"^\d+[ .](second|minute|hour|day|week|month|year)s?[ .]ago$")


class Failure(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


def before(root, when):
    """Last commit at or before `when`, or EMPTY_TREE if none."""
    return git(root, "rev-list", "-1", "--before=" + when, "HEAD") or EMPTY_TREE


def resolve_since(root, since, base, rows):
    """Return (commit, source)."""
    if since:
        if git(root, "rev-parse", "--verify", "--quiet", since + "^{commit}"):
            return git(root, "rev-parse", since + "^{commit}"), "ref"
        if ISO_TIME.match(since) or REL_TIME.match(since):
            return before(root, since), "time"
        raise Failure("since-not-found", "%r is not a commit or a time" % since)
    first = next((r["ts"] for r in rows if isinstance(r.get("ts"), str)), None)
    if first:
        return before(root, first), "session-start"
    if not git(root, "rev-parse", "--verify", "--quiet", "HEAD"):
        return EMPTY_TREE, "empty-tree"
    mb = git(root, "merge-base", "HEAD", base)
    if not mb:
        raise Failure("base-not-found", "no merge base with %r" % base)
    return mb, "merge-base"


def default_base(root, base, warnings):
    """The configured base, or origin/HEAD's branch, master, or main when it doesn't exist.

    Only for a base the user didn't pass with --base, which stays strict."""
    def exists(ref):
        return git(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}") is not None
    if exists(base):
        return base
    origin = git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    for ref in ([origin] if origin else []) + ["master", "main"]:
        if ref != base and exists(ref):
            warnings.append("baseBranch %r not found; using %r" % (base, ref))
            return ref
    return base


MAX_COUNT_BYTES = 10 * 1024 * 1024


def count_lines(path):
    """(lines, binary) for an untracked file; (None, False) when over the cap.
    Only regular files are read; symlinks, FIFOs and the like count as 0."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return 0, False
        if st.st_size > MAX_COUNT_BYTES:
            return None, False
        lines, last, first = 0, b"", True
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                if first and b"\0" in chunk[:8000]:
                    return 0, True
                first = False
                lines += chunk.count(b"\n")
                last = chunk[-1:]
    except OSError:
        return 0, False
    return lines + (1 if last and last != b"\n" else 0), False


STATUS = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "T": "modified"}


def change_facts(root, since):
    def run(step, *args):
        out = git(root, *args)
        if out is None:
            raise Failure("internal-error", "git %s failed" % step)
        return out

    commits = []
    if git(root, "rev-parse", "--verify", "--quiet", "HEAD"):
        rng = ["HEAD"] if since == EMPTY_TREE else [since + "..HEAD"]
        log = run("log", "log", "--format=%H%x00%s%x01", *rng)
        for rec in log.split("\x01"):
            sha, _, subject = rec.strip().partition("\x00")
            if sha:
                commits.append({"sha": sha[:12], "subject": subject})
    files = {}
    t = run("diff --name-status", "diff", "-M", "--name-status", "-z", since).split("\0")
    i = 0
    while i < len(t) and t[i]:
        st = t[i][0]
        if st in "RC":
            old, path, i = t[i + 1], t[i + 2], i + 3
        else:
            old, path, i = None, t[i + 1], i + 2
        f = {"path": path, "status": STATUS.get(st, "modified")}
        if old:
            f["old_path"] = old
        f.update(insertions=0, deletions=0, binary=False)
        files[path] = f
    n = run("diff --numstat", "diff", "-M", "--numstat", "-z", since).split("\0")
    i = 0
    while i < len(n) and n[i]:
        ins, dele, path = n[i].split("\t", 2)
        i += 1
        if not path:  # rename: old and new paths follow as separate fields
            path, i = n[i + 1], i + 2
        if path in files:
            if ins == "-":
                files[path]["binary"] = True
            else:
                files[path]["insertions"], files[path]["deletions"] = int(ins), int(dele)
    for path in run("ls-files", "ls-files", "-o", "--exclude-standard", "-z").split("\0"):
        if path and path not in files:
            lines, binary = count_lines(os.path.join(root, path))
            files[path] = {"path": path, "status": "added", "insertions": lines,
                           "deletions": 0, "binary": binary}
            if lines is None:
                files[path]["too_large"] = True
    out = sorted(files.values(), key=lambda f: f["path"])
    by_area, by_type = {}, {}
    for f in out:
        area = f["path"].split("/")[0] if "/" in f["path"] else "."
        kind = os.path.splitext(f["path"])[1].lstrip(".") or os.path.basename(f["path"])
        by_area[area] = by_area.get(area, 0) + 1
        by_type[kind] = by_type.get(kind, 0) + 1
    return {
        "commits": commits, "files": out,
        "totals": {"files": len(out),
                   "insertions": sum(f["insertions"] or 0 for f in out),
                   "deletions": sum(f["deletions"] for f in out)},
        "by_area": by_area, "by_type": by_type,
    }


# -- impact signals ----------------------------------------------------------

# Never read: matched on the lowercased basename. Listed by path only.
SECRET_NAMES = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.p12", "credentials.json")
SECRET_ALLOW = (".env.example", ".env.sample", ".env.template")
MANIFESTS = ("package.json", "pyproject.toml", "requirements*.txt", "setup.py", "setup.cfg",
             "go.mod", "Cargo.toml", "Gemfile", "composer.json", "pom.xml", "build.gradle*")
LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
             "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock")
CI_PATHS = (".github/workflows/*", ".gitlab-ci.yml", ".circleci/*", "Jenkinsfile",
            "azure-pipelines.yml", ".buildkite/*")
INFRA_PATHS = ("Dockerfile*", "docker-compose*", "compose.y*ml", "*.tf", "Chart.yaml",
               "k8s/*", "helm/*")
MIGRATION_PATHS = ("*migrations/*", "*migrate/*", "*alembic/*", "schema.prisma",
                   "*/schema.prisma", "db/schema.rb", "*/db/schema.rb", "*.sql")
CONFIG_EXTS = (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf")
AUTH_WORDS = re.compile(
    r"auth|authn|authz|authentication|authorization|permissions?|rbac|acl|oauth2?|login|"
    r"sessions?|security|polic(?:y|ies)|codeowners", re.I)
MAX_IMPORTERS = 20
LANG_EXTS = {
    "py": (".py",), "js": (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"), "go": (".go",),
    "rs": (".rs",), "public": (".java", ".kt", ".cs"),
}
# (regex, name group, rest group or None); matched against one diff line without its +/-.
API_PATTERNS = {
    "py": re.compile(r"^(?:async\s+def|def|class)\s+([A-Za-z]\w*)(.*)"),
    "js": re.compile(r"^export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var|"
                     r"interface|type|enum)\s+(\w+)(.*)"),
    "go": re.compile(r"^func\s+([A-Z]\w*)(.*)"),
    "rs": re.compile(r"^pub\s+(?:async\s+)?(?:fn|struct|enum|trait)\s+(\w+)(.*)"),
    "public": re.compile(r"^\s*public\s+(?:[^(={;]*?\s)?(\w+)\s*([(={;].*|$)"),
}
ENV_PATTERNS = [re.compile(p) for p in (
    r"os\.environ\[\s*['\"](\w+)", r"os\.getenv\(\s*['\"](\w+)", r"environ\.get\(\s*['\"](\w+)",
    r"process\.env\.(\w+)", r"process\.env\[\s*['\"](\w+)", r"^\s*(?:ENV|ARG)\s+(\w+)")]
DEP_NON_NAMES = {"name", "version", "description", "main", "license", "author"}
VERSION = re.compile(r"[\^~<>=!v]*\d")
DEP_JSON = re.compile(r'^\s*"([^"]+)"\s*:\s*"([^"]*)"\s*,?\s*$')
DEP_PIP = re.compile(r"""^\s*["']?([A-Za-z0-9_.\-]+)\s*(?:==|>=|<=|~=|!=|>|<)\s*([^\s;#,"']+)""")
DEP_TOML = re.compile(r'^\s*([A-Za-z0-9_.\-]+)\s*=\s*"([^"]*)"')
DEP_GO = re.compile(r"^\s*(?:require\s+)?([\w.\-]+\.[\w.\-/]+)\s+(v\d[\w.\-+]*)")


def matches(path, patterns):
    base = posixpath.basename(path)
    return any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(base, p) for p in patterns)


def is_secret(path):
    name = posixpath.basename(path).lower()
    return name not in SECRET_ALLOW and any(fnmatch.fnmatchcase(name, p) for p in SECRET_NAMES)


def read_regular(path):
    """Text of a small regular, non-binary file, else None (same bounds as count_lines)."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_COUNT_BYTES:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return None if b"\0" in data[:8000] else data.decode("utf-8", "replace")


def diff_lines(root, since, f, warnings):
    """(added, removed) line lists, or None when content can't be read.
    Untracked files have no git diff, so they are read as all-added."""
    paths = [p for p in (f.get("old_path"), f["path"]) if p]
    out = git(root, "diff", "-U0", "--no-ext-diff", "--no-textconv", "-M", since, "--", *paths)
    if out is None:
        warnings.append("impact: git diff failed for %s; content signals skipped" % f["path"])
        return None
    if not out and f["status"] == "added":
        text = read_regular(os.path.join(root, f["path"]))
        return (text.splitlines(), []) if text else None
    add, rem = [], []
    for line in out.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            add.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            rem.append(line[1:])
    return add, rem


def lang_of(path):
    ext = posixpath.splitext(path)[1].lower()
    return next((k for k, exts in LANG_EXTS.items() if ext in exts), None)


def api_names(lang, lines):
    names = {}
    for line in lines:
        m = API_PATTERNS[lang].match(line)
        if m:
            names[m.group(1)] = m.group(2).strip()
    return names


def dep_versions(lines):
    deps = {}
    for line in lines:
        for rx in (DEP_JSON, DEP_PIP, DEP_GO):
            m = rx.match(line)
            if m and m.group(1) not in DEP_NON_NAMES and (
                    rx is DEP_PIP or VERSION.match(m.group(2))):
                deps[m.group(1)] = m.group(2)
                break
    return deps


TOML_MANIFESTS = ("pyproject.toml", "Cargo.toml")
TOML_TABLES = ("dependencies", "dev-dependencies", "build-dependencies", "target.*.dependencies",
               "tool.poetry.dependencies", "tool.poetry.dev-dependencies",
               "tool.poetry.group.*.dependencies")
TOML_ARRAYS = ("project.optional-dependencies", "dependency-groups")
DEP_INLINE = re.compile(r'^\s*([A-Za-z0-9_.\-]+)\s*=\s*\{.*\bversion\s*=\s*"([^"]*)"')


def toml_deps(text):
    """{name: version} from dependency sections only; other tables are never read."""
    deps, section, in_arr = {}, "", False
    for line in text.splitlines():
        h = re.match(r"^\s*\[+\s*([^\]]+?)\s*\]+", line)
        if h:
            section, in_arr = h.group(1), False
            continue
        if section == "project" and not in_arr:
            in_arr = bool(re.match(r"^\s*dependencies\s*=\s*\[", line))
            if not in_arr:
                continue
        if section == "project" or section in TOML_ARRAYS:
            for spec in re.findall(r'"([^"]+)"', line):
                m = DEP_PIP.match(spec)
                if m:
                    deps[m.group(1)] = m.group(2)
            if section == "project" and "]" in line.split("=", 1)[-1]:
                in_arr = False
        elif matches(section, TOML_TABLES):
            m = DEP_TOML.match(line) or DEP_INLINE.match(line)
            if m and m.group(1) not in DEP_NON_NAMES and VERSION.match(m.group(2)):
                deps[m.group(1)] = m.group(2)
    return deps


def toml_dep_pair(root, since, f, warnings):
    """(new, old) dependency dicts from whole files, or None if either can't be read."""
    text = read_regular(os.path.join(root, f["path"]))
    old = ""
    if f["status"] != "added" and since != EMPTY_TREE:
        old = git(root, "show", "%s:%s" % (since, f.get("old_path") or f["path"]))
    if text is None or old is None:
        warnings.append("impact: could not read %s for dependencies" % f["path"])
        return None
    return toml_deps(text), toml_deps(old)


def content_signals(path, add, rem, toml_pair=None):
    """Yield (signal, evidence) from changed lines; evidence is derived facts only."""
    lang = lang_of(path)
    if lang and not is_test_file(path):  # a test's functions are not an API
        a, r = api_names(lang, add), api_names(lang, rem)
        for n in sorted(set(a) - set(r)):
            yield "public-api", "+" + n
        for n in sorted(set(r) - set(a)):
            yield "public-api", "-" + n
        for n in sorted(set(a) & set(r)):
            if a[n] != r[n] and a[n].startswith("(") and r[n].startswith("("):
                yield "signature", n
    if matches(path, MANIFESTS):
        if posixpath.basename(path) in TOML_MANIFESTS:
            new, old = toml_pair or ({}, {})
        else:
            new, old = dep_versions(add), dep_versions(rem)
        found = False
        for n in sorted(set(new) | set(old)):
            if n in new and n in old:
                if new[n] != old[n]:
                    found = True
                    yield "dependency", "%s %s→%s" % (n, old[n], new[n])
            elif n in new:
                found = True
                yield "dependency", "+%s %s" % (n, new[n])
            else:
                found = True
                yield "dependency", "-" + n
        if not found:
            yield "dependency", "manifest changed"
    names = sorted({m.group(1) for line in add + rem for rx in ENV_PATTERNS
                    for m in [rx.search(line)] if m})
    for n in names:
        yield "env-var", n


def import_matcher(f):
    """(grep stem, predicate(importer_path, text)) for a changed source file, or None."""
    path, lang = f["path"], lang_of(f["path"])
    stem = posixpath.splitext(posixpath.basename(path))[0]
    if lang == "py":
        parts = posixpath.splitext(path)[0].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            return None
        stem = parts[-1]
        mods = [".".join(parts[i:]) for i in range(len(parts))]
        rx = []
        for m in mods:
            rx.append(r"^\s*(?:from\s+%s\s+import|import\s+%s\b)" % ((re.escape(m),) * 2))
            if "." in m:
                parent = m.rsplit(".", 1)[0]
                rx.append(r"^\s*from\s+%s\s+import\s+.*\b%s\b" % (re.escape(parent), re.escape(stem)))
        rx = re.compile("|".join(rx), re.M)
        # ponytail: bare-name suffixes can match an unrelated top-level module of the same name.
        return stem, "py", lambda ip, text: bool(rx.search(text))
    if lang == "js":
        noext = posixpath.splitext(path)[0]
        targets = {path, noext}
        if stem == "index":
            targets.add(posixpath.dirname(path))
        spec = re.compile(r"""(?:from|import|require)\s*\(?\s*['"](\.{1,2}/[^'"]*|\.{1,2})['"]""")

        def js(ip, text):
            for s in spec.findall(text):
                r = posixpath.normpath(posixpath.join(posixpath.dirname(ip), s))
                if r in targets or posixpath.splitext(r)[0] in targets:
                    return True
            return False
        return stem, "js", js
    if lang == "go":
        d = posixpath.dirname(path)
        if not d:
            return None
        rx = re.compile(r'"(?:[^"]*/)?%s"' % re.escape(d))
        return posixpath.basename(d), "go", lambda ip, text: (
            posixpath.dirname(ip) != d and bool(rx.search(text)))
    return None


def importers(root, f, changed):
    """(sorted importer paths capped at MAX_IMPORTERS, truncated) one hop out."""
    m = import_matcher(f)
    if not m:
        return [], False
    stem, lang, confirm = m
    out = git(root, "grep", "-l", "-F", "-z", "--", stem)
    found = []
    for ip in sorted(set((out or "").split("\0"))):
        if (not ip or ip in changed
                or posixpath.splitext(ip)[1].lower() not in LANG_EXTS[lang] or is_secret(ip)):
            continue
        text = read_regular(os.path.join(root, ip))
        if text and confirm(ip, text):
            found.append(ip)
            if len(found) > MAX_IMPORTERS:
                break
    return found[:MAX_IMPORTERS], len(found) > MAX_IMPORTERS


def path_signals(path, status):
    """Yield (signal, evidence) decided by the path alone."""
    if status == "deleted":
        yield "deleted", "file removed"
    if matches(path, LOCKFILES):
        yield "lockfile", "lockfile changed"
    if matches(path, CI_PATHS):
        yield "ci", "CI config changed"
    if matches(path, INFRA_PATHS):
        yield "infra", "infra file changed"
    if matches(path, MIGRATION_PATHS):
        yield "migration", "migration or schema file"
    base = posixpath.basename(path)
    if (posixpath.splitext(path)[1].lower() in CONFIG_EXTS or base == ".env.example"
            or base.startswith("settings.")) and not (
            matches(path, MANIFESTS + LOCKFILES + CI_PATHS)):
        yield "config", "config file changed"
    segs = [t for seg in path.split("/") for t in re.split(r"[._\-]", seg) if t]
    if any(AUTH_WORDS.fullmatch(t) for t in segs):
        yield "auth", "auth-related path"


def impact_signals(root, since, files, warnings=None):
    """[{"signal", "file", "evidence"}] for changed files. Secret files: path only."""
    warnings = [] if warnings is None else warnings
    items = []
    changed = {f["path"] for f in files}
    for f in files:
        path = f["path"]
        if is_secret(path) or is_secret(f.get("old_path") or ""):
            items.append({"signal": "config", "file": path,
                          "evidence": "secret file; contents not read"})
            if f["status"] == "deleted":
                items.append({"signal": "deleted", "file": path, "evidence": "file removed"})
            continue
        mine = []
        for sig_, ev in path_signals(path, f["status"]):
            mine.append({"signal": sig_, "file": path, "evidence": ev})
        if f["status"] != "deleted" and not f.get("binary") and not f.get("too_large") \
                and not matches(path, LOCKFILES):
            lines = diff_lines(root, since, f, warnings)
            if lines:
                pair = (toml_dep_pair(root, since, f, warnings)
                        if posixpath.basename(path) in TOML_MANIFESTS else None)
                for sig_, ev in content_signals(path, *lines, toml_pair=pair):
                    mine.append({"signal": sig_, "file": path, "evidence": ev})
        found, cut = importers(root, f, changed)
        for ip in found:
            item = {"signal": "importer", "file": path, "evidence": ip}
            if cut:
                item["truncated"] = True
            mine.append(item)
        items += cap_per_signal(mine, path)
    return items


SIGNAL_CAP = 10  # items per file per signal


def cap_per_signal(items, path):
    """Keep the first SIGNAL_CAP items of each signal; add one summary item per cut signal."""
    kept, seen, cut = [], {}, {}
    for i in items:
        n = seen[i["signal"]] = seen.get(i["signal"], 0) + 1
        if n <= SIGNAL_CAP or i["signal"] == "importer":  # importers: own cap of 20
            kept.append(i)
        else:
            cut[i["signal"]] = cut.get(i["signal"], 0) + 1
    for sig_, n in cut.items():
        kept.append({"signal": sig_, "file": path, "evidence": "%d more not shown" % n,
                     "truncated": n})
    return kept


# -- test facts, efficiency, project facts, previous recap -------------------

TEST_DIRS = {"tests", "test", "__tests__", "spec"}
TEST_STEM = re.compile(r"^test_|_test$|\.test$|\.spec$")
RUN_EVENTS = ("post", "fail")
LIST_CAP = 20  # efficiency lists; the cut count goes in efficiency.truncated
KIND_NAMES = ("test", "lint", "typecheck", "build")
RUNNER_PREFIXES = (("npx",), ("uv", "run"), ("poetry", "run"))


def is_test_file(path):
    parts = path.split("/")
    base = parts[-1]
    return (bool(re.match(r"^test_.*\.py$|.*_test\.(py|go)$|.*\.(test|spec)\.[^.]+$", base))
            or any(p in TEST_DIRS for p in parts[:-1]))


def test_stem(path):
    return TEST_STEM.sub("", posixpath.splitext(posixpath.basename(path))[0])


def is_pass(r):
    return r.get("event") == "post" and r.get("exit", 0) == 0


def runs_of(rows, kinds=KIND_NAMES):
    return [r for r in rows if r.get("event") in RUN_EVENTS and r.get("kind") in kinds
            and isinstance(r.get("command"), str)]


def test_facts(files, rows):
    tests = [f["path"] for f in files if is_test_file(f["path"])]
    stems = {test_stem(p) for p in tests}
    untested = [f["path"] for f in files if f["status"] != "deleted" and lang_of(f["path"])
                and not is_test_file(f["path"]) and posixpath.splitext(
                    posixpath.basename(f["path"]))[0] not in stems]
    commands, kinds = {}, set()
    for r in runs_of(rows):
        c = commands.setdefault(r["command"], {
            "kind": r["kind"], "runs": 0, "passes": 0, "failures": 0,
            "fail_then_pass": False, "total_ms": 0})
        c["runs"] += 1
        if is_pass(r):
            c["passes"] += 1
            c["fail_then_pass"] = c["fail_then_pass"] or c["failures"] > 0
        else:
            c["failures"] += 1
        c["total_ms"] += r.get("duration_ms") if isinstance(r.get("duration_ms"), int) else 0
        kinds.add(("integration" if r.get("integration") else "unit")
                  if r["kind"] == "test" else r["kind"])
    test_ts = [str(r.get("ts", "")) for r in runs_of(rows, ("test",))]
    edit_ts = [str(r.get("ts", "")) for r in rows if r.get("event") == "post" and r.get("path")]
    last_test, last_edit = max(test_ts, default=None), max(edit_ts, default=None)
    return {
        "test_files_changed": tests, "untested_files": untested, "commands": commands,
        "kinds_ran": sorted(kinds), "last_test_ts": last_test, "last_edit_ts": last_edit,
        "stale": bool(last_test and last_edit and last_edit > last_test),
        "status": "recorded" if test_ts else "no test run recorded",
    }


def runner_args(command):
    """(runner, args, head) for 'python -m pytest a' -> ('pytest', ['a'], 'python -m pytest');
    (None, None, None) if unparseable or empty. head keeps the runner text as typed."""
    try:
        t = orig = shlex.split(command)
    except ValueError:
        return None, None, None
    if not t:
        return None, None, None
    while t and "=" in t[0] and not t[0].startswith("-"):
        t = t[1:]
    for pre in RUNNER_PREFIXES:
        if tuple(t[:len(pre)]) == pre:
            t = t[len(pre):]
    if t and posixpath.basename(t[0]).startswith("python") and t[1:2] == ["-m"]:
        t = t[2:]
    elif t:
        t = [posixpath.basename(t[0])] + t[1:]
    n = 2 if t[1:2] in (["test"], ["run"], ["t"]) else 1
    return " ".join(t[:n]), t[n:], " ".join(orig[:len(orig) - len(t) + n])


def is_full_suite(command, root):
    args = runner_args(command)[1]  # None: unparseable or empty
    if args is None:
        return False
    for a in args:
        if a in ("-k", "-t", "-run") or "::" in a or a.startswith(("-k", "-run=")):
            return False
        if a.startswith("-"):
            continue
        # Go's `./pkg/...` names the dir `./pkg`. Strip it here: Windows drops trailing dots, so
        # `./...` would otherwise "exist". The repo root itself (`.`, `./...`) selects nothing.
        path = os.path.normpath(os.path.join(root, a[:-4] if a.endswith("/...") else a))
        if path != os.path.normpath(root) and os.path.exists(path):
            return False
    return True


def suggest_targeted(command, test_paths):
    """Runner plus changed test files, only for runners that take file arguments."""
    runner, _, head = runner_args(command)
    if runner in ("pytest", "py.test"):
        exts = (".py",)
    elif runner in ("jest", "vitest", "mocha"):
        exts = LANG_EXTS["js"]
    elif runner == "go test":
        dirs = sorted({"./" + d + "/..." if d else "./..."
                       for d in (posixpath.dirname(p) for p in test_paths if p.endswith("_test.go"))})
        return " ".join([head] + dirs) if dirs else None
    else:
        return None
    paths = [p for p in test_paths if p.endswith(exts)]
    return " ".join([head] + paths) if paths else None


def efficiency(rows, files, root=None):
    root = root or os.getcwd()
    stamps = [str(r["ts"]) for r in rows if isinstance(r.get("ts"), str)]
    span = 0
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        span = int((datetime.strptime(max(stamps), fmt) - datetime.strptime(min(stamps), fmt))
                   .total_seconds()) if stamps else 0
    except ValueError:
        pass
    timed = [r for r in rows if r.get("event") in RUN_EVENTS and isinstance(r.get("command"), str)
             and isinstance(r.get("duration_ms"), int)]
    longest = [{"command": r["command"], "duration_ms": r["duration_ms"]}
               for r in sorted(timed, key=lambda r: -r["duration_ms"])[:5]]
    edits = {}
    for r in rows:
        if r.get("event") == "post" and r.get("path"):
            edits[r["path"]] = edits.get(r["path"], 0) + 1
    fails = {}
    for r in rows:
        if r.get("event") == "fail" and isinstance(r.get("command"), str):
            fails[r["command"]] = fails.get(r["command"], 0) + 1
    done = {r.get("id") for r in rows if r.get("event") in RUN_EVENTS}
    no_result = [{k: r[k] for k in ("tool", "command", "path", "ts") if k in r}
                 for r in rows if r.get("event") == "pre" and r.get("id") not in done
                 and "collect_recap.py" not in str(r.get("command", ""))]  # our own call
    test_paths = [f["path"] for f in files if is_test_file(f["path"])]
    full = []
    for r in runs_of(rows, ("test",)):
        if is_full_suite(r["command"], root):
            full.append({"command": r["command"], "ts": r.get("ts"),
                         "suggested": suggest_targeted(r["command"], test_paths)})
    lists = {
        "files_edited_3plus": [{"path": p, "edits": n} for p, n in edits.items() if n >= 3],
        "no_result": no_result,
        "denied": [r for r in rows if r.get("event") == "denied"],
        "installs": [r["command"] for r in rows if r.get("event") in RUN_EVENTS
                     and r.get("kind") == "install" and isinstance(r.get("command"), str)],
    }
    truncated = {k: len(v) - LIST_CAP for k, v in lists.items() if len(v) > LIST_CAP}
    lists = {k: v[:LIST_CAP] for k, v in lists.items()}
    out = {
        "active_span_s": span, "longest_commands": longest,
        "files_edited_3plus": lists["files_edited_3plus"],
        "repeated_failures": [{"command": c, "failures": n} for c, n in fails.items() if n >= 2],
        "no_result": {"label": "blocked, denied, or interrupted (inferred)",
                      "items": lists["no_result"]},
        "denied": lists["denied"], "installs": lists["installs"],
        "full_suite_runs": full,
    }
    if truncated:
        out["truncated"] = truncated  # {list name: how many were cut}
    return out


def read_text(root, name):
    return None if is_secret(name) else read_regular(os.path.join(root, name))


def test_command(root):
    pkg = read_text(root, "package.json")
    try:
        if pkg and isinstance(json.loads(pkg).get("scripts", {}).get("test"), str):
            return "npm test", "package.json"
    except (ValueError, AttributeError):
        pass
    for name, cmd in (("Makefile", "make test"), ("justfile", "just test")):
        text = read_text(root, name)
        if text and re.search(r"^test\s*:", text, re.M):
            return cmd, name
    for name, marker in (("pytest.ini", ""), ("pyproject.toml", "[tool.pytest"),
                         ("setup.cfg", "[tool:pytest]"), ("tox.ini", "")):
        text = read_text(root, name)
        if text is not None and marker in text:
            return "pytest", name
    for name, cmd in (("go.mod", "go test ./..."), ("Cargo.toml", "cargo test")):
        if os.path.isfile(os.path.join(root, name)):
            return cmd, name
    return "unknown", None


def project_facts(root):
    cmd, source = test_command(root)
    words = cmd.split()
    needles = [] if cmd == "unknown" else [cmd.lower(), " ".join(words[:2]).lower()]
    instr = []
    for name in ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md"):
        text = read_text(root, name)
        if text is not None:
            low = text.lower()
            instr.append({"file": name, "mentions_test_command": any(n in low for n in needles)})
    return {"test_command": cmd, "source": source, "instructions": instr}


def previous_recap(root):
    try:
        newest = sorted(Path(root, ".claude", "recaps").glob("*.md"), key=lambda p: p.name)[-1]
    except (OSError, IndexError):
        return None
    text = read_regular(str(newest)) or ""
    bullets, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() == "next time"
        elif inside:
            m = re.match(r"\s*(?:[-*]|\d+\.)\s+(.*\S)", line)
            if m and len(bullets) < 10:
                bullets.append(m.group(1)[:300])
    return {"file": newest.name, "next_time": bullets}


# -- output ------------------------------------------------------------------

def fail(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


class ArgError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgError(message)


def main(argv):
    p = Parser(prog="collect_recap.py", add_help=False)
    p.add_argument("--since")
    p.add_argument("--base")
    p.add_argument("--session")
    p.add_argument("--save", action="store_true")
    try:
        args, arg_error = p.parse_args(argv), None
    except ArgError as e:
        args, arg_error = None, e
    root = git(os.getcwd(), "rev-parse", "--show-toplevel")
    if not root:
        return fail("not-a-repo", "not inside a git repository")
    if arg_error:
        return fail("bad-arguments", str(arg_error))
    cfg, warnings = load_config(root)
    home = Path.home()
    log = find_log(home, root, args.session)
    rows = read_log(log)
    try:
        base = args.base or default_base(root, cfg["baseBranch"], warnings)
        commit, source = resolve_since(root, args.since, base, rows)
    except Failure as e:
        return fail(e.code, str(e))
    try:
        changes = change_facts(root, commit)
    except Failure as e:
        return fail(e.code, str(e))
    impact = impact_signals(root, commit, changes["files"], warnings)
    return {
        "ok": True, "config": cfg, "warnings": warnings, "save_requested": args.save,
        "since": {"commit": commit, "source": source},
        "session": {"id": log.stem if log else None,
                    "log_path": str(log) if log else None, "events": len(rows)},
        "changes": changes, "impact": impact,
        "tests": test_facts(changes["files"], rows),
        "efficiency": efficiency(rows, changes["files"], root),
        "project": project_facts(root), "previous_recap": previous_recap(root),
        "empty": not changes["files"] and not changes["commits"],
    }


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
    except Exception as e:  # always emit one JSON object
        result = fail("internal-error", str(e))
    print(json.dumps(result))
    sys.exit(0)
