#!/usr/bin/env python3
"""Collect facts about a repo's tests for the test-writer skill.

Prints exactly one JSON object on stdout and always exits 0. Failures are
reported as {"ok": false, "error": {"code", "message"}}.

Read-only, except --snapshot: it copies the project's tracked and untracked
(not ignored) files, without .git, into a new test-writer-* temp directory for
the mutation check, and removes this user's day-old snapshots.

Standard library only. Python 3.9+. No network.
"""
import argparse
import ast
import fnmatch
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

MAX_LIST = 20000        # files considered from git ls-files
MAX_READ = 200 * 1024   # bytes; bigger files are never read
MAX_SCAN = 2000         # files read when searching for symbols, imports, services
SAMPLE = 50             # test files sampled per language for conventions
MAX_TARGETS = 50
MAX_SYMBOLS = 200
MAX_EVIDENCE = 5
EXEMPLARS = 3

SNAPSHOT_PREFIX = "test-writer-"
SNAPSHOT_MARKER = ".test-writer-snapshot"
SNAPSHOT_MAX_BYTES = 500 * 1024 * 1024
STALE_SECONDS = 24 * 3600

# Never read, whatever the caller asks. Matched against the lowercased basename.
SECRET_NAMES = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_dsa*", "id_ecdsa*",
                "id_ed25519*", "*.p12", "*.pfx", "*.keystore", "*.jks", "credentials.json",
                ".netrc", ".pypirc", ".npmrc")
SECRET_ALLOW = (".env.example", ".env.sample", ".env.template")
SKIP_PARTS = {"node_modules", "vendor", "third_party", "dist", "build", "target", ".venv",
              "venv", "__pycache__", ".tox", ".git"}

LANGS = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
         ".cjs": "javascript", ".ts": "javascript", ".tsx": "javascript", ".mts": "javascript",
         ".cts": "javascript", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "java",
         ".rb": "ruby", ".php": "php", ".cs": "csharp"}

# name: (language, keyword that ties a command to it, default run, file template, single template)
FRAMEWORKS = {
    "pytest": ("python", r"\bpytest\b", "pytest", "pytest {file}", "pytest {file}::{name}"),
    "unittest": ("python", r"\bunittest\b", "python -m unittest", "python -m unittest {file}",
                 "python -m unittest {module}.{name}"),
    "jest": ("javascript", r"\bjest\b", "npx jest", "npx jest {file}", "npx jest {file} -t '{name}'"),
    "vitest": ("javascript", r"\bvitest\b", "npx vitest run", "npx vitest run {file}",
               "npx vitest run {file} -t '{name}'"),
    "mocha": ("javascript", r"\bmocha\b", "npx mocha", "npx mocha {file}",
              "npx mocha {file} --grep '{name}'"),
    "go test": ("go", r"\bgo test\b", "go test ./...", "go test ./{dir}",
                "go test ./{dir} -run '^{name}$'"),
    "cargo test": ("rust", r"\bcargo (?:test|nextest)\b", "cargo test", "cargo test --test {stem}",
                   "cargo test {name}"),
    "junit": ("java", r"\b(?:mvnw?|gradlew?)\b[^\n]*\b(?:test|verify|check)\b", "mvn test",
              "mvn test -Dtest={class}", "mvn test -Dtest={class}#{name}"),
    "rspec": ("ruby", r"\brspec\b", "bundle exec rspec", "bundle exec rspec {file}",
              "bundle exec rspec {file}:{line}"),
    "phpunit": ("php", r"\bphpunit\b", "vendor/bin/phpunit", "vendor/bin/phpunit {file}",
                "vendor/bin/phpunit {file} --filter '{name}'"),
    "dotnet test": ("csharp", r"\bdotnet test\b", "dotnet test",
                    "dotnet test --filter 'FullyQualifiedName~{class}'",
                    "dotnet test --filter 'FullyQualifiedName~{class}.{name}'"),
}

