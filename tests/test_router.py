# tests/test_router.py — the bus-level SINGLETON GUARD. Only one `router.py --live` may process a
# given state dir's bus; a second one that can't acquire the exclusive flock must exit instead of
# double-processing (the cutover bug: 3 strays triple-processed the bus). flock is per-open-file-
# description, so a SECOND open()+flock in the same process conflicts with the first — that's what lets
# these tests simulate a "first router already holding the lock" without spawning a real router.
import fcntl
import os
import sys

import pytest


from cmux_fleet import router  # noqa: E402


def _release_router_lock():
    """Drop any lock the module-global holds so tests don't leak it across cases."""
    fd = router._lock_fd
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
        except OSError:
            pass
        router._lock_fd = None


def test_first_live_router_acquires_and_records_pid():
    _release_router_lock()
    router.acquire_singleton_lock()
    assert router._lock_fd is not None
    assert open(router.LOCKFILE).read().strip() == str(os.getpid())   # winner stamps its pid
    _release_router_lock()


def test_second_live_router_refuses_when_locked():
    _release_router_lock()
    # simulate a first live router: a SEPARATE fd holding the exclusive lock (independent open-file-
    # description -> conflicts with acquire_singleton_lock's own open, even in one process).
    holder = open(router.LOCKFILE, "a+")
    holder.seek(0)
    holder.truncate()
    holder.write("99999")                              # the "other router"'s pid, for the message
    holder.flush()
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(SystemExit) as ei:
            router.acquire_singleton_lock()
        assert ei.value.code != 0                      # non-zero: refused, did not start
        assert router._lock_fd is None                 # never took the lock
        assert open(router.LOCKFILE).read().strip() == "99999"   # holder's pid NOT clobbered
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


def test_lock_is_reusable_after_release():
    # once the holder releases, the next router acquires cleanly (no stale-lock wedge).
    _release_router_lock()
    holder = open(router.LOCKFILE, "a+")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fcntl.flock(holder, fcntl.LOCK_UN)                 # released
    holder.close()
    router.acquire_singleton_lock()                    # must succeed now
    assert router._lock_fd is not None
    _release_router_lock()


# --- event-driven idle-wake retry (design 2.2b, Phase 3) -----------------------------------------
# When an idle-wake is skipped (parent mid-turn at event time), a bounded background loop re-fires the
# WAKE ONLY over ~30s so latency is seconds, not the 2m heartbeat — never re-delivering content.
from cmux_fleet import state as fs  # noqa: E402


def test_maybe_idle_wake_schedules_retry_on_skip_on_running(monkeypatch):
    monkeypatch.setattr(router, "LIVE", True)
    monkeypatch.setattr(fs, "idlewake_on", lambda: True)
    monkeypatch.setattr(fs, "inbox_pending", lambda surf, kind=None: [{"seq": 1}])
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: False)     # skipped
    monkeypatch.setattr(fs, "surface_busy", lambda s: True)             # ...because genuinely mid-turn
    scheduled = []
    monkeypatch.setattr(router, "_schedule_idle_wake_retry", lambda surf, label: scheduled.append(surf))
    router.maybe_idle_wake("S", "cond")
    assert scheduled == ["S"]                                           # retry (parent goes idle soon)


def test_maybe_idle_wake_no_retry_on_draft_or_noprompt_skip(monkeypatch):
    # codex: a draft / no-clean-prompt skip must NOT spawn a guaranteed-useless retry loop (heartbeat
    # is the backstop). Only skip-on-running retries.
    monkeypatch.setattr(router, "LIVE", True)
    monkeypatch.setattr(fs, "idlewake_on", lambda: True)
    monkeypatch.setattr(fs, "inbox_pending", lambda surf, kind=None: [{"seq": 1}])
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: False)     # skipped
    monkeypatch.setattr(fs, "surface_busy", lambda s: False)           # ...NOT mid-turn (draft/no prompt)
    scheduled = []
    monkeypatch.setattr(router, "_schedule_idle_wake_retry", lambda surf, label: scheduled.append(surf))
    router.maybe_idle_wake("S", "cond")
    assert scheduled == []                                              # no useless retry spin


