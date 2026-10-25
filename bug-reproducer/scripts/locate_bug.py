#!/usr/bin/env python3
"""Pull candidates out of a bug report for the bug-reproducer skill.

Reads the report from --file (or stdin) and prints exactly one JSON object on
stdout, always exiting 0. Failures are reported as
{"ok": false, "error": {"code", "message"}}.

The report is only ever matched with regexes: nothing in it is executed. The
script never writes files. The only subprocess is read-only git, run by
detect_tests.py, which sits next to this file (a byte-identical copy of
test-writer's) and supplies the test command and the nearest tests.

Standard library only. Python 3.9+. No network.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import detect_tests as dt
except Exception as e:  # reported as JSON by main()
    dt, IMPORT_ERROR = None, e

MAX_REPORT = 256 * 1024  # bytes; a longer report keeps its tail (logs end with the error)
MAX_ITEMS = 30           # frames, paths, errors, versions
MAX_SYMBOLS = 20
MAX_TERMS = 12           # distinctive strings searched for
MAX_HITS = 10            # per symbol or string
MAX_MATCHES = 5          # repo files one mentioned path can resolve to
MAX_TARGETS = 10         # files passed to detect_tests.py
MIN_TERM = 8             # shortest string worth searching for
MAX_LINE = 4000          # characters kept per line; longer lines are minified output, not prose

CONFIG_FILE = os.path.join(".claude", "bug-reproducer.json")
DEFAULT_CONFIG = {"autoFix": False}

EXT_LANG = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
            ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript", ".java": "java",
            ".kt": "kotlin", ".scala": "scala", ".groovy": "groovy", ".go": "go", ".rs": "rust",
            ".rb": "ruby", ".dart": "dart"}
PATH_EXTS = sorted(set(EXT_LANG) | {".php", ".cs", ".c", ".h", ".cc", ".cpp", ".hpp", ".swift",
                                    ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh",
                                    ".sql", ".html", ".css", ".vue", ".svelte"},
                   key=len, reverse=True)

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
URL = re.compile(r"(?<![\w+.-])[a-z][a-z0-9+.-]*://[^\s<>()\"'`]+", re.I)
BLOB_URL = re.compile(r"/(?:-/)?blob/[^/\s]+/(?P<path>[^\s#?]+)(?:#L(?P<line>\d+))?")

# Stack-trace frames, tried in this order on each line.
PY_FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>\S+))?')
JAVA_FRAME = re.compile(r"^\s*at\s+(?:[\w.$-]+/)?(?P<func>[\w$.<>]+)"
                        r"\((?P<path>[\w$-]+\.(?:java|kt|scala|groovy)):(?P<line>\d+)\)")
RUBY_FRAME = re.compile(r"(?<![^\s:'\"`])(?P<path>[^\s:'\"`]+\.rb):(?P<line>\d+):in\s+[`'](?P<func>[^'`]+)'")
DART_FRAME = re.compile(r"^\s*#\d+\s+(?P<func>.+?)\s+\((?P<path>[^()\s]+?):(?P<line>\d+)(?::\d+)?\)\s*$")
RUST_PANIC = re.compile(r"panicked at (?:'(?P<msg>[^']*)',\s*)?(?P<path>[^\s:'\"]+\.rs):(?P<line>\d+)")
GO_FILE = re.compile(r"^\s+(?P<path>\S+\.go):(?P<line>\d+)(?:\s+\+0x[0-9a-f]+)?\s*$")
GO_FUNC = re.compile(r"^(?P<func>[\w./*()\[\]-]+?)\([^()]*\)\s*$")
AT_FRAME = re.compile(r"^\s*at\s+(?:(?P<func>[^()]+?)\s+\()?(?P<path>(?:file://)?[^\s()]+?)"
                      r":(?P<line>\d+)(?::\d+)?\)?\s*$")
RUST_FUNC = re.compile(r"^\s*\d+:\s+(?:0x[0-9a-f]+\s+-\s+)?(?P<func>\S+)\s*$")

ERRORS = (
    re.compile(r"The following (?P<type>[A-Za-z_]\w*) was thrown[^\n]*$"),
    re.compile(r":in\s+[`'][^']*':\s*(?P<message>.+?)\s+\((?P<type>[A-Z][\w:]*)\)\s*$"),
    re.compile(r"(?:^|\s)(?:[\w$]+\.)*(?P<type>[A-Z][\w$]*(?:Error|Exception|Exit|Interrupt|Failure))"
               r"(?::\s*(?P<message>.+?))?\s*$"),
    re.compile(r"^panic:\s*(?P<message>.+?)\s*$"),
    re.compile(r"^error(?:\[(?P<type>E\d+)\])?:\s*(?P<message>.+?)\s*$"),
    re.compile(r"^\s*(?:fatal|FATAL|Error|ERROR)\s*:\s*(?P<message>.+?)\s*$"),
)
EXIT_CODE = re.compile(r"\b(?:exit(?:ed)?\s+(?:with\s+)?(?:code|status)|non-zero\s+exit\s+status"
                       r"|exited\s+with)\s*[:=]?\s*(-?\d{1,3})\b", re.I)
VERSION = re.compile(r"(?<![\w./-])(?P<name>[A-Za-z][\w+-]*(?:\.js)?)(?:\s+version)?(?:@|:\s*|=|\s+)v?"
                     r"(?P<version>\d+\.\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z.]+)?)(?![\w.])")
NOT_A_NAME = {"line", "at", "in", "on", "to", "from", "and", "or", "of", "is", "was", "the", "a",
              "than", "with", "by", "for", "port"}
PATH_MENTION = re.compile(
    r"(?<![\w/.:\\-])(?P<path>(?:[A-Za-z]:)?(?:\.{0,2}[/\\])?(?:[\w.@-]+[/\\])*[\w@-][\w.@-]*"
    r"(?:" + "|".join(re.escape(e) for e in PATH_EXTS) + r"))(?![\w]|\.\w)(?:[:#]L?(?P<line>\d+)\b)?")
CODE_SPAN = re.compile(r"`([^`\n]{1,120})`")
CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*(?:(?:\.|::|#)[A-Za-z_$][\w$]*)*)\(\)")
QUOTED = re.compile(r"\"([^\"\n]{%d,160})\"|'([^'\n]{%d,160})'" % (MIN_TERM, MIN_TERM))
IDENT = re.compile(r"[A-Za-z_$][\w$]*")
QUALIFIED = re.compile(r"^[A-Za-z_$][\w$]*(?:(?:\.|::|#)[A-Za-z_$][\w$]*)*(?:\(\))?$")
RUST_HASH = re.compile(r"^h[0-9a-f]{16}$")
FILLER = {"async", "new", "block", "in", "anonymous", "module", "lambda", "closure", "listcomp",
          "genexpr", "dictcomp", "setcomp", "rescue", "ensure", "top", "level", "Object", "main",
          "processTicksAndRejections"}


def fail(code, message):
    return {"ok": False, "error": {"code": code, "message": message}, "warnings": []}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parse_args(argv):
    p = _Parser(prog="locate_bug.py", add_help=False)
    p.add_argument("--file", metavar="PATH")
    return p.parse_args(argv)


def read_report(path, warnings):
    """Report text, ANSI-stripped. Raises ValueError for a missing or empty report."""
    if path and path != "-":
        if dt.is_secret(path) or dt.is_secret(os.path.realpath(path)):
            raise ValueError("%s looks like a secret file; it is never read" % path)
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > MAX_REPORT:
                    f.seek(size - MAX_REPORT)
                data = f.read(MAX_REPORT)
        except OSError as e:
            raise ValueError("cannot read %s: %s" % (path, e.strerror or e))
    else:
        data = sys.stdin.buffer.read()
        size = len(data)
        data = data[-MAX_REPORT:]
    if size > MAX_REPORT:
        warnings.append("report is over %d bytes; only its last %d were read" % (MAX_REPORT, MAX_REPORT))
    text = ANSI.sub("", data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n"))
    if not text.strip():
        raise ValueError("the bug report is empty")
    lines = text.split("\n")
    if any(len(line) > MAX_LINE for line in lines):
        warnings.append("lines over %d characters were cut" % MAX_LINE)
        text = "\n".join(line[:MAX_LINE] for line in lines)
    return text


def load_config(root, warnings):
    path = os.path.join(root, CONFIG_FILE)
    if not os.path.isfile(path):
        return dict(DEFAULT_CONFIG)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        warnings.append("%s is not valid JSON (%s); using defaults" % (CONFIG_FILE, e))
        return dict(DEFAULT_CONFIG)
    if not isinstance(data, dict) or not isinstance(data.get("autoFix", False), bool):
        warnings.append('%s must be an object with a boolean "autoFix"; using defaults' % CONFIG_FILE)
        return dict(DEFAULT_CONFIG)
    for key in sorted(set(data) - set(DEFAULT_CONFIG)):
        warnings.append("%s: unknown key %r ignored" % (CONFIG_FILE, key))
    return {"autoFix": data.get("autoFix", False)}


# ---------------------------------------------------------------- parsing

def dart_path(uri):
    """package:app/src/a.dart -> lib/src/a.dart; file:///x/a.dart -> /x/a.dart; dart:core/a.dart stays."""
    if uri.startswith("package:") and "/" in uri:
        return "lib/" + uri[8:].split("/", 1)[1]
    return uri[7:] if uri.startswith("file://") else uri


