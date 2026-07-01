# Operations

Day-to-day running of a cmux fleet. All commands assume `fleet` is on your
`PATH` (via `bin/fleet`) and you are issuing them from inside a conductor's cmux
surface, so `$CMUX_SURFACE_ID` is set.

## Cheat sheet

```
# daemon
fleet daemon start [--heartbeat]            # detached router (survives shell exit + recycle)
fleet daemon status | stop | restart
echo auto > "$CMUX_STATE_DIR/notify-mode"   # passive | autodrain | auto

# spawn + drive
fleet launch <role> [--place tab|pane|workspace] [--dry-run] [-- <tool flags>]
fleet launch --adhoc <name> --tool claude -- --model opus
fleet drive-child <surface> "<prompt>"
fleet child-digest <session-frag> 5

# inventory + lifecycle
fleet ls                                    # live x hook store; flags STALE / pending / MUTED
fleet recycle [label] [--resume] [--force] [-- <flags>]   # restart in place, same identity
fleet archive <label>          / fleet revive <label>     # park / bring back
fleet rm <label> [--kill] [--with-group] [--wip-commit]   # drop; optionally close + dissolve group
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
fleet daemon start --heartbeat     # also nudge live-idle conductors with pending work (every 540s)
fleet daemon status                # running? which state dir, uptime, bus seq
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

`status` prints the daemon (supervisor) pid, the state dir it routes, uptime,
the last bus seq, and the log path (`<state>/router.log`). The pidfile holds the
supervisor; the supervisor runs one `router.py --live` child.

**Never run `router.py` by hand from a session.** `fleet daemon` is the only
supported way to run it. A `nohup &` router started from inside an agent's Bash
tool either dies with the tool's process group, or silently survives as a stray
duplicate on the same bus so every event is processed twice (this was a real
bug during the cutover). Verify exactly one router exists (the daemon's child):

```
ps aux | grep 'router.py --live'   # expect exactly one line
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
cat "$CMUX_STATE_DIR/notify-mode"            # current mode
echo passive   > "$CMUX_STATE_DIR/notify-mode"
echo autodrain > "$CMUX_STATE_DIR/notify-mode"
echo auto      > "$CMUX_STATE_DIR/notify-mode"
```

- **passive** (the default when the file is absent or empty): pending work waits
  in the inbox and surfaces via context on the conductor's next turn.
- **autodrain**: the Stop hook auto-continues the conductor to process pending
  child completions. Peer messages drain at Stop in every mode.
- **auto**: autodrain plus router idle-wake of a conductor that is sitting idle
  at an empty prompt.

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
  cmux's native `respawn-pane`. Default is a fresh session (it sheds context and
  auto-primes from the latest `handover/*.md`); `--resume` continues the
  session. It runs detached (so it can recycle the caller itself) behind a
  quiet-gate that waits for an idle prompt with an empty draft before respawning,
  never half-killing a mid-turn agent. `--force` skips the draft guard.

  ```
  fleet recycle                 # recycle self, fresh session
  fleet recycle worker --resume
  fleet recycle worker --add-plugin some-plugin -- --model opus
  ```

- **archive** parks a live agent: it stops the process (SIGINT twice for a clean
  TUI exit), closes the tab, and moves the entry to the archive shelf with
  enough to resume it later (it captures cmux's ground-truth launch binding
  before the surface is torn down).

  ```
  fleet archive worker
  ```

- **revive** brings a parked agent back into a fresh surface, resuming its last
  session. It replays the captured launch binding (with `--resume` swapped in),
  falling back to the registry spec for older entries.

  ```
  fleet revive worker
  fleet revive worker --place pane -- --effort high
  ```

For a roster role, both recycle and revive are toml-authoritative: they
re-resolve the current roster, so they pick up floor or role changes made since
the agent launched.

- **rm** drops a label from the live and archive stores. `--kill` also stops the
  process and closes its tab (for a throwaway). `--with-group` dissolves the
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

A build is a checkout directory; a profile pins every entrypoint at one build so
two builds run side by side with separate config, state, and daemons. Activate
one in a shell, then everything that shell launches is pinned to that build:

```
eval "$(/path/to/<build>/bin/fleet profile dev --init)"   # PATH + all CMUX_* knobs
cp profiles/test.fleet.toml "$CMUX_FLEET_TOML"             # a starting roster
fleet daemon start                                        # this profile's own router
fleet launch sandbox-conductor                            # auto-anchors its own group
```

`--init` creates the state dir and seeds the roster from `fleet.toml.example`.
`fleet daemon` is per-state, so started inside the activated shell it manages
that profile's router against that profile's `CMUX_STATE_DIR`, separate from
prod's daemon. Full model and the Nth-build workflow are in `docs/profiles.md`.

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
