# Contributing

Thanks for helping with cmux-fleet. This is a small, dependency-free plugin;
the bar is that a change keeps it that way.

## Branching

- `main` is always releasable. Do not commit work-in-progress to it.
- Cut a feature branch per change and open a PR against `main`.

## Versioning

This project follows [Semantic Versioning](https://semver.org). The version is
**single-sourced** at `cmux_fleet/__init__.py` (`__version__`); `pyproject.toml`
reads it via `[tool.hatch.version]`, so the wheel/sdist never drift. The two
plugin manifests are independent JSON that Claude Code reads directly, so bump
them together with `__version__` and keep all three identical:

- `cmux_fleet/__init__.py` (`__version__`) — the source of truth
- `.claude-plugin/plugin.json` (`version`)
- `.claude-plugin/marketplace.json` (the plugin entry)

`tests/test_version_single_source.py` fails the build if they diverge. Pin an
explicit `vX.Y.Z`. Never rely on a commit-SHA fallback for the version.

## Changelog

Every PR adds an entry under the current unreleased heading in `CHANGELOG.md`,
in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) form (Added /
Changed / Fixed / Removed). Cutting a release moves the unreleased entries under
a dated `## [X.Y.Z]` heading.

## Tags and releases

Tag a release with an annotated tag, `git tag -a vX.Y.Z -m "..."`, matching the
manifest version.

## Tests

Run the suite (pytest is the one dev dependency; see `tests/README.md`):

```sh
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

Three layers. State the pass count in your PR.

1. **Static validators** check the manifests, skill, and `hooks.json` for
   well-formed JSON and the expected schema.
2. **pytest units** cover state transitions (inbox, registry, archive), the
   hook stdin-to-exit-code contract (inject a throwaway `$CMUX_STATE_DIR`), and
   `config.py` resolution precedence.
3. **e2e** drives the CLI lifecycle (`ls` -> `archive` -> `revive` -> `rm`)
   against a throwaway state with a stubbed cmux (launch/revive spawning is
   exercised via `--dry-run`), plus the `claude --plugin-dir` load when a
   headless `claude` is available.

## Python style

- Standard library only. No external runtime dependencies.
- Python 3.11+ (the config and CLI read TOML via `tomllib`).
- Every module resolves paths through `cmux_fleet/config.py`. Do not hardcode a
  path, a state directory, or the cmux binary anywhere else.
