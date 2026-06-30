# cmux-fleet

Native parent/child agent orchestration for [cmux](https://cmux.io).

A conductor agent spawns child agents (workers or sub-conductors) as real cmux
surfaces, places them in panes, tabs, or workspaces, and gets told when they
finish. The notify path is **input-safe**: a child's completion and a peer's
message reach a conductor through its context, never by typing into its input
box, so an agent that is mid-draft is never clobbered.

cmux already owns sessions, lifecycle, transcripts, and an event bus. This
plugin adds only the orchestration delta on top: an org chart, a unified inbox,
and the hooks and daemon that move completions to the right parent.

## Requirements

- **cmux**, on `$PATH` as `cmux` (or point `CMUX_BIN` at it).
- **Claude Code** and/or **codex** as the agent tool.
- **python3.11+** (the CLI and config read TOML via `tomllib`).
- No other runtime dependencies. The plugin is standard-library Python.

## Install

cmux-fleet is its own Claude Code marketplace. `marketplace.json` lists one
plugin sourced from the repo root (`./`), so adding the repo as a marketplace
makes the plugin installable from it.

```
/plugin marketplace add seanyoungberg/cmux-fleet
/plugin install cmux-fleet@cmux-fleet
```

To put the `fleet` CLI on your `PATH`, symlink or add `bin/` to your shell:

```
ln -s "$(pwd)/bin/fleet" ~/.local/bin/fleet
```

`bin/fleet` is a thin shim to `scripts/fleet.py`.

## Quickstart

1. Copy the example config and edit your roster:

   ```
   mkdir -p ~/.config/cmux-fleet
   cp fleet.toml.example ~/.config/cmux-fleet/fleet.toml
   ```

   The file is optional. With no config at all you can still `fleet launch
   --adhoc <name>`; a roster only adds named roles.

2. Start the router daemon (one per machine, serves every conductor):

   ```
   python3 scripts/router.py --live
   ```

3. From inside a cmux conductor surface, launch a child:

   ```
   fleet launch worker
   fleet launch --adhoc scratch --tool claude -- --model opus
   fleet ls
   ```

The router watches the bus; when the child finishes a turn, the conductor sees
the completion in its context on its next turn (or sooner, depending on the
mode dial below).

## Configuration

Every setting resolves with the precedence **environment variable > `[fleet]`
block in the toml > built-in default**, in `scripts/config.py`. With none of
them set and cmux on `$PATH`, the defaults are stranger-safe: state under XDG,
no vault assumption, marketplace and floor disabled.

| Env var | `[fleet]` key | Default |
| --- | --- | --- |
| `CMUX_FLEET_TOML` | (locates itself) | `$XDG_CONFIG_HOME/cmux-fleet/fleet.toml` (`~/.config/cmux-fleet/fleet.toml`) |
| `CMUX_FLEET_ROOT` | `root` | `$HOME` (set `root = "."` for the toml's own directory) |
| `CMUX_STATE_DIR` | `state_dir` | `$XDG_STATE_HOME/cmux-fleet` (`~/.local/state/cmux-fleet`) |
| `CMUX_BIN` | `cmux_bin` | `which cmux`, else `/Applications/cmux.app/Contents/Resources/bin/cmux` |
| `CMUX_FLEET_MARKETPLACE` | `marketplace` | `""` (internal `--plugin-dir` name resolution disabled) |
| `CMUX_FLEET_FLOOR` | `floor_claudemd` | `""` (no ad-hoc `CLAUDE.md` symlink) |
| `CMUX_HOOKSTORE_DIR` | `hookstore_dir` | `~/.cmuxterm` (cmux-owned) |
| `CMUX_FLEET_ADHOC_SUBDIR` | `adhoc_subdir` | `agents/ad-hoc` (relative to root) |

`CMUX_FLEET_ROOT` is the workspace root that a role's relative `cwd` composes
against. It defaults to `$HOME`, so a config file in `~/.config` does not silently
make role cwds resolve there. For a project-local layout, set `root = "."` (a
relative `[fleet]` path anchors to the toml's own directory) or give an absolute
path. `CMUX_HOOKSTORE_DIR` points at cmux's own per-agent hook stores (this plugin
reads them, cmux writes them).

The roster and per-tool launch config live in the same toml; see
`fleet.toml.example` for the role-first, tool-nested layout.

## The mode dial

A single file, `$CMUX_STATE_DIR/notify-mode`, controls how aggressively pending
work reaches a conductor:

- **passive** (default): completions and peers wait in the inbox; the conductor
  sees them via context on its next turn. Nothing is auto-driven.
- **autodrain**: at a turn's end (the Stop hook), the conductor auto-continues
  to process pending child completions instead of stopping. Peer messages always
  drain at Stop regardless of mode.
- **auto**: autodrain, plus the router wakes a genuinely idle conductor (at the
  prompt, empty draft) to handle pending completions now.

## Roadmap

These are planned, not yet implemented: `fleet vitals` (a triage table),
`fleet find` (content-aware session lookup), `fleet graph` (a fleet tree),
`fleet serve` (a localhost live view), native cmux sidebar telemetry, and
config-gated git worktree isolation per workstream.

## More

- `docs/architecture.md` for the model (bus / hook store / transcript, the state
  files, identity, the daemon and hooks).
- `docs/operations.md` for running it day to day.
- The conductor skill under `skills/cmux-fleet/` orients an agent that is
  acting as a conductor.
