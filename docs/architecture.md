# Architecture

The spine as-built, by file:

- `scripts/config.py` resolves every path/setting (env > `[fleet]` toml > XDG default).
- `scripts/fleet_state.py` owns the state model: the label-keyed registry, the unified inbox, the archive shelf, the hook-store union, the idle-wake gate.
- `scripts/router.py` is the bus daemon: child `Stop` -> deliver a completion to the parent. One process serves every conductor.
- `scripts/hooks/awareness.py` + `scripts/hooks/drain.py` surface the inbox into a conductor's context (never its input box).
- `scripts/fleet.py` is the CLI: launch, the lifecycle verbs, peer messaging, broadcast, worktrees, profiles, and the read-only views (`scripts/fleet_features.py`).
- `scripts/peer-msg.py`, `scripts/child-digest.py`, `scripts/drive-child.py`, `scripts/inbox-ack.py` are the agent-facing helpers.

## The division of labor

cmux already provides the hard parts of a multi-agent runtime: it owns agent
**sessions**, their **lifecycle** (running / idle / needsInput), their
**transcripts**, and an **event bus**. cmux-fleet does not reimplement any of
that. It adds the orchestration delta a conductor needs on top:

- an **org chart**: who is whose child, what role and label each agent carries;
- a **unified inbox** for child completions and peer messages, with per-surface
  ack cursors;
- **input-safe surfacing**: pending work reaches an agent through its context,
  not by typing into its input box.

## Three sources, three jobs

The router and hooks treat cmux's facilities as three distinct sources:

- **Bus = doorbell.** A child finishing a turn fires `agent.hook.Stop` on the
  bus. The event tells the router *that* something happened and *which session*,
  but the content is redacted. It is only a trigger.
- **Hook store = truth.** cmux writes a per-agent hook store under
  `$CMUX_HOOKSTORE_DIR` (`~/.cmuxterm/<agent>-hook-sessions.json`, one per agent
  kind). Each carries `sessions{}` and `activeSessionsBySurface{}` with
  `surfaceId`, `sessionId`, `agentLifecycle`, `pid`, `cwd`, and the launch
  command. cmux-fleet reads the **union** of these stores, so every lookup
  (which surface, which session, is it idle, what is its transcript) is
  tool-agnostic across claude, codex, and any future agent. The bus session id
  is tool-prefixed (`claude-<uuid>`); the store keys on the bare uuid, so the
  router strips the prefix before matching.
- **Transcript = content.** To know what a child actually said, the router reads
  the last assistant message out of the transcript JSONL (cmux's cached last
  body is clobbered by the post-Stop notification hook, so the file is the
  reliable source). The parser handles both the claude and codex transcript
  dialects.

## State files

All mutable state lives under `$CMUX_STATE_DIR` (default
`$XDG_STATE_HOME/cmux-fleet`). Code lives in the plugin; state lives here, so a
throwaway state dir gives you a clean run.

- `fleet.json`: the **live** fleet, keyed by label:
  `{role, kind, tool, cwd, parent, place, status, surface, session, ...}`. Only
  running agents.
- `archive.json`: **parked** agents, keyed by label, with enough to revive
  them (last session, cwd, place, the captured launch binding).
- `inbox.jsonl`: the unified append-only message stream. One line per message:
  `{seq, ts, kind, to, ...payload}`, where `kind` is `completion` (a child
  finished) or `peer` (a deliberate conductor-to-peer send).
- `inbox.seq`: a flock-guarded monotonic counter behind `seq` (the router and
  peer-msg both allocate seqs concurrently).
- `inbox-cursors.json`: per-surface, per-kind ack high-water marks. Acking
  completions never swallows an unread peer, because the cursor is per-kind.
- `inbox-blocks.json`: an ephemeral per-kind drain loop-guard (safe to delete;
  it is rebuilt). Kept separate from the durable ack cursors.