def lang_of(path, default=None):
    return EXT_LANG.get(os.path.splitext(path)[1].lower(), default)


def frames(lines):
    out = []
    for i, line in enumerate(lines):
        prev = lines[i - 1] if i else ""
        frame = None
        m = PY_FRAME.search(line)
        if m:
            frame = ("python", m["path"], m["line"], m["func"])
        elif JAVA_FRAME.search(line):
            m = JAVA_FRAME.search(line)
            func = m["func"]
            parts = func.split(".")
            # com.acme.Bar.baz in Bar.java -> com/acme/Bar.java, so the path resolves exactly.
            path = "/".join(parts[:-2] + [m["path"]]) if len(parts) > 2 else m["path"]
            frame = (lang_of(m["path"]), path, m["line"], func)
        elif RUBY_FRAME.search(line):
            m = RUBY_FRAME.search(line)
            frame = ("ruby", m["path"], m["line"], m["func"])
        elif DART_FRAME.match(line):
            m = DART_FRAME.match(line)
            frame = ("dart", dart_path(m["path"]), m["line"], m["func"])
        elif RUST_PANIC.search(line):
            m = RUST_PANIC.search(line)
            frame = ("rust", m["path"], m["line"], None)
        elif GO_FILE.match(line):
            m = GO_FILE.match(line)
            f = GO_FUNC.match(prev.strip())
            frame = ("go", m["path"], m["line"], f["func"] if f else None)
        elif AT_FRAME.match(line):
            m = AT_FRAME.match(line)
            path = m["path"][7:] if m["path"].startswith("file://") else m["path"]
            func = m["func"]
            lang = lang_of(path, "javascript")
            if lang == "rust" and not func:
                f = RUST_FUNC.match(prev)
                func = f["func"] if f else None
            frame = (lang, path, m["line"], func)
        if frame:
            out.append({"language": frame[0], "path": frame[1], "line": int(frame[2]),
                        "function": frame[3], "index": i})
    return out