def test_maybe_idle_wake_no_retry_on_success(monkeypatch):
    monkeypatch.setattr(router, "LIVE", True)
    monkeypatch.setattr(fs, "idlewake_on", lambda: True)
    monkeypatch.setattr(fs, "inbox_pending", lambda surf, kind=None: [{"seq": 1}])
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: True)      # woke immediately
    scheduled = []
    monkeypatch.setattr(router, "_schedule_idle_wake_retry", lambda surf, label: scheduled.append(surf))
    router.maybe_idle_wake("S", "cond")
    assert scheduled == []                                              # nothing to retry


def test_idle_wake_retry_loop_wakes_then_stops(monkeypatch):
    router._retrying.clear()
    monkeypatch.setattr(router.time, "sleep", lambda s: None)           # no real waiting
    monkeypatch.setattr(fs, "idlewake_on", lambda: True)
    monkeypatch.setattr(fs, "inbox_pending", lambda surf, kind=None: [{"seq": 1}])
    monkeypatch.setattr(fs, "surface_busy", lambda s: True)             # still mid-turn between tries
    n = {"wake": 0}
    def wake(surf, msg):
        n["wake"] += 1
        return n["wake"] >= 2                                           # False, then True on the 2nd try
    monkeypatch.setattr(fs, "wake_if_idle", wake)
    router._retrying.add("S")                                           # as _schedule would have
    router._idle_wake_retry_loop("S", "cond")
    assert n["wake"] == 2                                               # retried until it woke
    assert "S" not in router._retrying                                  # cleared in finally


def test_idle_wake_retry_loop_stops_when_inbox_drained(monkeypatch):
    router._retrying.clear()
    monkeypatch.setattr(router.time, "sleep", lambda s: None)
    monkeypatch.setattr(fs, "idlewake_on", lambda: True)
    monkeypatch.setattr(fs, "inbox_pending", lambda surf, kind=None: [])   # already handled
    waked = []
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: waked.append(surf) or True)
    router._retrying.add("S")
    router._idle_wake_retry_loop("S", "cond")
    assert waked == []                                                  # never re-fired the wake
    assert "S" not in router._retrying


def test_idle_wake_retry_loop_never_redelivers_content(monkeypatch):
    # the retry re-fires the WAKE only; it must never touch inbox_put -> no duplicate rows.
    router._retrying.clear()
    monkeypatch.setattr(router.time, "sleep", lambda s: None)
    monkeypatch.setattr(fs, "idlewake_on", lambda: True)
    monkeypatch.setattr(fs, "inbox_pending", lambda surf, kind=None: [{"seq": 1}])
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: False)    # never wakes -> exhausts backoff
    monkeypatch.setattr(fs, "surface_busy", lambda s: True)            # still mid-turn (don't early-stop)
    put = []
    monkeypatch.setattr(fs, "inbox_put", lambda *a, **k: put.append(a))
    router._retrying.add("S")
    router._idle_wake_retry_loop("S", "cond")
    assert put == []                                                   # zero re-delivery
    assert "S" not in router._retrying


def test_idle_wake_retry_loop_stops_when_muted_midflight(monkeypatch):
    # if the dial flips to 'passive' during the retry window, stop re-attempting.
    router._retrying.clear()
    monkeypatch.setattr(router.time, "sleep", lambda s: None)
    monkeypatch.setattr(fs, "idlewake_on", lambda: False)              # muted
    waked = []
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: waked.append(surf) or True)
    router._retrying.add("S")
    router._idle_wake_retry_loop("S", "cond")
    assert waked == []                                                 # muted -> never wakes
    assert "S" not in router._retrying


def test_schedule_idle_wake_retry_dedups(monkeypatch):
    router._retrying.clear()
    spawned = []
    class _FakeThread:
        def __init__(self, *a, **k): spawned.append(k.get("args"))
        def start(self): pass
    monkeypatch.setattr(router.threading, "Thread", _FakeThread)
    router._schedule_idle_wake_retry("S", "cond")
    router._schedule_idle_wake_retry("S", "cond")                     # same surface, retry in flight
    assert len(spawned) == 1                                          # only one loop spawned
    router._retrying.discard("S")                                     # cleanup (FakeThread never clears)