# (category, label, regex): a convention counts once per sampled file that matches.
CONVENTIONS = {
    "python": [
        ("naming", "test_ functions", r"^\s*(?:async\s+)?def test_\w+"),
        ("naming", "Test classes", r"^class Test\w*"),
        ("naming", "unittest.TestCase", r"\bTestCase\b"),
        ("fixtures", "@pytest.fixture", r"@pytest\.fixture"),
        ("fixtures", "tmp_path", r"\btmp_path\b"),
        ("mocking", "unittest.mock", r"\bunittest\.mock\b|\bfrom unittest import mock\b|^\s*import mock\b"),
        ("mocking", "pytest-mock mocker", r"\bmocker\b"),
        ("mocking", "monkeypatch", r"\bmonkeypatch\b"),
        ("mocking", "responses", r"^\s*import responses\b|@responses\.activate"),
        ("mocking", "freezegun", r"\bfreezegun\b|\bfreeze_time\b"),
        ("assertions", "plain assert", r"^\s*assert\b"),
        ("assertions", "self.assert*", r"\bself\.assert\w+\("),
        ("assertions", "pytest.raises", r"\bpytest\.raises\b"),
        ("parametrize", "pytest.mark.parametrize", r"@pytest\.mark\.parametrize"),
        ("parametrize", "subTest", r"\bself\.subTest\b"),
        ("setup", "setUp/tearDown", r"\bdef (?:setUp|tearDown)(?:Class)?\b"),
        ("setup", "yield fixtures", r"^\s*yield\b"),
        ("aaa_comments", "Arrange/Act/Assert", r"#\s*(?:Arrange|Act|Assert)\b"),
        ("aaa_comments", "Given/When/Then", r"#\s*(?:Given|When|Then)\b"),
    ],
    "javascript": [
        ("naming", "describe blocks", r"\bdescribe\("),
        ("naming", "it()", r"\bit\("),
        ("naming", "test()", r"^\s*test\("),
        ("mocking", "jest.mock/fn/spyOn", r"\bjest\.(?:mock|fn|spyOn)\b"),
        ("mocking", "vi.mock/fn/spyOn", r"\bvi\.(?:mock|fn|spyOn)\b"),
        ("mocking", "sinon", r"\bsinon\b"),
        ("mocking", "msw", r"""from\s+['"]msw"""),
        ("mocking", "nock", r"\bnock\("),
        ("assertions", "expect().toX", r"\bexpect\([^)]*\)\.(?:not\.)?to[A-Z]"),
        ("assertions", "chai expect().to", r"\bexpect\([^)]*\)\.to\."),
        ("assertions", "node assert", r"""(?:require\(|from\s+)['"](?:node:)?assert"""),
        ("parametrize", "each", r"\b(?:it|test|describe)\.each\b"),
        ("setup", "beforeEach/afterEach", r"\b(?:before|after)Each\("),
        ("setup", "beforeAll/afterAll", r"\b(?:before|after)All\("),
        ("aaa_comments", "Arrange/Act/Assert", r"//\s*(?:Arrange|Act|Assert)\b"),
        ("aaa_comments", "Given/When/Then", r"//\s*(?:Given|When|Then)\b"),
    ],
}
FILE_PATTERNS = {
    "python": [("test_*.py", r"(^|/)test_[^/]*\.py$"), ("*_test.py", r"_test\.py$")],
    "javascript": [("*.test.*", r"\.test\.[^./]+$"), ("*.spec.*", r"\.spec\.[^./]+$"),
                   ("__tests__/", r"(^|/)__tests__/")],
}

SERVICES = [
    ("database", r"^\s*(?:import|from)\s+(?:psycopg2?|psycopg|asyncpg|sqlalchemy|pymongo|motor"
                 r"|MySQLdb|mysql\.connector|pymysql|django\.db|peewee)\b"),
    ("cache", r"^\s*(?:import|from)\s+(?:redis|aioredis|pymemcache)\b"),
    ("network", r"^\s*(?:import|from)\s+(?:requests|httpx|aiohttp|urllib3|boto3|botocore|grpc"
                r"|websockets)\b"),
    ("queue", r"^\s*(?:import|from)\s+(?:pika|kafka|confluent_kafka|aiokafka|celery)\b"),
    ("database", r"""(?:from\s+|require\(\s*)['"](?:pg|mysql2?|mongodb|mongoose|sequelize|typeorm"""
                 r"""|@prisma/client|knex)['"]"""),
    ("cache", r"""(?:from\s+|require\(\s*)['"](?:redis|ioredis|memcached)['"]"""),
    ("network", r"""(?:from\s+|require\(\s*)['"](?:axios|node-fetch|got|undici|aws-sdk|@aws-sdk/[^'"]+)['"]"""),
    ("queue", r"""(?:from\s+|require\(\s*)['"](?:amqplib|kafkajs|bullmq|bull)['"]"""),
]

COVERAGE_REPORTS = ("coverage.xml", "cobertura.xml", "lcov.info", "coverage/lcov.info",
                    "coverage/coverage-final.json", "coverage/cobertura-coverage.xml", ".coverage",
                    "coverage.out", "target/site/jacoco/jacoco.xml",
                    "build/reports/jacoco/test/jacocoTestReport.xml")
CI_FILES = (".gitlab-ci.yml", ".circleci/config.yml", "azure-pipelines.yml", "Jenkinsfile")
INSTALL = re.compile(r"\b(?:pip3?|npm|yarn|pnpm|gem|bundle|go|cargo|uv|apt(?:-get)?|brew)\s+"
                     r"(?:install|add|get|i)\b")
IDENT = re.compile(r"^[A-Za-z_$][\w$]*(?:(?:\.|::)[A-Za-z_$][\w$]*)*$")


