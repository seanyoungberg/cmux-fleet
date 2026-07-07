---
name: cmux-fleet
description: Run a parent/child agent fleet natively on cmux. Use when you are a conductor that needs to spawn, place, drive, observe, or get notified about child agents (workers or sub-conductors) in cmux — launching children into panes/tabs/workspaces, retrieving their output, answering their Feed gates, and receiving input-safe completion notifications. Covers launch, drive-child, the notify flow and mode dial, and layout.
---

# cmux-fleet

You are a **conductor**: a long-lived agent that spawns and coordinates child agents (workers or sub-conductors) as native cmux surfaces. cmux owns sessions/lifecycle/transcripts/the bus; this toolkit adds the orchestration layer. Scripts are in `${CLAUDE_PLUGIN_ROOT}/scripts/`; state is under `$CMUX_STATE_DIR` (default `$XDG_STATE_HOME/cmux-fleet`). The cmux binary resolves from `$CMUX_BIN`, then `which cmux` (see `scripts/config.py`).

## At a glance — the everyday flow
The common loop is **launch → drive → (completions arrive on their own) → digest**, then **recycle or retire**. `fleet` is on PATH; the helper scripts live in `${CLAUDE_PLUGIN_ROOT}/scripts/`. Exact invocations are in the sections below.
- **Spawn + task a worker:** `fleet launch <role> --parent $CMUX_SURFACE_ID` → `fleet drive-child <child-surface> "<task>"`
- **Read what it did:** `fleet child-digest <session-frag> 5` — a child's *completion* also reaches you automatically via the notify flow; you don't poll for it.
- **Restart yourself clean:** write a handover (`/cmux-handover`) → `fleet recycle --fresh` → end your turn. (`recycle` defaults to RESUME now — `--fresh` is what sheds context + primes the handover.)
- **Park vs throw away:** `fleet archive <label>` (durable, revivable) · `fleet rm <label> --kill` (throwaway).
- **Message a peer conductor:** `fleet peer-msg <peer-label> "<body>"`.
- **Know your fleet:** `fleet ls` (defaults to *yours* — you + your children; `--scope all` for the world) + `fleet inbox` (pending work). **Know yourself:** `$CMUX_SURFACE_ID` (invariant across recycle).

## Who am I
- `cmux identify --json` → my surface/workspace/pane. My stable id is `$CMUX_SURFACE_ID` (invariant across relaunch).
- My fleet: `fleet ls` → live members (label / role / kind / status / lifecycle), reconciled against cmux's hook store (flags **STALE** = registry says live but the surface is gone), plus the archived (parked) shelf. Defaults to **your** scope (you + your children); `fleet ls --scope all` for every live member.

