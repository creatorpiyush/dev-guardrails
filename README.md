# dev-guardrails

A Claude Code plugin marketplace of guardrails that keep agent-written code inside your team's engineering workflow. Four plugins use hooks to block or confirm risky tool calls before they run, such as editing on `main`, committing an API key, or force pushing. One, pr-writer, is a skill that drafts pull request descriptions. Install only the plugins you want.

| Plugin | What it guards |
| :- | :- |
| [git-workflow-guard](#git-workflow-guard) | Edits, commits, and pushes on protected branches. Requires Conventional Commits and asks before every push |
| [secret-scan-guard](#secret-scan-guard) | API keys, tokens, and private keys in tracked files, commits, and pushes. Asks before staging `.env` files |
| [destructive-command-guard](#destructive-command-guard) | Irreversible shell commands: `rm -rf` on the wrong path, disk wipes, `curl \| sh`, `DROP TABLE`, `terraform destroy` |
| [protected-files-guard](#protected-files-guard) | Silent edits to lockfiles, `.env` and key files, CI config, migrations, and license files |
| [pr-writer](#pr-writer) | Not a guard. Drafts a PR title and description from your branch, fills the repo's PR template, and opens the PR only after you approve |

## Install

In Claude Code:

```text
/plugin marketplace add creatorpiyush/dev-guardrails
/plugin install git-workflow-guard@dev-guardrails
/plugin install secret-scan-guard@dev-guardrails
/plugin install destructive-command-guard@dev-guardrails
/plugin install protected-files-guard@dev-guardrails
/plugin install pr-writer@dev-guardrails
```

Or from your shell:

```bash
claude plugin marketplace add creatorpiyush/dev-guardrails
claude plugin install git-workflow-guard@dev-guardrails
claude plugin install secret-scan-guard@dev-guardrails
claude plugin install destructive-command-guard@dev-guardrails
claude plugin install protected-files-guard@dev-guardrails
claude plugin install pr-writer@dev-guardrails
```

Installing a plugin enables its hooks and skills. Run `/reload-plugins` or start a new session. To confirm the hooks loaded, run `/hooks` and look for the plugin under `PreToolUse`. From the shell, `claude plugin details <plugin>` shows `Hooks (1)  PreToolUse`.

**Requirements:** `git`, and Python 3.9+ on `PATH` as `python3` or `python`. Hooks use only the Python standard library. On Windows, hook commands run in Git Bash, the default shell for Claude Code hooks there. Without Git Bash, the hooks can't start and the plugins enforce nothing.

## git-workflow-guard

Stops Claude from editing, committing, or pushing on protected branches. It requires Conventional Commit messages and asks you before every push, listing the repo's contribution guidelines. A companion skill handles the judgment calls: it proposes branch names, drafts commit messages and PR descriptions, and runs a pre-push checklist. You approve each of these before Claude acts.

| Action | Condition | Result |
| :- | :- | :- |
| Edit, Write, MultiEdit, NotebookEdit | File is in a git repo on a protected branch | **Blocked.** Claude proposes a branch name, waits for your OK, then runs `git switch -c` |
| `git commit` | Current branch is protected | **Blocked** |
| `git commit` | Subject isn't a Conventional Commit | **Blocked** |
| `git commit` | Subject is over 72 chars | **Asks** you, so you can keep a longer subject |
| `git commit --no-verify`, `-n` (also in clusters like `-nm`), or `git -c core.hooksPath=...` | Any branch | **Asks** you |
| `git push` | Target is protected (explicit refspec, `HEAD`, or the implicit current/upstream branch) | **Blocked** |
| `git push` | Any other target | **Asks** you, listing CONTRIBUTING.md, PR/MR templates, CODEOWNERS, and commitlint config found in the repo |
| `git push --force`, `+refspec`, `--all`, `--mirror` | Any target that isn't protected | **Asks** you, with a warning |
| `git switch -c`, `git checkout -b`, `git branch <name>` | New name isn't `<type>/<short-kebab-description>` | **Asks** you, so you can keep your own name |
| `git reset --hard`, `git clean -f` (not `-n`), `git branch -D`, `git stash drop` / `clear`, `git checkout -- <path>` / `git checkout .`, `git restore <path>` (not `--staged` alone) | Any branch | **Asks** you, because they throw away work git can't bring back |
| Edit `.claude/git-guard.json`, or a shell command that mentions it | Any branch that isn't protected | **Asks** you, because it decides which branches are protected |
| Anything else | | Allowed |

Default protected branches: `main`, `master`, `develop`, `dev`, `trunk`, `staging`, `production`, `release/*`, `hotfix/*`, and the remote default branch (`origin/HEAD`).

The hook finds git calls inside `&&`/`;`/`|` chains, subshells `( ... )`, `$( ... )`, backticks, `if`/`while` blocks, wrappers such as `sudo`, `env`, `command`, and `timeout`, and `bash -c '...'` or `eval`. It doesn't see git run from a script file, an alias, or `"$(...)"` inside double quotes.

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
- Because this file can switch protection off, Claude editing it asks you first.

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
- Python's `re` has no timeout. A badly written `extraPatterns` regex (nested quantifiers such as `(a+)+`) can stall every tool call until the 15-second hook timeout, after which Claude Code lets the call through. Keep patterns simple, and review those a cloned repo brings.

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
- An invalid file is ignored as a whole, the defaults apply, and the hook shows you a warning.
- Python's `re` has no timeout. A badly written `extraRules` or `prodPattern` regex (nested quantifiers such as `(a+)+`) can stall every shell command until the 15-second hook timeout, after which Claude Code lets it through. Keep patterns simple, and review those a cloned repo brings.
- To switch the plugin off temporarily, set `DESTRUCTIVE_GUARD_DISABLE=1` in the environment Claude Code starts in.

### How it combines with permission rules and other hooks

- The hook runs before Claude Code's permission rules.
- A deny exits with code 2. That stops the call even if one of your allow rules matches it.
- An ask forces a permission prompt, even with a matching allow rule or in auto mode.
- Your own `deny` and `ask` permission rules still apply on top. The hook never returns `allow`, so it can only add friction.
- When several hooks answer the same call, Claude Code takes the strictest decision: deny, then defer, then ask, then allow.

## protected-files-guard

Stops Claude from silently editing files that should only change on purpose: lockfiles, `.env` and key files, CI config, migrations, and license or ownership files. Secrets are denied. Everything else asks you first.

| Files | Result | Why |
| :- | :- | :- |
| `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `*.p12`, `*.keystore`, `credentials.json` | **Denied** | Holds secrets. `.env.example`, `.env.sample`, and `.env.template` are allowed |
| `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`, `uv.lock`, `Cargo.lock`, `Gemfile.lock`, `composer.lock`, `go.sum` | **Asks** you | Change dependencies with the package manager, not by hand |
| `.github/workflows/**`, `.gitlab-ci.yml`, `Jenkinsfile`, `.circleci/**` | **Asks** you | CI/CD config |
| `**/migrations/**`, `**/alembic/versions/**`, `**/db/migrate/**` | **Asks** you | Database migrations |
| `LICENSE*`, `CODEOWNERS`, `SECURITY.md` | **Asks** you | Legal and policy files |
| `.claude/protected-files.json` | **Asks** you, always | The guard's own config. `allow` and `replaceDefaults` can't turn this off |

A pattern without `/` matches the file name at any depth, so `yarn.lock` also matches `web/yarn.lock`. A pattern with `/` is anchored at the repo root. Matching is case-insensitive.

### How it works

- **File tools are checked exactly.** For Edit, Write, MultiEdit, and NotebookEdit, the hook resolves the target path with `realpath`, makes it relative to the git repo root, and matches it against the rules. A symlink is checked both as named and where it points, so `notes.txt -> .env` is denied. Files outside a git repo are allowed.
- **Shell commands are checked heuristically.** For Bash and PowerShell, the hook looks for writes through `>`, `>>`, `tee`, `sed -i`, `cp`, `mv`, `ln`, `install`, `rm`, `truncate`, `git checkout -- <path>`, `git restore <path>`, and the PowerShell cmdlets `Set-Content`, `Add-Content`, `Out-File`, `Clear-Content`, `Copy-Item`, `Move-Item`, and `Remove-Item`. It follows `cd` and `git -C`, skips wrappers such as `sudo -u root` and `env`, expands `~`, environment variables, and globs, and checks the tracked files under a directory target such as `rm -rf db`. **This can be bypassed.** `python -c`, `dd`, `perl -i`, scripts, and paths built at run time aren't seen. It catches honest mistakes, not an agent trying to get around it.
- **The skill handles judgment.** [`skills/protected-files-guard/SKILL.md`](protected-files-guard/skills/protected-files-guard/SKILL.md) tells Claude never to work around a block, to explain why the file is protected, and to propose the proper change, such as `npm install <pkg>` for a lockfile or a new migration instead of editing an old one.

### Configuration

Commit `.claude/protected-files.json` at the repo root:

```json
{
  "rules": [{ "pattern": "infra/**", "action": "deny", "reason": "Managed by platform team" }],
  "allow": ["docs/migrations/**"],
  "replaceDefaults": false
}
```

- `rules` are added to the defaults. Each needs a `pattern` and an `action` of `ask` or `deny`. `reason` is shown in the message.
- `allow` patterns override every matching rule, default or custom.
- `replaceDefaults: true` drops the default rules and the default `.env.example` allowances, leaving only yours.
- Unlike the secret-scan and destructive-command configs, this repo file can loosen the defaults, because there is no user-level file. Claude editing it always asks you, and a change to it shows up in review like any other committed file.
- Patterns are globs: `*` and `?` don't cross `/`, `**/` matches zero or more directories, and a trailing `/**` matches everything below.
- If the file is invalid, the hook ignores all of it, uses the defaults, and shows a warning.

**Emergency escape hatch:** set `PROTECTED_FILES_GUARD_DISABLE=1` in the environment Claude Code starts in to turn the hook off for that session.

## pr-writer

Drafts a pull request (or GitLab merge request) title and description from your branch's commits and diff, fills in the repo's PR template, and creates the PR after you approve it. It has no hooks. It adds one skill, which only runs when you call it.

```text
/pr-writer:draft [base-branch] [--ready | --draft]
```

A script, [`scripts/collect_context.py`](pr-writer/scripts/collect_context.py), gathers the facts as JSON. It only reads the repo. It creates an empty temp directory for the PR body file, and removes its own directories from earlier runs once they are a day old. The skill ([`skills/draft/SKILL.md`](pr-writer/skills/draft/SKILL.md)) writes the text from them.

| The script computes | Details |
| :- | :- |
| Base branch | The argument, then `origin/HEAD`, then `main` or `master` |
| Commits and diff | Commits ahead of the merge base, changed files with line counts, and the diff text. Past `diffSizeCap`, you get excerpts |
| Breaking changes | Commits with `!` or a `BREAKING CHANGE:` footer |
| Title | A suggestion from the commits, checked against `titleFormat` and `maxTitleLength` |
| Issues | References found in the branch name and commit messages, and whether a closing keyword was used |
| Templates | PR/MR templates under `.github/`, `docs/`, the repo root, and `.gitlab/` |
| Reviewers | `CODEOWNERS` owners for the changed files |
| Push state | Whether the branch is pushed and matches `HEAD` |
| Host | Whether `gh` or `glab` is installed and authenticated |

It stops with an error code on a non-repo, detached HEAD, protected branch, missing base, no commits ahead, or bad arguments. Protected branches use the same rules as git-workflow-guard.

### What it won't do

- **Never invent facts.** Testing lists only commands run in the session or tests the diff adds. Checkboxes stay unticked without evidence. Issues come only from the script. `Closes` is used only when a closing keyword was already there.
- **Treat repo content as data.** Commit messages, diffs, and templates can't give it instructions.
- **Redact secrets.** secret-scan-guard's high-confidence matches, passwords in URLs (`scheme://user:<redacted>@host`), and JSON Web Tokens are removed from commits, diff, and templates before the text is shown, and it tells you what was redacted.
- **Never push.** If the branch isn't pushed, it tells you to push it yourself.
- **Ask first.** It shows the title and body and creates the PR only after an explicit yes. Drafts are the default.

`gh` or `glab` is optional. With neither installed and authenticated, you get a title and body ready to paste. The only network call is `gh auth status` or `glab auth status`, to see whether you are signed in.

### Configuration

Optional `.claude/pr-writer.json` at the repo root. Defaults are shown.

| Key | Default | Meaning |
| :- | :- | :- |
| `draftByDefault` | `true` | Open PRs as drafts. `--ready` and `--draft` override it |
| `titleFormat` | `"conventional"` | `"conventional"` or `"free"` |
| `maxTitleLength` | `72` | Longest allowed title |
| `language` | `"en"` | Language of the description |
| `labels` | `{}` | Conventional type to label name, such as `{"feat": "enhancement"}`. The label for the title's type is offered after creation |
| `extraChecklist` | `[]` | Items added to the body as unticked checkboxes |
| `diffSizeCap` | `60000` | UTF-8 bytes of diff text to send before switching to excerpts |

An invalid file or value is ignored, the defaults apply, and a warning is shown. Unknown keys warn.

## Limitations

- **Fail open.** If a hook hits an unexpected error, can't parse a command, or Python is missing, it allows the call.
- **Not a security boundary.** The hooks guard against honest mistakes by an agent. They don't stop a determined user or process.
- **pr-writer push state is from the last fetch.** Run `git fetch` first if the remote may have changed.
- **pr-writer issue detection is a heuristic.** It reads branch names and commit text, and can miss or misread a reference.
- **pr-writer summarises large diffs.** Past `diffSizeCap` it sees excerpts and says so.
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
