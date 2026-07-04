# tests/test_fleet_doctor.py — the heartbeat fleet-doctor SWEEP (NOTIFY-LAYER conditions #1/#2/#3).
# router.fleet_doctor_sweep() walks every LIVE child once per heartbeat tick and emits a DEDUPED
# kind='doctor' parent alert on each bad condition (stall / low-ctx / needs-input). This is
# reliability code: the "no false-alarm storm" guarantee (dedup + healthy-agent-skips + muted/conductor
# skips) matters as much as firing correctly, so the false-positive cases below carry equal weight.
#
# Hermetic: the hook store is mocked (fs.read_hook_store), the wake is captured (fs.wake_if_idle) so
# nothing shells out to cmux, and ctx is controlled via features._context_used. Inbox assertions read
# the file-backed inbox, robust to module reloads.
import pytest

from cmux_fleet import router
from cmux_fleet import state as fs
from cmux_fleet import features

NOW = 1_800_000_000.0
STALL_S = router.STALL_S
STALE_UA = NOW - (STALL_S + 60)            # frozen well past the stall threshold
FRESH_UA = NOW - 5                          # a live turn re-stamped 5s ago


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
    fs, features = _state, _features
    monkeypatch.setattr(router, "fs", _state)   # the sweep reads router.fs -> make it the module we patch
    router._doctor_fired.clear()
    yield
    router._doctor_fired.clear()


def _seed_parent_child(child_extra=None, session="claude-cccccccc-1111-2222-3333-444444444444"):
    fs.live_put("parent", {"surface": "PARENT", "kind": "conductor", "role": "c", "session": "claude-parent"})
    entry = {"surface": "CHILD", "kind": "child", "role": "w", "parent": "parent",
             "tool": "claude", "session": session}
    entry.update(child_extra or {})
    fs.live_put("child", entry)
    return session


def _store(life, ua, surface="CHILD", sid="cccccccc-1111-2222-3333-444444444444", transcript=""):
    return {"sessions": {sid: {"sessionId": sid, "surfaceId": surface, "agentLifecycle": life,
                               "updatedAt": ua, "transcriptPath": transcript}},
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


def test_needs_input_dedups_then_rearms_on_leaving(fs, monkeypatch, wake):
    _seed_parent_child()
    store = {"v": _store("needsInput", NOW - 100)}
    monkeypatch.setattr(fs, "read_hook_store", lambda: store["v"])
    assert router.fleet_doctor_sweep(now=NOW) == 1              # fires on entering needsInput
    assert router.fleet_doctor_sweep(now=NOW + 120) == 0        # steady-state wait -> no re-alert
    store["v"] = _store("running", NOW + 130)                  # answered -> back to work (re-arm)
    assert router.fleet_doctor_sweep(now=NOW + 140) == 0
    store["v"] = _store("needsInput", NOW + 200)               # asks AGAIN
    assert router.fleet_doctor_sweep(now=NOW + 210) == 1        # re-fires on the new transition


# ── skips: conductors, muted, unresolved parent ────────────────────────────────────────────────────
def test_conductor_is_never_swept(fs, monkeypatch, wake):
    """A conductor has no parent to alert (branch on KIND, stale-path parity) — even a stalled one."""
    fs.live_put("boss", {"surface": "BOSS", "kind": "conductor", "role": "c", "session": "claude-boss"})
    monkeypatch.setattr(fs, "read_hook_store",
                        lambda: _store("running", STALE_UA, surface="BOSS", sid="boss"))
    fs.live_put("boss", {"surface": "BOSS", "kind": "conductor", "role": "c", "session": "claude-boss"})
    assert router.fleet_doctor_sweep(now=NOW) == 0
    assert fs.inbox_read() == []


def test_muted_child_is_skipped(fs, monkeypatch, wake):
    """Muted = 'this one is my manual concern, don't nudge me'. The three sweep signals are member-health
    nudges — exactly the chatter class mute governs (unlike the surface-VANISHED stale alert). Production
    receipt: 3 of 4 live needsInput members were muted human-driven agents; alerting them = a storm."""
    _seed_parent_child(child_extra={"muted": True})
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


def test_orphan_record_on_surface_does_not_mask_bound(fs, monkeypatch, wake):
    """resolve_bound_record picks the fleet-BOUND session's record, not max-updatedAt: a fresher ORPHAN
    record squatting on the same surface must not hide the bound record's real state (or vice versa)."""
    bound_id = "11111111-1111-1111-1111-111111111111"
    orphan_id = "22222222-2222-2222-2222-222222222222"
    _seed_parent_child(session=f"claude-{bound_id}")
    store = {"sessions": {
        bound_id: {"sessionId": bound_id, "surfaceId": "CHILD",
                   "agentLifecycle": "running", "updatedAt": STALE_UA, "transcriptPath": ""},
        orphan_id: {"sessionId": orphan_id, "surfaceId": "CHILD",   # fresher, but NOT the bound session
                    "agentLifecycle": "running", "updatedAt": NOW - 1, "transcriptPath": ""},
    }, "activeSessionsBySurface": {}}
    monkeypatch.setattr(fs, "read_hook_store", lambda: store)
    n = router.fleet_doctor_sweep(now=NOW)
    assert n == 1                                              # judged on the BOUND (stalled) record...
    assert fs.inbox_pending("PARENT", kind="doctor")[0]["reason"] == "stall"  # ...not the fresh orphan
