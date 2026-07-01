# tests/test_daemon.py — `fleet daemon` lifecycle bookkeeping + the Tier-1 heartbeat filter + the
# stray-router reap (bus singleton, daemon side). The actual double-fork daemonize is smoke-tested
# (hard to unit-test); here we cover the pidfile/liveness logic, the heartbeat's skip rules, and the
# reap's match-before-kill safety without spawning a daemon.
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

from cmux_fleet import daemon as fd  # noqa: E402  (not popped by other test files)


def _dead_pid():
    """A pid guaranteed not alive (fork a child, reap it, return its now-free pid)."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


def _no_fork(*_a, **_k):
    raise AssertionError("_start must not fork when a daemon is already running / a start is in flight")


def test_status_reports_not_running(capsys):
    try:
        os.remove(fd.PIDFILE)
    except OSError:
        pass
    rc = fd._status()
    assert rc == 3 and "not running" in capsys.readouterr().out


def test_stale_pidfile_is_cleaned(capsys):
    os.makedirs(os.path.dirname(fd.PIDFILE), exist_ok=True)
    with open(fd.PIDFILE, "w") as f:
        f.write(str(_dead_pid()))
    assert fd._running_pid() == 0                 # dead pid -> not running
    assert not os.path.exists(fd.PIDFILE)          # ...and the stale pidfile is removed


def test_start_refuses_if_running(monkeypatch, capsys):
    os.makedirs(os.path.dirname(fd.PIDFILE), exist_ok=True)
    with open(fd.PIDFILE, "w") as f:
        f.write(str(os.getpid()))                  # our own (alive) pid stands in for a running daemon
    monkeypatch.setattr(fd, "_is_daemon_supervisor", lambda pid: True)  # validated as our supervisor
    monkeypatch.setattr(fd.os, "fork", _no_fork)   # must refuse BEFORE forking
    rc = fd._start(0)
    assert rc == 1 and "already running" in capsys.readouterr().out
    os.remove(fd.PIDFILE)


def test_stop_when_not_running_is_clean(capsys):
    try:
        os.remove(fd.PIDFILE)
    except OSError:
        pass
    assert fd._stop() == 0
    assert "not running" in capsys.readouterr().out


def test_heartbeat_nudges_only_idle_conductors_with_pending(monkeypatch):
    from cmux_fleet import state as fs
    fs.live_put("cond",  {"role": "c", "kind": "conductor", "tool": "claude", "surface": "SC", "status": "live"})
    fs.live_put("busy",  {"role": "c", "kind": "conductor", "tool": "claude", "surface": "SB", "status": "live"})
    fs.live_put("muted", {"role": "c", "kind": "conductor", "tool": "claude", "surface": "SM", "status": "live", "muted": True})
    fs.live_put("child", {"role": "w", "kind": "child",     "tool": "claude", "surface": "SW", "status": "live"})
    for s in ("SC", "SB", "SM", "SW"):
        fs.inbox_put("completion", s, {"gist": "x", "label": "k"})   # everyone has pending

    attempted = []
    def fake_wake(surf, msg):
        attempted.append(surf)
        return surf == "SC"                        # only the idle one actually wakes; the gate declines SB
    monkeypatch.setattr(fs, "wake_if_idle", fake_wake)

    fd._heartbeat_tick()
    assert "SC" in attempted                       # idle conductor with pending -> nudged
    assert "SB" in attempted                       # a busy conductor is still attempted; wake_if_idle declines it
    assert "SM" not in attempted                   # muted -> filtered before the gate
    assert "SW" not in attempted                   # a child -> never nudged (conductors only)


# --- stray-router reap (bus singleton, daemon side) ----------------------------------------------
def test_lock_holder_pid_detects_held_and_free():
    os.makedirs(os.path.dirname(fd.ROUTER_LOCK), exist_ok=True)
    holder = open(fd.ROUTER_LOCK, "a+")
    holder.seek(0)
    holder.truncate()
    holder.write("9999")                           # the "stray router"'s pid it stamped
    holder.flush()
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert fd._lock_holder_pid() == 9999        # lock held -> report the holder pid
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    assert fd._lock_holder_pid() == 0               # released -> nobody to reap


def test_reap_skips_non_router_pid(monkeypatch, capsys):
    # a pid holds the lock but ps says it is NOT a router (pid reuse) -> never signal it.
    monkeypatch.setattr(fd, "_lock_holder_pid", lambda: 4242)
    monkeypatch.setattr(fd, "_is_live_router", lambda pid: False)
    killed = []
    monkeypatch.setattr(fd.os, "kill", lambda *a: killed.append(a))
    assert fd._reap_stray_router() == 0
    assert killed == []                             # safety: unrelated process left alone
    assert "not a live router" in capsys.readouterr().out


def test_reap_kills_stray_live_router(monkeypatch):
    monkeypatch.setattr(fd, "_lock_holder_pid", lambda: 4242)
    monkeypatch.setattr(fd, "_is_live_router", lambda pid: True)
    state = {"alive": True}
    sent = []
    def fake_kill(pid, sig):
        sent.append((pid, sig))
        state["alive"] = False                      # SIGTERM takes it down
    monkeypatch.setattr(fd.os, "kill", fake_kill)
    monkeypatch.setattr(fd, "_alive", lambda pid: state["alive"])
    monkeypatch.setattr(fd.time, "sleep", lambda *_: None)
    assert fd._reap_stray_router() == 4242          # reaped
    assert sent and sent[0][0] == 4242              # signalled the stray pid


def test_reap_noop_when_lock_free(monkeypatch):
    monkeypatch.setattr(fd, "_lock_holder_pid", lambda: 0)
    killed = []
    monkeypatch.setattr(fd.os, "kill", lambda *a: killed.append(a))
    assert fd._reap_stray_router() == 0
    assert killed == []


# --- FIX 1: atomic start (manager lock) + conditional _clear_files -------------------------------
def test_clear_files_only_removes_matching_pid():
    os.makedirs(os.path.dirname(fd.PIDFILE), exist_ok=True)
    with open(fd.PIDFILE, "w") as f:
        f.write("1111")
    with open(fd.METAFILE, "w") as f:
        f.write('{"pid": 1111}')
    fd._clear_files(2222)                            # pidfile names a DIFFERENT supervisor -> leave it
    assert os.path.exists(fd.PIDFILE) and os.path.exists(fd.METAFILE)
    assert fd._read_pid() == 1111
    fd._clear_files(1111)                            # names our pid -> remove
    assert not os.path.exists(fd.PIDFILE) and not os.path.exists(fd.METAFILE)


def test_manager_lock_holder_pid_detects_held_and_free():
    os.makedirs(os.path.dirname(fd.MANAGER_LOCK), exist_ok=True)
    holder = open(fd.MANAGER_LOCK, "a+")
    holder.seek(0)
    holder.truncate()
    holder.write("31337")                            # the "running supervisor" stamps its pid
    holder.flush()
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert fd._manager_lock_holder_pid() == 31337   # held -> report the stamped pid
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    assert fd._manager_lock_holder_pid() == 0        # released -> free


def test_concurrent_start_refuses_and_preserves_owner_files(monkeypatch, capsys):
    """A second `fleet daemon start` while the first holds the manager lock must refuse WITHOUT forking
    and WITHOUT deleting the running daemon's pid/meta (the non-orphaning invariant of FIX 1)."""
    os.makedirs(os.path.dirname(fd.PIDFILE), exist_ok=True)
    owner_pid = 314159
    with open(fd.PIDFILE, "w") as f:
        f.write(str(owner_pid))
    with open(fd.METAFILE, "w") as f:
        f.write('{"pid": 314159}')
    held = open(fd.MANAGER_LOCK, "a+")               # the first supervisor holds the manager lock...
    held.seek(0)
    held.truncate()
    held.write(str(owner_pid))                       # ...and has stamped its pid
    held.flush()
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(fd, "_is_daemon_supervisor", lambda pid: pid == owner_pid)
    monkeypatch.setattr(fd.os, "fork", _no_fork)
    try:
        rc = fd._start(0)
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()
    assert rc == 1
    assert "already running" in capsys.readouterr().out
    assert os.path.exists(fd.PIDFILE) and os.path.exists(fd.METAFILE)   # owner files untouched
    assert fd._read_pid() == owner_pid


