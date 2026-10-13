---
name: destructive-command-guard
description: Safer ways to run commands that delete files, reset git state, drop or reset databases, destroy infrastructure, or pipe downloaded scripts to a shell. Use before running rm -r, git reset --hard, git clean, DROP/TRUNCATE, migrate reset, terraform destroy, kubectl delete, docker prune, or curl | sh.
---

# Destructive command guard

The plugin's hook blocks commands that are never safe for an agent, such as
`rm -rf ~`, `rm -rf .` at the project root, `mkfs`, and `dd` to a disk. It asks
the user before other destructive commands. **When the hook blocks a command,
don't look for another command that does the same thing.** Tell the user what
you wanted to do and why, and let them run it.

Prefer a reversible or previewable path first. Use the destructive command only
when the user has asked for that outcome.

| Instead of | Do this first |
| :- | :- |
| `rm -rf <dir>` | Check the path with `ls <dir>`. Use a path relative to the project, never a variable that might be empty. Delete only build output and caches you created |
| `git reset --hard` | `git stash push -u -m "backup before reset"`, or `git reset --soft` / `--mixed` |
| `git clean -fdx` | `git clean -ndx` to preview, then remove specific paths |
| `git checkout -- .` / `git restore .` | `git stash push -u`, or restore single files |
| `git branch -D` | `git branch -d`, which refuses unmerged work |
| `git push --force` | `git push --force-with-lease`, and only after the user approves |
| `DROP` / `TRUNCATE` / `DELETE` without `WHERE` | `SELECT count(*)` with the same condition, a backup (`pg_dump`, `mysqldump`, `.backup`), and a transaction (`BEGIN; ... ROLLBACK;`) |
| `prisma migrate reset`, `rails db:reset`, `manage.py flush` | Confirm it's a local development database, not one from a shared `DATABASE_URL` |
| `terraform destroy`, `terraform apply` | `terraform plan` (or `plan -destroy`) and show the user the plan |
| `kubectl delete` | `kubectl get` the same selector, plus `--dry-run=client`; confirm the context with `kubectl config current-context` |
| `docker system prune` / `volume prune` | `docker system df`, and name specific containers or volumes |
| `curl ... \| sh` | Download to a file, show the user what it does, then run it |

When the user asks for a destructive action, state exactly what will be lost
and whether it can be recovered before you run it.