# --- router bus-consumption health stamp (design enrichment: wedge-detection, Phase 4) -----------
def test_stamp_health_writes_fresh_pid_and_ts():
    # the router stamps its OWN pid + a recent ts on each consumed bus frame so the daemon can prove it
    # is processing the bus (not merely alive) and flag a wedge.
    import json, os, time
    router._health["ts"] = 0.0                                        # reset the write-throttle
    router._stamp_health(force=True)
    h = json.load(open(router.HEALTH_FILE))
    assert h["pid"] == os.getpid()
    assert abs(h["ts"] - time.time()) < 5


# --- root cause #3: moved/desynced child completion recovered via registry truth -----------------
# A running child whose surface was MOVED across workspaces loses its live hook-store `sessions{}`
# record, leaving only a frozen `activeSessionsBySurface` pointer. The pre-fix handle() resolved the
# surface from that missing session record and RETURNED before queueing anything — a silent completion
# loss that stalled the parent (never woken). handle() must now fall back to fleet-REGISTRY truth.
def test_handle_recovers_moved_child_via_registry_when_hookstore_session_absent(fs, monkeypatch):
    """A Stop whose bus session_id matches a LIVE registry row while the hook-store `sessions{}` entry
    is ABSENT must still queue a completion for the parent AND attempt a wake — with a thin/empty gist
    (the transcript is gone) rather than dropping. This is the codex-named root-cause-#3 regression."""
    uuid = "11111111-1111-1111-1111-111111111111"
    fs.live_put("parent", {"surface": "PARENT", "kind": "conductor", "role": "c",
                           "session": "claude-parent"})
    fs.live_put("child", {"surface": "CHILD", "kind": "child", "role": "w", "parent": "parent",
                          "session": f"claude-{uuid}"})

    monkeypatch.setattr(router, "LIVE", True)
    monkeypatch.setattr(router, "_reg", {"mtime": 0, "by_label": {}, "by_surface": {}})  # force a reload
    # THE DESYNC: hook store has NO sessions{} record for the child (its live record vanished on the
    # move) — only the frozen activeSessionsBySurface pointer, so _rec_by_session finds no surface.
    monkeypatch.setattr(router, "store",
                        lambda: {"sessions": {}, "activeSessionsBySurface": {"CHILD": {"sessionId": uuid}}})
    monkeypatch.setattr(router, "cmux", lambda *a, **k: "")            # no real `cmux notify` shell-out
    monkeypatch.setattr(router.time, "sleep", lambda s: None)          # skip deliver()'s flush sleep
    # Record the wake attempt at the router's wake entrypoint. Patching `router.maybe_idle_wake` (a
    # module global that deliver() resolves at call time) is stable across the suite — unlike patching
    # `fs.wake_if_idle`, which desyncs when another test reloads the state module.
    waked = []
    monkeypatch.setattr(router, "maybe_idle_wake", lambda parent_surface, label: waked.append(parent_surface))

    router.handle({"name": "agent.hook.Stop", "occurred_at": "2026-07-01T12:00:00Z",
                   "payload": {"phase": "completed", "session_id": f"claude-{uuid}"}})

    pending = fs.inbox_pending("PARENT", kind="completion")           # file-backed: robust to module reloads
    assert len(pending) == 1                                          # completion QUEUED, not silently dropped
    row = pending[0]
    assert row["label"] == "child"
    assert row["child_surface"] == "CHILD"
    assert row["gist"] == ""                                          # thin gist (transcript gone) beats loss
    assert waked == ["PARENT"]                                        # ...and the parent's wake was attempted


