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


def _exec_respawns(calls):
    """The respawn-pane calls that CARRY a launch (C's delivery: `/bin/zsh -ilc '<launch>...'`), as
    opposed to the verify's bare-shell respawn (`exec /bin/zsh -il`, no -c). 'The launch fired' on the
    default exec path == exactly one of these."""
    return [c for c in calls if c and c[0] == "respawn-pane" and any("-ilc" in str(x) for x in c)]


def test_verify_waits_out_a_slow_kill_before_launching(fs, monkeypatch):
    # lifecycle reads non-terminal (old claude still alive) for two polls, THEN flips to 'ended' -- the
    # launch (the exec respawn) must not fire until that flip, proving a poll rather than a fixed sleep.
    from cmux_fleet import state as fleet_state
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    calls = []
    def fake_cmuxq(*a, **k):
        calls.append(a)
        return "OK" if a and a[0] == "respawn-pane" else ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "")
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")
    # old agent still shows a LIVE pid (respawn's kill is slow) so the pid branch of the verify never
    # short-circuits — confirmation must come from the terminal lifecycle flip, proving the poll. The
    # graceful-close pre-step is a no-op here (no pid on record to close).
    monkeypatch.setattr(fleet_state, "surface_has_live_pid", lambda surf: True)
    states = iter(["idle", "idle", "ended"])
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: next(states, "ended"))
    assert fleet._recycle_exec_one(_base_payload()) == 0
    assert next(states, "exhausted") == "exhausted"          # all 3 readings were consumed by the poll
    assert len(_exec_respawns(calls)) == 1                   # the launch DID fire, once confirmed dead
    assert not any(c[0] == "send" for c in calls)            # ...as the pane process, never a paste
    assert not any(c[0] == "notify" for c in calls)