def git(cwd, *args):
    """Run read-only git in cwd; return stdout, or None on any failure."""
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=30, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def fail(code, message, **extra):
    out = {"ok": False, "error": {"code": code, "message": message}, "warnings": []}
    out.update(extra)
    return out


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parse_args(argv):
    p = _Parser(prog="detect_tests.py", add_help=False)
    p.add_argument("targets", nargs="*")
    p.add_argument("--diff", metavar="BASE")
    p.add_argument("--snapshot", action="store_true")
    a = p.parse_args(argv)
    if a.snapshot and (a.targets or a.diff is not None):
        raise ValueError("--snapshot takes no other arguments")
    if a.diff is not None:
        if a.targets:
            raise ValueError("pass paths or a symbol, or --diff <base>, not both")
        if not a.diff or a.diff.startswith("-"):
            raise ValueError("--diff needs a base branch or commit")
    return a


# ---------------------------------------------------------------- files

def language(path):
    return LANGS.get(os.path.splitext(path)[1].lower())


def is_secret(path):
    name = posixpath.basename(path.replace(os.sep, "/")).lower()
    return name not in SECRET_ALLOW and any(fnmatch.fnmatchcase(name, p) for p in SECRET_NAMES)


def skipped(path):
    parts = path.split("/")
    return bool(SKIP_PARTS.intersection(parts[:-1])) or parts[-1].endswith((".min.js", ".min.css"))


def is_test(path):
    lang = language(path)
    if not lang:
        return False
    parts = path.split("/")
    name, dirs = parts[-1], parts[:-1]
    stem = name.split(".")[0]
    if lang == "python":
        return name.startswith("test_") or stem.endswith("_test")
    if lang == "javascript":
        return bool(re.search(r"\.(?:test|spec)\.[^.]+$", name)) or "__tests__" in dirs
    if lang == "go":
        return name.endswith("_test.go")
    if lang == "rust":
        return "tests" in dirs
    if lang == "java":
        return bool(re.search(r"(?:Test|Tests|IT|Spec)$", stem)) or ("src" in dirs and "test" in dirs)
    if lang == "ruby":
        return stem.endswith(("_spec", "_test")) or name.startswith("test_")
    if lang == "php":
        return stem.endswith("Test")
    return stem.endswith(("Test", "Tests")) or any(d.endswith((".Tests", ".Test")) for d in dirs)


def subject(path):
    """The name a test file is about: test_foo.py, foo.test.ts, FooTest.java -> foo/Foo."""
    stem = posixpath.basename(path).split(".")[0]
    if stem.startswith("test_"):
        return stem[5:]
    for suffix in ("_test", "_spec", "Tests", "Test", "IT", "Spec"):
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem[:-len(suffix)]
    return stem


class Repo:
    def __init__(self, root):
        self.root = root
        self.real = os.path.realpath(root)
        out = git(root, "-c", "core.quotepath=off", "ls-files", "-co", "--exclude-standard", "-z")
        files = sorted({f for f in (out or "").split("\0") if f and not skipped(f)})
        self.truncated = len(files) > MAX_LIST
        self.files = files[:MAX_LIST]
        self.fileset = set(self.files)
        self._cache = {}
        # A test_*.py with no tests in it (a module named test_something) is source.
        self.tests = [f for f in self.files if is_test(f) and not (
            language(f) == "python"
            and not re.search(r"^\s*(?:async\s+)?def test|\bTestCase\b", self.read(f) or "", re.M))]
        tests = set(self.tests)
        self.sources = [f for f in self.files if language(f) and f not in tests]

    def read(self, rel):
        """Text of a repo file, or None if it is secret, too big, binary, or outside the repo."""
        if rel in self._cache:
            return self._cache[rel]
        text = None
        if not is_secret(rel):
            path = os.path.join(self.root, rel)
            try:
                real = os.path.realpath(path)
                if real.startswith(self.real + os.sep) and not is_secret(real) \
                        and os.path.getsize(path) <= MAX_READ:
                    with open(path, "rb") as f:
                        data = f.read()
                    if b"\0" not in data[:8192]:
                        text = data.decode("utf-8", "replace")
            except OSError:
                pass
        self._cache[rel] = text
        return text

    def json(self, rel):
        try:
            value = json.loads(self.read(rel) or "null")
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def named(self, *patterns):
        return [f for f in self.files
                if any(fnmatch.fnmatchcase(posixpath.basename(f), p) for p in patterns)]


# ---------------------------------------------------------------- frameworks and commands

def package_deps(pkg):
    deps = {}
    for key in ("dependencies", "devDependencies"):
        if isinstance((pkg or {}).get(key), dict):
            deps.update(pkg[key])
    return deps


def python_deps_text(repo):
    names = ["pyproject.toml", "setup.cfg", "tox.ini", "Pipfile", "setup.py"]
    names += [f for f in repo.files if re.match(r"requirements[^/]*\.txt$|requirements/[^/]+\.txt$", f)]
    return "\n".join(repo.read(n) or "" for n in names)


