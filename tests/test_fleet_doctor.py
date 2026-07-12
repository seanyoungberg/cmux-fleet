# tests/test_fleet_doctor.py — the heartbeat fleet-doctor SWEEP (NOTIFY-LAYER conditions #1/#2/#3).
# router.fleet_doctor_sweep() walks every LIVE child once per heartbeat tick and emits a DEDUPED
# kind='doctor' parent alert on each bad condition (stall / low-ctx / needs-input). This is
# reliability code: the "no false-alarm storm" guarantee (dedup + healthy-agent-skips + muted/conductor
# skips) matters as much as firing correctly, so the false-positive cases below carry equal weight.
#
# Hermetic: the hook store is mocked (fs.read_hook_store), the wake is captured (fs.wake_if_idle) so
# nothing shells out to cmux, and ctx is controlled via features._context_used. Inbox assertions read
# the file-backed inbox, robust to module reloads.
import json
import os

import pytest

from cmux_fleet import router
from cmux_fleet import state as fs
from cmux_fleet import features

NOW = 1_800_000_000.0
STALL_S = router.STALL_S
STALE_UA = NOW - (STALL_S + 60)            # frozen well past the stall threshold
FRESH_UA = NOW - 5                          # a live turn re-stamped 5s ago
# A LIVE member's hook-store record carries a live pid; the sweep's #0 dead-pid guard (2026-07-06)
# treats a dead/None pid as a down ghost and suppresses its health alerts, so every "live member"
# fixture below must model a live pid or the stall/needsInput/low-ctx cases would never fire.
LIVE_PID = os.getpid()


@pytest.fixture(autouse=True)
def _sync(monkeypatch):
    """Keep every module handle CONSISTENT + reset the cross-tick dedup set per test. test_features.py
    re-imports cmux_fleet.{config,state,features} under a throwaway env (its _reset_pkg_modules
    teardown), which leaves the already-imported cmux_fleet.router bound to a STALE state module — and
    this file's module-level fs/features pointing at the old objects too — so a patch would never reach
    the sweep. Point router.fs (and our fs/features globals) at the CURRENT modules via monkeypatch —
    which restores them on teardown, so we never leak a rebinding into test_router.py's own fs handle.
    Then clear _doctor_fired (it persists across ticks, like it does in the long-lived daemon process)."""
    global fs, features
    import cmux_fleet.state as _state
    import cmux_fleet.features as _features
    import cmux_fleet.resolve as _resolve
    fs, features = _state, _features
    monkeypatch.setattr(router, "fs", _state)   # the sweep reads router.fs -> make it the module we patch
    monkeypatch.setattr(router, "rs", _resolve)  # ...and router.rs (the resolver, step 1) the same way
    monkeypatch.setattr(_resolve, "fs", _state)  # resolve delegates to state: keep it on the same module
    # Hermetic attachment inputs: the sweep's detached condition (step 1) reads the tree, ps env, and
    # transcript mtimes. None of this file's fixtures model attachment, so silence all three signals —
    # otherwise a fixture with a frozen record plus a just-written transcript file would fire a spurious
    # 'detached' row and skew the fired-count assertions (test_resolve.py owns the detached cases).
    monkeypatch.setattr(_resolve, "surface_ws_map", lambda ttl=2.0: {})
    monkeypatch.setattr(_resolve, "_env_workspace", lambda pid: "")
    monkeypatch.setattr(_resolve, "_transcript_age", lambda rec, now: None)
    router._doctor_fired.clear()
    router._conductor_live_seen.clear()         # process-local transition guard; reset per test like _doctor_fired
    yield
    router._doctor_fired.clear()
    router._conductor_live_seen.clear()


def _seed_parent_child(child_extra=None, session="claude-cccccccc-1111-2222-3333-444444444444"):
    fs.live_put("parent", {"surface": "PARENT", "kind": "conductor", "role": "c", "session": "claude-parent"})
    entry = {"surface": "CHILD", "kind": "child", "role": "w", "parent": "parent",
             "tool": "claude", "session": session}
    entry.update(child_extra or {})
    fs.live_put("child", entry)
    return session


def _store(life, ua, surface="CHILD", sid="cccccccc-1111-2222-3333-444444444444", transcript="", pid=LIVE_PID):
    return {"sessions": {sid: {"sessionId": sid, "surfaceId": surface, "agentLifecycle": life,
                               "updatedAt": ua, "transcriptPath": transcript, "pid": pid}},
            "activeSessionsBySurface": {}}


@pytest.fixture
def wake(monkeypatch):
    """Capture wake attempts instead of shelling out to cmux; default the dial to auto, the ctx window to
    a FIXED 200k (the host's real fleet.toml sets a 1M window that would otherwise leak in and skew the
    low-ctx %), and ctx-used to unknown (so low-ctx never fires unless a test opts in)."""
    woke = []
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: woke.append(surf) or True)
    monkeypatch.setattr(fs, "idlewake_on", lambda: True)
    monkeypatch.setattr(features, "_context_used", lambda path: (None, ""))
    monkeypatch.setattr(features, "_context_window", lambda model: 200_000)
    return woke


# ── #1 stall ─────────────────────────────────────────────────────────────────────────────────────
def test_stall_fires_once_for_frozen_running(fs, monkeypatch, wake):
    _seed_parent_child()
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("running", STALE_UA))
    n = router.fleet_doctor_sweep(now=NOW)
    assert n == 1
    alerts = fs.inbox_pending("PARENT", kind="doctor")
    assert len(alerts) == 1
    a = alerts[0]
    assert a["reason"] == "stall" and a["label"] == "child" and a["child_surface"] == "CHILD"
    assert a["stalled_s"] >= router.STALL_S
    assert wake == ["PARENT"]                                   # parent woken (dial=auto)


