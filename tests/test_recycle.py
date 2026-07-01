# tests/test_recycle.py — regression coverage for the recycle confirm logic (the b337d1f hotfix).
# `_poll_session_back` is what decides whether a recycled agent actually re-bound a session. The fix
# added `exclude` so a STALE store sid (snapshotted pre-relaunch) can't false-confirm a crashed launch.
# These are pure unit tests: poll_session / fleet_state.lifecycle / time.sleep are monkeypatched, no cmux.
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

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