- `log.jsonl`: an append-only event ledger (`launched`, `archived`, `revived`,
  `recycled`, `removed`, `broadcast`, ...). The source-of-truth timeline.
- `notify-mode`: the dial: `passive` | `autodrain` | `auto`.
- `router.seq`: the bus replay cursor, distinct from the inbox seq.

## Identity

Every agent carries four identity fields:

- **kind**: `child` or `conductor`. The router branches on kind, not role.
- **role**: the behavioral type (set as `AGENT_ROLE`); owns the working
  directory in the roster.
- **label**: the unique instance (set as `AGENT_LABEL`); the registry key,
  durable across recycles. Defaults to the role for a single instance.
- **surfaceId**: the agent's current cmux seat. Mutable: recycle and revive
  move an agent to a fresh surface while the label stays put.

A conductor self-identifies via `$CMUX_SURFACE_ID`, which the hooks and the CLI
read to answer "who am I" and "what is in my inbox".

## The router daemon

`scripts/router.py` is one long-lived process, not a hook, and serves every
conductor on the machine. It tails the cmux agent bus
(`cmux events --category agent`) through a PTY (a low-volume stream is otherwise
block-buffered), and on each child `Stop` with `phase == completed`:

1. maps the (bare) session id to a surface via the hook store;
2. looks up that surface in the live registry; ignores it if it is not a
   registered member; backfills a lazily-bound session on the first turn;
3. debounces the roughly two Stops per turn;
4. for a **child**, resolves its parent label to the parent's current surface
   and **delivers** a completion: appends a `completion` row to the inbox, fires
   a `cmux notify` banner, and, in `auto` mode, wakes the parent if it is idle.
   A **muted** child is suppressed (no row, no notify, no wake; the parent reads
   it on demand). A **conductor**'s own Stop only triggers an idle-wake check.

Run it `python3 scripts/router.py` to observe (log decisions, write nothing) or
`--live` to act.

## The two hooks

Both load per agent through `claude --plugin-dir` (the cmux claude wrapper
deep-merges them with cmux's own lifecycle hooks; the arrays concatenate, so
both run). Both self-identify via `$CMUX_SURFACE_ID`, read and write under
`$CMUX_STATE_DIR`, and fail open (any error exits 0).

- **awareness.py** (`UserPromptSubmit`): the always-on, input-safe channel. On
  every turn it injects the conductor's pending inbox (child completions and
  peer messages) into context via `additionalContext`, never the input box.
  Emits nothing when the inbox is empty. Acks are per-kind, so completions and
  peers ack independently.
- **drain.py** (`Stop`): the auto-continue path. Returns
  `{decision: block, reason: ...}` so the agent continues the turn and processes
  pending work instead of stopping. Child completions drain here only in
  `autodrain`/`auto` mode; peer messages drain here always. A per-kind
  block-mark stops an un-acked set from re-blocking forever (it falls back to
  the awareness hook).

The only action that ever injects into an input box is the idle-wake, and it is
gated: `fleet_state.wake_if_idle` submits a wake only when the surface is at the
prompt with an empty draft, and never when the agent is running or has a human
draft pending.

## config.py: resolution precedence

`scripts/config.py` is the one path and setting resolver; every other script
imports its constants and nothing else hardcodes a path. Each key resolves in
order: **environment variable, then the `[fleet]` block in the toml, then a
built-in default** (an XDG path, a `which` lookup, or a skip). The defaults make
a stranger's first run work with no env, no config file, and no vault: state
under XDG, the cmux binary off `$PATH`, the marketplace and floor `CLAUDE.md`
disabled. Tapestry- or machine-specific behavior is layered back in by pointing
the env vars or `[fleet]` block at a vault, never the reverse. See the
configuration table in the README for every key.

## Peer messaging (A2A)

