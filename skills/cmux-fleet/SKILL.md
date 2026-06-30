---
name: cmux-fleet
description: Run a parent/child agent fleet natively on cmux. Use when you are a conductor that needs to spawn, place, drive, observe, or get notified about child agents (workers or sub-conductors) in cmux — launching children into panes/tabs/workspaces, retrieving their output, answering their Feed gates, and receiving input-safe completion notifications. Covers launch, drive-child, the notify flow and mode dial, and layout.
---

# cmux-fleet

You are a **conductor**: a long-lived agent that spawns and coordinates child agents (workers or sub-conductors) as native cmux surfaces. cmux owns sessions/lifecycle/transcripts/the bus; this toolkit adds the orchestration layer. Scripts are in `${CLAUDE_PLUGIN_ROOT}/scripts/`; state is under `$CMUX_STATE_DIR` (default `$XDG_STATE_HOME/cmux-fleet`). The cmux binary resolves from `$CMUX_BIN`, then `which cmux` (see `scripts/config.py`).

## At a glance — the everyday flow
The common loop is **launch → drive → (completions arrive on their own) → digest**, then **recycle or retire**. `fleet` is on PATH; the helper scripts live in `${CLAUDE_PLUGIN_ROOT}/scripts/`. Exact invocations are in the sections below.
- **Spawn + task a worker:** `fleet launch <role> --parent $CMUX_SURFACE_ID` → `drive-child.py <child-surface> "<task>"`
- **Read what it did:** `child-digest.py <session-frag> 5` — a child's *completion* also reaches you automatically via the notify flow; you don't poll for it.
- **Restart yourself clean:** write a handover (`/cmux-handover`) → `fleet recycle` → end your turn.
- **Park vs throw away:** `fleet archive <label>` (durable, revivable) · `fleet rm <label> --kill` (throwaway).
- **Message a peer conductor:** `peer-msg.py <peer-label> "<body>"`.
- **Know your fleet:** `fleet ls`. **Know yourself:** `$CMUX_SURFACE_ID` (invariant across recycle).

## Who am I
- `cmux identify --json` → my surface/workspace/pane. My stable id is `$CMUX_SURFACE_ID` (invariant across relaunch).
- My fleet: `fleet ls` → every live member (label / role / kind / status / lifecycle), reconciled against cmux's hook store (flags **STALE** = registry says live but the surface is gone), plus the archived (parked) shelf.

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
- **`fleet ls`** — live members + status; flags **STALE** (a closed tab / crash that never archived) and lists the archived shelf.
- **`fleet archive <label>`** — park a live agent: stops its process (SIGINT) + closes the tab + shelves it (cwd + last session) in `archive.json`. Revivable.
- **`fleet revive <label>`** — bring a *parked* (archived) agent back into a fresh surface, resuming its last session (per the tool: `claude --resume` / `codex resume`); the label + home persist, the surface is new. (revive = parked→live; for live→live restart-in-place, use `recycle`.)
- **`fleet recycle [label]`** — restart a **live** agent *in place on the same surface*, same identity. Default = **fresh** session (sheds context) + auto-prime from the latest handover; `--resume` continues the session. Self-targets via `$CMUX_SURFACE_ID` if no label. Runs **detached**, waits for the target to go quiet (idle + empty draft; `--force` to override) then uses cmux's native `respawn-pane`, so the label + all parent/child routing stay valid (only the session id changes). Pairs with `/cmux-handover` (write handover → `fleet recycle` → end turn). `--dry-run` to preview.
  - **Loadout is TOML-AUTHORITATIVE for a roster role:** recycle/revive **re-resolve the current `cmux-fleet.toml`** (floor + role config), so a restart **picks up any config change since launch** (new floor plugin, `enable_plugins`, `setting_sources`, effort…). An **ad-hoc** agent (no roster role) instead reproduces its captured launch. **A one-off `-- <flags>` / `--add-plugin` applies to THAT restart only** — it does *not* stick (the toml is the source of truth). To make a change durable, edit the role's toml; to carry a one-off arg across a restart, pass it again. (cmux's own app-restart restore still replays "what ran"; this is just the `fleet` verbs.)
