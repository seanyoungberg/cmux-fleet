# Architecture

The spine as-built, by file:

- `cmux_fleet/config.py` resolves every path/setting (env > `[fleet]` toml > XDG default).
- `cmux_fleet/state.py` owns the state model: the label-keyed registry, the unified inbox, the archive shelf, the hook-store union, the idle-wake gate.
- `cmux_fleet/router.py` is the bus router: child `Stop` -> deliver a completion to the parent. One process serves every conductor.
- `cmux_fleet/daemon.py` is the daemon manager (`fleet daemon start|stop|status|restart` + `start --foreground` for launchd): it double-forks `router.py --live` into a detached supervisor so the router survives shell exit, Bash-tool cleanup, and a conductor recycle.
- `cmux_fleet/hookverbs.py` holds the `fleet hook-awareness` / `fleet hook-drain` logic that surfaces the inbox into a conductor's context (never its input box); the plugin hook files are thin fail-open shims (`scripts/hooks/_shim.py`) that shell into those verbs.
- `cmux_fleet/cli.py` is the CLI: launch, the lifecycle verbs, peer messaging, broadcast, worktrees, profiles, and the read-only views (`cmux_fleet/features.py`).
- `fleet peer-msg`, `fleet child-digest`, `fleet drive-child`, `fleet inbox-ack` are the agent-facing helper verbs (`cmux_fleet/helpers.py`).

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

## The claude wrapper and the `CMUX_*` footgun

A fleet agent is `claude` (or `codex`) running in a cmux **terminal surface**.
When it starts there, cmux sets `$CMUX_SURFACE_ID` and a `cmux-claude-wrapper`
shim on `PATH` injects a `--settings` that wires the full Claude Code lifecycle
(SessionStart, UserPromptSubmit, Stop, SessionEnd, Notification,
PermissionRequest, ...) to `cmux hooks`. That is what makes a session send-able,
readable, restorable, and a clean emitter of lifecycle events with correct
attribution — with none of the plugin's own hook plumbing. The wrapper
**deep-merges** any `--settings` you pass (hook-event arrays concatenate, your
scalars win), so a conductor's own `--plugin-dir` hooks ride *on top of* cmux's
lifecycle instead of clobbering it.

Three wrapper behaviors shape the orchestration built on it:

- **It is gated on `$CMUX_SURFACE_ID`.** Set → it injects the hooks and a session
  id. Unset, or `CMUX_CLAUDE_HOOKS_DISABLED=1` → it passes straight through to the
  real `claude` with no hooks and no session id. Those are the two levers for
  running a `claude` inside a cmux terminal *without* it touching cmux state.
- **It assigns a fresh `--session-id`** unless you already pass
  `--resume`/`--session-id`/`--continue`. So a relaunch with `--resume <id>` keeps
  the surface's session; a bare launch gets a new one. The surface id is the stable
  identity, not the session.
- **It injects for `claude -p` too.** This is the footgun: a nested `claude -p`
  (or a Claude Code Task-tool subagent, which runs in-process on the parent's
  surface) inherits the parent's `$CMUX_SURFACE_ID`, so *its* lifecycle hooks fire
  against the **parent** surface and corrupt the parent's status a few seconds
  later. The fix is to **scrub every `CMUX_*` variable (and set
  `CMUX_CLAUDE_HOOKS_DISABLED=1`) before spawning any nested agent** — the same
  discipline cmux's own background summarizer follows. For any non-trivial helper,
  **prefer a real cmux child (its own surface and session) over an in-process
  subagent**, so lifecycle stays attributable.

## Reading `agentLifecycle`

cmux runs the session state machine and stores the result, so the plugin never
recomputes busy/idle from raw events. The field has four values —
`unknown | running | idle | needsInput` — and there is **no `ended`**
(session-end clears the record). Two traps matter when routing on it:

- **A finished turn sits at `idle`,** with its real last message; it does *not*
  auto-flip to `needsInput`. `needsInput` fires only on a genuine signal: a
  permission / `AskUserQuestion` / `ExitPlanMode` gate, or a long idle-at-prompt.
  So a child that just completed is `idle`, not `needsInput`. **Not-busy = `idle`
  or `needsInput`**; its output is ready to read either way.
- **Busy = a *fresh*, live `running` record.** A `running` record that has not
  ticked within a staleness window (~90s) is treated as **not** mid-turn, so an
  orphaned or stale `running` can never wedge the wake gate into thinking a
  finished conductor is still working. The input-safe wake gate (see **The two
  hooks**, below) then confirms an actual clean idle prompt *on screen* before
  injecting — the screen is ground truth, not the store. These status signals are
  upstream-flaky (see the risk register in `docs/operations.md`), which is why the
  router trusts the debounced `Stop` and a transcript read, never the live sidebar
  pill.

## The Feed: programmatic gate supervision