def symbol_parts(func):
    """Searchable names in a frame's function: com.acme.Bar.baz -> [baz, Bar]."""
    if not func:
        return []
    words = [w for w in IDENT.findall(func) if w not in FILLER and not RUST_HASH.match(w)]
    if not words:
        return []
    names = [words[-1]]
    if len(words) > 1 and words[-2][:1].isupper():
        names.append(words[-2])
    return names


def errors(lines):
    out, seen = [], set()
    for i, line in enumerate(lines):
        m = RUST_PANIC.search(line)
        if m:
            item = {"type": "panic", "message": m["msg"]}
        else:
            for rx in ERRORS:
                m = rx.search(line)
                if m:
                    groups = m.groupdict()
                    item = {"type": groups.get("type"), "message": groups.get("message")}
                    break
            else:
                continue
        if item["message"] is None and line.rstrip().endswith(":") and i + 1 < len(lines):
            item["message"] = lines[i + 1].strip() or None  # Rust panics and Flutter put it on the next line
        key = (item["type"], item["message"])
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out[:MAX_ITEMS]


def versions(text):
    out, seen = [], set()
    for m in VERSION.finditer(text):
        name = m["name"]
        if name.lower() in NOT_A_NAME or (name, m["version"]) in seen:
            continue
        seen.add((name, m["version"]))
        out.append({"name": name, "version": m["version"]})
    return out[:MAX_ITEMS]


