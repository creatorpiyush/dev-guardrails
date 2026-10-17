#!/usr/bin/env python3
"""Find comments and prose in changed lines that read like generic boilerplate.

Prints exactly one JSON object on stdout and always exits 0. Failures are
reported as {"ok": false, "error": {"code", "message"}}. It never edits files.

  scan.py PATH...          whole files; directories expand to tracked and untracked files
  scan.py --staged         added lines in `git diff --cached`
  scan.py --diff BASE      added lines since merge-base(BASE, HEAD), working tree included
  scan.py                  added lines in uncommitted changes, plus untracked files

Standard library only. Python 3.9+.
"""
import argparse
import ast
import bisect
import io
import json
import os
import re
import statistics
import subprocess
import sys
import tokenize
from collections import Counter

CONFIG_FILE = os.path.join(".claude", "humanizer.json")
DEFAULT_CONFIG = {
    "ignore": [],
    "extraPhrases": [],
    "allowPhrases": [],
    "emDashPer100Words": 1.5,
    "commentRatio": 3,
    "commitNudge": False,
    "nudgeThreshold": 3,
    "voiceSampleFiles": ["README*", "CONTRIBUTING*", "docs/**/*.md"],
}
NOTE = ("Findings and scores are heuristics for prioritising review. "
        "They are not a measure of authorship.")

INFO, LOW, MEDIUM, HIGH = "info", "low", "medium", "high"
WEIGHT = {INFO: 0, LOW: 1, MEDIUM: 2, HIGH: 3}

MAX_FILE_BYTES = 1000000
SAMPLE_FILES, SAMPLE_BYTES, SAMPLE_CODE_FILES = 40, 400000, 20
SAMPLE_MIN_WORDS = 200  # below this, the repo's docs say too little about its style

# ---------------------------------------------------------------- rules

# id, severity, pattern. Matched case-insensitively against comment and prose
# text with inline code, URLs, and link targets masked out. Add rows to extend.
PHRASES = [
    ("phrase.important-to-note", MEDIUM, r"\bit(?:'s|’s| is) important to note\b"),
    ("phrase.worth-noting", MEDIUM, r"\b(?:it(?:'s|’s| is) )?worth noting\b"),
    ("phrase.delve", HIGH, r"\bdelv(?:e|es|ed|ing)\b"),
    ("phrase.dive-into", MEDIUM, r"\b(?:dive|dives|diving|dove) (?:deep |deeper )?into\b"),
    ("phrase.fast-paced", HIGH,
     r"\bin today(?:'s|’s) (?:fast-paced|ever-changing|rapidly evolving|digital)\b"),
    ("phrase.navigate-complexities", HIGH,
     r"\bnavigat(?:e|es|ed|ing) the (?:complexities|intricacies|landscape)\b"),
    ("phrase.tapestry", HIGH, r"\btapestry\b"),
    # verb only: "the leverage", "a leverage" are nouns
    ("phrase.leverage", MEDIUM, r"(?<!the )(?<!\ba )(?<!of )\bleverag(?:e|es|ed|ing)\b"),
    ("phrase.seamless", MEDIUM, r"\bseamless(?:ly)?\b"),
    ("phrase.robust", LOW, r"\brobust(?:ly)?\b"),
    ("phrase.comprehensive-solution", MEDIUM,
     r"\bcomprehensive (?:solution|suite|guide|overview|approach|framework)\b"),
    ("phrase.cutting-edge", MEDIUM, r"\bcutting[- ]edge\b"),
    ("phrase.game-changer", HIGH, r"\bgame[- ]chang(?:er|ers|ing)\b"),
    # not "unlock the mutex"
    ("phrase.unlock", MEDIUM, r"\bunlock(?:s|ed|ing)? (?:the |your |its |their )?(?:full |true )?"
                              r"(?:power|potential|value|benefits|possibilities|insights)\b"),
    ("phrase.empower", MEDIUM, r"\bempower(?:s|ed|ing|ment)?\b"),
    ("phrase.streamline", LOW, r"\bstreamlin(?:e|es|ed|ing)\b"),
    ("phrase.in-conclusion", MEDIUM, r"\bin conclusion\b"),
    ("phrase.overall", LOW, r"(?:^|[.!?]\s+)overall,"),
    ("phrase.this-ensures", LOW, r"\bthis ensures\b"),
    ("phrase.crucial-role", HIGH,
     r"\bplay(?:s|ed|ing)? an? (?:crucial|pivotal|vital|key|critical|essential) role\b"),
    ("phrase.testament", HIGH, r"\ba testament to\b"),
    ("formula.not-only", MEDIUM, r"\bnot only\b[^.!?]{1,120}?\bbut(?: also)?\b"),
    ("formula.whether-you", MEDIUM, r"\bwhether you(?:'re|’re| are)\b[^.!?]{1,100}?\bor\b"),
    ("hedge.pile-up", LOW, r"\b(?:may|might|could|can) (?:potentially|possibly)\b"
                           r"|\b(?:can|may|might|could) help to\b|\bit (?:seems|appears) that\b"),
]
ENSURE_THAT = re.compile(r"\bensur(?:e|es|ing) that\b", re.I)
TUTORIAL = re.compile(r"(?:^|[.!?])[ \t]*(?:let's|let’s|let us|now,? we|first,? we|next,? we|"
                      r"then,? we|here,? we|we will now|we'll now|we're going to)\b", re.I | re.M)