def test_stall_does_not_fire_for_fresh_running(fs, monkeypatch, wake):
    """The important false-positive case: a genuinely-live turn (fresh updatedAt) is NOT a stall."""
    _seed_parent_child()
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("running", FRESH_UA))
    n = router.fleet_doctor_sweep(now=NOW)
    assert n == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []
    assert wake == []


def test_stall_does_not_fire_for_ancient_running(fs, monkeypatch, wake):
    """The OTHER false-positive case (smoke test 2026-07-04): a 'running' record stale for HOURS is a
    done-stuck ghost — cmux left the lifecycle at 'running' after the agent actually finished — NOT a live
    stall. A real stall is caught FRESH (within a tick of crossing STALL_S), so anything past STALL_WINDOW
    was never fresh-caught. Excluding it kills the observed false positives (a finished worker @8.6h)."""
    _seed_parent_child()
    ancient = NOW - (router.STALL_WINDOW + 3600)               # frozen an hour PAST the recent window
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("running", ancient))
    n = router.fleet_doctor_sweep(now=NOW)
    assert n == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []
    assert wake == []


def test_stall_dedups_across_ticks(fs, monkeypatch, wake):
    """A second sweep with the SAME stalled state must NOT re-alert — the no-storm guarantee."""
    _seed_parent_child()
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("running", STALE_UA))
    assert router.fleet_doctor_sweep(now=NOW) == 1
    assert router.fleet_doctor_sweep(now=NOW + 120) == 0        # next tick, same stall -> silent
    assert len(fs.inbox_pending("PARENT", kind="doctor")) == 1  # still exactly one row


def test_stall_rearms_after_recovery(fs, monkeypatch, wake):
    """Once the condition CLEARS (turn resumes), the alarm re-arms so a fresh stall re-fires."""
    _seed_parent_child()
    store = {"v": _store("running", STALE_UA)}
    monkeypatch.setattr(fs, "read_hook_store", lambda: store["v"])
    assert router.fleet_doctor_sweep(now=NOW) == 1              # stalls -> fires
    store["v"] = _store("running", NOW - 3)                     # turn came back to life -> re-arm
    assert router.fleet_doctor_sweep(now=NOW + 120) == 0
    store["v"] = _store("running", NOW - (router.STALL_S + 5))  # stalls AGAIN
    assert router.fleet_doctor_sweep(now=NOW + 240) == 1        # re-fires (re-armed)


# ── #2 low-ctx ───────────────────────────────────────────────────────────────────────────────────
def test_low_ctx_fires_once_at_threshold(fs, monkeypatch, wake):
    _seed_parent_child()
    monkeypatch.setattr(fs, "read_hook_store",
                        lambda: _store("idle", FRESH_UA, transcript="/t/child.jsonl"))
    monkeypatch.setattr(features, "_context_used", lambda path: (150_000, "opus"))  # 200k window -> 25% left
    n = router.fleet_doctor_sweep(now=NOW)
    assert n == 1
    a = fs.inbox_pending("PARENT", kind="doctor")[0]
    assert a["reason"] == "low-ctx" and a["ctx_pct_remaining"] == 25


def test_low_ctx_does_not_fire_when_ample(fs, monkeypatch, wake):
    _seed_parent_child()
    monkeypatch.setattr(fs, "read_hook_store",
                        lambda: _store("idle", FRESH_UA, transcript="/t/child.jsonl"))
    monkeypatch.setattr(features, "_context_used", lambda path: (40_000, "opus"))   # 80% left
    assert router.fleet_doctor_sweep(now=NOW) == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []


def test_low_ctx_skips_unknowable_window(fs, monkeypatch, wake):
    """used=None (codex / unparseable transcript) must never false-alarm on an unknown context size."""
    _seed_parent_child(child_extra={"tool": "codex"})
    monkeypatch.setattr(fs, "read_hook_store",
                        lambda: _store("idle", FRESH_UA, transcript="/t/child.jsonl"))
    monkeypatch.setattr(features, "_context_used", lambda path: (None, ""))
    assert router.fleet_doctor_sweep(now=NOW) == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []


def test_low_ctx_dedups_across_ticks(fs, monkeypatch, wake):
    _seed_parent_child()
    monkeypatch.setattr(fs, "read_hook_store",
                        lambda: _store("idle", FRESH_UA, transcript="/t/child.jsonl"))
    monkeypatch.setattr(features, "_context_used", lambda path: (150_000, "opus"))
    assert router.fleet_doctor_sweep(now=NOW) == 1
    assert router.fleet_doctor_sweep(now=NOW + 120) == 0        # ctx doesn't recover -> single alert
    assert len(fs.inbox_pending("PARENT", kind="doctor")) == 1


# ── #3 needs-input ───────────────────────────────────────────────────────────────────────────────
def test_needs_input_fires_even_when_updatedat_is_days_old(fs, monkeypatch, wake):
    """THE #3 acceptance (loom-dev sat 46h): a genuine needsInput wait FREEZES updatedAt for days, so
    there is deliberately NO freshness gate — resolving the BOUND record of a LIVE member is what
    excludes orphans, not recency. A days-stale bound needsInput record MUST still alert."""
    _seed_parent_child()
    ancient = NOW - 46 * 3600                                   # 46 hours frozen, like loom-dev
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("needsInput", ancient))
    monkeypatch.setattr(features, "pending_interactive_gate", lambda p: True)   # a genuine gate on the transcript
    n = router.fleet_doctor_sweep(now=NOW)
    assert n == 1
    a = fs.inbox_pending("PARENT", kind="doctor")[0]
    assert a["reason"] == "needs-input" and a["label"] == "child"
    assert wake == ["PARENT"]


def test_needs_input_does_not_fire_for_working_child(fs, monkeypatch, wake):
    _seed_parent_child()
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("running", FRESH_UA))
    assert router.fleet_doctor_sweep(now=NOW) == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []


def test_needs_input_dedup_persists_across_daemon_restart_after_ack(fs, monkeypatch, wake):
    """Ack clears the row, not the underlying condition. The condition dedup must survive a daemon
    restart so a steady-state needsInput child does not produce a fresh doctor seq after every restart."""
    _seed_parent_child()
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("needsInput", NOW - 100))
    monkeypatch.setattr(features, "pending_interactive_gate", lambda p: True)   # a genuine gate on the transcript
    assert router.fleet_doctor_sweep(now=NOW) == 1
    alert = fs.inbox_pending("PARENT", kind="doctor")[0]
    fs.inbox_ack("PARENT", "doctor", alert["seq"])

    router._doctor_fired.clear()                         # simulate a fresh daemon process
    assert fs.inbox_pending("PARENT", kind="doctor") == []
    assert router.fleet_doctor_sweep(now=NOW + 120) == 0
    assert len([r for r in fs.inbox_read() if r.get("kind") == "doctor"]) == 1


def test_recent_completion_suppresses_copending_needs_input(fs, monkeypatch, wake):
    """A just-finished child commonly idles at needsInput. The completion row is the real alert; the
    doctor sweep should mark that condition as seen without writing a second doctor row."""
    session = _seed_parent_child()
    fs.inbox_put("completion", "PARENT", {"label": "child", "child_session": session, "gist": "done"})
    completion_ts = fs.inbox_read()[-1]["ts"]
    now = completion_ts + 60
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("needsInput", completion_ts - 1))

    assert router.fleet_doctor_sweep(now=now) == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []
    assert wake == []
    assert ("needs-input", "child", session) in fs.doctor_dedup_load()

    router._doctor_fired.clear()                         # restart should keep the suppressed state quiet
    assert router.fleet_doctor_sweep(now=now + 120) == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []


def test_later_same_session_needs_input_after_new_turn_still_alerts(fs, monkeypatch, wake):
    """A prior turn's completion must not silence a genuine gate in a later turn on the same session.
    The updatedAt stamp after the completion is the available transition proof; this should alert even
    if the coincidence window is accidentally widened again."""
    session = _seed_parent_child()
    fs.inbox_put("completion", "PARENT", {"label": "child", "child_session": session, "gist": "turn 1 done"})
    completion_ts = fs.inbox_read()[-1]["ts"]
    now = completion_ts + 12 * 60
    monkeypatch.setattr(router, "NEEDS_INPUT_COMPLETION_SUPPRESS_S", 30 * 60)
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("needsInput", completion_ts + 11 * 60))
    monkeypatch.setattr(features, "pending_interactive_gate", lambda p: True)   # a genuine gate on the transcript

    assert router.fleet_doctor_sweep(now=now) == 1
    alert = fs.inbox_pending("PARENT", kind="doctor")[0]
    assert alert["reason"] == "needs-input" and alert["label"] == "child"
    assert wake == ["PARENT"]


# ── #iii transcript gate discriminator: real gate vs done-idle (the 100%-FP class) ──────────────────
def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


_GATE_ROWS = [                                                  # last assistant ends on an UNANSWERED gate
    {"type": "user", "message": {"role": "user", "content": "do the thing"}},
    {"type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use",
        "content": [{"type": "text", "text": "I need to ask."},
                    {"type": "tool_use", "name": "AskUserQuestion", "input": {}}]}},
]
_DONE_ROWS = [                                                  # last assistant is a normal end_turn (done-idle)
    {"type": "user", "message": {"role": "user", "content": "do the thing"}},
    {"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "done."}]}},
]


def test_needs_input_fires_on_pending_gate(fs, monkeypatch, wake, tmp_path):
    """End-to-end with a REAL transcript file: a needsInput member whose transcript ends on an UNANSWERED
    AskUserQuestion is a genuine gate (the loom-dev 46h class), so the sweep alerts the parent."""
    _seed_parent_child()
    tp = _write_jsonl(tmp_path / "gate.jsonl", _GATE_ROWS)
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("needsInput", NOW - 100, transcript=tp))
    assert router.fleet_doctor_sweep(now=NOW) == 1
    a = fs.inbox_pending("PARENT", kind="doctor")[0]
    assert a["reason"] == "needs-input" and a["label"] == "child"
    assert wake == ["PARENT"]


def test_needs_input_suppressed_for_done_idle(fs, monkeypatch, wake, tmp_path):
    """The 100%-FP class (timing-test 2026-07-07): cmux stamps needsInput ~60s after ANY turn ends, so a
    done-idle agent (transcript ends stop_reason=end_turn) is indistinguishable from a gate by lifecycle
    alone. The transcript discriminator suppresses it — no doctor row, no wake — and marks it seen. This
    is the survey case (#iv) too: the survey follows a completed end_turn turn."""
    session = _seed_parent_child()
    tp = _write_jsonl(tmp_path / "done.jsonl", _DONE_ROWS)
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("needsInput", NOW - 100, transcript=tp))
    assert router.fleet_doctor_sweep(now=NOW) == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []
    assert wake == []
    assert ("needs-input", "child", session) in fs.doctor_dedup_load()


def test_pending_interactive_gate_discriminator(tmp_path):
    """The pure discriminator: only a trailing UNANSWERED interactive tool_use is a gate. An answered gate
    (a user/tool_result after it), a normal end_turn, an empty path, and an absent file are all not-a-gate
    -> False (fail closed to suppress)."""
    assert features.pending_interactive_gate(_write_jsonl(tmp_path / "g.jsonl", _GATE_ROWS))
    assert not features.pending_interactive_gate(_write_jsonl(tmp_path / "d.jsonl", _DONE_ROWS))
    answered = _GATE_ROWS + [{"type": "user", "message": {"role": "user",
                             "content": [{"type": "tool_result", "content": "answer"}]}}]
    assert not features.pending_interactive_gate(_write_jsonl(tmp_path / "a.jsonl", answered))
    assert not features.pending_interactive_gate("")
    assert not features.pending_interactive_gate(str(tmp_path / "nonexistent.jsonl"))


