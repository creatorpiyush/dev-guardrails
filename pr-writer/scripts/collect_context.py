#!/usr/bin/env python3
"""Collect read-only git context for the pr-writer skill.

Prints exactly one JSON object on stdout and always exits 0. Failures are
reported as {"ok": false, "error": {"code", "message"}}.

Standard library only. Python 3.9+. No network.
"""
import argparse
import json
import os
import subprocess
import sys

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


def main(argv):
    try:
        base, ready, draft, check_title = parse_args(argv)
    except ValueError as e:
        return fail("bad-arguments", str(e))
    cwd = os.getcwd()
    root = git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return fail("not-a-repo", "Not inside a git repository.")
    warnings = []
    config = load_config(root, warnings)
    # Task 4: --check-title mode returns here, before the branch checks.
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
    # Tasks 2-5 fill the remaining keys here.
    return out


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
    except Exception as e:  # always emit one JSON object
        result = {"ok": False, "error": {"code": "internal-error", "message": str(e)},
                  "warnings": []}
    print(json.dumps(result))
    sys.exit(0)
