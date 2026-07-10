"""Step 2 of the v2 migration: exec-delivery for launch and revive (adapter.exec_deliver), the
relocated resume-menu dismisser, and the soak flag. The design's contract (vault entity 'Agent
management v2', sections 3-4): a process start is delivered as the pane PROCESS via respawn-pane
with the husk-preserving `; exec /bin/zsh -il` chain — no paste, no Enter, no re-kicks — and
CMUX_FLEET_EXEC_LAUNCH=0 reverts launch, revive, AND recycle to the proven paste tower together
(the paste code is kept for a one-week soak; deleting it is step 3)."""
import pytest

from cmux_fleet import adapter
from cmux_fleet import cli as fleet


def _capture(monkeypatch, poll_sid="sid-new"):
    calls = []

    def fake_cmuxq(*args):
        calls.append(args)
        return "OK"
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=60: poll_sid)
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: False)
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    return calls


def _respawns(calls):
    return [c for c in calls if c and c[0] == "respawn-pane"]


def _pastes(calls):
    return [c for c in calls if c and c[0] == "send" and any("claude" in str(a) for a in c)]


def test_launch_bind_uses_exec_by_default(monkeypatch):
    calls = _capture(monkeypatch)
    ws, surf, sid = fleet._bind_launched_session(
        "WS", "SURF", "cd /x && claude --flag", "claude", "lbl", "/x", [], lazy=False, timeout=5)
    assert sid == "sid-new"
    rs_calls = _respawns(calls)
    assert len(rs_calls) == 1, calls
    cmd = rs_calls[0][-1]
    assert "; exec /bin/zsh -il" in cmd                     # husk chain: a crashed launch never kills the surface
    assert "cd /x && claude --flag" in cmd                  # the actual launch rides as the pane process
    assert 'export PATH="$HOME/.local/bin' in cmd           # PATH-guarded, byte-parity with recycle
    assert not _pastes(calls)                               # and nothing was typed into a terminal


def test_launch_bind_paste_under_soak_flag(monkeypatch):
    monkeypatch.setenv("CMUX_FLEET_EXEC_LAUNCH", "0")
    calls = _capture(monkeypatch)
    ws, surf, sid = fleet._bind_launched_session(
        "WS", "SURF", "cd /x && claude --flag", "claude", "lbl", "/x", [], lazy=False, timeout=5)
    assert sid == "sid-new"
    assert not _respawns(calls)
    assert _pastes(calls), calls                            # the proven paste tower, unchanged, one flag away


def test_exec_send_and_confirm_stops_at_resume_menu_without_kicks(monkeypatch):
    calls = _capture(monkeypatch, poll_sid="")
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: True)
    sid = fleet._exec_send_and_confirm("WS", "SURF", "claude --resume abc", lazy=False, timeout=5)
    assert sid == ""                                        # caller gates/dismisses the menu
    assert not any(c[:1] == ("send-key",) for c in calls)   # exec path never kicks Enter


def test_deliver_launch_exec_and_paste_branches(monkeypatch):
    calls = _capture(monkeypatch)
    fleet._deliver_launch("WS", "SURF", "cd /y && claude")
    assert _respawns(calls) and not _pastes(calls)
    calls2 = _capture(monkeypatch)
    monkeypatch.setenv("CMUX_FLEET_EXEC_LAUNCH", "0")
    fleet._deliver_launch("WS", "SURF", "cd /y && claude")
    assert _pastes(calls2) and not _respawns(calls2)


def test_exec_deliver_refuses_over_a_live_tui():
    calls = []
    ok = adapter.exec_deliver("S", "claude", lambda m: None,
                              cmux=lambda *a: calls.append(a) or "OK",
                              tui_up=lambda: True,
                              paste_fallback=lambda: pytest.fail("must not paste over a live TUI"))
    assert ok is False and calls == []                      # refused, zero cmux actions


def test_exec_deliver_falls_back_to_paste_on_respawn_error():
    fell_back = []
    ok = adapter.exec_deliver("S", "claude", lambda m: None,
                              cmux=lambda *a: "Error: Command timed out",
                              tui_up=lambda: False,
                              paste_fallback=lambda: fell_back.append(1) or True)
    assert ok is True and fell_back == [1]


def test_dismiss_resume_menu_picks_full_session(monkeypatch):
    keys = []
    pane = "1. Resume from summary (recommended)\n2. Resume full session as-is\n"

    def fake_cmux(*args):
        if args[0] == "capture-pane":
            return pane
        keys.append(args)
        return "OK"
    status = adapter.dismiss_resume_menu("S", lambda m: None, cmux=fake_cmux, timeout=5,
                                         sleep=lambda *_: None)
    assert status == adapter.RESUME_DISMISSED
    assert [k[-1] for k in keys] == ["down", "enter"]       # option 2: full, never the lossy summary
    # and the cli names still front the same machinery (step-3 deletes the shims, not the behavior)
    assert fleet.RESUME_DISMISSED == adapter.RESUME_DISMISSED
