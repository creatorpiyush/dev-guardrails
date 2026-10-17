# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `secret-scan-guard` plugin 1.0.0: scans new file content, staged and `-a`/pathspec commits, and the local commits a `git push` would send. High-confidence provider keys (AWS, GitHub, GitLab, Slack, Anthropic, OpenAI, Stripe, SendGrid, npm, Twilio, private keys) block. Entropy-checked assignments, JWTs, URL credentials, Google API keys, and AWS secret keys near AWS context ask. Command lines embedding a secret ask, as do staging or committing `.env` and key files. Placeholders, lockfiles, minified and binary files, integrity hashes, and `secret-scan:allow` lines are skipped, but a marker the agent adds itself asks. Findings are redacted to the provider prefix. Config comes from a trusted `~/.claude/secret-scan.json` and a repo `.claude/secret-scan.json` that can only add patterns and name a baseline. Edits to either, or to the baseline, ask. `secret_scan.py --baseline` records SHA-256 fingerprints of existing findings. `SECRET_SCAN_GUARD_DISABLE=1` turns it off. Its skill guides credential handling and leak response.
- `destructive-command-guard` plugin 1.0.0: catches irreversible shell commands by parsing them, not by matching strings. It unwraps `sudo`, `env`, `xargs`, `bash -c`, `eval`, `$(...)`, backticks, and heredocs, and checks a rule table with stable ids covering filesystem, disk, remote-code, database, and infrastructure commands. Asks escalate to deny under `sudo` or against a production database. Config comes from a trusted user file `~/.claude/destructive-guard.json` and a repo file `.claude/destructive-guard.json` that can only make rules stricter. `DESTRUCTIVE_GUARD_DISABLE=1` turns it off. Git commands are left to git-workflow-guard.
- `protected-files-guard` plugin 1.0.0: stops silent edits to files that should change only on purpose. Edits to `.env`/`.env.*` (except `.env.example`, `.sample`, `.template`), `*.pem`, `*.key`, `id_rsa*`, `*.p12`, `*.keystore`, and `credentials.json` are denied. Lockfiles, CI config, migrations, `LICENSE*`, `CODEOWNERS`, and `SECURITY.md` ask, and the lockfile message points to the package manager. File tool paths are resolved with `realpath`, so symlinks can't dodge rules. Shell commands are checked heuristically for redirects, `tee`, `sed -i`, `cp`, `mv`, `rm`, `truncate`, `git checkout --`, `git restore`, and PowerShell write cmdlets. A repo `.claude/protected-files.json` adds rules, `allow` overrides, and `replaceDefaults`, with `**` glob support. `PROTECTED_FILES_GUARD_DISABLE=1` turns it off. Its skill explains why a file is protected and proposes the proper change.
- `pr-writer` plugin 1.0.0: `/pr-writer:draft [base-branch] [--ready | --draft]` drafts a pull request or merge request title and description from the branch's commits and diff. A read-only script collects the base, commits, diff (excerpted past `diffSizeCap`), breaking changes, issue references, PR templates, `CODEOWNERS` owners, push state, and whether `gh` or `glab` is signed in. The skill fills the repo's template, checks the title against Conventional Commits, redacts secrets, and treats commit and diff text as data. It lists only testing it can show, leaves checkboxes unticked without evidence, and never pushes. It creates the PR only after an explicit yes, as a draft by default, and gives paste-ready text when no authenticated CLI is available. Config comes from `.claude/pr-writer.json`. It has no hooks.

## [1.0.0] - 2026-10-13

### Added

- `dev-guardrails` marketplace with the `git-workflow-guard` plugin.
- `PreToolUse` hook that blocks file edits and commits on protected branches.
- Conventional Commit check on `git commit` subjects (`-m`, `--message`, and heredocs), with a 72-character limit.
- Confirmation prompt for `git commit --no-verify`.
- `git push` check that blocks protected targets and asks before any other push, listing the repo's contribution guidelines and warning on force, `--all`, and `--mirror` pushes.
- Per-repo override of protected branches through `.claude/git-guard.json`.
- `git-workflow-guard` skill for branch naming, commit drafting, and pre-push checklists.

[Unreleased]: https://github.com/creatorpiyush/dev-guardrails/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/creatorpiyush/dev-guardrails/releases/tag/v1.0.0
