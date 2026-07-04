# Operations

Day-to-day running of a cmux fleet. All commands assume `fleet` is on your
`PATH` (the installed app, or a checkout's `bin/fleet` shim) and you are issuing
them from inside a conductor's cmux surface, so `$CMUX_SURFACE_ID` is set.

## Cheat sheet

```
# daemon
fleet daemon start [--heartbeat]            # detached router (survives shell exit + recycle)
fleet daemon status | stop | restart
echo auto > "$CMUX_STATE_DIR/notify-mode"   # passive (mute) | auto (default, wake-now)

# spawn + drive
fleet launch <role> [--place tab|pane|workspace] [--dry-run] [-- <tool flags>]
fleet launch --adhoc <name> --tool claude -- --model opus
fleet drive-child <surface> "<prompt>"
fleet child-digest <session-frag> 5

# inventory + lifecycle
fleet ls                                    # live x hook store; flags STALE / pending / MUTED
fleet recycle [label] [--fresh] [--session id] [--force] [-- <flags>]   # restart in place (default RESUME; --fresh sheds)
fleet recycle --all|--conductors|--children|--my-children [--include-muted]  # bulk restart, sequential + gated
fleet sessions <label>                      # list resumable prior sessions (id, age, size, snippet)
fleet archive <label>   / fleet revive <label> [--fresh] [--session id]     # park / bring back
fleet rm <label> [--detach] [--force] [--with-group] [--wip-commit]   # close + archive (default); --detach drops row only
fleet mute <label> / unmute <label>

# views (read-only, no LLM)
fleet vitals [--json] [--paint]   fleet find <query> [--turns N]   fleet graph [--html]
fleet serve [--port N]            fleet paint

# worktrees (config-gated) + multi-build
fleet worktree ls / clean <label> [--wip-commit]
eval "$(/path/to/<build>/bin/fleet profile <name> --init)"   # pin a build (see docs/profiles.md)

# comms
fleet peer-msg <to-label> "<msg>" [--reply-to <id>] [--no-reply]
fleet broadcast "<msg>" [--target all|all-conductors|all-children|my-children]
fleet inbox-ack <seq> [--peer]
```

## The router daemon

One router serves every conductor on the machine. Run it as a managed daemon:

```
fleet daemon start                 # detached router; survives shell exit + conductor recycle
fleet daemon start --foreground    # supervised router in THIS process (for launchd; see below)
fleet daemon start --heartbeat     # also nudge live-idle conductors with pending work (every 540s)
fleet daemon status                # running? which BUILD owns it (version/python/package), state, uptime, bus seq
fleet daemon stop
fleet daemon restart               # preserves the running --heartbeat setting unless overridden
```

`start` double-forks with `setsid` so the router is fully detached (own session,
no controlling terminal, its own process group) and reparented to init. That is
what lets it survive the starting shell exiting, an agent's Bash-tool
process-group cleanup, and a conductor self-recycle (a bare `nohup &` router does
not: it dies with the pane's process group). It writes `<state>/router.pid` and
`<state>/router.daemon.json`, logs to `<state>/router.log`, refuses to start if
one is already running, and cleans a stale pidfile (dead pid). The daemon is
per-state, so under a profile it manages that profile's router (see
`docs/profiles.md`).

`--heartbeat [SECS]` adds a periodic tick (default 540s) that re-nudges only
LIVE-IDLE conductors that have a pending inbox, through the same input-safe
`wake_if_idle` gate (never a busy surface, never a human draft); muted agents and
non-conductors are skipped. There is no dead-session detection or auto-recycle.
`restart` preserves the running `--heartbeat` setting unless you pass a new one.

`status` prints the daemon (supervisor) pid AND **which build owns it** — the app
`version`, the `python` that runs it, and the `cmux_fleet` package dir (recorded
in `router.daemon.json` at start) — plus the state dir it routes, uptime, the
last bus seq, and the log path (`<state>/router.log`). The build identity is what
a migration cutover/rollback checks to prove the daemon is running the intended
install (see the runbook below). The pidfile holds the supervisor; the supervisor
runs one `python -m cmux_fleet.router --live` child.

**Never run the router by hand from a session.** `fleet daemon` is the only
supported way to run it. A `nohup &` router started from inside an agent's Bash
tool either dies with the tool's process group, or silently survives as a stray
duplicate on the same bus so every event is processed twice (this was a real
bug during the cutover). Verify exactly one router exists (the daemon's child):

```
ps aux | grep 'cmux_fleet.router --live'   # expect exactly one line
```

If you see more than one, stray routers are double-processing the bus: stop the
daemon, kill the strays, then `fleet daemon start`.

Under the hood the daemon's `router.py --live` writes to the inbox, fires `cmux
notify` banners, and (in `auto` mode) wakes idle conductors, tailing the cmux
agent bus with a replay cursor (`router.seq`) so a restart resumes where it left
off. To observe without acting, run it directly: `python -m cmux_fleet.router`
(no `--live`) logs what it would do and changes nothing.

## The mode dial

The dial is the file `$CMUX_STATE_DIR/notify-mode`. Read or set it directly:

```
cat "$CMUX_STATE_DIR/notify-mode"            # current mode ('auto' when absent)
echo passive > "$CMUX_STATE_DIR/notify-mode" # the one mute
echo auto    > "$CMUX_STATE_DIR/notify-mode" # explicit wake-now (same as absent)
```

- **auto** (the default when the file is absent or empty): the router idle-wakes a
  conductor sitting idle at an empty prompt to handle a completion/peer now, and the
  Stop hook auto-continues it to drain pending work at a turn's end.
- **passive**: the single mute — suppresses idle-wake AND auto-drain fleet-wide;
  pending work waits in the inbox and surfaces via context on the next turn.

The retired `autodrain` value normalizes to `auto`. Peer messages drain at Stop in
every mode; `peer-msg --no-wake` is the per-message opt-out for acks/FYIs.

## Draft-through — waking through a human draft

When an idle conductor has a **human draft** in its input box, the wake gate applies
the `draft-through` policy (`$CMUX_STATE_DIR/draft-through`):

```
cat  "$CMUX_STATE_DIR/draft-through"                 # current policy ('stale' when absent)
echo clobber  > "$CMUX_STATE_DIR/draft-through"      # clobber ANY draft immediately (no wait)
echo preserve > "$CMUX_STATE_DIR/draft-through"      # never clobber (a walked-away draft just waits)
```

- **stale** (the default): the **stale-draft gate**. A *walked-away* draft — one left
  **unchanged for ≥ 90s** — is clobbered (input cleared via `send-key ctrl+u`, wake
  injected, `draft_clobbered` logged) so it can't silence the conductor indefinitely;
  a **fresh** draft (active typing) is preserved. Meets "never an indefinite silent
  stall" while protecting a human mid-thought.
- **clobber**: aggressive — clobber-with-log on *any* draft, no stale wait.
- **preserve**: conservative — never clobber; a walked-away draft waits in the inbox
  until the human returns.

The input-clear (`ctrl+u`) is **best-effort** (it degrades to a mashed submit, never a
silent stall) and still wants a live-TUI prototype for multi-line / pasted-image
drafts; the elegant save/clear/wake/**restore** path (preserve the draft across the
wake) is the planned follow-up.

## Launching agents

```
fleet launch <role>                          # a roster role from the toml
fleet launch worker --place pane             # override placement for this launch
fleet launch lead --place workspace --group lead
fleet launch --adhoc scratch --tool claude -- --model opus --effort high
```

Anything after `--` is forwarded verbatim to the agent tool; the launcher never
needs to know the flag. Placement is `tab`, `pane`, or `workspace`. A
`place = workspace` conductor anchors its own workspace-group: if the group does
not exist it is auto-created on the conductor's own workspace (no manual
`workspace-group create`), and a conductor with no explicit `group` defaults it
to its label. A `place = workspace` child joins its parent conductor's group. An
`--adhoc` agent is off-roster, gets a cwd under `adhoc_subdir`, and (if a floor
`CLAUDE.md` is configured) inherits it via a symlink. Add `--dry-run` to resolve
and print the launch command without spawning.

Check what a role would launch with, base settings plus what fleet stacks on
top, without launching:

```
fleet config <role>
fleet config --adhoc scratch --cwd /some/dir
```

## Worktrees (code repos)

Config-gated and **default-off.** For a role whose `cwd` is a git repo, set
`worktree = true` and each agent runs in its own git worktree at
`<repo>/.worktrees/<label>` on branch `fleet/<label>`, instead of sharing the
repo's working tree. This keeps parallel code agents off each other's `git
status`, branches, and build artifacts. The vault (one big doc repo) leaves it
off; code repos opt in.

```
fleet launch coder                 # worktree=true role -> launches in its own worktree
fleet launch coder --worktree feat # ad-hoc: name the branch (else fleet/<label>)
fleet launch coder --worktree-base release/2.0
fleet launch coder --no-worktree   # force-disable for a worktree=true role
fleet worktree ls                  # branch / state (clean|dirty|GONE) / path per agent
fleet archive coder                # park it (keeps the registry row + worktree record)
fleet worktree clean coder         # then tear the tree down (refuse-if-dirty; --wip-commit to override)
```

**One owner: the fleet.** It runs `git worktree add` itself and launches the
tool *into* that directory (`claude` plain, no `-w`; codex via the `cd`). It does
**not** use Claude's own `-w`/`--worktree` (those are stripped from passthrough
when `worktree=true`) and does not hook `WorktreeCreate`/`WorktreeRemove`,
running two owners on one tree causes double-cleanup, lock races, and branch
collisions.

Lifecycle: `launch` creates the tree (idempotent, locked, pruned first);
`recycle`/`revive` reuse it (the cwd is replayed, the tree persists); `archive`
keeps it; `rm --kill` tears it down. Teardown **refuses if the tree is dirty**
(commit/stash first, or pass `--wip-commit` to snapshot as `fleet WIP: <label>`)
and **always keeps the branch**: nothing here merges or deletes a branch. After
a worktree launch, fleet reconciles the surface's actual cwd against the intended
worktree path and fails loud (with cleanup + rerun commands) if the workspace
collapsed into an existing surface.

**Reclaiming a worktree:** `fleet worktree clean <label>` needs the agent's
registry row and refuses while the agent is still live, so it works on an
**archived** entry (archive keeps the row and the worktree record) or a stale one
whose surface is gone. The supported path is `fleet archive <label>` then `fleet
worktree clean <label>`. `rm --kill` is the other route: it drops the agent and,
in the same step, tears down its (clean) worktree. Note that `rm --kill`,
`rm --with-group`, and a placement-mismatch cleanup all **delete** the registry
row, so after them `fleet worktree clean` can no longer find the tree: reclaim it
manually with `git worktree remove <path>` (and `git branch -D fleet/<label>`).

## Inspecting the fleet

```
fleet ls
```

Reconciles the live registry against cmux's hook store. It flags `STALE` (the
registry says live but the surface has no live session, e.g. a closed tab or a
crash) and `pending` (launched, awaiting its first turn to bind a session, which
codex does lazily). It also lists archived, revivable agents.

## Fleet views

Read-only, derived from live state on every call (registry, hook stores,
transcripts). No daemon, no stored status. Status is inferred without an LLM
(cmux's `agentLifecycle` is authoritative, refined by keyword tables).

```
fleet vitals [--json] [--paint]      triage table, most-urgent first
                                     (error/needs-input/review/working/done/idle),
                                     each with context-remaining % (! flags <=30% left)
fleet find <query> [--turns N]       find an agent by label/role/cwd, or by what it
                                     said in the last N transcript turns
fleet graph [--html] [--out FILE]    the parentage tree (text, or a self-contained page)
fleet serve [--port N]               localhost view: GET / -> graph HTML, /vitals.json -> rows
fleet paint                          push status pill + context bar onto cmux's sidebar
```

`vitals` context-remaining % assumes a window guessed from the model; set
`CMUX_FLEET_CONTEXT_WINDOW` (or `[fleet].context_window`) for an exact number (the
ranking holds either way). `find` matches a label/role/cwd first, then scans
transcripts. For a dedicated board, install `sidebars/fleet.swift` into
`~/.config/cmux/sidebars/` and `cmux sidebar open fleet`; it reads what `paint`
writes.

## Recycling, archiving, reviving

- **recycle** restarts an agent in place on its own surface, same identity, via
  cmux's native `respawn-pane`. Default is **RESUME** (it preserves context — the
  least-disruptive default, ratified 2026-07-01); **`--fresh`** sheds context into
  a new session and auto-primes from the latest `handover/*.md`. `--session <id>`
  resumes an arbitrary prior session (`fleet sessions <label>` lists them);
  `--resume` is a no-op alias. It runs detached (so it can recycle the caller
  itself) behind a quiet-gate that waits for an idle prompt with an empty draft
  before respawning, never half-killing a mid-turn agent. `--force` skips the
  draft guard. Bulk selectors restart many sequentially + gated, skipping self and
  muted agents: `--all` / `--conductors` / `--children` / `--my-children`
  (`--include-muted` to force).

  ```
  fleet recycle                 # recycle self, RESUME (preserve context) — the default
  fleet recycle --fresh         # shed context + prime from the handover (the handover pattern)
  fleet recycle worker --session <id>          # resume an arbitrary prior session
  fleet recycle worker --fresh --add-plugin some-plugin -- --model opus
  fleet recycle --children      # restart my live children, sequential + gated (RESUME each)
  ```

- **archive** parks a live agent: it stops the process (SIGINT twice for a clean
  TUI exit), closes the tab, and moves the entry to the archive shelf with
  enough to resume it later (it captures cmux's ground-truth launch binding
  before the surface is torn down).

  ```
  fleet archive worker
  ```

- **revive** brings a parked agent back into a fresh surface, resuming its last
  session by default. It replays the captured launch binding (with `--resume`
  swapped in), falling back to the registry spec for older entries. `--fresh`
  sheds into a new session (auto-primed from the handover); `--session <id>`
  resumes an arbitrary prior session.

  ```
  fleet revive worker
  fleet revive worker --place pane -- --effort high
  fleet revive worker --session <id>     # resume a specific prior session
  ```

- **sessions** lists the resumable prior sessions for an agent's surface (id, age,
  size, first-user-message snippet, freshest first; `*` marks the currently-bound
  one) — pick an id for `recycle --session` / `revive --session`.

  ```
  fleet sessions worker
  ```

For a roster role, both recycle and revive are toml-authoritative: they
re-resolve the current roster, so they pick up floor or role changes made since
the agent launched.

- **rm** retires a label: by default it writes a recovery row to the archive,
  stops the process, and closes the surface — so a removed label can never leave
  a zombie surface running (revive it later with `fleet revive`). A surface that
  is mid-turn ("running") is refused; `--force` closes it anyway. `--detach` is
  the explicit opt-in for the old soft behavior: drop the registry row only and
  leave the surface running (for handing a pane to a human to drive directly).
  Note detach ≠ mute — a muted child stays tracked, a detached label is fully
  untracked. `--kill` remains as an alias for the default; the one thing it
  still adds is worktree teardown for a worktree-isolated agent
  (refuse-if-dirty; `--wip-commit` to snapshot). `--with-group` dissolves the
  agent's workspace-group by ref (closing every member surface) and sweeps all of
  that group's members out of the registry, so no stale rows linger. A swept
  member's worktree dir and branch are left unmanaged: because the registry rows
  are gone, `fleet worktree clean` cannot find them, so reclaim them manually with
  `git worktree list` / `git worktree remove <path>` (and `git branch -D
  fleet/<label>` if you want the branch). Without it, only this agent's own
  workspace goes and any remaining members are left ungrouped.

  ```
  fleet rm scratch
  fleet rm scratch --kill
  fleet rm sandbox-conductor --with-group   # tear down a whole conductor group
  ```

## Muting a child

When you are driving a child directly (you are in the loop), mute it so the
router stops pushing its completions to its parent. The parent then reads it on
demand (`fleet ls` shows it `MUTED`; use `fleet child-digest` for the content).

```
fleet mute worker
fleet unmute worker
```

## Peer messaging

Send a deliberate message to a peer conductor. Delivery is input-safe (the same
path as completions): the peer sees it in context, and an idle peer is woken to
handle it now unless you pass `--no-wake`.

```
fleet peer-msg <to-label> "your message"
fleet peer-msg <to-label> "ack, on it" --reply-to <msg_id>
fleet peer-msg <to-label> "fyi only" --no-reply
```

A fresh message expects a reply by default; `--no-reply` marks it
informational; `--reply-to` makes the message a reply.

Broadcast an out-of-band heads-up (a toml edit, a plugin bump) to live agents
over the same input-safe path. It never restarts anything; each recipient
decides what to do (often `fleet recycle` to pick up the change).

```
fleet broadcast "roster updated, recycle to pick it up"
fleet broadcast "heads up" --target all-children --no-wake
```

Targets: `all`, `all-conductors` (the default), `all-children`, `my-children`.

## Acking the inbox

After handling what the awareness or drain hook surfaced, ack it so it stops
re-surfacing. The hooks print the exact command, including the seq:

```
fleet inbox-ack <seq>           # ack completions
fleet inbox-ack <seq> --peer    # ack peer messages
```

Acking an exact seq is race-safe: a message that arrived later has a higher seq
and survives the ack.

## Profiles and multi-build

A build is a `fleet` app + plugin — usually a checkout directory (the side-by-side
dev model), or the single installed uv-tool app. A profile pins every entrypoint at
one build so two builds run side by side with separate config, state, and daemons.
Activate one in a shell, then everything that shell launches is pinned to that build:

```
eval "$(/path/to/<build>/bin/fleet profile dev --init)"   # a checkout build (or bare `fleet profile ...` for the installed app)
cp profiles/test.fleet.toml "$CMUX_FLEET_TOML"             # a starting roster
fleet daemon start                                        # this profile's own router
fleet launch sandbox-conductor                            # auto-anchors its own group
```

`--init` creates the state dir and seeds the roster from the bundled
`fleet.toml.example`. The `PATH` pin is THIS build's `fleet` dir (a checkout's
`bin/` or the installed console-script dir); `CMUX_FLEET_MARKETPLACE` is emitted
only for a real **checkout** — a bare installed app **omits** it, so set it (or
`[fleet].marketplace`) explicitly if a roster uses `plugins = [...]`. `fleet
daemon` is per-state, so started inside the activated shell it manages that
profile's router against that profile's `CMUX_STATE_DIR`, separate from prod's
daemon. Full model and the Nth-build workflow are in `docs/profiles.md`.

### Gotcha: a recycled agent bakes `CMUX_STATE_DIR` into its env

Config precedence is **env > `[fleet]` toml > default**, and a recycled or revived
agent carries the `CMUX_STATE_DIR` (and the other `CMUX_*` paths) that were in its
launch command's env, re-injected on every respawn. So changing `state_dir` in the
toml alone does **not** move an already-running agent: its baked env var shadows
the toml value silently. To relocate state to a new directory, recycle the agents
with an explicit override, e.g. `fleet recycle <label> -- ...` from a shell whose
`CMUX_STATE_DIR` points at the new location, or unset the baked var so the
toml/default applies. A future `fleet migrate` would rebake the env as part of the
move (not built yet).

## Stranger first run

cmux-fleet runs with no prior setup. With no env vars, no `fleet.toml`, and cmux
on `$PATH`:

- state is created fresh under `$XDG_STATE_HOME/cmux-fleet`;
- there is no roster, so use `fleet launch --adhoc <name>` to spawn agents;
- the marketplace and floor `CLAUDE.md` are disabled, so no vault is assumed;
- the mode dial defaults to `passive`.

To exercise an isolated run, point `CMUX_STATE_DIR` at a throwaway directory:

```
CMUX_STATE_DIR=$(mktemp -d) python -m cmux_fleet.router
```

Layer your own setup back in by setting the env vars or filling in the `[fleet]`
block and roster in your `fleet.toml`.

## Reboot persistence (launchd)

The `fleet daemon start` detached daemon survives a shell exit, a Bash-tool
process-group cleanup, and a conductor recycle — but **not a machine reboot**. To
start the router at login, run it under launchd in **foreground** mode
(`fleet daemon start --foreground`): no fork/detach, so launchd's `KeepAlive`
supervises the process directly and captures its stdout/stderr.

Write `~/Library/LaunchAgents/io.cmux.fleet.plist` (adjust the absolute
`fleet` path — `command -v fleet` — and any `CMUX_*` env for the profile this
daemon serves):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>io.cmux.fleet</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/.local/bin/fleet</string>
    <string>daemon</string>
    <string>start</string>
    <string>--foreground</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- pin the SAME state/config this daemon should route (a profile's paths, or omit for defaults) -->
    <key>CMUX_STATE_DIR</key>  <string>/Users/you/.local/state/cmux-fleet</string>
    <key>PATH</key>           <string>/Users/you/.local/bin:/usr/bin:/bin</string>
  </dict>
  <key>KeepAlive</key>        <true/>
  <key>RunAtLoad</key>        <true/>
  <key>StandardOutPath</key>  <string>/Users/you/.local/state/cmux-fleet/launchd.out.log</string>
  <key>StandardErrorPath</key><string>/Users/you/.local/state/cmux-fleet/launchd.err.log</string>
</dict>
</plist>
```

Load / reload / verify (modern `launchctl`; `gui/$(id -u)` is your login domain):

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.cmux.fleet.plist   # load
launchctl kickstart -k gui/$(id -u)/io.cmux.fleet                             # (re)start now
fleet daemon status                                                          # confirm it owns the bus
launchctl bootout gui/$(id -u)/io.cmux.fleet                                  # unload
```

`fleet daemon stop` will stop the router, but under `KeepAlive` launchd restarts
it — so `bootout` (or set the job disabled) is the way to actually take it down.
One launchd job per state dir: a second profile's daemon is a second plist with
its own `Label` and `CMUX_STATE_DIR`.

## Upstream risk register (design around these)

cmux's session persistence and agent-status signals are not fully trustworthy at
the versions the fleet runs on, and the resume / parked-context strategy leans on
exactly those. Treat them as not-yet-reliable and design around them —
**handover-to-disk is the backstop, not an optional nicety.** The risks that
touch the design directly:

- **Hibernation can overwrite a live transcript with a stub.** Background
  hibernation has been observed replacing a running session's JSONL with a stub,
  after which `claude --resume` returns "No conversation found" — permanent
  context loss. Because resume depends on transcript integrity, a written
  handover is the mandatory fallback here.
- **Running / needs-input indicators are flaky.** The status signal the fleet
  routes on is unreliable upstream (a long-standing, widely-hit bug). cmux-fleet
  mitigates by reading the hook store's `agentLifecycle` plus the debounced
  `Stop` trigger rather than the sidebar pill — but the underlying machinery is
  the same, so a completion is confirmed from the transcript, never from a pill.
- **Sessions can be lost on restart, relaunch, auto-update, or the restore
  shortcut.** Any of these can drop a workspace or session with no recovery. Do
  not assume cmux's own restore brings the fleet back up; the launcher plus
  handover-to-disk is the reliable path until declarative session-restore is
  wired.

## Migration runbook: app/plugin cutover

**Document, verify, decide — do not fire blind.** Moving a live fleet onto a new
build is not "repoint a symlink and restart the daemon." Running agents keep the
PATH, `--plugin-dir`, and `CMUX_*` env they were launched with; repointing a
symlink does **not** mutate an already-running process. So a cutover has to reason
about what each *live* agent actually resolves, not just what the shell resolves.

### Why "no conductor recycle" is not free

- `fleet profile` deliberately prepends a build-specific `bin/` to `PATH`, and a
  launch injects `CMUX_*` but **not** `PATH` — so a launched agent keeps its
  start-time `PATH`. The `fleet` a live conductor's hook shim finds is whatever
  was first on that baked `PATH`, not necessarily the one you just installed.
- `--plugin-dir` is baked per launch; a marketplace/symlink repoint does not
  change a running agent's loaded plugin.
- Therefore a live conductor can keep calling an *old* `fleet` for its hook verbs
  even after you upgrade the app and the plugin. The daemon can be new while hook
  behavior is old.

Phase 3 makes this tractable: the plugin hooks are now **thin shims that resolve
`fleet` at call time** (`$CMUX_FLEET_BIN` → `which fleet`), so a repointable app
path CAN update a running agent — but only if that agent's shim actually resolves
the path you're swapping. That is what you verify before trusting it.

### Preconditions (gate the cutover)

1. **Phase 3 thin shims are in the installed plugin.** `scripts/hooks/{awareness,
   drain}.py` must be the shims (they shell into `fleet hook-awareness` /
   `fleet hook-drain`), not the old inline-logic files. If a live agent still
   loads inline-logic hooks, it imports checkout code regardless of the app swap —
   recycle is mandatory for those.
2. **A staging run has proven an already-running conductor uses the installed app
   for hook verbs** (see the verification step). Until it has, **do not advertise
   "no conductor recycle."**

### Inventory the live fleet (before touching anything)

For every live agent, record what it actually resolves — from its launch command
and cmux's per-agent hook store:

```
fleet ls                                   # the live roster + sessions
fleet daemon status                        # CURRENT daemon: pid + version/python/package (the old build)
# for each conductor surface, capture the PATH / CMUX_FLEET_BIN / plugin-dir it was launched with:
#   - its launch command env (CMUX_* + PATH assumptions) from the hook store / registry entry
#   - the `fleet` a live shim resolves:  the app path the conductor's hook shim would pick
```

Write down, per agent: baked `PATH`, `CMUX_FLEET_BIN` (if set), `--plugin-dir`,
and `CMUX_STATE_DIR`. These decide whether a symlink/app swap reaches it.

### Choose ONE cutover strategy (explicit decision)

- **A — repointable app path (no recycle, IF verified).** Make every hook shim
  resolve an absolute, atomically-swappable app path: set `CMUX_FLEET_BIN` (or put
  a stable shim path first on the agents' `PATH`) to a symlink you flip
  atomically (`ln -sfn <new> <link>`). Requires that the *live* agents already
  carry that `CMUX_FLEET_BIN` / stable-path assumption — verify per agent.
- **B — recycle after cutover (always correct).** Upgrade the app + plugin, then
  `fleet recycle <label>` each conductor so it relaunches with the new PATH /
  plugin-dir / env. The safe default when the inventory shows agents with baked
  build-specific paths that a swap won't reach.

### Cutover steps

1. Install/upgrade the **app**: `uv tool install --force <local-wheel>` (built from
   the release commit — the reliable local path) or `uv tool install --force
   "git+…@vX.Y.Z"`. Confirm `command -v fleet` resolves the new build, then
   `fleet daemon status` reports its `version`/`python`/`package`. **There is no
   `fleet --version` verb** — use `daemon status` for build identity. If
   `~/.local/bin/fleet` is currently a symlink to the checkout, `--force` overwrites
   it with the installed console script (that swap IS the cutover for anything that
   resolves `fleet` via `~/.local/bin`).
2. Install/upgrade the **plugin** separately (the app installer never touches it).
   Confirm the installed hooks are the Phase 3 shims. On a machine whose plugin is a
   marketplace symlink to the repo, this happens by merging the packaged branch to
   the checked-out branch (the symlink then serves the thin plugin).
3. Move the daemon onto the new build:
   - **launchd via a PATH-resolving boot script (this machine's model):** the
     LaunchAgent runs `daemon-boot.sh`, which does `fleet daemon start` off `PATH`
     (with `~/.local/bin` on it). So the app swap in step 1 is picked up
     automatically — **no plist edit needed.** Just stop the old daemon
     (`fleet daemon stop`) and start the installed one (`~/.local/bin/fleet daemon
     start`, or let the launchd interval heal it).
   - **launchd with a baked `fleet` path (foreground/KeepAlive model):** `launchctl
     bootout gui/$(id -u)/<label>` → edit the plist's `fleet` path if it changed →
     `launchctl bootstrap …` → `launchctl kickstart -k …`. Don't also `fleet daemon
     start` by hand — that races `KeepAlive`.
   - **unmanaged daemon:** `fleet daemon stop`, confirm no `cmux_fleet.router
     --live` (or checkout `router.py --live`) remains except the expected one, then
     `fleet daemon start`. **Watch (F4):** `daemon stop` has been observed to
     misreport "not running" once while the daemon was alive (non-reproducible);
     always confirm with `ps` / `fleet daemon status` after stop, before starting the
     new build, so you don't leave a stray router double-processing the bus.
   - Record **current vs new** daemon: pid + `version`/`python`/`package` from
     `fleet daemon status` before and after — proof the new build owns the bus.
   - **`profile --init` caveats (F2/F3):** `fleet profile <name> --init` without
     `--base` writes to the XDG-default state `~/.local/state/cmux-fleet-<name>` (with
     `name=staging` that IS the live checkout-staging state — use `--base <dir>` or a
     distinct name to avoid a collision); and its emitted `marketplace` pin inherits
     whatever `fleet.toml` `config.py` resolves (prod's, if `CMUX_FLEET_TOML` is
     unset), so set `CMUX_FLEET_TOML` first for a true wheel-only pin.
4. Apply the chosen agent strategy (A: flip the `CMUX_FLEET_BIN` symlink; B:
   `fleet recycle` each conductor).

### Verify (this is the gate for "no recycle")

On an **already-running** conductor (not a fresh one), confirm its hook path now
reaches the new app: check that the `fleet` its shim resolves is the new build
(inspect its `CMUX_FLEET_BIN` / `PATH`), and that a hook-driven action (a peer
message or completion surfaced into its context) is served by the new
`fleet hook-*`. Only after a staging conductor passes this may strategy A be
called "no recycle." If it fails, that agent needs strategy B (recycle).

### Rollback

- **App:** repoint to the previous build — `uv tool install --force …@<old-tag>`
  or flip the `CMUX_FLEET_BIN` symlink back. **Shell hash caveat:** an interactive
  shell caches command paths; after moving `fleet` on `PATH`, run `hash -r` (bash)
  / `rehash` (zsh) or the shell keeps calling the old path. A *running* agent
  process does not re-hash — that's exactly why the inventory + recycle decision
  matters.
- **Daemon:** launchd — `bootout` the new, restore the old plist, `bootstrap` +
  `kickstart`. Unmanaged — `fleet daemon stop`, start the old build's `fleet
  daemon`. Confirm the router cursor (`router.seq`) is still compatible before
  downgrading; verify pid + build via `fleet daemon status`.
- **Plugin:** reinstall the prior plugin version; recycle any conductor that must
  load it now (a running agent keeps its baked `--plugin-dir`).

Until a staging run proves strategy A end-to-end, treat conductor **recycle**
(strategy B) as the default for a production cutover.
