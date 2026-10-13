# dev-guardrails

A Claude Code plugin marketplace of guardrails that keep agent-written code inside your team's engineering workflow. Each plugin uses hooks to block or confirm risky tool calls before they run, such as editing on `main`, committing an API key, or force pushing. Install only the plugins you want.

| Plugin | What it guards |
| :- | :- |
| [git-workflow-guard](#git-workflow-guard) | Edits, commits, and pushes on protected branches. Requires Conventional Commits and asks before every push |
| [secret-scan-guard](#secret-scan-guard) | API keys, tokens, and private keys in tracked files, commits, and pushes. Asks before staging `.env` files |
| [destructive-command-guard](#destructive-command-guard) | Irreversible shell commands: `rm -rf` on the wrong path, disk wipes, `curl \| sh`, `DROP TABLE`, `terraform destroy` |

## Install

In Claude Code:

```text
/plugin marketplace add creatorpiyush/dev-guardrails
/plugin install git-workflow-guard@dev-guardrails
/plugin install secret-scan-guard@dev-guardrails
/plugin install destructive-command-guard@dev-guardrails
```

Or from your shell:

```bash
claude plugin marketplace add creatorpiyush/dev-guardrails
claude plugin install git-workflow-guard@dev-guardrails
claude plugin install secret-scan-guard@dev-guardrails
claude plugin install destructive-command-guard@dev-guardrails
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

Keeps credentials out of anything git will commit or push.

| Action | Condition | Result |
| :- | :- | :- |
| Edit, Write, MultiEdit, NotebookEdit | The new text (not the rest of the file) contains a high-confidence secret | **Blocked** |
| Same | The new text contains a medium-confidence secret | **Asks** you |
| Same | File is gitignored (such as `.env`), outside a git repo, a lockfile, minified, or binary | Allowed |
| Same | File is `.claude/secret-scan.json`, `~/.claude/secret-scan.json`, or the baseline | **Asks** you |
| `git commit` | Lines added in the staged diff contain a secret. With `-a`, unstaged tracked changes are scanned too; with `git commit <path>`, those paths are | **Blocked** (high) or **asks** (medium) |
| `git commit` | The commit includes `.env`, `*.pem`, `*.key`, or `id_rsa`-style files, or changes the guard's config or baseline | **Asks** you |
| `git push` | Lines added in any local commit about to be pushed (not yet on a remote) contain a secret | **Blocked** (high) or **asks** (medium) |
| `git add` | Paths include `.env`, `.env.*`, `*.pem`, `*.key`, or `id_rsa`-style keys. `.env.example`, `.sample`, `.template`, and `.dist` are fine | **Asks** you |
| Any Bash or PowerShell command | The command line itself contains a secret, such as `curl -H "Authorization: Bearer ..."` | **Asks** you. It's already in the transcript, so blocking wouldn't help |

High confidence (blocks): AWS access key IDs, GitHub and GitLab tokens, Slack tokens and webhooks, Anthropic and OpenAI API keys, Stripe live keys, SendGrid keys, npm tokens, Twilio API keys, and private key blocks.

Medium confidence (asks): Google API keys (often public in web apps), credentials in URLs (`scheme://user:password@host`), JSON Web Tokens, `password`/`secret`/`token`/`api_key`/`auth` assignments whose value is 16+ characters with at least 3.5 bits/char of entropy, and 40-character AWS secret keys within 3 lines of an AWS key or the word `aws`.

Rule ids are in [`hooks/patterns.py`](secret-scan-guard/hooks/patterns.py). These are never findings:

- Placeholder values containing `example`, `dummy`, `changeme`, `your_`, `xxxx`, `<...>`, `${...}`, `$VAR`, `process.env.`, `os.environ`, or `os.getenv`, or a single repeated character.
- Integrity hashes (`sha512-...`).
- A line containing `secret-scan:allow`. If Claude is the one adding the marker, the hook asks instead, so Claude can't allowlist its own secret.

Messages show the rule id, file, line, and at most the first 4 characters of the match (just the provider prefix, such as `ghp_***`). Full values never reach stdout, stderr, or disk.

### Configuration

There are two files with the same format:

```json
{
  "allowPaths": ["tests/fixtures/**"],
  "allowPatterns": ["^sk_test_"],
  "extraPatterns": [{"id": "internal-token", "regex": "int_[A-Za-z0-9]{32}", "action": "deny"}],
  "baseline": ".claude/secret-scan-baseline.json"
}
```

- `~/.claude/secret-scan.json` is yours and trusted. It can use every key.
- `.claude/secret-scan.json` in the repo can only add `extraPatterns` and name a `baseline`. Its `allowPaths` and `allowPatterns` are ignored with a warning, so a cloned repo, or Claude editing one, can't allowlist secrets.
- `allowPaths` are `fnmatch` globs on repo-relative paths, where `*` also matches `/`. `allowPatterns` are regexes searched in the matched value. `extraPatterns` take `action` `deny` (block) or `ask`.
- An invalid file is ignored entirely, and the hook shows you a warning.

### Baseline for existing repos

To adopt the hook in a repo that already contains findings (test fixtures, revoked keys), record them once:

```bash
python3 ~/.claude/plugins/marketplaces/dev-guardrails/secret-scan-guard/hooks/secret_scan.py --baseline
```

Run it from inside the repo. The script is in the marketplace clone shown above, or in your own clone of this repository. It scans every tracked file and writes `.claude/secret-scan-baseline.json`, or the configured `baseline` path, or `--output PATH`. The file holds only SHA-256 fingerprints of path, rule id, and value. It never holds the secrets. Commit it so your team shares it. A recorded finding stops blocking in that file. A new secret, or the same secret in another file, still blocks. Regenerate after removing secrets to drop stale entries. A renamed file loses its suppression.

### Turning it off

Set `SECRET_SCAN_GUARD_DISABLE=1` in the environment you start `claude` from. Setting it inside a Bash command has no effect, because the hook runs in Claude Code's own environment.

The plugin's skill shows Claude the safe pattern: read credentials from environment variables, keep values in a gitignored `.env`, and document names in `.env.example`. It also covers what to do when a secret was committed or pushed.

A secret written through Bash, such as `echo "$KEY" > config.py`, isn't caught at edit time. It's still caught when the file is committed or pushed.

## destructive-command-guard

Catches irreversible shell commands before Claude runs them, and says what to do instead.

### How it parses commands

The hook doesn't search the raw command string. It tokenises the command with `shlex` and splits it into simple commands on `;`, `&&`, `||`, `|`, and newlines. It then unwraps `sudo`, `env`, `nohup`, `time`, `xargs`, `bash -c`/`sh -c`, `eval`, `$(...)`, backticks, and heredocs, and checks each inner command, up to 5 levels deep. It reads the command name and arguments, so `echo "rm -rf /"` and `grep "DROP TABLE" schema.sql` aren't flagged. A command `shlex` can't parse gets a few conservative pattern checks and is otherwise allowed.

### Rules

Every block or prompt shows the rule id, why the command is dangerous, and a safer alternative.

| Rule id | Catches | Default |
| :- | :- | :- |
| `fs.rm-catastrophic` | `rm` with `-r` or `-f` on `/`, `/*`, `~`, `$HOME`, `.`, `..`, `*`, the project root, or a parent of it; `--no-preserve-root` | deny |
| `fs.rm-unresolved` | `rm -r` whose target is a variable or comes from `xargs` (`rm -rf $DIR/` becomes `rm -rf /` when `DIR` is empty) | ask |
| `fs.rm-outside-repo` | `rm -r` on a path outside the repo | ask |
| `fs.rm-inside-repo` | `rm -r` on a path inside the repo, except build output (`node_modules`, `dist`, `build`, `target`, `__pycache__`, `.venv`, `.pytest_cache`, `coverage`, `.next`), which is allowed silently | ask |
| `fs.find-delete` | `find ... -delete`, `find ... -exec rm` | ask |
| `fs.shred-truncate` | `shred`; `truncate` on anything but `.log` files and build output | ask |
| `disk.overwrite` | `mkfs*`, `dd of=/dev/sd*`, redirects to `/dev/sd*` or `/dev/nvme*`, `Format-Volume`, `diskutil erase*` | deny |
| `fs.chmod-root` | `chmod -R` / `chown -R` on `/` or `~` | deny |
| `fs.chmod-777` | `chmod -R 777` anywhere else | ask |
| `proc.fork-bomb` | `:(){ :\|:& };:` | deny |
| `remote.pipe-to-shell` | `curl\|wget ... \| sh/bash/zsh/python`, `bash <(curl ...)`, `eval "$(curl ...)"`, PowerShell `iwr ... \| iex` | ask |
| `db.drop` | `DROP TABLE\|DATABASE\|SCHEMA`, `TRUNCATE` | ask |
| `db.unbounded-write` | `DELETE FROM` or `UPDATE` without `WHERE` | ask |
| `db.flush` | `FLUSHALL`, `FLUSHDB`, `dropDatabase()` | ask |
| `infra.terraform` | `terraform destroy`, `terraform apply -auto-approve` | ask |
| `infra.kubectl-delete` | `kubectl delete ns ...`, `kubectl delete ... --all` | ask |
| `infra.docker-prune` | `docker system prune -a`, `docker volume prune` | ask |
| `infra.cloud-storage-delete` | `aws s3 rm --recursive`, `aws s3 rb --force` | ask |
| `infra.cloud-delete` | `gcloud ... delete`, `az ... delete` | ask |

The database rules apply when `psql`, `mysql`, `mariadb`, `sqlite3`, `mongo`/`mongosh`, or `redis-cli` gets SQL through `-c`, `-e`, `--eval`, positional arguments, a heredoc, or a here-string.

**Escalation to deny:**
- Any ask rule becomes deny under `sudo`, so `sudo rm -rf /var/x` is denied.
- A database rule becomes deny when the host or connection string in the same command matches `prodPattern`, which defaults to `prod(uction)?`. That covers `-h`, `--host`, a `postgres://...` argument, or a `DATABASE_URL=...` prefix. Table names aren't checked, so `DROP TABLE products` isn't escalated.

**Git is out of scope.** `git reset --hard`, `git clean`, force pushes, and `git branch -D` belong to git-workflow-guard. Covering them here too would show two prompts for one command.

### Configuration

Two optional files with the same format:

| File | Trust | May |
| :- | :- | :- |
| `~/.claude/destructive-guard.json` | Trusted (yours) | Change any rule, including to `allow`; set `allow`; add `extraRules`; replace `prodPattern` |
| `.claude/destructive-guard.json` in the repo | Untrusted (comes with a clone) | Add `extraRules` (ask or deny only); make rules **stricter**; add to `prodPattern` |

```json
{
  "rules": { "infra.docker-prune": "allow", "fs.rm-inside-repo": "deny" },
  "allow": ["rm -rf src/generated"],
  "extraRules": [
    { "id": "team.deploy-prod", "regex": "^\\./deploy\\.sh prod", "action": "deny", "reason": "deploys to production" }
  ],
  "prodPattern": "prod(uction)?|live"
}
```

- `allow` holds regular expressions. Each must match a whole command, or a whole simple command within it.
- A repo file can never loosen anything. If it tries to downgrade a rule, set `allow`, or add an `allow` extra rule, the hook ignores that entry and logs a warning. A cloned repository can't switch off your protections.
- An invalid file is ignored as a whole, the defaults apply, and a warning goes to stderr. Run `claude --debug` to see the warning.
- To switch the plugin off temporarily, set `DESTRUCTIVE_GUARD_DISABLE=1` in the environment Claude Code starts in.

### How it combines with permission rules and other hooks

- The hook runs before Claude Code's permission rules.
- A deny exits with code 2. That stops the call even if one of your allow rules matches it.
- An ask forces a permission prompt, even with a matching allow rule or in auto mode.
- Your own `deny` and `ask` permission rules still apply on top. The hook never returns `allow`, so it can only add friction.
- When several hooks answer the same call, Claude Code takes the strictest decision: deny, then defer, then ask, then allow.

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