# ── #0 dead-pid guard (the SessionEnd-freeze class, 2026-07-06) ────────────────────────────────────
def test_dead_pid_ghost_suppresses_false_alerts(fs, monkeypatch, wake):
    """A bound record frozen at 'needsInput' on a DEAD process is a SessionEnd-less ghost (an abrupt
    kill or the SessionEnd store-write race), NOT a live wait. Because #3 has no freshness gate, without
    the pid guard this ghost would nudge the parent EVERY tick, forever (the down-agent-reads-as-live
    class). The pid is authoritative: a dead pid -> suppress, no alert."""
    _seed_parent_child()
    # freshly-frozen needsInput (would pass #3) but the process behind it is dead (pid does not exist).
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("needsInput", NOW - 100, pid=999_999))
    assert router.fleet_doctor_sweep(now=NOW) == 0                 # guard suppresses the false alert
    assert fs.inbox_pending("PARENT", kind="doctor") == []
    # a None pid (the exact berg-sandbox incident fingerprint) is likewise treated as down.
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("running", STALE_UA, pid=None))
    assert router.fleet_doctor_sweep(now=NOW) == 0
    # and a LIVE pid on the same frozen record still alerts (the guard is pid-gated, not blanket-off).
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("running", STALE_UA, pid=LIVE_PID))
    assert router.fleet_doctor_sweep(now=NOW) == 1


def test_needs_input_dedups_then_rearms_on_leaving(fs, monkeypatch, wake):
    _seed_parent_child()
    store = {"v": _store("needsInput", NOW - 100)}
    monkeypatch.setattr(fs, "read_hook_store", lambda: store["v"])
    monkeypatch.setattr(features, "pending_interactive_gate", lambda p: True)   # a genuine gate on the transcript
    assert router.fleet_doctor_sweep(now=NOW) == 1              # fires on entering needsInput
    assert router.fleet_doctor_sweep(now=NOW + 120) == 0        # steady-state wait -> no re-alert
    store["v"] = _store("running", NOW + 130)                  # answered -> back to work (re-arm)
    assert router.fleet_doctor_sweep(now=NOW + 140) == 0
    store["v"] = _store("needsInput", NOW + 200)               # asks AGAIN
    assert router.fleet_doctor_sweep(now=NOW + 210) == 1        # re-fires on the new transition


# ── #5 conductor-liveness: stall-lift (A) + DOWN husk (B), peer + desktop alerting ───────────────────
CONDUCTOR_GRACE = router.CONDUCTOR_DOWN_GRACE_S


def _seed_two_conductors():
    """A down-candidate conductor 'downc' (surface DOWN) + a live peer 'peer' (surface PEER) to receive
    the alert; a conductor has no parent, so the alert fans out to peers + desktop."""
    fs.live_put("peer", {"surface": "PEER", "kind": "conductor", "role": "c", "session": "claude-peer"})
    fs.live_put("downc", {"surface": "DOWN", "kind": "conductor", "role": "c", "session": "claude-down"})


def _no_agents():
    return {"sessions": {}, "activeSessionsBySurface": {}}


def test_conductor_stall_fires_to_peer(fs, monkeypatch, wake):
    """Predicate A: the blanket conductor-skip is lifted for STALL. A conductor whose bound 'running'
    record is frozen in the recent window is a stalled turn — alert the PEER conductor + desktop."""
    _seed_two_conductors()
    monkeypatch.setattr(router, "cmux", lambda *a, **k: "")            # capture the desktop notify
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("running", STALE_UA, surface="DOWN", sid="down"))
    assert router.fleet_doctor_sweep(now=NOW) == 1
    a = fs.inbox_pending("PEER", kind="doctor")[0]
    assert a["reason"] == "stall" and a["label"] == "downc"
    assert wake == ["PEER"]


def test_conductor_needs_input_is_not_swept(fs, monkeypatch, wake):
    """Content gates stay OFF for conductors: a conductor idling at needsInput is not a fleet gate to
    escalate (no parent; the human drives it), so no alert."""
    _seed_two_conductors()
    monkeypatch.setattr(router, "cmux", lambda *a, **k: "")
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("needsInput", NOW - 100, surface="DOWN", sid="down"))
    assert router.fleet_doctor_sweep(now=NOW) == 0
    assert fs.inbox_read() == []


def test_conductor_down_fires_after_grace_once_seen_live(fs, monkeypatch, wake):
    """Predicate B: a conductor SEEN live by this process that becomes a bare-shell husk (no live agent)
    and stays down past the grace window alerts peers + desktop — the ~9h berg-sandbox outage class."""
    _seed_two_conductors()
    monkeypatch.setattr(router, "cmux", lambda *a, **k: "")
    store = {"v": _store("running", FRESH_UA, surface="DOWN", sid="down")}     # downc alive this tick
    monkeypatch.setattr(fs, "read_hook_store", lambda: store["v"])
    assert router.fleet_doctor_sweep(now=NOW) == 0                             # seen live -> recorded, nothing fires
    store["v"] = _no_agents()                                                 # recycle bricked it -> husk shell
    assert router.fleet_doctor_sweep(now=NOW + CONDUCTOR_GRACE + 5) == 1
    a = fs.inbox_pending("PEER", kind="doctor")[0]
    assert a["reason"] == "conductor-down" and a["label"] == "downc"
    assert wake == ["PEER"]