def test_respawn_error_falls_back_to_direct_kill_then_succeeds(fs, monkeypatch):
    # respawn-pane errors on the first attempt (the confirmed 05:44 failure: 'Command timed out'). The
    # fallback SIGINTs the LIVE agent pid directly (cmux-independent) and re-respawns; once that
    # confirms, the launch proceeds normally -- this is NOT the abort path. (2026-07-10 update: the
    # fallback used to SIGINT bare _pid_for_surface with NO aliveness check — the blind corpse-shot at
    # pid 70208. It now targets _surface_pids, live + identity-checked, so the graceful close is
    # stubbed out here to isolate the fallback's own selection.)
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
    monkeypatch.setattr(fleet, "_graceful_close", lambda *a, **k: None)   # isolate the FALLBACK's kill
    monkeypatch.setattr(fleet, "_surface_pids", lambda surf: {4242})      # one LIVE agent pid on the seat
    monkeypatch.setattr(fleet, "_agent_pid_check", lambda pid, tool: True)  # identity confirms at signal time
    killed = []
    monkeypatch.setattr(fleet.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert fleet._recycle_exec_one(_base_payload()) == 0
    assert len(respawn_calls) == 3                            # verify attempt + fallback re-respawn + the exec LAUNCH
    assert len(_exec_respawns(respawn_calls)) == 1            # launch fired after the fallback confirmed
    assert killed == [(4242, signal.SIGINT), (4242, signal.SIGINT)]   # SIGINT x2 at the LIVE pid only
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
    monkeypatch.setattr(fleet.os, "kill", lambda pid, sig: None)
    logged = []
    monkeypatch.setattr(fleet_state, "log_event", lambda event, **fields: logged.append((event, fields)))
    assert fleet._recycle_exec_one(_base_payload()) == 1
    assert not any(c[0] == "send" for c in calls)              # launch NEVER sent...
    assert _exec_respawns(calls) == []                         # ...and never exec'd either (abort pre-launch)
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
    calls = []
    def fake_cmuxq(*a, **k):
        calls.append(a)
        return "OK" if a and a[0] == "respawn-pane" else ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    # THE incident fingerprint: lifecycle frozen NON-terminal 'running', but the process is DEAD.
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "running")
    monkeypatch.setattr(fleet_state, "surface_has_live_pid", lambda surf: False)   # pid dead/None -> gone
    monkeypatch.setattr(fleet, "_surface_pids", lambda surf: set())                # no live pid snapshot
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
    assert len(_exec_respawns(calls)) == 1                    # launch DID fire (confirmed + relaunched)
    assert not any(c[0] == "notify" for c in calls)           # NOT the abort path
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
    monkeypatch.setattr(fleet.os, "kill", lambda pid, sig: None)
    logged = []
    monkeypatch.setattr(fleet_state, "log_event", lambda event, **f: logged.append((event, f)))
    payload = {"label": "w", "surface": "S", "send_cmd": "cd /x && claude", "mode": "fresh",
               "tool": "claude", "force": True, "prime": None, "old_session": "OLD", "cwd": "/x"}
    assert fleet._recycle_exec_one(payload) == 1                  # ABORT
    assert not any(c[0] == "send" for c in calls)                # never typed the launch into the live TUI
    assert _exec_respawns(calls) == []                           # ...and never respawned over it either
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
    killed = []
    monkeypatch.setattr(fleet.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert fleet._recycle_exec_one(_base_payload()) == 0
    assert killed == []                                        # no pid -> no kill attempted
    assert len(respawn_calls) == 3                             # verify attempt + re-respawn + the exec launch
    assert len(_exec_respawns(respawn_calls)) == 1


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
    submission checks the kick counts are about. PINS the exec-launch flag OFF: this harness tests the
    PASTE path's enter-race machinery, which C keeps as the explicit fallback.
    Returns (n_send_text, n_sendkey_enter)."""
    from cmux_fleet import state as fleet_state
    monkeypatch.setenv("CMUX_FLEET_EXEC_LAUNCH", "0")
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
    # the PASTE path's self-heal stays intact (flag pinned off — on the exec path there is no self-heal
    # by design): seat stays a BARE SHELL (nothing surfaced) -> initial fire + exactly ONE re-fire (the
    # PATH-not-ready crash recovery), then WARN + escalate.
    from cmux_fleet import state as fleet_state
    monkeypatch.setenv("CMUX_FLEET_EXEC_LAUNCH", "0")
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


# --- C: exec-style launch — the launch IS the pane process; the paste class is structurally dead -----
def test_exec_launch_command_shape_and_no_paste(monkeypatch):
    # the exact live-probed shape: ONE argv element, "/bin/zsh -ilc " + shlex.quote(inner), with the
    # NON-NEGOTIABLE chained `; exec /bin/zsh -il` (a bare -ilc pane DIES WITH ITS COMMAND and cmux
    # destroys the whole surface — live-reproduced). Zero send/send-key: nothing to collapse or settle.
    import shlex
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a, **k: calls.append(a) or "OK")
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: False)
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    assert fleet._exec_launch("S", "cd /x && claude --model 'claude-fable-5[1m]'", lambda m: None) is True
    respawns = [c for c in calls if c[0] == "respawn-pane"]
    assert len(respawns) == 1
    cmd = respawns[0][respawns[0].index("--command") + 1]
    assert cmd == "/bin/zsh -ilc " + shlex.quote(
        "cd /x && claude --model 'claude-fable-5[1m]'; exec /bin/zsh -il")
    assert not any(c[0] in ("send", "send-key") for c in calls)   # no paste, no Enter, no re-kick


def test_exec_launch_guard_refuses_over_live_tui(monkeypatch):
    # B carries over: respawn-pane KILLS the pane process, so exec-launch over a live agent (a cmux
    # restart-resume appearing between verify and launch) would DESTROY it. Refuse before any call.
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a, **k: calls.append(a) or "OK")
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: True)
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    assert fleet._exec_launch("S", "cd /x && claude", lambda m: None) is False
    assert calls == []                                            # not one keystroke, not one respawn


def test_exec_launch_falls_back_to_paste_on_respawn_error(monkeypatch):
    # an erroring respawn-pane (wedged cmux) degrades to the PROVEN paste path rather than leaving a
    # bare shell with no launch at all.
    monkeypatch.setattr(fleet, "cmuxq", lambda *a, **k: "Error: Command timed out")
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: False)
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    fired = []
    monkeypatch.setattr(fleet, "_fire_launch", lambda surf, guarded, log: fired.append(guarded) or True)
    assert fleet._exec_launch("S", "cd /x && claude", lambda m: None) is True
    assert fired == ["cd /x && claude"]                           # paste fallback carried the launch


def test_exec_launch_enabled_flag_values(monkeypatch):
    for off in ("0", "false", "OFF", " False "):
        monkeypatch.setenv("CMUX_FLEET_EXEC_LAUNCH", off)
        assert fleet._exec_launch_enabled() is False
    for on in ("1", "true", "", "yes"):
        monkeypatch.setenv("CMUX_FLEET_EXEC_LAUNCH", on)
        assert fleet._exec_launch_enabled() is True
    monkeypatch.delenv("CMUX_FLEET_EXEC_LAUNCH")
    assert fleet._exec_launch_enabled() is True                   # default ON


def test_exec_path_no_self_heal_refire_but_still_escalates(fs, monkeypatch):
    # the exec path has NO self-heal by design: a no-bind after an exec'd launch is a REAL failure ->
    # exactly ONE exec respawn (no re-exec, no paste), then WARN + escalation to the parent.
    from cmux_fleet import state as fleet_state
    fleet_state.live_put("parent", {"role": "c", "kind": "conductor", "surface": "PARENT"})
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "parent": "parent"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    calls = []
    def fake_cmuxq(*a, **k):
        calls.append(a)
        return "OK" if a and a[0] == "respawn-pane" else ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "_quiet_gate", lambda *a, **k: True)
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "ended")
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "")      # nothing ever binds
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: False)         # bare shell (crashed launch)
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    monkeypatch.setattr(fleet_state, "wake_if_idle", lambda surf, msg: False)
    logged = []
    monkeypatch.setattr(fleet_state, "log_event", lambda event, **f: logged.append((event, f)))
    assert fleet._recycle_exec_one(_base_payload()) == 0
    assert len(_exec_respawns(calls)) == 1                        # ONE exec launch, never a re-exec
    assert not any(c[0] == "send" for c in calls)                 # and never a paste
    assert any(e == "recycle_abort" and f["reason"] == "no-session-after-launch" for e, f in logged)
    rows = fleet_state.inbox_pending("PARENT", kind="doctor")
    assert rows and rows[0]["failure"] == "no-session-after-launch"   # the actor was told (D)


def test_recycle_uses_exec_launch_by_default_end_to_end(fs, monkeypatch):
    # default ON: a clean fresh recycle delivers the launch via the exec respawn (2 respawn-pane calls:
    # the verify bare-shell + the launch) with ZERO pastes, and binds the registry off A's confirm.
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
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: False)
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")
    assert fleet._recycle_exec_one(_base_payload()) == 0
    respawns = [c for c in calls if c[0] == "respawn-pane"]
    assert len(respawns) == 2 and len(_exec_respawns(respawns)) == 1
    assert not any(c[0] in ("send",) for c in calls)              # the paste class is dead on this path
    assert fleet_state.live_get("w")["session"] == "claude-NEWSID"


# --- kill-path live-pid targeting (2026-07-10 wedge): the kill must target what the confirm trusts ---
# Fix A taught the CONFIRM path live-pid truth; the KILL path still drew targets from the stale-record
# lookup (_pid_for_surface = first record, no aliveness check). Live incident, surface F1C0AEDB with 4
# hook-store records: graceful close SIGINT'd dead 76035, the fallback SIGINT'd dead 70208, respawn-pane
# abandoned the REAL agent (76142, 4th record) orphaned on its old tty, and the verify — correctly —
# refused forever. B+D failed it safely; these lock in the fix that makes it not fail at all.
def _incident_store(live_pid, dead_pid=70208):
    """The 2026-07-10 hook-store shape: three dead/None-pid ghosts ordered FIRST, the real agent LAST
    (the pids are the incident's own: 70208 the corpse the fallback shot, 76142 the survivor)."""
    return {"sessions": {
        "3deb145a": {"sessionId": "3deb145a", "surfaceId": "F1C0", "pid": dead_pid, "updatedAt": 10},
        "b19a6251": {"sessionId": "b19a6251", "surfaceId": "F1C0", "pid": None, "updatedAt": 20},
        "ca54276c": {"sessionId": "ca54276c", "surfaceId": "F1C0", "pid": None, "updatedAt": 30},
        "f717aca3": {"sessionId": "f717aca3", "surfaceId": "F1C0", "pid": live_pid, "updatedAt": 40},
    }}


def test_signal_targets_only_the_live_pid_never_the_ghosts(monkeypatch):
    # NOTE: pid_alive is PATCHED (not real) — the os.kill capture below would otherwise swallow
    # pid_alive's own signal-0 probe and make every pid read alive.
    from cmux_fleet import state as fleet_state
    import signal
    live = 76142
    monkeypatch.setattr(fleet, "_store", lambda: _incident_store(live))
    monkeypatch.setattr(fleet_state, "pid_alive", lambda pid: pid == live)
    monkeypatch.setattr(fleet, "_agent_pid_check", lambda pid, tool: True)
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    killed = []
    monkeypatch.setattr(fleet.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    got = fleet._signal_agent_pids("F1C0", "claude", lambda m: None, "t")
    assert got == [live]                                       # the 4th record's LIVE pid was targeted
    assert killed == [(live, signal.SIGINT), (live, signal.SIGINT)]   # ...and NOTHING else was signalled


def test_signal_skips_a_live_pid_that_fails_the_identity_check(monkeypatch):
    # pid-reuse guard: alive but not identifiable as this tool's process (an OS-recycled pid) -> NEVER
    # signalled; better to let the verify refuse + escalate than SIGINT an unrelated process.
    from cmux_fleet import state as fleet_state
    live = 76142
    monkeypatch.setattr(fleet, "_store", lambda: _incident_store(live))
    monkeypatch.setattr(fleet_state, "pid_alive", lambda pid: pid == live)
    monkeypatch.setattr(fleet, "_agent_pid_check", lambda pid, tool: False)
    killed = []
    monkeypatch.setattr(fleet.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert fleet._signal_agent_pids("F1C0", "claude", lambda m: None, "t") == []
    assert killed == []                                        # not one signal fired


def test_agent_pid_check_real_ps_identity():
    # the check runs REAL ps: this very test process identifies as python, and not as claude.
    assert fleet._agent_pid_check(os.getpid(), "python") is True
    assert fleet._agent_pid_check(os.getpid(), "claude") is False
    assert fleet._agent_pid_check(99_999_999, "claude") is False      # nonexistent pid -> fail closed
    assert fleet._agent_pid_check("garbage", "claude") is False       # unparseable -> fail closed


def test_graceful_close_reaps_the_live_record_not_the_first(monkeypatch):
    # the graceful close draws its target from the live set: the first-record ghost (the old
    # _pid_for_surface pick) is never signalled, the real agent is, and the close returns as soon as
    # the signalled pid dies.
    from cmux_fleet import state as fleet_state
    import signal
    live = 76142
    monkeypatch.setattr(fleet, "_store", lambda: _incident_store(live))
    monkeypatch.setattr(fleet, "_agent_pid_check", lambda pid, tool: True)
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    state = {"alive": True}
    killed = []
    def fake_kill(pid, sig):
        killed.append((pid, sig))
        state["alive"] = False                                 # the SIGINT lands; the agent exits
    monkeypatch.setattr(fleet.os, "kill", fake_kill)
    monkeypatch.setattr(fleet_state, "pid_alive", lambda pid: state["alive"] if pid == live else False)
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "unknown")   # frozen, like the incident
    fleet._graceful_close("F1C0", "claude", lambda m: None, timeout=5)
    assert [p for p, _ in killed] == [live, live]              # only the real agent, SIGINT x2
    assert state["alive"] is False


def test_orphaned_live_agent_is_reaped_by_the_fallback_end_to_end(fs, monkeypatch):
    # THE incident, replayed to the FIXED outcome: a live orphan (record claims the surface, process
    # alive on an abandoned tty) survives the first respawn+verify; the direct-kill fallback now
    # selects IT (live + identity-checked), it dies cleanly, the re-respawn verifies, and the exec
    # launch proceeds — where the old code SIGINT'd corpses and wedged until a human killed the orphan.
    from cmux_fleet import state as fleet_state
    import signal
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "_RESPAWN_VERIFY_TIMEOUT", 0.05)          # don't burn 30s on attempt 1
    orphan = {"alive": True}
    calls = []
    def fake_cmuxq(*a, **k):
        calls.append(a)
        return "OK" if a and a[0] == "respawn-pane" else ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "_quiet_gate", lambda *a, **k: True)
    monkeypatch.setattr(fleet, "_surface_pids", lambda surf: {76142} if orphan["alive"] else set())
    monkeypatch.setattr(fleet, "_agent_pid_check", lambda pid, tool: True)
    monkeypatch.setattr(fleet, "_graceful_close", lambda *a, **k: None)  # the close missed it (incident shape)
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "unknown")           # never terminal
    monkeypatch.setattr(fleet_state, "surface_has_live_pid", lambda surf: orphan["alive"])
    monkeypatch.setattr(fleet_state, "pid_alive", lambda pid: orphan["alive"] if pid == 76142 else False)
    killed = []
    def fake_kill(pid, sig):
        killed.append((pid, sig))
        orphan["alive"] = False                                          # the SIGINT reaps the orphan
    monkeypatch.setattr(fleet.os, "kill", fake_kill)
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: False)
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")
    assert fleet._recycle_exec_one(_base_payload()) == 0                 # SUCCEEDS (old code: wedged abort)
    assert killed == [(76142, signal.SIGINT), (76142, signal.SIGINT)]    # the ORPHAN was the target
    assert len(_exec_respawns(calls)) == 1                               # and the launch proceeded
    assert fleet_state.live_get("w")["session"] == "claude-NEWSID"
