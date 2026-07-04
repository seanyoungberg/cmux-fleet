#!/usr/bin/env python3
# cmux_fleet/router.py - the fleet-wide completion router. One daemon serves every conductor. NOT a hook.
#
# Split awareness from activation (input-safe): a child Stop -> append a `completion` to the unified
# inbox + a `cmux notify` banner (never the input box); the parent's awareness hook surfaces it next
# turn. The ONLY input-injecting action is idle-wake (wake-now default; notify-mode passive mutes),
# via the shared fleet_state.wake_if_idle gate. Trigger = the bus (agent.hook.Stop); truth = cmux's hook store;
# org chart = fleet.json (label-keyed live store). Only registered live members are acted on.
#
# Also subscribes to the `surface` category for real-time registry hygiene (fleet-doctor capability #1):
# a tracked member's surface closing OUTSIDE `fleet rm`/`fleet archive` (accidental tab close, workspace
# teardown) immediately archives its registry row instead of leaving a STALE lie until someone runs
# `fleet ls`, then ALERTS the member's parent conductor through the SAME inbox+idle-wake channel
# completions use (kind='stale', no desktop notify). No auto-relaunch (Tier-2 stays deferred).
#
#   python3 router.py            # OBSERVE: log decisions, write/send nothing
#   python3 router.py --live     # ACTIVE: write inbox + notify; idle-wake unless notify-mode==passive
import fcntl, json, os, pty, subprocess, sys, threading, time
from datetime import datetime

from .config import CMUX  # path resolver
from . import state as fs

LIVE = "--live" in sys.argv
os.makedirs(fs.STATE, exist_ok=True)
CURSOR_FILE = os.path.join(fs.STATE, "router.seq")     # bus replay cursor (distinct from inbox.seq)
LOCKFILE = os.path.join(fs.STATE, "router.live.lock")  # bus-level singleton lock (one --live router)
DEBOUNCE_S = 3.0
HEALTH_FILE = os.path.join(fs.STATE, "router.health")  # liveness proof: stamped on each consumed bus frame
HEALTH_STAMP_THROTTLE_S = 5.0                           # cap health writes to <=1/5s on a busy bus

_lock_fd = None   # module-global so the flock survives for the whole process (closing the fd drops it)
_health = {"ts": 0.0, "frames": 0}   # router.health write-throttle + a rough consumed-frame counter

# registry cache + a materialized surface->entry index (the live store is label-keyed; the router
# needs surface->entry on each Stop, so build the inverse once per reload — critic issue #6).
_reg = {"mtime": 0, "by_label": {}, "by_surface": {}}
_last = {}   # surface -> last-handled event ts (debounce the ~2 Stops/turn)

# Event-driven idle-wake retry (design 2.2b): when an idle-wake is skipped because the parent was
# genuinely mid-turn AT EVENT TIME, re-attempt the wake a few times over the next ~30s so latency is
# seconds — not up to the 2m heartbeat. Re-fires the WAKE ONLY: the completion is already durable in
# the inbox, so nothing is re-delivered and no duplicate rows are created. Bounded + deduped per
# surface; the heartbeat is the backstop for anything past the cap.
RETRY_BACKOFF_S = (5, 10, 15)   # re-check at +5s, +15s, +30s after a skip, then defer to the heartbeat
_retrying = set()               # surfaces with an in-flight retry loop (dedup guard)
_retry_lock = threading.Lock()


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


def _member_by_session(sid_bare, ev_tool=""):
    """Registry-truth fallback for a Stop whose hook-store `sessions{}` record has vanished or desynced
    (root cause #3: a running child whose surface was MOVED across workspaces loses its live session
    record, leaving only a frozen `activeSessionsBySurface` pointer — so `_rec_by_session` finds no
    surface). Recover the member straight from the fleet registry by matching the bus session id to a
    LIVE member's registered `session`. TOOL-AWARE when the bus tool is known (never bind a codex id onto
    a claude agent); FAIL-OPEN to a uuid-only match when the bus id is bare. Returns the entry with its
    `label` merged (same shape as the by_surface index), or {} when nothing matches."""
    if not sid_bare:
        return {}
    for label, entry in registry()["by_label"].items():
        if fs.bare_uuid(entry.get("session") or "") != sid_bare:
            continue
        if ev_tool and entry.get("tool", "claude") != ev_tool:
            continue                                    # same uuid, different tool -> not this member
        return {**entry, "label": label}
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


