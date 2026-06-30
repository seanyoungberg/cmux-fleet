# Contributing

Thanks for helping with cmux-fleet. This is a small, dependency-free plugin;
the bar is that a change keeps it that way.

## Branching

- `main` is always releasable. Do not commit work-in-progress to it.
- Cut a feature branch per change and open a PR against `main`.

## Versioning

This project follows [Semantic Versioning](https://semver.org). When a change
touches behavior or packaging, bump the version in **both** manifests and keep
them identical:

- `.claude-plugin/plugin.json` (`version`)
- `.claude-plugin/marketplace.json` (the plugin entry)

Pin an explicit `vX.Y.Z`. Never rely on a commit-SHA fallback for the version.

## Changelog

Every PR adds an entry under the current unreleased heading in `CHANGELOG.md`,
in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) form (Added /
Changed / Fixed / Removed). Cutting a release moves the unreleased entries under
a dated `## [X.Y.Z]` heading.

## Tags and releases

Tag a release with an annotated tag, `git tag -a vX.Y.Z -m "..."`, matching the
manifest version.

## Tests

Three layers (the suite under `tests/` is being built in P3.2, so some entries
may currently be stubs; state the pass count in your PR):

1. **Static validators** check the manifests, skill, and `hooks.json` for
   well-formed JSON and the expected schema.
2. **pytest units** cover state transitions (inbox, registry, archive), the
   hook stdin-to-exit-code contract (inject a throwaway `$CMUX_STATE_DIR`), and
   `config.py` resolution precedence.
3. **e2e** drives the CLI lifecycle (`launch` -> `ls` -> `recycle` -> `archive`
   -> `revive` -> `rm`) against a throwaway cmux, plus a stranger-first-run pass
   (a fresh `$CMUX_STATE_DIR`, no config file, no vault).

## Python style

- Standard library only. No external runtime dependencies.
- Python 3.11+ (the config and CLI read TOML via `tomllib`).
- Every script resolves paths through `scripts/config.py`. Do not hardcode a
  path, a state directory, or the cmux binary anywhere else.
