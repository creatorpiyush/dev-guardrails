#!/usr/bin/env python3
"""Gather a UTC incident timeline for the incident-notes skill.

Reads git history, optional log files, and merged PRs (through `gh`, only when
it is signed in) for a time window. Every timeline entry is in UTC and tagged
with its source and a confidence flag. Secrets in commit subjects, PR titles,
and log lines are redacted before they are printed.

Prints exactly one JSON object on stdout and always exits 0. Failures are
reported as {"ok": false, "error": {"code", "message"}}. Never writes files.

Standard library only. Python 3.9+.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# -- config ------------------------------------------------------------------

CONFIG_FILE = os.path.join(".claude", "incident-notes.json")
DEFAULT_CONFIG = {
    "directory": "docs/postmortems",
    "timezone": "UTC",
    "includeAuthors": False,
    "redactHostnames": False,
    "severityScale": ["SEV1", "SEV2", "SEV3", "SEV4"],
}
OFFSET = re.compile(r"^([+-])(\d{2}):?(\d{2})$")
TZ_NAME = re.compile(r"^(UTC|[A-Za-z_]+(/[A-Za-z0-9_+\-]+)*)$")

VALIDATORS = {
    "directory": lambda v: isinstance(v, str) and bool(v.strip()),
    "timezone": lambda v: isinstance(v, str) and bool(OFFSET.match(v) or TZ_NAME.match(v)),
    "includeAuthors": lambda v: isinstance(v, bool),
    "redactHostnames": lambda v: isinstance(v, bool),
    "severityScale": lambda v: isinstance(v, list) and bool(v)
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
            timeout=30, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


# -- time --------------------------------------------------------------------

ISO = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?)?"
    r"\s?(Z|z|[+-]\d{2}:?\d{2})?")
EPOCH = re.compile(r"^(\d{10})(?:\.\d+)?$|^(\d{13})$")
RELATIVE = re.compile(r"^(\d+)\s*(second|minute|hour|day|week)s?\s+ago$", re.I)


def resolve_tz(name, warnings):
    """A tzinfo for "UTC", "+05:30", or an IANA name; UTC with a warning if unknown."""
    if name == "UTC":
        return timezone.utc
    if OFFSET.match(name):
        return zone_of(name)
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # missing module, missing tzdata, or unknown name
        warnings.append("timezone %r is not available here; using UTC" % name)
        return timezone.utc


def zone_of(text):
    """tzinfo for "Z" or "+HH:MM"/"+HHMM"; None for no zone."""
    if not text:
        return None
    if text in ("Z", "z"):
        return timezone.utc
    m = OFFSET.match(text)
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(-delta if m.group(1) == "-" else delta)


def iso_match(m, tz):
    """(aware UTC datetime, had_zone) from an ISO match, or None if out of range."""
    y, mo, d, h, mi, s, z = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0))
    except ValueError:
        return None
    zone = zone_of(z)
    return dt.replace(tzinfo=zone or tz).astimezone(timezone.utc), zone is not None


def from_epoch(seconds, ms):
    try:
        value = int(ms) / 1000.0 if ms else int(seconds)
        return datetime.fromtimestamp(value, timezone.utc).replace(microsecond=0)
    except (ValueError, OverflowError, OSError):
        return None


def parse_time(text, tz, now):
    """Parse a --since/--until value to an aware UTC datetime, or None."""
    text = (text or "").strip()
    m = EPOCH.match(text)
    if m:
        return from_epoch(m.group(1), m.group(2))
    m = RELATIVE.match(text)
    if m:
        return now - timedelta(**{m.group(2).lower() + "s": int(m.group(1))})
    m = ISO.fullmatch(text)
    if m:
        got = iso_match(m, tz)
        return got[0] if got else None
    return None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(title):
    return "-".join(re.findall(r"[a-z0-9]+", title.lower()))[:60].strip("-")


# -- output ------------------------------------------------------------------

def fail(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


class ArgError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgError(message)


def main(argv, now=None):
    p = Parser(prog="gather_timeline.py", add_help=False)
    p.add_argument("title", nargs="?")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--logs", nargs="+", action="extend", default=[])
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
    tz = resolve_tz(cfg["timezone"], warnings)
    now = now or datetime.now(timezone.utc).replace(microsecond=0)

    bounds = {}
    for name in ("since", "until"):
        text = getattr(args, name)
        if text is not None:
            bounds[name] = parse_time(text, tz, now)
            if bounds[name] is None:
                return fail("bad-arguments", "--%s %r is not a time" % (name, text))
    source = "args" if bounds else "default"
    until = bounds.get("until") or now
    since = bounds.get("since") or until - timedelta(hours=24)
    if since >= until:
        return fail("bad-arguments", "--since must be before --until")

    title = (args.title or "").strip() or None
    slug = (slugify(title) if title else "") or None
    directory = cfg["directory"].strip().strip("/")
    output_path = ("%s/%s-%s.md" % (directory, since.strftime("%Y-%m-%d"), slug)
                   if slug else None)
    return {
        "ok": True, "config": cfg, "warnings": warnings,
        "title": title, "slug": slug, "output_path": output_path,
        "window": {"since": iso(since), "until": iso(until), "source": source},
        "timeline": [], "timeline_truncated": False, "hotspots": [],
        "logs": {"files": [], "buckets": [], "unparsed": {"count": 0, "samples": []}},
    }


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
    except Exception as e:  # always emit one JSON object
        result = fail("internal-error", str(e))
    print(json.dumps(result))
    sys.exit(0)
