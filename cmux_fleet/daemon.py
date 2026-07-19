#!/usr/bin/env python3
# cmux_fleet/daemon.py (was fleet_daemon.py) — `fleet daemon start|stop|status|restart`: run the router as a PROPERLY DETACHED
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
#
# OWNERSHIP PROTOCOL (codex-review hardening): `start` is made atomic by a per-STATE MANAGER lock
# (`router.daemon.lock`), SEPARATE from the router's bus lock. It is flock(LOCK_EX|LOCK_NB)'d before
# the running-check and, via the shared open-file-description that fork() dups, held by the supervisor
# grandchild for its whole life — so two near-simultaneous starts can never both proceed and orphan
# the daemon from its pidfile. A pidfile is trusted only after IDENTITY validation (live + group
# leader + a `fleet daemon` ps cmdline), so `stop`'s killpg can never hit an unrelated (pid-reused)
# process group. And `_clear_files()` only unlinks pid/meta that STILL name the pid being cleared,
# so no start/stop ever deletes another live supervisor's files.
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

from .config import STATE, SIDEBAR_PAINT, RECONCILE_RESTORE

# The router is spawned as a module (`python -m cmux_fleet.router`) so it resolves THIS install's
# package no matter where the tool venv lives — a plain file path wouldn't survive the package move.
ROUTER_MODULE = "cmux_fleet.router"
PIDFILE = os.path.join(STATE, "router.pid")
METAFILE = os.path.join(STATE, "router.daemon.json")
LOG = os.path.join(STATE, "router.log")
ROUTER_SEQ = os.path.join(STATE, "router.seq")
ROUTER_LOCK = os.path.join(STATE, "router.live.lock")   # the router's bus-level singleton flock
ROUTER_HEALTH = os.path.join(STATE, "router.health")    # the router's bus-consumption liveness stamp
MANAGER_LOCK = os.path.join(STATE, "router.daemon.lock")  # the daemon MANAGER lock (start/ownership)
DEFAULT_HEARTBEAT = 540                              # 9 min, within the spec's 8-10 min window
HEARTBEAT_REMIND_S = 1800    # presentation cooldown (audit fix #4): a row a direct wake / drain / awareness
                             # pass already showed is NOT re-nudged until it has gone unshown-and-unacked
                             # this long. ~3 default ticks: a genuinely-ignored row still gets a reminder,
                             # but the heartbeat stops re-nudging every tick for rows the agent has seen.
USAGE_POLL_S = 180                                   # providers feature: refresh subscription-usage snapshot ~every 3 min
CODEX_HEALTH_S = 3600                                # providers feature: codex account token-health check ~hourly
PAINT_POLL_S = 4             # sidebar feature: repaint the fleet board ~every 4s when [fleet].sidebar_paint is set
                             # (on-change-only via features.PAINT_STATE, so an idle fleet costs a snapshot + diff)
HEALTH_STALE_S = 60          # a live router silent on the bus longer than this (bus HB ~15s) is WEDGED
HEALTH_CHECK_S = 30          # how often the supervisor re-checks the router is still consuming the bus

_manager_lock_fd = None   # supervisor keeps its manager-lock fd here so the flock survives for its life


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


def _clear_files(pid):
    """Unlink pid/meta, but ONLY if router.pid still names `pid`. A start/stop must never delete files
    that now belong to a DIFFERENT live supervisor (the concurrent-start orphan the manager lock also
    guards). If the pidfile is already gone/other, we leave meta alone too."""
    cur = _read_pid()
    if cur and cur != pid:
        return
    for f in (PIDFILE, METAFILE):
        try:
            os.remove(f)
        except OSError:
            pass


# --- daemon-manager lock (atomic start + ownership; SEPARATE from the router bus lock) -------------
def _acquire_manager_lock():
    """Take the per-STATE daemon MANAGER lock. Returns the held fd on success (the caller/supervisor
    MUST keep it open — closing the last fd on this open-file-description drops the lock), or None if
    another `start` is in flight or a supervisor is already up. flock is tied to the open-file-
    description, which fork() shares, so the supervisor grandchild inherits the hold with no gap."""
    try:
        fd = open(MANAGER_LOCK, "a+")
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    return fd


