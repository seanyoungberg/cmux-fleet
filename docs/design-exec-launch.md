# Design note: exec the recycle launch as the pane process (kill the paste class)

Status: **DESIGN ONLY** (cmux-advisor 2026-07-09: prove A+B first). No code implements this yet.

## Problem

The recycle relaunch is a **paste**: `respawn-pane` starts a bare login shell, then we `cmux send`
the composed launch command into it and press Enter. Everything fragile about the recycle tail lives
in that paste:

- **Large-paste collapse.** The composed command is huge (a full inline `--settings` JSON plus
  `--plugin-dir` flags — several KB). cmux/claude collapse it into a `[Pasted text #1]` block that can
  render mangled and never execute. Confirmed live 2026-07-09 on berg-sandbox: fleet's flagged launch
  (with `--model 'claude-fable-5[1m]' --effort xhigh`) sat inert in the input box while a **flagless**
  claude (user-default model, no effort pin) filled the seat. The same session it bit a second time on
  a different surface: a long `drive-child` brief from cmux-advisor vanished the same way.
- **The enter-race.** The Enter can land before the paste settles; `_fire_launch` re-kicks bare Enters
  to compensate (bounded, but still a heuristic).
- **The self-heal.** Because a paste can silently fail, the tail needs a re-fire path — which is
  exactly what pasted the launch into a live TUI when the confirm misdetected (fixed by B, but the
  paste class is why the machinery exists at all).

## Proposal

Make the launch the **pane process itself** — no paste, no Enter, no settle race:

```
cmux respawn-pane --surface <S> --command "/bin/zsh -ilc '<guarded launch>'"
```

- `zsh -il` keeps the interactive login shell semantics the current design already requires (cmux
  exposes `claude` as a zsh function via its shell integration, sourced from `~/.zshrc`; the PATH
  guard prefix stays for the wrapper's `find_real_claude`).
- `-c '<launch>'` executes the composed command directly. The command arrives as ONE argv element
  through cmux's API — never rendered into a TUI input box, so there is nothing to collapse, settle,
  or re-kick.
- `_poll_session_back` (live-pid confirm, fix A) remains the bind verification unchanged.

## What it deletes

`_fire_launch` (paste + enter + re-kick loop), the self-heal re-fire, and the whole
launch-as-inert-draft failure class the 07-04/07-07/07-09 incidents share. The B guard stays as
defense-in-depth for the resume-menu path and any residual send-based flows (prime, drive-child).

## Open questions to validate live before adoption

1. **Quoting.** The composed `send_cmd` contains single quotes (`--model 'claude-fable-5[1m]'`) and
   inline JSON. Embedding it in `zsh -ilc '<...>'` needs one shlex-quote layer; cmux passes
   `--command` verbatim to the pane spawner, so validate no second shell expansion happens en route.
2. **Exit semantics.** Today the shell survives a crashed launch (the self-heal re-fires into it).
   With exec-style launch, a crash ends the pane process — confirm cmux leaves a respawnable pane
   (and that `_escalate_recycle_failure` fires on the no-bind WARN as the recovery signal).
3. **Shell-init timing.** `-c` runs after `.zshrc` completes, which should *eliminate* the
   PATH-not-ready class the guard prefix works around — verify on a cold machine.
4. **Resume menu.** `--resume <id>` boots into the summary-vs-full menu; `_resume_and_gate` drives it
   via send-keys today and is unaffected, but re-test the interaction with an exec'd pane.
5. **Codex/other tools.** Same exec shape should work (`codex resume <id> ...`); validate the lazy
   bind path still registers on first turn.

## Migration sketch

Feature-flag it (`CMUX_FLEET_EXEC_LAUNCH=1`) in `_recycle_exec_one` only; A/B machinery stays as the
fallback path. Promote to default after a week of live recycles across claude + codex seats; then
delete the paste path.