def detect_frameworks(repo):
    found = {}

    def add(name, evidence):
        found.setdefault(name, [])
        if evidence not in found[name] and len(found[name]) < MAX_EVIDENCE:
            found[name].append(evidence)

    pyproject = repo.read("pyproject.toml") or ""
    if "pytest.ini" in repo.fileset:
        add("pytest", "pytest.ini")
    if "[tool.pytest" in pyproject:
        add("pytest", "pyproject.toml")
    if "[pytest]" in (repo.read("tox.ini") or ""):
        add("pytest", "tox.ini")
    if "[tool:pytest]" in (repo.read("setup.cfg") or ""):
        add("pytest", "setup.cfg")
    for f in repo.named("conftest.py")[:2]:
        add("pytest", f)
    if re.search(r"\bpytest\b", python_deps_text(repo)):
        add("pytest", "python dependencies")
    for f in [t for t in repo.tests if language(t) == "python"][:SAMPLE]:
        text = repo.read(f) or ""
        if re.search(r"\bunittest\.TestCase\b|from unittest import[^\n]*\bTestCase\b", text):
            add("unittest", f)
        if re.search(r"^\s*(?:import|from)\s+pytest\b", text, re.M):
            add("pytest", f)

    pkg = repo.json("package.json")
    deps = package_deps(pkg)
    for name in ("jest", "vitest", "mocha"):
        if name in deps:
            add(name, "package.json")
    if pkg and "jest" in pkg:
        add("jest", "package.json")
    for f in repo.named("jest.config.*"):
        add("jest", f)
    for f in repo.named("vitest.config.*", "vitest.workspace.*"):
        add("vitest", f)
    for f in repo.named(".mocharc*"):
        add("mocha", f)

    if "go.mod" in repo.fileset:
        add("go test", "go.mod")
    if "Cargo.toml" in repo.fileset:
        add("cargo test", "Cargo.toml")
    for f in ("pom.xml", "build.gradle", "build.gradle.kts"):
        if re.search(r"junit", repo.read(f) or "", re.I):
            add("junit", f)
    if ".rspec" in repo.fileset:
        add("rspec", ".rspec")
    if re.search(r"\brspec\b", repo.read("Gemfile") or ""):
        add("rspec", "Gemfile")
    for f in ("phpunit.xml", "phpunit.xml.dist"):
        if f in repo.fileset:
            add("phpunit", f)
    if re.search(r"phpunit/phpunit", repo.read("composer.json") or ""):
        add("phpunit", "composer.json")
    for f in repo.named("*.csproj"):
        if re.search(r"Microsoft\.NET\.Test\.Sdk|\bxunit\b|\bnunit\b|\bMSTest\b", repo.read(f) or "", re.I):
            add("dotnet test", f)

    return [{"name": n, "language": FRAMEWORKS[n][0], "evidence": found[n]}
            for n in FRAMEWORKS if n in found]


def make_targets(text, recipe_indent):
    """{target: recipe text} for a Makefile (tab recipes) or justfile (indented recipes)."""
    targets, current = {}, None
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)(?:\s[^:=]*)?:(?![=:])", line)
        if m and not line.startswith((" ", "\t")):
            current = m.group(1)
            targets[current] = line[m.end():]
        elif current and line.startswith(recipe_indent):
            targets[current] += "\n" + line.strip()
        elif line.strip() and not line.startswith(("#", " ", "\t")):
            current = None
    return targets


def command_sources(repo):
    """[(source, run, text, confidence, script_name)] in trust order."""
    out = []
    pkg = repo.json("package.json")
    if pkg and isinstance(pkg.get("scripts"), dict):
        runner = ("pnpm" if "pnpm-lock.yaml" in repo.fileset
                  else "yarn" if "yarn.lock" in repo.fileset else "npm")
        scripts = pkg["scripts"]
        for name in sorted(scripts, key=lambda n: (n != "test", n)):
            text = scripts[name]
            if not isinstance(text, str) or "no test specified" in text:  # npm init's stub
                continue
            run = runner + " test" if name == "test" else runner + " run " + name
            out.append(("package.json", run, text, "high", name))
    for fname, tool, indent in (("Makefile", "make", "\t"), ("justfile", "just", (" ", "\t"))):
        targets = make_targets(repo.read(fname) or "", indent)
        for name in sorted(targets, key=lambda n: (not n.startswith("test"), n)):
            out.append((fname, tool + " " + name, targets[name], "high", name))
    pyproject = repo.read("pyproject.toml") or ""
    if "[tool.pytest" in pyproject:
        out.append(("pyproject.toml", "pytest", "pytest", "medium", None))
    if "pytest.ini" in repo.fileset:
        out.append(("pytest.ini", "pytest", "pytest", "medium", None))
    if "[tool:pytest]" in (repo.read("setup.cfg") or ""):
        out.append(("setup.cfg", "pytest", "pytest", "medium", None))
    tox = repo.read("tox.ini")
    if tox:
        out.append(("tox.ini", "tox", tox, "medium", None))
    ci = [f for f in repo.files if re.match(r"\.github/workflows/[^/]+\.ya?ml$", f)] + \
         [f for f in CI_FILES if f in repo.fileset]
    for f in ci:
        for line in (repo.read(f) or "").splitlines():
            cmd = re.sub(r"^\s*(?:-\s*)?(?:run:\s*)?", "", line).strip().strip("'\"")
            if cmd and not cmd.startswith(("#", "uses:", "name:", "|", ">")) and not INSTALL.search(cmd):
                out.append((f, cmd, cmd, "medium", None))
    return out


