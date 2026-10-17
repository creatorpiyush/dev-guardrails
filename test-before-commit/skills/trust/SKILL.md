---
name: trust
description: Approve the commands in this repo's .claude/test-before-commit.json so test-before-commit runs them before each commit.
disable-model-invocation: true
---

# Approve this repo's test-before-commit commands

Run this from the repo root, and show the user its output:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/test_before_commit.py" --trust
```

If `python3` is missing, use `python`.

The plugin's hook asks the user to confirm this command and lists the exact
commands being approved. Don't change the config or the command to get past
that prompt. If the user denies it, stop.
