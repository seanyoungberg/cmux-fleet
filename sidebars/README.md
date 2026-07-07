# Fleet sidebar surfaces

Three independent ways to *see* a running fleet inside cmux. Pick any or all — they don't depend on each
other. Nothing here is berg-specific; it works for any fleet.

## 1. Dock board — `fleet vitals --watch`  (no install)

The full triage table (label · state · ctx-left · model · effort · cwd · idle · last) in a cmux **Dock**
terminal pane. Non-flickering: it clears+reprints only when the board's change-fingerprint moves.

Add to `.cmux/dock.json` (project) or `~/.config/cmux/dock.json` (global):

```json
{ "controls": [ { "id": "fleet", "title": "Fleet", "command": "fleet vitals --watch --interval 2", "height": 320 } ] }
```

Optionally pair with the native Feed as a second control: `"command": "cmux feed tui --opentui"`.

## 2. Built-in-sidebar pill strip — `fleet paint`  (no install, no beta flag)

`fleet paint` pushes native cmux widgets onto the built-in sidebar off live fleet state:

- one **status pill** per agent (`set-status`) — on a shared/conductor workspace they stack into a per-agent
  strip; on a per-agent workspace the pill shows the **state** (the workspace title already carries the label).
- one **context bar** per workspace (`set-progress`) — the worst (lowest-remaining) agent on it.
- vanished agents get their pill cleared, so the strip never accumulates ghosts.

It's on-change-only (no churn). Run it on a loop to keep it live: `while true; do fleet paint; sleep 3; done`
(or wire it into your router/heartbeat). Zero install — this is just the built-in sidebar.

## 3. Custom rich sidebar — `fleet.swift`  (opt-in; Custom Sidebars beta)

A vibe-coded SwiftUI-style sidebar that renders the whole board as **conductor→worker groups** with a state
icon/color, a threshold-colored context bar (green >50 / amber 30–50 / red <30, drains right), model·effort,
cwd, a tool marker, the latest message, and tap-to-focus.

Because a custom sidebar can only read cmux's own workspace fields (not fleet state), the board rides in
through a **workspace `description` blob**. Enable it with `fleet paint --sidebar` (or `FLEET_SIDEBAR_BLOB=1`)
— OFF by default because that blob shows as the marker workspace's *subtitle* in the built-in sidebar.

Install:

```sh
cp sidebars/fleet.swift ~/.config/cmux/sidebars/fleet.swift
# Settings → Beta features → Custom sidebars  (once)
cmux sidebar validate fleet && cmux sidebar select fleet
# feed it:
while true; do fleet paint --sidebar; sleep 3; done
```

## Known gap (enabling fix wanted)

`fleet.swift` groups agents by a pushed `parent` field because **neither** cmux's sidebar binding **nor**
fleet's own snapshot cleanly exposes per-agent workspace + conductor-group identity:

- cmux's `workspaces` binding has **no group-membership field**.
- fleet's snapshot `ws` = the hook-store `workspaceId`, which **collapses a conductor's group members to one
  workspace id** (observed: 4 group agents all reporting the conductor's ws). So per-agent paint can't key
  onto the individual workspaces even when cmux has them separated.

A small fleet-state change — resolve/expose each agent's **current individual workspace ref + its group** in
`snapshot()` — would let both the pill paint and the custom sidebar key on real per-agent identity (and unlock
native `Reorderable(move: "workspace.reorder")` + native group-collapse). Tracked for a follow-up.
