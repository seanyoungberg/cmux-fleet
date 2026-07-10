# tests/test_recycle.py — regression coverage for the recycle confirm logic. `_poll_session_back` is
# what decides whether a recycled agent actually re-bound a session. FRESH mode confirms on the
# LIVE-PID truth (_live_bound_sid, 2026-07-09 fix): the old sid-exclusion confirm rode poll_session's
# arbitrary-first-record fallback, which kept returning the dead lingering ghost while a healthy fresh
# agent sat on the seat unconfirmed — four identical berg-sandbox misdetects in one day, each ending
# with the self-heal pasting the launch into the live TUI as a garbled draft.
# These are pure unit tests: the hook store / lifecycle / time.sleep are monkeypatched, no cmux.
import os
import sys


from cmux_fleet import cli as fleet           # noqa: E402  (never popped by other test files)

# NOTE: `_poll_session_back` does `import fleet_state as fs` INTERNALLY, and another test module
# (test_features) pops `fleet_state` from sys.modules on teardown. So the tests below import
# fleet_state *inside* the test (after any popping) to patch the SAME cached object the function gets.


def test_fresh_confirms_on_live_pid_record(monkeypatch):
    # the ghost has the HIGHER updatedAt: it is the DEAD PID (not freshness) that must exclude it.
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet_state, "read_hook_store", lambda: {"sessions": {
        "g": {"sessionId": "stale-pre", "surfaceId": "S", "pid": None, "updatedAt": 100},
        "f": {"sessionId": "brand-new", "surfaceId": "S", "pid": os.getpid(), "updatedAt": 50}}})
    got = fleet._poll_session_back("S", "old", "fresh", timeout=5)
    assert got == "brand-new"                     # the record with a LIVE pid IS the running agent


def test_fresh_ignores_dead_ghost_records(monkeypatch):
    # only dead-pid records on the seat (the crashed-launch case) -> no false confirm -> "" at timeout.
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet_state, "read_hook_store", lambda: {"sessions": {
        "g": {"sessionId": "stale-pre", "surfaceId": "S", "pid": None, "updatedAt": 100}}})
    got = fleet._poll_session_back("S", "old", "fresh", timeout=0.05)
    assert got == ""                              # a crashed launch resolves to no-session, not success


def test_fresh_does_not_confirm_a_live_old_sid_zombie(monkeypatch):
    # cmux restart-resume interference can put the OLD session back live on the seat. Live it may be,
    # fresh it is not: never declare a fresh bind on old_sid (fall through to WARN + escalation).
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet_state, "read_hook_store", lambda: {"sessions": {
        "z": {"sessionId": "old", "surfaceId": "S", "pid": os.getpid(), "updatedAt": 100}}})
    got = fleet._poll_session_back("S", "old", "fresh", timeout=0.05)
    assert got == ""


def test_resume_confirms_old_sid_via_lifecycle(monkeypatch):
    # resume keeps the SAME sid; confirmation is the surface going live again, not a new sid. Confirm =
    # surface_has_live_agent: a live lifecycle AND a live pid (pid-aware, round 2).
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "old")
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "idle")
    monkeypatch.setattr(fleet_state, "surface_has_live_pid", lambda surf: True)   # resumed agent is live
    got = fleet._poll_session_back("S", "old", "resume", timeout=5)
    assert got == "old"


def test_resume_waits_while_lifecycle_dead(monkeypatch):
    # resume does NOT confirm while the surface lifecycle is still empty/ended.
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "old")
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "ended")
    got = fleet._poll_session_back("S", "old", "resume", timeout=0.05)
    assert got == ""


def test_resume_does_not_falseconfirm_on_dead_pid_ghost(monkeypatch):
    # round-2 gap (2026-07-06): a leftover FROZEN 'running' record on a DEAD pid must NOT false-confirm the
    # re-bind before the resumed agent has actually booted. Lifecycle reads live but the pid is dead ->
    # surface_has_live_agent is False -> keep waiting -> "" within the (tiny) timeout.
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "old")
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "running")         # frozen non-terminal...
    monkeypatch.setattr(fleet_state, "surface_has_live_pid", lambda surf: False)  # ...but the pid is DEAD
    got = fleet._poll_session_back("S", "old", "resume", timeout=0.05)
    assert got == ""


# --- resume-summary menu dismiss (Fix 2: PURE TIMING gate, event-driven poll, scaled ceiling) -----
def test_resume_menu_timeout_scales_with_plugin_count():
    # base is generous even at 0 plugins; heavier loadouts stretch it, bounded by the ceiling.
    assert fleet._resume_menu_timeout(0) == 60
    assert fleet._resume_menu_timeout(6) == 60 + 8 * 6          # homelab-weight -> longer window
    assert fleet._resume_menu_timeout(0) < fleet._resume_menu_timeout(6)
    assert fleet._resume_menu_timeout(1000) == 120             # capped at the ceiling
    assert fleet._count_plugin_dirs("x --plugin-dir a y --plugin-dir b") == 2


