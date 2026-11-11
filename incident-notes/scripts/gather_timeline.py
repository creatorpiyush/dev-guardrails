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
import fnmatch
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

def inside_repo(v):
    """A relative path with no '..', drive, or root: the postmortem is written under it."""
    if not isinstance(v, str) or not v.strip():
        return False
    p = v.strip().replace("\\", "/")
    return not (p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in p.split("/"))


VALIDATORS = {
    "directory": inside_repo,
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


def git_time(text):
    m = ISO.fullmatch((text or "").strip())
    got = m and iso_match(m, timezone.utc)
    return got[0] if got else None


# -- git facts -----------------------------------------------------------------

COMMIT_CAP = 200
TIMELINE_CAP = 300
LOG_SCAN_LIMIT = "5000"  # commits git log may return before the window filter
HOTSPOT_CAP = 10
HOTFIX = re.compile(r"(?i)(?:\b(?:hot-?fix|rollback|roll[ -]back|emergency|urgent|quick-?fix)\b|hotfix/)")


def commit_kinds(subject, body, parents):
    kinds = []
    if subject.startswith('Revert "') or "This reverts commit" in body:
        kinds.append("revert")
    if HOTFIX.search(subject):
        kinds.append("hotfix")
    if len(parents) > 1:
        kinds.append("merge")
    return kinds


def commit_events(root, since, until, include_authors, counter, cap=COMMIT_CAP):
    """Commits on any local or remote branch or tag, by author date in the window.

    Returns (entries newest first, truncated, {path: commits touching it})."""
    out = git(root, "log", "--branches", "--tags", "--remotes", "-n", LOG_SCAN_LIMIT,
              "--since=" + iso(since), "--name-only",
              "--format=%x1e%H%x1f%aI%x1f%P%x1f%an%x1f%s%x1f%b%x1f")
    entries, counts = [], {}
    # git() strips the output, and str.strip() treats \x1e and \x1f as whitespace,
    # so the first separator and the last record's empty trailing fields may be gone.
    for rec in filter(None, (out or "").split("\x1e")):
        parts = (rec.split("\x1f") + [""] * 7)[:7]
        sha, date, parents, author, subject, body, names = parts
        ts = git_time(date)
        if ts is None or not since <= ts <= until:
            continue
        files = [n for n in names.splitlines() if n.strip()]
        for name in files:
            counts[name] = counts.get(name, 0) + 1
        entry = {"ts": iso(ts), "source": "commit", "confidence": "exact",
                 "hash": sha[:12], "subject": redact(subject, counter), "files": len(files),
                 "kinds": commit_kinds(subject, body, parents.split())}
        if include_authors:
            entry["author"] = author
        entries.append(entry)
    return entries[:cap], len(entries) > cap, counts


def tag_events(root, since, until, counter):
    out = git(root, "for-each-ref", "refs/tags",
              "--format=%(refname:short)%1f%(objecttype)%1f%(creatordate:iso-strict)")
    entries = []
    for line in (out or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        name, objtype, date = parts
        ts = git_time(date)
        if ts is None or not since <= ts <= until:
            continue
        if re.search(r"(?i)deploy|prod", name):
            kind = "deploy"
        elif re.search(r"(?i)^v?\d+\.\d+|release", name):
            kind = "release"
        else:
            kind = "tag"
        # A lightweight tag carries its commit's date, not the time it was tagged.
        entries.append({"ts": iso(ts), "source": "tag",
                        "confidence": "exact" if objtype == "tag" else "approximate",
                        "name": redact(name, counter), "kind": kind})
    return entries


def hotspots(file_counts, counter):
    ranked = sorted(file_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"path": redact(p, counter), "commits": n} for p, n in ranked[:HOTSPOT_CAP]]


# -- redaction ------------------------------------------------------------------

# Copied from pr-writer/scripts/collect_context.py (itself from
# secret-scan-guard/hooks/patterns.py); keep in sync (tests/test_copies_in_sync.py).
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

# Medium-confidence secrets; only the secret part is replaced.
EXTRA_REDACTIONS = [
    ("url-credentials", re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@'\"]+:)(?![$<])[^/\s'\"]+@"),
     r"\1<redacted:url-credentials>@"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
     "<redacted:jwt>"),
]