def _release_manager_lock(fd):
    """Drop THIS process's fd for the manager lock. We CLOSE (never LOCK_UN): the supervisor may share
    the same open-file-description via fork(), and LOCK_UN would release the lock out from under it —
    closing only this fd leaves the supervisor's fd (and thus the lock) intact."""
    try:
        fd.close()
    except Exception:
        pass


def _manager_lock_holder_pid():
    """Pid the supervisor stamped into the manager lock while holding it, or 0 if the lock is free.
    Probe with a non-blocking flock (like the router bus probe): acquiring means nobody holds it."""
    if not os.path.exists(MANAGER_LOCK):
        return 0
    try:
        fd = open(MANAGER_LOCK, "a+")
    except OSError:
        return 0
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return 0
    except OSError:
        fd.seek(0)
        try:
            return int(fd.read().strip())
        except ValueError:
            return 0
    finally:
        fd.close()


def _is_daemon_supervisor(pid):
    """True iff `pid` is THIS build's live `fleet daemon` SUPERVISOR — not merely a live pid. Guards a
    stale pidfile whose pid was reused by an unrelated process, so `stop`'s killpg can never signal a
    foreign process group. Checks: alive, is its own process-group leader (the supervisor calls
    setpgrp, so pgid==pid — and its router child is NOT, since it inherits the supervisor's group),
    and its ps cmdline is the fleet daemon entrypoint (`fleet daemon` / `-m cmux_fleet ... daemon`)."""
    if not _alive(pid):
        return False
    try:
        if os.getpgid(pid) != pid:
            return False
    except OSError:
        return False
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    # Match the packaged entrypoints (`fleet daemon`, `python -m cmux_fleet ... daemon`) AND the legacy
    # checkout forms (`fleet.py`/`fleet_daemon.py`) so identity survives the flat->package move.
    return (("fleet" in out or "cmux_fleet" in out) and "daemon" in out)


