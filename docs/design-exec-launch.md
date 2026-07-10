# Exec-style recycle launch (the paste class, killed)

Status: **IMPLEMENTED** (2026-07-09, `dev/exec-launch`), default **ON** for `fleet recycle`.
Kill switch: `CMUX_FLEET_EXEC_LAUNCH=0|false|off` falls back to the paste path.

## Problem (history)

The recycle relaunch was a **paste**: `respawn-pane` started a bare login shell, then the launch
command was `cmux send`-ed into it and submitted with Enter. Everything fragile about the recycle
tail lived in that paste, and it was patched four times without removing the mechanism
(launch-verify + never-bound sweep v0.7.0; pid-aware confirm rounds 1 and 2; the A+B+D live-pid
confirm + TUI guard + escalation) — plus `drive-child` is an entire CLI verb invented to route
around the same enter-race:

- **Large-paste collapse.** The composed command is huge (inline `--settings` JSON + `--plugin-dir`
  flags, several KB). cmux/claude collapse it into a `[Pasted text #1]` block that renders mangled
  and never executes. Bit live twice on 2026-07-09 alone: four berg-sandbox recycles (a **flagless**
  claude on the default model filled the seat while fleet's `--model 'claude-fable-5[1m]'` launch sat
  inert in the input box) and a vanished `drive-child` brief.
- **The enter-race.** The Enter can land before the paste settles; `_fire_launch` re-kicks bare
  Enters to compensate.
- **The self-heal.** Because a paste can silently fail, the tail needed a re-fire path — which is
  what pasted the launch into a live TUI when the confirm misdetected (B's guard now refuses that).

## Implementation

The launch is delivered as the **pane process** via a **second** `respawn-pane` (`cli._exec_launch`):

```python
cmd = "/bin/zsh -ilc " + shlex.quote(guarded + "; exec /bin/zsh -il")
cmuxq("respawn-pane", "--surface", surf, "--command", cmd)
```

- **One `shlex.quote` layer, one argv element end-to-end** (OQ1 — live-probed SOLVED): cmuxq passes
  argv, cmux hands `--command` verbatim to the pane spawner. `--model 'claude-fable-5[1m]'
  --effort xhigh` and inline `{"enabledPlugins":[...]}` arrive **verbatim**; a **2898-byte** argv
  element carrying 2810 bytes of inline JSON executed byte-exact. No second shell expansion, nothing
  a TUI can collapse.
- **The chained `; exec /bin/zsh -il` is NON-NEGOTIABLE** (OQ2 — live-probed, and the original
  design note had this **wrong**): a bare `zsh -ilc '<launch>'` pane **dies with its command** — when
  the process exits, cmux **destroys the whole surface** (`not_found: Surface not found`), so a
  launch that crashes at startup would vaporize the seat and its surface UUID, strictly worse than
  the paste path's recoverable bare shell. With the chain, a crashed launch degrades to exactly the
  old bare-shell husk (`reap-surfaces` handles it) and the surface survives.
- **Why a second respawn, not the launch on the verify respawn:** `_respawn_and_verify` must confirm
  the OLD agent dead **before** any launch exists. If the launch rode the first respawn, the new
  agent's live pid would poison `_confirmed_gone`'s no-live-pid check — the verify would time out and
  the direct-kill fallback would SIGINT the agent just launched. The bare-shell respawn stays the
  verify vehicle; `_exec_launch` replaces only the delivery.
- **B carries over as defense-in-depth:** `respawn-pane` kills the pane process, so `_exec_launch`
  refuses to fire when an agent TUI (or resume menu) is already up — same guard, worse blast radius
  without it. `_fire_launch` remains intact as (a) the `CMUX_FLEET_EXEC_LAUNCH=0` fallback and (b)
  the automatic degradation when the exec respawn itself errors. `prime`, `drive-child`, and the
  resume-menu keystrokes still `send` — unchanged.
- **A is untouched:** `_poll_session_back`'s live-pid confirm remains the bind verification.
- **No self-heal on the exec path, by design:** `-c` runs after shell init completes, so the
  PATH-not-ready crash class can't happen; a no-bind after an exec'd launch is a real failure that
  escalates (D) rather than being papered over by a re-exec.

## Remaining open questions (watch on first live runs)

- **OQ3 — shell-init timing:** `-ilc` should *eliminate* the PATH-not-ready class (the command runs
  after `.zshrc`); the PATH-guard prefix is kept anyway. Verify on a cold machine.
- **OQ4 — the `--resume` summary-vs-full menu on an exec'd pane:** `_resume_and_gate` drives it via
  send-keys after the launch; capture-pane is proven on exec'd panes and send-keys is the same
  primitive family, but **watch the first live berg-sandbox resume-recycle with `recycle.log`
  tailing** — this is the one that matters.
- **OQ5 — codex lazy-bind:** the exec path is tool-agnostic (same composed command); the lazy branch
  never polls. Validate on the first codex recycle.

## Retirement plan

After a week of live recycles across claude + codex seats with no `CMUX_FLEET_EXEC_LAUNCH=0`
fallback needed: delete `_fire_launch`'s recycle callsites + the paste self-heal (the launch verb's
`_send_launch_and_confirm` is a separate path and out of scope here).
