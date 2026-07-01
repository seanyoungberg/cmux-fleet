#!/usr/bin/env python3
# fleet_daemon.py — `fleet daemon start|stop|status|restart`: run the router as a PROPERLY DETACHED
# daemon that survives the starting shell exiting, an agent Bash-tool process-group cleanup, AND a
# conductor self-recycle (the failure that blocked the cutover: a `nohup &` router died on
# process-group cleanup).
#
# HOW: double `os.fork()` with `os.setsid()` between them (macOS has no `setsid` binary), so the daemon
# is reparented to init in its own session with NO controlling terminal — a pane/process-group teardown
# can't reach it. The daemon then leads its OWN process group (`os.setpgrp()`) and runs router.py as a
# child in that group, so `stop` can signal the whole group (supervisor + router + the router's bus
# reader) with one `killpg`. Pidfile + meta + log live under $CMUX_STATE_DIR, so the daemon is
# per-build/profile (see config.py / docs/profiles.md). The router path is resolved relative to THIS
# module so `start` always runs this build's router.
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import STATE

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTER = os.path.join(HERE, "router.py")            # this build's router
PIDFILE = os.path.join(STATE, "router.pid")
METAFILE = os.path.join(STATE, "router.daemon.json")
LOG = os.path.join(STATE, "router.log")
ROUTER_SEQ = os.path.join(STATE, "router.seq")
ROUTER_LOCK = os.path.join(STATE, "router.live.lock")   # the router's bus-level singleton flock
DEFAULT_HEARTBEAT = 540                              # 9 min, within the spec's 8-10 min window


# --- pidfile / liveness ---------------------------------------------------------------------------
def _alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:                          # exists, owned by someone else (not expected here)
        return True


def _read_pid():
    try:
        return int(open(PIDFILE).read().strip())
    except (OSError, ValueError):
        return 0


def _clear_files():
    for f in (PIDFILE, METAFILE):
        try:
            os.remove(f)
        except OSError:
            pass


def _running_pid():
    """Live daemon pid, or 0. Cleans a STALE pidfile (pid dead) as a side effect."""
    pid = _read_pid()
    if pid and _alive(pid):
        return pid
    if pid:
        _clear_files()
    return 0


# --- stray-router reaping (bus singleton, daemon side) --------------------------------------------
def _lock_holder_pid():
    """If a `--live` router currently holds THIS state dir's bus lock, return its pid; else 0. Probe by
    trying to flock the per-STATE lockfile non-blocking: success => nobody holds it (release at once and
    return 0); failure => held, so return the holder pid the router wrote into the file."""
    if not os.path.exists(ROUTER_LOCK):
        return 0
    try:
        fd = open(ROUTER_LOCK, "a+")
    except OSError:
        return 0
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)               # free -> nobody to reap
        return 0
    except OSError:
        fd.seek(0)
        try:
            return int(fd.read().strip())
        except ValueError:
            return 0
    finally:
        fd.close()


def _is_live_router(pid):
    """True iff `pid` is actually a `router.py --live` process — a ps cmdline check so we never signal an
    unrelated process that inherited a reused pid. Matches the router script + `--live` (the lockfile is
    already per-STATE, so state-dir scoping comes for free)."""
    if not _alive(pid):
        return False
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    return "router.py" in out and "--live" in out


def _reap_stray_router():
    """Before spawning our router, reap a STRAY `router.py --live` that holds this bus's singleton lock
    but is NOT our daemon's child (a leftover nohup / crashed-but-alive process). Called only once we've
    confirmed no daemon of ours is running, so the lock holder — if any — is genuinely orphaned. Matched
    via the per-STATE lockfile pid + a ps cmdline check, so an unrelated (pid-reused) process is left
    alone with a warning. Returns the reaped pid, or 0."""
    pid = _lock_holder_pid()
    if not pid:
        return 0
    if not _is_live_router(pid):
        print(f"[fleet daemon] warn: bus lock held by pid {pid}, which is not a live router "
              f"(pid reuse?); leaving it alone. Remove {ROUTER_LOCK} by hand if this is wrong.")
        return 0
    print(f"[fleet daemon] reaping stray live router (pid {pid}) holding the bus for state={STATE} "
          f"before starting — prevents double-processing.")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return pid
    for _ in range(30):                              # up to ~3s for a graceful exit
        if not _alive(pid):
            break
        time.sleep(0.1)
    if _alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.2)
    return pid


# --- the daemon body (runs in the fully-detached grandchild) --------------------------------------
def _redirect_stdio():
    log = open(LOG, "a", buffering=1)
    nul = open(os.devnull, "r")
    os.dup2(nul.fileno(), 0)
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)


def _run_daemon(heartbeat_secs):
    """Supervise router.py --live (+ optional heartbeat). pidfile = THIS (supervisor) pid; the router is
    a child in our process group. On SIGTERM we tear the router down and clear the pidfile."""
    pid = os.getpid()
    with open(PIDFILE, "w") as f:
        f.write(str(pid))
    with open(METAFILE, "w") as f:
        json.dump({"pid": pid, "state": STATE, "started": time.time(), "heartbeat": heartbeat_secs}, f)
    print(f"[daemon] up pid={pid} state={STATE} "
          f"heartbeat={'every %ds' % heartbeat_secs if heartbeat_secs else 'off'}", flush=True)

    proc = subprocess.Popen([sys.executable, ROUTER, "--live"],
                            stdout=sys.stdout, stderr=sys.stderr, stdin=subprocess.DEVNULL)

    stopping = {"v": False}

    def _term(signum, _frame):
        stopping["v"] = True
        try:
            proc.terminate()
        except Exception:
            pass
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    next_tick = time.time() + heartbeat_secs if heartbeat_secs else None
    while proc.poll() is None and not stopping["v"]:
        time.sleep(1)
        if next_tick and time.time() >= next_tick:
            try:
                _heartbeat_tick()
            except Exception as e:                   # a bad tick must never kill the daemon
                print(f"[heartbeat] tick error: {e}", flush=True)
            next_tick = time.time() + heartbeat_secs

    if proc.poll() is None:                           # ensure the router is down before we exit
        proc.terminate()
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            proc.kill()
    print(f"[daemon] exiting (stopping={stopping['v']}, router rc={proc.poll()})", flush=True)
    _clear_files()


