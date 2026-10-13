---
name: secret-scan-guard
description: How to handle API keys, tokens, passwords, and other credentials in code. Use when code needs a credential, when adding config or environment variables, when creating .env files, or when a secret may have been committed or pushed.
---

# Secret scan guard

The plugin's hook blocks edits and commits that put a secret into a file git
would track, and asks before `git add` of `.env` files or private keys.
**When it blocks, don't get around it.** Don't split the secret across lines,
encode it, or write it through Bash. Follow this guide instead.

## When code needs a credential

1. Read it from the environment: `os.environ["STRIPE_API_KEY"]`,
   `process.env.STRIPE_API_KEY`, `std::env::var("STRIPE_API_KEY")`.
2. Put local values in `.env`, and check that `.env` is in `.gitignore` before
   writing it. Add the entry if it's missing.
3. Create or update `.env.example` with every variable name and a placeholder
   value (`STRIPE_API_KEY=sk_test_replace_me`). This file is committed.
4. If the project already loads config a certain way (dotenv, a settings
   module, a secret manager), use that.
5. Never ask the user to paste a real secret into chat. Ask them to put it in
   `.env` themselves.

## Tests and fixtures

Use obviously fake values (`test-token`, `sk_test_dummy`). If a realistic-looking
fixture is required, ask the user to allowlist the path in
`.claude/secret-scan.json` under `allowPaths`.

## If a secret was committed

- **Not pushed:** remove it from the file, `git add` the fix, and amend or
  rebase the commit that added it. Ask the user before rewriting history.
- **Pushed, or seen anywhere outside this machine:** treat it as leaked. Tell
  the user to revoke and rotate it with the provider right away. Removing it
  from git history doesn't undo the leak. Rewriting shared history
  (`git filter-repo`, BFG) needs the user's explicit approval.