# A private key's header rule only matches one line; cover the whole block.
KEY_BLOCK = re.compile(
    SECRET_RULES[-1][1].pattern
    + r"(?:.*?-----END (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----|(?:\r?\n[+\- ]?[A-Za-z0-9+/=]+)*)",
    re.S)


def redact(text, counter):
    """Replace secrets with <redacted:rule>; count hits per rule in counter."""
    text, n = KEY_BLOCK.subn("<redacted:private-key>", text)
    if n:
        counter["private-key"] = counter.get("private-key", 0) + n
    rules = [(name, rx, "<redacted:%s>" % name) for name, rx in SECRET_RULES] + EXTRA_REDACTIONS
    for name, rx, replacement in rules:
        text, n = rx.subn(replacement, text)
        if n:
            counter[name] = counter.get(name, 0) + n
    return text


# -- logs ----------------------------------------------------------------------

LOG_PADDING = timedelta(minutes=30)  # around the log span when no window is given
MAX_LOG_BYTES = 20 * 1024 * 1024  # read from the start; the rest is reported as truncated
MAX_SIGNATURES = 50  # distinct error signatures kept per file
MAX_BUCKETS = 360  # per-minute error buckets in the output
MAX_UNPARSED_SAMPLES = 5
SAMPLE_CHARS = 32
SIGNATURE_CHARS = 160
HEAD_CHARS = 120  # timestamps are looked for in the start of each line only
SECRET_NAMES = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.p12", "credentials.json")
SECRET_ALLOW = (".env.example", ".env.sample", ".env.template")

LOG_ISO = re.compile(
    r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?"
    r"\s?(Z|z|[+-]\d{2}:?\d{2})?(?!\d)")
SYSLOG = re.compile(r"^(?:<\d+>)?([A-Z][a-z]{2}) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2})")
MONTHS = {m: i + 1 for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}
LEAD_EPOCH = re.compile(r"^\[?(\d{10})(?:\.\d+)?\b|^\[?(\d{13})\b")
JSON_EPOCH = re.compile(r'"(?:ts|time|timestamp)"\s*:\s*(?:(\d{10})(?:\.\d+)?|(\d{13}))\b')
TIMESTAMPISH = re.compile(r"^\[?\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}")
ERROR_LINE = re.compile(
    r"\b(?:ERROR|FATAL|CRITICAL|CRIT|EMERG|ALERT|SEVERE|PANIC)\b"
    r"|(?i:level[\"']?\s*[=:]\s*[\"']?(?:error|fatal|critical)\b)"
    r"|Traceback \(most recent call last\)|^panic: |Exception\b")
LEVEL = re.compile(r"\b(?:ERROR|FATAL|CRITICAL|CRIT|EMERG|ALERT|SEVERE|PANIC)\b[\s:\]|-]*")
MESSAGE_KEYS = ("msg", "message", "error", "err")

# Normalisers, applied in order after secrets are redacted.
NORMALISE = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
     "<uuid>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "<email>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    # ponytail: IPv6 needs a hex letter or '::', so clock times like 10:00:05 stay put.
    (re.compile(r"(?<![\w:])(?=[0-9a-fA-F:]*(?:[a-fA-F]|::))"
                r"(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?![\w:])"), "<ip>"),
    (re.compile(r"\b(?:0x)?(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}\b"), "<hex>"),
    (re.compile(r"(?<!\w)'[^'\n]*'(?!\w)|\"[^\"\n]*\""), "<str>"),
    (re.compile(r"\d+"), "<n>"),
]
# ponytail: internal hostnames are recognised by suffix only; add a config list
# if teams need other domains.
HOSTNAME = re.compile(
    r"\b(?:[A-Za-z0-9-]+\.)+(?:internal|local|corp|lan|intranet|svc|cluster\.local)\b")


def is_secret(path):
    name = os.path.basename(path).lower()
    if name in SECRET_ALLOW:
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in SECRET_NAMES)


