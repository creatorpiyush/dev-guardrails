# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `secret-scan-guard` plugin 1.0.0: blocks edits and commits that add API keys, tokens, or private keys to tracked files, and asks before `git add` of `.env` files and private keys. Gitignored files are skipped. Allowlist with `.claude/secret-scan.json`.

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
