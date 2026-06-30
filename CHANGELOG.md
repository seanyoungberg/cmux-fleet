# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - unreleased

Initial port. The native-cmux parent/child orchestration spine, extracted and
cleaned from an internal `cmux-conductor` plugin and decoupled from any single
vault or machine.

### Added

- **`fleet` CLI** (`scripts/fleet.py`, on PATH via `bin/fleet`). Tool-agnostic
  command builder over cmux primitives. Verbs: `launch`, `config`, `ls`,
  `archive`, `revive`, `recycle`, `broadcast`, `mute`/`unmute`, `rm`. Roster is
  role-first and tool-nested (`[defaults]`, `[tool.<t>]`, `[role.<name>]`,
  `[role.<name>.<t>]`); claude and codex adapters, with full `--` flag
  passthrough. Off-roster agents via `--adhoc`.
- **Router daemon** (`scripts/router.py`). One process serves every conductor.
  Listens on cmux's agent event bus, maps a child `Stop` to its parent via the
  live registry, and delivers a completion. Bus is the doorbell, cmux's hook
  store is truth, the transcript is content.
- **Unified, input-safe inbox** (`scripts/fleet_state.py`). One append-only
  stream folds child completions and peer messages, with per-surface, per-kind
  ack cursors. Completions and peer messages reach an agent through context, not
  its input box.
- **Two conductor hooks**. `awareness.py` (UserPromptSubmit) injects the pending
  inbox into context each turn. `drain.py` (Stop) auto-continues a turn to
  process pending work, gated by the mode dial.
- **Peer messaging** (`scripts/peer-msg.py`) and supporting helpers
  (`scripts/child-digest.py`, `scripts/inbox-ack.py`, `scripts/drive-child.py`).
- **`config.py` decoupling**. One path/setting resolver, precedence
  `env > [fleet] toml > XDG default`. Introduces `CMUX_FLEET_ROOT`; moves state
  under `$XDG_STATE_HOME` (`CMUX_STATE_DIR`); resolves the cmux binary via
  `which(cmux)` with a macOS app-bundle fallback (`CMUX_BIN`); makes the
  marketplace and floor `CLAUDE.md` optional (default off, no vault assumption).
- **Plugin packaging**. The repo is its own Claude Code marketplace
  (`marketplace.json` source `./`, strict), with `plugin.json`, `hooks.json`,
  and a conductor skill under `skills/`.
- **Test suite** (`tests/`, pytest-only — the one dev dependency). 64 tests in
  three layers: static manifest/skill/hooks schema validators; pytest units for
  `fleet_state` transitions (inbox/registry/archive/dial), the hook
  stdin->exit-0 contract, and `config.py` resolution precedence
  (`env > [fleet] toml > XDG`, dirname-anchor, malformed-warn); and an e2e CLI
  lifecycle (`ls`/`archive`/`revive`/`rm` against a throwaway state with a
  stubbed cmux) plus a `claude --plugin-dir` load check. 63 passed, 1 skipped
  (real claude load, skipped when no headless `claude` is present).

[0.1.0]: https://github.com/seanyoungberg/cmux-fleet/releases/tag/v0.1.0
