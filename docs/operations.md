# Operations

Day-to-day running of a cmux fleet. All commands assume `fleet` is on your
`PATH` (via `bin/fleet`) and you are issuing them from inside a conductor's cmux
surface, so `$CMUX_SURFACE_ID` is set.

## Start the router

One router serves every conductor on the machine. Start it once:

```
python3 scripts/router.py --live
```

`--live` writes to the inbox, fires `cmux notify` banners, and (in `auto` mode)
wakes idle conductors. Omit `--live` to observe: it logs what it would do and
changes nothing, which is the way to confirm wiring before going live. The
router prints its mode, the notify-mode, and the resolved state dir on startup,
then logs each registry reload and each delivery.

It tails the cmux agent bus with a replay cursor (`router.seq`), so a restart
resumes where it left off rather than replaying old events.

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
needs to know the flag. Placement is `tab`, `pane`, or `workspace` (a workspace
needs a `--group`). An `--adhoc` agent is off-roster, gets a cwd under
`adhoc_subdir`, and (if a floor `CLAUDE.md` is configured) inherits it via a
symlink. Add `--dry-run` to resolve and print the launch command without
spawning.

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
fleet worktree clean <label>       # tear down (refuse-if-dirty; --wip-commit to override)
```

**One owner: the fleet.** It runs `git worktree add` itself and launches the
tool *into* that directory (`claude` plain, no `-w`; codex via the `cd`). It does
**not** use Claude's own `-w`/`--worktree` (those are stripped from passthrough
when `worktree=true`) and does not hook `WorktreeCreate`/`WorktreeRemove` —
running two owners on one tree causes double-cleanup, lock races, and branch
collisions.

Lifecycle: `launch` creates the tree (idempotent, locked, pruned first);
`recycle`/`revive` reuse it (the cwd is replayed, the tree persists); `archive`
keeps it; `rm --kill` tears it down. Teardown **refuses if the tree is dirty**
(commit/stash first, or pass `--wip-commit` to snapshot as `fleet WIP: <label>`)
and **always keeps the branch** — nothing here merges or deletes a branch. After
a worktree launch, fleet reconciles the surface's actual cwd against the intended
worktree path and fails loud (with cleanup + rerun commands) if the workspace
collapsed into an existing surface.

## Inspecting the fleet

```
fleet ls
```

Reconciles the live registry against cmux's hook store. It flags `STALE` (the
registry says live but the surface has no live session, e.g. a closed tab or a
crash) and `pending` (launched, awaiting its first turn to bind a session, which
codex does lazily). It also lists archived, revivable agents.

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
  process and closes its tab (for a throwaway).

  ```
  fleet rm scratch
  fleet rm scratch --kill
  ```

## Muting a child

When you are driving a child directly (you are in the loop), mute it so the
router stops pushing its completions to its parent. The parent then reads it on
demand (`fleet ls` shows it `MUTED`; use `child-digest.py` for the content).

```
fleet mute worker
fleet unmute worker
```

## Peer messaging

Send a deliberate message to a peer conductor. Delivery is input-safe (the same
path as completions): the peer sees it in context, and an idle peer is woken to
handle it now unless you pass `--no-wake`.

```
python3 scripts/peer-msg.py <to-label> "your message"
python3 scripts/peer-msg.py <to-label> "ack, on it" --reply-to <msg_id>
python3 scripts/peer-msg.py <to-label> "fyi only" --no-reply
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
python3 scripts/inbox-ack.py <seq>           # ack completions
python3 scripts/inbox-ack.py <seq> --peer    # ack peer messages
```

Acking an exact seq is race-safe: a message that arrived later has a higher seq
and survives the ack.

## Stranger first run

cmux-fleet runs with no prior setup. With no env vars, no `fleet.toml`, and cmux
on `$PATH`:

- state is created fresh under `$XDG_STATE_HOME/cmux-fleet`;
- there is no roster, so use `fleet launch --adhoc <name>` to spawn agents;
- the marketplace and floor `CLAUDE.md` are disabled, so no vault is assumed;
- the mode dial defaults to `passive`.

To exercise an isolated run, point `CMUX_STATE_DIR` at a throwaway directory:

```
CMUX_STATE_DIR=$(mktemp -d) python3 scripts/router.py
```

Layer your own setup back in by setting the env vars or filling in the `[fleet]`
block and roster in your `fleet.toml`.