def test_conductor_down_suppressed_within_grace(fs, monkeypatch, wake):
    """A husk INSIDE the grace window is a recycle rebinding, not a death — no alert (600s is generous
    on purpose: a real recovery took minutes of retries, 2026-07-08)."""
    _seed_two_conductors()
    monkeypatch.setattr(router, "cmux", lambda *a, **k: "")
    store = {"v": _store("running", FRESH_UA, surface="DOWN", sid="down")}
    monkeypatch.setattr(fs, "read_hook_store", lambda: store["v"])
    assert router.fleet_doctor_sweep(now=NOW) == 0
    store["v"] = _no_agents()
    assert router.fleet_doctor_sweep(now=NOW + CONDUCTOR_GRACE - 30) == 0      # still inside grace
    assert fs.inbox_read() == []


def test_conductor_down_suppressed_if_never_seen_live(fs, monkeypatch, wake):
    """The reboot/resume-menu storm guard: a conductor this PROCESS never observed live (post-reboot the
    launchd daemon replays launch cmds, leaving conductors unbound at a resume menu) is not a transition
    to DOWN — it must not fire even long past the grace window."""
    _seed_two_conductors()
    monkeypatch.setattr(router, "cmux", lambda *a, **k: "")
    monkeypatch.setattr(fs, "read_hook_store", _no_agents)
    assert router.fleet_doctor_sweep(now=NOW + 10 * CONDUCTOR_GRACE) == 0
    assert fs.inbox_read() == []


def test_conductor_down_rearms_on_recovery(fs, monkeypatch, wake):
    """DOWN fires once, then the conductor is revived (live again) and later dies again — the second death
    re-alerts (dedup re-armed on recovery), not silenced."""
    _seed_two_conductors()
    monkeypatch.setattr(router, "cmux", lambda *a, **k: "")
    store = {"v": _store("running", FRESH_UA, surface="DOWN", sid="down")}
    monkeypatch.setattr(fs, "read_hook_store", lambda: store["v"])
    assert router.fleet_doctor_sweep(now=NOW) == 0                             # seen live
    store["v"] = _no_agents()
    assert router.fleet_doctor_sweep(now=NOW + CONDUCTOR_GRACE + 5) == 1       # down -> fires
    assert router.fleet_doctor_sweep(now=NOW + CONDUCTOR_GRACE + 10) == 0      # steady down -> deduped
    store["v"] = _store("running", NOW + CONDUCTOR_GRACE + 20, surface="DOWN", sid="down")  # revived
    assert router.fleet_doctor_sweep(now=NOW + CONDUCTOR_GRACE + 25) == 0      # recovered, re-armed
    store["v"] = _no_agents()
    assert router.fleet_doctor_sweep(now=NOW + 3 * CONDUCTOR_GRACE) == 1       # dies again -> re-alerts


def test_conductor_seen_clock_pruned_on_removal(fs, monkeypatch, wake):
    """The seen-live clock is pruned when a conductor leaves the registry, so a REUSED label that starts
    as a husk is 'never seen live by this process' again — no false DOWN from a stale timestamp."""
    _seed_two_conductors()
    monkeypatch.setattr(router, "cmux", lambda *a, **k: "")
    store = {"v": _store("running", FRESH_UA, surface="DOWN", sid="down")}
    monkeypatch.setattr(fs, "read_hook_store", lambda: store["v"])
    assert router.fleet_doctor_sweep(now=NOW) == 0                 # downc seen live -> clock set
    assert "downc" in router._conductor_live_seen
    fs.live_del("downc")                                          # conductor removed from the registry
    store["v"] = _no_agents()
    assert router.fleet_doctor_sweep(now=NOW + 5) == 0
    assert "downc" not in router._conductor_live_seen             # pruned on removal
    _seed_two_conductors()                                        # label reused, starts as a husk
    assert router.fleet_doctor_sweep(now=NOW + 2 * CONDUCTOR_GRACE) == 0   # never-seen guard holds -> no fire
    assert fs.inbox_read() == []


# ── skips: muted, unresolved parent ─────────────────────────────────────────────────────────────────


def test_muted_child_is_skipped(fs, monkeypatch, wake):
    """Muted = 'this one is my manual concern, don't nudge me'. The three sweep signals are member-health
    nudges — exactly the chatter class mute governs (unlike the surface-VANISHED stale alert). Production
    receipt: 3 of 4 live needsInput members were muted human-driven agents; alerting them = a storm."""
    _seed_parent_child(child_extra={"muted": True})
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("needsInput", NOW - 100))
    assert router.fleet_doctor_sweep(now=NOW) == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []
    assert wake == []


def test_expected_close_surface_is_skipped_by_doctor_sweep(fs, monkeypatch, wake):
    """A CLI-close tombstone means the surface is being deliberately archived/removed. The doctor sweep
    must share the stale-alert guard and not flag the same surface during that close race."""
    _seed_parent_child()
    fs.expected_close_put("CHILD", now=NOW)
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("needsInput", NOW - 100))

    assert router.fleet_doctor_sweep(now=NOW) == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []
    assert wake == []


def test_non_live_session_bound_surface_is_skipped_by_doctor_sweep(fs, monkeypatch, wake):
    """A session-bound registry row whose surface no longer has a live agent is stale by the same
    predicate used by ls/bulk-recycle, so the doctor sweep should not produce health alerts for it."""
    _seed_parent_child()
    monkeypatch.setattr(fs, "surface_has_live_agent", lambda surf: False)
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("needsInput", NOW - 100))

    assert router.fleet_doctor_sweep(now=NOW) == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []
    assert wake == []


