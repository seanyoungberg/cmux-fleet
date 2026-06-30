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

## Commands

| Verb | What it does |
| --- | --- |
| `fleet launch <role\|--adhoc N>` | spawn an agent (`--place tab\|pane\|workspace`, `--dry-run`, `-- <tool flags>`) |
| `fleet ls` | live fleet reconciled against cmux's hook store (flags STALE / pending / MUTED) |
| `fleet recycle [label]` | restart in place, same surface and identity (`--resume`, `--force`) |
| `fleet archive` / `revive` | park a live agent / bring a parked one back |
| `fleet rm <label>` | drop a label (`--kill` closes it, `--with-group` dissolves its group) |
| `fleet mute` / `unmute` | stop / resume pushing a child's completions to its parent |
| `fleet vitals` / `find` / `graph` / `serve` / `paint` | read-only views (triage, lookup, tree, localhost, sidebar) |
| `fleet worktree ls` / `clean` | manage fleet-owned git worktrees (config-gated) |
| `fleet broadcast "<msg>"` | input-safe heads-up to a target set of live agents |
| `fleet profile <name>` | pin all entrypoints at this build (multi-build isolation) |
| `peer-msg.py` / `child-digest.py` / `drive-child.py` / `inbox-ack.py` | agent-facing helpers |

Full runbook in `docs/operations.md`.

## Multiple builds

cmux-fleet has no compile step, so a build is just a checkout directory. A
**profile** pins every entrypoint (the `fleet` CLI on `PATH`, the router, the
hooks, `--plugin-dir`, and every child launch) at one build, so a stable build
and a dev build run side by side with separate config, state, and daemons:

```
eval "$(/path/to/<build>/bin/fleet profile <name> --init)"
```

See `docs/profiles.md` for the model and the workflow to stand up an Nth build.

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
| `CMUX_FLEET_CONTEXT_WINDOW` | `context_window` | `0` (guess per model; set to your window, e.g. `200000` / `1000000`, for an exact `vitals` ctx %) |

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

## Fleet views

Read-only ways to see the fleet. All derive from live state every call, the
registry, cmux's hook stores, and the agents' transcripts. No daemon, no stored
status. Status is inferred **without an LLM** (cmux's `agentLifecycle` is
authoritative, refined by keyword tables); context-remaining % is read from each
agent's transcript token usage.

```
fleet vitals [--json] [--paint]     cheapest-first triage table: who needs you,
                                    who's near-full (ctx %, ! marks <=30% left)
fleet find <query> [--turns N]      content-aware lookup: matches a label/role/cwd
                                    OR what an agent has been saying in its transcript
fleet graph [--html] [--out FILE]   the fleet as a parentage tree (text, or a
                                    self-contained dark HTML page)
fleet serve [--port N]              thin read-only localhost view: GET / -> the graph
                                    HTML, GET /vitals.json -> the rows. No daemon.
fleet paint                         sync fleet state onto the cmux sidebar (a status
                                    pill + a context progress bar per workspace)
```

`fleet vitals` is the triage view: rows are most-urgent first
(error / needs-input / review / working / done / idle), each with its
context-remaining % so you can see who to recycle. The window is a guess per
model. Set `CMUX_FLEET_CONTEXT_WINDOW` (or `[fleet].context_window`) to your
model's window for an exact number (the ranking is correct regardless).

`fleet paint` writes the same state into cmux's native sidebar via `set-status` /
`set-progress` (additive, it never recolors or renames your workspaces), on
change only. For a dedicated board, install the custom sidebar in `sidebars/`:

```
cp sidebars/fleet.swift ~/.config/cmux/sidebars/fleet.swift
cmux sidebar validate fleet && cmux sidebar open fleet
```

It binds to cmux's live workspace data (refreshes ~1s) and reads the context bars
`fleet paint` writes; rows are tappable to jump. (The parentage **tree** is in
`fleet graph` / `fleet serve`: cmux's workspace binding has no parent field.)

Credit: the triage/near-full and no-LLM status-inference ideas are adapted from
[agentmaster](https://github.com/Supersynergy/agentmaster); the localhost-view
shape from [elevens](https://github.com/hummer98/elevens). Design-mined, not copied.

## Roadmap

Planned, not yet implemented: a packaged router/heartbeat daemon
(`fleet daemon start|stop|status`).

## More

- `docs/architecture.md` for the model (bus / hook store / transcript, the state
  files, identity, the daemon and hooks, recycle, worktrees, groups, profiles).
- `docs/operations.md` for running it day to day.
- `docs/profiles.md` for the multi-build / profile model.
- The conductor skill under `skills/cmux-fleet/` orients an agent that is
  acting as a conductor.
