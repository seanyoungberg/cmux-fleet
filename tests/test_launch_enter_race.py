# tests/test_launch_enter_race.py — FIX 1: the paste-settle ENTER-RACE on launch + drive.
# After an injected command (launch) or a pasted prompt (drive), the terminating Enter is often
# processed BEFORE the terminal finishes rendering the paste, so it never submits — the command/prompt
# sits unexecuted. Both paths now VERIFY-then-RETRY the Enter. These are pure units: the cmux reads
# (cmuxq / capture-pane / poll_session) are stubbed, so nothing touches a real surface.
#
# Also covers the resume-menu variant of the SAME code path (2026-07-02 incident, Item 1): a
# `--resume <id>` passthrough can surface claude's interactive resume-summary menu, which shows NONE of
# _TUI_MARKERS. The old blind re-kick mistook it for "still at the shell" and spammed Enter into it,
# landing on the menu's cursor-default, LOSSY "Resume from summary" option instead of "full as-is".
import pytest

from cmux_fleet import cli as fleet


def _load_drive():
    """The drive-child logic now lives in the `fleet drive-child` verb (cmux_fleet.helpers), folded out
    of the old standalone scripts/drive-child.py in Phase 2."""
    from cmux_fleet import helpers
    return helpers


# --- launch: _send_launch_and_confirm re-kicks the Enter until the session binds ------------------
def test_launch_rekicks_enter_until_session_binds(monkeypatch):
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    calls = []
    state = {"polls": 0}

    def fake_poll(surf, timeout=60):
        state["polls"] += 1
        return "SID-1" if state["polls"] >= 3 else ""     # binds only after a couple of re-kicks

    def fake_cmuxq(*args):
        calls.append(args)
        if args[:1] == ("capture-pane",):
            return "user@host cd /x && claude --foo"       # no TUI marker -> still at the shell
        return ""

    monkeypatch.setattr(fleet, "poll_session", fake_poll)
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    sid = fleet._send_launch_and_confirm("WS", "SURF", "cd /x && claude --foo", lazy=False, timeout=30)
    assert sid == "SID-1"
    # the command was injected once (with the terminating newline) ...
    assert ("send", "--workspace", "WS", "--surface", "SURF", "cd /x && claude --foo\n") in calls
    # ... and the lost Enter was re-kicked at least once while the shell still sat unexecuted.
    assert [c for c in calls if c == ("send-key", "--surface", "SURF", "enter")]


def test_launch_never_kicks_enter_into_a_booted_tui(monkeypatch):
    # once the agent TUI is on-screen the launch already started -> we must NOT spam Enter into it.
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=60: "")
    calls = []

    def fake_cmuxq(*args):
        calls.append(args)
        return "Context Remaining 80%" if args[:1] == ("capture-pane",) else ""

    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    sid = fleet._send_launch_and_confirm("WS", "SURF", "cmd", lazy=True, timeout=5)
    assert sid == ""                                        # lazy tool up; it binds on its first turn
    assert not any(c == ("send-key", "--surface", "SURF", "enter") for c in calls)


# --- launch: resume-menu awareness (Item 1 fix) ----------------------------------------------------
def test_send_launch_and_confirm_stops_kicking_into_resume_menu(monkeypatch):
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=60: "")
    calls = []

    def fake_cmuxq(*args):
        calls.append(args)
        if args[:1] == ("capture-pane",):
            return "1. Resume from summary (recommended)   2. Resume full session as-is"
        return ""

    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    sid = fleet._send_launch_and_confirm("WS", "SURF", "claude --resume abc", lazy=False, timeout=5)
    assert sid == ""                                    # unresolved -- caller must gate/dismiss the menu
    assert not any(c == ("send-key", "--surface", "SURF", "enter") for c in calls)   # no blind kick


