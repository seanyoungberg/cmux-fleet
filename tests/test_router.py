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


def test_maybe_idle_wake_schedules_retry_on_skip(monkeypatch):
    monkeypatch.setattr(router, "LIVE", True)
    monkeypatch.setattr(fs, "idlewake_on", lambda: True)
    monkeypatch.setattr(fs, "inbox_pending", lambda surf, kind=None: [{"seq": 1}])
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: False)     # skipped (busy)
    scheduled = []
    monkeypatch.setattr(router, "_schedule_idle_wake_retry", lambda surf, label: scheduled.append(surf))
    router.maybe_idle_wake("S", "cond")
    assert scheduled == ["S"]


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