def _alert_pending(surface):
    """Wake-worthy inbox rows: child completions OR stale-member alerts. Peer messages are excluded on
    purpose — their send path (fleet peer-msg) does its own wake."""
    return fs.inbox_pending(surface, kind="completion") or fs.inbox_pending(surface, kind="stale")


def maybe_idle_wake(parent_surface, label):
    if not (LIVE and fs.idlewake_on()):
        return
    if not _alert_pending(parent_surface):
        return
    if fs.wake_if_idle(parent_surface, "(auto-wake) handle your pending fleet inbox items"):
        log(f"[IDLE-WAKE] {label}: empty prompt -> submitted wake trigger")
    elif fs.surface_busy(parent_surface):               # skip-on-RUNNING -> parent goes idle soon -> retry
        log(f"[idle-wake] skip {label}: mid-turn -> scheduling bounded retry")
        _schedule_idle_wake_retry(parent_surface, label)
    else:                                               # draft / no clean prompt -> heartbeat is the backstop
        log(f"[idle-wake] skip {label}: draft or no clean prompt -> heartbeat backstop (no retry)")


def _schedule_idle_wake_retry(surface, label):
    """Spawn ONE bounded background retry loop per surface (deduped). Non-blocking by design: the bus
    loop must keep processing other Stops while a mid-turn parent finishes its current turn."""
    with _retry_lock:
        if surface in _retrying:
            return                                      # a retry is already chasing this surface
        _retrying.add(surface)
    threading.Thread(target=_idle_wake_retry_loop, args=(surface, label), daemon=True).start()


def _idle_wake_retry_loop(surface, label):
    """Re-attempt the idle-wake over RETRY_BACKOFF_S, stopping as soon as it wakes, the inbox drains,
    or the dial goes passive. Re-fires the WAKE ONLY (content stays durable) — never re-delivers."""
    try:
        for delay in RETRY_BACKOFF_S:
            time.sleep(delay)
            if not fs.idlewake_on():                    # dial muted mid-retry -> stop
                return
            if not _alert_pending(surface):
                log(f"[idle-wake-retry] {label}: inbox drained before wake -> done")
                return                                  # handled meanwhile (woken elsewhere / acked)
            if fs.wake_if_idle(surface, "(auto-wake) handle your pending fleet inbox items"):
                log(f"[idle-wake-retry] {label}: woke after ~{delay}s backoff")
                return
            if not fs.surface_busy(surface):            # turn ended but still not wakeable (draft/no prompt)
                log(f"[idle-wake-retry] {label}: parent idle but not wakeable (draft/no clean prompt) "
                    f"-> heartbeat backstop")
                return
        log(f"[idle-wake-retry] {label}: still not wakeable after {len(RETRY_BACKOFF_S)} tries; "
            f"heartbeat is the backstop")
    finally:
        with _retry_lock:
            _retrying.discard(surface)


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