HYPE = set("""powerful robust seamless seamlessly comprehensive cutting-edge innovative revolutionary
effortless effortlessly incredibly extremely highly truly amazing ultimate world-class
state-of-the-art next-generation best-in-class blazing lightning-fast intuitive elegant
exceptional unparalleled remarkable significantly greatly vastly dramatically game-changing
transformative holistic dynamic versatile""".split())
PADDING = set("""fast reliable secure scalable efficient simple flexible powerful robust clean
maintainable intuitive easy seamless modern lightweight performant elegant readable extensible
consistent accessible safe stable comprehensive modular reusable""".split())
TRIPLET = re.compile(r"\b([a-z-]+), ([a-z-]+),? and ([a-z-]+)\b", re.I)
HEDGES = {"may", "might", "could", "possibly", "potentially", "perhaps", "somewhat", "arguably",
          "generally", "typically", "likely"}

EMOJI = re.compile("[☀-➿⬀-⯿\U0001f000-\U0001faff]")
BULLET = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(.*)")
HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*)")  # only the start matters here
BOLD_LABEL = re.compile(r"(?:\*\*|__)[^*_]+(?:\*\*|__)")

# Comments that tools read. Never flagged.
DIRECTIVE = re.compile(
    r"\bnoqa\b|type:\s*ignore|\bpragma\b|\bpylint:|\bmypy:|\bpyright:|\bruff:|\bisort:|"
    r"\bfmt:\s*(?:off|on|skip)|eslint-(?:disable|enable|env)|^[ \t]*eslint\b|^[ \t]*global\s|"
    r"prettier-ignore|@ts-(?:expect-error|ignore|nocheck|check)|istanbul ignore|c8 ignore|"
    r"\bjshint\b|\btslint:|\brubocop:|\bnolint\b|NOLINT|clang-format|shellcheck\s|^[ \t]*go:|"
    r"^[ \t]*\+build|\bnosec\b|NOSONAR|^[ \t]*#?(?:end)?region\b|<reference|^[ \t]*!|-\*-|"
    r"coding[:=]\s*[-\w.]+|^[ \t]*vim?:|\bcspell:|\bspell-checker:|markdownlint|"
    r"sourceMappingURL|@flow\b|@jsx\b|@license\b|@preserve\b|\bcoverage:|\blgtm\b",
    re.I | re.M)
LICENSE = re.compile(r"copyright|\blicen[cs]e|spdx-license-identifier|all rights reserved", re.I)
GENERATED = re.compile(r"do not edit|auto-?generated|@generated|generated by|code generated",
                       re.I)

GENERIC = set("""the a an to of and then we it its this that in on for with is are be by from into
here now each all new value values variable var result results array list item items element
elements data function method code line current given next object string number simply just our
you your as at or up out over through""".split())
DOC_GENERIC = GENERIC | set("""returns return param params parameter parameters arg args argument
arguments type types int integer str string bool boolean none null optional dict float get gets
set sets takes self cls instance id name kwargs yields yield""".split())
# code pattern -> words a comment that only restates that code would use
CODE_WORDS = [(re.compile(p), w) for p, w in [
    (r"\bfor\b|\bforEach\b|\bwhile\b|\.map\(|\beach\b",
     {"loop", "loops", "iterate", "iterates", "iterating", "looping", "go", "goes"}),
    (r"\breturn\b", {"return", "returns", "returning", "give", "gives"}),
    (r"\+\+|\+=\s*1\b", {"increment", "increments", "increase", "increases", "add", "adds",
                         "one", "counter", "count", "by"}),
    (r"--|-=\s*1\b", {"decrement", "decrements", "decrease", "decreases", "subtract", "one", "by"}),
    (r"\bif\b|\?", {"check", "checks", "if", "whether", "test", "tests"}),
    (r"(?<![=!<>])=(?!=)", {"set", "sets", "assign", "assigns", "store", "stores", "save",
                            "initialize", "initialise", "init", "create", "creates"}),
    (r"\bimport\b|\brequire\(|\binclude\b|\buse\b", {"import", "imports", "require", "include",
                                                    "load", "module"}),
    (r"\bprint|console\.log|\blog\b|\becho\b|println|printf", {"print", "prints", "log", "logs",
                                                             "output", "display", "show"}),
    (r"\bdef\b|\bfunction\b|\bfn\b|\bfunc\b", {"define", "defines", "function", "declare"}),
    (r"\bappend\b|\bpush\b|\badd\b", {"append", "add", "adds", "push", "insert"}),
    (r"\(", {"call", "calls", "invoke", "invokes", "create", "creates", "make"}),
]]
REASON = re.compile(r"\b(?:because|since|so that|otherwise|avoid|avoids|workaround|why|hack|todo|"
                    r"fixme|xxx|note|see|must|needs?|bug|issue|unless|instead|in case|prevent|"
                    r"prevents|guard|so|but|until|cannot|can't|don't|never|only|required|"
                    r"requires|expects?)\b", re.I)
WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SENT_END = re.compile(r"(?<=[.!?])\s+|\n(?=[ \t]*(?:[-*+]|\d+[.)])[ \t])")
# inline code, URLs, link targets, HTML tags, reference links, and short quotes
MASKS = [re.compile(p, re.M) for p in (
    r"`[^`\n]+`", r"<?https?://[^\s)>\]]+>?", r"\]\([^)\s]*\)", r"<[^>\n]+>",
    r"^[ \t]*\[[^\]\n]*\]:[ \t]*\S+", r"\"[^\"\n]{1,80}\"", r"“[^”\n]{1,80}”")]

# ---------------------------------------------------------------- files

LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
             "bun.lockb", "bun.lock", "cargo.lock", "poetry.lock", "pipfile.lock", "uv.lock",
             "composer.lock", "gemfile.lock", "go.sum", "flake.lock", "pubspec.lock",
             "podfile.lock", "mix.lock", "packages.lock.json"}
VENDORED = {"vendor", "vendors", "node_modules", "third_party", "third-party", "thirdparty",
            "bower_components", "site-packages", ".venv", "venv"}