def test_dismiss_picks_full_when_menu_present(monkeypatch):
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    keys = []
    def fake_cmuxq(*args):
        if args[:1] == ("capture-pane",):
            # the LIVE menu shows BOTH option labels at once
            return "1. Resume from summary (recommended)\n2. Resume full session as-is\n3. Don't ask again"
        keys.append(args)
        return ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    got = fleet._dismiss_resume_summary_prompt("S", lambda m: None, timeout=5)
    assert got == fleet.RESUME_DISMISSED
    # DOWN (option 1 -> 2) then ENTER = 'Resume full session as-is'
    assert ("send-key", "--surface", "S", "down") in keys
    assert ("send-key", "--surface", "S", "enter") in keys


def test_dismiss_does_not_fire_keys_on_in_progress_banner(monkeypatch):
    # FIX 4: 'Resuming the full session' is the POST-selection / in-progress state, NOT the menu. The
    # old check fired down/enter on it -> a stray keystroke into a no-longer-menu surface. Now we must
    # NEVER send keys for it; the resume is underway, so the gate resolves READY (safe to bind).
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    keys = []
    def fake_cmuxq(*args):
        if args[:1] == ("capture-pane",):
            return "Resuming the full session as-is..."       # in-progress banner, menu already gone
        keys.append(args)
        return ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    got = fleet._dismiss_resume_summary_prompt("S", lambda m: None, timeout=0.05)
    assert got == fleet.RESUME_READY                          # in-progress -> proceed, not TIMEOUT
    assert not any(a[:1] == ("send-key",) for a in keys)      # no stray down/enter fired


def test_dismiss_ready_when_running_prompt(monkeypatch):
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "cmuxq",
                        lambda *a: "Context Remaining 42%" if a[:1] == ("capture-pane",) else "")
    got = fleet._dismiss_resume_summary_prompt("S", lambda m: None, timeout=5)
    assert got == fleet.RESUME_READY                          # no menu -> nothing to dismiss


def test_dismiss_times_out_when_still_booting(monkeypatch):
    # neither menu nor prompt within the ceiling (heavy loadout still booting) -> TIMEOUT, not READY.
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "cmuxq",
                        lambda *a: "loading plugins..." if a[:1] == ("capture-pane",) else "")
    got = fleet._dismiss_resume_summary_prompt("S", lambda m: None, timeout=0.05)
    assert got == fleet.RESUME_TIMEOUT


def test_gate_blocks_bind_on_timeout(monkeypatch):
    # THE register-skipped path: a timed-out dismiss -> gate False -> caller must NOT bind/register.
    monkeypatch.setattr(fleet, "_dismiss_resume_summary_prompt",
                        lambda *a, **k: fleet.RESUME_TIMEOUT)
    assert fleet._resume_and_gate("S", "cmd --plugin-dir a", "claude", "sess", lambda m: None) is False


def test_gate_allows_bind_when_resolved(monkeypatch):
    # dismissed OR already-ready both clear the gate; fresh / non-claude launches are a no-op pass.
    monkeypatch.setattr(fleet, "_dismiss_resume_summary_prompt",
                        lambda *a, **k: fleet.RESUME_DISMISSED)
    assert fleet._resume_and_gate("S", "cmd", "claude", "sess", lambda m: None) is True
    monkeypatch.setattr(fleet, "_dismiss_resume_summary_prompt",
                        lambda *a, **k: fleet.RESUME_READY)
    assert fleet._resume_and_gate("S", "cmd", "claude", "sess", lambda m: None) is True
    # non-claude tool / no session -> gate never runs the dismiss, always safe to proceed
    assert fleet._resume_and_gate("S", "cmd", "codex", "sess", lambda m: None) is True
    assert fleet._resume_and_gate("S", "cmd", "claude", "", lambda m: None) is True


# --- Fix 1: --force is the escape hatch — it must short-circuit the ENTIRE quiet-gate (respawn now, no
#     wait), not just the draft check. A desynced/STALE surface's lifecycle never reads idle/needsInput/
#     unknown, so before this --force could NEVER satisfy the lifecycle check and burned the full 180s to
#     an ABORT, identical to a non-force run. Non-force behavior is UNCHANGED. ------------------------
def test_quiet_gate_force_short_circuits_even_when_running(monkeypatch):
    from cmux_fleet import state as fleet_state
    slept = []
    monkeypatch.setattr(fleet.time, "sleep", lambda s: slept.append(s))
    # a desynced/running surface: lifecycle never goes quiet AND a human draft would block too.
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "running")
    monkeypatch.setattr(fleet, "_input_draft_nonempty", lambda surf: True)
    # force -> True IMMEDIATELY, no wait loop and no 2s settle sleep at all.
    assert fleet._quiet_gate("S", 180, force=True) is True
    assert slept == []                        # short-circuited before the wait loop AND the settle


