---
name: review
description: Review the comments and docs in your changes for generic AI boilerplate, then propose rewrites and apply only the ones you approve.
disable-model-invocation: true
argument-hint: "[paths... | --staged | --diff <base>]"
allowed-tools: Read, Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/scan.py"), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/scan.py" *), Bash(python "${CLAUDE_PLUGIN_ROOT}/scripts/scan.py"), Bash(python "${CLAUDE_PLUGIN_ROOT}/scripts/scan.py" *)
---

# Review comments and docs

Read `${CLAUDE_PLUGIN_ROOT}/skills/humanizer/SKILL.md` and follow it step by step. Use these arguments for its scan step, verbatim: `$ARGUMENTS` (empty means uncommitted changes).

Load that file by its path, not by skill name. Another installed plugin may also have a skill named `humanizer`.
