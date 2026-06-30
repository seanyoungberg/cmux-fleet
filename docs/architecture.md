# Architecture

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