# --- FIX 2: identity validation before trusting a pidfile / before killpg ------------------------
def test_is_daemon_supervisor_rejects_non_group_leader():
    """A live process that is NOT its own group leader (a child inheriting our pgrp) is never mistaken
    for the supervisor — so a reused pid can't be signalled by `stop`."""
    child = subprocess.Popen(["sleep", "30"])        # inherits our process group -> not a leader
    try:
        assert os.getpgid(child.pid) != child.pid    # precondition: not a group leader
        assert fd._is_daemon_supervisor(child.pid) is False
    finally:
        child.kill()
        child.wait()


def test_running_pid_rejects_live_non_supervisor_pid(monkeypatch):
    """A pidfile pointing at a LIVE but non-supervisor pid (pid reuse) is treated as stale and cleaned,
    so `_running_pid()` reports not-running rather than trusting an unrelated process."""
    os.makedirs(os.path.dirname(fd.PIDFILE), exist_ok=True)
    with open(fd.PIDFILE, "w") as f:
        f.write(str(os.getpid()))                    # alive, but not our daemon
    monkeypatch.setattr(fd, "_is_daemon_supervisor", lambda pid: False)
    assert fd._running_pid() == 0
    assert not os.path.exists(fd.PIDFILE)            # stale record cleaned


def test_running_pid_distrusts_pidfile_disagreeing_with_lock_owner(monkeypatch):
    """Even if the pidfile pid passes the process checks, a live manager-lock owner that disagrees means
    the pidfile is an orphan -> distrust it."""
    os.makedirs(os.path.dirname(fd.PIDFILE), exist_ok=True)
    with open(fd.PIDFILE, "w") as f:
        f.write("4001")
    monkeypatch.setattr(fd, "_is_daemon_supervisor", lambda pid: True)
    monkeypatch.setattr(fd, "_manager_lock_holder_pid", lambda: 4002)   # a DIFFERENT live supervisor
    assert fd._running_pid() == 0