def _archive_closed_surface(ev):
    """surface.closed -> registry hygiene. The event IS the ground truth that a tracked member's surface
    just died (no lifecycle re-derivation needed): if the close came through `fleet rm`/`fleet archive`,
    the entry is already off the live store by the time the frame arrives and the lookup below misses —
    so anything that DOES resolve here closed outside the fleet CLI (accidental tab close, workspace
    teardown). Archive it through the SAME shared path as `fleet archive`/`fleet rm --kill`
    (_build_archive_entry + archive_put — third caller, kept shared), tagged via=surface-closed in the
    ledger. Applies to muted members too (mute gates notification routing, not registry truth). Then
    ALERT the member's parent conductor through the SAME channel completions ride (inbox kind='stale'
    + maybe_idle_wake) — NOT a completion row (nothing finished; there is no gist/transcript to route)
    and no `cmux notify` desktop banner (registry-integrity signal, quieter than a completion). A
    conductor's own surface closing alerts nobody (no parent); the heartbeat/human notices. A human (or
    a future capability) decides whether to `fleet revive`."""
    surface = (ev.get("payload") or {}).get("surface_id") or ""
    entry = registry()["by_surface"].get(surface) if surface else None
    if not entry:
        return                                          # not a tracked live member's surface
    label = entry["label"]
    if not LIVE:
        log(f"[stale] (observe) would archive {label}: surface {surface[:8]} closed outside fleet CLI")
        return
    from . import cli                                   # lazy: cli is heavy and never imports router (no cycle)
    # binding capture is best-effort: unlike the CLI paths (which read it BEFORE closing), the surface
    # is already gone here, so _resume_binding usually returns {} and last_session falls back to the
    # registry session — "recorded but maybe-unresumable" beats a vanished agent.
    fs.archive_put(label, cli._build_archive_entry(entry, cli._resume_binding(surface)))
    fs.live_del(label)
    fs.log_event("archived", label=label, role=entry.get("role"), session=entry.get("session"),
                 via="surface-closed")
    origin = (ev.get("payload") or {}).get("origin", "?")
    log(f"[stale] archived {label}: surface {surface[:8]} closed out from under the registry "
        f"(origin={origin}); revive with: fleet revive {label}")
    if entry.get("kind") == "conductor":                # branch on KIND, not role (same as the Stop path):
        return                                          # a conductor has no parent to alert
    # Muted members still alert: mute suppresses the child's COMPLETION push specifically; a tracked
    # member vanishing is a registry-integrity signal the parent needs regardless of the chatter dial.
    parent = entry.get("parent")                        # parent LABEL (durable); resolve like deliver()'s path
    pe = registry()["by_label"].get(parent)
    parent_surface = pe.get("surface") if pe else parent   # fall back to a raw surface
    if not parent_surface:
        log(f"[stale] {label}: unresolved parent '{parent}' -> archived without alert")
        return
    seq = fs.inbox_put("stale", parent_surface, {
        "label": label, "child_surface": surface, "via": "surface-closed", "origin": origin})
    log(f"[STALE-ALERT seq={seq}] {label} -> {parent} (surface closed; archived)")
    maybe_idle_wake(parent_surface, parent)