def test_bind_launched_session_resume_gate_picks_full_not_summary(monkeypatch):
    # end-to-end through the REAL _dismiss_resume_summary_prompt / _resume_and_gate (only
    # _send_launch_and_confirm is stubbed, standing in for "the menu stopped the confirm loop early"):
    # a --resume <id> passthrough must dismiss via DOWN then ENTER (picks option 2, 'full as-is'), never
    # a bare/blind ENTER (which lands on the menu's cursor-default 'Resume from summary').
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "_send_launch_and_confirm", lambda *a, **k: "")
    calls = []

    def fake_cmuxq(*args):
        calls.append(args)
        if args[:1] == ("capture-pane",):
            return "1. Resume from summary (recommended)   2. Resume full session as-is"
        return ""

    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=60: "SID-FULL")
    ws, surf, sid = fleet._bind_launched_session(
        "WS", "SURF", "claude --resume abc123", "claude", "lbl", "/x",
        ["--resume", "abc123"], lazy=False, timeout=5)
    assert sid == "SID-FULL"
    keys = [c for c in calls if c[:1] == ("send-key",)]
    assert keys == [("send-key", "--surface", "SURF", "down"),      # picks option 2 ('full as-is')...
                    ("send-key", "--surface", "SURF", "enter")]    # ...never a blind bare Enter


def test_bind_launched_session_resume_timeout_aborts_without_register(monkeypatch):
    # a wedged/never-resolving menu must abort (sys.exit) rather than bind/register behind it -- matches
    # cmd_revive's no-teardown-on-timeout contract. _resume_and_gate returning False IS the RESUME_TIMEOUT
    # outcome (see test_recycle.py for its own unit coverage); this test proves cmd_launch's integration
    # point respects that and never reaches register().
    monkeypatch.setattr(fleet, "_send_launch_and_confirm", lambda *a, **k: "")
    monkeypatch.setattr(fleet, "_resume_and_gate", lambda *a, **k: False)
    registered = []
    monkeypatch.setattr(fleet, "register", lambda *a, **k: registered.append(a))
    with pytest.raises(SystemExit):
        fleet._bind_launched_session("WS", "SURF", "claude --resume abc123", "claude", "lbl", "/x",
                                     ["--resume", "abc123"], lazy=False, timeout=1)
    assert not registered            # NOT registering behind an undismissed/wedged menu


# --- drive: _submit settles then verifies + retries the Enter ------------------------------------
def test_drive_retries_enter_until_box_clears(monkeypatch):
    drive = _load_drive()
    monkeypatch.setattr(drive.time, "sleep", lambda *_: None)
    calls = []
    state = {"enters": 0}

    def fake_cmux(*args):
        calls.append(args)
        if args[:1] == ("send-key",):
            state["enters"] += 1
        return None

    def fake_capture(surf):
        # the draft sits in the input box until the SECOND Enter finally submits it (the enter-race)
        return "❯ " if state["enters"] >= 2 else "❯ please do the thing now"

    monkeypatch.setattr(drive, "cmux", fake_cmux)
    monkeypatch.setattr(drive, "_capture", fake_capture)
    assert drive._submit("SURF", "please do the thing now") is True
    enters = [c for c in calls if c[:1] == ("send-key",)]
    assert len(enters) >= 2                                 # first Enter lost the race -> retried


def test_drive_submits_on_first_enter(monkeypatch):
    drive = _load_drive()
    monkeypatch.setattr(drive.time, "sleep", lambda *_: None)
    calls = []
    state = {"enters": 0}

    def fake_cmux(*args):
        calls.append(args)
        if args[:1] == ("send-key",):
            state["enters"] += 1
        return None

    monkeypatch.setattr(drive, "cmux", fake_cmux)
    # box holds the draft until the first Enter, then clears
    monkeypatch.setattr(drive, "_capture",
                        lambda surf: "❯ " if state["enters"] >= 1 else "❯ do the thing")
    assert drive._submit("SURF", "do the thing") is True
    assert len([c for c in calls if c[:1] == ("send-key",)]) == 1


def test_drive_reports_failure_when_box_never_clears(monkeypatch):
    drive = _load_drive()
    monkeypatch.setattr(drive.time, "sleep", lambda *_: None)
    monkeypatch.setattr(drive, "cmux", lambda *a: None)
    monkeypatch.setattr(drive, "_capture", lambda surf: "❯ stuck prompt text here")  # never clears
    assert drive._submit("SURF", "stuck prompt text here") is False