def test_stop_refuses_to_signal_unrelated_live_pid(monkeypatch, capsys):
    """`stop` must NOT killpg a live pid that fails identity validation (the P1 unrelated-process-group
    kill). With no stray router either, it simply reports not running."""
    os.makedirs(os.path.dirname(fd.PIDFILE), exist_ok=True)
    with open(fd.PIDFILE, "w") as f:
        f.write(str(os.getpid()))                    # alive, unrelated
    monkeypatch.setattr(fd, "_is_daemon_supervisor", lambda pid: False)
    monkeypatch.setattr(fd, "_lock_holder_pid", lambda: 0)   # no stray router on the bus
    killpg_calls = []
    monkeypatch.setattr(fd.os, "killpg", lambda *a: killpg_calls.append(a))
    rc = fd._stop()
    assert rc == 0
    assert killpg_calls == []                         # never signalled the unrelated pid group
    assert "not running" in capsys.readouterr().out
    assert not os.path.exists(fd.PIDFILE)             # its stale record was cleaned


# --- FIX 3: stop reaps an orphaned live router (no validated supervisor) -------------------------
def test_stop_reaps_orphaned_router(monkeypatch, capsys):
    """Supervisor pidfile gone/dead, but a `router.py --live` child still holds the bus -> `stop`
    validates and reaps it instead of reporting not-running and leaving it live."""
    try:
        os.remove(fd.PIDFILE)                         # no supervisor
    except OSError:
        pass
    stray = 55501
    monkeypatch.setattr(fd, "_lock_holder_pid", lambda: stray)   # a live router holds the bus lock
    monkeypatch.setattr(fd, "_is_live_router", lambda pid: True)  # ...and ps confirms it is a router
    state = {"alive": True}
    sent = []
    def fake_kill(pid, sig):
        sent.append((pid, sig))
        state["alive"] = False                        # SIGTERM takes it down
    monkeypatch.setattr(fd.os, "kill", fake_kill)
    monkeypatch.setattr(fd, "_alive", lambda pid: state["alive"])
    monkeypatch.setattr(fd.time, "sleep", lambda *_: None)
    rc = fd._stop()
    assert rc == 0
    assert sent and sent[0] == (stray, signal.SIGTERM)   # reaped the orphaned router
    assert "reaped stray live router" in capsys.readouterr().out


