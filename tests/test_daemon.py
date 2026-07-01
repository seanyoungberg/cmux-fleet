# tests/test_daemon.py — `fleet daemon` lifecycle bookkeeping + the Tier-1 heartbeat filter + the
# stray-router reap (bus singleton, daemon side). The actual double-fork daemonize is smoke-tested
# (hard to unit-test); here we cover the pidfile/liveness logic, the heartbeat's skip rules, and the
# reap's match-before-kill safety without spawning a daemon.
import fcntl
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import fleet_daemon as fd  # noqa: E402  (not popped by other test files)


def _dead_pid():
    """A pid guaranteed not alive (fork a child, reap it, return its now-free pid)."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


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


def test_start_refuses_if_running(capsys):
    os.makedirs(os.path.dirname(fd.PIDFILE), exist_ok=True)
    with open(fd.PIDFILE, "w") as f:
        f.write(str(os.getpid()))                  # our own (alive) pid stands in for a running daemon
    rc = fd._start(0)                              # must refuse BEFORE forking
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
    import fleet_state as fs
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
