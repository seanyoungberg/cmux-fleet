#!/usr/bin/env python3
# cmux_fleet/router.py - the fleet-wide completion router. One daemon serves every conductor. NOT a hook.
#
# Split awareness from activation (input-safe): a child Stop -> append a `completion` to the unified
# inbox + a `cmux notify` banner (never the input box); the parent's awareness hook surfaces it next
# turn. The ONLY input-injecting action is idle-wake (auto mode, human away), via the shared
# fleet_state.wake_if_idle gate. Trigger = the bus (agent.hook.Stop); truth = cmux's hook store;
# org chart = fleet.json (label-keyed live store). Only registered live members are acted on.
#
#   python3 router.py            # OBSERVE: log decisions, write/send nothing
#   python3 router.py --live     # ACTIVE: write inbox + notify; idle-wake iff notify-mode==auto
import fcntl, json, os, pty, subprocess, sys, time
from datetime import datetime

from .config import CMUX  # path resolver
from . import state as fs

LIVE = "--live" in sys.argv
os.makedirs(fs.STATE, exist_ok=True)
CURSOR_FILE = os.path.join(fs.STATE, "router.seq")     # bus replay cursor (distinct from inbox.seq)
LOCKFILE = os.path.join(fs.STATE, "router.live.lock")  # bus-level singleton lock (one --live router)
DEBOUNCE_S = 3.0

_lock_fd = None   # module-global so the flock survives for the whole process (closing the fd drops it)

# registry cache + a materialized surface->entry index (the live store is label-keyed; the router
# needs surface->entry on each Stop, so build the inverse once per reload — critic issue #6).
_reg = {"mtime": 0, "by_label": {}, "by_surface": {}}
_last = {}   # surface -> last-handled event ts (debounce the ~2 Stops/turn)