def test_quiet_gate_noforce_blocks_while_running(monkeypatch):
    # non-force on a GENUINELY LIVE 'running' surface still times out (False) — the no-half-kill guard is
    # UNCHANGED. 'Genuinely live' = a live pid backs the record; the pid-aware quiet check only treats a
    # 'running' record as quiet when its process is DEAD (a frozen ghost), which this is not.
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "running")
    monkeypatch.setattr(fleet_state, "surface_has_live_pid", lambda surf: True)   # a real live turn
    monkeypatch.setattr(fleet, "_input_draft_nonempty", lambda surf: False)
    assert fleet._quiet_gate("S", 0.05, force=False) is False


def test_quiet_gate_noforce_passes_a_dead_pid_running_ghost(monkeypatch):
    # THE dead-agent recovery path: a record frozen 'running' whose PROCESS is dead (a SessionEnd-less
    # death — SIGKILL or the store-write race) is NOT a live turn, so a plain `fleet recycle` must clear
    # the gate immediately instead of blocking 180s to an ABORT (which forced --force before the fix).
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "running")
    monkeypatch.setattr(fleet_state, "surface_has_live_pid", lambda surf: False)  # process is dead -> ghost
    monkeypatch.setattr(fleet, "_input_draft_nonempty", lambda surf: False)
    assert fleet._quiet_gate("S", 0.05, force=False) is True


def test_quiet_gate_noforce_gated_by_nonempty_draft(monkeypatch):
    # idle lifecycle but a human draft in the box -> non-force must NOT respawn (times out False).
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "idle")
    monkeypatch.setattr(fleet, "_input_draft_nonempty", lambda surf: True)
    assert fleet._quiet_gate("S", 0.05, force=False) is False


# --- fresh-after-cwd-move: a FRESH recycle must PERSIST the new cwd so the next default RESUME finds
#     the new session (codex residual-blocker: compose-time pin was right, persistence was missing) -----
def test_fresh_recycle_persists_new_cwd_then_resume_composes_from_new(fs, monkeypatch):
    from cmux_fleet import state as fleet_state
    # role w moved to /NEW; registry still records /OLD + the old session.
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S",
                               "cwd": "/OLD", "session": "claude-OLD", "kind": "child"})
    # stub the respawn/bind machinery so _recycle_exec_one runs with no live surface
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "cmuxq", lambda *a, **k: "")
    monkeypatch.setattr(fleet, "_quiet_gate", lambda *a, **k: True)
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "ended")            # old session confirmed dead
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "")          # no stale pre_sid
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")       # fresh bind
    payload = {"label": "w", "surface": "S", "send_cmd": "cd /NEW && claude x", "mode": "fresh",
               "tool": "claude", "force": True, "prime": None, "old_session": "OLD", "cwd": "/NEW"}
    assert fleet._recycle_exec_one(payload) == 0
    # PERSISTED: registry cwd is now /NEW (was /OLD) and the fresh session is bound
    assert fleet_state.live_get("w")["cwd"] == "/NEW"
    assert fleet_state.live_get("w")["session"] == "claude-NEWSID"
    # a subsequent DEFAULT (resume) recycle now composes from /NEW (where the new session lives)
    monkeypatch.setattr(fleet, "_is_roster", lambda role: True)
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {})
    monkeypatch.setattr(fleet, "load_config", lambda: {"role": {"w": {}}})
    monkeypatch.setattr(fleet, "resolve", lambda *a: {
        "tool": "claude", "role": "w", "label": "w", "kind": "child", "place": "tab", "group": "",
        "cwd": "/NEW", "plugins": [], "flags": [], "env": {}, "settings": "",
        "setting_sources": ""})
    resume, _ = fleet._compose_recycle_cmd("w", fleet_state.live_get("w"), [], [], "resume", "")
    assert "cd /NEW" in resume and "--resume NEWSID" in resume


# --- verify-fresh-shell before firing the launch (recycle-verify-respawn fix) -----------------------
# respawn-pane is ASYNC: its "OK" means "accepted", not "old process killed". The old bug fired the
# launch on a fixed sleep(3) that could undershoot a slow/hung kill, typing the launch into the still-
# live old TUI (berg-sandbox's 9h silent self-recycle). The fix polls the surface's agentLifecycle for a
# terminal state ('', '-', 'ended') before ever calling _fire_launch, with a cmux-independent SIGINTx2
# fallback (same mechanism cmd_archive/cmd_rm use) if respawn-pane itself errors or never confirms.
def _base_payload(surf="S", old_sid="OLD"):
    return {"label": "w", "surface": surf, "send_cmd": "cd /x && claude", "mode": "fresh",
            "tool": "claude", "force": True, "prime": None, "old_session": old_sid, "cwd": "/x"}


