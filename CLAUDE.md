# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python -m pip install pytest                      # only test dependency
python -m pytest                                  # all tests
python -m pytest tests/test_git_guard.py::test_no_verify_asks   # one test
python -m pytest tests/test_secret_scan.py                       # one plugin's tests
python -m pytest tests/test_pr_writer.py                         # pr-writer tests
python -m pytest tests/test_test_before_commit.py                # test-before-commit tests
python -m pytest tests/test_humanizer.py                         # humanizer tests
python -m pytest tests/test_test_writer.py                       # test-writer tests
python -m pytest tests/test_session_recap.py                     # session-recap tests
python -m pytest -k push                          # tests matching a name
claude plugin validate .                          # marketplace.json + every relative-path plugin
claude plugin validate ./<plugin>                 # one plugin's manifest
```

There is no build or lint step. CI (`.github/workflows/ci.yml`) runs pytest on pull requests on ubuntu across Python 3.9 to 3.15 and on macos/windows with 3.9 and 3.15. A push to `main` runs only ubuntu 3.9 and 3.15 plus windows 3.15. Both parse every `*.json` file with `python3 -m json.tool`.

To try changes in a real session: `claude plugin marketplace add ./` then `claude plugin install git-workflow-guard@dev-guardrails`. A marketplace added from a local path loads plugins in place, so `/reload-plugins` picks up edits. For a one-off check without installing: `claude -p --plugin-dir ./git-workflow-guard "..."`.

## Architecture

The repo is a Claude Code plugin **marketplace** named `dev-guardrails`. `.claude-plugin/marketplace.json` lists plugins, and each plugin is a sibling folder at the repo root (`"source": "./<plugin>"`). The entry `name` must equal the `name` in that plugin's `.claude-plugin/plugin.json`. Components (`hooks/`, `skills/`) sit at the plugin root, not inside `.claude-plugin/`. Bundled files are referenced through `${CLAUDE_PLUGIN_ROOT}`.

### git-workflow-guard

There are two halves, and keep them separate: **hooks enforce, the skill handles judgment.**

- `hooks/hooks.json` registers one script on two `PreToolUse` matchers: `Edit|Write|MultiEdit|NotebookEdit` and `Bash|PowerShell`. The command is shell form, `"$(command -v python3 || command -v python)" "${CLAUDE_PLUGIN_ROOT}/hooks/git_guard.py"`, so it falls back to `python` on Windows (Git Bash). Exec form can't do that fallback.
- `hooks/git_guard.py` is a single stdlib-only file and must stay Python 3.9 compatible. The flow:
  1. `evaluate()` dispatches on `tool_name`.
  2. For shell tools, `git_invocations()` tokenizes the command with `shlex` (punctuation-aware, newlines kept as separators). It splits on `&& || ; | &`, `(`, `)`, and backticks (so subshells and `$(...)` are seen), `unwrap()` strips keywords, `VAR=`, and wrappers like `sudo -u x` and `env`, and `nested_script()` recurses into `bash -c` and `eval` (options before the script name only) up to `MAX_DEPTH`. `split_segments` also emits each `(`/`)` as a one-token segment, and `git_invocations` restores `cwd` at `)`, so a `cd` inside a subshell doesn't leak. `takes_separate_value()` handles wrapper clusters (`sudo -iu root`, but not `-uroot`). It follows `cd <dir>` and `git -C <dir>`, and yields `(cwd, subcommand, args, -c values)` for each git call. Quoted text stays one token, so `"$(git ...)"` inside double quotes isn't seen.
  3. `check_edit` / `check_commit` / `check_push` / `check_branch_name` / `check_discard` each return `None`, `("block", msg)`, or `("ask", msg)`. `check_discard` asks on `reset --hard`, `clean -f`, `branch -D`, `checkout -f` / `switch -f|--discard-changes`, `stash drop|clear`, `checkout -- <path>`, and `restore`. Editing `.claude/git-guard.json`, or a shell command mentioning it, asks.
  4. `main()` aggregates the results. Any block exits 2 with the messages on stderr, which Claude reads as the denial reason. Otherwise any ask prints `hookSpecificOutput.permissionDecision: "ask"` JSON on stdout and exits 0. Otherwise it exits 0 silently.
- **Fail-open is a design requirement.** The `__main__` wrapper turns any exception into exit 0. Unparseable input (bad JSON, unbalanced quotes, `-m "$VAR"`, `-F file`) means allow, not block. Every `git` call goes through `git()`, which returns `None` on failure.
- Protected-branch rules live in `load_rules()`: the module constants `PROTECTED` and `PROTECTED_PREFIXES`, an optional per-repo `.claude/git-guard.json` (each key present replaces its default list, and an invalid file falls back to the defaults entirely), plus the `origin/HEAD` target, which is always added.
- Commit subjects come from the first `-m`/`--message`/`-F -`/short-option cluster (`-am`). `commit_cluster()` splits a cluster at the first letter that takes a value (`COMMIT_VALUE_LETTERS`, `COMMIT_ATTACHED_LETTERS`), so `-nm` is `-n` plus `-m` and `-unormal` has no `-m`. `skips_hooks()` uses it for `-n`, and also catches `-c core.hooksPath=...`. Heredocs are extracted with the `HEREDOC` regex, from inside the `-m` value or from the raw command for `-F -`.
- Push targets include explicit refspec destinations (`src:dst`, `+` = force, `HEAD` → current branch, `refs/heads/` stripped). With no refspec, the targets are the current branch and its upstream's branch name.
- `skills/git-workflow-guard/SKILL.md` is model-facing guidance: confirm the branch name, then the commit message, then the pre-push checklist and PR body. Hook block messages also instruct Claude. If you change a rule, update the hook messages, the SKILL.md, and the README table together.

### secret-scan-guard

- `hooks/secret_scan.py` follows the same contract and fail-open wrapper as `git_guard.py`. The shell parsing block (`SEPARATOR_CHARS`, `KEYWORDS`, `WRAPPERS`, `SHELLS`, `MAX_DEPTH`, `split_segments`, `unwrap`, `nested_script`, `git_invocations`) is **copied** from `git_guard.py`, because plugins install separately and can't import each other. When you fix parsing in one, fix the other. `tests/test_copies_in_sync.py` fails if they drift.
- `hooks/patterns.py` holds the rule table: `Rule(id, regex, confidence, show, value)`. `high` blocks, `medium` asks. `show` caps how many leading characters a preview reveals (the provider prefix, 4 at most). `value(match, lines, i)` extracts the secret or drops the match (the entropy and AWS-context checks). Placeholder, integrity-hash, and skip-file lists live there too.
- `scan()` works on `(line number, text, added)` tuples, so edits and diffs share one path. Only added lines produce findings, and context lines feed the AWS-context check. Diffs are read with `-U3`.
- Edits are scanned only when the file would be committed. Files outside a repo, and files that `git check-ignore` reports as ignored, are skipped. Only the new text is scanned (`content`, `new_string`, each `edits[].new_string`, `new_source`).
- `git commit` scans `git diff --cached`, plus `git diff` for `-a`, plus `git diff -- <paths>` for pathspecs. `git push` scans `git log -p <src refs> --not --remotes`. Every shell command is also scanned for embedded secrets, but that only asks.
- `secret-scan:allow` on a line suppresses it, unless an edit is adding the marker (the line isn't already in the file). Then it asks, so the agent can't allow its own secret.
- Trust boundary in `resolve()`: `~/.claude/secret-scan.json` can do anything. The repo `.claude/secret-scan.json` can only add `extraPatterns` and name a `baseline`, and its allow lists are ignored with a warning. Edits to either config, or to the baseline, ask. Keep it that way.
- The baseline holds SHA-256 of `path\0rule\0value`, generated by `secret_scan.py --baseline`. Findings and values are never written anywhere else, and previews are redacted, because stderr reaches the transcript.
- Config warnings go to `systemMessage` on exit 0, because stderr is never shown on exit 0. On a block they go to stderr.
- Fake secrets in `tests/test_secret_scan.py` are built at runtime from split prefixes and seeded random bodies. Keep it that way, or this guard and GitHub push protection will flag the test file. Push tests make real commits, with identity passed through `git -c`.

### destructive-command-guard

- `hooks/destructive_command_guard.py` handles parsing, config, and decisions. `hooks/rules.py` holds the rule table: `Rule(id, matcher, action, reason, safer)`, plus each matcher. The script imports `rules` from its own directory.
- `analyze()` takes these steps, recursing to `MAX_DEPTH`:
  1. Extracts heredoc bodies into placeholders.
  2. Recurses into `$(...)`, `<(...)`, and backticks, but not inside single quotes, and not backticks in PowerShell.
  3. Splits on `; && || |` and newlines with `shlex`.
  4. `unwrap()` strips keywords, `VAR=`, `sudo`, `env`, `xargs`, and similar wrappers. `split_io()` strips redirects, heredocs, and here-strings.
  5. Builds a `Cmd` and runs every matcher on it.
  6. Recurses into `bash -c`, `eval`, `pwsh -Command`, and shell stdin scripts.
  If tokenising fails, only the `FALLBACK` regexes run.
- `decide()` applies the configured action, then the allow regexes, then escalation: ask becomes deny under `sudo`, or for `db.*` rules when a connection string matches `prodPattern`. Connection strings come from `rules.db_parts()`, never from SQL text.
- Trust boundary in `resolve_settings()`: the user file (`~/.claude/destructive-guard.json`) can do anything. The repo file can only tighten rules, add rules, and add prod patterns. Every loosening attempt is ignored with a warning. Keep it that way.
- Warnings go to `systemMessage` on exit 0 and to stderr on a deny, like secret-scan-guard.
- Git commands are deliberately out of scope (git-workflow-guard owns them).
- Tests set a fake `HOME`/`USERPROFILE` per test, so the real user config never applies.

### protected-files-guard

- `hooks/protected_files_guard.py` follows the same contract and fail-open wrapper as `git_guard.py`. Rules are `(patterns, action, reason)` in `DEFAULT_RULES`, with `DEFAULT_ALLOW` for `.env.example` and friends. `deny` exits 2, `ask` prompts.
- `glob_re()` converts gitignore-style globs to regex: no `/` matches the name at any depth, a `/` anchors at the repo root, `**/` is zero or more directories. Matching is case-insensitive. Don't swap it for `fnmatch`, which gets `**` wrong.
- File tools: `targets_for()` checks the path as named (parent dir realpath'd) and its full `realpath`, each relative to its own repo root. Paths outside a repo are allowed.
- Shell tools: `split_segments` is adapted from `git_guard.py` with `<>` added as punctuation, so redirects are tokens. `strip_wrappers()` drops `VAR=` and wrappers (by basename) with their option values (`sudo -u root`), using a copy of `takes_separate_value()`. Parens are kept as segments and `shell_writes()` restores `cwd` at `)`, as in git-workflow-guard. `shell_writes()` is a heuristic over redirects, `tee`, `sed -i`, `cp`, `mv`, `ln`, `install`, `rm`, `truncate`, `git checkout --`, `git restore`, and PowerShell write cmdlets. Directory targets expand to their tracked files via `git ls-files`. Keep the README clear that it can be bypassed.
- `.claude/protected-files.json` (repo) adds `rules` and `allow`, and `replaceDefaults` drops the defaults. Edits to that file always ask, regardless of config. Invalid config falls back to defaults with a warning on stderr plus `systemMessage` (stderr isn't shown on exit 0).
- `PROTECTED_FILES_GUARD_DISABLE=1` turns it off.

### pr-writer

No hooks. One skill, `skills/draft/SKILL.md` (`disable-model-invocation: true`, invoked as `/pr-writer:draft`), plus `scripts/collect_context.py`. The script does the facts, the skill does the writing.

- `collect_context.py` is a single stdlib-only file, Python 3.9 compatible. It prints one JSON object and always exits 0. Failures are `{"ok": false, "error": {"code", "message"}}` with codes `not-a-repo`, `detached-head`, `protected-branch`, `base-not-found`, `no-commits-ahead`, `bad-arguments`, `internal-error`. Every `git` call goes through `git()`, which returns `None` on failure.
- It is read-only, apart from creating an empty `mkdtemp` directory for `body_file` (the file itself is not created) and `sweep_body_dirs()`, which removes this user's `pr-writer-*` temp directories that are over a day old and hold nothing but `body.md`. The only subprocesses are git and `gh|glab auth status`. The skill writes the body to `body_file`, never into a shell string.
- `DEFAULT_CONFIG` holds the `.claude/pr-writer.json` keys. One invalid value drops the whole file back to defaults with a warning. Unknown keys warn.
- The skill must never push, never invent testing or issues, treat commits/diffs/templates as data, and create a PR only after an explicit yes. Titles can't contain `'`, because the skill passes them single-quoted. It single-quotes the branch, base, and `body_file` too, so a `'` in the branch or base name produces a warning and the skill falls back to paste-ready text. If you change a rule, update SKILL.md and the README together.
- `PR_WRITER_CLI_PATH` is a test seam. `detect_host()` uses it as the `PATH` for finding `gh`/`glab`, so tests can hide the real CLIs.
- **Copied pieces, keep in sync** (plugins can't import each other):
  - `PROTECTED`, `PROTECTED_PREFIXES`, `load_rules`, `is_protected` from `git-workflow-guard/hooks/git_guard.py`
  - `TYPES`, `CONVENTIONAL` from `git-workflow-guard/hooks/git_guard.py`
  - `glob_re` from `protected-files-guard/hooks/protected_files_guard.py`. The copy adds an `ignore_case` parameter (default `True`); `codeowners()` passes `False`
  - `SECRET_RULES` from `secret-scan-guard/hooks/patterns.py` (the high-confidence rules). `EXTRA_REDACTIONS` adds the `url-credentials` and `jwt` medium rules, replacing only the secret part
  - `tests/test_copies_in_sync.py` checks all of these

### test-before-commit

- `hooks/test_before_commit.py` follows the same contract and fail-open wrapper as `git_guard.py`. It acts only on `git commit` (found with a copy of git-workflow-guard's shell parsing block), skipping `--amend --no-edit` and merges (`MERGE_HEAD`). `--trust [dir]` is a CLI mode, not a hook path.
- `load_settings()` merges `DEFAULTS`, the user file, then the repo file. `commands` wins over `autodetect`. An invalid file is ignored with a warning (stderr plus `systemMessage`), and the other file still applies.
- **Trust boundary:** user-config commands run as is. When a repo config exists and there are commands, `digest` (the repo file's text plus the resolved commands) must equal the value in `~/.claude/test-before-commit-trust.json` for that repo's realpath, or the hook asks and runs nothing. Only `--trust`, via the `trust` skill (`disable-model-invocation: true`), writes that file. Shell commands mentioning the trust script, trust file, or config name ask, and so do file-tool edits of the user config or trust file. Keep it that way.
- `run_command()` never puts `tool_input` into a command. It uses `shlex.split` and no shell unless the command sets `shell`, writes output to a temp file (not a pipe), runs each command in its own process group or session, and kills the whole tree on timeout. Total runtime is capped by `BUDGET` (1700s), under the 1800s `timeout` in `hooks.json`. Keep the two in step.
- Failure output is the last `TAIL_LINES` lines of the last `TAIL_BYTES`, ANSI-stripped, with each line capped at `LINE_CAP`.
- The cache (`git rev-parse --git-path test-before-commit-cache.json`) holds one passing key: `write-tree`, the unstaged `git diff --binary`, and the commands.
- **Copied pieces, keep in sync:** the shell parsing block from `git_guard.py`, and `glob_re` from `protected_files_guard.py`, both checked by `tests/test_copies_in_sync.py`.
- Tests set a fake `HOME`/`USERPROFILE`, stage files in an unborn repo, and run small Python scripts as the configured commands.

### humanizer

No guard. `skills/review/SKILL.md` (`disable-model-invocation: true`, invoked as `/humanizer:review`) loads `skills/humanizer/SKILL.md` by path, which holds the workflow and editing rules. `scripts/scan.py` finds the text; Claude rewrites it.

- `scan.py` is a single stdlib-only file, Python 3.9 compatible. It prints one JSON object and always exits 0. Failures are `{"ok": false, "error": {"code", "message"}}` with codes `not-a-repo`, `bad-arguments`, `base-not-found`, `internal-error`. It never writes files. Every `git` call goes through `git()`, which returns `None` on failure.
- Scope is `added_lines()` over a `-U0` diff (`--cached`, `merge-base`, or `HEAD`/the empty tree), plus untracked files. Explicit paths are scanned whole. Prose regions are cut down to their added lines. Comment and docstring regions stay whole, but line-level findings outside the added lines are dropped.
- Extraction: `python_regions()` uses `tokenize` and `ast`. Everything else goes through `lex_comments()`, a string-aware lexer driven by the `LANGS` table. `prose_regions()` handles Markdown, rst, and txt. Directive lines are dropped before consecutive comments are merged, so a shebang doesn't take its neighbours with it.
- Rules: `PHRASES` is the table to extend (`id`, severity, regex). The structural checks live in `Analyzer.analyze()`. Findings with a `style` tag can be downgraded to `info` by the voice sample (`SAMPLE_MIN_WORDS` words or more).
- The hook `hooks/humanizer_nudge.py` only ever asks, never exits 2. It imports `scan` lazily for `load_config`, so a broken scanner fails open. `HUMANIZER_SCAN_TIMEOUT` is a test seam for the scan's time limit.
- **Copied pieces, keep in sync:** the shell parsing block from `git_guard.py` (in the hook), and `glob_re` from `protected_files_guard.py` (in `scan.py`), both checked by `tests/test_copies_in_sync.py`.
- Fixtures in `tests/fixtures/humanizer/` are before/after pairs. `cases.json` lists the finding ids each `before` file must produce. Its `after` file must have no medium or high findings.
- The README must keep saying it is an editor, not a detector-evasion tool, and that it doesn't replace AI-disclosure requirements.

### test-writer

No hooks. `skills/write/SKILL.md` (`disable-model-invocation: true`, invoked as `/test-writer:write`) loads `skills/test-writer/SKILL.md` by path. That skill is also model-invocable, so "write tests for X" triggers it. `scripts/detect_tests.py` gathers the facts; Claude writes and runs the tests.

- `detect_tests.py` is a single stdlib-only file, Python 3.9 compatible. It prints one JSON object and always exits 0. Failures are `{"ok": false, "error": {"code", "message"}}` with codes `not-a-repo`, `bad-arguments`, `target-not-found`, `base-not-found`, `snapshot-too-large`, `internal-error`. Every `git` call goes through `git()`, which returns `None` on failure.
- Files come from `git ls-files -co --exclude-standard`, minus `SKIP_PARTS` directories and minified files. `Repo.read()` refuses `SECRET_NAMES` (checked on the name and on the `realpath`, so a symlink can't reach `.env`), files over `MAX_READ`, binary files, and anything resolving outside the repo. A `test_*.py` with no `def test` or `TestCase` counts as source.
- Tables to extend: `FRAMEWORKS` (language, the keyword that ties a command to the framework, default, file and single templates), `CONVENTIONS` and `FILE_PATTERNS` (Python and JavaScript only; other languages report `"unknown"`), `SERVICES`, `COVERAGE_REPORTS`.
- `command_sources()` returns candidates in trust order: `package.json` scripts, `Makefile`/`justfile` targets, pytest config, `tox.ini`, CI files. `INSTALL` drops install lines. A framework gets the first candidate whose text matches its keyword (`npm test` matches any JS framework), else its default with `confidence: "low"`.
- `score_tests()` ranks exemplars: same stem 100, imports the target 80, names the file 60, mirrored path 40, same directory 30, shared imports 5 each (max 25). A test with a 60+ part is an existing test for the target.
- **`--snapshot` is the one write.** It copies `ls-files -co` output, without `.git`, into `mkdtemp(prefix="test-writer-")` with a `.test-writer-snapshot` marker, so the mutation check can edit a copy that git-workflow-guard doesn't treat as a repo. `sweep_snapshots()` removes this user's day-old `test-writer-*` directories, and only those holding the marker. The skill never deletes snapshots itself.
- The skill must only add or extend test files, never change the code under test, never weaken existing tests, report suspected bugs instead of fixing them, and never commit or push. Test commands aren't in `allowed-tools`, so each one asks. If you change a rule, update SKILL.md and the README together.
- Tests set `TMPDIR`/`TEMP`/`TMP` per test for snapshots, and write fixtures as untracked files in unborn repos.

### session-recap

No guard. `hooks/log_activity.py` logs, `scripts/collect_recap.py` gathers facts, and `skills/summary/SKILL.md` (`disable-model-invocation: true`, invoked as `/session-recap:summary`) loads `skills/session-recap/SKILL.md` by path. That skill is also model-invocable, so "recap this session" triggers it.

- `hooks/hooks.json` registers the one script on `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionDenied` (edit and shell tools), and `Stop`. It follows the same fail-open contract as `git_guard.py`: any error exits 0, it never prints a decision and never exits 2. `SESSION_RECAP_DISABLE=1` exits before reading stdin.
- The log is `~/.claude/session-recap/<sha256 of realpath, 16 hex>/<session id>.jsonl`. `record()` reads only the fields it names, so contents and output can't reach the log. Commands are redacted first (`SECRET_RULES`, `EXTRA_REDACTIONS`) and capped at 500 characters. Docs facts that shape it: success has no exit-code field (logged as 0), failures come from `PostToolUseFailure`, other plugins' blocks are inferred from a `pre` with no `post`/`fail` by `tool_use_id`, and `PermissionDenied` fires only in auto mode.
- `prune()` runs on every event. This repo's directory uses its `retentionDays`; other repos' directories always use 14 days, so a checked-in config can't shorten them. Keep it that way.
- The Stop nudge (`autoRecap`) fires once per session (a `recap_suggested` row marks it) at 3 or more distinct edited files, and skips when `stop_hook_active`.
- `log_activity.py` imports `load_config` lazily from `collect_recap.py`, so a broken collector fails the hook open.
- `collect_recap.py` is a single stdlib-only file, Python 3.9 compatible. It prints one JSON object and always exits 0. Failures are `{"ok": false, "error": {"code", "message"}}` with codes `not-a-repo`, `bad-arguments`, `base-not-found`, `since-not-found`, `internal-error`. It never writes files, and every `git` call goes through `git()`. It never reads secret files (path only). `--session` picks the log, else the newest. `previous_recap` parses `## Next time` bullets from the newest `.claude/recaps/*.md`, so the skill's saved headings must stay exact.
- Skill rules: read the diff of the top 3 impact files, label impact observed or inferred, quote `tests.status` and `stale` exactly, tie each "Next time" item to a field, write a saved recap or `CLAUDE.md` line only after an explicit yes, never commit or push. If you change a rule, update both SKILL.md files and the README together.
- **Copied pieces, keep in sync:** the shell parsing block and the redaction tables in `log_activity.py` (from `git_guard.py` and `secret-scan-guard/hooks/patterns.py`), and `log_dir`/`safe_session` in `collect_recap.py` (from `log_activity.py`), all checked by `tests/test_copies_in_sync.py`.

### Tests

`tests/test_git_guard.py` runs the script as a subprocess, piping JSON payloads into `sys.executable`, against throwaway repos. The `repo` fixture creates an **unborn** repo and switches branches with `git symbolic-ref HEAD refs/heads/<name>`, so no commits or git identity are needed. Keep tests that way so they stay fast and work on Windows CI.

## Repo conventions

This repo follows the workflow its own plugin enforces:
- Work on `<type>/<short-kebab>` branches, never on `main`.
- Write Conventional Commit subjects of 72 characters or fewer.
- On release, bump `version` in the plugin's `plugin.json`, because installed users don't receive changes until it changes. Move the `CHANGELOG.md` `[Unreleased]` entries (Keep a Changelog format) under the new version.
- When you add a plugin, also register it in `marketplace.json`, add tests, and add a README section.
