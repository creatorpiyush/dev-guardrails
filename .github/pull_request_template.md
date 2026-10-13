## Summary

<!-- What does this change do, and why? -->

## Changes

-

## Testing

<!-- How did you verify this? Paste test output or describe manual checks. -->

## Notes

<!-- Anything reviewers should know: trade-offs, follow-ups, screenshots. -->

## Checklist

- [ ] Branch is not `main` and is named `<type>/<short-description>`
- [ ] Every commit follows Conventional Commits, with subjects of 72 characters or fewer
- [ ] `python -m pytest` passes
- [ ] `claude plugin validate .` passes (if manifests or plugins changed)
- [ ] README and docs are updated for user-facing changes
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]`
- [ ] Plugin `version` is bumped (release PRs only)
- [ ] No secrets, debug output, or unrelated files in the diff