def handle(ev):
    if ev.get("category") == "surface":
        if ev.get("name") == "surface.closed":
            _archive_closed_surface(ev)
        return                                          # other surface.* frames are not ours to act on
    if ev.get("name") != "agent.hook.Stop":
        return
    p = ev.get("payload") or {}
    if p.get("phase") != "completed":
        return
    st = store()
    raw_sid = p.get("session_id") or ""
    sid_bare = fs.bare_uuid(raw_sid)
    surface = _rec_by_session(st, sid_bare).get("surfaceId", "")
    entry = registry()["by_surface"].get(surface) if surface else None
    if not entry:
        # ROOT CAUSE #3 (moved/desynced child): the hook store's `sessions{}` record for this Stop is
        # missing — a running child whose surface was MOVED across workspaces loses its live session
        # record, leaving only a frozen `activeSessionsBySurface` pointer, so `_rec_by_session` resolves
        # NO surface. Do NOT drop the Stop (silent completion loss = the parent stalls, never woken).
        # Recover the member from fleet-REGISTRY truth by matching the bus session id, then fall through
        # to the normal queue+notify+wake path. The gist may be thin/empty because the cmux session
        # record vanished — a thin digest beats silent loss (global acceptance: a moved-then-completed
        # child still wakes its parent). Registry-side match is tool-aware + fail-open.
        entry = _member_by_session(sid_bare, fs.bus_tool(raw_sid))
        if not entry:
            return                                      # truly unknown session -> not ours to act on
        surface = entry.get("surface") or ""
        if not surface:
            return                                      # a registered member with no surface can't be routed
        log(f"[recover] {entry.get('label')}: hook-store session missing -> registry fallback "
            f"(surface {surface[:8]}); routing via registry truth")
    # Keep the registry `session` honest against cmux's live id on EVERY Stop: empty -> backfill (codex
    # binds on its 1st turn); DIVERGED -> reconcile (a fresh respawn re-issues the conversation id, or a
    # bridge id was stored at bind -> the "No conversation found" class on a later archive/revive).
    # TOOL-AWARE (reconcile_session enforces it): only reconcile from the entry's OWN tool id — a
    # codex-store id must never overwrite a claude agent's session (berg-sandbox's stale 019f144d was a
    # codex id on a now-claude agent).
    entry_tool = entry.get("tool", "claude")
    ev_tool = fs.bus_tool(raw_sid)
    if LIVE:                                             # OBSERVE mode holds no singleton lock -> writes NOTHING
        action = fs.reconcile_session(entry["label"], sid_bare, entry_tool, event_tool=ev_tool)
        if action == "backfill":
            log(f"[backfill] {entry['label']}: session {sid_bare[:12]} bound on first turn")
        elif action == "reconcile":
            log(f"[reconcile] {entry['label']}: registry session -> {sid_bare[:12]} (was stale/bridge id)")
        elif action == "skip-tool":
            # a tool mismatch on a RESOLVED surface means the resolution itself was bad (a stale/bad
            # hook-store session record pointed a foreign-tool Stop at this entry) -- not ours to route.
            log(f"[reconcile] skip {entry['label']}: {ev_tool} Stop on a {entry_tool} agent (no cross-tool id write)")
            return
    else:
        # observe: report what a LIVE router would reconcile, mutate nothing (respects the observe contract
        # + the singleton-lock invariant — an unlocked observer racing the daemon on fleet.json is the bug).
        stored = fs.bare_uuid(entry.get("session") or "")
        if sid_bare and stored != sid_bare and not (ev_tool and ev_tool != entry_tool):
            log(f"[reconcile] (observe) would set {entry['label']} session -> {sid_bare[:12]} (was {stored[:12] or 'unbound'})")

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


def _stamp_health(force=False):
    """Prove the router is CONSUMING the bus (not merely alive): stamp router.health on each frame read
    from the events stream. Bus heartbeat frames (~15s) keep it fresh even when no child is completing,
    so a STALE stamp under a live router pid = wedged — the fleet-wide silent-completion-loss class the
    daemon now surfaces as unhealthy. Throttled to <=1 write / HEALTH_STAMP_THROTTLE_S; best-effort."""
    _health["frames"] += 1
    now = time.time()
    if not force and (now - _health["ts"]) < HEALTH_STAMP_THROTTLE_S:
        return
    _health["ts"] = now
    try:
        tmp = f"{HEALTH_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump({"pid": os.getpid(), "ts": now, "frames": _health["frames"]}, f)
        os.replace(tmp, HEALTH_FILE)
    except Exception:
        pass


def main():
    if LIVE:
        acquire_singleton_lock()                       # hard invariant: one live bus processor
    log(f"[router] mode={'LIVE' if LIVE else 'OBSERVE'} notify-mode={fs.mode()} state={fs.STATE}")
    registry()
    if LIVE:
        _stamp_health(force=True)      # baseline stamp so the daemon's wedge check has ground truth at once
    master, slave = pty.openpty()      # PTY or cmux block-buffers a low-volume stream (proven gotcha)
    proc = subprocess.Popen(
        [CMUX, "events", "--category", "agent", "--category", "surface", "--reconnect",
         "--cursor-file", CURSOR_FILE, "--no-ack"],    # heartbeat frames ON = a ~15s bus-liveness tick
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
                if LIVE:
                    _stamp_health()        # each consumed frame (event OR ~15s heartbeat) proves liveness
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