# --- tool-mismatch on a RESOLVED surface must not fall through to delivery -------------------------
# A codex Stop can resolve, via a stale/bad hook-store `sessions{}` record, to a CLAUDE-typed member's
# surface in the PRIMARY (_rec_by_session/by_surface) lookup -- reconcile_session() correctly refuses to
# write the cross-tool id (returns 'skip-tool'), but that skip IS the signal the resolution was wrong: the
# pre-fix handle() fell through unconditionally to debounce/log/deliver, re-queueing the member's STALE
# last-known completion to its parent on every such Stop (the berg-sandbox ack-loop).
def test_handle_skips_delivery_on_tool_mismatch(fs, monkeypatch):
    uuid = "33333333-3333-3333-3333-333333333333"
    fs.live_put("parent", {"surface": "PARENT", "kind": "conductor", "role": "c",
                           "session": "claude-parent"})
    fs.live_put("memsearch-expert", {"surface": "CHILD", "kind": "child", "role": "w",
                                     "parent": "parent", "tool": "claude",
                                     "session": "claude-stale-uuid-on-record"})

    monkeypatch.setattr(router, "LIVE", True)
    monkeypatch.setattr(router, "_reg", {"mtime": 0, "by_label": {}, "by_surface": {}})  # force a reload
    # THE STALE RECORD: the hook store's sessions{} entry for this bare uuid resolves (via the PRIMARY
    # _rec_by_session/by_surface lookup) to CHILD, a claude-typed entry -- but the bus event that produced
    # this uuid is a CODEX Stop, so entry_tool != ev_tool -> reconcile_session() returns 'skip-tool'.
    monkeypatch.setattr(router, "store",
                        lambda: {"sessions": {uuid: {"sessionId": uuid, "surfaceId": "CHILD"}},
                                 "activeSessionsBySurface": {"CHILD": {"sessionId": uuid}}})
    monkeypatch.setattr(router, "cmux", lambda *a, **k: "")            # no real `cmux notify` shell-out
    monkeypatch.setattr(router.time, "sleep", lambda s: None)          # skip deliver()'s flush sleep
    waked = []
    monkeypatch.setattr(router, "maybe_idle_wake", lambda parent_surface, label: waked.append(parent_surface))

    router.handle({"name": "agent.hook.Stop", "occurred_at": "2026-07-01T12:00:00Z",
                   "payload": {"phase": "completed", "session_id": f"codex-{uuid}"}})

    pending = fs.inbox_pending("PARENT", kind="completion")
    assert pending == []                                              # no stale completion re-queued
    assert waked == []                                                # ...and no wake attempted


# --- event-driven stale-surface reconciliation (fleet-doctor capability #1) -----------------------
# The router also subscribes to `--category surface`: a tracked member's surface closing OUTSIDE
# `fleet rm`/`fleet archive` (accidental tab close, workspace teardown — verified live: both emit
# surface.closed per member surface; a WINDOW close does NOT, known gap) must immediately move the
# registry row live -> archive. Pure hygiene: no completion queued, no wake — the surface is gone.
def _surface_closed_ev(surface_id):
    return {"name": "surface.closed", "category": "surface", "occurred_at": "2026-07-03T23:28:23Z",
            "payload": {"kind": "terminal", "origin": "tab_close", "surface_id": surface_id}}


def test_handle_archives_registry_on_surface_closed(fs, monkeypatch):
    """A surface.closed for a LIVE member archives it via the shared _build_archive_entry path
    (via=surface-closed in the ledger) and queues/wakes NOTHING."""
    import json as _json
    from cmux_fleet import cli
    fs.live_put("parent", {"surface": "PARENT", "kind": "conductor", "role": "c",
                           "session": "claude-parent"})
    fs.live_put("worker", {"surface": "CHILD", "kind": "child", "role": "w", "parent": "parent",
                           "tool": "claude", "session": "claude-worker-uuid", "cwd": "/tmp/w"})

    monkeypatch.setattr(router, "LIVE", True)
    monkeypatch.setattr(router, "_reg", {"mtime": 0, "by_label": {}, "by_surface": {}})  # force a reload
    # the surface is already gone when the frame arrives -> binding capture yields {} (no cmux shell-out)
    monkeypatch.setattr(cli, "_resume_binding", lambda surf: {})
    waked = []
    monkeypatch.setattr(router, "maybe_idle_wake", lambda parent_surface, label: waked.append(parent_surface))

    router.handle(_surface_closed_ev("CHILD"))

    assert fs.live_get("worker") is None                              # off the live store...
    arch = fs.archive_get("worker")
    assert arch is not None                                           # ...parked on the archive shelf
    assert arch["last_session"] == "claude-worker-uuid"               # resumable via the registry session
    assert arch["cwd"] == "/tmp/w"
    assert fs.inbox_pending("PARENT", kind="completion") == []        # NO completion queued
    assert waked == []                                                # NO wake attempted
    assert fs.live_get("parent") is not None                          # bystanders untouched
    with open(fs.LOG) as f:                                          # ledger row distinguishable from
        last = _json.loads(f.read().strip().splitlines()[-1])        # an operator-initiated archive
    assert last["event"] == "archived" and last["via"] == "surface-closed"
    assert last["label"] == "worker"


