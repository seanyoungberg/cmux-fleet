"""cmux_fleet — the native-cmux fleet APP: the `fleet` CLI, the completion router/daemon, and the
shared state/config library. Packaged as a uv tool (entry point `fleet = cmux_fleet.cli:main`);
the Claude plugin (hooks + skills) installs separately and reaches this via `fleet` on PATH.
Stdlib-only at runtime (config reads TOML via `tomllib`, python>=3.11)."""

__version__ = "0.14.0"
