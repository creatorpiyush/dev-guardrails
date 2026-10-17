---
name: test-before-commit
description: What to do when a git commit is blocked because the project's tests or lint failed, timed out, or need approval. Use when a commit is blocked by test-before-commit.
---

# Test before commit

The plugin's hook runs the project's test and lint commands before every
`git commit`. When one fails, the commit is blocked and the hook message
shows the command, its exit code, and the end of its output.

## When a commit is blocked by a failing check

1. Read the output and find the cause. Run the failing command yourself if
   you need more than the last 60 lines.
2. Fix the cause in the code. Don't skip, weaken, mark as expected-to-fail,
   or delete tests, and don't loosen lint rules, to make the check pass.
3. Stage the fix with `git add`, then retry the same commit.

If the check looks wrong (a flaky test, a broken tool, a failure unrelated to
the change), stop and tell the user. Let them decide.

**Never work around the hook without asking the user first**: don't use
`--no-verify`, set `TEST_BEFORE_COMMIT_DISABLE`, edit
`.claude/test-before-commit.json` or `~/.claude/test-before-commit.json`, or
add `skipPatterns` to get past a failing check.

## When the hook asks instead

- **Timed out**: tell the user which command was slow. They choose whether to
  commit anyway or raise its `timeout`.
- **Needs approval**: the repo config's commands haven't been approved. Only
  the user approves them, with `/test-before-commit:trust`. Never run the
  trust command or edit the approval file yourself.
