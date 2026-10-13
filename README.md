# dev-guardrails

A Claude Code plugin marketplace with guardrails that keep agent-written code inside your team's git workflow. Its first plugin, **git-workflow-guard**, stops Claude from editing, committing, or pushing on protected branches. It requires Conventional Commit messages and asks you before every push, listing the repo's contribution guidelines. A companion skill handles the judgment calls: it proposes branch names, drafts commit messages and PR descriptions, and runs a pre-push checklist. You approve each of these before Claude acts.

## What it does

| Action | Condition | Result |
| :- | :- | :- |
| Edit, Write, MultiEdit, NotebookEdit | File is in a git repo on a protected branch | **Blocked.** Claude proposes a branch name, waits for your OK, then runs `git switch -c` |
| `git commit` | Current branch is protected | **Blocked** |
| `git commit` | Subject isn't a Conventional Commit or is over 72 chars | **Blocked** |
| `git commit --no-verify` / `-n` | Any branch | **Asks** you |
| `git push` | Target is protected (explicit refspec, `HEAD`, or the implicit current/upstream branch) | **Blocked** |
| `git push` | Any other target | **Asks** you, listing CONTRIBUTING.md, PR/MR templates, CODEOWNERS, and commitlint config found in the repo |
| `git push --force`, `+refspec`, `--all`, `--mirror` | Any target that isn't protected | **Asks** you, with a warning |
| Anything else | | Allowed |

Default protected branches: `main`, `master`, `develop`, `dev`, `trunk`, `staging`, `production`, `release/*`, `hotfix/*`, and the remote default branch (`origin/HEAD`).

Conventional Commit subject format:

```text
<type>(<optional scope>)!: <description>
type = feat | fix | docs | style | refactor | perf | test | build | ci | chore | revert
```

## Install

In Claude Code:

```text
/plugin marketplace add creatorpiyush/dev-guardrails
/plugin install git-workflow-guard@dev-guardrails
```

Or from your shell:

```bash
claude plugin marketplace add creatorpiyush/dev-guardrails
claude plugin install git-workflow-guard@dev-guardrails
```

Run `/reload-plugins` or start a new session. To confirm the hooks loaded, run `/hooks` and check that `PreToolUse` lists the plugin's hooks on `Edit|Write|MultiEdit|NotebookEdit` and `Bash|PowerShell`. From the shell, `claude plugin details git-workflow-guard` shows `Hooks (1)  PreToolUse`.

**Requirements:** `git`, and Python 3.9+ on `PATH` as `python3` or `python`. The hook uses only the Python standard library. On Windows, the hook command runs in Git Bash, the default shell for Claude Code hooks there. Without Git Bash, the hook can't start and the plugin enforces nothing.

## How it works

The plugin has two parts:

- **Hooks enforce.** [`hooks/git_guard.py`](git-workflow-guard/hooks/git_guard.py) runs as a `PreToolUse` hook on every file edit and every Bash/PowerShell call. It exits 2 to block, which Claude reads as the reason. It returns `permissionDecision: "ask"` to show you a confirmation prompt. Otherwise it allows the call.
- **The skill handles judgment.** [`skills/git-workflow-guard/SKILL.md`](git-workflow-guard/skills/git-workflow-guard/SKILL.md) tells Claude how to choose a branch name, write a commit message, and prepare a push and PR. It asks for your approval at each step. The skill tells Claude never to work around a hook block.

## Configuration

To change the protected branches for one repo, commit `.claude/git-guard.json` at the repo root:

```json
{
  "protected": ["main", "production"],
  "protectedPrefixes": ["release/"]
}
```

- Each key you set replaces its default list. A key you omit keeps its default.
- The remote default branch (`origin/HEAD`) is always protected.
- If the file isn't valid JSON, or a key isn't a list of strings, the hook ignores the whole file and uses the defaults.

## Limitations

- **Fails open.** If the hook hits an unexpected error, can't parse a command, or Python is missing, it allows the call. A commit message built from shell variables (`-m "$MSG"`) or read from a file (`-F msg.txt`) isn't checked.
- **Not a security boundary.** The hook guards against honest mistakes by an agent. It doesn't stop a determined user or process. Claude can still change files through Bash commands such as `sed -i` or `echo >`, and only the skill tells it not to.
- **Needs `python3` (or `python`) and `git`** on `PATH`.
- **Use server-side branch protection too.** Turn on branch protection rules or rulesets on GitHub (or protected branches on GitLab) for anything that matters.

## Add another plugin to this marketplace

1. Create a sibling folder at the repo root, such as `my-plugin/`, with `.claude-plugin/plugin.json` and its components (`skills/`, `hooks/`, `agents/`, ...). Reference bundled files through `${CLAUDE_PLUGIN_ROOT}`.
2. Add an entry to [`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json):

   ```json
   { "name": "my-plugin", "source": "./my-plugin", "description": "What it does" }
   ```

   The entry `name` must match the `name` in the plugin's `plugin.json`.
3. Run `claude plugin validate .` and `claude plugin validate ./my-plugin`.
4. Add tests under `tests/` and a CHANGELOG entry.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full process.

## License

[MIT](LICENSE)
