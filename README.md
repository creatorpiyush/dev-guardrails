# dev-guardrails

A Claude Code plugin marketplace of guardrails that keep agent-written code inside your team's engineering workflow. Each plugin uses hooks to block or confirm risky tool calls before they run, such as editing on `main`, committing an API key, or force pushing. Install only the plugins you want.

| Plugin | What it guards |
| :- | :- |
| [git-workflow-guard](#git-workflow-guard) | Edits, commits, and pushes on protected branches. Requires Conventional Commits and asks before every push |
| [secret-scan-guard](#secret-scan-guard) | API keys, tokens, and private keys in tracked files and commits. Asks before staging `.env` files |

## Install

In Claude Code:

```text
/plugin marketplace add creatorpiyush/dev-guardrails
/plugin install git-workflow-guard@dev-guardrails
/plugin install secret-scan-guard@dev-guardrails
```

Or from your shell:

```bash
claude plugin marketplace add creatorpiyush/dev-guardrails
claude plugin install git-workflow-guard@dev-guardrails
claude plugin install secret-scan-guard@dev-guardrails
```

Installing a plugin enables its hooks. Run `/reload-plugins` or start a new session. To confirm the hooks loaded, run `/hooks` and look for the plugin under `PreToolUse`. From the shell, `claude plugin details <plugin>` shows `Hooks (1)  PreToolUse`.

**Requirements:** `git`, and Python 3.9+ on `PATH` as `python3` or `python`. Hooks use only the Python standard library. On Windows, hook commands run in Git Bash, the default shell for Claude Code hooks there. Without Git Bash, the hooks can't start and the plugins enforce nothing.

## git-workflow-guard

Stops Claude from editing, committing, or pushing on protected branches. It requires Conventional Commit messages and asks you before every push, listing the repo's contribution guidelines. A companion skill handles the judgment calls: it proposes branch names, drafts commit messages and PR descriptions, and runs a pre-push checklist. You approve each of these before Claude acts.

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

### How it works

- **Hooks enforce.** [`hooks/git_guard.py`](git-workflow-guard/hooks/git_guard.py) runs as a `PreToolUse` hook on every file edit and every Bash/PowerShell call. It exits 2 to block, which Claude reads as the reason. It returns `permissionDecision: "ask"` to show you a confirmation prompt. Otherwise it allows the call.
- **The skill handles judgment.** [`skills/git-workflow-guard/SKILL.md`](git-workflow-guard/skills/git-workflow-guard/SKILL.md) tells Claude how to choose a branch name, write a commit message, and prepare a push and PR. It asks for your approval at each step. The skill tells Claude never to work around a hook block.

### Configuration

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

A commit message built from shell variables (`-m "$MSG"`) or read from a file (`-F msg.txt`) isn't checked.

## secret-scan-guard

Keeps credentials out of anything git will commit.

| Action | Condition | Result |
| :- | :- | :- |
| Edit, Write, MultiEdit, NotebookEdit | New content in a file git would track contains a secret | **Blocked.** The secret is redacted in the message |
| Same | File is gitignored (such as `.env`), or outside a git repo | Allowed. That's where secrets belong |
| `git commit` | Lines added in the staged diff contain a secret. With `-a`, unstaged changes to tracked files are scanned too | **Blocked** |
| `git add` | Paths include `.env`, `.env.*`, `*.pem`, `*.key`, or `id_rsa`-style keys. `.env.example`, `.sample`, `.template`, and `.dist` are fine | **Asks** you |

Detected secrets: AWS access key IDs, GitHub and GitLab tokens, Slack tokens and webhooks, OpenAI and Anthropic API keys, Stripe live keys, Google API keys, npm tokens, private key blocks, and JSON Web Tokens. Only high-confidence patterns are used. There's no entropy guessing, and documentation placeholders containing `EXAMPLE` are ignored.

### Configuration

To allow known false positives such as test fixtures, commit `.claude/secret-scan.json`:

```json
{
  "allowPaths": ["tests/fixtures/*"],
  "allowPatterns": ["^sk-test-"]
}
```

- `allowPaths` are `fnmatch` globs on repo-relative paths, where `*` also matches `/`.
- `allowPatterns` are regular expressions. A match is ignored when the regex is found in the matched secret.
- If the file is invalid, the hook ignores the whole file and nothing is allowlisted.

The plugin's skill shows Claude the safe pattern: read credentials from environment variables, keep values in a gitignored `.env`, and document names in `.env.example`. It also covers what to do when a secret was committed or pushed.

Secrets written through Bash, such as `echo "$KEY" > config.py`, aren't caught at edit time. They're still caught when the file is committed.

## Limitations

- **Fail open.** If a hook hits an unexpected error, can't parse a command, or Python is missing, it allows the call.
- **Not a security boundary.** The hooks guard against honest mistakes by an agent. They don't stop a determined user or process.
- **Use server-side protection too.** Turn on branch protection rules and secret scanning push protection on GitHub (or the GitLab equivalents) for anything that matters.

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
