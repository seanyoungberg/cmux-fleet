# tests/test_router.py — the bus-level SINGLETON GUARD. Only one `router.py --live` may process a
# given state dir's bus; a second one that can't acquire the exclusive flock must exit instead of
# double-processing (the cutover bug: 3 strays triple-processed the bus). flock is per-open-file-
# description, so a SECOND open()+flock in the same process conflicts with the first — that's what lets
# these tests simulate a "first router already holding the lock" without spawning a real router.
import fcntl
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import router  # noqa: E402


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