def test_handle_archives_muted_member_on_surface_closed(fs, monkeypatch):
    """Mute gates notification routing, NOT registry truth: a muted member's surface closing is just
    as stale a row as an unmuted one's -> still archived."""
    from cmux_fleet import cli
    fs.live_put("muted-worker", {"surface": "MUTED", "kind": "child", "role": "w", "parent": "parent",
                                 "muted": True, "session": "claude-m"})
    monkeypatch.setattr(router, "LIVE", True)
    monkeypatch.setattr(router, "_reg", {"mtime": 0, "by_label": {}, "by_surface": {}})
    monkeypatch.setattr(cli, "_resume_binding", lambda surf: {})

    router.handle(_surface_closed_ev("MUTED"))

    assert fs.live_get("muted-worker") is None
    assert fs.archive_get("muted-worker") is not None


def test_handle_ignores_surface_closed_for_untracked_surface(fs, monkeypatch):
    """A surface.closed for a surface the fleet doesn't track (any random tab) is a no-op — and other
    surface.* frames (created/selected/focused) never reach the Stop path."""
    fs.live_put("worker", {"surface": "CHILD", "kind": "child", "role": "w", "session": "claude-w"})
    monkeypatch.setattr(router, "LIVE", True)
    monkeypatch.setattr(router, "_reg", {"mtime": 0, "by_label": {}, "by_surface": {}})

    router.handle(_surface_closed_ev("SOME-RANDOM-TAB"))
    router.handle({"name": "surface.created", "category": "surface",
                   "payload": {"surface_id": "CHILD"}})              # non-closed frame: no-op too

    assert fs.live_get("worker") is not None                          # nothing archived
    assert fs.archive_get("worker") is None


def test_handle_observe_mode_does_not_archive_on_surface_closed(fs, monkeypatch):
    """OBSERVE routers hold no singleton lock and must write NOTHING (same contract as the Stop path):
    a surface.closed only logs what a LIVE router would do."""
    fs.live_put("worker", {"surface": "CHILD", "kind": "child", "role": "w", "session": "claude-w"})
    monkeypatch.setattr(router, "LIVE", False)
    monkeypatch.setattr(router, "_reg", {"mtime": 0, "by_label": {}, "by_surface": {}})

    router.handle(_surface_closed_ev("CHILD"))

    assert fs.live_get("worker") is not None                          # untouched
    assert fs.archive_get("worker") is None


def test_member_by_session_matches_bare_uuid_tool_aware(monkeypatch):
    """The registry-truth fallback matches a bus session id to a live member by bare uuid, is TOOL-AWARE
    (never binds a codex id onto a claude agent), and FAILS OPEN to a uuid-only match when the bus id
    carries no tool prefix."""
    uuid = "22222222-2222-2222-2222-222222222222"
    reg = {"by_label": {
        "cl": {"surface": "CL", "kind": "child", "session": f"claude-{uuid}", "tool": "claude"},
        "other": {"surface": "OT", "kind": "child", "session": "claude-deadbeef", "tool": "claude"},
    }, "by_surface": {}, "mtime": 1}
    monkeypatch.setattr(router, "registry", lambda: reg)

    m = router._member_by_session(uuid, "claude")                    # exact match, tool agrees
    assert m.get("label") == "cl" and m.get("surface") == "CL"       # entry returned with label merged
    assert router._member_by_session(uuid, "").get("label") == "cl"  # bare bus id -> fail-open uuid match
    assert router._member_by_session(uuid, "codex") == {}            # tool disagrees -> no cross-tool bind
    assert router._member_by_session("99999999-9999-9999-9999-999999999999", "claude") == {}  # no match
    assert router._member_by_session("", "claude") == {}             # empty id -> nothing