def test_verify_waits_out_a_slow_kill_before_launching(fs, monkeypatch):
    # lifecycle reads non-terminal (old claude still alive) for two polls, THEN flips to 'ended' -- the
    # launch must not fire until that flip, proving a poll rather than a fixed sleep.
    from cmux_fleet import state as fleet_state
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    sent = []
    def fake_cmuxq(*a, **k):
        if a and a[0] == "respawn-pane":
            return "OK"
        sent.append(a)
        return ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "")
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")
    # old agent still shows a LIVE pid (respawn's kill is slow) so the pid branch of the verify never
    # short-circuits — confirmation must come from the terminal lifecycle flip, proving the poll. The
    # graceful-close pre-step is a no-op here (no pid on record to close).
    monkeypatch.setattr(fleet_state, "surface_has_live_pid", lambda surf: True)
    monkeypatch.setattr(fleet, "_pid_for_surface", lambda surf: None)
    states = iter(["idle", "idle", "ended"])
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: next(states, "ended"))
    assert fleet._recycle_exec_one(_base_payload()) == 0
    assert next(states, "exhausted") == "exhausted"          # all 3 readings were consumed by the poll
    assert any(c[0] == "send" for c in sent)                 # the launch DID fire, once confirmed dead
    assert not any(c[0] == "notify" for c in sent)


def test_respawn_error_falls_back_to_direct_kill_then_succeeds(fs, monkeypatch):
    # respawn-pane errors on the first attempt (the confirmed 05:44 failure: 'Command timed out'). The
    # fallback SIGINTs the old pid directly (cmux-independent) and re-respawns; once that confirms, the
    # launch proceeds normally -- this is NOT the abort path.
    from cmux_fleet import state as fleet_state
    import signal
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    respawn_calls, other_calls = [], []
    def fake_cmuxq(*a, **k):
        if a and a[0] == "respawn-pane":
            respawn_calls.append(a)
            return "Error: Command timed out" if len(respawn_calls) == 1 else "OK"
        other_calls.append(a)
        return ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "")
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "ended")   # dead by the time we actually poll
    monkeypatch.setattr(fleet, "_pid_for_surface", lambda surf: 4242)
    # graceful-close pre-step sees the pid as already gone -> skips (no SIGINT); this test isolates the
    # respawn-error -> direct-kill FALLBACK path, whose SIGINTx2 is the only kill we expect to see.
    monkeypatch.setattr(fleet_state, "pid_alive", lambda pid: False)
    killed = []
    monkeypatch.setattr(fleet.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert fleet._recycle_exec_one(_base_payload()) == 0
    assert len(respawn_calls) == 2                            # primary attempt + fallback re-respawn
    assert killed == [(4242, signal.SIGINT), (4242, signal.SIGINT)]   # SIGINT x2, cmux-independent
    assert any(c[0] == "send" for c in other_calls)            # launch fired after the fallback confirmed
    assert not any(c[0] == "notify" for c in other_calls)      # not an abort


def test_respawn_fails_even_after_fallback_aborts_without_launch(fs, monkeypatch):
    # respawn-pane errors on BOTH the primary attempt and the direct-kill fallback -> ABORT: never type
    # the launch into a possibly-live TUI, fire a desktop notify, and log the abort for the operator.
    from cmux_fleet import state as fleet_state
    calls = []
    def fake_cmuxq(*a, **k):
        calls.append(a)
        if a and a[0] == "respawn-pane":
            return "Error: Command timed out"
        return ""
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "_pid_for_surface", lambda surf: 4242)
    monkeypatch.setattr(fleet.os, "kill", lambda pid, sig: None)
    logged = []
    monkeypatch.setattr(fleet_state, "log_event", lambda event, **fields: logged.append((event, fields)))
    assert fleet._recycle_exec_one(_base_payload()) == 1
    assert not any(c[0] == "send" for c in calls)              # launch NEVER sent
    assert any(c[0] == "notify" for c in calls)                # desktop banner fired
    assert logged and logged[0][0] == "recycle_abort"


# --- THE dead-agent brick, reproduced (2026-07-05 berg-sandbox; root-caused + fixed 2026-07-06) -------
# A self-recycle left the seat DEAD with a hook-store record FROZEN at 'running' + pid None: no clean
# SessionEnd fired (a SIGKILL-class death, or a cmux SessionEnd store-write race under load), so the
# lifecycle never reached a terminal value. The OLD verify confirmed 'old agent gone' ONLY via a terminal
# lifecycle string -> it never matched -> EVERY recycle (even --force) aborted forever ('old session still
# ALIVE'). The pid-aware verify fixes it: a dead/None pid is conclusive proof the agent is gone.
def test_dead_agent_frozen_running_pid_none_recycles_without_force(fs, monkeypatch):
    from cmux_fleet import state as fleet_state
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    sent = []
    def fake_cmuxq(*a, **k):
        if a and a[0] == "respawn-pane":
            return "OK"
        sent.append(a)
        return ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    # THE incident fingerprint: lifecycle frozen NON-terminal 'running', but the process is DEAD.
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "running")
    monkeypatch.setattr(fleet_state, "surface_has_live_pid", lambda surf: False)   # pid dead/None -> gone
    monkeypatch.setattr(fleet, "_surface_pids", lambda surf: set())                # no live pid snapshot
    monkeypatch.setattr(fleet, "_pid_for_surface", lambda surf: None)              # nothing to gracefully close
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "")
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")
    logged = []
    monkeypatch.setattr(fleet_state, "log_event", lambda event, **f: logged.append((event, f)))
    # NO --force: recovery must work on the DEFAULT path (force was the pre-fix manual workaround). The
    # pid-aware quiet-gate clears the frozen 'running' immediately (dead process = no live turn), and the
    # pid-aware verify confirms the old agent gone, so the relaunch proceeds.
    payload = {"label": "w", "surface": "S", "send_cmd": "cd /x && claude", "mode": "fresh",
               "tool": "claude", "force": False, "prime": None, "old_session": "OLD", "cwd": "/x"}
    assert fleet._recycle_exec_one(payload) == 0
    assert any(c[0] == "send" for c in sent)                  # launch DID fire (confirmed + relaunched)
    assert not any(c[0] == "notify" for c in sent)            # NOT the abort path
    assert not any(e[0] == "recycle_abort" for e in logged)   # never aborted
    assert fleet_state.live_get("w")["session"] == "claude-NEWSID"