def commands_for(repo, frameworks):
    sources = command_sources(repo)
    gradle = any(f in repo.fileset for f in ("build.gradle", "build.gradle.kts"))
    result = []
    for fw in frameworks:
        name = fw["name"]
        lang, keyword, default, file_t, single_t = FRAMEWORKS[name]
        if name == "junit":
            if gradle:
                tool = "./gradlew" if "gradlew" in repo.fileset else "gradle"
                default = tool + " test"
                file_t = tool + " test --tests '{class}'"
                single_t = tool + " test --tests '{class}.{name}'"
            elif "mvnw" in repo.fileset:
                default, file_t, single_t = (t.replace("mvn ", "./mvnw ", 1) for t in (default, file_t, single_t))
        matches = []
        for source, run, text, confidence, script in sources:
            # `npm test` runs whatever JS framework the repo has, even through a wrapper.
            if re.search(keyword, text) or (source == "package.json" and script == "test"
                                            and lang == "javascript"):
                if not any(m["run"] == run for m in matches):
                    matches.append({"run": run, "source": source, "confidence": confidence})
        if not matches:
            matches.append({"run": default, "source": "framework default", "confidence": "low"})
        result.append(dict(matches[0], framework=name, file=file_t, single=single_t,
                           alternatives=matches[1:MAX_EVIDENCE + 1]))
    return result


# ---------------------------------------------------------------- conventions

def conventions(repo):
    out = {}
    for lang in sorted({language(t) for t in repo.tests}):
        tests = [t for t in repo.tests if language(t) == lang]
        if lang not in CONVENTIONS:
            out[lang] = "unknown"
            continue
        sample = tests[:SAMPLE]
        dirs = {}
        for t in tests:
            d = posixpath.dirname(t) or "."
            dirs[d] = dirs.get(d, 0) + 1
        info = {"test_files": len(tests), "sampled": len(sample),
                "test_dirs": dict(sorted(dirs.items(), key=lambda kv: (-kv[1], kv[0]))[:5]),
                "file_patterns": {}}
        for label, rx in FILE_PATTERNS[lang]:
            n = sum(1 for t in tests if re.search(rx, t))
            if n:
                info["file_patterns"][label] = n
        for category, label, rx in CONVENTIONS[lang]:
            n = sum(1 for t in sample if re.search(rx, repo.read(t) or "", re.M))
            if n:
                info.setdefault(category, {})[label] = n
        if lang == "python":
            info["conftest"] = repo.named("conftest.py")[:10]
        out[lang] = info
    return out


# ---------------------------------------------------------------- targets