def mentioned_paths(text):
    found = []
    for m in URL.finditer(text):
        b = BLOB_URL.search(m.group())
        if b:
            found.append((b["path"], b["line"]))
    for m in PATH_MENTION.finditer(URL.sub(" ", text)):
        found.append((m["path"], m["line"]))
    return found


def mentioned_symbols(text):
    names = []
    for span in CODE_SPAN.findall(text):
        span = span.strip()
        if QUALIFIED.match(span):
            names.append(re.split(r"\.|::|#", span[:-2] if span.endswith("()") else span)[-1])
    for call in CALL.findall(text):
        names.append(re.split(r"\.|::|#", call)[-1])
    return names


def terms(text, errs):
    """Literal strings worth searching for: quoted strings and error-message fragments."""
    out = []
    for a, b in QUOTED.findall(text):
        out.append((a or b).strip())
    for span in CODE_SPAN.findall(text):
        if " " in span.strip():
            out.append(span.strip())
    for e in errs:
        # Drop the parts that vary per run (quoted values, numbers) and keep the longest fixed piece.
        pieces = re.split(r"'[^']*'|\"[^\"]*\"|\b\d+\b|:", e["message"] or "")
        out.append(max((p.strip() for p in pieces), key=len, default=""))
    return unique([t for t in out if len(t) >= MIN_TERM])[:MAX_TERMS]


def unique(items):
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------- repo lookups

class Index:
    def __init__(self, repo, skip=None):
        self.repo = repo
        self.skip = skip  # the report file itself, so its own text isn't a "hit"
        self.by_name = {}
        for f in repo.files:
            self.by_name.setdefault(f.rsplit("/", 1)[-1], []).append(f)

    def resolve(self, raw):
        """Repo files a mentioned path can mean, trying the longest matching suffix first."""
        p = re.sub(r"^[A-Za-z]:", "", raw.replace("\\", "/"))
        parts = [x for x in p.split("/") if x not in ("", ".", "..")]
        if not parts or dt.is_secret(parts[-1]):
            return []
        candidates = self.by_name.get(parts[-1], [])
        for i in range(len(parts)):
            suffix = "/".join(parts[i:])
            hits = [f for f in candidates if f == suffix or f.endswith("/" + suffix)]
            if hits:
                return hits[:MAX_MATCHES]
        return []

    def grep(self, term):
        """Lines containing term literally, in files Repo.read allows (never secret files)."""
        hits = []
        for f in self.repo.files[:dt.MAX_SCAN]:
            text = None if f == self.skip else self.repo.read(f)
            if not text or term not in text:
                continue
            for n, line in enumerate(text.split("\n"), 1):
                if term in line:
                    if len(hits) == MAX_HITS:
                        return hits, True
                    hits.append({"path": f, "line": n})
        return hits, False