def test_dead_agent_recycle_refuses_when_old_pid_still_alive(fs, monkeypatch):
    # SAFETY FLOOR: if respawn does NOT kill the old claude (wedged cmux) its pid stays ALIVE and the
    # lifecycle is non-terminal -> the verify must NOT confirm-gone (never type into a live TUI). Abort.
    from cmux_fleet import state as fleet_state
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "_RESPAWN_VERIFY_TIMEOUT", 0.05)                     # don't burn the 30s poll
    calls = []
    def fake_cmuxq(*a, **k):
        calls.append(a)
        return "OK" if a and a[0] == "respawn-pane" else ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "running")          # never terminal
    monkeypatch.setattr(fleet_state, "surface_has_live_pid", lambda surf: True)     # old claude SURVIVED
    monkeypatch.setattr(fleet, "_surface_pids", lambda surf: {4242})                # pid still alive
    monkeypatch.setattr(fleet_state, "pid_alive", lambda pid: True)
    monkeypatch.setattr(fleet, "_pid_for_surface", lambda surf: None)               # graceful close no-op
    monkeypatch.setattr(fleet.os, "kill", lambda pid, sig: None)
    logged = []
    monkeypatch.setattr(fleet_state, "log_event", lambda event, **f: logged.append((event, f)))
    payload = {"label": "w", "surface": "S", "send_cmd": "cd /x && claude", "mode": "fresh",
               "tool": "claude", "force": True, "prime": None, "old_session": "OLD", "cwd": "/x"}
    assert fleet._recycle_exec_one(payload) == 1                  # ABORT
    assert not any(c[0] == "send" for c in calls)                # never typed the launch into the live TUI
    assert any(e[0] == "recycle_abort" for e in logged)


def test_direct_kill_skips_quietly_with_no_known_pid(fs, monkeypatch):
    # a surface with no pid on record (already-gone entry) must not crash the fallback -- it just skips
    # straight to the re-respawn attempt.
    from cmux_fleet import state as fleet_state
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    respawn_calls = []
    def fake_cmuxq(*a, **k):
        if a and a[0] == "respawn-pane":
            respawn_calls.append(a)
            return "Error: Command timed out" if len(respawn_calls) == 1 else "OK"
        return ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "")
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "ended")
    monkeypatch.setattr(fleet, "_pid_for_surface", lambda surf: None)
    killed = []
    monkeypatch.setattr(fleet.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert fleet._recycle_exec_one(_base_payload()) == 0
    assert killed == []                                        # no pid -> no kill attempted
    assert len(respawn_calls) == 2


# --- Part 1: _fire_launch must VERIFY the ENTER submitted -- RE-KICK the Enter, NEVER re-send the paste.
#     The terminating newline can lose the paste-settle race, leaving the launch as an inert DRAFT at the
#     shell; the downstream self-heal then re-sends the WHOLE TEXT on top of it -> the doubled/tripled
#     draft seen in orphan surface AAF4EC13. These drive _recycle_exec_one to a CLEAN fresh bind (so
#     _fire_launch runs exactly once), count the send-key enter re-kicks, and assert the launch TEXT is
#     sent exactly once no matter how many kicks it takes. --------------------------------------------
def _run_recycle_counting_keys(monkeypatch, surfaced_seq):
    """Run ONE fresh recycle to a clean bind, driving _agent_surfaced by `surfaced_seq` (bool per check)
    with _resume_menu_visible always False. A False is PREPENDED for _fire_launch's TUI-up GUARD (the
    pre-paste never-type-into-a-live-agent check), so `surfaced_seq` keeps describing the POST-paste
    submission checks the kick counts are about. Returns (n_send_text, n_sendkey_enter)."""
    from cmux_fleet import state as fleet_state
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    calls = []
    def fake_cmuxq(*a, **k):
        calls.append(a)
        return "OK" if a and a[0] == "respawn-pane" else ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "_quiet_gate", lambda *a, **k: True)
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "ended")          # old session confirmed dead
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")     # clean fresh bind -> no self-heal
    surfaced = iter([False] + list(surfaced_seq))     # [0] = the guard check (fresh shell, not surfaced)
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: next(surfaced))
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    assert fleet._recycle_exec_one(_base_payload()) == 0
    n_send_text = sum(1 for c in calls if c and c[0] == "send")
    n_sendkey_enter = sum(1 for c in calls if c[:1] == ("send-key",) and c[-1:] == ("enter",))
    return n_send_text, n_sendkey_enter


