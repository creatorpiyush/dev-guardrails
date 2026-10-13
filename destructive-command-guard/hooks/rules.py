"""Rule table for destructive-command-guard.

Each rule has a stable id, a matcher, a default action, a one-line reason,
and a safer alternative. Matchers receive one simple command (`Cmd`), already
unwrapped from sudo/env/xargs/bash -c and friends, and return a short detail
string when the rule matches, or None.

Git commands are out of scope: git-workflow-guard owns them.
"""
import os
import re
from collections import namedtuple
from dataclasses import dataclass, field

DENY, ASK, ALLOW = "deny", "ask", "allow"
STRICTNESS = {ALLOW: 0, ASK: 1, DENY: 2}

DEFAULT_PROD_PATTERN = r"prod(uction)?"

# rm -rf on these, anywhere inside the project, is routine and allowed silently.
BUILD_DIRS = {"node_modules", "dist", "build", "target", "__pycache__", ".venv",
              ".pytest_cache", "coverage", ".next"}

FETCHERS = {"curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod"}
INTERPRETERS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "python", "python3", "perl",
                "ruby", "node", "iex", "invoke-expression", "pwsh", "powershell"}
# An argument that is a substitution running a fetcher: $(curl ...), <(curl ...), `curl ...`, (iwr ...)
FETCH_SUBSTITUTION = re.compile(r"^(\$\(|<\(|`|\()\s*(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b", re.I)
BLOCK_DEVICE = re.compile(r"^/dev/(sd|hd|vd|xvd|nvme|disk|rdisk|mmcblk|mapper/)", re.I)


@dataclass
class Cmd:
    """One simple command, after wrappers are stripped."""
    argv: list
    cwd: str
    display: str                                   # original text, for messages
    sudo: bool = False                             # ran under sudo/doas
    xargs: bool = False                            # arguments also come from stdin
    piped_from_fetcher: bool = False               # curl ... | <this>
    env: dict = field(default_factory=dict)        # leading VAR=value assignments
    stdin: list = field(default_factory=list)      # heredoc / here-string bodies
    redirects: list = field(default_factory=list)  # > target, >> target

    @property
    def name(self):
        base = os.path.basename(self.argv[0].replace("\\", "/")).lower()
        return base[:-4] if base.endswith(".exe") else base

    @property
    def args(self):
        return self.argv[1:]


@dataclass
class Context:
    project: str  # realpath of the git repo root, or of cwd outside a repo
    home: str     # realpath of the home directory


# ---------------------------------------------------------------- paths

def real(path):
    return os.path.normcase(os.path.realpath(path))


def is_within(path, parent):
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


HOME_VAR = re.compile(r"\$\{?(HOME|USERPROFILE)\}?")
LITERAL_CATASTROPHIC = {"/", "/*", "~", "~/", "~/*", ".", "./", "./*", "..", "../", "*"}


def classify_target(target, cmd, ctx):
    """Return 'catastrophic', 'unresolved', 'build', 'inside', or 'outside'."""
    if target is None:
        return "unresolved"  # comes from stdin (xargs)
    if target in LITERAL_CATASTROPHIC or HOME_VAR.sub("~", target) in LITERAL_CATASTROPHIC:
        return "catastrophic"
    target = HOME_VAR.sub(lambda _: os.path.expanduser("~"), target)
    if "$" in target or "`" in target or target.startswith("%"):
        return "unresolved"  # may expand to an empty string, and then to /
    target = os.path.expanduser(target)
    if re.search(r"[*?\[]", target):  # a glob deletes the contents of its directory
        target = re.split(r"[*?\[]", target, 1)[0] or "."
    p = real(os.path.join(cmd.cwd, target))
    if os.path.dirname(p) == p or is_within(ctx.home, p) or is_within(ctx.project, p):
        return "catastrophic"  # root, home, the project, or a parent of them
    if not is_within(p, ctx.project):
        return "outside"
    rel = os.path.relpath(p, ctx.project).replace("\\", "/").split("/")
    return "build" if any(part in BUILD_DIRS for part in rel) else "inside"


# ---------------------------------------------------------------- rm / chmod

RM_NAMES = {"rm", "remove-item", "ri", "del", "erase"}
RM_OPTS_WITH_VALUE = {"-path", "-literalpath", "-include", "-exclude", "-filter"}


