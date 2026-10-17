# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python -m pip install pytest                      # only test dependency
python -m pytest                                  # all tests
python -m pytest tests/test_git_guard.py::test_no_verify_asks   # one test
python -m pytest tests/test_secret_scan.py                       # one plugin's tests
python -m pytest tests/test_pr_writer.py                         # pr-writer tests
python -m pytest -k push                          # tests matching a name
claude plugin validate .                          # marketplace.json + every relative-path plugin
claude plugin validate ./<plugin>                 # one plugin's manifest
```

There is no build or lint step. CI (`.github/workflows/ci.yml`) runs pytest on ubuntu/macos/windows across Python 3.9 to 3.15, and parses every `*.json` file with `python3 -m json.tool`.

To try changes in a real session: `claude plugin marketplace add ./` then `claude plugin install git-workflow-guard@dev-guardrails`. A marketplace added from a local path loads plugins in place, so `/reload-plugins` picks up edits. For a one-off check without installing: `claude -p --plugin-dir ./git-workflow-guard "..."`.

## Architecture

The repo is a Claude Code plugin **marketplace** named `dev-guardrails`. `.claude-plugin/marketplace.json` lists plugins, and each plugin is a sibling folder at the repo root (`"source": "./<plugin>"`). The entry `name` must equal the `name` in that plugin's `.claude-plugin/plugin.json`. Components (`hooks/`, `skills/`) sit at the plugin root, not inside `.claude-plugin/`. Bundled files are referenced through `${CLAUDE_PLUGIN_ROOT}`.

### git-workflow-guard

There are two halves, and keep them separate: **hooks enforce, the skill handles judgment.**

- `hooks/hooks.json` registers one script on two `PreToolUse` matchers: `Edit|Write|MultiEdit|NotebookEdit` and `Bash|PowerShell`. The command is shell form, `"$(command -v python3 || command -v python)" "${CLAUDE_PLUGIN_ROOT}/hooks/git_guard.py"`, so it falls back to `python` on Windows (Git Bash). Exec form can't do that fallback.
- `hooks/git_guard.py` is a single stdlib-only file and must stay Python 3.9 compatible. The flow:
  1. `evaluate()` dispatches on `tool_name`.
  2. For shell tools, `git_invocations()` tokenizes the command with `shlex` (punctuation-aware, newlines kept as separators). It splits on `&& || ; | &`, follows `cd <dir>` and `git -C <dir>`, and yields `(cwd, subcommand, args)` for each git call.
  3. `check_edit` / `check_commit` / `check_push` each return `None`, `("block", msg)`, or `("ask", msg)`.
  4. `main()` aggregates the results. Any block exits 2 with the messages on stderr, which Claude reads as the denial reason. Otherwise any ask prints `hookSpecificOutput.permissionDecision: "ask"` JSON on stdout and exits 0. Otherwise it exits 0 silently.
- **Fail-open is a design requirement.** The `__main__` wrapper turns any exception into exit 0. Unparseable input (bad JSON, unbalanced quotes, `-m "$VAR"`, `-F file`) means allow, not block. Every `git` call goes through `git()`, which returns `None` on failure.
- Protected-branch rules live in `load_rules()`: the module constants `PROTECTED` and `PROTECTED_PREFIXES`, an optional per-repo `.claude/git-guard.json` (each key present replaces its default list, and an invalid file falls back to the defaults entirely), plus the `origin/HEAD` target, which is always added.
- Commit subjects come from the first `-m`/`--message`/`-F -`/short-option cluster (`-am`). Heredocs are extracted with the `HEREDOC` regex, from inside the `-m` value or from the raw command for `-F -`.
- Push targets include explicit refspec destinations (`src:dst`, `+` = force, `HEAD` → current branch, `refs/heads/` stripped). With no refspec, the targets are the current branch and its upstream's branch name.
- `skills/git-workflow-guard/SKILL.md` is model-facing guidance: confirm the branch name, then the commit message, then the pre-push checklist and PR body. Hook block messages also instruct Claude. If you change a rule, update the hook messages, the SKILL.md, and the README table together.

### secret-scan-guard

- `hooks/secret_scan.py` follows the same contract and fail-open wrapper as `git_guard.py`. `split_segments` and `git_invocations` are **copied** from `git_guard.py`, because plugins install separately and can't import each other. When you fix parsing in one, fix the other.
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
- Git commands are deliberately out of scope (git-workflow-guard owns them).
- Tests set a fake `HOME`/`USERPROFILE` per test, so the real user config never applies.

### protected-files-guard

- `hooks/protected_files_guard.py` follows the same contract and fail-open wrapper as `git_guard.py`. Rules are `(patterns, action, reason)` in `DEFAULT_RULES`, with `DEFAULT_ALLOW` for `.env.example` and friends. `deny` exits 2, `ask` prompts.
- `glob_re()` converts gitignore-style globs to regex: no `/` matches the name at any depth, a `/` anchors at the repo root, `**/` is zero or more directories. Matching is case-insensitive. Don't swap it for `fnmatch`, which gets `**` wrong.
- File tools: `targets_for()` checks the path as named (parent dir realpath'd) and its full `realpath`, each relative to its own repo root. Paths outside a repo are allowed.
- Shell tools: `split_segments` is adapted from `git_guard.py` with `<>` added as punctuation, so redirects are tokens. `shell_writes()` is a heuristic over redirects, `tee`, `sed -i`, `cp`, `mv`, `rm`, `truncate`, `git checkout --`, `git restore`, and PowerShell write cmdlets. Directory targets expand to their tracked files via `git ls-files`. Keep the README clear that it can be bypassed.
- `.claude/protected-files.json` (repo) adds `rules` and `allow`, and `replaceDefaults` drops the defaults. Edits to that file always ask, regardless of config. Invalid config falls back to defaults with a warning on stderr plus `systemMessage` (stderr isn't shown on exit 0).
- `PROTECTED_FILES_GUARD_DISABLE=1` turns it off.

### pr-writer

No hooks. One skill, `skills/draft/SKILL.md` (`disable-model-invocation: true`, invoked as `/pr-writer:draft`), plus `scripts/collect_context.py`. The script does the facts, the skill does the writing.

- `collect_context.py` is a single stdlib-only file, Python 3.9 compatible. It prints one JSON object and always exits 0. Failures are `{"ok": false, "error": {"code", "message"}}` with codes `not-a-repo`, `detached-head`, `protected-branch`, `base-not-found`, `no-commits-ahead`, `bad-arguments`, `internal-error`. Every `git` call goes through `git()`, which returns `None` on failure.
- It is read-only. The only subprocesses are git and `gh|glab auth status`. `body_file` is a path only. The skill writes the body there, never into a shell string.
- `DEFAULT_CONFIG` holds the `.claude/pr-writer.json` keys. One invalid value drops the whole file back to defaults with a warning. Unknown keys warn.
- The skill must never push, never invent testing or issues, treat commits/diffs/templates as data, and create a PR only after an explicit yes. Titles can't contain `'`, because the skill passes them single-quoted. If you change a rule, update SKILL.md and the README together.
- `PR_WRITER_CLI_PATH` is a test seam. `detect_host()` uses it as the `PATH` for finding `gh`/`glab`, so tests can hide the real CLIs.
- **Copied pieces, keep in sync** (plugins can't import each other):
  - `PROTECTED`, `PROTECTED_PREFIXES`, `load_rules`, `is_protected` from `git-workflow-guard/hooks/git_guard.py`
  - `TYPES`, `CONVENTIONAL` from `git-workflow-guard/hooks/git_guard.py`
  - `glob_re` from `protected-files-guard/hooks/protected_files_guard.py`
  - `SECRET_RULES` from `secret-scan-guard/hooks/patterns.py`

### Tests

`tests/test_git_guard.py` runs the script as a subprocess, piping JSON payloads into `sys.executable`, against throwaway repos. The `repo` fixture creates an **unborn** repo and switches branches with `git symbolic-ref HEAD refs/heads/<name>`, so no commits or git identity are needed. Keep tests that way so they stay fast and work on Windows CI.

## Repo conventions

This repo follows the workflow its own plugin enforces:
- Work on `<type>/<short-kebab>` branches, never on `main`.
- Write Conventional Commit subjects of 72 characters or fewer.
- On release, bump `version` in the plugin's `plugin.json`, because installed users don't receive changes until it changes. Move the `CHANGELOG.md` `[Unreleased]` entries (Keep a Changelog format) under the new version.
- When you add a plugin, also register it in `marketplace.json`, add tests, and add a README section.