def cmux(*args, timeout=10):
    try:
        return subprocess.run([CMUX, *args], capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def log(m):
    print(m, flush=True)


def registry():
    try:
        m = os.path.getmtime(fs.LIVE)
    except OSError:
        return _reg
    if m != _reg["mtime"]:
        data = fs.live_all()
        _reg["by_label"] = data
        _reg["by_surface"] = {v.get("surface"): {**v, "label": lbl} for lbl, v in data.items()}
        _reg["mtime"] = m
        log(f"[registry] {len(data)} live member(s): "
            + ", ".join(f"{lbl}({v.get('kind')})" for lbl, v in data.items()))
    return _reg


# --- cmux hook store reads (truth) ---------------------------------------------------------
def store():
    return fs.read_hook_store()                              # union of all per-agent stores (tool-agnostic)


def _rec_by_session(st, uuid):
    for s in (st.get("sessions") or {}).values():
        if s.get("sessionId") == uuid:
            return s
    return {}


def surface_of(st, sid_raw):
    # the bus event's session_id is tool-prefixed (claude-<uuid> / codex-<uuid>); the store keys on
    # the bare uuid. bare_uuid strips ANY tool prefix so codex Stops map to a surface like claude's do.
    return _rec_by_session(st, fs.bare_uuid(sid_raw)).get("surfaceId", "")


def transcript_of(st, surface):
    cur = ((st.get("activeSessionsBySurface") or {}).get(surface) or {}).get("sessionId", "")
    if cur:
        r = _rec_by_session(st, cur)
        if r:
            return r.get("transcriptPath", "")
    for s in (st.get("sessions") or {}).values():
        if s.get("surfaceId") == surface:
            return s.get("transcriptPath", "")
    return ""


def last_assistant_text(path, cap=160):
    """The child's REAL last message from its transcript, tool-agnostic (claude + codex dialects).
    Lives in fleet_state so the router and child-digest share one parser."""
    return fs.last_agent_text(path, cap)


def maybe_idle_wake(parent_surface, label):
    if not (LIVE and fs.idlewake_on()):
        return
    if not fs.inbox_pending(parent_surface, kind="completion"):
        return
    if fs.wake_if_idle(parent_surface, "(auto-wake) handle your pending child completions"):
        log(f"[IDLE-WAKE] {label}: empty prompt -> submitted wake trigger")
    else:
        log(f"[idle-wake] skip {label}: busy or has a draft")


def deliver(parent_surface, parent_label, child_entry, child_surface):
    time.sleep(0.5)                                    # let the final assistant line flush to disk
    gist = last_assistant_text(transcript_of(store(), child_surface))
    label = child_entry.get("label", child_surface[:8])
    if LIVE:
        seq = fs.inbox_put("completion", parent_surface, {
            "child_surface": child_surface, "child_session": child_entry.get("session", ""),
            "label": label, "gist": gist})
        cmux("notify", "--surface", parent_surface, "--title",
             f"child {label} finished", "--body", (gist[:120] or "(done)"))
        log(f"[QUEUE seq={seq}] {label} -> {parent_label} | {gist[:60]}")
        maybe_idle_wake(parent_surface, parent_label)
    else:
        log(f"[WOULD-QUEUE] {label} -> {parent_label} | {gist[:60]}")


def handle(ev):
    if ev.get("name") != "agent.hook.Stop":
        return
    p = ev.get("payload") or {}
    if p.get("phase") != "completed":
        return
    st = store()
    sid_bare = fs.bare_uuid(p.get("session_id") or "")
    surface = _rec_by_session(st, sid_bare).get("surfaceId", "")
    if not surface:
        return
    entry = registry()["by_surface"].get(surface)
    if not entry:
        return                                          # not a registered live member -> ignore
    if not entry.get("session"):                        # lazily-registered at launch (codex binds on
        e = fs.live_get(entry["label"]) or {}           # its 1st turn) -> backfill the bound session now
        if e:
            e["session"] = f"claude-{sid_bare}" if e.get("tool", "claude") == "claude" else sid_bare
            fs.live_put(entry["label"], e)
            log(f"[backfill] {entry['label']}: session {sid_bare[:12]} bound on first turn")

    try:
        ts = datetime.fromisoformat(ev.get("occurred_at", "").replace("Z", "+00:00"))
    except Exception:
        ts = None
    last = _last.get(surface)
    if ts and last and (ts - last).total_seconds() < DEBOUNCE_S:
        return
    if ts:
        _last[surface] = ts

    label, kind = entry.get("label"), entry.get("kind")
    log(f"[event] Stop {label}/{kind} surface={surface[:8]}")
    if kind == "child":                                 # branch on KIND, not role (critic issue #1)
        if entry.get("muted"):                          # muted child: suppress push (no inbox row, no
            log(f"[muted] {label}: completion suppressed (parent reads on demand)")
            return                                       # notify, no idle-wake). Parent reads on demand.
        parent = entry.get("parent")                    # parent LABEL (durable); resolve to its surface
        pe = registry()["by_label"].get(parent)
        parent_surface = pe.get("surface") if pe else parent   # fall back to a raw surface
        if not parent_surface:
            log(f"[skip] child {label}: unresolved parent '{parent}'")
            return
        deliver(parent_surface, parent, entry, surface)
    elif kind == "conductor":
        maybe_idle_wake(surface, label)


def acquire_singleton_lock():
    """Bus-level singleton: only ONE `--live` router may PROCESS a given state dir's bus. A second
    live router (a leftover nohup, a crashed-but-alive process) sitting on the SAME bus double-processes
    every event -> duplicate child completions reach conductors (this happened during cutover: 3 strays
    triple-processed the bus). Acquire an exclusive, non-blocking flock tied to STATE BEFORE consuming;
    a router that can't get it exits instead of processing in parallel. OBSERVE routers do NOT lock —
    they write nothing, so running one alongside the live one for debugging stays safe.

    The lockfile is per-STATE (under fs.STATE), so the invariant is scoped to this build/profile's bus.
    We open with 'a+' (create, no truncate) so a REFUSED second router can't wipe the holder's pid line
    before it fails the flock; only the winner (after acquiring) rewrites the file with its own pid."""
    global _lock_fd
    fd = open(LOCKFILE, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.seek(0)
        holder = fd.read().strip()
        fd.close()
        log(f"[router] REFUSING to start: another --live router already holds the bus lock for "
            f"state={fs.STATE}" + (f" (pid {holder})" if holder else "")
            + f" [{LOCKFILE}]. Only one live router may process this bus; exiting to avoid "
            f"double-processing. Stop the other router (or `fleet daemon restart`) first.")
        sys.exit(3)
    fd.seek(0)
    fd.truncate()
    fd.write(str(os.getpid()))
    fd.flush()
    _lock_fd = fd   # keep the fd (and thus the lock) alive for the process lifetime


def main():
    if LIVE:
        acquire_singleton_lock()                       # hard invariant: one live bus processor
    log(f"[router] mode={'LIVE' if LIVE else 'OBSERVE'} notify-mode={fs.mode()} state={fs.STATE}")
    registry()
    master, slave = pty.openpty()      # PTY or cmux block-buffers a low-volume stream (proven gotcha)
    proc = subprocess.Popen(
        [CMUX, "events", "--category", "agent", "--reconnect",
         "--cursor-file", CURSOR_FILE, "--no-heartbeat", "--no-ack"],
        stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    buf = b""
    try:
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("{"):
                    try:
                        handle(json.loads(line))
                    except Exception:
                        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