def rm_flags_and_targets(cmd):
    recursive = force = False
    targets, after_dashdash, skip = [], False, False
    args = cmd.args
    for i, a in enumerate(args):
        low = a.lower()
        if skip:
            skip = False
            continue
        if after_dashdash or not a.startswith("-"):
            targets.append(a)
        elif a == "--":
            after_dashdash = True
        elif low in ("--recursive", "-recurse", "-r"):
            recursive = True
        elif low in ("--force", "-force", "-f"):
            force = True
        elif low in ("-path", "-literalpath"):
            if i + 1 < len(args):
                targets.append(args[i + 1])
            skip = True
        elif low in RM_OPTS_WITH_VALUE:
            skip = True
        elif re.fullmatch(r"-[rRfivdI]+", a):  # clusters such as -rf
            recursive = recursive or "r" in low
            force = force or "f" in low
    if cmd.xargs:
        targets.append(None)
    return recursive, force, targets


def rm_match(category, needs):
    """Matcher for rm whose target falls in `category`."""
    def match(cmd, ctx):
        if cmd.name not in RM_NAMES:
            return None
        recursive, force, targets = rm_flags_and_targets(cmd)
        if needs == "r-or-f":
            if not (recursive or force):
                return None
            if any(a == "--no-preserve-root" for a in cmd.args):
                return "--no-preserve-root"
        elif not recursive:
            return None
        for t in targets:
            if classify_target(t, cmd, ctx) == category:
                return t if t is not None else "(paths from stdin)"
        return None
    return match


def is_recursive_flag(a):
    return a == "--recursive" or bool(re.fullmatch(r"-[a-zA-Z]*R[a-zA-Z]*", a))


def chmod_targets(cmd):
    operands = [a for a in cmd.args if not a.startswith("-")]
    return operands[0] if operands else "", operands[1:]


def chmod_root(cmd, ctx):
    if cmd.name not in ("chmod", "chown") or not any(is_recursive_flag(a) for a in cmd.args):
        return None
    _, targets = chmod_targets(cmd)
    for t in targets:
        expanded = os.path.expanduser(HOME_VAR.sub("~", t))
        p = real(os.path.join(cmd.cwd, expanded))
        if os.path.dirname(p) == p or is_within(ctx.home, p):
            return t
    return None


def chmod_777(cmd, ctx):
    if cmd.name != "chmod" or not any(is_recursive_flag(a) for a in cmd.args):
        return None
    mode, targets = chmod_targets(cmd)
    if re.fullmatch(r"0?777|(a|ugo)\+rwx", mode):
        return " ".join(targets) or mode
    return None


# ---------------------------------------------------------------- other filesystem

def find_delete(cmd, ctx):
    if cmd.name != "find":
        return None
    args = cmd.args
    if "-delete" in args:
        return "-delete"
    for i, a in enumerate(args[:-1]):
        if a in ("-exec", "-execdir", "-ok", "-okdir") and os.path.basename(args[i + 1]) in ("rm", "shred"):
            return a + " " + args[i + 1]
    return None


def shred_truncate(cmd, ctx):
    if cmd.name == "shred":
        return " ".join(a for a in cmd.args if not a.startswith("-")) or "shred"
    if cmd.name == "truncate":
        targets, skip = [], False
        for a in cmd.args:
            if skip:
                skip = False
            elif a in ("-s", "--size", "-r", "--reference"):
                skip = True
            elif not a.startswith("-"):
                targets.append(a)
        risky = [t for t in targets if not t.endswith(".log")
                 and classify_target(t, cmd, ctx) != "build"]
        return " ".join(risky) or None
    return None


def disk_overwrite(cmd, ctx):
    if re.fullmatch(r"mkfs(\.\w+)?", cmd.name) or cmd.name == "format-volume":
        return cmd.name
    if cmd.name == "diskutil" and any(re.match(r"(?i)(erase|zero|random|secureErase)", a) for a in cmd.args):
        return "diskutil " + cmd.args[0]
    if cmd.name == "dd":
        for a in cmd.args:
            if a.startswith("of=") and BLOCK_DEVICE.match(a[3:]):
                return a
    for target in cmd.redirects:
        if BLOCK_DEVICE.match(target):
            return "> " + target
    return None