LICENSE_FILES = re.compile(r"^(?:licen[cs]e|copying|notice|unlicense)(?:[.-].*)?$", re.I)
PROSE_NAMES = re.compile(r"^(?:readme|changelog|contributing|authors|history|news)$", re.I)
PROSE_EXT = {".md": "md", ".mdx": "md", ".markdown": "md", ".rst": "rst", ".txt": "txt"}

# Comment syntax per language family. "word": a line marker only counts at the
# start of a word (so `$#` and `a#b` aren't comments).
LANGS = {
    "c": {"line": ["//"], "block": [("/*", "*/")], "strings": "\"'`"},
    "rust": {"line": ["//"], "block": [("/*", "*/")], "strings": "\""},
    "css": {"line": [], "block": [("/*", "*/")], "strings": "\"'"},
    "scss": {"line": ["//"], "block": [("/*", "*/")], "strings": "\"'"},
    "hash": {"line": ["#"], "block": [], "strings": "\"'", "word": True},
    "php": {"line": ["//", "#"], "block": [("/*", "*/")], "strings": "\"'"},
    "ps1": {"line": ["#"], "block": [("<#", "#>")], "strings": "\"'", "word": True},
    "tf": {"line": ["#", "//"], "block": [("/*", "*/")], "strings": "\""},
    "sql": {"line": ["--"], "block": [("/*", "*/")], "strings": "'\""},
    "lua": {"line": ["--"], "block": [("--[[", "]]")], "strings": "'\""},
    "hs": {"line": ["--"], "block": [("{-", "-}")], "strings": "\""},
    "html": {"line": [], "block": [("<!--", "-->")], "strings": ""},
}
EXT_LANG = {ext: lang for lang, exts in {
    "c": ".js .jsx .mjs .cjs .ts .tsx .mts .cts .c .h .cc .cpp .cxx .hpp .hh .java .go .swift "
         ".kt .kts .scala .cs .dart .groovy .gradle .m .mm .proto .zig .sol",
    "rust": ".rs",
    "css": ".css",
    "scss": ".scss .less",
    "hash": ".sh .bash .zsh .fish .rb .pl .pm .r .yaml .yml .toml .cfg .conf .ini .mk .cmake "
            ".nix .ex .exs .jl .tcl .awk .dockerfile",
    "php": ".php",
    "ps1": ".ps1 .psm1",
    "tf": ".tf .hcl",
    "sql": ".sql",
    "lua": ".lua",
    "hs": ".hs .elm",
    "html": ".html .htm .xml .svg .vue .svelte .xhtml",
}.items() for ext in exts.split()}
NAME_LANG = {"makefile": "hash", "dockerfile": "hash", "gemfile": "hash", "rakefile": "hash",
             "justfile": "hash", "vagrantfile": "hash", "cmakelists.txt": "hash"}


def file_type(rel):
    """"py", "md", "rst", "txt", a LANGS key, or None when unsupported."""
    name = rel.rsplit("/", 1)[-1]
    lower = name.lower()
    if lower in NAME_LANG:
        return NAME_LANG[lower]
    ext = os.path.splitext(lower)[1]
    if ext in (".py", ".pyi"):
        return "py"
    if ext in PROSE_EXT:
        return PROSE_EXT[ext]
    if not ext and PROSE_NAMES.match(name):
        return "md"
    return EXT_LANG.get(ext)


# Copied from protected-files-guard/hooks/protected_files_guard.py.
def glob_re(pattern):
    """Compile a gitignore-style glob. `**/` is zero or more directories, `/**`
    everything below, `*` and `?` never cross `/`. Case-insensitive, because
    macOS and Windows file systems are, so `.ENV` is the same file as `.env`."""
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
    return re.compile(out, re.IGNORECASE)


def skip_reason(rel, data, ignore):
    """Why a file isn't scanned, or None. `data` is its bytes."""
    name = rel.rsplit("/", 1)[-1]
    if any(p.fullmatch(rel) for p in ignore):
        return "ignored by config"
    if any(part.lower() in VENDORED for part in rel.split("/")[:-1]):
        return "vendored"
    if name.lower() in LOCKFILES:
        return "lockfile"
    if LICENSE_FILES.match(name):
        return "license"
    if len(data) > MAX_FILE_BYTES:
        return "too large"
    if b"\0" in data[:8000]:
        return "binary"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    head = text.split("\n", 50)[:50]
    ftype = file_type(rel)
    if ".min." in name.lower() or (
            ftype not in PROSE_EXT.values() and any(len(l) > 1000 for l in head)):
        return "minified"  # prose is exempt: a Markdown paragraph is often one long line
    if GENERATED.search("\n".join(head[:10])):
        return "generated"
    if ftype is None:
        return "unsupported type"
    return None


# ---------------------------------------------------------------- git


def git(cwd, *args, stdin=None):
    """Run read-only git in cwd; return stdout bytes, or None on any failure."""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def git_text(cwd, *args, stdin=None):
    out = git(cwd, *args, stdin=stdin)
    return None if out is None else out.decode("utf-8", "replace").strip()


def unquote(path):
    """Undo git's C-style quoting of unusual path names."""
    if not path.startswith('"'):
        return path
    raw = path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
    return raw.encode("latin-1", "replace").decode("utf-8", "replace")


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
DIFF_OPTS = ["-U0", "-M", "--no-color", "--no-ext-diff", "--no-textconv",
             "--src-prefix=a/", "--dst-prefix=b/"]