def test_enter_rekick_normal_surfaced_first_check(fs, monkeypatch):
    # already submitted on the first check (a TUI marker is up) -> no re-kick at all, just the initial
    # send + enter; the launch TEXT is sent exactly once.
    n_send, n_enter = _run_recycle_counting_keys(monkeypatch, [True])
    assert n_send == 1                                        # paste sent once, never resent
    assert n_enter == 1                                       # only the initial enter, zero re-kicks


def test_enter_rekick_one_miss_then_lands(fs, monkeypatch):
    # first check both-False (still at the shell), second check True -> EXACTLY one extra send-key enter,
    # and the paste is NOT resent.
    n_send, n_enter = _run_recycle_counting_keys(monkeypatch, [False, True])
    assert n_send == 1                                        # NEVER re-send the text
    assert n_enter == 2                                       # initial enter + exactly one re-kick


def test_enter_rekick_exhausts_all_kicks(fs, monkeypatch):
    # never surfaces across all 5 kicks -> exactly 5 EXTRA send-key enter calls (6 total incl. the
    # initial), still no text resent, and _fire_launch returns so the outer poll/self-heal/WARN logic
    # runs unchanged after it (the function does NOT itself abort here).
    n_send, n_enter = _run_recycle_counting_keys(monkeypatch, [False] * 5)
    assert n_send == 1                                        # paste sent once even after 5 dead kicks
    assert n_enter == 6                                       # 1 initial + 5 re-kicks (the 5 "extra")


# --- Part 2: when the launch is sent but NOTHING binds even after the existing self-heal re-fire, the
#     tail WARN must ESCALATE like the respawn-abort path (a desktop notify + a recycle_abort event),
#     not fail silently -- the same silent-failure class that left berg-sandbox down ~9h. Same event
#     type as the respawn-abort path (a different `reason`) so one consumer catches both classes. -------
def test_no_session_after_launch_escalates_and_never_pastes_into_live_tui(fs, monkeypatch):
    # THE 2026-07-09 berg-sandbox failure shape: a TUI IS up on the seat but the confirm resolves no
    # session. The guard must refuse BOTH the initial fire and the self-heal re-fire (zero pastes into
    # the live agent — the old code fired twice, garbling its input box), and the WARN must escalate.
    from cmux_fleet import state as fleet_state
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    calls = []
    def fake_cmuxq(*a, **k):
        calls.append(a)
        return "OK" if a and a[0] == "respawn-pane" else ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "_quiet_gate", lambda *a, **k: True)
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "ended")
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "")           # nothing binds, both polls
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: True)               # a live TUI is on the seat
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    logged = []
    monkeypatch.setattr(fleet_state, "log_event", lambda event, **fields: logged.append((event, fields)))
    assert fleet._recycle_exec_one(_base_payload()) == 0       # WARN path proceeds (does not itself abort)
    assert any(c and c[0] == "notify" for c in calls)          # desktop banner fired (was a silent WARN before)
    aborts = [f for e, f in logged if e == "recycle_abort"]
    assert aborts and aborts[0]["reason"] == "no-session-after-launch"
    assert sum(1 for c in calls if c and c[0] == "send") == 0  # NOT ONE paste into the live agent


def test_no_session_after_launch_self_heal_preserved_on_bare_shell(fs, monkeypatch):
    # the self-heal's legit case is intact: seat stays a BARE SHELL (nothing surfaced) -> initial fire
    # + exactly ONE re-fire (the PATH-not-ready crash recovery), then WARN + escalate.
    from cmux_fleet import state as fleet_state
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    calls = []
    def fake_cmuxq(*a, **k):
        calls.append(a)
        return "OK" if a and a[0] == "respawn-pane" else ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "_quiet_gate", lambda *a, **k: True)
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "ended")
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "")           # nothing ever binds
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: False)              # bare shell throughout
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    logged = []
    monkeypatch.setattr(fleet_state, "log_event", lambda event, **fields: logged.append((event, fields)))
    assert fleet._recycle_exec_one(_base_payload()) == 0
    assert sum(1 for c in calls if c and c[0] == "send") == 2  # initial + ONE self-heal re-fire, no runaway resend
    aborts = [f for e, f in logged if e == "recycle_abort"]
    assert aborts and aborts[0]["reason"] == "no-session-after-launch"


def test_fire_launch_guard_unit(monkeypatch):
    # B at the unit level: a surfaced TUI refuses the fire before a single keystroke is sent.
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a, **k: calls.append(a) or "")
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: True)
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    assert fleet._fire_launch("S", "cd /x && claude", lambda m: None) is False
    assert calls == []                                          # zero sends, zero enters