def line_time(line, tz, ref):
    """(UTC datetime, exact, (start, end)) for the line's own timestamp, else None."""
    head = line[:HEAD_CHARS]
    m = LOG_ISO.search(head)
    if m:
        got = iso_match(m, tz)
        return (got[0], got[1], m.span()) if got else None
    m = SYSLOG.match(head)
    if m and m.group(1) in MONTHS:
        try:
            dt = datetime(ref.year, MONTHS[m.group(1)], int(m.group(2)),
                          int(m.group(3)), int(m.group(4)), int(m.group(5)))
            dt = dt.replace(tzinfo=tz).astimezone(timezone.utc)
            if dt > ref + timedelta(days=1):  # syslog has no year: December seen in January
                dt = dt.replace(year=dt.year - 1)
        except ValueError:
            return None
        return dt, False, m.span()
    m = LEAD_EPOCH.match(head) or JSON_EPOCH.search(head)
    if m:
        dt = from_epoch(m.group(1), m.group(2))
        return (dt, True, m.span()) if dt else None
    return None


def message_of(line):
    """The message field of a JSON log line, else the line itself."""
    if line.lstrip().startswith("{"):
        try:
            data = json.loads(line)
        except ValueError:
            return line
        if isinstance(data, dict):
            for key in MESSAGE_KEYS:
                if isinstance(data.get(key), str):
                    return data[key]
    return line


def signature(line, span, redact_hosts, counter):
    """The line minus timestamp and level, with secrets, identifiers, and numbers masked."""
    if line.lstrip().startswith("{"):
        text = message_of(line)
    else:
        text = line[:span[0]] + line[span[1]:] if span else line
    text = LEVEL.sub("", text, count=1)
    text = redact(text, counter)  # before normalising, which would break the secret patterns
    if redact_hosts:
        text = HOSTNAME.sub("<host>", text)
    for rx, repl in NORMALISE:
        text = rx.sub(repl, text)
    text = " ".join(text.split()).strip(" -:|[]")
    return text[:SIGNATURE_CHARS]


def open_log(path):
    """(binary file, None) or (None, reason it was skipped)."""
    if is_secret(path) or is_secret(os.path.realpath(path)):
        return None, "secret file"
    if not os.path.exists(path):
        return None, "not found"
    if not os.path.isfile(path):
        return None, "not a file"
    try:
        f = open(path, "rb")
    except OSError:
        return None, "unreadable"
    if b"\0" in f.read(8192):
        f.close()
        return None, "binary"
    f.seek(0)
    return f, None


class LogLines(object):
    """Iterate (line number, text) over a binary file, stopping at MAX_LOG_BYTES."""

    def __init__(self, f):
        self.f = f
        self.truncated = False

    def __iter__(self):
        total = 0
        for number, raw in enumerate(self.f, 1):
            total += len(raw)
            if total > MAX_LOG_BYTES:
                self.truncated = True
                return
            yield number, raw.decode("utf-8", "replace").rstrip("\r\n")


def log_span(path, tz, ref):
    """(first, last) timestamps in the file, or None."""
    f, _ = open_log(path)
    if f is None:
        return None
    first = last = None
    with f:
        for _, line in LogLines(f):
            got = line_time(line, tz, ref)
            if got:
                first = first or got[0]
                last = got[0]
    return (first, last) if first else None