def test_unresolved_parent_falls_back_without_touching_bystanders(fs, monkeypatch, wake):
    """An unresolved parent LABEL falls back to using the label as a raw surface — stale-path parity
    (_archive_closed_surface: `pe.get('surface') if pe else parent`). It must never crash and never
    alert a real BYSTANDER conductor; the fallback row goes to the bogus 'ghost' surface (harmless —
    nobody reads it) so this vestigial path is contained."""
    fs.live_put("bystander", {"surface": "BYST", "kind": "conductor", "session": "claude-byst"})
    fs.live_put("orphan", {"surface": "ORPH", "kind": "child", "role": "w", "parent": "ghost",
                           "tool": "claude", "session": "claude-orphan"})
    monkeypatch.setattr(fs, "read_hook_store",
                        lambda: _store("running", STALE_UA, surface="ORPH", sid="orphan"))
    assert router.fleet_doctor_sweep(now=NOW) == 1            # fires to the 'ghost' fallback surface
    assert fs.inbox_pending("BYST", kind="doctor") == []     # ...a real bystander is NOT alerted
    assert fs.inbox_pending("ghost", kind="doctor")[0]["label"] == "orphan"


# ── dial + recycle behavior ────────────────────────────────────────────────────────────────────────
def test_passive_writes_inbox_but_does_not_wake(fs, monkeypatch, wake):
    """'passive' is a WAKE mute, not an INBOX mute: the alert is still written (surfaces via awareness
    next turn) but no idle-wake is injected — mirrors the completion/stale channel under passive."""
    monkeypatch.setattr(fs, "idlewake_on", lambda: False)      # dial = passive
    _seed_parent_child()
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store("running", STALE_UA))
    n = router.fleet_doctor_sweep(now=NOW)
    assert n == 1
    assert len(fs.inbox_pending("PARENT", kind="doctor")) == 1  # row still written
    assert wake == []                                           # ...but no wake under passive


def test_recycle_new_session_realerts(fs, monkeypatch, wake):
    """A recycle rebinds a NEW session id; the dedup key includes session, so a still-bad member after a
    recycle re-alerts once under its new session (old key pruned) rather than staying silenced."""
    _seed_parent_child(session="claude-old-session")
    monkeypatch.setattr(fs, "read_hook_store",
                        lambda: _store("running", STALE_UA, sid="old-session"))
    assert router.fleet_doctor_sweep(now=NOW) == 1
    # recycle: registry session changes + a fresh hook record under the new id, still stalled
    _seed_parent_child(session="claude-new-session")
    monkeypatch.setattr(fs, "read_hook_store",
                        lambda: _store("running", STALE_UA, sid="new-session"))
    assert router.fleet_doctor_sweep(now=NOW + 120) == 1       # re-alerts under the new session
    assert len(router._doctor_fired) == 1                       # old key pruned (set didn't grow)


# ── #4 never-bound (P0-4: a LAZY child launched, no session bound past NEVER_BOUND_S) ───────────────
AGED_LAUNCH = NOW - (router.NEVER_BOUND_S + 60)      # registered long enough ago to be past the grace window
FRESH_LAUNCH = NOW - 10                               # just launched, still inside the boot/drive grace


def _seed_never_bound(launched_at, tool="codex"):
    """A parent + a lazily-registered child with NO session bound (the pending state)."""
    fs.live_put("parent", {"surface": "PARENT", "kind": "conductor", "role": "c", "session": "claude-parent"})
    fs.live_put("child", {"surface": "CHILD", "kind": "child", "role": "w", "parent": "parent",
                          "tool": tool, "session": "", "launchedAt": launched_at})
    # the top-level st read still happens; give it an empty store (the never-bound branch never resolves a record).
    return {"sessions": {}, "activeSessionsBySurface": {}}


def test_never_bound_fires_when_aged_and_pane_shows_error(fs, monkeypatch, wake):
    """The incident (2026-07-07): codex died on a bad flag, sat 'pending' unnoticed. Past the grace
    window with a startup ERROR on the pane, the sweep alerts the parent — deduped, wake-routed."""
    store = _seed_never_bound(AGED_LAUNCH)
    monkeypatch.setattr(fs, "read_hook_store", lambda: store)
    monkeypatch.setattr(router, "_surface_error_line",
                        lambda surf: "error: unexpected argument '--effort' found")
    n = router.fleet_doctor_sweep(now=NOW)
    assert n == 1
    a = fs.inbox_pending("PARENT", kind="doctor")[0]
    assert a["reason"] == "never-bound" and a["label"] == "child" and a["child_surface"] == "CHILD"
    assert "unexpected argument" in a["pane_error"] and a["pending_s"] >= router.NEVER_BOUND_S
    assert wake == ["PARENT"]


def test_never_bound_does_not_fire_without_a_pane_error(fs, monkeypatch, wake):
    """THE false-positive case: a healthy child launched in a batch and NOT YET DRIVEN is also pending
    with no session. It shows its TUI, not an error, so the sweep must NOT alert (log-only)."""
    store = _seed_never_bound(AGED_LAUNCH)
    monkeypatch.setattr(fs, "read_hook_store", lambda: store)
    monkeypatch.setattr(router, "_surface_error_line", lambda surf: "")   # healthy TUI, no error
    assert router.fleet_doctor_sweep(now=NOW) == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []
    assert wake == []


def test_never_bound_does_not_fire_within_grace_window(fs, monkeypatch, wake):
    """A just-launched child mid-cold-boot is legitimately unbound; even a transient error line on the
    pane must not fire before the grace window — the sweep never even reads the pane inside it."""
    store = _seed_never_bound(FRESH_LAUNCH)
    monkeypatch.setattr(fs, "read_hook_store", lambda: store)
    monkeypatch.setattr(router, "_surface_error_line",
                        lambda surf: pytest.fail("must not scan the pane inside the grace window"))
    assert router.fleet_doctor_sweep(now=NOW) == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []


def test_never_bound_skips_claude(fs, monkeypatch, wake):
    """claude binds at boot (a failed bind sys.exits BEFORE register), so a no-session live row is never
    a claude — the branch skips it defensively rather than scanning the pane for one."""
    store = _seed_never_bound(AGED_LAUNCH, tool="claude")
    monkeypatch.setattr(fs, "read_hook_store", lambda: store)
    monkeypatch.setattr(router, "_surface_error_line",
                        lambda surf: pytest.fail("must not scan the pane for a claude row"))
    assert router.fleet_doctor_sweep(now=NOW) == 0
    assert fs.inbox_pending("PARENT", kind="doctor") == []


def test_never_bound_dedups_then_rearms_on_bind(fs, monkeypatch, wake):
    """Fires once, silent on the next tick (no storm); when the child finally BINDS a session the pending
    key leaves live_keys and the dedup prunes, so a future never-bound would re-fire fresh."""
    store = _seed_never_bound(AGED_LAUNCH)
    monkeypatch.setattr(fs, "read_hook_store", lambda: store)
    monkeypatch.setattr(router, "_surface_error_line", lambda surf: "error: unexpected argument")
    assert router.fleet_doctor_sweep(now=NOW) == 1
    assert router.fleet_doctor_sweep(now=NOW + 120) == 0             # same pending state -> silent
    assert len(fs.inbox_pending("PARENT", kind="doctor")) == 1
    # child binds on its first turn: session backfills -> the ("never-bound","child","") key prunes.
    fs.live_put("child", {"surface": "CHILD", "kind": "child", "role": "w", "parent": "parent",
                          "tool": "codex", "session": "codex-boundnow", "launchedAt": AGED_LAUNCH})
    monkeypatch.setattr(fs, "surface_has_live_agent", lambda surf: True)
    monkeypatch.setattr(fs, "read_hook_store",
                        lambda: _store("idle", FRESH_UA, sid="boundnow"))
    router.fleet_doctor_sweep(now=NOW + 240)
    assert ("never-bound", "child", "") not in router._doctor_fired  # re-armed


def test_orphan_record_on_surface_does_not_mask_bound(fs, monkeypatch, wake):
    """resolve_bound_record picks the fleet-BOUND session's record, not max-updatedAt: a fresher ORPHAN
    record squatting on the same surface must not hide the bound record's real state (or vice versa)."""
    bound_id = "11111111-1111-1111-1111-111111111111"
    orphan_id = "22222222-2222-2222-2222-222222222222"
    _seed_parent_child(session=f"claude-{bound_id}")
    store = {"sessions": {
        bound_id: {"sessionId": bound_id, "surfaceId": "CHILD", "pid": LIVE_PID,
                   "agentLifecycle": "running", "updatedAt": STALE_UA, "transcriptPath": ""},
        orphan_id: {"sessionId": orphan_id, "surfaceId": "CHILD", "pid": LIVE_PID,  # fresher, but NOT the bound session
                    "agentLifecycle": "running", "updatedAt": NOW - 1, "transcriptPath": ""},
    }, "activeSessionsBySurface": {}}
    monkeypatch.setattr(fs, "read_hook_store", lambda: store)
    n = router.fleet_doctor_sweep(now=NOW)
    assert n == 1                                              # judged on the BOUND (stalled) record...
    assert fs.inbox_pending("PARENT", kind="doctor")[0]["reason"] == "stall"  # ...not the fresh orphan


# ── PID AUTHORITY: a LIVE agent is never DOWN, and never gets a destructive remedy ───────────────────
# The 2026-07-11 session-killer (cmux-advisor). fleet-doctor told the fleet that conductor berg-sandbox
# "appears DOWN (stall) ... `fleet revive berg-sandbox`". berg-sandbox was NOT down: Berg — the human —
# was sitting in it TYPING, pid up, 88% context. A human composing a message produces the stall signal
# exactly (turn 'running', no Stop hook, updatedAt frozen), and conductors are precisely where humans
# sit — so the false-positive rate is highest where acting is most expensive. Obeying the advice would
# have destroyed the live session: `revive` archives the agent and relands it on a FRESH surface, so the
# advertised "remedy" for the false positive KILLS the thing it falsely accused. The doctor also
# contradicted itself — its own inbox header says "still LIVE — a health alert, not an archive".
#
# These pin the INVARIANT, not the prose: whatever an alert says about a LIVE pid, it may never call it
# DOWN and never route to revive/archive/--force. They assert on every channel the advice reaches a peer
# or a human through — the peer WAKE line, the DESKTOP banner, and the INBOX row — because that is what
# stops this regressing into a session-killer.
from cmux_fleet import hookverbs

DESTRUCTIVE = ("revive", "archive", "--force")
SANDBOX, SANDBOX_SURF = "berg-sandbox", "5AFE0000-0000-4000-8000-000000000001"


def _scrub(text, *ids):
    """Lowercased text with the label/surface blanked: an IDENTIFIER may contain 'down' (the shared
    fixture's conductor is literally 'downc' on surface 'DOWN'), and that is not a DOWN verdict. We are
    asserting on what the doctor SAYS, so the identifiers must not be able to launder or trip it."""
    low = text.lower()
    for i in ids:
        if i:
            low = low.replace(i.lower(), "<id>")
    return low


def _assert_safe_for_live(text, where, *ids):
    """The duty of care owed to a LIVE pid: never DOWN, never destructive, and it must say outright that
    a human may simply be typing in it."""
    low = _scrub(text, *ids)
    assert "down" not in low, f"{where}: a LIVE pid was described as DOWN -- {text!r}"
    for verb in DESTRUCTIVE:
        assert verb not in low, f"{where}: destructive remedy {verb!r} offered for a LIVE pid -- {text!r}"
    assert "live" in low, f"{where}: must state the agent is still live -- {text!r}"
    assert "typing" in low, f"{where}: must say a human may simply be typing -- {text!r}"