def test_stop_not_running_when_no_supervisor_and_no_router(monkeypatch, capsys):
    """No supervisor AND no stray router -> a clean not-running, no signals sent."""
    try:
        os.remove(fd.PIDFILE)
    except OSError:
        pass
    monkeypatch.setattr(fd, "_lock_holder_pid", lambda: 0)
    killed = []
    monkeypatch.setattr(fd.os, "kill", lambda *a: killed.append(a))
    monkeypatch.setattr(fd.os, "killpg", lambda *a: killed.append(a))
    rc = fd._stop()
    assert rc == 0 and killed == []
    assert "not running" in capsys.readouterr().out


# --- FIX (Phase 4): launchd foreground grammar + status build identity ---------------------------
def test_daemon_parses_the_exact_plist_command():
    """The design plist runs `fleet daemon start --foreground`. A launchd plist is unforgiving, so pin
    that EXACT grammar (codex P2.3): action=start + foreground=True — NOT a bare `fleet daemon --foreground`."""
    ns = fd._daemon_parser().parse_args(["start", "--foreground"])
    assert ns.action == "start" and ns.foreground is True


def test_daemon_bare_start_is_not_foreground():
    ns = fd._daemon_parser().parse_args(["start"])
    assert ns.foreground is False


def test_bare_daemon_foreground_without_action_is_rejected():
    # `fleet daemon --foreground` (no action) must NOT parse -> the grammar stays `daemon <action> ...`.
    import pytest
    with pytest.raises(SystemExit):
        fd._daemon_parser().parse_args(["--foreground"])


def test_start_foreground_runs_in_process_without_forking(monkeypatch):
    """Foreground start must run the supervised router in THIS process (for launchd) — never fork/detach —
    and hold the manager lock while the supervisor runs."""
    try:
        os.remove(fd.PIDFILE)
    except OSError:
        pass
    monkeypatch.setattr(fd.os, "fork", _no_fork)          # foreground path must never fork
    seen = {"ran": False, "lock_held": False}

    def fake_run(hb):
        seen["ran"] = True
        seen["lock_held"] = fd._manager_lock_fd is not None
    monkeypatch.setattr(fd, "_run_daemon", fake_run)
    try:
        rc = fd._start_foreground(0)
        assert rc == 0
        assert seen["ran"] and seen["lock_held"]
    finally:
        fd._manager_lock_fd = None                        # don't leak the module global across tests


def test_status_reports_owning_build(monkeypatch, capsys):
    """`fleet daemon status` must identify WHICH build owns the daemon (version + python + package),
    not just state dir + pid — the runbook needs this to prove the cutover (codex P2.5)."""
    monkeypatch.setattr(fd, "_running_pid", lambda: 4321)
    with open(fd.METAFILE, "w") as f:
        json.dump({"pid": 4321, "state": fd.STATE, "started": time.time() - 65, "heartbeat": 0,
                   "python": "/opt/py/bin/python", "package": "/opt/app/cmux_fleet",
                   "version": "9.9.9", "router_module": "cmux_fleet.router"}, f)
    rc = fd._status()
    out = capsys.readouterr().out
    assert rc == 0
    assert "9.9.9" in out and "/opt/py/bin/python" in out and "/opt/app/cmux_fleet" in out
