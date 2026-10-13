# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python -m pip install pytest                      # only test dependency
python -m pytest                                  # all tests
python -m pytest tests/test_git_guard.py::test_no_verify_asks   # one test
python -m pytest tests/test_secret_scan.py                       # one plugin's tests
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
- Edits are scanned only when the file would be committed. Files outside a repo, and files that `git check-ignore` reports as ignored, are skipped.
- `git commit` scans added lines of `git diff --cached`, plus `git diff` for `-a`-style commits. `RULES` holds high-confidence regexes only. Matches are redacted in messages, because stderr reaches the transcript.
- Fake secrets in `tests/test_secret_scan.py` are built by string concatenation. Keep it that way, or this guard and GitHub push protection will flag the test file.

### Tests

`tests/test_git_guard.py` runs the script as a subprocess, piping JSON payloads into `sys.executable`, against throwaway repos. The `repo` fixture creates an **unborn** repo and switches branches with `git symbolic-ref HEAD refs/heads/<name>`, so no commits or git identity are needed. Keep tests that way so they stay fast and work on Windows CI.

## Repo conventions

This repo follows the workflow its own plugin enforces:
- Work on `<type>/<short-kebab>` branches, never on `main`.
- Write Conventional Commit subjects of 72 characters or fewer.
- On release, bump `version` in the plugin's `plugin.json`, because installed users don't receive changes until it changes. Move the `CHANGELOG.md` `[Unreleased]` entries (Keep a Changelog format) under the new version.
- When you add a plugin, also register it in `marketplace.json`, add tests, and add a README section.