def _seed_sandbox_and_peer():
    """The incident shape: a LIVE conductor (berg-sandbox, a human typing in it) + a peer to alert."""
    fs.live_put("peer", {"surface": "PEER", "kind": "conductor", "role": "c", "session": "claude-peer"})
    fs.live_put(SANDBOX, {"surface": SANDBOX_SURF, "kind": "conductor", "role": "c",
                          "session": "claude-sandbox"})


@pytest.mark.parametrize("reason", ["stall", "detached"])
def test_live_conductor_alert_is_never_down_and_never_destructive(fs, reason):
    """The unit invariant, over BOTH reasons that fire on a PRESENT surface. conductor_alert_text is the
    ONE place these words are written and it is gated on the pid — so this holds for any future reason
    routed through it, not just the two that exist today."""
    wake, title, body = router.conductor_alert_text(reason, SANDBOX, SANDBOX_SURF, live=True)
    for text, where in ((wake, "peer wake"), (title, "desktop title"), (body, "desktop body")):
        low = _scrub(text, SANDBOX, SANDBOX_SURF)
        assert "down" not in low, f"{where}: a LIVE pid was described as DOWN -- {text!r}"
        for verb in DESTRUCTIVE:
            assert verb not in low, f"{where}: destructive remedy {verb!r} for a LIVE pid -- {text!r}"
    _assert_safe_for_live(wake, "peer wake", SANDBOX, SANDBOX_SURF)   # the line a peer conductor ACTS on
    assert "inspect" in wake.lower()                                   # ...and the correct action is INSPECT


def test_down_conductor_still_gets_the_down_alarm(fs):
    """The other half: the fix must not disarm the REAL alarm. With no live pid the conductor IS down and
    `revive` IS the remedy — that text must survive intact."""
    wake, title, body = router.conductor_alert_text("conductor-down", SANDBOX, SANDBOX_SURF, live=False)
    assert "appears DOWN" in wake and f"fleet revive {SANDBOX}" in wake
    assert "DOWN" in title and "revive" in body


def test_berg_sandbox_repro_live_conductor_stall_never_says_down_or_revive(fs, monkeypatch, wake):
    """END-TO-END through the real sweep on the live-observed shape: a conductor whose bound 'running'
    record froze past STALL_S while its pid is ALIVE (Berg typing). The sweep still ALERTS — a stalled
    conductor is worth a look — but every channel must route to INSPECT, not to a killer."""
    msgs, banners = [], []
    _seed_sandbox_and_peer()
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: msgs.append((surf, msg)) or True)
    monkeypatch.setattr(router, "cmux", lambda *a, **k: banners.append(a) or "")
    monkeypatch.setattr(fs, "read_hook_store",
                        lambda: _store("running", STALE_UA, surface=SANDBOX_SURF, sid="sandbox"))
    assert router.fleet_doctor_sweep(now=NOW) == 1
    assert fs.surface_has_live_agent(SANDBOX_SURF) is True        # precondition: the pid really IS alive

    row = fs.inbox_pending("PEER", kind="doctor")[0]
    assert row["reason"] == "stall" and row["live"] is True        # the row records the pid verdict
    (surf, wake_msg), = msgs
    assert surf == "PEER"
    _assert_safe_for_live(wake_msg, "peer wake", SANDBOX, SANDBOX_SURF)

    notify, = [a for a in banners if a and a[0] == "notify"]
    banner = _scrub(" ".join(notify), SANDBOX, SANDBOX_SURF)
    assert "down" not in banner, f"desktop banner called a LIVE conductor DOWN: {banner!r}"
    for verb in DESTRUCTIVE:
        assert verb not in banner, f"desktop banner offered {verb!r} for a LIVE conductor: {banner!r}"

    # ...and the INBOX row the peer actually reads must agree with its own header ("still LIVE").
    _assert_safe_for_live(hookverbs._doctor_line(row), "inbox row", SANDBOX, SANDBOX_SURF)


def test_dead_conductor_end_to_end_still_says_down_and_revive(fs, monkeypatch, wake):
    """Guards the fix against over-correcting into silence: a REAL death (no live agent on the surface)
    still reaches peers + desktop as DOWN, with revive."""
    msgs, banners = [], []
    _seed_sandbox_and_peer()
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: msgs.append((surf, msg)) or True)
    monkeypatch.setattr(router, "cmux", lambda *a, **k: banners.append(a) or "")
    store = {"v": _store("running", FRESH_UA, surface=SANDBOX_SURF, sid="sandbox")}   # seen live once...
    monkeypatch.setattr(fs, "read_hook_store", lambda: store["v"])
    assert router.fleet_doctor_sweep(now=NOW) == 0
    store["v"] = _no_agents()                                                         # ...then the pid dies
    assert router.fleet_doctor_sweep(now=NOW + CONDUCTOR_GRACE + 5) == 1

    row = fs.inbox_pending("PEER", kind="doctor")[0]
    assert row["reason"] == "conductor-down" and row["live"] is False
    (_, wake_msg), = msgs
    assert "appears DOWN" in wake_msg and f"fleet revive {SANDBOX}" in wake_msg
    assert "DOWN" in " ".join(banners[-1]) and "revive" in " ".join(banners[-1])


def test_stall_row_for_a_live_child_is_inspect_first(fs):
    """The child channel owes the same duty: a stall row ONLY ever fires on a live pid (the sweep skips
    no-live-agent surfaces and dead-pid ghosts outright), so its rendered advice must not read as a
    death notice either."""
    line = hookverbs._doctor_line({"reason": "stall", "label": "worker", "seq": 1,
                                   "child_surface": SANDBOX_SURF, "stalled_s": 600})
    _assert_safe_for_live(line, "child stall row", "worker", SANDBOX_SURF)
    assert f"capture-pane --surface {SANDBOX_SURF}" in line     # the FULL uuid -> the command is runnable