Child-to-parent delivery is automatic (the router). Talking to a **peer**
conductor is deliberate: `peer-msg.py` appends a `peer` row to the same unified
inbox, addressed to the peer's surface, so the peer's awareness hook surfaces it
in context. A reply protocol rides the row: a fresh message expects a reply by
default, `--no-reply` marks it informational, `--reply-to <id>` makes a message a
reply. Peer rows always drain at the recipient's next Stop regardless of the mode
dial (a deliberate send should not wait on the dial), and an idle peer is woken
through the same `wake_if_idle` gate unless `--no-wake`. `fleet broadcast` is the
same mechanism fanned out to a target set (`all`, `all-conductors`,
`all-children`, `my-children`); it never restarts anyone.

## Lifecycle: recycle, revive, archive

- **recycle** restarts a live agent in place on its own surface, same identity,
  via cmux's `respawn-pane`. It runs detached behind a quiet-gate (idle prompt,
  empty draft) so it can recycle the caller itself. The confirm is crash-safe:
  the relaunch is PATH-guarded (the fresh login shell may not have finished
  building `$PATH`, which otherwise makes the cmux wrapper exit 127), and the
  pre-relaunch session id is snapshotted and **excluded** from the fresh-mode
  confirm so a crashed launch resolves to "no session" instead of false-confirming
  on a stale store entry. If no fresh session binds, it re-fires once.
- **archive** parks a live agent: SIGINT for a clean TUI exit, close the tab,
  move the entry to `archive.json` with the captured launch binding.
- **revive** brings a parked agent back on a fresh surface, resuming its last
  session by replaying that binding (with `--resume` swapped in).

For a roster role, recycle and revive are toml-authoritative: they re-resolve the
current roster, so a restart picks up role or floor changes made since launch.

## Worktrees

Config-gated, default-off. A role with `worktree = true` (or `--worktree`) runs
each agent in its own git worktree at `<repo>/.worktrees/<label>` on branch
`fleet/<label>`, instead of sharing the repo's working tree. **One owner: the
fleet.** It runs `git worktree add` itself and launches the tool into that
directory (`claude` plain with its `-w` stripped from passthrough; codex via the
`cd`), and never hooks cmux's `WorktreeCreate`/`WorktreeRemove`, so two owners
never race on one tree. Teardown refuses on a dirty tree (`--wip-commit`
overrides) and always keeps the branch. `scripts/worktree.py` holds the logic
(repo discovery, base-ref cascade, idempotent locked create, fail-closed
teardown); the registry entry carries the worktree bookkeeping so recycle/revive
reuse the tree and `rm --kill` tears it down.

## Workspace groups: one conductor = one group

A `place = workspace` conductor anchors its own cmux workspace-group: its
workspace is the anchor, so the conductor and its children form one collapsible
sidebar group. On launch, if the group does not exist, the fleet creates it on
the conductor's own new workspace via `workspace-group create --from <that
workspace>` (always an explicit `--from`; the implicit form adopts the caller's
workspace). A conductor with no explicit `group` defaults it to its label; a
`place = workspace` child joins its parent's group. Group name-to-ref resolution
is centralized (`_group_ref`) because cmux's `new-workspace --group` and
`workspace-group delete` take a ref, not a name. Recycle and revive preserve the
group; `fleet rm --with-group` dissolves it by ref.

## Profiles and multi-build isolation

A build is a checkout directory; a profile pins every entrypoint at one build so
independent builds run side by side with no shared config, state, or daemons.
`fleet profile <name>` emits a sourceable env block (PATH to the build's `bin`,
plus `CMUX_STATE_DIR`, `CMUX_FLEET_TOML`, `CMUX_FLEET_ROOT`,
`CMUX_FLEET_MARKETPLACE`, `CMUX_BIN`). The launcher also injects those same paths
into every child it spawns (`_profile_env`), so a conductor and all its
descendants, including their hooks, stay on one build regardless of a child
shell's ambient env. See `docs/profiles.md`.