# --- D: recycle failures escalate to an ACTOR (parent conductor / peer fan-out), not just the log ----
def test_escalation_routes_child_failure_to_parent_inbox(fs, monkeypatch):
    from cmux_fleet import state as fleet_state
    fleet_state.live_put("parent", {"role": "c", "kind": "conductor", "surface": "PARENT"})
    fleet_state.live_put("w", {"role": "w", "kind": "child", "parent": "parent", "surface": "S"})
    woke = []
    monkeypatch.setattr(fleet_state, "wake_if_idle", lambda surf, msg: woke.append(surf) or True)
    monkeypatch.setattr(fleet_state, "idlewake_on", lambda: True)
    fleet._escalate_recycle_failure("w", "S", "fresh", "no-session-after-launch", "detail-x")
    rows = fleet_state.inbox_pending("PARENT", kind="doctor")
    assert len(rows) == 1
    r = rows[0]
    assert r["reason"] == "recycle-failed" and r["label"] == "w" and r["failure"] == "no-session-after-launch"
    assert r["event_key"].startswith("doctor:recycle-failed:w:S:")   # per-attempt (timestamped) event
    assert woke == ["PARENT"]                                        # the actor was woken
    assert fleet_state.unpresented("PARENT", rows, 1800) == []       # wake marked presented (no heartbeat re-nudge)


def test_escalation_fans_out_conductor_failure_to_peers(fs, monkeypatch):
    from cmux_fleet import router
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(router, "fs", fleet_state)                   # keep module handles consistent
    monkeypatch.setattr(router, "cmux", lambda *a, **k: "")          # no real desktop notify
    monkeypatch.setattr(fleet_state, "wake_if_idle", lambda surf, msg: True)
    monkeypatch.setattr(fleet_state, "idlewake_on", lambda: True)
    fleet_state.live_put("cond-a", {"role": "a", "kind": "conductor", "surface": "SA", "session": "sa"})
    fleet_state.live_put("cond-b", {"role": "b", "kind": "conductor", "surface": "SB", "session": "sb"})
    fleet._escalate_recycle_failure("cond-a", "SA", "resume", "respawn-not-confirmed", "detail-y")
    rows = fleet_state.inbox_pending("SB", kind="doctor")            # the PEER is alerted...
    assert len(rows) == 1 and rows[0]["reason"] == "recycle-failed" and rows[0]["label"] == "cond-a"
    assert fleet_state.inbox_pending("SA", kind="doctor") == []      # ...never the failed seat itself


def test_child_escalation_is_per_attempt_realerts_after_ack(fs, monkeypatch):
    # each deliberate re-run that fails again must re-alert, even though the prior failure was acked —
    # the event key is timestamped per attempt, so the event-ack ledger can't swallow a NEW failure.
    from cmux_fleet import state as fleet_state
    fleet_state.live_put("parent", {"role": "c", "kind": "conductor", "surface": "PARENT"})
    fleet_state.live_put("w", {"role": "w", "kind": "child", "parent": "parent", "surface": "S"})
    monkeypatch.setattr(fleet_state, "wake_if_idle", lambda surf, msg: False)
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(fleet.time, "time", lambda: clock["t"])      # fleet.time IS the time module
    fleet._escalate_recycle_failure("w", "S", "fresh", "no-session-after-launch", "d")
    row = fleet_state.inbox_pending("PARENT", kind="doctor")[0]
    fleet_state.ack_events("PARENT", [row])                          # parent handles + acks attempt #1
    fleet_state.inbox_ack("PARENT", "doctor", row["seq"])
    clock["t"] += 600                                                # 10 min later: re-run fails again
    fleet._escalate_recycle_failure("w", "S", "fresh", "no-session-after-launch", "d")
    assert len(fleet_state.inbox_pending("PARENT", kind="doctor")) == 1   # attempt #2 re-alerts


def test_doctor_line_renders_recycle_failed():
    from cmux_fleet import hookverbs
    line = hookverbs._doctor_line({"seq": 7, "label": "w", "child_surface": "SURFACE1",
                                   "reason": "recycle-failed", "failure": "no-session-after-launch"})
    assert "RECYCLE FAILED" in line and "no-session-after-launch" in line and "fleet recycle w" in line


def test_resume_gate_abort_escalates(fs, monkeypatch):
    # the third terminal-failure site: a wedged resume-summary menu aborts AND escalates (it previously
    # only logged — the one failure path with not even a surface banner).
    from cmux_fleet import state as fleet_state
    fleet_state.live_put("parent", {"role": "c", "kind": "conductor", "surface": "PARENT"})
    fleet_state.live_put("w", {"role": "w", "kind": "child", "parent": "parent", "surface": "S",
                               "tool": "claude", "session": "claude-OLD", "cwd": "/x"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "cmuxq", lambda *a, **k: "OK" if a and a[0] == "respawn-pane" else "")
    monkeypatch.setattr(fleet, "_quiet_gate", lambda *a, **k: True)
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "ended")
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: False)
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    monkeypatch.setattr(fleet, "_resume_and_gate", lambda *a, **k: False)          # menu never resolves
    monkeypatch.setattr(fleet_state, "wake_if_idle", lambda surf, msg: False)
    payload = {"label": "w", "surface": "S", "send_cmd": "cd /x && claude", "mode": "resume",
               "tool": "claude", "force": True, "prime": None, "old_session": "OLD", "cwd": "/x"}
    assert fleet._recycle_exec_one(payload) == 1                     # ABORT preserved
    rows = fleet_state.inbox_pending("PARENT", kind="doctor")
    assert rows and rows[0]["failure"] == "resume-menu-wedged"       # ...and the parent was told