When a child hits an interactive gate — an `AskUserQuestion`, a permission
request, or an `ExitPlanMode` — cmux parks it in the **Feed**, a short-lived
(~120s) semaphore keyed by the request id. `feed.list` reports each item's `kind`
(permission / question / exit_plan / stop / sessionStart), `status` (`pending`
for a live gate, `resolved` once answered, `telemetry` for non-gates), and its
source. A pending gate is answerable over the cmux socket **without touching the
child's input box**:

- `feed.permission.reply {request_id, mode}` — `once` | `always` |
  `bypassPermissions` | `deny`
- `feed.question.reply {request_id, selections}` — selections are option *labels*
- `feed.exit_plan.reply {request_id, mode}`

This is the channel that lets a conductor (or an operator) answer a child's gates
programmatically, and it is agent-agnostic — the same Feed serves claude and
codex. cmux-fleet does not yet drive the Feed from the router (auto-answering a
gate by policy is unbuilt); today it is a deliberate, in-the-loop move.

## Two agent tools: claude and codex

cmux-fleet runs `claude` and `codex` as first-class agent tools over one roster,
one hook-store union, one router, and one inbox. `cmux_fleet/cli.py` is a dumb
builder over each tool's native flags and env; the only tool-specific code is a
small adapter branch plus the resume form. Two substrate differences shape the
orchestration:

- **codex registers lazily and never ends.** claude binds a session at boot;
  codex binds on its **first turn**. So a freshly launched codex child has no
  session yet — `fleet ls` shows it `pending`, and the router backfills the
  session on its first `Stop`. codex also fires **no `SessionEnd`**, so its
  hook-store entry lingers after the process exits and re-binds on the next turn.
  The practical rule: **drive a `pending` codex child to bind it.**
- **The resume form differs.** claude resumes with a `--resume <id>` flag; codex
  resumes with a `resume <id>` subcommand. Both continue the *same* session id;
  recycle and revive carry both shapes.

Plugins are cross-tool the same way. A plugin's hooks fire **per agent tool**, so
a plugin that must run under both ships **one source tree with a per-tool
hook-declaration file**: claude reads `.claude-plugin/plugin.json` +
`hooks/hooks.json`; codex reads `.codex-plugin/plugin.json`, whose `hooks` field
names a *separate* codex hooks file; and an adapter absorbs the I/O deltas (codex
has no `SessionEnd`, carries an `apply_patch` tool, and hands hooks a different
stdin shape). cmux-fleet's own conductor hooks are claude-side today: codex runs
as a first-class **agent**, but the adapter does not yet map claude's
`plugins`/`settings` vocabulary onto codex (it warns and ignores those keys), so
a codex agent does not load the plugin's conductor hooks.

## State files

All mutable state lives under `$CMUX_STATE_DIR` (default
`$XDG_STATE_HOME/cmux-fleet`). Code lives in the plugin; state lives here, so a
throwaway state dir gives you a clean run.

- `fleet.json`: the **live** fleet, keyed by label:
  `{role, kind, tool, cwd, parent, place, status, surface, session, ...}`. Only
  running agents.
- `archive.json`: **archived** agents, keyed by label, with enough to revive
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
- `notify-mode`: the wake **mute switch**: `passive` (mute) | `auto` (default, wake-now).
- `router.seq`: the bus replay cursor, distinct from the inbox seq.
- `router.pid` / `router.daemon.json` / `router.log`: the daemon manager's
  pidfile (the supervisor pid), its metadata (state dir, start time, heartbeat
  interval), and the router's log. Written by `fleet daemon`, one set per state
  dir.

## Identity

Every agent carries four identity fields:

- **kind**: `child` or `conductor`. The router branches on kind, not role.
- **role**: the behavioral type (set as `AGENT_ROLE`); owns the working
  directory in the roster.
- **label**: the unique instance (set as `AGENT_LABEL`); the registry key,
  durable across recycles. Defaults to the role for a single instance.
- **surfaceId**: the agent's current cmux surface. Mutable: recycle and revive
  move an agent to a fresh surface while the label stays put.

A conductor self-identifies via `$CMUX_SURFACE_ID`, which the hooks and the CLI
read to answer "who am I" and "what is in my inbox".

## The router daemon

`cmux_fleet/router.py` is one long-lived process, not a hook, and serves every
conductor on the machine. It runs under `fleet daemon` (`cmux_fleet/daemon.py`),
which double-forks with `setsid` so the router keeps its own session and process
group and survives the starting shell exiting, an agent's Bash-tool process-group
cleanup, and a conductor self-recycle. The manager writes `<state>/router.pid`
(the supervisor pid), `<state>/router.daemon.json`, and `<state>/router.log`,
refuses to double-start, and cleans a stale pidfile. It is per-state, so under a
profile it manages that profile's router. Running the router by hand from a
session is unsupported: a bare `nohup &` dies with the tool's process group, or
survives as a stray duplicate that double-processes the bus. See
`docs/operations.md` for the verbs.

The router tails the cmux agent bus (`cmux events --category agent`) through a
PTY (a low-volume stream is otherwise block-buffered), and on each child `Stop`
with `phase == completed`:

1. maps the (bare) session id to a surface via the hook store;
2. looks up that surface in the live registry; ignores it if it is not a
   registered member; backfills a lazily-bound session on the first turn;
3. debounces the roughly two Stops per turn;
4. for a **child**, resolves its parent label to the parent's current surface
   and **delivers** a completion: appends a `completion` row to the inbox, fires
   a `cmux notify` banner, and, in `auto` mode, wakes the parent if it is idle.
   A **muted** child is suppressed (no row, no notify, no wake; the parent reads
   it on demand). A **conductor**'s own Stop only triggers an idle-wake check.

Run it `python -m cmux_fleet.router` to observe (log decisions, write nothing) or
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
  pending work instead of stopping. Child completions drain here unless the dial
  is `passive` (wake-now default); peer messages drain here always. A per-kind
  block-mark stops an un-acked set from re-blocking forever (it falls back to
  the awareness hook).

The only action that ever injects into an input box is the idle-wake, and it is
gated: `state.wake_if_idle` submits a wake only when the surface is at the
prompt with an empty draft, and never when the agent is running or has a human
draft pending.

## config.py: resolution precedence

`cmux_fleet/config.py` is the one path and setting resolver; every other module
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
conductor is deliberate: `fleet peer-msg` appends a `peer` row to the same unified
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
  via cmux's `respawn-pane`. Default is **RESUME** (preserve context — the
  least-disruptive default); **`--fresh`** sheds into a new session + primes from
  the handover; `--session <id>` resumes an arbitrary prior session. It runs
  detached behind a quiet-gate (idle prompt, empty draft) so it can recycle the
  caller itself. The confirm is crash-safe: the relaunch is PATH-guarded (the
  fresh login shell may not have finished building `$PATH`, which otherwise makes
  the cmux wrapper exit 127), and the pre-relaunch session id is snapshotted and
  **excluded** from the fresh-mode confirm so a crashed launch resolves to "no
  session" instead of false-confirming on a stale store entry. If no fresh session
  binds, it re-fires once. Bulk selectors (`--all`/`--conductors`/`--children`/
  `--my-children`) restart many sequentially + gated, skipping self and muted.
- **archive** parks a live agent: SIGINT for a clean TUI exit, close the tab,
  move the entry to `archive.json` with the captured launch binding. `last_session`
  is captured from cmux's checkpoint (ground truth) so revive resumes the real id.
- **revive** brings an archived agent back on a fresh surface, resuming its last
  session by replaying that binding (with `--resume` swapped in); `--fresh` sheds,
  `--session <id>` targets an arbitrary prior session.

Session ids are kept honest against cmux's live id: the router reconciles the
registry `session` on every `Stop` (tool-aware, so a codex id never overwrites a
claude agent's session), killing the "No conversation found" class on later
archive/revive.

### Launch-config compilation: the one rule, stated once

Every operation that composes a launch command — `launch`, `recycle` (single-target and bulk alike),
`revive`, and `register` — follows the same rule, so this is worth stating once rather than
re-deriving per verb:

- **A roster role (has a `[role.<name>]` block in `fleet.toml`) is TOML-AUTHORITATIVE, always.** Every
  one of the four verbs re-resolves the CURRENT toml on every call — never the toml as it was at
  original launch time, never a captured binding. A restart of a roster agent picks up role/floor
  changes made since it was first launched, full stop. There is no verb where a roster role's identity
  is reconstructed from history instead of current config.
- **An ad-hoc / off-roster identity has no toml to be authoritative, so it falls back to the captured
  binding** (the exact command cmux recorded at bind time) — this is the one case where history is the
  source of truth, and only because there's nothing else to consult.
- **Caller `--` flags (one-off `--effort`, `--model`, `--add-plugin`, etc.) always layer on top with the
  highest precedence, for that single invocation only.** They are never persisted anywhere — not into
  the toml, not into the registry, not into a future respawn's defaults. If you want an override to
  survive the NEXT restart too, it has to go in the toml (a role's `flags`) or be passed again.
- **A mid-session interactive change (`/effort`, `/model`) does NOT survive a respawn.** Those write to
  the GLOBAL `~/.claude/settings.json`, and a composed `--effort`/`--model` launch flag always overrides
  a saved setting — so unless the role has its own pin in the toml, the next recycle/revive/launch
  reverts to whatever the role/floor's toml flags say (or the floor default, if the role has no pin at
  all). This is why `recycle`/`launch` print `[fleet] session-prefs: effort=X (source)` — the source
  tells you whether what you're about to get is a role-pin (durable, survives respawns) or a
  floor-inherited value (will NOT reflect an interactive change you made this session).

None of this is a special case per verb — it's the same rule, checked in the code that composes each
one (`_compose_recycle_cmd` for recycle/revive, `resolve()` for launch, the roster-vs-binding branch in
`cmd_register`). If a future change to any of these ever needs to deviate from this rule, that's a
sign something's wrong, not a sign the rule needs an exception.

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
