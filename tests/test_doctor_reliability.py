# tests/test_doctor_reliability.py — the token-flow liveness fix (held-dev, 2026-07-18). Clock-based
# liveness verdicts (detached / stall / stale-gone) false-positived on long-idle OR long-turn agents of
# ANY kind (4 live specimens). The fix gates each verdict on TRANSCRIPT-ADVANCE / a live PID rather than
# wall-clock. Every verdict is tested in BOTH directions: a healthy advancing agent must clear, a
# genuinely dead/frozen one must still be flagged (the discriminator must stay discriminating). The
# transcript state is a real-shaped fixture, never the verdict the code produces.
import json
import os
import time

import pytest

from cmux_fleet import resolve as rs
from cmux_fleet import router
from cmux_fleet import features as ff
from cmux_fleet import state as fs


# --- fixture plumbing (mirrors test_resolve._turn_transcript / _rec exactly) ---------------------
def _turn_transcript(path, now, age_s):
    """A REAL-shaped transcript whose last TURN is `age_s` old, with FRESH bookkeeping + mtime — so an
    mtime rule can't pass; only the last real turn is the activity signal (the step-1 mtime lesson)."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - age_s)) + ".000Z"
    path.write_text(json.dumps({"type": "assistant", "timestamp": ts}) + "\n"
                    + json.dumps({"type": "system", "timestamp": ts}) + "\n")
    os.utime(path, (now, now))                                # mtime deliberately FRESH
    return str(path)


def _rec(surf, sid, pid, life, updated, ws="WS-000", transcript=""):
    return {"surfaceId": surf, "sessionId": sid, "pid": pid, "agentLifecycle": life,
            "updatedAt": updated, "workspaceId": ws, "transcriptPath": transcript}


def _store(sessions, active=None):
    return {"sessions": sessions, "activeSessionsBySurface": active or {}}


def _seat(monkeypatch, tmp_path, *, life, record_age, transcript_age, env_ws="", ws_tree="WS-TREE"):
    """One LIVE seat (this test process's pid, so it reads alive) with controllable clocks + lifecycle."""
    now = time.time()
    tpath = _turn_transcript(tmp_path / "t.jsonl", now, transcript_age) if transcript_age is not None else ""
    rec = _rec("S1", "sid1", os.getpid(), life, now - record_age, transcript=tpath)
    st = _store({"sid1": rec}, {"S1": {"sessionId": "sid1"}})
    monkeypatch.setattr(fs, "read_hook_store", lambda: st)
    monkeypatch.setattr(rs, "_env_workspace", lambda _pid: env_ws)
    return "S1", st, {"S1": ws_tree}, now


# ============ VERDICT 1 — detached (behavioral): a live long turn must not read detached ============
def test_long_running_turn_advancing_transcript_is_not_detached(monkeypatch, tmp_path):
    # HEALTHY: a 12-min turn — cmux stamped 'running' at turn start (record frozen 12m) while tool-calls
    # keep the transcript advancing (last turn 30s ago). The skew EXCEEDS ATTACH_SKEW_S, but a 'running'
    # record within TURN_GRACE_S is a live turn, not a dead channel.
    surf, st, ws_map, now = _seat(monkeypatch, tmp_path, life="running", record_age=720, transcript_age=30)
    att = rs.attachment(surf, st=st, ws_map=ws_map, now=now)
    assert att["attached"] is True and att["reasons"] == []


def test_running_record_frozen_past_grace_still_detached(monkeypatch, tmp_path):
    # STILL FLAGGED: the dark-while-running case (berg-sandbox's 3.5h) — a 'running' record frozen PAST
    # TURN_GRACE_S with a recent transcript is genuinely dark. The discriminator stays discriminating.
    surf, st, ws_map, now = _seat(monkeypatch, tmp_path, life="running", record_age=3000, transcript_age=30)
    att = rs.attachment(surf, st=st, ws_map=ws_map, now=now)
    assert att["attached"] is False and any(r.startswith("behavioral") for r in att["reasons"])


def test_non_running_frozen_while_transcript_advances_is_detached(monkeypatch, tmp_path):
    # STILL FLAGGED: the agent WORKS (transcript 30s) while cmux thinks it idle (record frozen 12m,
    # life=idle) — genuinely dark, trips with NO grace (the non-running behavioral case is unchanged).
    surf, st, ws_map, now = _seat(monkeypatch, tmp_path, life="idle", record_age=720, transcript_age=30)
    att = rs.attachment(surf, st=st, ws_map=ws_map, now=now)
    assert att["attached"] is False and any(r.startswith("behavioral") for r in att["reasons"])


# ============ VERDICT 1b — detached (env): a freshly-moved agent must not read detached ============
def test_moved_agent_with_advancing_record_not_detached_despite_stale_env(monkeypatch, tmp_path):
    # HEALTHY: a just-moved agent — its CMUX_WORKSPACE_ID env is stale (a live move can't rewrite process
    # env) but its record is ADVANCING here (40s). Env-mismatch alone must not condemn an advancing seat.
    surf, st, ws_map, now = _seat(monkeypatch, tmp_path, life="idle", record_age=40, transcript_age=40,
                                  env_ws="WS-OLD", ws_tree="WS-NEW")
    att = rs.attachment(surf, st=st, ws_map=ws_map, now=now)
    assert att["attached"] is True and att["reasons"] == []


def test_dark_agent_env_mismatch_with_frozen_record_is_detached(monkeypatch, tmp_path):
    # STILL FLAGGED: a genuinely dark agent — env mismatch AND the record frozen (channel quiet 20m). The
    # env signal keeps its teeth once the channel is also quiet (this is the usage-ops shape).
    surf, st, ws_map, now = _seat(monkeypatch, tmp_path, life="idle", record_age=1200, transcript_age=1200,
                                  env_ws="WS-OLD", ws_tree="WS-NEW")
    att = rs.attachment(surf, st=st, ws_map=ws_map, now=now)
    assert att["attached"] is False and any(r.startswith("env") for r in att["reasons"])


# ==================== VERDICT 2 — stall (STUCK): via the real doctor sweep ====================
def _doctor_world(monkeypatch, tmp_path, *, life, record_age, transcript_age):
    monkeypatch.setattr(router, "fs", fs)
    monkeypatch.setattr(router, "rs", rs)
    now = time.time()
    tpath = _turn_transcript(tmp_path / "t.jsonl", now, transcript_age)
    rec = _rec("S-CH", "sid-ch", os.getpid(), life, now - record_age, transcript=tpath)
    st = _store({"sid-ch": rec}, {"S-CH": {"sessionId": "sid-ch"}})
    monkeypatch.setattr(fs, "read_hook_store", lambda: st)
    monkeypatch.setattr(rs, "_env_workspace", lambda _pid: "")
    monkeypatch.setattr(rs, "surface_ws_map", lambda ttl=2.0: {})
    monkeypatch.setattr(fs, "wake_if_idle", lambda *a, **k: False)
    router._doctor_fired.clear()
    fs.live_put("parent-c", {"role": "lead", "kind": "conductor", "surface": "S-PAR", "session": "x"})
    fs.live_put("child-a", {"role": "worker", "kind": "child", "parent": "parent-c",
                            "surface": "S-CH", "session": "claude-sid-ch", "tool": "claude"})
    return now


def test_stall_not_flagged_while_transcript_advances(monkeypatch, tmp_path):
    # HEALTHY: a 15-min turn — the record froze 15m ago (inside the stall window) but the transcript is
    # advancing (30s). Token-flow says LIVE; no stall (nor detached) alarm fires.
    now = _doctor_world(monkeypatch, tmp_path, life="running", record_age=900, transcript_age=30)
    assert router.fleet_doctor_sweep(now=now) == 0
    assert [r for r in fs.inbox_read() if r.get("kind") == "doctor"] == []


def test_stall_flagged_when_transcript_also_frozen(monkeypatch, tmp_path):
    # STILL FLAGGED: a genuinely wedged turn — record AND transcript both frozen 15m -> stall fires.
    now = _doctor_world(monkeypatch, tmp_path, life="running", record_age=900, transcript_age=900)
    assert router.fleet_doctor_sweep(now=now) == 1
    rows = [r for r in fs.inbox_read() if r.get("kind") == "doctor"]
    assert len(rows) == 1 and rows[0]["reason"] == "stall" and rows[0]["label"] == "child-a"


def test_detached_not_flagged_for_a_long_running_turn_via_sweep(monkeypatch, tmp_path):
    # end-to-end: the SWEEP (not just attachment) must not alarm a healthy 12-min running turn detached.
    now = _doctor_world(monkeypatch, tmp_path, life="running", record_age=720, transcript_age=30)
    assert router.fleet_doctor_sweep(now=now) == 0


# ==================== VERDICT 3 — stale-gone: a live idle worker via snapshot ====================
def _snapshot_row(monkeypatch, *, has_process):
    # store has NO record for the surface (cmux's record aged out after a long idle) -> lifecycle '' ->
    # _classify reads 'stale'. Whether that is truly gone is settled by the PROCESS TABLE.
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store({}))
    monkeypatch.setattr(ff, "_surface_ws_map", lambda: {})
    monkeypatch.setattr(ff, "_open_gate_uuids", lambda: set())
    monkeypatch.setattr(rs, "pids_ps",
                        lambda surf, ps_out=None, tool=None: (["12345"] if has_process else []))
    fs.live_put("worker-x", {"role": "worker", "kind": "child", "surface": "S-IDLE", "tool": "claude",
                             "session": "claude-sid-x"})
    return {r["label"]: r for r in ff.snapshot()}["worker-x"]


def test_idle_worker_with_live_pid_reads_idle_not_stale(monkeypatch):
    # HEALTHY: a 44-min-idle WORKER whose cmux record aged out — a live pid proves it present. A live
    # agent is NEVER shown gone; it reads the honest 'idle' (present, dormant), not 'stale'.
    row = _snapshot_row(monkeypatch, has_process=True)
    assert row["state"] == "idle"


def test_worker_with_no_pid_and_dropped_record_reads_stale(monkeypatch):
    # STILL FLAGGED: genuinely gone — no record AND no live process -> 'stale' stands (proven absence).
    row = _snapshot_row(monkeypatch, has_process=False)
    assert row["state"] == "stale"
