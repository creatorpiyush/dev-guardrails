---
name: destructive-command-guard
description: How to respond when a shell command is blocked or prompted as destructive (rm -rf, disk writes, curl | sh, DROP/TRUNCATE/DELETE, terraform destroy, kubectl delete, docker prune, cloud deletes), and how to pick a safer alternative before running one.
---

# Destructive command guard

The plugin's hook checks every Bash and PowerShell command. It denies commands
that are never safe for an agent, and asks the user before other irreversible
ones. Each message names a rule id, the risk, and a safer alternative.

When a command is blocked or prompted:

1. Explain the risk to the user in plain words: what would be lost, and
   whether it could be recovered.
2. Offer the safer alternative from the message, or a better one for this
   situation. Examples: `rm -ri` on explicit paths, a `SELECT` with the same
   `WHERE` before a `DELETE`, `terraform plan` before `destroy`,
   `kubectl delete --dry-run=server`, or downloading a script and reading it
   before running it.
3. If the user still wants the original command after a deny, they run it
   themselves in a terminal.

**Never rephrase a command to get past the hook.** Don't split it into steps,
move the path into a variable, wrap it in another shell or language, encode
it, or write it into a script and run that. Doing so defeats the only check
standing between a mistake and lost data.

Before running something destructive, prefer the preview: list what a glob or
`find` matches, check `git status`, confirm the database host and the
Kubernetes context, and take a backup when data is involved.