def fork_bomb(command):
    return ":(){ :|:& };:" if re.search(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", command) else None


# ---------------------------------------------------------------- remote code

def pipe_to_shell(cmd, ctx):
    if cmd.name in INTERPRETERS and cmd.piped_from_fetcher:
        return "... | " + cmd.name
    if cmd.name in INTERPRETERS | {"eval", "source", "."}:
        for a in cmd.args:  # bash <(curl ...), eval "$(curl ...)", bash -c "$(curl ...)", iex (iwr ...)
            if FETCH_SUBSTITUTION.match(a):
                return cmd.name + " " + a
    return None


# ---------------------------------------------------------------- databases

DB_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "mongo", "mongosh", "redis-cli"}
# client -> (options holding SQL, options holding a host or connection string,
#            options that take a value but hold neither)
DB_OPTIONS = {
    "psql": ({"-c", "--command"}, {"-h", "--host", "-d", "--dbname"}, {"-U", "--username", "-p", "--port", "-f", "--file", "-v", "--set", "-o", "--output"}),
    "mysql": ({"-e", "--execute"}, {"-h", "--host", "-D", "--database"}, {"-u", "--user", "-P", "--port"}),
    "mariadb": ({"-e", "--execute"}, {"-h", "--host", "-D", "--database"}, {"-u", "--user", "-P", "--port"}),
    "sqlite3": (set(), set(), {"-cmd", "-separator", "-nullvalue"}),
    "mongo": ({"--eval"}, {"--host", "--uri"}, {"-u", "--username", "-p", "--password", "--port", "--authenticationDatabase"}),
    "mongosh": ({"--eval"}, {"--host", "--uri"}, {"-u", "--username", "-p", "--password", "--port", "--authenticationDatabase"}),
    "redis-cli": (set(), {"-h", "-u"}, {"-p", "-a", "-n", "--user", "--pass", "-s"}),
}
CONN_ENV = re.compile(r"(URL|URI|HOST|DSN|DATABASE|DB)", re.I)


def db_parts(cmd):
    """Return (statement texts, connection texts) for a database client call."""
    if cmd.name not in DB_CLIENTS:
        return [], []
    sql_opts, conn_opts, value_opts = DB_OPTIONS[cmd.name]
    sql, conn, positionals = list(cmd.stdin), [], []
    args, i = cmd.args, 0
    while i < len(args):
        a = args[i]
        opt, eq, val = a.partition("=")
        short = a[:2] if len(a) > 2 and a[1] != "-" else None
        if eq and opt in sql_opts | conn_opts | value_opts:
            (sql if opt in sql_opts else conn if opt in conn_opts else []).append(val)
        elif a in sql_opts | conn_opts | value_opts:
            if i + 1 < len(args):
                (sql if a in sql_opts else conn if a in conn_opts else []).append(args[i + 1])
            i += 1
        elif short and short in sql_opts | conn_opts:  # -cSQL, -hHOST
            (sql if short in sql_opts else conn).append(a[2:])
        elif not a.startswith("-"):
            positionals.append(a)
        i += 1
    if cmd.name == "sqlite3":  # sqlite3 [opts] DB [SQL...]
        conn += positionals[:1]
        sql += positionals[1:]
    elif cmd.name == "redis-cli":  # redis-cli [opts] COMMAND args
        sql.append(" ".join(positionals))
    else:
        conn += positionals  # database name or connection URI
    conn += [v for k, v in cmd.env.items() if CONN_ENV.search(k)]
    return sql, conn


def statements(cmd):
    for text in db_parts(cmd)[0]:
        for stmt in text.split(";"):
            stmt = re.sub(r"--[^\n]*", "", stmt).strip()
            if stmt:
                yield stmt