# --- Part 3: _session_pref_provenance needs a MODEL-analog of the effort floor-warning. The per-key loop
#     only warns when a flag IS present (its body runs solely when `val` is truthy), so a roster role with
#     NO --model token anywhere printed NO warning and silently rode the AMBIENT global default (the
#     sonnet-instead-of-opus surprise that bit an unpinned role). ------------------------------------
def test_provenance_warns_when_roster_role_has_no_model(monkeypatch):
    monkeypatch.setattr(fleet, "_is_roster", lambda role: True)
    monkeypatch.setattr(fleet, "load_config", lambda: {"tool": {"claude": {}}, "role": {"r": {}}})
    _line, warn = fleet._session_pref_provenance("r", "claude", "cd /x && claude", None, None)
    assert warn and "role 'r'" in warn and "--model" in warn    # non-empty and names the role


def test_provenance_no_model_warn_when_pinned(monkeypatch):
    monkeypatch.setattr(fleet, "_is_roster", lambda role: True)
    monkeypatch.setattr(fleet, "load_config",
                        lambda: {"tool": {"claude": {}},
                                 "role": {"r": {"claude": {"flags": "--model claude-opus-4-8"}}}})
    line, warn = fleet._session_pref_provenance("r", "claude", "cd /x && claude --model claude-opus-4-8", None, None)
    assert warn == ""                                           # model IS pinned -> no gap warning
    assert "model=claude-opus-4-8 (role-pin)" in line


def test_provenance_no_model_warn_for_adhoc_role(monkeypatch):
    # non-roster (ad-hoc) rides the binding, not a role pin -> the gate must NOT warn (matches the
    # existing `roster` gating on the effort branch).
    monkeypatch.setattr(fleet, "_is_roster", lambda role: False)
    _line, warn = fleet._session_pref_provenance("adhoc", "claude", "cd /x && claude", None, None)
    assert warn == ""


def test_provenance_model_gap_does_not_overwrite_effort_floor_warn(monkeypatch):
    # effort rides the [tool] floor (a warning) AND --model is absent (also a warning) on the SAME call:
    # the effort floor-warning must WIN (`warn = warn or ...`), never get clobbered by the model gap.
    monkeypatch.setattr(fleet, "_is_roster", lambda role: True)
    monkeypatch.setattr(fleet, "load_config",
                        lambda: {"tool": {"claude": {"flags": "--effort high"}}, "role": {"r": {}}})
    _line, warn = fleet._session_pref_provenance("r", "claude", "cd /x && claude --effort high", None, None)
    assert "floor" in warn and "effort" in warn                # effort floor-warning won
    assert "no --model anywhere" not in warn                   # model gap did NOT overwrite it


# --- recovery-safety #11: fail-loud when a RESUME has nothing to resume -----------------------------
def test_recycle_resume_fails_loud_when_no_checkpoint_and_no_registry_session(monkeypatch):
    # both sources empty: cmux holds no checkpoint AND the registry has no recorded session -> a resume
    # would compose an empty `--resume` and dead-end at runtime. Refuse up front with the recovery options.
    from cmux_fleet import state as fs
    fs.live_put("w", {"role": "r", "kind": "child", "tool": "claude", "cwd": "/x", "place": "tab",
                      "group": "", "surface": "S1", "session": "", "plugins": [], "flags": [],
                      "settings": "", "status": "live"})
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {})          # cmux has no checkpoint
    with __import__("pytest").raises(SystemExit) as ei:
        fleet.cmd_recycle(["w"])                                            # default = resume
    msg = str(ei.value)
    assert "NO resumable session" in msg and "--fresh" in msg               # signposts the recovery paths


def test_recycle_resume_ok_when_checkpoint_present(monkeypatch):
    # a checkpoint alone (empty registry session) is enough to resume -> no fail-loud; it schedules.
    from cmux_fleet import state as fs
    fs.live_put("w", {"role": "r", "kind": "child", "tool": "claude", "cwd": "/x", "place": "tab",
                      "group": "", "surface": "S1", "session": "", "plugins": [], "flags": [],
                      "settings": "", "status": "live"})
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {"checkpoint_id": "CKPT-1"})
    monkeypatch.setattr(fleet, "_is_roster", lambda role: False)            # registry-fallback compose
    monkeypatch.setattr(fleet.subprocess, "Popen", lambda *a, **k: None)    # don't actually spawn
    rc = fleet.cmd_recycle(["w"])                                           # must NOT fail-loud
    assert rc == 0
