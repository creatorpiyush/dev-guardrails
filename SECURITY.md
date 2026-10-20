# Security policy

## What these plugins run

Read the scripts before you install a plugin. Each one is standard-library Python and runs on your machine with your user's permissions. None of them is a security boundary: the hooks guard against honest mistakes by an agent, fail open on errors, and can be bypassed. Protect important branches on the server as well.

| Plugin | Runs | Reads | Writes | Network |
| :- | :- | :- | :- | :- |
| git-workflow-guard | `hooks/git_guard.py` on every file edit and Bash/PowerShell call | The tool call, `.claude/git-guard.json`, guideline file names, read-only `git` output | Nothing | None |
| secret-scan-guard | `hooks/secret_scan.py` on every file edit and Bash/PowerShell call | The tool call, the edited file, `~/.claude/secret-scan.json`, `.claude/secret-scan.json`, the baseline, read-only `git diff`/`git log` output | Nothing as a hook. `secret_scan.py --baseline` writes the baseline file you ask for | None |
| destructive-command-guard | `hooks/destructive_command_guard.py` on every Bash/PowerShell call | The tool call, `~/.claude/destructive-guard.json`, `.claude/destructive-guard.json` | Nothing | None |
| protected-files-guard | `hooks/protected_files_guard.py` on every file edit and Bash/PowerShell call | The tool call, `.claude/protected-files.json`, read-only `git` output | Nothing | None |
| test-before-commit | `hooks/test_before_commit.py` on every file edit and Bash/PowerShell call. On `git commit`, the commands you configured, from the repo root, without a shell unless a command sets `shell` | The tool call, `~/.claude/test-before-commit.json`, `.claude/test-before-commit.json`, the trust file, `package.json`/`pyproject.toml` for autodetect, read-only `git` output | `.git/test-before-commit-cache.json`, and `git write-tree` objects. `--trust` writes `~/.claude/test-before-commit-trust.json` | Whatever your test commands do |
| pr-writer | `scripts/collect_context.py` when you run `/pr-writer:draft` | Read-only `git` output, `.claude/pr-writer.json`, `.claude/git-guard.json`, PR templates, `CODEOWNERS` | An empty temp directory for the PR body file. It removes its own day-old ones (named `pr-writer-*`, owned by you, holding only `body.md`) | `gh auth status` or `glab auth status`, to see whether you are signed in |
| humanizer | `scripts/scan.py` when you run `/humanizer:review`. If you set `commitNudge`, `hooks/humanizer_nudge.py` on Bash/PowerShell `git` calls, which runs `scan.py --staged` | The tool call, `.claude/humanizer.json`, the changed files, tracked docs and up to 20 tracked code files for the voice sample, read-only `git` output | Nothing. Claude applies only the rewrites you approve, with the normal edit tools | None |
| test-writer | `scripts/detect_tests.py` when you run `/test-writer:write` or ask for tests. The test commands Claude runs are the repo's own and ask for permission each time | Tracked and untracked files that aren't gitignored (never `.env`, key, or credential files), read-only `git` output | Nothing, except `--snapshot`: a copy of the project without `.git` in a new temp directory for the mutation check. It removes its own day-old ones (named `test-writer-*`, owned by you, holding its marker file) | Whatever your test commands do |
| session-recap | `hooks/log_activity.py` on every file edit and Bash/PowerShell call, and on tool failures, permission denials, and Stop. `scripts/collect_recap.py` when you run `/session-recap:summary` | The tool call (paths, edit counts, commands, durations, exit status), `.claude/session-recap.json`, read-only `git` output, changed files (never `.env`, key, or credential files), `.claude/recaps/*.md` | `~/.claude/session-recap/<repo hash>/<session>.jsonl`: paths, counts, redacted commands, never contents or output; the directory is mode 0700 and files 0600. It deletes logs older than the retention limit. Claude writes a recap or `CLAUDE.md` line only after you say yes | None |

Hook messages go into Claude's context, so secret-scan-guard only ever shows redacted previews, and pr-writer redacts secrets before Claude sees the diff. pr-writer never runs a `gh` or `glab` binary from inside the repository. test-before-commit puts the last 60 lines of a failing command's output into Claude's context, so keep secrets out of your test output.

## Supported versions

Only the latest release of each plugin receives security fixes.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's private vulnerability reporting. Go to the repository's **Security** tab and select **Report a vulnerability**. Don't open a public issue.

Include the affected plugin and version, steps to reproduce, and the impact. You can expect an acknowledgement within 7 days. Once a fix is released, the advisory is published with credit to the reporter, unless you ask to stay anonymous.

Examples of in-scope issues:

- A crafted tool input that makes a hook or script run commands or write files
- A crafted config file or repository layout that makes a hook or script run arbitrary code
- A secret that reaches Claude's context or the transcript unredacted through a hook message or pr-writer's output
- A repo config that loosens secret-scan-guard or destructive-command-guard, which only your user config may do

A way to get a commit, push, edit, or command past a guard is a bug, not a vulnerability. Report it as a regular issue.
