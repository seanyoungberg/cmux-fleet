# tests/test_notif_dedup.py — notification dedup, audit fix-order #5 (EVENT-KEY ack) and #4
# (presentation cooldown). One event identity per inbox row (`event_key`): ONE ack clears that event on
# every presentation path (awareness / drain / heartbeat / router wake / `fleet inbox`) and refuses a
# producer replay of it; the presentation ledger keeps the heartbeat from re-nudging rows some path
# already showed. Hermetic like test_fleet_doctor: hook store mocked, wakes captured, file-backed inbox.
import os

import pytest

from cmux_fleet import features
from cmux_fleet import helpers as fh
from cmux_fleet import router
from cmux_fleet import state as fs

NOW = 1_800_000_000.0
STALE_UA = NOW - (router.STALL_S + 60)
FRESH_UA = NOW - 5
LIVE_PID = os.getpid()


@pytest.fixture(autouse=True)
def _sync(monkeypatch):
    """Same module-consistency + dedup-reset dance as test_fleet_doctor (see its _sync docstring)."""
    global fs, features
    import cmux_fleet.features as _features
    import cmux_fleet.state as _state
    fs, features = _state, _features
    monkeypatch.setattr(router, "fs", _state)
    monkeypatch.setattr(fh, "fs", _state)
    router._doctor_fired.clear()
    router._conductor_live_seen.clear()
    yield
    router._doctor_fired.clear()
    router._conductor_live_seen.clear()


# ── event keys on rows ────────────────────────────────────────────────────────────────────────────
def test_rows_carry_event_keys():
    s1 = fs.inbox_put("completion", "S", {"label": "w"})
    assert fs.inbox_pending("S")[0]["event_key"] == f"completion:seq-{s1}"   # fallback: per-row identity
    fs.inbox_put("peer", "S", {"msg_id": "abc"}, event_key="peer:abc")
    assert fs.inbox_pending("S", kind="peer")[0]["event_key"] == "peer:abc"  # provided: stable identity


def test_peer_msg_rows_carry_msg_id_event_key(monkeypatch):
    fs.live_put("me",  {"role": "c", "kind": "conductor", "surface": "SND", "status": "live"})
    fs.live_put("you", {"role": "c", "kind": "conductor", "surface": "RCP", "status": "live"})
    monkeypatch.setenv("CMUX_SURFACE_ID", "SND")
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: True)
    fh.cmd_peer_msg(["you", "hello"])
    row = fs.inbox_pending("RCP", kind="peer")[0]
    assert row["event_key"] == f"peer:{row['msg_id']}"


# ── event-level ack: one ack clears every reader ─────────────────────────────────────────────────
def test_ack_events_clears_pending_without_cursor_and_stays_per_surface():
    fs.inbox_put("doctor", "S", {"reason": "stall", "label": "w"}, event_key="doctor:stall:w:sess")
    fs.inbox_put("doctor", "T", {"reason": "stall", "label": "w"}, event_key="doctor:stall:w:sess")
    fs.ack_events("S", fs.inbox_pending("S", kind="doctor"))
    assert fs.inbox_pending("S", kind="doctor") == []            # cleared with NO cursor advance
    assert len(fs.inbox_pending("T", kind="doctor")) == 1        # another surface's copy survives


def test_acked_event_refuses_producer_replay_then_rearm_allows_fresh_occurrence():
    key = "doctor:needs-input:w:sess"
    seq = fs.inbox_put("doctor", "S", {"reason": "needs-input", "label": "w"}, event_key=key)
    fs.ack_events("S", fs.inbox_pending("S", kind="doctor"))     # a real ack pairs ledger + cursor
    fs.inbox_ack("S", "doctor", seq)                             # (cmd_inbox_ack always does both)
    assert fs.inbox_put("doctor", "S", {"reason": "needs-input", "label": "w"}, event_key=key) == 0
    assert fs.inbox_pending("S", kind="doctor") == []            # replay refused: no row resurrected
    fs.inbox_event_rearm(key)                                    # condition cleared -> new occurrence
    assert fs.inbox_put("doctor", "S", {"reason": "needs-input", "label": "w"}, event_key=key) > 0
    assert len(fs.inbox_pending("S", kind="doctor")) == 1        # only the NEW row; old stays cursor-cleared


# ── inbox-ack CLI: the seq names the row; the row names the kind + event ──────────────────────────
def test_inbox_ack_infers_kind_from_row(monkeypatch, capsys):
    monkeypatch.setenv("CMUX_SURFACE_ID", "S")
    fs.inbox_put("completion", "S", {"label": "w", "gist": "done"})
    dseq = fs.inbox_put("doctor", "S", {"reason": "stall", "label": "w"}, event_key="doctor:stall:w:x")
    fh.cmd_inbox_ack([str(dseq)])                                # NO --doctor: the row's own kind wins
    assert fs.inbox_pending("S", kind="doctor") == []
    assert len(fs.inbox_pending("S", kind="completion")) == 1    # old behavior would eat this by cursor