## Spawn a child
**`fleet launch <role> --parent $CMUX_SURFACE_ID`** (the `fleet` shim is on PATH via the plugin). **Spawns by default**; add `--dry-run` to preview the resolved launch first.
- A child defaults to a **tab in your agents pane** (`place=tab`). Override with `--place pane|workspace`, `--tool <t>`, `--label <name>`, `--cwd <dir>`.
- **Pass any tool flag verbatim after `--`:** `fleet launch research-agent -- --effort max --add-dir /tmp/x`. Everything after `--` is forwarded to the underlying tool; a caller flag overrides the same flag from the role/floor, repeatable flags stack.
- **Off-roster dynamic agent:** `fleet launch --adhoc cmux-gh-repo-explorer --plugins gh-tools -- --model opus` → no roster entry, `cwd=<adhoc_subdir>/<name>` (created fresh; if a floor `CLAUDE.md` is configured it is symlinked in so the agent inherits `/cmux-fleet:ground`). Promote it to a real role later by writing its toml block.
- The roster is the fleet toml (`$CMUX_FLEET_TOML`, default `$XDG_CONFIG_HOME/cmux-fleet/fleet.toml`; role-first, tool-nested): a role owns orchestration (`cwd`/`place`/`group`/`kind`) once; per-tool sub-blocks carry that tool's `plugins`/`flags`/`env`/`settings`. `AGENT_ROLE` is auto-set. The launcher is a dumb builder over native flags/env/`--settings` — it invents no setting names, so any valid `claude`/`codex` flag just works.
- It creates the surface, launches the tool, polls cmux's hook store for the bound session, and writes the **label-keyed** registry entry (incl. `tool`, `kind`, parent label). It **aborts without sending** if it can't resolve the exact target UUID. claude binds at boot; **codex binds lazily on its first turn** (it shows `pending` in `fleet ls` until then, so drive it to bind).
- A conductor child needs the aware/drain hooks: give its role `plugins = ["cmux-fleet", ...]` so `--plugin-dir` adds them (the cmux wrapper merges them with cmux's lifecycle).

`fleet.py` is one Python tool, `fleet` on PATH. **Prerequisite:** the fleet-wide router daemon (`router.py --live`) must be running or no completion ever reaches you (see the notify flow; lifecycle in `docs/operations.md`).

## Identity (role vs label)
Each agent has a **role** (behavioral type + config, e.g. `worker`; `AGENT_ROLE`, owns the home dir `<root>/<role-cwd>/`) and a **label** (unique instance handle; `AGENT_LABEL`; the registry key + routing/recycle anchor). Label defaults to role for a singleton; to run two of a role, give distinct labels (`fleet launch worker --label worker-2`) — same role/home/behavior, different instance. The **surfaceId** is just the current seat (changes on respawn). See `docs/architecture.md`.

## Manage your fleet (lifecycle — YOUR job)
You own your fleet's inventory: reconcile what's registered against what's actually running, and recycle/retire as needed.
- **`fleet ls [--scope mine|all|conductors|children]`** — live members + status; flags **STALE** (a closed tab / crash that never archived) and lists the archived shelf. Defaults **`--scope mine`** (you + your direct children); **`--scope all`** for the whole fleet. (One vocabulary shared by every scope-aware verb — see the boot doc's `--scope` model.)
- **`fleet archive <label>`** — park a live agent: stops its process (SIGINT) + closes the tab + shelves it (cwd + last session) in `archive.json`. Revivable.
- **`fleet revive <label>`** — bring a *parked* (archived) agent back into a fresh surface, resuming its last session (per the tool: `claude --resume` / `codex resume`); the label + home persist, the surface is new. `--fresh` sheds into a new session instead; `--session <id>` resumes an arbitrary prior one. (revive = parked→live; for live→live restart-in-place, use `recycle`.)
- **`fleet sessions <label>`** — list resumable prior sessions for the agent's surface (id · age · size · first-user snippet, freshest first; `*` = currently bound). Pick an id for `recycle --resume --session <id>` / `revive --session <id>` — no hand-hunting under `~/.claude/projects`.
- **`fleet recycle [label]`** — restart a **live** agent *in place on the same surface*, same identity. Default = **RESUME** (preserve context — the least-disruptive default, ratified 2026-07-01); **`--fresh`** sheds context into a new session + auto-primes from the latest handover. `--session <id>` resumes an ARBITRARY prior session (`fleet sessions <label>` lists them); `--resume` is a no-op alias (resume is the default now). Self-targets via `$CMUX_SURFACE_ID` if no label (bare recycle = **just you**). Runs **detached**, waits for the target to go quiet (idle + empty draft; `--force` to override) then uses cmux's native `respawn-pane`, so the label + all parent/child routing stay valid (only the session id changes). BULK: `fleet recycle --scope mine|all|conductors|children [--include-muted]` restarts many, sequential + gated, skipping self + muted (`mine` = your children; cross-conductor recycle is the safe topology). *(Legacy `--all|--conductors|--children|--my-children` still work, hidden/deprecated.)* Pairs with `/cmux-handover` (write handover → `fleet recycle --fresh` → end turn). **`--dry-run` FIRST, every time: read the printed `session-prefs: effort=… model=… (source)` line and confirm the model/effort are what you intend before the real recycle** — the cheap guard against a silent came-back-on-the-wrong-model/effort restart (fixed the 2026-07-04 stale-global-default incident at the `[tool.claude]` floor, but dry-run-verify is the standing catch for any future drift).
  - **Loadout is TOML-AUTHORITATIVE for a roster role:** recycle/revive **re-resolve the current `cmux-fleet.toml`** (floor + role config), so a restart **picks up any config change since launch** (new floor plugin, `enable_plugins`, `setting_sources`, effort…). An **ad-hoc** agent (no roster role) instead reproduces its captured launch. **A one-off `-- <flags>` / `--add-plugin` applies to THAT restart only** — it does *not* stick (the toml is the source of truth). To make a change durable, edit the role's toml; to carry a one-off arg across a restart, pass it again. (cmux's own app-restart restore still replays "what ran"; this is just the `fleet` verbs.)
- **`fleet rm <label> [--detach] [--force] [--with-group]`** — retire a label: bare `rm` now stops the process, closes the surface AND writes an archive row (recoverable via `fleet revive`) — a removed label can never leave a zombie surface running. A mid-turn ("running") surface refuses; `--force` closes it anyway. **`--detach`** is the explicit opt-out: drop the registry row only and leave the surface running (e.g. handing a pane to a human — NOT `fleet mute`, which keeps the child tracked and only stops completion pushes). `--kill` is kept as an alias for the default; the one thing it still adds is tearing down a worktree-isolated agent's worktree. `--with-group` dissolves the agent's whole workspace-group (closes every member surface) and sweeps all of that group's members out of the registry; swept members' worktree dirs/branches are left unmanaged (their registry rows are gone, so `fleet worktree clean` can't find them) and must be reclaimed manually with `git worktree list` / `git worktree remove <path>` (+ `git branch -D fleet/<label>`). Without it, only this agent's own workspace is removed.
- **`fleet config <role|--cwd DIR>`** — the effective config a launch would get (base settings stack + what fleet adds, with overrides flagged). Claude has no native dump.
- **`fleet mute <label>` / `fleet unmute <label>`** — stop/resume pushing a child's completions to *you*. A muted child's Stop is suppressed in the router (no inbox row, no notify, no idle-wake); you read it **on demand** (`fleet ls` shows it `MUTED` + session → `child-digest`). Use when the user drives a child directly and you shouldn't be spammed by its completions. Survives recycle; resets on archive→revive. Bulk: **`fleet mute --scope mine`** mutes all your children at once.
- **`fleet broadcast "<msg>" --scope mine|all|conductors|children [--no-wake] [--expect-reply] [--dry-run]`** — input-safe heads-up to live agents about an out-of-band change that won't reach them on its own (a toml/floor edit, a plugin bump). Same delivery as `peer-msg` (awareness hook → context, never the input box) + idle-wake. Informational by default; **never restarts anything** (recipients decide, usually `fleet recycle`). Self excluded. It's an **act, so `--scope` is REQUIRED** (no default fan-out — you say who; `mine` = your children). Canonical use: after editing `cmux-fleet.toml`, broadcast `--scope conductors` so each recycles to pick it up (recycle is toml-authoritative). *(Legacy `--target all|all-conductors|all-children|my-children` still works, deprecated.)*

## See your fleet (read-only views)
Derived from live state every call (registry + hook stores + transcripts); no daemon, no stored status. Status is inferred WITHOUT an LLM (cmux `agentLifecycle` is authoritative, refined by keyword tables).
- **`fleet inbox [--scope mine|<label>|all|conductors|children] [--json]`** — your **pending inbox on demand** (child completions + auto-archive/health alerts + peer messages, oldest→newest), each with its `inbox-ack` command. The **catch-up read** for wakes that queued while you were down — run it at session start / after a recycle (the push path can't replay across a fresh session). Defaults `--scope mine` (yours); `--scope <label>` peeks one agent's inbox; `--scope all` = the multi-inbox triage view.
- **`fleet vitals [--scope mine|all|conductors|children] [--json] [--paint]`** — the triage table: one row per live agent, **most-urgent first** (error / needs-input / review / working / done / idle), with each agent's **context-remaining %** (a `!` marks ≤30% left → recycle candidate). Your first-glance "who needs me / who's near-full." Defaults `--scope mine`; `--paint` also syncs the (full-fleet) sidebar. Window is a per-model guess — set `CMUX_FLEET_CONTEXT_WINDOW` / `[fleet].context_window` for an exact %; the ranking is right regardless.
- **`fleet find <query> [--turns N] [--json]`** — find an agent by label/role/cwd **or by what it has been saying** (scans the last N transcript turns). The "which session was working on X" lookup; searches live + archived.
- **`fleet graph [--scope mine|all|<label>] [--html] [--out FILE]`** — the fleet as a **parentage tree** (nests by the `parent` label; cycle-safe). Defaults `--scope mine` (your subtree, rooted at you); `--scope all` = the full tree; a bare `<label>` roots the subtree there. Text by default; `--html` writes a self-contained dark page and prints its path → open it in a view pane.
- **`fleet serve [--port N]`** — a **thin** read-only localhost view: `GET /` → the live graph HTML, `GET /vitals.json` → the rows. Foreground, no daemon. Ctrl-C to stop.
- **`fleet paint`** — sync fleet state onto the cmux **sidebar**: a status pill (`set-status`) + a context progress bar (`set-progress`) per workspace, **on change only**, additive (never recolors/renames your workspaces). The native sidebar then shows each agent's state at a glance. For a dedicated board, install `sidebars/fleet.swift` (see README).

## Layout (DEFAULT — this is the canonical fleet layout)
**Two panes stacked top/bottom, both full width. Never more than 2 panes (no side-by-side columns) unless the user explicitly asks.**
- **BOTTOM pane = agents.** The conductor's own terminal plus its child terminals live here as **tabs in this one pane**. A child you launch gets folded in as a tab.
- **TOP pane = view.** Markdown / HTML / diff surfaces. Any agent that wants to expose something opens it here, as another tab in this same pane.
- **General placement rule:** a new agent goes into a pane that already has agents; a new view (.md/html) goes into a pane that already has views. You almost never create a third pane.

Recipes (caller surface = `$CMUX_SURFACE_ID`, your bottom/agents pane). Use the `cmux-workspace` skill for the full CLI; `--focus false` always, never `focus-pane`/`select-workspace`/`drag-surface-to-split`:
- **Fold a freshly-launched child into the agents pane (as a tab):** `cmux move-surface --surface <child-surface-uuid> --pane <agents-pane-uuid> --focus false` (a `--place pane` launch makes its own split first; fold it in to collapse back to 2 panes).
- **Open a view above the agents:** `cmux markdown open <file> --direction up --surface $CMUX_SURFACE_ID --focus false` (splits a full-width view pane on top, agents stay on the bottom). Re-`open` more files to stack them as tabs in that view pane.
- **Rebuild from sprawl:** fold every child into your pane, close stray view panes (`cmux close-surface --surface <s>`), then re-open the view with `--direction up`.

### When a child gets its OWN workspace instead
Dispatch a child to a **dedicated workspace in the conductor's group** (same top/bottom split inside it), NOT a tab, when any of:
- it's an agent the user will work with **directly**, or
- it will do **long-running** work, or
- it's one of a **group of agents on the same task**.

Otherwise (the common case) children are tabs in the conductor's bottom pane. For the dedicated case just use `--place workspace`: a workspace child with **no** `--group` automatically **joins its parent conductor's group** (one conductor = one group), so you do not name the group. Pass `--group <name>` only to override that default and put the child in a different group. (`--place pane` + fold for the tab case.)

## Drive a child
`fleet drive-child <child-surface-uuid> "<prompt>"` — sends the text then a SEPARATE `send-key enter`. A trailing `\n` in `cmux send` only inserts a newline; it does NOT submit. It fails loud (non-zero) if a cmux call errors. Always target by surface UUID, never a bare `surface:N` ref.

## Retrieve a child's output
`fleet child-digest <session-frag> <N>` → last N transcript turns (the reliable source). Do NOT trust the hook store's `lastBody` for content — the cmux Notification hook clobbers it right after Stop.

## The notify flow (how children's completions reach you)
A fleet-wide daemon (`router.py --live`, run separately) watches the bus. When a child finishes, it appends the completion to the queue + fires a `cmux notify` banner. It NEVER types into your input. You become aware of it through your hooks:
- **awareness** (every turn): pending completions are injected into your context as a `[fleet] N pending` note, each with a `fleet child-digest` command and an ack command. Handle them, then ack: `fleet inbox-ack <seq>` so they stop re-surfacing.
- **The mode dial** `$CMUX_STATE_DIR/notify-mode` (read live, no restart) — a **mute switch**; wake-now is the default:
  - *(default — no file)* or `auto` — the router **idle-wakes you** (when your input is empty) so you catch completions/peers even while idle and unattended, and your Stop hook auto-continues you to drain pending at the end of any turn.
  - `passive` — the one mute: awareness only, nothing auto-driven or woken. Calm, human-driven ("leave me alone, I'll drain on my own"). *(The old `autodrain` value is retired — it now behaves as `auto`.)*

Key fact: a completion or peer **wakes a flat-idle conductor by default** (wake-now); only `passive` mutes that. A Stop hook fires only on a turn you're already taking, so the router's idle-wake + the heartbeat backstop are what reach you while you sit idle. Build around the Stop *event* + your captured state, not the upstream live status field (it's unreliable).

- **Draft-through** `$CMUX_STATE_DIR/draft-through` (default `stale`): a human draft in your input box is preserved while fresh, but a **walked-away** draft (unchanged ≥ 90s) is cleared + woken (audited as `draft_clobbered`) so it can't silence you indefinitely — active typing is never clobbered. Override with `clobber` (any draft, immediately) or `preserve` (never). The input-clear (`ctrl+u`) is best-effort (degrades to a mashed submit); save/clear/wake/**restore** is the follow-up.

## Talk to a peer conductor (A2A)
Child→parent comms are automatic; talking to a **peer** conductor is a **deliberate choice**, and the peer is NOT expecting it. Same input-safe delivery as the notify flow, on a separate `peer-inbox` channel, never the input box. Because a peer send is deliberate, it reaches the recipient **promptly in both states** (see Delivery).
- **Send:** `fleet peer-msg <peer-label> "<body>"` (peers resolve by label from the fleet registry). The message reaches the peer carrying the `@<peer>: [<you>]` identity markers.
- **Reply protocol (explicit):** a fresh message **expects a reply** by default; add `--no-reply` to make it informational; the recipient replies with `--reply-to <msg_id>` (a reply expects none further unless `--expect-reply`).
- **Delivery (both states, prompt by default):**
  - **IDLE recipient** (at the prompt, no draft) is **woken now** to handle the message, so it doesn't sit until the human's next prompt and bundle in with it. `--no-wake` opts out and leaves it for the peer's next turn. A human's draft is never clobbered.
  - **BUSY recipient** is never interrupted mid-turn, but its **drain (Stop) hook surfaces pending peer messages at the end of its current turn, ALWAYS** — not gated on `notify-mode` (unlike child completions), because a peer send is deliberate and shouldn't wait on the dial.
- **Receiving:** peer messages arrive as a `[peer] N message(s)` context note (distinct from `[fleet]` child completions), each with its reply command. Ack when handled: `fleet inbox-ack <seq> --peer`.

## Answer a child's gate (permission / question / plan)
A child's AskUserQuestion/permission/ExitPlanMode parks in the Feed (≤120s), keyed by `request_id`.
- See it: `cmux rpc feed.list '{"limit":50}'` → items with `status:"pending"` (sort by `updated_at`).
- Answer: `cmux rpc feed.question.reply '{"request_id":"<id>","selections":["<label>"]}'` (selections are option LABELS). Also `feed.permission.reply {request_id, mode:once|always|bypassPermissions|deny}`, `feed.exit_plan.reply {request_id, mode}`.

## Hard-won rules
- Send by surface **UUID only** (bare `surface:N` refs are window-scoped and shift; one misfired into a live conductor).
- Done-detection = the debounced Stop event + transcript, not the `agentLifecycle` field. `idle` is a blink; an agent at the prompt reports `needsInput`. So **wakeable = idle OR needsInput**, busy = `running`.
- Identity = surfaceId, not session_id (a relaunch gives a new session_id; the surface is the persistent seat).
- `new-workspace --group` needs a group REF (`workspace_group:N`), not the name.
- **Never `move-surface` a child that has already taken its first turn (bound its session), especially across workspaces.** It desyncs the surface's hook-store binding (the live `sessions{}` record vanishes, a frozen `activeSessionsBySurface` pointer remains), so the router can't map that child's `Stop` → the parent and **silently drops every completion** — the conductor stalls with no wake, and `fleet ls` shows the agent STALE though its pane is alive. Folding a *freshly-launched* child into the agents pane *before* its first turn (the Layout recipe) is safe — the bind happens after. If you must relocate a running agent, `fleet recycle <label>` afterward to rebind, and confirm a `[QUEUE …]` line appears in `router.log` on its next `Stop`. Better: launch into the target workspace up front (`CMUX_WORKSPACE_ID=<workspace-uuid> fleet launch …`). (Root cause #3 in the notifications redesign; a fleet-owned safe-move is backlogged.)
