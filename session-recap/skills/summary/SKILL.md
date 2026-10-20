---
name: summary
description: Write a short recap of this session's changes, their impact, the testing that actually ran, and what would make the next session faster.
disable-model-invocation: true
argument-hint: "[--since <ref|time>] [--base <branch>] [--save]"
allowed-tools: Read, Bash(git diff *), Bash(git show *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/collect_recap.py" *), Bash(python "${CLAUDE_PLUGIN_ROOT}/scripts/collect_recap.py" *)
---

# Recap this session

Read `${CLAUDE_PLUGIN_ROOT}/skills/session-recap/SKILL.md` and follow it step by step. Use these arguments for its collect step, verbatim: `$ARGUMENTS --session ${CLAUDE_SESSION_ID}` (the first part may be empty).

Load that file by its path, not by skill name.

That file says `${CLAUDE_PLUGIN_ROOT}`, which is not expanded when you read it. Wherever it appears, use `${CLAUDE_PLUGIN_ROOT}` exactly as written here, so the script path matches this skill's allowed tools.