def db_drop(cmd, ctx):
    for s in statements(cmd):
        if re.search(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b|\bTRUNCATE\b", s, re.I):
            return s[:80]
    return None


def db_unbounded_write(cmd, ctx):
    for s in statements(cmd):
        if re.match(r"(?i)(DELETE\s+FROM|UPDATE\s+\S+\s+SET)\b", s) and not re.search(r"(?i)\bWHERE\b", s):
            return s[:80]
    return None


def db_flush(cmd, ctx):
    for s in statements(cmd):
        if re.search(r"\bFLUSH(ALL|DB)\b|\bdropDatabase\s*\(", s, re.I):
            return s[:80]
    return None


# ---------------------------------------------------------------- infrastructure

def subcommands(cmd):
    return [a.lower() for a in cmd.args if not a.startswith("-")]


def terraform(cmd, ctx):
    if cmd.name not in ("terraform", "tofu", "terragrunt"):
        return None
    subs, flags = subcommands(cmd), [a.lower().lstrip("-") for a in cmd.args if a.startswith("-")]
    if "destroy" in subs:
        return "destroy"
    if "apply" in subs and ("auto-approve" in flags or "destroy" in flags):
        return "apply " + " ".join("-" + f for f in flags if f in ("auto-approve", "destroy"))
    return None


def kubectl_delete(cmd, ctx):
    if cmd.name not in ("kubectl", "oc") or "delete" not in subcommands(cmd):
        return None
    subs = subcommands(cmd)
    after = subs[subs.index("delete") + 1:]
    if after and re.match(r"(ns|namespaces?)(/|$)", after[0]):
        return "delete " + after[0]
    if any(a in ("--all", "--all-namespaces", "-A") for a in cmd.args):
        return "delete --all"
    return None


def docker_prune(cmd, ctx):
    if cmd.name not in ("docker", "podman"):
        return None
    subs = subcommands(cmd)
    if subs[:2] == ["system", "prune"] and any(re.fullmatch(r"-[a-z]*a[a-z]*|--all", a) for a in cmd.args):
        return "system prune -a"
    if subs[:2] == ["volume", "prune"]:
        return "volume prune"
    return None


def cloud_storage_delete(cmd, ctx):
    if cmd.name == "aws" and subcommands(cmd)[:2] == ["s3", "rm"] and "--recursive" in cmd.args:
        return "s3 rm --recursive"
    if cmd.name == "aws" and subcommands(cmd)[:2] == ["s3", "rb"] and "--force" in cmd.args:
        return "s3 rb --force"
    return None


def cloud_delete(cmd, ctx):
    if cmd.name in ("gcloud", "az") and "delete" in subcommands(cmd):
        return " ".join(subcommands(cmd)[:4])
    return None


# ---------------------------------------------------------------- the table

Rule = namedtuple("Rule", "id matcher action reason safer")

RULES = [
    Rule("fs.rm-catastrophic", rm_match("catastrophic", "r-or-f"), DENY,
         "deletes the filesystem root, your home directory, the whole project, or a parent of them",
         "delete explicit subpaths instead, e.g. `rm -ri ./path/to/dir`"),
    Rule("fs.rm-unresolved", rm_match("unresolved", "r"), ASK,
         "the target depends on a variable or stdin; if it's empty, this becomes `rm -rf /`",
         "check the variable first (`: \"${DIR:?}\"`) or spell out the path"),
    Rule("fs.rm-outside-repo", rm_match("outside", "r"), ASK,
         "recursively deletes a directory outside this repository",
         "`ls` the path first, then `rm -ri` it"),
    Rule("fs.rm-inside-repo", rm_match("inside", "r"), ASK,
         "recursively deletes project files that may not be recoverable from git",
         "check `git status` and delete specific paths with `rm -ri`, or use `git rm` for tracked files"),
    Rule("fs.find-delete", find_delete, ASK,
         "deletes every file `find` matches, which is easy to get wrong",
         "run the same `find` without `-delete` first and check the list"),
    Rule("fs.shred-truncate", shred_truncate, ASK,
         "irreversibly destroys file contents",
         "move the file aside (`mv file file.bak`) or rely on git history instead"),
    Rule("disk.overwrite", disk_overwrite, DENY,
         "overwrites or formats a disk device",
         "double-check the device with `lsblk` / `diskutil list` and run it yourself"),
    Rule("fs.chmod-root", chmod_root, DENY,
         "recursively changes permissions or ownership of / or your home directory",
         "target the specific directory that needs the change"),
    Rule("fs.chmod-777", chmod_777, ASK,
         "makes every file world-writable",
         "grant only what's needed, e.g. `chmod -R u+rwX,go+rX <dir>`"),
    Rule("proc.fork-bomb", None, DENY,
         "fork bomb: exhausts processes until the machine hangs",
         "don't run it"),
    Rule("remote.pipe-to-shell", pipe_to_shell, ASK,
         "runs a script straight from the internet without reading it",
         "download it (`curl -fsSLo install.sh <url>`), read it, then run it"),
    Rule("db.drop", db_drop, ASK,
         "drops or truncates database objects",
         "back up first (`pg_dump`/`mysqldump`) and check with a `SELECT count(*)`"),
    Rule("db.unbounded-write", db_unbounded_write, ASK,
         "DELETE or UPDATE without WHERE changes every row",
         "run a `SELECT` with the intended WHERE first, inside `BEGIN; ... ROLLBACK;`"),
    Rule("db.flush", db_flush, ASK,
         "deletes every key or the whole database",
         "delete specific keys or collections, after a backup"),
    Rule("infra.terraform", terraform, ASK,
         "destroys or changes infrastructure without a reviewed plan",
         "run `terraform plan` (or `plan -destroy`) and review it"),
    Rule("infra.kubectl-delete", kubectl_delete, ASK,
         "deletes a namespace or every resource of a kind",
         "check `kubectl config current-context`, then `kubectl delete --dry-run=server ...`"),
    Rule("infra.docker-prune", docker_prune, ASK,
         "removes all unused images or volumes, including data volumes",
         "check `docker system df` and remove specific images or volumes"),
    Rule("infra.cloud-storage-delete", cloud_storage_delete, ASK,
         "deletes every object under the prefix, or the bucket itself",
         "list with `aws s3 ls --recursive`, or add `--dryrun`"),
    Rule("infra.cloud-delete", cloud_delete, ASK,
         "deletes a cloud resource",
         "describe the resource first and confirm the project/subscription"),
]
RULES_BY_ID = {r.id: r for r in RULES}
