---
name: write
description: Write tests for a file, a symbol, or the changes since a base branch, in the repo's own test style, then run them and check they can fail.
disable-model-invocation: true
argument-hint: "<path... | symbol | --diff <base>>"
allowed-tools: Read, Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/detect_tests.py"), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/detect_tests.py" *), Bash(python "${CLAUDE_PLUGIN_ROOT}/scripts/detect_tests.py"), Bash(python "${CLAUDE_PLUGIN_ROOT}/scripts/detect_tests.py" *)
---

# Write tests

Read `${CLAUDE_PLUGIN_ROOT}/skills/test-writer/SKILL.md` and follow it step by step. Use these arguments for its detection step, verbatim: `$ARGUMENTS`.

Load that file by its path, not by skill name. Another installed plugin may also have a skill named `test-writer`.

That file says `${CLAUDE_PLUGIN_ROOT}`, which is not expanded when you read it. Wherever it appears, use `${CLAUDE_PLUGIN_ROOT}` exactly as written here, so the script path matches this skill's allowed tools.
