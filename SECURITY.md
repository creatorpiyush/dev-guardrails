# Security policy

## What this plugin runs

`git-workflow-guard` registers `PreToolUse` hooks. Each time Claude Code edits a file or runs a Bash or PowerShell command, it runs `git-workflow-guard/hooks/git_guard.py` on your machine with your user's permissions. The script reads the tool call as JSON, runs read-only `git` commands (`rev-parse`, `symbolic-ref`), and reads files in the repository (`.claude/git-guard.json` and the names of guideline files). It makes no network calls and writes nothing to disk.

Read the script before you install the plugin. It is a single standard-library Python file.

This plugin is a workflow guardrail, not a security boundary. It fails open on errors. Protect important branches on the server as well.

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's private vulnerability reporting. Go to the repository's **Security** tab and select **Report a vulnerability**. Don't open a public issue.

Include the affected version, steps to reproduce, and the impact. You can expect an acknowledgement within 7 days. Once a fix is released, the advisory is published with credit to the reporter, unless you ask to stay anonymous.

Examples of in-scope issues:

- A crafted tool input that makes the hook run commands or write files
- A crafted `.claude/git-guard.json` or repository layout that makes the hook run arbitrary code

A way to get a commit or push past the guard is a bug, not a vulnerability. Report it as a regular issue.
