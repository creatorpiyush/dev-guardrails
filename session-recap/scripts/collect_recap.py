#!/usr/bin/env python3
"""Collect read-only facts for the session-recap skill.

Prints exactly one JSON object on stdout and always exits 0. Failures are
reported as {"ok": false, "error": {"code", "message"}}. Never writes files.

Standard library only. Python 3.9+.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
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
        commit, source = resolve_since(root, args.since, args.base or cfg["baseBranch"], rows)
    except Failure as e:
        return fail(e.code, str(e))
    try:
        changes = change_facts(root, commit)
    except Failure as e:
        return fail(e.code, str(e))
    return {
        "ok": True, "config": cfg, "warnings": warnings, "save_requested": args.save,
        "since": {"commit": commit, "source": source},
        "session": {"id": log.stem if log else None,
                    "log_path": str(log) if log else None, "events": len(rows)},
        "changes": changes,
        "empty": not changes["files"] and not changes["commits"],
    }


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
    except Exception as e:  # always emit one JSON object
        result = fail("internal-error", str(e))
    print(json.dumps(result))
    sys.exit(0)
