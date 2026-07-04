# tests/test_recycle.py — regression coverage for the recycle confirm logic (the b337d1f hotfix).
# `_poll_session_back` is what decides whether a recycled agent actually re-bound a session. The fix
# added `exclude` so a STALE store sid (snapshotted pre-relaunch) can't false-confirm a crashed launch.
# These are pure unit tests: poll_session / fleet_state.lifecycle / time.sleep are monkeypatched, no cmux.
import os
import sys


from cmux_fleet import cli as fleet           # noqa: E402  (never popped by other test files)

# NOTE: `_poll_session_back` does `import fleet_state as fs` INTERNALLY, and another test module
# (test_features) pops `fleet_state` from sys.modules on teardown. So the resume tests below import
# fleet_state *inside* the test (after any popping) to patch the SAME cached object the function gets.


def test_fresh_excludes_old_and_pre_sid(monkeypatch):
    # the surface keeps reporting a stale (excluded) sid -> never a fresh bind -> "" within timeout.
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "stale-pre")
    got = fleet._poll_session_back("S", "old", "fresh", timeout=0.05, exclude={"old", "stale-pre"})
    assert got == ""                              # a crashed launch resolves to no-session, not success


def test_fresh_confirms_a_genuinely_new_sid(monkeypatch):
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "brand-new")
    got = fleet._poll_session_back("S", "old", "fresh", timeout=5, exclude={"old", "stale-pre"})
    assert got == "brand-new"                     # a real new bind confirms


def test_resume_ignores_exclude_and_uses_lifecycle(monkeypatch):
    # resume keeps the SAME sid; confirmation is the surface going live again, not a new sid. So a sid
    # that is IN exclude still confirms in resume mode (exclude is a fresh-mode-only guard).
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "old")
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "idle")
    got = fleet._poll_session_back("S", "old", "resume", timeout=5, exclude={"old"})
    assert got == "old"


def test_resume_waits_while_lifecycle_dead(monkeypatch):
    # resume does NOT confirm while the surface lifecycle is still empty/ended.
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "old")
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "ended")
    got = fleet._poll_session_back("S", "old", "resume", timeout=0.05, exclude={"old"})
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
    # non-force on a 'running' surface still times out (False) — the no-half-kill guard is UNCHANGED.
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "running")
    monkeypatch.setattr(fleet, "_input_draft_nonempty", lambda surf: False)
    assert fleet._quiet_gate("S", 0.05, force=False) is False


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
        "enable_plugins": [], "setting_sources": ""})
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
    with _resume_menu_visible always False. Returns (n_send_text, n_sendkey_enter)."""
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
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "")        # no stale pre_sid
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")     # clean fresh bind -> no self-heal
    surfaced = iter(surfaced_seq)
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
def test_no_session_after_launch_escalates(fs, monkeypatch):
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
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "")
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "")           # nothing binds, both polls
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: True)               # keep _fire_launch's kick loop short
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    logged = []
    monkeypatch.setattr(fleet_state, "log_event", lambda event, **fields: logged.append((event, fields)))
    assert fleet._recycle_exec_one(_base_payload()) == 0       # WARN path proceeds (does not itself abort)
    assert any(c and c[0] == "notify" for c in calls)          # desktop banner fired (was a silent WARN before)
    aborts = [f for e, f in logged if e == "recycle_abort"]
    assert aborts and aborts[0]["reason"] == "no-session-after-launch"
    assert sum(1 for c in calls if c and c[0] == "send") == 2  # initial + ONE self-heal re-fire, no runaway resend


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
