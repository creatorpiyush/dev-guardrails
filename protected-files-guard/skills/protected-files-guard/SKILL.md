---
name: protected-files-guard
description: How to handle files that change only on purpose - lockfiles, .env and key files, CI config, migrations, LICENSE, CODEOWNERS. Use when a protected-files-guard hook blocks or asks about an edit, or when a task needs one of these files changed.
---

# Protected files guard

The plugin's hooks enforce the rules. They deny edits to `.env` files and
private keys, and ask the user before Claude touches lockfiles, CI config,
migrations, and license or ownership files. This skill covers what to do
around them.

**Never work around a block or a declined prompt.** Don't edit the file
through Bash (`sed -i`, `>`, `tee`, `cp`, `mv`, `python -c`, ...), another
tool, a symlink, or a change to `.claude/protected-files.json`. The shell
check is heuristic, so a workaround may get through. That makes it worse,
not allowed.

## When a hook blocks or asks

1. Stop and tell the user which file it is and why it's protected. Quote the
   reason from the hook message.
2. Propose the proper way to make the change:
   - **Lockfile:** run the package manager and let it rewrite the lockfile:
     `npm install <pkg>`, `yarn add`, `pnpm add`, `poetry add`, `uv add`,
     `cargo add` / `cargo update -p <crate>`, `bundle add`,
     `composer require`, `go get <module>` then `go mod tidy`.
   - **`.env` or a key file:** tell the user which variable or key to set and
     let them edit it. Document new variable names in `.env.example`.
   - **Migration:** generate a new migration with the framework's tool
     (`python manage.py makemigrations`, `alembic revision --autogenerate`,
     `rails generate migration`) instead of editing one that may have run.
   - **CI config, LICENSE, CODEOWNERS, SECURITY.md:** show the exact diff and
     why it's needed, and let the user approve it.
3. Wait for the user's answer. On an ask prompt, the user decides; if they
   decline, don't retry the same change another way.

If a rule is wrong for this repo, tell the user they can change it in
`.claude/protected-files.json` (`rules`, `allow`, `replaceDefaults`).