def read_log(path, tz, window, cfg, counter, ref=None):
    """Facts for one log file. Keys starting with '_' are merged by main(), not output."""
    since, until = window
    ref = ref or until
    info = {"path": path, "lines": 0, "parsed": 0, "first": None, "last": None,
            "assumed_timezone": None, "errors": 0, "truncated": False, "signatures_dropped": 0,
            "_entries": [], "_buckets": {}, "_unparsed": 0, "_samples": []}
    f, skipped = open_log(path)
    if f is None:
        info["skipped"] = skipped
        return info
    sigs = {}
    event = None  # the current timestamped line and the lines that continue it
    with f:
        lines = LogLines(f)
        for number, line in lines:
            info["lines"] = number
            got = line_time(line, tz, ref)
            if got:
                ts, exact, span = got
                info["parsed"] += 1
                info["first"] = info["first"] or iso(ts)
                info["last"] = iso(ts)
                if not exact:
                    info["assumed_timezone"] = cfg["timezone"]
                event = {"ts": ts, "exact": exact, "error": False}
            else:
                span = None
                if TIMESTAMPISH.match(line):
                    info["_unparsed"] += 1
                    if len(info["_samples"]) < MAX_UNPARSED_SAMPLES:
                        info["_samples"].append(redact(line, counter)[:SAMPLE_CHARS])
                if event is None:
                    continue
            if event["error"] or not ERROR_LINE.search(line):
                continue
            event["error"] = True  # one error per event, however many lines it spans
            if not since <= event["ts"] <= until:
                continue
            info["errors"] += 1
            minute = iso(event["ts"])[:16] + ":00Z"
            info["_buckets"][minute] = info["_buckets"].get(minute, 0) + 1
            sig = signature(line, span, cfg["redactHostnames"], counter)
            if sig in sigs:
                sigs[sig]["count"] += 1
            elif len(sigs) < MAX_SIGNATURES:
                sigs[sig] = {"ts": iso(event["ts"]), "source": "log",
                             "confidence": "exact" if event["exact"] and got else "approximate",
                             "file": path, "line": number, "signature": sig, "count": 1}
            else:
                info["signatures_dropped"] += 1
        info["truncated"] = lines.truncated
    info["_entries"] = list(sigs.values())
    return info


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
    ref = bounds.get("until") or now  # the year for syslog lines, which have none
    spans = [] if bounds else [s for s in (log_span(p, tz, ref) for p in args.logs) if s]
    if spans:
        source = "logs"
        bounds = {"since": min(s[0] for s in spans) - LOG_PADDING,
                  "until": max(s[1] for s in spans) + LOG_PADDING}
    until = bounds.get("until") or now
    since = bounds.get("since") or until - timedelta(hours=24)
    if since >= until:
        return fail("bad-arguments", "--since must be before --until")

    title = (args.title or "").strip() or None
    slug = (slugify(title) if title else "") or None
    directory = cfg["directory"].strip().strip("/")
    output_path = ("%s/%s-%s.md" % (directory, since.strftime("%Y-%m-%d"), slug)
                   if slug else None)
    counter = {}
    commits, commits_truncated, file_counts = commit_events(
        root, since, until, cfg["includeAuthors"], counter)
    timeline = commits + tag_events(root, since, until, counter)
    files, buckets, unparsed, samples = [], {}, 0, []
    for path in args.logs:
        info = read_log(path, tz, (since, until), cfg, counter, ref=ref)
        timeline += info.pop("_entries")
        for minute, n in info.pop("_buckets").items():
            buckets[minute] = buckets.get(minute, 0) + n
        unparsed += info.pop("_unparsed")
        samples += info.pop("_samples")
        files.append(info)
    bucket_list = [{"minute": m, "errors": n} for m, n in sorted(buckets.items())]
    timeline.sort(key=lambda e: (e["ts"], e["source"]))
    return {
        "ok": True, "config": cfg, "warnings": warnings,
        "title": title, "slug": slug, "output_path": output_path,
        "window": {"since": iso(since), "until": iso(until), "source": source},
        "timeline": timeline[:TIMELINE_CAP], "timeline_truncated": len(timeline) > TIMELINE_CAP,
        "commits": {"count": len(commits), "truncated": commits_truncated},
        "hotspots": hotspots(file_counts, counter), "redactions": counter,
        "logs": {"files": files, "buckets": bucket_list[:MAX_BUCKETS],
                 "buckets_truncated": len(bucket_list) > MAX_BUCKETS,
                 "unparsed": {"count": unparsed, "samples": samples[:MAX_UNPARSED_SAMPLES]}},
    }


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
    except Exception as e:  # always emit one JSON object
        result = fail("internal-error", str(e))
    print(json.dumps(result))
    sys.exit(0)
