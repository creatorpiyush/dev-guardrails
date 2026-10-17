---
name: git-workflow-guard
description: Branch, commit, and push workflow for code changes in a git repo. Use before editing code, and whenever the user mentions commit, push, PR, pull request, merge request, or branch. Confirms branch names, drafts Conventional Commit messages, and builds a pre-push checklist and PR description for approval.
---

# Git workflow guard

The plugin's hooks enforce the hard rules: no edits, commits, or pushes on
protected branches, Conventional Commit subjects only, and a confirmation
prompt before every push. Soft conventions ask the user instead of blocking:
subjects over 72 characters and branch names outside
`<type>/<short-kebab-description>`. This skill covers the judgment around them.

**When a hook blocks a tool call, follow its message. Never work around a
block**: don't edit files through Bash, change the hook config, use
`--no-verify`, or change `git` flags to get past it. If the rule seems wrong
for this repo, tell the user and let them change `.claude/git-guard.json`.

## Phase 1: Branch

Before the first edit, check the current branch with `git branch --show-current`.

If it is protected (main, master, develop, dev, trunk, staging, production,
release/*, hotfix/*, the remote default branch, or anything listed in
`.claude/git-guard.json`):

1. Look for the repo's naming convention in `git branch -a` and
   CONTRIBUTING.md. Default to `<type>/<short-kebab-description>`, for example
   `feat/csv-export` or `fix/login-timeout`.
2. Offer the user options: one recommended name plus one or two
   alternatives. If you have a question tool (such as AskUserQuestion), use
   it, so the user can pick one or type their own.
3. Wait for the answer. Don't create the branch first.
4. Run `git switch -c <name>`. If the user chose a name outside the
   convention, use it as given. The hook asks them to confirm it once more.

## Phase 2: Commit

1. Review `git status` and `git diff --staged`. Stage only files that belong
   to this change. Split unrelated work into separate commits.
2. Draft a Conventional Commit message:
   - Subject: `<type>(<optional scope>)!: <description>`, at most 72 characters,
     imperative mood, no trailing period.
   - Types: feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert.
   - Add `!` and a `BREAKING CHANGE:` footer for breaking changes.
   - Body: what changed and why, wrapped at 72 characters.
3. Show the message to the user and wait for approval. If the subject can't
   fit in 72 characters without losing meaning, offer options: a shortened
   subject with the detail moved to the body, or the longer subject as is.
   Use the user's choice. The hook asks them to confirm a long subject.
4. Commit with `-m` or a heredoc so the hook can read the subject:

   ```bash
   git commit -m "$(cat <<'EOF'
   feat(export): add CSV export for reports

   Users asked to open report data in spreadsheets.
   EOF
   )"
   ```

## Phase 3: Push

1. Read CONTRIBUTING.md and the PR/MR template if they exist
   (`.github/pull_request_template.md`, `.github/PULL_REQUEST_TEMPLATE/`,
   `.gitlab/merge_request_templates/`). Note CODEOWNERS and commitlint config.
2. Build a pre-push checklist from them, and run what you can:
   - Tests pass, and linters and formatters are clean.
   - Every commit on the branch follows Conventional Commits (`git log <base>..HEAD --oneline`).
   - Docs and CHANGELOG are updated if the change needs it.
   - No secrets, debug output, or unrelated files in the diff.
   - Anything else CONTRIBUTING.md requires.
3. Draft the PR title (a Conventional Commit subject) and body. Fill in the repo's
   template. Without one, use:

   ```markdown
   ## Summary
   ## Changes
   ## Testing
   ## Notes
   ```

4. Show the checklist results, title, and body. Wait for approval.
5. Run `git push -u origin <branch>`. The hook asks the user to confirm.
6. Offer to open the PR with
   `gh pr create --title "<title>" --body-file <file>` (or `glab mr create` on GitLab).

Never force push (`--force`, `--force-with-lease`, `+refspec`) without the
user's explicit approval for that specific push.