def public_symbols(lang, text):
    names = []
    if lang == "python":
        try:
            tree = ast.parse(text)
            names = [n.name for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        except (SyntaxError, ValueError):
            names = re.findall(r"^(?:async\s+)?(?:def|class)\s+(\w+)", text, re.M)
        names = [n for n in names if not n.startswith("_")]
    elif lang == "javascript":
        names = re.findall(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var"
                           r"|interface|type|enum|abstract\s+class)\s+([A-Za-z_$][\w$]*)", text, re.M)
        for group in re.findall(r"^\s*export\s*\{([^}]*)\}", text, re.M):
            for item in group.split(","):
                item = item.strip().split(" as ")[-1].strip()
                if IDENT.match(item):
                    names.append(item)
    elif lang == "go":
        names = re.findall(r"^func\s+(?:\([^)]*\)\s*)?([A-Z]\w*)", text, re.M)
        names += re.findall(r"^type\s+([A-Z]\w*)", text, re.M)
    elif lang == "rust":
        names = re.findall(r"^\s*pub\s+(?:async\s+)?(?:fn|struct|enum|trait|type|const)\s+(\w+)", text, re.M)
    elif lang in ("java", "csharp"):
        names = re.findall(r"\bpublic\s+(?:(?:static|final|abstract|sealed|partial)\s+)*"
                           r"(?:class|interface|enum|record)\s+(\w+)", text)
        names += re.findall(r"\bpublic\s+(?:(?:static|final|abstract|virtual|override|async|synchronized)\s+)*"
                            r"[\w<>\[\],.?]+\s+(\w+)\s*\(", text)
    elif lang == "ruby":
        names = re.findall(r"^\s*(?:class|module)\s+([A-Z]\w*)", text, re.M)
        names += re.findall(r"^\s*def\s+(?:self\.)?([a-z_]\w*[?!]?)", text, re.M)
    elif lang == "php":
        names = re.findall(r"^\s*(?:final\s+|abstract\s+)?class\s+(\w+)", text, re.M)
        names += re.findall(r"\bpublic\s+(?:static\s+)?function\s+(\w+)", text)
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out[:MAX_SYMBOLS]


def python_module(path):
    parts = path[:-3].split("/")
    if parts and parts[0] in ("src", "lib"):
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imports(path, text):
    """What a file imports: dotted Python modules, or JS specifiers (relative ones resolved)."""
    found = set()
    if language(path) == "python":
        for m in re.finditer(r"^\s*from\s+([\w.]+)\s+import\s+\(?([\w\s,*]+)", text, re.M):
            found.add(m.group(1))
            for name in m.group(2).replace("\n", ",").split(","):
                name = name.strip().split(" ")[0]
                if name and name != "*":
                    found.add(m.group(1) + "." + name)
        for m in re.finditer(r"^\s*import\s+([\w., ]+)", text, re.M):
            for name in m.group(1).split(","):
                found.add(name.strip().split(" ")[0])
    elif language(path) == "javascript":
        for spec in re.findall(r"""(?:\bfrom\s+|\brequire\(\s*|\bimport\(\s*|^\s*import\s+)['"]([^'"]+)['"]""",
                               text, re.M):
            if spec.startswith("."):
                spec = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
                spec = re.sub(r"\.(?:[cm]?[jt]sx?)$", "", spec)
            found.add(spec)
    return {f for f in found if f}


def imports_target(test_imports, target):
    lang = language(target)
    if lang == "python":
        mod = python_module(target)
        return any(m == mod or mod.endswith("." + m) for m in test_imports)
    if lang == "javascript":
        bare = re.sub(r"\.(?:[cm]?[jt]sx?)$", "", target)
        return bare in test_imports or (bare.endswith("/index") and bare[:-6] in test_imports)
    return False


NEUTRAL_DIRS = {"src", "lib", "app", "tests", "test", "__tests__", "spec", "specs", "main", "java", "kotlin"}


def mirror_dir(path):
    return [p for p in path.split("/")[:-1] if p not in NEUTRAL_DIRS]


def score_tests(repo, target, test_imports):
    """[(score, reason, path, strong)] for test files in the target's language, best first."""
    lang, stem = language(target), posixpath.basename(target).split(".")[0]

    def tops(names):
        return {n.split(".")[0] for n in names} if lang == "python" else set(names)

    own = tops(imports(target, repo.read(target) or ""))
    scored = []
    for t in repo.tests:
        if language(t) != lang:
            continue
        parts = []
        if subject(t).lower() == stem.lower():
            parts.append((100, "same stem"))
        if imports_target(test_imports.get(t, set()), target):
            parts.append((80, "imports the target"))
        elif re.search(r"(?<![\w.-])" + re.escape(posixpath.basename(target)) + r"\b", repo.read(t) or ""):
            parts.append((60, "names the file"))  # tests that run a script as a subprocess
        if mirror_dir(t) == mirror_dir(target):
            parts.append((40, "mirrored path"))
        if posixpath.dirname(t) == posixpath.dirname(target):
            parts.append((30, "same directory"))
        shared = len(own & tops(test_imports.get(t, set())))
        if shared:
            parts.append((min(5 * shared, 25), "shared imports"))
        if parts:
            strong = max(parts)[0] >= 60  # stem, import, or file name: it tests this target
            scored.append((sum(p[0] for p in parts), max(parts)[1], t, strong))
    scored.sort(key=lambda s: (-s[0], s[2]))
    return scored


DEFINITION = (r"(?:\b(?:def|class|function|func|fn|interface|struct|trait|enum|type|module)\s+"
              r"(?:\([^)]*\)\s*)?{name}\b|\b(?:const|let|var)\s+{name}\s*="
              r"|\b(?:public|private|protected|static)\b[\w \t<>\[\],.?]*\s{name}\s*\()")


def find_symbol(repo, name):
    rx = re.compile(DEFINITION.format(name=re.escape(name)), re.M)
    hits = []
    for f in repo.sources[:MAX_SCAN]:
        text = repo.read(f) or ""
        m = rx.search(text)
        if m:
            hits.append((f, text.count("\n", 0, m.start()) + 1))
    return hits


def describe_target(repo, path, test_imports, symbol=None, line=None):
    lang = language(path)
    symbols = [symbol] if symbol else public_symbols(lang, repo.read(path) or "")
    scored = score_tests(repo, path, test_imports)
    existing = [t for _, _, t, strong in scored
                if strong or (lang == "go" and posixpath.dirname(t) == posixpath.dirname(path))][:10]
    tested_text = "\n".join(repo.read(t) or "" for t in existing)
    tested = [s for s in symbols if re.search(r"(?<![\w$])" + re.escape(s) + r"(?![\w$])", tested_text)]
    exemplars = [{"path": t, "reason": reason} for _, reason, t, _ in scored[:EXEMPLARS]]
    if not exemplars:
        exemplars = [{"path": t, "reason": "same language"}
                     for t in repo.tests if language(t) == lang][:EXEMPLARS]
    out = {"path": path, "kind": "symbol" if symbol else "file", "language": lang,
           "public_symbols": symbols, "tested_symbols": tested,
           "untested_symbols": [s for s in symbols if s not in tested],
           "existing_tests": existing, "exemplars": exemplars}
    if symbol:
        out.update(symbol=symbol, line=line)
    return out


def resolve_targets(repo, args, cwd, warnings):
    """[(path, symbol, line)]; raises ValueError (bad argument) or LookupError (not found)."""
    found = []
    for arg in args:
        full = os.path.realpath(os.path.join(cwd, arg))
        if os.path.exists(full):
            rel = os.path.relpath(full, repo.real).replace(os.sep, "/")
            if rel == ".." or rel.startswith("../"):
                raise ValueError("%s is outside the repository" % arg)
            if os.path.isdir(full):
                prefix = "" if rel == "." else rel + "/"
                inside = [f for f in repo.sources if f.startswith(prefix)]
                if not inside:
                    warnings.append("no source files under %s" % arg)
                found += [(f, None, None) for f in inside]
            elif is_secret(rel):
                raise ValueError("%s looks like a secret file; it is never read" % arg)
            else:
                found.append((rel, None, None))
        elif IDENT.match(arg):
            name = re.split(r"\.|::", arg)[-1]
            hits = find_symbol(repo, name)
            if not hits:
                raise LookupError("symbol '%s' is not defined in any source file" % arg)
            found += [(f, name, line) for f, line in hits]
        else:
            raise LookupError("%s does not exist" % arg)
    return found


def diff_targets(repo, base, warnings):
    if not git(repo.root, "rev-parse", "--verify", "--quiet", base + "^{commit}"):
        raise LookupError("base '%s' not found" % base)
    mb = (git(repo.root, "merge-base", base, "HEAD") or "").strip()
    if not mb:
        raise LookupError("no common ancestor with '%s'" % base)
    changed = (git(repo.root, "-c", "core.quotepath=off", "diff", "--name-only", "--diff-filter=d",
                   "-z", mb) or "").split("\0")
    untracked = (git(repo.root, "-c", "core.quotepath=off", "ls-files", "-o", "--exclude-standard",
                     "-z") or "").split("\0")
    paths = sorted({p for p in changed + untracked if p in repo.fileset and language(p) and not is_test(p)})
    if not paths:
        warnings.append("no changed source files since %s" % base)
    return [(p, None, None) for p in paths]


# ---------------------------------------------------------------- coverage and services

def coverage(repo, frameworks):
    names = {f["name"] for f in frameworks}
    tools = []
    py = python_deps_text(repo)
    if re.search(r"\bpytest-cov\b", py):
        tools.append("pytest-cov")
    if re.search(r"(?<![\w-])coverage\b", py) or ".coveragerc" in repo.fileset:
        tools.append("coverage.py")
    deps = package_deps(repo.json("package.json"))
    tools += sorted(d for d in deps if d in ("c8", "nyc") or d.startswith("@vitest/coverage-"))
    if "jest" in names:
        tools.append("jest --coverage (built in)")
    if "go test" in names:
        tools.append("go test -cover (built in)")
    if any("jacoco" in (repo.read(f) or "") for f in ("pom.xml", "build.gradle", "build.gradle.kts")):
        tools.append("jacoco")
    if any("coverlet" in (repo.read(f) or "") for f in repo.named("*.csproj")):
        tools.append("coverlet")
    reports = [r for r in COVERAGE_REPORTS if os.path.isfile(os.path.join(repo.root, r))]
    return {"tools": tools, "reports": reports}


def compose_services(text):
    names, indent, inside = [], None, False
    for line in text.splitlines():
        if re.match(r"^services:\s*(?:#.*)?$", line):
            inside = True
            continue
        if not inside or not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            inside = False
            continue
        m = re.match(r"^(\s+)([\w.-]+):\s*(?:#.*)?$", line)
        if m and (indent is None or len(m.group(1)) == indent):
            indent = len(m.group(1))
            names.append(m.group(2))
    return names


def services(repo, files):
    found = {}
    for f in files[:MAX_SCAN]:
        text = repo.read(f) or ""
        for kind, rx in SERVICES:
            m = re.search(rx, text, re.M)
            if m:
                lineno = text.count("\n", 0, m.start()) + 1
                line = text.splitlines()[lineno - 1].strip()[:120]
                ev = found.setdefault(kind, [])
                if len(ev) < MAX_EVIDENCE:
                    ev.append("%s:%d: %s" % (f, lineno, line))
    for f in repo.named("docker-compose*.yml", "docker-compose*.yaml", "compose.yml", "compose.yaml"):
        names = compose_services(repo.read(f) or "")
        if names:
            found.setdefault("docker-compose", []).append("%s: %s" % (f, ", ".join(names)))
    return [{"kind": k, "evidence": found[k]} for k in sorted(found)]


# ---------------------------------------------------------------- snapshot

def sweep_snapshots(now=None):
    """Remove this user's day-old snapshots. Only test-writer-* directories that
    hold the marker file are touched. Errors are ignored."""
    tmp, now = tempfile.gettempdir(), time.time() if now is None else now
    try:
        names = [n for n in os.listdir(tmp) if n.startswith(SNAPSHOT_PREFIX)]
    except OSError:
        return
    for name in names:
        d = os.path.join(tmp, name)
        try:
            st = os.lstat(d)
            if not stat.S_ISDIR(st.st_mode) or now - st.st_mtime < STALE_SECONDS:
                continue
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                continue
            if not os.path.isfile(os.path.join(d, SNAPSHOT_MARKER)):
                continue
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def snapshot(root):
    sweep_snapshots()
    out = git(root, "-c", "core.quotepath=off", "ls-files", "-co", "--exclude-standard", "-z")
    if out is None:
        return fail("internal-error", "git ls-files failed")
    files = sorted({f for f in out.split("\0") if f})
    total = 0
    for rel in files:
        try:
            total += os.lstat(os.path.join(root, rel)).st_size
        except OSError:
            pass
    if total > SNAPSHOT_MAX_BYTES:
        return fail("snapshot-too-large", "The project is %d MB; snapshots stop at %d MB."
                    % (total // 2**20, SNAPSHOT_MAX_BYTES // 2**20))
    dest = tempfile.mkdtemp(prefix=SNAPSHOT_PREFIX)
    with open(os.path.join(dest, SNAPSHOT_MARKER), "w", encoding="utf-8") as f:
        f.write("Created by test-writer for a mutation check. Safe to delete.\n")
    copied, warnings = 0, []
    for rel in files:
        src, dst = os.path.join(root, rel), os.path.join(dest, rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.islink(src):
                try:
                    os.symlink(os.readlink(src), dst)
                except OSError:  # no symlink permission (Windows)
                    shutil.copy2(src, dst)
            elif os.path.isdir(src):  # submodule
                shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(".git"))
            elif os.path.isfile(src):
                shutil.copy2(src, dst)
            else:
                continue
            copied += 1
        except OSError as e:
            if len(warnings) < MAX_EVIDENCE:
                warnings.append("could not copy %s: %s" % (rel, e.strerror or e))
    return {"ok": True, "error": None, "warnings": warnings, "snapshot": dest, "files": copied}


# ---------------------------------------------------------------- main

def main(argv):
    try:
        args = parse_args(argv)
    except ValueError as e:
        return fail("bad-arguments", str(e))
    cwd = os.getcwd()
    root = (git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    if not root:
        return fail("not-a-repo", "Not inside a git repository.")
    if args.snapshot:
        return snapshot(root)
    repo = Repo(root)
    warnings = []
    if repo.truncated:
        warnings.append("repository has more than %d files; only the first %d were considered"
                        % (MAX_LIST, MAX_LIST))
    try:
        if args.diff is not None:
            raw = diff_targets(repo, args.diff, warnings)
        else:
            raw = resolve_targets(repo, args.targets, cwd, warnings)
    except ValueError as e:
        return fail("bad-arguments", str(e), repo_root=root)
    except LookupError as e:
        code = "base-not-found" if args.diff is not None else "target-not-found"
        return fail(code, str(e), repo_root=root)
    if len(raw) > MAX_TARGETS:
        warnings.append("%d targets; only the first %d are described" % (len(raw), MAX_TARGETS))
        raw = raw[:MAX_TARGETS]

    frameworks = detect_frameworks(repo)
    if not frameworks:
        warnings.append("no test framework detected")
    test_imports = {t: imports(t, repo.read(t) or "") for t in repo.tests[:MAX_SCAN]} if raw else {}
    targets = [describe_target(repo, p, test_imports, s, line) for p, s, line in raw]
    scanned = repo.tests + sorted({t["path"] for t in targets}) + repo.named("conftest.py")
    return {"ok": True, "error": None, "warnings": warnings, "repo_root": root,
            "frameworks": frameworks, "commands": commands_for(repo, frameworks),
            "conventions": conventions(repo), "targets": targets,
            "coverage": coverage(repo, frameworks), "services": services(repo, scanned),
            "truncated": repo.truncated}


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
    except Exception as e:  # always emit one JSON object
        result = {"ok": False, "error": {"code": "internal-error", "message": str(e)},
                  "warnings": []}
    print(json.dumps(result))
    sys.exit(0)