def test_inbox_ack_row_kind_beats_wrong_flag(monkeypatch, capsys):
    monkeypatch.setenv("CMUX_SURFACE_ID", "S")
    fs.inbox_put("peer", "S", {"from_label": "p", "body": "hi", "msg_id": "m1"}, event_key="peer:m1")
    dseq = fs.inbox_put("doctor", "S", {"reason": "stall", "label": "w"}, event_key="doctor:stall:w:x")
    fh.cmd_inbox_ack([str(dseq), "--peer"])                      # wrong flag -> the row wins, loudly
    assert "is a doctor row" in capsys.readouterr().out
    assert fs.inbox_pending("S", kind="doctor") == []
    assert len(fs.inbox_pending("S", kind="peer")) == 1          # the peer stream is untouched


def test_inbox_ack_falls_back_to_flag_when_seq_has_no_row(monkeypatch):
    monkeypatch.setenv("CMUX_SURFACE_ID", "S")
    fs.inbox_ack("S", "peer", 3)                                 # pre-acked high-water; row long gone
    fh.cmd_inbox_ack(["7", "--peer"])                            # idempotent re-ack must not crash
    assert fs._cursors()["S"]["peer"] == 7


def test_inbox_ack_records_event_keys_of_all_cleared_rows(monkeypatch):
    monkeypatch.setenv("CMUX_SURFACE_ID", "S")
    fs.inbox_put("doctor", "S", {"reason": "stall", "label": "a"}, event_key="doctor:stall:a:x")
    hi = fs.inbox_put("doctor", "S", {"reason": "low-ctx", "label": "b"}, event_key="doctor:low-ctx:b:x")
    fh.cmd_inbox_ack([str(hi), "--doctor"])                      # batch ack through the high seq
    assert fs.event_acked("S", "doctor:stall:a:x")               # BOTH cleared events recorded...
    assert fs.event_acked("S", "doctor:low-ctx:b:x")
    assert fs.inbox_put("doctor", "S", {"reason": "stall", "label": "a"},
                        event_key="doctor:stall:a:x") == 0       # ...so neither can be replayed


# ── sweep-level: ack survives total dedup-state loss; clear->bad re-alerts ────────────────────────
def _seed_parent_child(session="claude-cccccccc-1111-2222-3333-444444444444"):
    fs.live_put("parent", {"surface": "PARENT", "kind": "conductor", "role": "c", "session": "claude-parent"})
    fs.live_put("child", {"surface": "CHILD", "kind": "child", "role": "w", "parent": "parent",
                          "tool": "claude", "session": session})
    return session


def _store(life, ua, sid="cccccccc-1111-2222-3333-444444444444"):
    return {"sessions": {sid: {"sessionId": sid, "surfaceId": "CHILD", "agentLifecycle": life,
                               "updatedAt": ua, "transcriptPath": "", "pid": LIVE_PID}},
            "activeSessionsBySurface": {}}


@pytest.fixture
def wake(monkeypatch):
    woke = []
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: woke.append(surf) or True)
    monkeypatch.setattr(fs, "idlewake_on", lambda: True)
    monkeypatch.setattr(features, "_context_used", lambda path: (None, ""))
    monkeypatch.setattr(features, "_context_window", lambda model: 200_000)
    return woke


def test_sweep_replay_after_ack_suppressed_despite_dedup_loss(monkeypatch, wake):
    """The class the persisted dedup alone can't close: parent ACKED the alert, then the daemon's
    condition state is lost wholesale (restart + nuked doctor-dedup.json). The re-swept, still-bad,
    already-HANDLED condition must not come back on any path."""
    _seed_parent_child()
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("running", STALE_UA))
    assert router.fleet_doctor_sweep(now=NOW) == 1
    seq = fs.inbox_pending("PARENT", kind="doctor")[0]["seq"]
    monkeypatch.setenv("CMUX_SURFACE_ID", "PARENT")
    fh.cmd_inbox_ack([str(seq)])                                 # bare seq: row-inferred doctor ack
    router._doctor_fired.clear()                                 # simulate total dedup-state loss
    os.remove(fs.DOCTOR_DEDUP)
    assert router.fleet_doctor_sweep(now=NOW) == 0               # replay refused at the producer
    assert fs.inbox_pending("PARENT", kind="doctor") == []


def test_sweep_clear_then_bad_realerts_after_ack(monkeypatch, wake):
    """The inverse guarantee: an acked condition that CLEARS re-arms (event ack forgotten), so a fresh
    occurrence alerts again — the ledger must never eat a genuine new stall."""
    session = _seed_parent_child()
    store = {"v": _store("running", STALE_UA)}
    monkeypatch.setattr(fs, "read_hook_store", lambda: store["v"])
    assert router.fleet_doctor_sweep(now=NOW) == 1
    seq = fs.inbox_pending("PARENT", kind="doctor")[0]["seq"]
    monkeypatch.setenv("CMUX_SURFACE_ID", "PARENT")
    fh.cmd_inbox_ack([str(seq)])
    store["v"] = _store("running", FRESH_UA)                     # the turn recovered (condition CLEAR)
    assert router.fleet_doctor_sweep(now=NOW) == 0               # rearm tick, nothing fired
    ekey = router._doctor_event_key("stall", "child", session)
    assert not fs.event_acked("PARENT", ekey)                    # the event ack was forgotten
    store["v"] = _store("running", STALE_UA)                     # a NEW stall
    assert router.fleet_doctor_sweep(now=NOW) == 1               # ...alerts fresh
    assert len(fs.inbox_pending("PARENT", kind="doctor")) == 1