def _heartbeat_tick():
    """Tier-1 nudge ONLY (Berg 2026-06-30): re-nudge LIVE-IDLE conductors that have a pending inbox.
    wake_if_idle is the input-safe gate (skips running surfaces and a non-empty human draft); we also
    skip muted agents. NO dead-session detection / auto-recycle."""
    import fleet_state as fs
    nudged = 0
    for label, e in fs.live_all().items():
        if e.get("kind") != "conductor" or e.get("muted"):
            continue
        surf = e.get("surface", "")
        if not surf or not fs.inbox_pending(surf):   # both kinds; nothing pending -> skip
            continue
        if fs.wake_if_idle(surf, "(heartbeat) you have pending inbox items waiting in your context; handle them"):
            nudged += 1
            print(f"[heartbeat] nudged {label} ({surf[:8]})", flush=True)
    print(f"[heartbeat] tick: {nudged} nudge(s)", flush=True)


# --- verbs ----------------------------------------------------------------------------------------
def _start(heartbeat_secs):
    os.makedirs(STATE, exist_ok=True)
    running = _running_pid()
    if running:
        print(f"[fleet daemon] already running (pid {running}); use `fleet daemon restart` to replace")
        return 1

    _reap_stray_router()                             # clear any orphaned live router on this bus first

    pid1 = os.fork()
    if pid1 > 0:                                      # ORIGINAL caller: reap child1, await pidfile, report
        os.waitpid(pid1, 0)
        for _ in range(50):
            time.sleep(0.1)
            p = _read_pid()
            if p and _alive(p):
                print(f"[fleet daemon] started (pid {p}); state={STATE}; log={LOG}"
                      + (f"; heartbeat every {heartbeat_secs}s" if heartbeat_secs else ""))
                return 0
        print(f"[fleet daemon] ERROR: daemon did not come up within 5s; check {LOG}")
        return 1

    # child1: detach into a new session, then fork again so the daemon can never reacquire a tty
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    # grandchild = the daemon
    os.setpgrp()                                     # lead our own group so `stop` can killpg the tree
    os.chdir("/")
    os.umask(0o022)
    _redirect_stdio()
    try:
        _run_daemon(heartbeat_secs)
    finally:
        os._exit(0)


def _stop():
    pid = _read_pid()
    if not pid or not _alive(pid):
        if pid:
            _clear_files()
        print("[fleet daemon] not running" + (" (cleaned stale pidfile)" if pid else ""))
        return 0
    # signal the whole process group (supervisor + router + the router's bus reader)
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_files()
        print("[fleet daemon] not running (cleaned stale pidfile)")
        return 0
    for _ in range(50):                              # up to ~5s for a graceful exit
        time.sleep(0.1)
        if not _alive(pid):
            break
    if _alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.3)
    _clear_files()
    print(f"[fleet daemon] stopped (pid {pid})")
    return 0


def _status():
    pid = _read_pid()
    if not pid or not _alive(pid):
        if pid:
            _clear_files()
        print(f"[fleet daemon] not running (state={STATE})")
        return 3
    meta = {}
    try:
        meta = json.load(open(METAFILE))
    except Exception:
        pass
    up = "unknown"
    if meta.get("started"):
        s = int(time.time() - meta["started"])
        up = f"{s // 3600}h{(s % 3600) // 60}m{s % 60}s"
    seq = ""
    try:
        seq = open(ROUTER_SEQ).read().strip()
    except OSError:
        pass
    hb = meta.get("heartbeat", 0)
    print(f"[fleet daemon] running (pid {pid})")
    print(f"  state    : {meta.get('state', STATE)}")
    print(f"  uptime   : {up}")
    print(f"  heartbeat: {('every %ds' % hb) if hb else 'off'}")
    print(f"  bus seq  : {seq or '(none yet)'}")
    print(f"  log      : {LOG}")
    return 0


def cmd_daemon(argv):
    ap = argparse.ArgumentParser(prog="fleet daemon")
    ap.add_argument("action", choices=["start", "stop", "status", "restart"])
    ap.add_argument("--heartbeat", nargs="?", const=DEFAULT_HEARTBEAT, type=int, default=0,
                    metavar="SECS", help="also nudge live-idle conductors with a pending inbox every "
                                         "SECS seconds (default %d); omit for router-only" % DEFAULT_HEARTBEAT)
    a = ap.parse_args(argv)
    if a.action == "start":
        return _start(a.heartbeat)
    if a.action == "stop":
        return _stop()
    if a.action == "status":
        return _status()
    if a.action == "restart":
        hb = a.heartbeat
        if not hb:                                   # preserve the running daemon's heartbeat setting
            try:
                hb = int(json.load(open(METAFILE)).get("heartbeat", 0))
            except Exception:
                hb = 0
        _stop()
        return _start(hb)


if __name__ == "__main__":
    sys.exit(cmd_daemon(sys.argv[1:]))
