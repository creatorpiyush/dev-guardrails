#!/usr/bin/env python3
"""Hook: append one JSON line per tool event to a per-session activity log.

The log lives under ~/.claude/session-recap/<repo hash>/<session>.jsonl and
holds paths, edit counts, commands, exit codes, and durations. It never holds
file contents or tool output: record() reads only the fields it names.
/session-recap:summary reads it back. Old logs are pruned on every event.

Never blocks, never prints. Any error, bad input, or write failure exits 0.
SESSION_RECAP_DISABLE=1 turns it off.

Standard library only. Python 3.9+.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECT_DIR = os.path.join(HERE, "..", "scripts")

DEFAULT_RETENTION_DAYS = 14  # used only if collect_recap.py fails to import
COMMAND_CAP = 500
SHELL_TOOLS = {"Bash", "PowerShell"}
EXIT_CODE = re.compile(r"Exit code (\d+)")


# -- paths -------------------------------------------------------------------

def repo_root(cwd):
    """Realpath of the repo's top level, or None outside a repo."""
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                           capture_output=True, text=True, timeout=5,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return os.path.realpath(out) if r.returncode == 0 and out else None


def log_dir(home, root):
    digest = hashlib.sha256(os.path.realpath(root).encode("utf-8")).hexdigest()[:16]
    return Path(home) / ".claude" / "session-recap" / digest


def safe_session(sid):
    name = re.sub(r"[^A-Za-z0-9_-]", "_", str(sid or ""))[:128]
    return name or "unknown"


def rel_path(path, root, cwd):
    """Path relative to the repo root, or absolute when outside it."""
    if not os.path.isabs(path):
        path = os.path.join(cwd or root, path)
    real = os.path.realpath(path)
    rel = os.path.relpath(real, root) if _inside(real, root) else real
    return rel.replace(os.sep, "/")


def _inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


# -- record ------------------------------------------------------------------

def record(payload, root, cfg):
    """One log row for a PostToolUse / PostToolUseFailure payload, else None.

    Reads only tool_name, tool_use_id, duration_ms, tool_input.file_path /
    notebook_path / edits / command, tool_response.interrupted, error (first
    line), is_interrupt, reason. Nothing else.
    """
    name = payload.get("hook_event_name")
    # Task 4 adds branches here for PreToolUse, PermissionDenied and Stop.
    event = {"PostToolUse": "post", "PostToolUseFailure": "fail"}.get(name)
    if event is None:
        return None
    tool = payload.get("tool_name")
    inp = payload.get("tool_input")
    inp = inp if isinstance(inp, dict) else {}
    row = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "event": event, "tool": tool}
    if payload.get("tool_use_id"):
        row["id"] = payload["tool_use_id"]

    path = inp.get("file_path") or inp.get("notebook_path")
    if isinstance(path, str) and path:
        row["path"] = rel_path(path, root, payload.get("cwd"))
        edits = inp.get("edits")
        row["edits"] = len(edits) if isinstance(edits, list) else 1
    command = inp.get("command")
    if tool in SHELL_TOOLS and isinstance(command, str):
        # Task 3 replaces these two lines with redact() and classify().
        row["command"] = command[:COMMAND_CAP]
        row["kind"] = "other"

    error = payload.get("error")
    if event == "post":
        row["exit"] = 0
    elif isinstance(error, str):
        m = EXIT_CODE.match(error.split("\n", 1)[0])
        if m:
            row["exit"] = int(m.group(1))
    if isinstance(payload.get("duration_ms"), (int, float)):
        row["duration_ms"] = payload["duration_ms"]
    resp = payload.get("tool_response")
    if payload.get("is_interrupt") is True or (
            isinstance(resp, dict) and resp.get("interrupted") is True):
        row["interrupted"] = True
    return row


# -- retention ---------------------------------------------------------------

def prune(base, days, now):
    """Delete *.jsonl older than `days` under base/*/, then empty hash dirs."""
    cutoff = now - days * 86400
    for d in Path(base).iterdir():
        if not d.is_dir():
            continue
        for f in d.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
        try:
            d.rmdir()  # fails unless empty
        except OSError:
            pass


# -- main --------------------------------------------------------------------

def retention_days(root):
    try:
        sys.path.insert(0, COLLECT_DIR)
        import collect_recap  # the plugin's own collector; lazy so a failure fails open
        return collect_recap.load_config(root)[0]["retentionDays"]
    except Exception:
        return DEFAULT_RETENTION_DAYS


def main():
    if os.environ.get("SESSION_RECAP_DISABLE") == "1":
        return
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        return
    root = repo_root(payload.get("cwd") or os.getcwd())
    if not root:
        return
    days = retention_days(root)
    row = record(payload, root, {"retentionDays": days})
    home = Path.home()
    if row:
        d = log_dir(home, root)
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, separators=(",", ":")) + "\n"
        with open(d / (safe_session(payload.get("session_id")) + ".jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(line)
    prune(home / ".claude" / "session-recap", days, time.time())


if __name__ == "__main__":
    try:
        main()
    except Exception:  # fail open: never break the user's session
        pass
    sys.exit(0)
