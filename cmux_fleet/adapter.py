# cmux_fleet/adapter.py — the cmux ACTION adapter, step 2 of the v2 migration (design section 4).
#
# resolve.py answers questions; THIS module owns the risky cmux actions with their guards, starting
# with the two the design names for step 2: exec-delivery (a process start is delivered as the pane
# PROCESS via respawn-pane, never as typed text) and the resume-summary menu dismisser (the one
# sanctioned screen interaction; upstream gives no flag to suppress the menu, GitHub #46751).
#
# Exec-delivery is the mechanism that killed the paste class for recycle (5cfe1ba: one argv element
# end-to-end, byte-exact at 2898 bytes, nothing a TUI can collapse; every recycle since binds in
# 2-5s with zero re-kicks). Step 2 generalizes it to launch and revive. The paste tower stays in
# cli.py untouched for a one-week soak behind the same flag (CMUX_FLEET_EXEC_LAUNCH=0 reverts all
# three verbs to paste); deleting it is step 3's business, after soak.
#
# I/O is injected by the caller (cmux runner, TUI probe, paste fallback): the cli test suite's seams
# (fleet.cmuxq, fleet._agent_surfaced, fleet._resume_menu_visible, fleet._fire_launch) stay the
# patch points, and this module stays unit-testable without a live cmux.
import shlex
import time

# claude's resume-summary menu outcomes (relocated with the dismisser; cli re-exports these names)
RESUME_DISMISSED = "dismissed"   # the summary menu rendered and we picked 'full session as-is'
RESUME_READY = "ready"           # no menu: already at a running prompt (small session)
RESUME_TIMEOUT = "timeout"       # neither appeared in the window -> caller must NOT bind/register


def exec_deliver(surf, guarded, log, *, cmux, tui_up, paste_fallback):
    """Deliver `guarded` as the PANE PROCESS via respawn-pane — no paste, no Enter, no settle race,
    no re-kick, no self-heal. The command travels as ONE argv element end-to-end.

    TRAP (live-reproduced): a bare `zsh -ilc '<cmd>'` pane DIES WITH ITS COMMAND — cmux destroys the
    whole SURFACE on exit. The chained `; exec /bin/zsh -il` is NON-NEGOTIABLE: a launch that crashes
    at startup degrades to the recoverable bare-shell husk, never a destroyed surface UUID.

    Guards: if an agent TUI (or the resume menu) is already up, firing is REFUSED — respawn-pane
    kills the pane process, so firing over a live agent that appeared since the caller's checks would
    destroy it. A respawn-pane ERROR falls back to `paste_fallback()` (the proven paste path) rather
    than leaving a bare shell with no launch at all. Returns True iff a launch was delivered by
    either mechanism."""
    if tui_up():
        log("SKIP exec-launch: an agent TUI is already up on this surface — never respawn over a live agent")
        return False
    log("exec-launch: respawning the pane with the launch as its process (no paste)")
    cmd = "/bin/zsh -ilc " + shlex.quote(guarded + "; exec /bin/zsh -il")
    out = cmux("respawn-pane", "--surface", surf, "--command", cmd)
    if "error" in (out or "").lower():
        log(f"exec-launch: respawn-pane -> {(out or '').strip()!r}; falling back to the paste path")
        return paste_fallback()
    return True


def path_guard(send_cmd):
    """Prefix the standard bin dirs so the cmux claude-wrapper can resolve the real binary whatever
    the shell-init timing did to $PATH (the 127 'claude not found' class). Harmless no-op when PATH
    is already complete, and for codex/other tools."""
    return 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"; ' + send_cmd


def resume_menu_timeout(plugin_count, base=60, per_plugin=8, ceiling=120):
    """Loadout-scaled ceiling for the resume-summary menu to appear: a heavy plugin boot can take
    30-40s+ before claude even renders it, so a fixed window closed too early (the homelab symptom)."""
    return min(ceiling, base + per_plugin * max(0, plugin_count))


def dismiss_resume_menu(surf, log, *, cmux, timeout=None, plugin_count=0, sleep=time.sleep):
    """`claude --resume` on an old/large session shows an interactive menu before resuming:
         1. Resume from summary (recommended)   2. Resume full session as-is   3. Don't ask me again
    A respawn/launch has NO human to choose, so the agent HANGS at the menu (and any bind-confirm
    false-passes on the bound-but-stuck session). Policy: ALWAYS resume FULL, never summarize/compact
    (Berg-ratified; no claude flag exists to force it, so DOWN then ENTER on the rendered menu is the
    one keystroke interaction the fleet is allowed).

    Pure timing gate, not a detection problem: poll for one of three states until a loadout-scaled
    ceiling. RESUME_DISMISSED = menu was up, 'full session as-is' picked. RESUME_READY = already at a
    running prompt (or the post-selection 'Resuming...' banner was seen and no keystroke is safe).
    RESUME_TIMEOUT = neither appeared: the caller MUST treat the seat as unbound and not register."""
    if timeout is None:
        timeout = resume_menu_timeout(plugin_count)
    end = time.time() + timeout
    resuming = False                    # saw the POST-selection 'Resuming ...' state (menu already gone)
    while time.time() < end:
        pane = cmux("capture-pane", "--surface", surf) or ""
        # ONLY the LIVE menu is actionable — it shows BOTH option labels at once. The post-selection
        # 'Resuming the full session' banner means the menu is GONE; a keystroke there goes astray.
        if "Resume from summary" in pane and "Resume full session as-is" in pane:
            log("resume-summary menu detected -> picking 'Resume full session as-is' (full, never compact)")
            cmux("send-key", "--surface", surf, "down")
            sleep(0.5)
            cmux("send-key", "--surface", surf, "enter")
            return RESUME_DISMISSED
        if "Context Remaining" in pane or "bypass permissions" in pane:
            return RESUME_READY         # small session resumed straight to a running prompt
        if "Resuming the full session" in pane:
            resuming = True             # menu already resolved -> resume underway; don't touch keys
        sleep(1)
    if resuming:
        log("resume in progress (summary menu already cleared); proceeding to bind")
        return RESUME_READY
    log(f"WARN: resume launched but neither the summary-menu nor a running prompt appeared within "
        f"{timeout:.0f}s (plugin_count={plugin_count}); NOT binding -- surface is still booting or "
        f"wedged behind the menu. Re-run once it settles.")
    return RESUME_TIMEOUT
