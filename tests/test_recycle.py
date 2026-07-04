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
