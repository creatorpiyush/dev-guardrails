---
name: secret-scan-guard
description: How to handle API keys, tokens, passwords, and other credentials in code. Use when code needs a credential, when adding config or environment variables, when creating .env files, when the secret-scan-guard hook blocks or asks, or when a secret may have been committed or pushed.
---

# Secret scan guard

The plugin's hook checks new file content, commits, pushes, and command lines
for credentials. Provider-format keys (AWS, GitHub, Stripe, private keys, ...)
are blocked. Weaker signals (high-entropy `password = "..."`, JWTs, URL
credentials) ask the user.

**When it blocks, don't get around it.** Don't paste the secret into another
file, split or encode it, write it through Bash, add `secret-scan:allow`, or
edit `.claude/secret-scan.json` or the baseline. Follow this guide instead.

## When code needs a credential

1. Read it from the environment: `os.environ["STRIPE_API_KEY"]`,
   `process.env.STRIPE_API_KEY`, `std::env::var("STRIPE_API_KEY")`. If the
   project already uses dotenv, a settings module, or a secret manager, use
   that.
2. Put local values in `.env`, and check that `.env` is in `.gitignore` before
   writing it. Add the entry if it's missing.
3. Add every variable name to `.env.example` with a placeholder value
   (`STRIPE_API_KEY=replace_me`). That file is committed.
4. In shell commands, use `"$TOKEN"`, not the literal value.
5. Never ask the user to paste a real secret into chat. Ask them to put it in
   `.env` themselves.

## False positives

Use obviously fake values in tests (`test-token`, `sk_test_dummy`). If a
finding really is safe (a revoked key, a realistic fixture), tell the user and
let them decide. They can allowlist it in `~/.claude/secret-scan.json`, or
record existing findings with the documented `--baseline` command.

## If a secret was committed

- **Not pushed:** remove it from the file and amend or rebase the commit that
  added it. Ask the user before rewriting history. The push check scans every
  local commit, so fixing only the latest one isn't enough.
- **Pushed, or seen anywhere outside this machine:** treat it as leaked. Tell
  the user to revoke and rotate it with the provider now. Deleting it from git
  history doesn't undo the leak. Rewriting shared history (`git filter-repo`,
  BFG) needs the user's explicit approval.
