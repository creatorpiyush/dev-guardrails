---
name: draft
description: Draft a blameless postmortem for an incident from this debugging session, git history, merged PRs, and log files, with a sourced UTC timeline, root cause, and action items.
disable-model-invocation: true
argument-hint: "[\"<incident title>\"] [--since <time>] [--until <time>] [--logs <paths>]"
allowed-tools: Read, Bash(git show *), Bash(git log *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gather_timeline.py"), Bash(python "${CLAUDE_PLUGIN_ROOT}/scripts/gather_timeline.py"), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gather_timeline.py" *), Bash(python "${CLAUDE_PLUGIN_ROOT}/scripts/gather_timeline.py" *)
---

# Draft a postmortem

Read `${CLAUDE_PLUGIN_ROOT}/skills/incident-notes/SKILL.md` and follow it step by step. Use these arguments for its gather step, verbatim: `$ARGUMENTS` (may be empty).

Load that file by its path, not by skill name.

That file says `${CLAUDE_PLUGIN_ROOT}`, which is not expanded when you read it. Wherever it appears, use `${CLAUDE_PLUGIN_ROOT}` exactly as written here, so the script path matches this skill's allowed tools.
