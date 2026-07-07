# cmux-fleet

Native parent/child agent orchestration for [cmux](https://cmux.io).

A conductor agent spawns child agents (children or sub-conductors) as real cmux
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
- **[uv](https://docs.astral.sh/uv/)** to install the app (`uv tool install`).
- No other runtime dependencies. The app is standard-library Python.

## Install

Two independent installs. **The app installer never touches the plugin** — you
install each the way that fits it, and the plugin reaches the app through the
`fleet` on `PATH`.

### 1. The app — the `fleet` CLI + router daemon

Install it as a uv tool, so a `fleet` console script lands on your `PATH` and the
package is vendored in its own environment (no repo checkout needed at runtime):

```
uv tool install .                                             # from a checkout, or:
uv tool install "git+https://github.com/seanyoungberg/cmux-fleet@v0.1.0"   # pinned tag (private repo → git auth)
```

Upgrade with `uv tool upgrade cmux-fleet`, or install a new tag to move between
releases. The app is standard-library only.

### 2. The plugin — the conductor hooks + skill

Installed separately, your way; the app install deliberately does **not** do
this. cmux-fleet is its own Claude Code marketplace (`marketplace.json` sources
one plugin from the repo root, `./`):

```
/plugin marketplace add seanyoungberg/cmux-fleet
/plugin install cmux-fleet@cmux-fleet
```

The plugin's hooks are thin shims that call the installed `fleet` app on `PATH`.
**If the app is not installed the hooks silently no-op** — fleet features simply
don't activate and the rest of Claude Code is unaffected — so install the app
first. There is no network fallback; the plugin requires the app.

## Quickstart

1. Seed a roster (optional). With no config at all you can still `fleet launch
   --adhoc <name>`; a roster only adds named roles.

   ```
   fleet profile default --init   # pins this build + seeds the bundled example roster, or plainly:
   mkdir -p ~/.config/cmux-fleet && cp fleet.toml.example ~/.config/cmux-fleet/fleet.toml
   ```

2. Start the router daemon (one per machine/profile, serves every conductor). It
   double-forks and detaches, so it outlives the starting shell and a conductor
   recycle:

   ```
   fleet daemon start
   fleet daemon status          # running? version/python/package that owns it, state dir, uptime, bus seq
   ```

   For reboot persistence, run it under launchd with `fleet daemon start
   --foreground` (the plist + a cutover runbook are in `docs/operations.md`).
   `fleet daemon` is the only supported way to run the router — never start it by
   hand from an agent session: a bare `nohup &` router dies with the tool's
   process group, or worse survives as a stray duplicate that double-processes
   the bus. Exactly one `cmux_fleet.router --live` should ever exist:

   ```
   ps aux | grep 'cmux_fleet.router --live'   # expect one line
   ```

3. From inside a cmux conductor surface, launch a child and check it registered:

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
| `fleet ls [--scope …]` | live fleet reconciled against cmux's hook store (flags STALE / pending / MUTED); defaults `--scope mine` (you + your children), `--scope all` for the world |
| `fleet recycle [label]` | restart in place, same surface and identity (default RESUME; `--fresh` sheds, `--session <id>`, `--force`; bulk `--scope mine\|all\|conductors\|children`) |
| `fleet sessions <label>` | list resumable prior sessions for the agent's surface (id, age, size, snippet) |
| `fleet archive` / `revive` | archive a live agent / bring an archived one back (`revive --fresh\|--session`) |
| `fleet rm <label>` | close + archive a label (revivable; `--detach` drops the row only, `--force` overrides the mid-turn guard, `--with-group` dissolves its group) |
| `fleet mute` / `unmute` | stop / resume pushing a child's completions to its parent (`--scope mine` mutes all your children) |
| `fleet inbox [--scope …]` | your pending inbox on demand (completions + alerts + peer msgs); the catch-up read after a recycle |
| `fleet vitals` / `find` / `graph` / `serve` / `paint` | read-only views (triage, lookup, tree, localhost, sidebar); scope-aware verbs default `--scope mine` |
| `fleet worktree ls` / `clean` | manage fleet-owned git worktrees (config-gated) |
| `fleet broadcast "<msg>" --scope …` | input-safe heads-up to a scoped set of live agents (`--scope` required — an act) |
| `fleet profile <name>` | pin all entrypoints at this build (multi-build isolation) |
| `fleet daemon start\|stop\|status\|restart` | run the router as a detached daemon (survives shell exit + recycle); `start --foreground` for launchd; `--heartbeat` to nudge idle conductors |
| `fleet peer-msg` / `fleet child-digest` / `fleet drive-child` / `fleet inbox-ack` | agent-facing helpers |

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
block in the toml > built-in default**, in `cmux_fleet/config.py`. With none of
them set and cmux on `$PATH`, the defaults are stranger-safe: state under XDG,
no vault assumption, marketplace and floor disabled.

| Env var | `[fleet]` key | Default |
| --- | --- | --- |
| `CMUX_FLEET_TOML` | (locates itself) | `$XDG_CONFIG_HOME/cmux-fleet/fleet.toml` (`~/.config/cmux-fleet/fleet.toml`) |
| `CMUX_FLEET_ROOT` | `root` | `$HOME` (set `root = "."` for the toml's own directory) |
| `CMUX_STATE_DIR` | `state_dir` | `$XDG_STATE_HOME/cmux-fleet` (`~/.local/state/cmux-fleet`) |
| `CMUX_BIN` | `cmux_bin` | `which cmux`, else `/Applications/cmux.app/Contents/Resources/bin/cmux` |
| `CMUX_FLEET_PLUGIN_INDEX` | `plugin_index` | `<toml-dir>/plugins.toml` (the plugin index; declares `[marketplace.*]`) |
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

A single file, `$CMUX_STATE_DIR/notify-mode`, is the wake **mute switch**.
Wake-now is the **default**; the dial exists only to opt out:

- **auto** (the default — no file needed): a completed child, or a peer message,
  **wakes a genuinely idle conductor** (at the prompt, empty draft) to handle it
  now, and at a turn's end (the Stop hook) the conductor auto-continues to drain
  pending work instead of stopping.
- **passive**: the single mute. Completions and peers wait in the inbox and
  surface via context on the conductor's next turn; nothing is auto-driven or
  woken — for a "leave me alone, I'll drain on my own" or debugging conductor.

(Peer messages still drain at Stop regardless, and `peer-msg --no-wake`
is the per-message opt-out for acks/FYIs.)

## Fleet views

Read-only ways to see the fleet. All derive from live state every call, the
registry, cmux's hook stores, and the agents' transcripts. No daemon, no stored
status. Status is inferred **without an LLM** (cmux's `agentLifecycle` is
authoritative, refined by keyword tables); context-remaining % is read from each
agent's transcript token usage.

Every scope-aware verb takes `--scope mine|all|conductors|children` (one shared
vocabulary). Reads default `--scope mine` — you + your direct children; add
`--scope all` for the whole fleet.

```
fleet inbox [--scope …] [--json]    your pending inbox on demand (completions +
                                    alerts + peer msgs) — the catch-up read after a recycle
fleet vitals [--scope …] [--json]   cheapest-first triage table: who needs you,
             [--paint]              who's near-full (ctx %, ! marks <=30% left)
fleet find <query> [--turns N]      content-aware lookup: matches a label/role/cwd
                                    OR what an agent has been saying in its transcript
fleet graph [--scope …] [--html]    the fleet as a parentage tree (text, or a
            [--out FILE]            self-contained dark HTML page); default = your subtree
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

Forward-looking, not yet built:

- **A `fleet daemon install-launchd` helper.** Reboot persistence works today by
  running the router under launchd with `fleet daemon start --foreground`; the
  plist and the cutover steps are documented in `docs/operations.md`. A helper
  verb that writes the `~/Library/LaunchAgents` plist and `bootstrap`s it (so you
  don't hand-author it) is still to come. Until then, or on a non-launchd box,
  run `fleet daemon start` after a reboot.

## More

- `docs/architecture.md` for the model (bus / hook store / transcript, the state
  files, identity, the daemon and hooks, recycle, worktrees, groups, profiles).
- `docs/operations.md` for running it day to day.
- `docs/profiles.md` for the multi-build / profile model.
- The conductor skill under `skills/cmux-fleet/` orients an agent that is
  acting as a conductor.