def added_lines(diff):
    """{path: set of added line numbers} from a -U0 diff."""
    files, cur = {}, None
    for line in diff.split("\n"):
        if line.startswith("+++ "):
            p = unquote(line[4:].rstrip("\t"))
            cur = p[2:] if p.startswith("b/") else None
            if cur is not None:
                files.setdefault(cur, set())
        elif line.startswith("@@") and cur is not None:
            m = HUNK.match(line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                files[cur].update(range(start, start + count))
    return {f: s for f, s in files.items() if s}


def untracked(root):
    out = git(root, "ls-files", "-z", "--others", "--exclude-standard")
    return [p for p in out.decode("utf-8", "replace").split("\0") if p] if out else []


# ---------------------------------------------------------------- config


def check_config(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("the config must be a JSON object")
    for key in ("ignore", "extraPhrases", "allowPhrases", "voiceSampleFiles"):
        v = cfg.get(key, [])
        if not isinstance(v, list) or not all(isinstance(s, str) and s.strip() for s in v):
            raise ValueError("\"{}\" must be a list of non-empty strings".format(key))
    for key in ("emDashPer100Words", "commentRatio", "nudgeThreshold"):
        v = cfg.get(key, 1)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ValueError("\"{}\" must be a positive number".format(key))
    if not isinstance(cfg.get("commitNudge", False), bool):
        raise ValueError("\"commitNudge\" must be true or false")


def load_config(root):
    """Return (config, warnings). An invalid file falls back to the defaults."""
    try:
        with open(os.path.join(root, CONFIG_FILE), encoding="utf-8") as f:
            cfg = json.load(f)
        check_config(cfg)
    except FileNotFoundError:
        return dict(DEFAULT_CONFIG), []
    except (OSError, ValueError) as e:
        return dict(DEFAULT_CONFIG), [
            "humanizer: ignoring invalid {} and using the defaults: {}".format(CONFIG_FILE, e)]
    warnings = ["humanizer: unknown key {!r} in {} is ignored".format(k, CONFIG_FILE)
                for k in cfg if k not in DEFAULT_CONFIG]
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
    return merged, warnings


# ---------------------------------------------------------------- extraction
# A region is {"kind", "lines": [(lineno, text)], "code", "func"}. Line text has
# comment markers removed. `code` is the line a comment annotates. `func` is
# (name, params) for a docstring.


def region(kind, lines, code=None, func=None):
    return {"kind": kind, "lines": lines, "code": code, "func": func}


def strip_marker(text, opener, closer):
    """Comment body lines without their markers. Returns ([lines], is_doc)."""
    raw = text.split("\n")
    doc, out = False, []
    for k, line in enumerate(raw):
        s = line.strip()
        if k == 0:
            s = s[len(opener):]
            if opener in ("//", "/*") and s[:1] in ("/", "!", "*") and not s.startswith("*/"):
                doc, s = True, s[1:]  # ///, //!, /**, /*!
            if opener in ("#", "--"):
                s = s.lstrip(opener[0])
        if closer and k == len(raw) - 1 and s.rstrip().endswith(closer):
            s = s.rstrip()[:-len(closer)]
        s = s.strip()
        if opener == "/*" and k and s.startswith("*"):
            s = s[1:].strip()
        out.append(s)
    return out, doc


def keep_comment(lines, start):
    """False for lint directives, shebangs, encoding lines, and license headers."""
    body = "\n".join(lines)
    if DIRECTIVE.search(body):
        return False
    return not (LICENSE.search(body) and (start <= 5 or "spdx" in body.lower()))


def next_code(src, end, markers):
    """The first non-blank line after line `end`, unless it's a comment."""
    for line in src[end:]:
        s = line.strip()
        if s:
            return None if s.startswith(tuple(markers)) else s
    return None


def lex_comments(text, lang):
    """[(start offset, end offset, opener, closer)] for each comment, string-aware."""
    spec = LANGS[lang]
    out, i, n = [], 0, len(text)
    # ponytail: heuristic lexer; heredocs, regex literals, and nested template strings can fool it
    while i < n:
        hit = next(((o, c) for o, c in spec["block"] if text.startswith(o, i)), None)
        if hit:
            end = text.find(hit[1], i + len(hit[0]))
            end = n if end < 0 else end + len(hit[1])
            out.append((i, end, hit[0], hit[1]))
            i = end
            continue
        marker = next((m for m in spec["line"] if text.startswith(m, i) and (
            not spec.get("word") or i == 0 or text[i - 1] in " \t\n")), None)
        if marker:
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append((i, end, marker, None))
            i = end
            continue
        c = text[i]
        if c in spec["strings"]:
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            i = j + 1 if j < n else i + 1  # unterminated: treat the quote as text
        else:
            i += 1
    return out


def generic_regions(text, lang):
    src = text.split("\n")
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    markers = LANGS[lang]["line"] + [o for o, _ in LANGS[lang]["block"]]
    pieces = []
    for s, e, opener, closer in lex_comments(text, lang):
        line = bisect.bisect_right(starts, s)
        col = s - starts[line - 1]
        lines, doc = strip_marker(text[s:e], opener, closer)
        pieces.append({"start": line, "end": line + len(lines) - 1, "lines": lines, "doc": doc,
                       "full": not src[line - 1][:col].strip(), "opener": opener,
                       "block": closer is not None, "col": col})
    return group_pieces(pieces, src, markers)


def group_pieces(pieces, src, markers):
    """Drop directive lines, then merge consecutive full-line comments into one region."""
    merged = []
    for p in pieces:
        if not p["block"] and DIRECTIVE.search(p["lines"][0]):
            continue  # a shebang or noqa line doesn't take its neighbours with it
        last = merged[-1] if merged else None
        if (last and not p["block"] and not last["block"] and p["full"] and last["full"]
                and p["opener"] == last["opener"] and p["doc"] == last["doc"]
                and p["start"] == last["end"] + 1):
            last["lines"] += p["lines"]
            last["end"] = p["end"]
        else:
            merged.append(dict(p, lines=list(p["lines"])))
    out = []
    for p in merged:
        if not keep_comment(p["lines"], p["start"]):
            continue
        if p["full"]:
            code = next_code(src, p["end"], markers)
        else:
            code = src[p["start"] - 1][:p["col"]].strip()
        numbered = list(zip(range(p["start"], p["end"] + 1), p["lines"]))
        if p["doc"]:
            out.append(region("docstring", numbered, code, signature(code)))
        else:
            out.append(region("comment", numbered, code))
    return out


SIG = re.compile(r"(?:fn|func|function|def)\s+(?:\([^)]*\)\s*)?(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)"
                 r"|(\w+)\s*[=:]\s*(?:async\s*)?(?:function\s*)?\(([^)]*)\)"
                 r"|(\w+)\s*\(([^)]*)\)\s*(?:[:{]|->|throws)")


def signature(code):
    """(name, [identifiers in the parameter list]) of a declaration line, or None."""
    m = SIG.search(code or "")
    if not m:
        return None
    g = m.groups()
    name, params = next((g[k], g[k + 1]) for k in (0, 2, 4) if g[k])
    return name, IDENT.findall(params)


def python_regions(text):
    src = text.split("\n")
    pieces = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                pieces.append({"start": row, "end": row, "lines": strip_marker(tok.string, "#", None)[0],
                               "doc": False, "full": not src[row - 1][:col].strip(),
                               "opener": "#", "block": False, "col": col})
    except (tokenize.TokenError, SyntaxError):
        return generic_regions(text, "hash")
    regions = group_pieces(pieces, src, ["#"])
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return regions
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if not (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            continue
        first, last = body[0].lineno, body[0].end_lineno
        lines = []
        for k in range(first, last + 1):
            s = src[k - 1].strip()
            if k == first:
                s = re.sub(r"^[rRuUbBfF]*(?:\"\"\"|'''|\"|')", "", s)
            if k == last:
                s = re.sub(r"(?:\"\"\"|'''|\"|')$", "", s.rstrip())
            lines.append((k, s.strip()))
        if not keep_comment([t for _, t in lines], first):
            continue
        func = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
            names += [x.arg for x in (a.vararg, a.kwarg) if x]
            func = (node.name, [x for x in names if x not in ("self", "cls")])
        elif isinstance(node, ast.ClassDef):
            func = (node.name, [])
        regions.append(region("docstring", lines, func=func))
    return regions


FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
VERSION_HEADING = re.compile(r"^\[?v?\d+\.\d+")


def prose_regions(text, ftype, name):
    """Paragraphs, list blocks, and headings. Skips code blocks, front matter,
    and released changelog entries."""
    lines = text.split("\n")
    regions, cur = [], []
    fence, literal, published = None, False, False
    changelog = name.lower().startswith(("changelog", "history", "news"))

    def flush():
        if cur:
            regions.append(region("prose", list(cur)))
            del cur[:]

    first = 0
    if ftype == "md" and lines and lines[0].strip() == "---":  # front matter
        end = next((k for k in range(1, len(lines)) if lines[k].strip() in ("---", "...")), None)
        if end is not None:
            first = end + 1
    for k in range(first, len(lines)):
        n, line = k + 1, lines[k]
        s = line.strip()
        if fence:
            m = FENCE.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if literal:  # rst literal block: the indented lines after `::`
            if not s or line[:1] in " \t":
                continue
            literal = False
        m = FENCE.match(line)
        if ftype != "rst" and m:
            flush()
            fence = m.group(1)
            continue
        if not s:
            flush()
            continue
        if ftype == "rst":
            if re.fullmatch(r"([=\-~^\"'`#*+])\1{2,}", s):  # heading underline
                continue
            if s.startswith(".. "):
                flush()
                literal = True
                continue
            if s.endswith("::"):
                literal = True
        if name.lower().endswith(".mdx") and re.match(r"(?:import|export)\s", s):
            continue
        h = HEADING.match(line) if ftype == "md" else None
        if h:
            flush()
            if changelog:
                published = bool(VERSION_HEADING.match(h.group(1))) and "unreleased" not in s.lower()
            if not published:
                regions.append(region("prose", [(n, line)]))
            continue
        if not published:
            cur.append((n, line))
    flush()
    return regions


def extract(text, rel):
    ftype = file_type(rel)
    if ftype == "py":
        return python_regions(text)
    if ftype in ("md", "rst", "txt"):
        return prose_regions(text, ftype, rel.rsplit("/", 1)[-1])
    return generic_regions(text, ftype)


# ---------------------------------------------------------------- analysis


def mask(text):
    """Blank out MASKS matches, keeping offsets. Quoted text is someone's words, not ours."""
    for rx in MASKS:
        text = rx.sub(lambda m: " " * len(m.group()), text)
    return text


def sentences(text):
    """[(offset, sentence)] split at sentence ends and list items."""
    out, pos = [], 0
    for m in SENT_END.finditer(text):
        out.append((pos, text[pos:m.start()]))
        pos = m.end()
    out.append((pos, text[pos:]))
    return [(o, s) for o, s in out if s.strip()]


def words(text):
    return [w.lower().strip("'’-") for w in WORD.findall(text)]


def ident_words(code):
    """Lowercased identifiers and their parts: fooBar_baz -> foobar_baz, foo, bar, baz."""
    out = set()
    for ident in IDENT.findall(code):
        out.add(ident.lower())
        out.update(p.lower() for p in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", ident))
    return out


def covered(word, vocab):
    return word in vocab or word.rstrip("s") in vocab or word + "s" in vocab


def restates(comment, code):
    """True when a short comment's words are mostly the code's identifiers and keywords."""
    if not code or REASON.search(comment):
        return False
    ws = words(comment)
    if not ws or len(ws) > 12:
        return False
    vocab = ident_words(code)
    for rx, extra in CODE_WORDS:
        if rx.search(code):
            vocab |= extra
    content = [w for w in ws if w not in GENERIC]
    return bool(content) and sum(covered(w, vocab) for w in content) / len(content) >= 0.75


def echoes(doc, func):
    """True when a docstring only repeats its function's name and parameters."""
    ws = words(doc)
    if not func or not ws or len(ws) > 40:
        return False
    name, params = func
    vocab = ident_words(" ".join([name] + params))
    content = [w for w in ws if w not in DOC_GENERIC]
    return not content or sum(covered(w, vocab) for w in content) / len(content) >= 0.85


class Analyzer:
    def __init__(self, cfg, sample):
        self.cfg, self.sample = cfg, sample
        self.allow = [p.casefold() for p in cfg["allowPhrases"]]
        self.phrases = [(pid, sev, re.compile(rx, re.I | re.M)) for pid, sev, rx in PHRASES]
        self.phrases += [("phrase.extra", MEDIUM, re.compile(r"\b" + re.escape(p) + r"\b", re.I))
                         for p in cfg["extraPhrases"]]

    def analyze(self, reg):
        """Findings for one region. Each has whole_region set when it judges the
        region as a whole rather than one line."""
        lines = reg["lines"]
        text = "\n".join(t for _, t in lines)
        masked = mask(text)
        starts = [0]
        for _, t in lines[:-1]:
            starts.append(starts[-1] + len(t) + 1)
        found = []

        def add(fid, sev, offset, excerpt, message, whole=False, style=None):
            found.append({"id": fid, "severity": sev,
                          "line": lines[bisect.bisect_right(starts, offset) - 1][0],
                          "excerpt": " ".join(excerpt.split())[:120], "message": message,
                          "whole_region": whole, "style": style})

        for pid, sev, rx in self.phrases:
            for m in rx.finditer(masked):
                hit = m.group().strip(" .!?,\n")
                if any(a in hit.casefold() or pid.endswith("." + a) for a in self.allow):
                    continue
                add(pid, sev, m.start(), hit, "stock phrase", style=("phrase", hit))
        ensures = list(ENSURE_THAT.finditer(masked))
        if len(ensures) >= 2 and "ensure that" not in self.allow:
            add("phrase.ensure-that", MEDIUM, ensures[1].start(), ensures[1].group(),
                "\"ensure that\" {} times in one block".format(len(ensures)), whole=True)

        sents = sentences(masked)
        for off, s in sents:
            ws = words(s)
            hype = [w for w in ws if w in HYPE]
            if len(hype) >= 3:
                add("hype-stack", MEDIUM, off, s, "stacked hype words: " + ", ".join(hype))
            hedges = [w for w in ws if w in HEDGES]
            if len(hedges) >= 3:
                add("hedge.stack", MEDIUM, off, s, "stacked hedges: " + ", ".join(hedges))
            for m in TRIPLET.finditer(s):
                if all(w.lower() in PADDING for w in m.groups()):
                    add("formula.rule-of-three", LOW, off + m.start(), m.group(),
                        "rule-of-three padding")

        nwords = len(words(masked))
        dashes = masked.count("—")
        limit = self.cfg["emDashPer100Words"]
        if dashes >= 2 and dashes * 100.0 / max(nwords, 1) > limit:
            add("style.em-dash", LOW, masked.index("—"), "{} em-dashes in {} words".format(
                dashes, nwords), "more than {:g} em-dashes per 100 words".format(limit),
                whole=True, style=("em_dash", None))

        lengths = [len(words(s)) for _, s in sents if len(words(s)) >= 3]
        if len(lengths) >= 5 and statistics.mean(lengths) >= 6 and \
                statistics.pstdev(lengths) / statistics.mean(lengths) < 0.2:
            add("style.uniform-sentences", LOW, 0, sents[0][1], "{} sentences of near-identical "
                "length (about {:.0f} words)".format(len(lengths), statistics.mean(lengths)),
                whole=True)
        openers = Counter(words(s)[0] for _, s in sents if len(words(s)) >= 3)
        if openers:
            w, c = openers.most_common(1)[0]
            if c >= 3 and c * 2 >= sum(openers.values()):
                add("style.repeated-openers", LOW, 0, w, "{} sentences start with {!r}".format(
                    c, w), whole=True)

        bullets = []
        for (_, raw), off in zip(lines, starts):
            m = HEADING.match(raw) or BULLET.match(raw)
            if BULLET.match(raw):
                bullets.append(m.group(1))
            if EMOJI.match(m.group(1) if m else raw.strip()):
                add("style.emoji-prefix", MEDIUM, off, raw, "emoji or checkmark prefix",
                    style=("emoji", None))
        if len(bullets) >= 3 and all(BOLD_LABEL.match(b) for b in bullets):
            add("formula.bold-label-bullets", LOW, 0, bullets[0],
                "every bullet starts with a bold label", whole=True, style=("bold_labels", None))

        if reg["kind"] in ("comment", "docstring"):
            for m in TUTORIAL.finditer(masked):
                add("comment.tutorial-voice", MEDIUM, m.start(), m.group(), "tutorial voice")
        code = reg["code"]
        if reg["kind"] == "comment" and code:
            if restates(text, code):
                add("comment.restates-code", MEDIUM, 0, text,
                    "restates the code: " + code[:60], whole=True)
            elif (len(lines) <= 3 and nwords >= 8 and not REASON.search(text)
                  and not code.rstrip().endswith((":", "{"))
                  and len(" ".join(text.split())) > self.cfg["commentRatio"] * len(code)):
                add("comment.over-explains", LOW, 0, text, "comment is over {:g}x the length "
                    "of the line it annotates".format(self.cfg["commentRatio"]), whole=True)
        if reg["kind"] == "docstring" and echoes(text, reg["func"]):
            add("docstring.echo", MEDIUM, 0, text,
                "only repeats the name and parameters of " + reg["func"][0], whole=True)
        return [self.downgrade(f) for f in found]

    def downgrade(self, f):
        """A finding that matches the repo's existing docs drops to info."""
        style, s = f.pop("style"), self.sample
        if not style or not s or s["words"] < SAMPLE_MIN_WORDS:
            return f
        kind, hit = style
        if ((kind == "phrase" and s["text"].count(hit.casefold()) >= 2)
                or (kind == "emoji" and s["emoji"] > 0)
                or (kind == "em_dash" and s["em_dash"] >= self.cfg["emDashPer100Words"] / 2)
                or (kind == "bold_labels" and s["bold_labels"] >= 3)):
            f["severity"], f["downgraded"] = INFO, "matches the repo's existing docs"
        return f


# ---------------------------------------------------------------- voice sample


def prose_metrics(regions):
    text = "\n".join(mask("\n".join(t for _, t in r["lines"])) for r in regions)
    ws = words(text)
    lens = [len(words(s)) for _, s in sentences(text) if len(words(s)) >= 3]
    per100 = 100.0 / max(len(ws), 1)
    return {"words": len(ws),
            "sentence_length": round(statistics.mean(lens), 1) if lens else None,
            "emoji": round(len(EMOJI.findall(text)) * per100, 2),
            "em_dash": round(text.count("—") * per100, 2),
            "bold_labels": sum(1 for r in regions for _, t in r["lines"]
                               if BULLET.match(t) and BOLD_LABEL.match(BULLET.match(t).group(1))),
            "text": text.casefold()}


def comment_density(items):
    """Comment lines / non-blank lines over [(text, rel, lines to count or None)]."""
    comment, total = 0, 0
    for text, rel, only in items:
        src = text.split("\n")
        keep = set(range(1, len(src) + 1)) if only is None else set(only)
        total += sum(1 for n in keep if n <= len(src) and src[n - 1].strip())
        comment += sum(1 for r in extract(text, rel) for n, _ in r["lines"] if n in keep)
    return round(comment / total, 3) if total else None


def voice_sample(root, cfg, scope):
    """Metrics of the repo's tracked docs and code, minus the lines being scanned."""
    out = git(root, "ls-files", "-z")
    if out is None:
        return None
    globs = [glob_re(g) for g in cfg["voiceSampleFiles"]]
    ignore = [glob_re(g) for g in cfg["ignore"]]
    prose, code, budget = [], [], SAMPLE_BYTES
    for rel in sorted(p for p in out.decode("utf-8", "replace").split("\0") if p):
        excluded = scope.get(rel, set())
        is_doc = any(g.fullmatch(rel) for g in globs)
        if excluded is None or budget <= 0:
            continue
        if is_doc and len(prose) >= SAMPLE_FILES:
            continue
        if not is_doc and (file_type(rel) in (None, "md", "rst", "txt")
                           or len(code) >= SAMPLE_CODE_FILES):
            continue
        try:
            with open(os.path.join(root, *rel.split("/")), "rb") as f:
                data = f.read(MAX_FILE_BYTES + 1)
        except OSError:
            continue
        if skip_reason(rel, data, ignore):
            continue
        budget -= len(data)
        text = data.decode("utf-8")
        if is_doc:
            for r in extract(text, rel):
                lines = [(n, t) for n, t in r["lines"] if n not in excluded]
                if lines and r["kind"] == "prose":
                    prose.append(region("prose", lines))
        else:
            code.append((text, rel, [k for k in range(1, text.count("\n") + 2)
                                     if k not in excluded]))
    metrics = prose_metrics(prose)
    metrics["comment_density"] = comment_density(code)
    return metrics


def deviations(new, sample, cfg):
    if sample["words"] < SAMPLE_MIN_WORDS:
        return ["the repo's docs are too short to compare against"]
    out = []
    if new["sentence_length"] and sample["sentence_length"]:
        ratio = new["sentence_length"] / sample["sentence_length"]
        if ratio > 1.5 or ratio < 0.5:
            out.append("sentences average {} words; the repo's docs average {}".format(
                new["sentence_length"], sample["sentence_length"]))
    if new["emoji"] > 0 and sample["emoji"] == 0:
        out.append("uses emoji; the repo's docs don't")
    if new["em_dash"] > max(cfg["emDashPer100Words"], 2 * sample["em_dash"]):
        out.append("{} em-dashes per 100 words; the repo's docs use {}".format(
            new["em_dash"], sample["em_dash"]))
    nd, sd = new["comment_density"], sample["comment_density"]
    if nd is not None and sd is not None and nd > 0.3 and nd > 2 * sd:
        out.append("{:.0%} of added code lines are comments; the repo averages {:.0%}".format(
            nd, sd))
    return out


# ---------------------------------------------------------------- scope


class ArgError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgError(message)


def parse_args(argv):
    p = Parser(prog="scan.py", add_help=False)
    p.add_argument("paths", nargs="*")
    p.add_argument("--staged", action="store_true")
    p.add_argument("--diff", metavar="BASE")
    args = p.parse_args(argv)
    if sum(map(bool, (args.paths, args.staged, args.diff))) > 1:
        raise ArgError("use paths, --staged, or --diff <base>, not more than one")
    return args


def fail(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


def path_scope(root, cwd, paths, in_repo):
    """({rel: None}, skipped) for explicit files and directories, scanned whole."""
    scope, skipped = {}, []
    for p in paths:
        full = os.path.abspath(os.path.join(cwd, p))
        if not os.path.exists(full):
            raise ArgError("no such file or directory: " + p)
        if os.path.isdir(full):
            if in_repo:  # tracked and untracked, minus .gitignore
                out = git(full, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                          "--full-name", ".")
                rels = [r for r in (out or b"").decode("utf-8", "replace").split("\0") if r]
            else:
                rels = [os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
                        for d, _, fs in os.walk(full) for f in fs]
            scope.update((rel, None) for rel in rels)
            continue
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        if in_repo and git(root, "check-ignore", "-q", "--", rel) is not None:
            skipped.append({"file": rel, "reason": "gitignored"})
        else:
            scope[rel] = None
    return scope, skipped


def diff_scope(root, args):
    """({rel: added line numbers, or None for a whole new file}, mode)."""
    if args.staged:
        out = git(root, "diff", "--cached", *DIFF_OPTS)
        if out is None:
            raise RuntimeError("git diff --cached failed")
        return added_lines(out.decode("utf-8", "replace")), "staged"
    if args.diff:
        if git(root, "rev-parse", "-q", "--verify", args.diff + "^{commit}") is None:
            return None, "base-not-found"
        base = git_text(root, "merge-base", args.diff, "HEAD")
        if not base:
            return None, "base-not-found"
        mode = "diff"
    else:  # HEAD, or the empty tree in a repo with no commits yet
        base = (git_text(root, "rev-parse", "-q", "--verify", "HEAD")
                or git_text(root, "hash-object", "-t", "tree", "--stdin", stdin=b""))
        mode = "working-tree"
    out = git(root, "diff", base, *DIFF_OPTS)
    if out is None:
        raise RuntimeError("git diff failed")
    scope = added_lines(out.decode("utf-8", "replace"))
    scope.update((rel, None) for rel in untracked(root))
    return scope, mode


def read(root, rel, staged):
    if staged:
        return git(root, "show", ":" + rel)
    try:
        with open(os.path.join(root, *rel.split("/")), "rb") as f:
            return f.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None


# ---------------------------------------------------------------- main


def scan(argv, cwd):
    try:
        args = parse_args(argv)
    except ArgError as e:
        return fail("bad-arguments", str(e))
    top = git_text(cwd, "rev-parse", "--show-toplevel")
    if top is None and not args.paths:
        return fail("not-a-repo", "{} is not inside a git repository. Pass file paths to scan "
                                  "them directly.".format(cwd))
    root = os.path.normpath(top) if top else cwd
    cfg, warnings = load_config(root)
    ignore = [glob_re(g) for g in cfg["ignore"]]
    skipped = []
    if args.paths:
        try:
            scope, skipped = path_scope(root, cwd, args.paths, top is not None)
        except ArgError as e:
            return fail("bad-arguments", str(e))
        mode = "paths"
    else:
        scope, mode = diff_scope(root, args)
        if mode == "base-not-found":
            return fail("base-not-found", "can't find a merge base with {!r}. Check the branch "
                                          "name, or run git fetch.".format(args.diff))
    sample = voice_sample(root, cfg, scope) if top else None
    analyzer = Analyzer(cfg, sample)

    regions, new_prose, code_items, examined = [], [], [], 0
    for rel in sorted(scope):
        only = scope[rel]
        data = read(root, rel, mode == "staged")
        if data is None:
            skipped.append({"file": rel, "reason": "unreadable"})
            continue
        why = skip_reason(rel, data, ignore)
        if why:
            if why != "unsupported type":  # JSON, images, ...: no comments to read
                skipped.append({"file": rel, "reason": why})
            continue
        text = data.decode("utf-8")
        if file_type(rel) not in ("md", "rst", "txt"):
            code_items.append((text, rel, only))
        for reg in extract(text, rel):
            if only is not None:
                if reg["kind"] == "prose":  # just the added lines of a paragraph
                    reg["lines"] = [(n, t) for n, t in reg["lines"] if n in only]
                if not any(n in only for n, _ in reg["lines"]):
                    continue
            examined += 1
            if reg["kind"] == "prose":
                new_prose.append(reg)
            findings = [f for f in analyzer.analyze(reg)
                        if only is None or f["whole_region"] or f["line"] in only]
            if not findings:
                continue
            for f in findings:
                del f["whole_region"]
            findings.sort(key=lambda f: (-WEIGHT[f["severity"]], f["line"]))
            regions.append({
                "file": rel, "start": reg["lines"][0][0], "end": reg["lines"][-1][0],
                "kind": reg["kind"], "text": "\n".join(t for _, t in reg["lines"]),
                "findings": findings, "score": sum(WEIGHT[f["severity"]] for f in findings)})

    new = prose_metrics(new_prose)
    new["comment_density"] = comment_density(code_items)
    total = sum(r["score"] for r in regions)
    nwords = new["words"] + sum(len(words(r["text"])) for r in regions if r["kind"] != "prose")
    by_sev = Counter(f["severity"] for r in regions for f in r["findings"])
    voice = None
    if sample:
        voice = {"sample": {k: v for k, v in sample.items() if k != "text"},
                 "new": {k: v for k, v in new.items() if k != "text"},
                 "deviations": deviations(new, sample, cfg)}
    regions.sort(key=lambda r: (-r["score"], r["file"], r["start"]))
    return {
        "ok": True, "note": NOTE, "mode": mode, "base": args.diff, "root": root,
        "config": cfg, "warnings": warnings, "skipped": skipped,
        "summary": {
            "files": len(scope), "regions_examined": examined, "regions_flagged": len(regions),
            "findings": sum(by_sev.values()),
            "by_severity": {s: by_sev.get(s, 0) for s in (HIGH, MEDIUM, LOW, INFO)},
            "score": total, "score_per_100_words": round(total * 100.0 / max(nwords, 1), 1)},
        "voice": voice, "regions": regions}


def main(argv=None):
    try:
        result = scan(sys.argv[1:] if argv is None else argv, os.getcwd())
    except Exception as e:  # always print JSON
        result = fail("internal-error", "{}: {}".format(type(e).__name__, e))
    sys.stdout.write(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
