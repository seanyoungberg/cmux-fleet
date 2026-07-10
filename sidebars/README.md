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

A SwiftUI-style sidebar that renders the board as **collapsible conductor→worker groups**: a state icon/color,
a threshold-colored context bar (green >50 / amber 30–50 / red <30), the latest message, an unread badge, and
tap-to-focus.

**Native-first.** Now that each agent owns a workspace, nearly everything comes from cmux's own fields —
`w.title` (label), `w.progress` (ctx bar), `w.latestMessage`, `workspace.select(w.id)` (tap). The only things
fleet has to push are **state, parent and the collapse bit**, and they ride in a short workspace `description`
that reads as prose in the built-in sidebar instead of clobbering it:

| workspace | description |
|---|---|
| child     | `working · ↳berg-sandbox` |
| conductor | `ready · ▾berg-sandbox`  (`▸` = collapsed) |
| shared ws | `… · +2`  (agents still sharing one workspace) |

A conductor carries **its own label**, not the word `conductor`: a conductor's workspace *title* is decorated
(`Conductor - cmux-advisor`), so a child can never match its parent by title. Title is a display string, not an
identity. The glyph alone encodes kind.

**Collapse without `@State`** (the interpreter has none): the chevron rewrites that workspace's description with
the glyph flipped (`workspace.action` / `set-description`); `fleet paint` reads the glyph back and carries it
forward, so a repaint never clobbers the choice.

Feed it: `fleet paint --sidebar` (or `FLEET_SIDEBAR_BLOB=1`). OFF by default.

```sh
# Settings → Beta features → Custom sidebars  (once)
cmux sidebar validate fleet && cmux sidebar select fleet
while true; do fleet paint --sidebar; sleep 3; done
```

> `fleet paint` **without** `--sidebar` CLEARS the descriptors, and `fleet paint --help` is unhandled so it
> falls through to a real paint. Don't run a bare paint while the custom sidebar is live.

## Deploy — the repo is the single source of truth

`~/.config/cmux/sidebars/fleet.swift` is a **symlink** into this repo. That symlink *is* the deploy mechanism:
edit the repo file, and the live sidebar follows (hot reload still works, `cmux sidebar validate` resolves
through the link). Treat the sidebar as code — commit it.

```sh
ln -sfn "$PWD/sidebars/fleet.swift" ~/.config/cmux/sidebars/fleet.swift
```

Two rules, both learned the hard way:

- **Never keep a second copy under `~/.config`.** A regular file there silently diverges from the repo (ours
  drifted 1 KB), and the untracked side is one prune or one wrong-copy edit from being lost.
- **Point the symlink at the MAIN checkout, never at a git worktree.** A worktree is ephemeral; `git worktree
  prune` leaves a dangling symlink and the sidebar dies. Corollary: editing `sidebars/fleet.swift` *inside a
  worktree* does **not** hot-reload the live sidebar — it resolves to main. Land sidebar changes on main.

## Authoring the `.swift` — interpreter gotchas

The custom-sidebar file is **interpreted**, not compiled, and it supports only a subset of Swift. Worse,
`cmux sidebar validate` only **parses**; it never exercises the code, so it happily reports `OK` for a sidebar
that renders nothing. There is no eval/render RPC either (`extension.sidebar.snapshot` and `sidebar.custom.open`
are the only two), so the render can only be confirmed by eye. Budget for that.

Three rules, each of which cost an hour. Every one fails **silently** — the sidebar renders empty, or a field
reads as absent, and nothing anywhere reports an error:

- **Reach every optional with `if let`. Never `!= nil`.** The interpreter evaluates `x != nil` / `x == nil` to
  *nothing*, so `if w.description != nil && w.description != "" { … }` is never true and the field looks absent
  even when it is populated. This one is nasty: cmux's own shipped `status-board.swift` uses the `!= nil` form
  (line 29: `w.progress != nil && w.progress.value != nil`), so **copying the example reproduces the bug**.

      func descOf(_ w) -> String {          // right
        if let d = w.description { return d }
        return ""
      }

  Corollary: `Text("\(x == nil)")` interpolates to an empty string, which is itself the tell — if a probe
  prints `dNil=` with nothing after it, you are looking at this bug.
- **No top-level `let`.** A file-scope `let CHILD = " · ↳"` is *not resolvable from inside a `func`*. The func
  silently misbehaves (ours returned `false` for every workspace, so the board filtered itself to nothing).
  Declare `let` only **inside a func** or **inside the view body**.
- **Never return an array from a helper.** `func rows() -> [Any]` does not work. Bind arrays with `let` in the
  **view body** and pass them into view helpers as parameters (`func group(_ c, _ kids) -> some View`).

Prefer proven views (`ProgressView(value:total:).tint(...)`) over hand-rolled shapes.

**Debugging.** `cmux sidebar-state` is a dump of the sidebar *layer's* own state — it is **not** the SwiftUI
binding surface, and its field list says nothing about what binds. Don't infer the transport from it. Instead,
make the empty state **self-diagnosing**: print `workspaces.count` and interpolate the raw field
(`Text("[\(descOf(w))]")`). One screenshot then separates binding vs. data vs. matching logic. Interpolating the
raw optional is the reliable probe; interpolating a `== nil` comparison is not.
