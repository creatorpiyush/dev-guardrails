#!/usr/bin/env python3
"""Collect read-only facts for the session-recap skill.

Prints exactly one JSON object on stdout and always exits 0. Failures are
reported as {"ok": false, "error": {"code", "message"}}. Never writes files.

Standard library only. Python 3.9+.
"""
import argparse
import json
import os
import subprocess
import sys

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
        args = p.parse_args(argv)
    except ArgError as e:
        return fail("bad-arguments", str(e))
    root = git(os.getcwd(), "rev-parse", "--show-toplevel")
    if not root:
        return fail("not-a-repo", "not inside a git repository")
    cfg, warnings = load_config(root)
    return {"ok": True, "config": cfg, "warnings": warnings,
            "save_requested": args.save}


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
    except Exception as e:  # always emit one JSON object
        result = fail("internal-error", str(e))
    print(json.dumps(result))
    sys.exit(0)