def test_facts(root, targets, warnings):
    try:
        result = dt.main([os.path.join(root, t) for t in targets])
    except Exception as e:  # keep the report facts even if test detection breaks
        result = {"ok": False, "error": {"message": str(e)}, "warnings": []}
    for w in result.get("warnings", []):
        warnings.append("detect_tests: " + w)
    if not result.get("ok"):
        warnings.append("detect_tests: %s" % (result.get("error") or {}).get("message"))
        return {"frameworks": [], "commands": [], "conventions": {}, "targets": []}
    keep = ("path", "language", "symbol", "line", "existing_tests", "exemplars")
    return {"frameworks": result["frameworks"], "commands": result["commands"],
            "conventions": result["conventions"],
            "targets": [{k: t[k] for k in keep if k in t} for t in result["targets"]]}


def main(argv):
    if dt is None:
        return fail("internal-error", "cannot load detect_tests.py: %s" % IMPORT_ERROR)
    warnings = []
    try:
        args = parse_args(argv)
        text = read_report(args.file, warnings)
    except ValueError as e:
        return fail("bad-arguments", str(e))
    root = (dt.git(os.getcwd(), "rev-parse", "--show-toplevel") or "").strip()
    if not root:
        return fail("not-a-repo", "Not inside a git repository.")

    repo = dt.Repo(root)
    skip = None
    if args.file and args.file != "-":
        rel = os.path.relpath(os.path.realpath(args.file), repo.real).replace(os.sep, "/")
        skip = None if rel.startswith("../") else rel
    index = Index(repo, skip)
    lines = text.split("\n")

    found_frames = frames(lines)
    # Paths and quoted strings are read from the prose only, so frames aren't listed twice.
    frame_lines = {f.pop("index") for f in found_frames}
    prose = "\n".join(line for i, line in enumerate(lines) if i not in frame_lines)
    if len(found_frames) > MAX_ITEMS:
        warnings.append("%d stack frames; only the first %d are listed" % (len(found_frames), MAX_ITEMS))
        found_frames = found_frames[:MAX_ITEMS]
    for f in found_frames:
        f["repo_paths"] = index.resolve(f["path"])

    paths, seen = [], set()
    for raw, line in mentioned_paths(prose):
        line = int(line) if line else None
        if (raw, line) in seen:
            continue
        seen.add((raw, line))
        paths.append({"path": raw, "line": line, "repo_paths": index.resolve(raw)})
    paths = paths[:MAX_ITEMS]

    errs = errors(lines)
    names = [n for f in found_frames for n in symbol_parts(f["function"])]
    names += mentioned_symbols(prose) + [e["type"] for e in errs if e["type"] and e["type"][0].isupper()]
    names = [n for n in unique(names) if len(n) >= 3 and n not in FILLER][:MAX_SYMBOLS]
    symbols = [{"name": n, "definitions": [{"path": p, "line": ln}
                                           for p, ln in dt.find_symbol(repo, n)[:MAX_HITS]]}
               for n in names]

    grep = []
    for term in terms(prose, errs):
        hits, truncated = index.grep(term)
        grep.append({"term": term, "hits": hits, "truncated": truncated})

    in_repo = [p for f in found_frames for p in f["repo_paths"]]
    in_repo += [p for m in paths for p in m["repo_paths"]]
    in_repo += [d["path"] for s in symbols for d in s["definitions"]]
    in_repo += [h["path"] for g in grep for h in g["hits"]]
    sources = set(repo.sources)
    targets = [p for p in unique(in_repo) if p in sources][:MAX_TARGETS]

    return {"ok": True, "error": None, "warnings": warnings, "repo_root": root,
            "config": load_config(root, warnings),
            "report": {"lines": len(lines), "chars": len(text)},
            "frames": found_frames, "paths": paths, "symbols": symbols, "errors": errs,
            "exit_codes": unique(int(c) for c in EXIT_CODE.findall(text)),
            "versions": versions(URL.sub(" ", text)), "grep": grep,
            "tests": test_facts(root, targets, warnings), "truncated": repo.truncated}


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
    except Exception as e:  # always emit one JSON object
        result = {"ok": False, "error": {"code": "internal-error", "message": str(e)},
                  "warnings": []}
    print(json.dumps(result))
    sys.exit(0)
