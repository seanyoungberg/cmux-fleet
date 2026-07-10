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

### A stubbed seam needs one real-seam test

The suite stubs expensive boundaries: the `ps` sweep, the cmux binary, the hook
store. A stub hides the boundary it stands in for, so every stubbed seam also
needs at least one test that exercises the real thing, with a fixture that
reproduces that boundary's **real text shape**.

Both halves are load-bearing, and each was learned from a bug that shipped green:

- The `ps` sweep was stubbed to `""` suite-wide, so the never-orphan pid union
  was structurally untestable, and a version that would refuse to close any
  conductor forever shipped and passed.
- The regression test added for that bug did un-stub the sweep, but fed it a fake
  two-column table. A parser that read the TTY column as argv0 sailed straight
  through it. `ps axeww` prints PID, TT, STAT, TIME, COMMAND; argv0 is field 5.

A parser is only tested by input it could actually receive. Copy fixtures from
real output (`ps axeww` with env appended; a transcript carrying real turn lines
*and* the bookkeeping lines the tool interleaves between them), and assert that
the old, broken implementation fails them.

## Python style

- Standard library only. No external runtime dependencies.
- Python 3.11+ (the config and CLI read TOML via `tomllib`).
- Every module resolves paths through `cmux_fleet/config.py`. Do not hardcode a
  path, a state directory, or the cmux binary anywhere else.
