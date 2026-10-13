# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `secret-scan-guard` plugin 1.0.0: scans new file content, staged and `-a`/pathspec commits, and the local commits a `git push` would send. High-confidence provider keys (AWS, GitHub, GitLab, Slack, Anthropic, OpenAI, Stripe, SendGrid, npm, Twilio, private keys) block. Entropy-checked assignments, JWTs, URL credentials, Google API keys, and AWS secret keys near AWS context ask. Command lines embedding a secret ask, as do staging or committing `.env` and key files. Placeholders, lockfiles, minified and binary files, integrity hashes, and `secret-scan:allow` lines are skipped, but a marker the agent adds itself asks. Findings are redacted to the provider prefix. Config comes from a trusted `~/.claude/secret-scan.json` and a repo `.claude/secret-scan.json` that can only add patterns and name a baseline. Edits to either, or to the baseline, ask. `secret_scan.py --baseline` records SHA-256 fingerprints of existing findings. `SECRET_SCAN_GUARD_DISABLE=1` turns it off. Its skill guides credential handling and leak response.
- `destructive-command-guard` plugin 1.0.0: catches irreversible shell commands by parsing them, not by matching strings. It unwraps `sudo`, `env`, `xargs`, `bash -c`, `eval`, `$(...)`, backticks, and heredocs, and checks a rule table with stable ids covering filesystem, disk, remote-code, database, and infrastructure commands. Asks escalate to deny under `sudo` or against a production database. Config comes from a trusted user file `~/.claude/destructive-guard.json` and a repo file `.claude/destructive-guard.json` that can only make rules stricter. `DESTRUCTIVE_GUARD_DISABLE=1` turns it off. Git commands are left to git-workflow-guard.

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