- **`fleet rm <label> [--kill]`** — drop a label; `--kill` also stops the process + closes the tab (for throwaway agents that don't need to be durable).
- **`fleet config <role|--cwd DIR>`** — the effective config a launch would get (base settings stack + what fleet adds, with overrides flagged). Claude has no native dump.
- **`fleet mute <label>` / `fleet unmute <label>`** — stop/resume pushing a child's completions to *you*. A muted child's Stop is suppressed in the router (no inbox row, no notify, no idle-wake); you read it **on demand** (`fleet ls` shows it `MUTED` + session → `child-digest`). Use when the user drives a child directly and you shouldn't be spammed by its completions. Survives recycle; resets on archive→revive.
- **`fleet broadcast "<msg>" [--target all|all-conductors|all-children|my-children] [--no-wake] [--expect-reply] [--dry-run]`** — input-safe heads-up to live agents about an out-of-band change that won't reach them on its own (a toml/floor edit, a plugin bump). Same delivery as `peer-msg` (awareness hook → context, never the input box) + idle-wake. Informational by default; **never restarts anything** (recipients decide, usually `fleet recycle`). Self excluded; default target `all-conductors`. Canonical use: after editing `cmux-fleet.toml`, broadcast `all-conductors` so each recycles to pick it up (recycle is toml-authoritative).

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

Otherwise (the common case) children are tabs in the conductor's bottom pane. `--place workspace --group <conductor-group>` for the dedicated case; `--place pane` + fold for the tab case.

## Drive a child
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/drive-child.py <child-surface-uuid> "<prompt>"` — sends the text then a SEPARATE `send-key enter`. A trailing `\n` in `cmux send` only inserts a newline; it does NOT submit. It fails loud (non-zero) if a cmux call errors. Always target by surface UUID, never a bare `surface:N` ref.

## Retrieve a child's output
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/child-digest.py <session-frag> <N>` → last N transcript turns (the reliable source). Do NOT trust the hook store's `lastBody` for content — the cmux Notification hook clobbers it right after Stop.

## The notify flow (how children's completions reach you)
A fleet-wide daemon (`router.py --live`, run separately) watches the bus. When a child finishes, it appends the completion to the queue + fires a `cmux notify` banner. It NEVER types into your input. You become aware of it through your hooks:
- **awareness** (every turn): pending completions are injected into your context as a `[fleet] N pending` note, each with a `child-digest` command and an ack command. Handle them, then ack: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/inbox-ack.py <seq>` so they stop re-surfacing.
- **The mode dial** `$CMUX_STATE_DIR/notify-mode` (read live, no restart):
  - `passive` — awareness only; you pick up pending work on your next turn. Calm, human-driven.
  - `autodrain` — your Stop hook auto-continues you to process pending at the end of any turn.
  - `auto` — the router also idle-wakes you (when your input is empty) so you catch completions even while idle and unattended.

Key fact: nothing wakes a flat-idle conductor except `auto` mode (a Stop hook only fires on a turn you're already taking). Build around the Stop *event* + your captured state, not the upstream live status field (it's unreliable).

## Talk to a peer conductor (A2A)
Child→parent comms are automatic; talking to a **peer** conductor is a **deliberate choice**, and the peer is NOT expecting it. Same input-safe delivery as the notify flow, on a separate `peer-inbox` channel, never the input box. Because a peer send is deliberate, it reaches the recipient **promptly in both states** (see Delivery).
- **Send:** `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/peer-msg.py <peer-label> "<body>"` (peers resolve by label from the fleet registry). The message reaches the peer carrying the `@<peer>: [<you>]` identity markers.
- **Reply protocol (explicit):** a fresh message **expects a reply** by default; add `--no-reply` to make it informational; the recipient replies with `--reply-to <msg_id>` (a reply expects none further unless `--expect-reply`).
- **Delivery (both states, prompt by default):**
  - **IDLE recipient** (at the prompt, no draft) is **woken now** to handle the message, so it doesn't sit until the human's next prompt and bundle in with it. `--no-wake` opts out and leaves it for the peer's next turn. A human's draft is never clobbered.
  - **BUSY recipient** is never interrupted mid-turn, but its **drain (Stop) hook surfaces pending peer messages at the end of its current turn, ALWAYS** — not gated on `notify-mode` (unlike child completions), because a peer send is deliberate and shouldn't wait on the dial.
- **Receiving:** peer messages arrive as a `[peer] N message(s)` context note (distinct from `[fleet]` child completions), each with its reply command. Ack when handled: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/inbox-ack.py <seq> --peer`.

## Answer a child's gate (permission / question / plan)
A child's AskUserQuestion/permission/ExitPlanMode parks in the Feed (≤120s), keyed by `request_id`.
- See it: `cmux rpc feed.list '{"limit":50}'` → items with `status:"pending"` (sort by `updated_at`).
- Answer: `cmux rpc feed.question.reply '{"request_id":"<id>","selections":["<label>"]}'` (selections are option LABELS). Also `feed.permission.reply {request_id, mode:once|always|bypassPermissions|deny}`, `feed.exit_plan.reply {request_id, mode}`.

## Hard-won rules
- Send by surface **UUID only** (bare `surface:N` refs are window-scoped and shift; one misfired into a live conductor).
- Done-detection = the debounced Stop event + transcript, not the `agentLifecycle` field. `idle` is a blink; an agent at the prompt reports `needsInput`. So **wakeable = idle OR needsInput**, busy = `running`.
- Identity = surfaceId, not session_id (a relaunch gives a new session_id; the surface is the persistent seat).
- `new-workspace --group` needs a group REF (`workspace_group:N`), not the name.
