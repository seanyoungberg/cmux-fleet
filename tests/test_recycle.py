# tests/test_recycle.py — regression coverage for the recycle confirm logic (the b337d1f hotfix).
# `_poll_session_back` is what decides whether a recycled agent actually re-bound a session. The fix
# added `exclude` so a STALE store sid (snapshotted pre-relaunch) can't false-confirm a crashed launch.
# These are pure unit tests: poll_session / fleet_state.lifecycle / time.sleep are monkeypatched, no cmux.
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import fleet           # noqa: E402  (never popped by other test files)

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
    import fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "old")
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "idle")
    got = fleet._poll_session_back("S", "old", "resume", timeout=5, exclude={"old"})
    assert got == "old"


def test_resume_waits_while_lifecycle_dead(monkeypatch):
    # resume does NOT confirm while the surface lifecycle is still empty/ended.
    import fleet_state
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "old")
    monkeypatch.setattr(fleet_state, "lifecycle", lambda surf: "ended")
    got = fleet._poll_session_back("S", "old", "resume", timeout=0.05, exclude={"old"})
    assert got == ""
