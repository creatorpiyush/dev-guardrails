# Contributing

Thanks for helping improve dev-guardrails. This repository follows the same workflow its plugin enforces.

## Workflow

1. Fork the repository, or create a branch if you have write access. Never commit to `main`.
2. Name the branch `<type>/<short-kebab-description>`, for example `feat/push-tag-check` or `fix/windows-paths`.
3. Write commits as [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

   ```text
   <type>(<optional scope>)!: <description>
   ```

   Types are `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, and `revert`. Keep the subject at 72 characters or fewer.
4. Open a pull request and fill in the template.

## Run the tests

You need Python 3.9+ and git.

```bash
python -m pip install pytest
python -m pytest
```

CI runs the tests on Ubuntu, macOS, and Windows on every supported Python version, and checks that every JSON file parses.

If you have Claude Code installed, also validate the manifests:

```bash
claude plugin validate .
claude plugin validate ./git-workflow-guard
```

## Try your changes in Claude Code

Add your clone as a local marketplace:

```bash
claude plugin marketplace add ./
claude plugin install git-workflow-guard@dev-guardrails
```

A marketplace added from a local path loads plugins in place, so edits take effect after `/reload-plugins` or a new session.

## Add a new plugin

1. Create a folder at the repo root named after the plugin, in kebab-case.
2. Add `.claude-plugin/plugin.json` with `name`, `version`, `description`, `author`, `repository`, `license`, `homepage`, and `keywords`.
3. Put components at the plugin root (`skills/`, `hooks/hooks.json`, `agents/`, ...), not inside `.claude-plugin/`. Reference bundled files through `${CLAUDE_PLUGIN_ROOT}`.
4. Register the plugin in `.claude-plugin/marketplace.json` with `"source": "./<plugin-name>"`. The entry `name` must match the manifest `name`.
5. Add tests under `tests/`, and document the plugin in the README.
6. Run `claude plugin validate .`.

## Release

1. Bump `version` in the plugin's `.claude-plugin/plugin.json` using [Semantic Versioning](https://semver.org/). Users don't receive changes until the version changes.
2. Move the `[Unreleased]` entries in `CHANGELOG.md` under the new version and date.
3. Commit as `chore(release): <plugin> v<version>`, merge, and tag the merge commit `v<version>`.