def _running_pid():
    """Live, VALIDATED daemon supervisor pid, or 0. A pidfile pointing at a dead pid — or a live pid
    that is NOT actually our supervisor (pid reuse) — is treated as stale and cleaned as a side effect.
    Also distrusts a pidfile that disagrees with the live manager-lock owner (the orphan invariant)."""
    pid = _read_pid()
    if pid and _is_daemon_supervisor(pid):
        holder = _manager_lock_holder_pid()
        if holder and holder != pid:                 # pidfile disagrees with the live lock owner -> stale
            _clear_files(pid)
            return 0
        return pid
    if pid:
        _clear_files(pid)
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
    return ("cmux_fleet.router" in out or "router.py" in out) and "--live" in out


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
          f"— an orphaned bus processor with no supervisor of ours.")
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
    if _manager_lock_fd is not None:                 # stamp our pid into the manager lock we hold, so
        try:                                         # status/stop can confirm the lock owner == pidfile
            _manager_lock_fd.seek(0)
            _manager_lock_fd.truncate()
            _manager_lock_fd.write(str(pid))
            _manager_lock_fd.flush()
        except Exception:
            pass
    with open(PIDFILE, "w") as f:
        f.write(str(pid))
    with open(METAFILE, "w") as f:
        # Record WHICH BUILD owns this daemon (codex P2.5 / runbook): the python that runs it, the
        # cmux_fleet package dir, and the app version — so `fleet daemon status` can prove the code path,
        # not just state dir + pid, during a migration cutover/rollback.
        import cmux_fleet
        json.dump({"pid": pid, "state": STATE, "started": time.time(), "heartbeat": heartbeat_secs,
                   "python": sys.executable, "router_module": ROUTER_MODULE,
                   "package": os.path.dirname(os.path.abspath(cmux_fleet.__file__)),
                   "version": getattr(cmux_fleet, "__version__", "?")}, f)
    print(f"[daemon] up pid={pid} state={STATE} "
          f"heartbeat={'every %ds' % heartbeat_secs if heartbeat_secs else 'off'}", flush=True)

    proc = subprocess.Popen([sys.executable, "-m", ROUTER_MODULE, "--live"],
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
    next_health = time.time() + HEALTH_CHECK_S        # router-wedge check runs regardless of --heartbeat
    next_usage = time.time() + 5                       # first usage poll shortly after boot, then every USAGE_POLL_S
    next_cxhealth = time.time() + 120                   # first codex health check ~2 min after boot, then hourly
    next_paint = time.time() + 3 if SIDEBAR_PAINT else None   # sidebar auto-repaint (opt-in: [fleet].sidebar_paint)
    next_reconcile = time.time() + 8 if RECONCILE_RESTORE else None   # restore reconciliation, once on start (Ship 2)
    while proc.poll() is None and not stopping["v"]:
        time.sleep(1)
        now = time.time()
        if next_tick and now >= next_tick:
            try:
                _heartbeat_tick()
            except Exception as e:                   # a bad tick must never kill the daemon
                print(f"[heartbeat] tick error: {e}", flush=True)
            next_tick = now + heartbeat_secs
        if now >= next_usage:                         # providers feature: refresh the subscription-usage snapshot
            try:
                from . import providers as pv
                snap = pv.poll_all()                  # writes provider-usage.json; no-op/{} if [providers] unset
                if snap:
                    print(f"[usage] polled {len(snap)} provider(s)", flush=True)
            except Exception as e:                    # a bad poll must never kill the daemon
                print(f"[usage] poll error: {e}", flush=True)
            next_usage = now + USAGE_POLL_S
        if now >= next_cxhealth:                       # providers feature: codex account token-health + notify
            try:
                from . import providers as pv
                health = pv.codex_health_scan(pv._codex_notify)   # alerts ONLY on a NEW revocation (edge-triggered)
                dead = [h["acct"] for h in health if h["status"] == "revoked"]
                if dead:
                    print(f"[codex-health] revoked (needs re-login): {', '.join(dead)}", flush=True)
            except Exception as e:                    # a bad health check must never kill the daemon
                print(f"[codex-health] check error: {e}", flush=True)
            next_cxhealth = now + CODEX_HEALTH_S
        if next_paint and now >= next_paint:          # sidebar feature: keep the custom fleet board live
            try:
                painted = _sidebar_paint_tick()       # on-change-only; snapshot + diff, then set-description
                if painted:
                    print(f"[sidebar] repainted {painted} update(s)", flush=True)
            except Exception as e:                    # a bad paint must never kill the daemon (nor the router)
                print(f"[sidebar] paint error: {e}", flush=True)
            next_paint = now + PAINT_POLL_S
        if next_reconcile and now >= next_reconcile:   # Ship 2: reconcile the registry vs cmux's restore
            try:                                        # snapshot ONCE on start (burst-triggered runs come
                from . import reconcile as rc           # from the router). Archive-first closes det. husks.
                rep = rc.reconcile_restore(close=True, log=lambda m: print(m, flush=True),
                                           reason="daemon-start", force=True)
                if rep.get("closed") or rep.get("resume_orphans"):
                    print(f"[reconcile] start sweep: closed {len(rep.get('closed', []))} husk(s), "
                          f"{len(rep.get('resume_orphans', []))} resume-orphan(s) flagged", flush=True)
            except Exception as e:                      # a bad reconcile must never kill the daemon/router
                print(f"[reconcile] start error: {e}", flush=True)
            next_reconcile = None                       # run-once on start; the router handles relaunch bursts
        if now >= next_health:
            try:
                _check_router_health(proc.pid)       # surface an alive-but-wedged router (silent-loss class)
            except Exception as e:                   # a bad health check must never kill the daemon
                print(f"[health] check error: {e}", flush=True)
            next_health = now + HEALTH_CHECK_S

    if proc.poll() is None:                           # ensure the router is down before we exit
        proc.terminate()
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            proc.kill()
    print(f"[daemon] exiting (stopping={stopping['v']}, router rc={proc.poll()})", flush=True)
    _clear_files(pid)                                # only OUR pid/meta — never a successor daemon's


def _sidebar_paint_tick():
    """Repaint the fleet sidebar from the LIVE snapshot so state/ctx/model/last don't go stale between
    manual `fleet paint` runs — the sidebar has no auto-refresh of its own, and native fields (latestMessage)
    update themselves, so a snapshot-derived board drifts without this. On-change-only via features.PAINT_STATE
    (an idle fleet is a snapshot + a diff, no writes). Runs ONLY when [fleet].sidebar_paint is set; the caller
    isolates it so a bad paint can never kill the daemon/router.

    sidebar_blob=False completes the native de-jank: `_paint` still writes the per-agent pills + progress bars
    (with the ⟐kind⟐last suffix the native fleet-proto sidebar reads) + the subscriptions carrier, and its
    else-branch CLEARS the FLEET4 description blob every tick so it stops garbling the built-in sidebar.
    NOTE: [fleet].sidebar_paint gates this WHOLE tick — turning it OFF stops pills/progress too (and never
    clears the blob), so it is NOT the way to retire the blob; sidebar_blob here is. The blob is still
    available on demand for the legacy fleet.swift fallback via FLEET_SIDEBAR_BLOB=1 (see features._paint)."""
    from . import features as ft
    return ft._paint(ft.snapshot(), sidebar_blob=False)


def _heartbeat_tick():
    """Tier-1 nudge ONLY (Berg 2026-06-30): re-nudge LIVE-IDLE conductors that have a pending inbox.
    wake_if_idle is the input-safe gate (skips running surfaces and a non-empty human draft); we also
    skip muted agents. NO dead-session detection / auto-recycle. The whole backstop honors the dial:
    'notify-mode passive' is a fleet-wide wake mute (design 2.1), so the tick no-ops under it."""
    from . import state as fs
    # fleet-doctor sweep (conditions #1/#2/#3): parent alerts on stall / low-ctx / needs-input, deduped.
    # Runs BEFORE the dial gate below: it WRITES inbox rows regardless of the dial (they surface via
    # awareness next turn — 'passive' is a wake mute, not an inbox mute), and its own wake is dial-gated
    # internally. A bad sweep must never kill the tick, so it's isolated in its own try.
    try:
        from . import router
        fired = router.fleet_doctor_sweep()
        if fired:
            print(f"[fleet-doctor] {fired} parent alert(s) fired this tick", flush=True)
    except Exception as e:
        print(f"[fleet-doctor] sweep error: {e}", flush=True)
    if not fs.idlewake_on():                          # 'passive' mutes the backstop too (coherent mute)
        print("[heartbeat] tick: muted (notify-mode=passive)", flush=True)
        return
    nudged = 0
    for label, e in fs.live_all().items():
        if e.get("kind") != "conductor" or e.get("muted"):
            continue
        surf = e.get("surface", "")
        if not surf:
            continue
        pending = fs.inbox_pending(surf)             # both kinds; already event-ack-filtered
        if not pending:
            continue
        # Presentation cooldown (audit fix #4): nudge ONLY for rows no path (direct wake / drain /
        # awareness / a prior heartbeat) has shown within HEARTBEAT_REMIND_S. A fresh row nudges at
        # once; an already-shown-but-unacked row waits out the reminder window. This is what turns the
        # heartbeat from a re-nudge-every-tick backstop into a reminder — the duplicate-notification
        # class the audit flagged (heartbeat re-waking rows a direct wake / drain already surfaced).
        fresh = fs.unpresented(surf, pending, HEARTBEAT_REMIND_S)
        if not fresh:
            continue
        if fs.wake_if_idle(surf, "(heartbeat) you have pending inbox items waiting in your context; handle them"):
            fs.presented_mark(surf, fresh, "heartbeat")   # reminded now -> reset this row's cooldown clock
            nudged += 1
            print(f"[heartbeat] nudged {label} ({surf[:8]}; {len(fresh)} un-shown row(s))", flush=True)
    print(f"[heartbeat] tick: {nudged} nudge(s)", flush=True)


# --- router bus-consumption health (the wedge detector) ------------------------------------------
def _router_health():
    try:
        return json.load(open(ROUTER_HEALTH))
    except Exception:
        return {}


def _router_wedged(router_pid):
    """True iff the router process is ALIVE but has not consumed a bus frame within HEALTH_STALE_S — the
    'alive but not processing the bus' wedge that silently drops completions fleet-wide (the recurrent
    daemon-WEDGE class). Fail-open: an absent / foreign-pid / timestamp-less health record returns False
    (never cry wolf), so only a genuinely stale stamp under the CURRENT router pid trips it."""
    if not router_pid or not _alive(router_pid):
        return False
    h = _router_health()
    if h.get("pid") != router_pid or not h.get("ts"):
        return False
    return (time.time() - h["ts"]) > HEALTH_STALE_S


def _check_router_health(router_pid):
    """Surface a wedged router as UNHEALTHY in the daemon log so it doesn't just silently eat completions.
    Detection only — recovery stays a human `fleet daemon restart` (auto-restart is out of scope)."""
    if not _router_wedged(router_pid):
        return False
    age = round(time.time() - (_router_health().get("ts") or 0))
    print(f"[health] WEDGED: router pid {router_pid} is ALIVE but has not consumed a bus frame in ~{age}s "
          f"(>{HEALTH_STALE_S}s; bus heartbeat is ~15s) — completions may be dropping silently. "
          f"Run `fleet daemon restart`.", flush=True)
    return True


# --- verbs ----------------------------------------------------------------------------------------
def _acquire_for_start():
    """Shared start preamble for both the detached and foreground paths: make STATE, take the manager
    lock BEFORE the running-check (so check->reap->run is serialized against a concurrent start), refuse
    if a supervisor is already up, then reap any orphaned live router on this bus. Returns the held
    manager-lock fd on success, or None if the caller should abort (message already printed)."""
    os.makedirs(STATE, exist_ok=True)
    lock_fd = _acquire_manager_lock()
    if lock_fd is None:
        running = _running_pid()
        if running:
            print(f"[fleet daemon] already running (pid {running}); use `fleet daemon restart` to replace")
        else:
            print("[fleet daemon] another `fleet daemon start` is in progress; try again in a moment")
        return None
    running = _running_pid()
    if running:                                       # a validated supervisor is up (rare: it holds the lock)
        _release_manager_lock(lock_fd)
        print(f"[fleet daemon] already running (pid {running}); use `fleet daemon restart` to replace")
        return None
    _reap_stray_router()                             # clear any orphaned live router on this bus first
    return lock_fd


def _start_foreground(heartbeat_secs):
    """launchd/supervisor mode (codex P2.3): run the supervised router in THIS process — NO fork, setsid,
    or stdio redirect — so launchd (KeepAlive) can supervise it directly and capture its stdout/stderr.
    Same ownership protocol as the detached path (manager lock + validated pidfile/meta); it blocks until
    the router exits or SIGTERM. This is exactly what the design plist runs: `fleet daemon start --foreground`."""
    global _manager_lock_fd
    lock_fd = _acquire_for_start()
    if lock_fd is None:
        return 1
    _manager_lock_fd = lock_fd
    print(f"[fleet daemon] foreground (supervised) start; state={STATE}; log={LOG}", flush=True)
    try:
        _run_daemon(heartbeat_secs)                  # writes pidfile/meta = THIS pid; blocks until stop
    finally:
        _release_manager_lock(lock_fd)
    return 0


def _start(heartbeat_secs):
    global _manager_lock_fd

    lock_fd = _acquire_for_start()
    if lock_fd is None:
        return 1

    pid1 = os.fork()
    if pid1 > 0:                                      # ORIGINAL caller: reap child1, await pidfile, report
        os.waitpid(pid1, 0)
        _release_manager_lock(lock_fd)               # supervisor inherited the hold via the shared OFD
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
    # grandchild = the daemon. It holds the manager lock (inherited via the shared OFD) for its life.
    _manager_lock_fd = lock_fd
    os.setpgrp()                                     # lead our own group so `stop` can killpg the tree
    os.chdir("/")
    os.umask(0o022)
    _redirect_stdio()
    try:
        _run_daemon(heartbeat_secs)
    finally:
        os._exit(0)


def _stop():
    pid = _running_pid()                             # VALIDATED live supervisor, or 0 (+ stale cleanup)
    if not pid:
        # No supervisor of ours — but a `router.py --live` child may still hold the bus (supervisor
        # crash, or the pre-fix concurrent-start race). Reap that orphaned router with the same
        # match-before-kill validation as start; never touch pid/meta that may be another instance's.
        reaped = _reap_stray_router()
        if reaped:
            print(f"[fleet daemon] no live supervisor; reaped stray live router (pid {reaped})")
        else:
            print("[fleet daemon] not running")
        return 0
    # Re-validate identity IMMEDIATELY before signalling (narrow the validate->killpg TOCTOU window),
    # so killpg can never hit a foreign process group if the pid died and got reused between checks.
    if not _is_daemon_supervisor(pid):
        _clear_files(pid)
        print("[fleet daemon] not running (stale pidfile did not identify a live daemon)")
        return 0
    # signal the whole process group (supervisor + router + the router's bus reader)
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_files(pid)
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
    _clear_files(pid)
    print(f"[fleet daemon] stopped (pid {pid})")
    return 0


def _status():
    pid = _running_pid()                             # VALIDATED (identity + lock-owner), or 0
    if not pid:
        stray = _lock_holder_pid()                   # surface an orphaned live router so ops can act
        if stray and _is_live_router(stray):
            print(f"[fleet daemon] not running, BUT a live router (pid {stray}) still holds the bus "
                  f"(state={STATE}); run `fleet daemon start` (it reaps it) or `stop`.")
        else:
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
    print(f"  version  : {meta.get('version', '?')}")            # WHICH build owns this daemon (P2.5/runbook)
    print(f"  python   : {meta.get('python', '?')}")
    print(f"  package  : {meta.get('package', '?')}")
    print(f"  router   : python -m {meta.get('router_module', ROUTER_MODULE)} --live")
    print(f"  state    : {meta.get('state', STATE)}")
    print(f"  uptime   : {up}")
    print(f"  heartbeat: {('every %ds' % hb) if hb else 'off'}")
    print(f"  bus seq  : {seq or '(none yet)'}")
    rpid = _lock_holder_pid()                          # the live router pid (holds the bus singleton lock)
    h = _router_health()
    if h.get("pid") == rpid and h.get("ts"):
        age = int(time.time() - h["ts"])
        hp = ("WEDGED — not consuming the bus; `fleet daemon restart`" if age > HEALTH_STALE_S
              else "consuming bus")
        print(f"  router hp: {hp} (last bus frame ~{age}s ago)")
    else:
        print(f"  router hp: (no fresh stamp yet)")
    print(f"  log      : {LOG}")
    return 0


def _daemon_parser():
    """The `fleet daemon <action> [--foreground] [--heartbeat [SECS]]` parser. Extracted so a test can
    assert the EXACT design-plist command (`fleet daemon start --foreground`) parses as intended — a
    launchd plist is unforgiving, a silent grammar drift means reboot persistence fails."""
    ap = argparse.ArgumentParser(prog="fleet daemon")
    ap.add_argument("action", choices=["start", "stop", "status", "restart"])
    ap.add_argument("--foreground", action="store_true",
                    help="(start only) run the supervised router in the FOREGROUND — no fork/detach — so "
                         "launchd/systemd can supervise it directly. This is what the design plist runs.")
    ap.add_argument("--heartbeat", nargs="?", const=DEFAULT_HEARTBEAT, type=int, default=0,
                    metavar="SECS", help="also nudge live-idle conductors with a pending inbox every "
                                         "SECS seconds (default %d); omit for router-only" % DEFAULT_HEARTBEAT)
    return ap


def cmd_daemon(argv):
    a = _daemon_parser().parse_args(argv)
    if a.action == "start":
        return _start_foreground(a.heartbeat) if a.foreground else _start(a.heartbeat)
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
        return _start(hb)                            # restart always re-detaches (launchd owns foreground)


if __name__ == "__main__":
    sys.exit(cmd_daemon(sys.argv[1:]))
