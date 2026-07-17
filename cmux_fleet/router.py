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
from . import resolve as rs

LIVE = "--live" in sys.argv
os.makedirs(fs.STATE, exist_ok=True)
CURSOR_FILE = os.path.join(fs.STATE, "router.seq")     # bus replay cursor (distinct from inbox.seq)
LOCKFILE = os.path.join(fs.STATE, "router.live.lock")  # bus-level singleton lock (one --live router)
DEBOUNCE_S = 3.0
HEALTH_FILE = os.path.join(fs.STATE, "router.health")  # liveness proof: stamped on each consumed bus frame
HEALTH_STAMP_THROTTLE_S = 5.0                           # cap health writes to <=1/5s on a busy bus

_lock_fd = None   # module-global so the flock survives for the whole process (closing the fd drops it)
_health = {"ts": 0.0, "frames": 0}   # router.health write-throttle + a rough consumed-frame counter

# A materialized surface->entry index (the live store is label-keyed; the router needs surface->entry on
# each Stop, so build the inverse — critic issue #6). Rebuilt from a FRESH live read on EVERY call: there
# is deliberately NO mtime cache (Ship 5b). The old 1s-granularity mtime gate was the ROOT the expected-
# close tombstone existed to paper over — a just-written `live_del` could be invisible to the router until
# the cached mtime advanced, so a deliberate rm/archive/move still resolved to a "live" member and
# _archive_closed_surface mis-fired a duplicate archive + a spurious `kind='stale'` alert. A fresh read
# per event sees the removal the instant the CLI writes it, which (with registry-before-close write order)
# makes the tombstone redundant. The read is one small JSON parse of ~a dozen rows — negligible per frame.
_reg_seen = {"sig": None}   # last-LOGGED membership signature — a log-dedup only (NOT a read cache): keeps
                            # the bus log quiet across the now-per-event reload, nothing gates a read on it.
_last = {}   # surface -> last-handled event ts (debounce the ~2 Stops/turn)

# Event-driven idle-wake retry (design 2.2b): when an idle-wake is skipped because the parent was
# genuinely mid-turn AT EVENT TIME, re-attempt the wake a few times over the next ~30s so latency is
# seconds — not up to the 2m heartbeat. Re-fires the WAKE ONLY: the completion is already durable in
# the inbox, so nothing is re-delivered and no duplicate rows are created. Bounded + deduped per
# surface; the heartbeat is the backstop for anything past the cap.
RETRY_BACKOFF_S = (5, 10, 15)   # re-check at +5s, +15s, +30s after a skip, then defer to the heartbeat
_retrying = set()               # surfaces with an in-flight retry loop (dedup guard)
_retry_lock = threading.Lock()

# --- fleet-doctor heartbeat sweep dedup + thresholds (conditions #1/#2/#3) ------------------------
# Driven once per heartbeat tick (daemon._heartbeat_tick), the sweep walks every LIVE child and emits a
# DEDUPED parent alert on each bad condition. Dedup is edge-triggered per (reason, label, session): fire
# once when a condition turns bad, re-arm when it clears; the session component resets the alarm on a
# recycle/rebind. The set is mirrored to fs.DOCTOR_DEDUP so a daemon restart does not re-alert a
# steady-state condition that was already seen in a prior process. See docs/backlog.md
# 'NOTIFY-LAYER FAILURE CONDITIONS'.
STALL_S = 600           # a fleet-bound 'running' record frozen longer than this = a dead/stalled turn
                        # (the INVERSE of surface_busy's live-turn check). A live turn re-stamps updatedAt
                        # every ~1-35s, so 10m never clips a legit slow turn EXCEPT one blocked on a single
                        # >10m tool call (deduped to one nudge). Paired with STALL_UPPER below.
STALL_WINDOW = 1800     # ...but ONLY fire while the stall is RECENT (age in (STALL_S, STALL_WINDOW)). The
                        # daemon sweeps every ~2m, so a GENUINE stall is caught FRESH — within one tick of
                        # crossing STALL_S, i.e. ~10-12m stale. A record already stale for HOURS was never
                        # "caught fresh": it's an agent cmux left stuck at 'running' after it was actually
                        # DONE (lifecycle never transitioned to idle) — a done-stuck ghost, not a live
                        # stall. Smoke test 2026-07-04 found two (a finished worker @8.6h, loom-dev @6h)
                        # that a bare `age > STALL_S` would false-flag as "stalled." The upper bound
                        # excludes them (30m >> any fresh-caught stall) without missing a real one.
LOW_CTX_PCT = 30        # context-remaining % at/under which to alert once (matches vitals' near-full flag)
NEVER_BOUND_S = 600     # a LAZY child (codex) registered but with NO session bound this long = launched
                        # but never came up (dead-on-arrival on a bad flag) OR never driven. 10m clears any
                        # cold boot; the sweep only ALERTS when the pane ALSO shows a startup error (so a
                        # healthy batch-launched-not-yet-driven child never false-fires) -- see condition #4.
NEEDS_INPUT_COMPLETION_SUPPRESS_S = 120      # only the immediate post-Stop idle duplicate; not later gates
NEEDS_INPUT_COMPLETION_SKEW_S = 2.0          # row write can lag the hook-store lifecycle stamp slightly
# --- conductor-liveness (condition #5, the both-down backstop the sweep used to skip entirely) ----------
# A conductor has no parent to alert, so it went uncaught: a self-recycle could brick the seat and it sat
# down for hours (berg-sandbox, ~9h, 2026-07-04). The sweep now runs TWO conductor-only predicates —
# stall (reuses #1) and DOWN (a registry-live conductor whose surface holds no live agent: a bare-shell
# husk a failed recycle left). Alerting a conductor is higher-stakes than a child nudge (a false-alarm
# STORM on conductors is worse than the gap), so DOWN fires only under two guards: (1) transition-only —
# the conductor was seen LIVE by THIS process before it went husk, which defuses the reboot/resume-menu
# storm (launchd replays launch cmds on boot, so post-reboot conductors sit unbound at a resume menu; a
# process that never saw them live never fires); and (2) a generous grace window, so a legit recycle that
# rebinds within it never fires. See the conductor-down issue + REPORT-doctor-reliability.
CONDUCTOR_DOWN_GRACE_S = 600   # unbound past this = down, not mid-recycle (2026-07-08: a real recovery that
                               # day took several minutes of retries; 300s would false-fire mid-recovery)
_doctor_fired = set()   # {(reason, label, session)} — parent-alert/seen-state dedup across ticks/restarts


def _doctor_event_key(reason, label, session):
    """The doctor row's durable EVENT identity — one occurrence of one condition on one bound session.
    Deliberately the string form of the _doctor_fired dedup key: the two must move together (fire
    together, re-arm together) or an acked event and a re-armed alarm could disagree."""
    return f"doctor:{reason}:{label}:{session}"


def _rearm(reason, label, session):
    """The sweep observed condition (reason,label,session) CLEAR -> re-arm its alarm AND forget its
    event-level ack (fs.inbox_event_rearm): the next time it goes bad is a NEW occurrence that must
    alert again — an acked key left behind would suppress that genuine re-alert at the producer.
    No-op unless the alarm was actually armed, so healthy members cost nothing per tick."""
    key = (reason, label, session)
    if key not in _doctor_fired:
        return
    _doctor_fired.discard(key)
    try:
        fs.inbox_event_rearm(_doctor_event_key(reason, label, session))
    except Exception as e:
        log(f"[fleet-doctor] {label}: event-ack rearm error {e}")
# {label: last_ts_seen_LIVE} — PROCESS-LOCAL (never persisted): the transition guard for conductor-down.
# Deliberately reset on restart so a fresh daemon never fires DOWN on a conductor it never observed live
# (the reboot/resume-menu storm guard). Persisting it would reintroduce that storm.
_conductor_live_seen = {}


def cmux(*args, timeout=10):
    try:
        return subprocess.run([CMUX, *args], capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def log(m):
    print(m, flush=True)


def registry():
    """Fresh surface->entry index, rebuilt from a live read on EVERY call — no mtime cache (see the
    _reg_seen note above for why: the stale cache was the tombstone's whole reason to exist). Logs only
    when the live membership actually changes, so per-event reloads don't spam the bus log."""
    data = fs.live_all()
    reg = {"by_label": data,
           "by_surface": {v.get("surface"): {**v, "label": lbl} for lbl, v in data.items()}}
    sig = tuple(sorted((lbl, v.get("kind")) for lbl, v in data.items()))
    if sig != _reg_seen["sig"]:
        _reg_seen["sig"] = sig
        log(f"[registry] {len(data)} live member(s): "
            + ", ".join(f"{lbl}({v.get('kind')})" for lbl, v in data.items()))
    return reg


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
    pending = _alert_pending(parent_surface)
    if not pending:
        return
    if fs.wake_if_idle(parent_surface, "(auto-wake) handle your pending fleet inbox items"):
        fs.presented_mark(parent_surface, pending, "wake")   # cooldown: the heartbeat won't re-nudge these
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
            pending = _alert_pending(surface)
            if not pending:
                log(f"[idle-wake-retry] {label}: inbox drained before wake -> done")
                return                                  # handled meanwhile (woken elsewhere / acked)
            if fs.wake_if_idle(surface, "(auto-wake) handle your pending fleet inbox items"):
                fs.presented_mark(surface, pending, "wake")
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


def _recent_completion_for(label, session, now, rec=None):
    """True iff this child/session recently produced a completion row.

    Completion rows remain in inbox.jsonl after ack, so this catches both still-pending and already-acked
    completions. Suppress only the immediate post-completion idle duplicate: if the hook-store record has
    a later lifecycle transition than the completion row, a new turn happened after that Stop and a
    needsInput state is a real gate.
    """
    sid = fs.bare_uuid(session or "")
    try:
        rec_updated = float((rec or {}).get("updatedAt") or 0)
    except (TypeError, ValueError):
        rec_updated = 0
    for r in fs.inbox_read():
        if r.get("kind") != "completion" or r.get("label") != label:
            continue
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if now - ts > NEEDS_INPUT_COMPLETION_SUPPRESS_S:
            continue
        if sid and fs.bare_uuid(r.get("child_session") or "") != sid:
            continue
        if rec_updated and rec_updated > ts + NEEDS_INPUT_COMPLETION_SKEW_S:
            continue
        return True
    return False


def deliver(parent_surface, parent_label, child_entry, child_surface, occurred_at=""):
    time.sleep(0.5)                                    # let the final assistant line flush to disk
    gist = last_assistant_text(transcript_of(store(), child_surface))
    label = child_entry.get("label", child_surface[:8])
    # Event identity: label+session+the Stop's bus timestamp — one key per REAL completion (distinct
    # completions of the same child differ in occurred_at), stable across re-delivery of the same frame.
    sid = fs.bare_uuid(child_entry.get("session", ""))
    ekey = f"completion:{label}:{sid}:{occurred_at}" if occurred_at else None
    if LIVE:
        seq = fs.inbox_put("completion", parent_surface, {
            "child_surface": child_surface, "child_session": child_entry.get("session", ""),
            "label": label, "gist": gist}, event_key=ekey)
        if not seq:
            log(f"[skip] {label}: completion event already acked (replayed frame) -> no row, no push")
            return
        cmux("notify", "--surface", parent_surface, "--title",
             f"child {label} finished", "--body", (gist[:120] or "(done)"))
        log(f"[QUEUE seq={seq}] {label} -> {parent_label} | {gist[:60]}")
        maybe_idle_wake(parent_surface, parent_label)
    else:
        log(f"[WOULD-QUEUE] {label} -> {parent_label} | {gist[:60]}")


def _surface_ws_now(surface):
    """The workspace UUID that CURRENTLY contains `surface` per cmux's live TREE, or '' when the surface
    is GONE (truly closed). The move-vs-close arbiter for _archive_closed_surface: a MOVED surface
    resolves to its NEW workspace (it still exists somewhere); a genuinely CLOSED one resolves to ''.
    Ground truth is the TREE, never the hook store -- the per-surface hook record DESYNCS on a move (root
    cause #3), so only the tree can tell a move from a close. Uses the router's own cmux() wrapper (which
    swallows subprocess errors -> ''), so an unreadable tree fails CLOSED to '': a genuine external close
    still archives, and the fix only ADDS a skip when the surface can be POSITIVELY located as alive."""
    from . import cli                                    # lazy: cli is heavy and never imports router
    return cli.surface_ws_from_tree(cmux("tree", "--all", "--id-format", "both"), surface)


def _surface_error_line(surface):
    """First startup-error line on `surface`'s live pane (a bad flag / missing binary / early crash), or
    ''. The never-bound sweep's discriminator: a DEAD lazy launch shows the error; a merely-undriven one
    shows its TUI (no error) -> no false alarm. Shares cli's PURE scanner so the marker list lives once."""
    from . import cli                                    # lazy: shared marker list + pure scanner
    return cli.launch_error_line(cmux("capture-pane", "--surface", surface) or "")


def conductor_alert_text(reason, label, surface, live):
    """The peer-wake line + desktop (title, body) for a conductor alert, WORDED BY LIVENESS — never by
    the reason string. Returns (wake, title, body).

    PID AUTHORITY (the house rule): a LIVE pid is never DOWN, and never gets a destructive remedy.
    Two of the reasons routed here — stall and detached — fire ONLY on a surface that _sweep_conductor
    just proved PRESENT (a live agent is on it). Their positive signal is "the bound 'running' record
    stopped advancing", which is EXACTLY what a human typing into the conductor produces: no Stop hook,
    no stream, a frozen updatedAt. The old text hardcoded the DOWN script for every reason, so on
    2026-07-11 a live berg-sandbox — Berg mid-sentence in it, pid up, 88% context — was reported to its
    peers as "appears DOWN (stall) ... `fleet revive berg-sandbox`". `revive` archives the agent and
    relands it on a FRESH surface, so obeying the alert would have DESTROYED the very session it
    misread: the advertised remedy for the false positive is to kill what it falsely accused. The alert
    also contradicted itself — the inbox header says "still LIVE — a health alert, not an archive".

    So: live -> INSPECT (read the surface, peek the inbox), and say outright that a human may just be
    typing. Dead -> the DOWN text, `revive` included. Deriving this from the pid HERE, in the one place
    that writes the words, is what makes it structural: no reason routed through this function, present
    or future, can hand a live agent a revive/archive/--force."""
    if live:
        return (f"(fleet-doctor) conductor {label} looks STUCK ({reason}) but is STILL LIVE (pid up) — "
                f"a human may simply be TYPING in it. INSPECT, never recycle blind: read its surface "
                f"(`cmux capture-pane --surface {surface}`) and peek its inbox before you touch it.",
                f"conductor {label} needs a look ({reason})",
                f"still LIVE (pid up) — may just be a human typing. Inspect first: "
                f"cmux capture-pane --surface {surface}")
    return (f"(fleet-doctor) conductor {label} appears DOWN ({reason}); "
            f"check it and `fleet revive {label}` if it is",
            f"conductor {label} DOWN ({reason})",
            f"fleet-doctor found no live agent on {label}; revive with: fleet revive {label}")


def _alert_conductor_peers(reason, down_label, down_entry, surface, payload, now=None, members=None, woke=None):
    """Alert every OTHER live conductor plus Berg's desktop that conductor `down_label` needs attention
    (conductor-liveness condition #5). A conductor has no parent, so the alert fans out to peers and to a
    surfaceless macOS banner (`cmux notify` with no --surface) that reaches Berg regardless of focus or
    which peers are awake. Rides the same inbox+wake rail as completions/stale. Shared by the sweep's
    DOWN/stall/detached predicates, _archive_closed_surface's conductor branch, and recycle-failed.
    Best-effort per channel; the desktop banner fires even with zero live peers (the both-down case this
    backstop exists for).

    The WORDS come from conductor_alert_text, gated on a fresh pid check (rs.present) rather than on the
    reason — see there for why a live agent must never be told it is DOWN."""
    now = time.time() if now is None else now
    members = fs.live_all() if members is None else members
    woke = set() if woke is None else woke
    # PID AUTHORITY, and it must be the PROCESS TABLE. This used to read rs.present() — cmux's STORE — under
    # a comment claiming pid authority it did not have. A DARK agent (alive, but filed by cmux under another
    # surfaceId) is absent from the store, so it would be announced to its peers as "appears DOWN ... `fleet
    # revive`" — and revive archives and relands it, destroying a live agent on the strength of a signal that
    # simply could not see it. rs.alive() asks `ps`, which can. A genuinely dead agent still has no pid and
    # still gets the DOWN text; nothing that is actually running can ever be called down again.
    # ...and UNKNOWN is not GONE. `alive()` collapses "I could not look" into False, and the DOWN script's
    # remedy is `fleet revive`, which archives and relands the agent — destructive. So ask for the tri-state
    # and treat anything short of a PROVEN absence as live: only an agent we can actually see is not there
    # may be told it is not there.
    verdict, _pids, _ = rs.liveness(surface, tool=(down_entry or {}).get("tool"))
    live = verdict != rs.GONE
    wake, title, body = conductor_alert_text(
        reason, down_label, surface, live)
    peers = [(lbl, e) for lbl, e in members.items()
             if e.get("kind") == "conductor" and lbl != down_label and e.get("surface")]
    ekey = _doctor_event_key(reason, down_label, down_entry.get("session") or "")
    seq = None
    for pl, pe in peers:                                # write the inbox row regardless of the --live
        ps = pe.get("surface")                          # global (like _emit; the sweep runs from the daemon
        s = fs.inbox_put("doctor", ps, {"reason": reason, "label": down_label,     # and _archive_closed_surface
                                        "child_surface": surface, "live": live, **payload},
                         event_key=ekey)                                          # already LIVE-gates upstream)
        if not s:
            continue                                    # THIS peer already acked this condition -> no row/wake
        seq = s
        if fs.idlewake_on() and ps not in woke:
            woke.add(ps)
            if fs.wake_if_idle(ps, wake):
                fs.presented_mark(ps, [{"event_key": ekey}], "wake")
    cmux("notify", "--title", title, "--body", body)
    log(f"[CONDUCTOR-{'STUCK' if live else 'DOWN'} seq={seq}] {down_label} {reason} "
        f"(live={live}) -> {len(peers)} peer(s) + desktop")
    return seq


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
    if fs.expected_close_recent(surface):
        # a DELIBERATE CLI close (rm/archive/--with-group) that beat its own live_del to this process —
        # NOT an accidental external close. The CLI already archived + reconciled the registry; skip the
        # duplicate archive AND the spurious `kind='stale'` "revive?" alert to the parent (fleet-doctor #5).
        log(f"[stale] skip {label}: expected CLI close (surface {surface[:8]})")
        return
    # MOVE-vs-CLOSE (root cause #3, the 2026-07-07 incident): cmux emits surface.closed when a surface
    # LEAVES a workspace -- including a cross-workspace MOVE, where the surface still EXISTS in its new
    # workspace. Archiving here would EVICT a live child (three moved children auto-archived that day).
    # POSITIVELY confirm against the live tree: if the surface resolves to a workspace it MOVED, not
    # closed -> never archive, never alert (nothing is wrong). Ship 5b RETIRED the workspace-reconcile that
    # used to run here: `workspace` is a DERIVED field now (resolve.py reads the live tree), so there is
    # nothing stored to bring up to date -- ls/graph/routing already read the agent's true workspace from
    # cmux, and the completion lane self-recovers via _member_by_session. (Post-migrate the reconcile also
    # went inert: `clean.get("workspace")` is always None, so `moved` was always True -> a redundant live_put
    # + a misleading "reconciled" log on every move event.) Fails CLOSED (ws_now == '' on an unreadable
    # tree) so a genuine external close still archives.
    ws_now = _surface_ws_now(surface)
    if ws_now:
        log(f"[move] {label}: surface {surface[:8]} still present in workspace {ws_now[:8]} "
            f"(MOVED, not closed); no archive")
        return
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
        # A conductor's surface closing is the SAME undetected-down gap as the sweep's husk predicate:
        # no parent means the old silent return let a dead conductor sit unnoticed (Berg's 2026-07-08
        # ruling: a closed conductor surface must ALERT, not vanish quietly). Already archived above; the
        # alert fans out to peers + Berg's desktop so a human revives it. registry() is post-live_del, so
        # the down conductor is not its own peer.
        _alert_conductor_peers("conductor-closed", label, entry, surface,
                               {"origin": origin, "via": "surface-closed"}, members=registry()["by_label"])
        return
    # Muted members still alert: mute suppresses the child's COMPLETION push specifically; a tracked
    # member vanishing is a registry-integrity signal the parent needs regardless of the chatter dial.
    parent = entry.get("parent")                        # parent LABEL (durable); resolve like deliver()'s path
    pe = registry()["by_label"].get(parent)
    parent_surface = pe.get("surface") if pe else parent   # fall back to a raw surface
    if not parent_surface:
        log(f"[stale] {label}: unresolved parent '{parent}' -> archived without alert")
        return
    seq = fs.inbox_put("stale", parent_surface, {
        "label": label, "child_surface": surface, "via": "surface-closed", "origin": origin},
        event_key=f"stale:{label}:{surface}")          # a surface closes once; stable per close event
    log(f"[STALE-ALERT seq={seq}] {label} -> {parent} (surface closed; archived)")
    maybe_idle_wake(parent_surface, parent)


def fleet_doctor_sweep(now=None):
    """One heartbeat sweep (conditions #1/#2/#3): walk every LIVE child and emit a DEDUPED parent alert
    on each bad condition — #1 stall (bound 'running' record frozen past STALL_S), #2 low-ctx
    (context-remaining <= LOW_CTX_PCT), #3 needs-input (bound record at needsInput). Returns the count
    of NEW alerts fired this tick.

    Reuses the SAME inbox+wake rail completions/stale ride: one inbox kind='doctor' with a `reason`
    field ('stall'|'low-ctx'|'needs-input') the awareness hook renders and `fleet inbox-ack --doctor`
    clears. Rows are written regardless of the dial (they surface next turn via awareness — 'passive' is
    a wake mute, not an inbox mute); the WAKE is dial-gated (fs.idlewake_on), the same coherent mute as
    the heartbeat nudge. CONDUCTORS take a separate path (_sweep_conductor, condition #5): the child
    content gates (needs-input/low-ctx) stay off, but a conductor is now checked for stall and DOWN and
    the alert fans out to PEER conductors + Berg's desktop (a conductor has no parent). SKIPS MUTED
    children (mute = 'this one is my manual concern, don't nudge me'; the child health signals are
    exactly the chatter class mute governs — unlike a VANISHED surface, a hard integrity fact that alerts
    even muted; conductor liveness likewise ignores mute). Self-contained + fail-safe per member; no
    dependence on the router's --live/observe globals, so it runs from the daemon process."""
    from . import features                              # lazy: the heavier view module, off the hot path
    now = time.time() if now is None else now
    members = fs.live_all()
    st = fs.read_hook_store()
    ws_map = rs.surface_ws_map()               # one tree read per sweep, shared by the attachment checks
    live_keys, woke = set(), set()
    fired = 0
    persisted = fs.doctor_dedup_load()
    if persisted:
        _doctor_fired.update(persisted)

    def _emit(reason, label, entry, surface, payload):
        nonlocal fired
        session = entry.get("session") or ""
        key = (reason, label, session)
        if key in _doctor_fired:
            return                                     # already alerted this occurrence -> dedup (no storm)
        parent = entry.get("parent")                   # parent LABEL (durable); resolve like deliver()'s path
        pe = members.get(parent)
        parent_surface = pe.get("surface") if pe else parent   # fall back to a raw surface
        if not parent_surface:
            log(f"[fleet-doctor] {label}: unresolved parent '{parent}' -> {reason} not alerted")
            return
        seq = fs.inbox_put("doctor", parent_surface, {
            "reason": reason, "label": label, "child_surface": surface, **payload},
            event_key=_doctor_event_key(reason, label, session))
        if not seq:
            # The parent already ACKED this very occurrence (event-key ledger) but the dedup set lost
            # it (nuked file, pruned-then-revived member). Handled is handled: arm the alarm so we stop
            # re-attempting, count nothing, wake nobody. A REAL re-occurrence re-arms via _rearm first.
            _doctor_fired.add(key)
            log(f"[fleet-doctor] {label} {reason}: suppressed — event already acked by {parent}")
            return
        _doctor_fired.add(key)
        fired += 1
        log(f"[DOCTOR-ALERT seq={seq}] {label} {reason} -> {parent}")
        if fs.idlewake_on() and parent_surface not in woke:   # dial-gated wake, once per parent per tick
            woke.add(parent_surface)
            if fs.wake_if_idle(parent_surface, f"(fleet-doctor) child {label} needs attention ({reason}); "
                                               f"handle your pending fleet inbox items"):
                fs.presented_mark(parent_surface, [{"event_key": _doctor_event_key(reason, label, session)}], "wake")

    def _emit_conductor(reason, label, entry, surface, payload):
        nonlocal fired
        key = (reason, label, entry.get("session") or "")
        if key in _doctor_fired:
            return                                     # already alerted this occurrence -> dedup (no storm)
        _alert_conductor_peers(reason, label, entry, surface, payload, now=now, members=members, woke=woke)
        _doctor_fired.add(key)
        fired += 1

    def _sweep_conductor(label, entry, surface, session):
        # Conductor-liveness (#5). Content gates (needs-input / low-ctx) stay OFF for conductors — only
        # STALL (a stuck turn) and DOWN (a husk seat) apply. Keeping the dedup key live across the
        # end-of-sweep prune (like the child path) needs this member in live_keys.
        live_keys.add((label, session))
        if rs.present(surface):
            _conductor_live_seen[label] = now                  # transition-guard clock: last seen alive
            _rearm("conductor-down", label, session)           # recovered -> re-arm DOWN (+ its event ack)
            rec = rs.bound_record(surface, st=st, bound=fs.bare_uuid(session))
            life = (rec.get("agentLifecycle") or "") if rec else ""
            ua = (rec.get("updatedAt") or 0) if rec else 0
            # A. stall — reuses #1's RECENT-window guard (a running record frozen for HOURS is a
            # done-stuck ghost, not a live stall; the window excludes it). ALSO gate on turn-end: a
            # 'running' record whose turn already CLOSED is cmux's lifecycle LAGGING, not a stall — the
            # idle-timer lag, or (cmux.swift:24200) the `hasPendingBackgroundWork ? .running : .idle`
            # branch that HOLDS 'running' while background shells drain and never resets (the 2026-07-15
            # berg-sandbox done-idle "human typing" false alarm — root-caused in doctor-rootcause.md).
            # features.turn_ended fails closed to False, so it only ever CLEARS a lagged 'running', never
            # invents a stall; hadPendingBackgroundWorkAtStop is cmux's own positive flag for that case.
            turn_done = (features.turn_ended((rec or {}).get("transcriptPath", ""))
                         or bool((rec or {}).get("hadPendingBackgroundWorkAtStop")))
            if life == "running" and ua and STALL_S < (now - ua) < STALL_WINDOW and not turn_done:
                _emit_conductor("stall", label, entry, surface, {"stalled_s": int(now - ua)})
            else:
                _rearm("stall", label, session)
            # C. detached (invariant I4) — present but hook-dead: the record is frozen while the agent
            # demonstrably works (transcript advancing), or an env/pointer mismatch proves the channel
            # dead. A conductor has no parent, so this fans out to peers + the desktop like DOWN. The
            # live case this exists for: berg-sandbox, record frozen 3.5h while actively writing turns.
            att = rs.attachment(surface, st=st, ws_map=ws_map, now=now)
            if att["attached"] is False:
                _emit_conductor("detached", label, entry, surface, {
                    "evidence": att["reasons"],
                    "record_frozen_min": int((att["record_age_s"] or 0) / 60),
                    "transcript_age_min": (int((att["transcript_age_s"] or 0) / 60)
                                           if att["transcript_age_s"] is not None else None),
                    "remedy": f"fleet recycle {label} (resume) reattaches in ~8s"})
            else:
                _rearm("detached", label, session)
            return
        # B. DOWN — a registry-live conductor whose surface holds NO live agent (a bare-shell husk a
        # failed recycle left; also the dead-pid brick, which surface_has_live_agent already reads as
        # not-live). Two guards keep this from storming: transition-only (seen live by THIS process) +
        # grace (a legit recycle rebinds within it). expected_close was already filtered by the caller.
        seen = _conductor_live_seen.get(label)
        if seen is None:
            return                                     # never seen live here (boot/resume-menu) -> not a transition
        if now - seen < CONDUCTOR_DOWN_GRACE_S:
            return                                     # inside the recycle grace window -> not yet down
        _emit_conductor("conductor-down", label, entry, surface, {"down_s": int(now - seen)})

    for label, entry in members.items():
        try:
            surface, session = entry.get("surface") or "", entry.get("session") or ""
            if not surface:
                continue
            if fs.expected_close_recent(surface, now=now):
                log(f"[fleet-doctor] skip {label}: expected CLI close (surface {surface[:8]})")
                continue
            if entry.get("kind") == "conductor":       # conductor-liveness path (no parent; stall/down only)
                _sweep_conductor(label, entry, surface, session)
                continue
            if entry.get("muted"):
                continue                               # muted child: manual concern (conductors still checked)

            # #4 never-bound — a LAZY child registered with NO session bound past NEVER_BOUND_S. claude
            # binds at boot (a failed bind sys.exits BEFORE register), so a no-session live row is always a
            # lazy tool that either died-on-arrival (bad flag) or was launched-and-never-driven. Handled
            # HERE, before the session-based conditions (they all assume a bound record). We ONLY alert on
            # a CONFIRMED pane startup-error -> a healthy child queued for later driving (no error) never
            # false-fires; a silent exit with no error is LOG-only (the launch-time verify P0-4a is the
            # primary catch). live_keys keeps this member's dedup key across the end-of-sweep prune.
            if not session:
                live_keys.add((label, ""))
                if entry.get("tool") == "claude":
                    continue                               # defensive: claude never registers unbound
                age = now - (entry.get("launchedAt") or now)
                if age < NEVER_BOUND_S:
                    continue                               # still inside the boot/drive grace window
                errline = _surface_error_line(surface)
                if errline:
                    _emit("never-bound", label, entry, surface,
                          {"pane_error": errline, "pending_s": int(age)})
                else:
                    _rearm("never-bound", label, "")   # re-arm; undriven or silent exit
                    log(f"[fleet-doctor] {label}: pending {int(age)}s (no session, no pane error) "
                        f"— undriven or silent exit; not alerting")
                continue

            if session and not rs.present(surface):
                log(f"[fleet-doctor] skip {label}: surface {surface[:8]} has no live agent")
                continue
            live_keys.add((label, session))
            rec = rs.bound_record(surface, st=st, bound=fs.bare_uuid(session))
            life = (rec.get("agentLifecycle") or "") if rec else ""
            ua = (rec.get("updatedAt") or 0) if rec else 0

            # #0 dead-pid guard — a bound record on a DEAD process is a SessionEnd-less ghost (the
            # 2026-07-06 dead-agent class: an abrupt kill or a SessionEnd store-write race freezes the
            # lifecycle non-terminal with a dead/None pid), NOT a live member. Its frozen string would
            # fire FALSE health alerts — worst of all a 'needsInput' ghost, which has NO freshness gate
            # and would nudge the parent forever. The pid is authoritative: if it's dead, the member is
            # DOWN, so suppress all three signals, re-arm their dedup, and log once. `fleet unstick`
            # (or a `fleet recycle`, now pid-aware) clears the ghost record itself; the daemon only
            # READS cmux's store, so it deliberately does not rewrite it here.
            if rec and life not in ("", "-", "ended") and not fs.pid_alive(rec.get("pid")):
                for r in ("stall", "needs-input", "low-ctx", "detached"):
                    _rearm(r, label, session)
                log(f"[fleet-doctor] {label}: bound record {life!r} on a DEAD pid {rec.get('pid')} "
                    f"(surface {surface[:8]}) — down; suppressing health alerts. `fleet unstick {label}` to clear")
                continue

            # #6 detached (invariant I4) — present but hook-dead: record frozen while the transcript
            # advances (the agent works, cmux is deaf), or an env/pointer mismatch proves the channel
            # dead. Requires the conjunction, never record-frozen alone: an idle agent freezes BOTH
            # clocks together and must never read detached (live-validated across 11 agents,
            # 2026-07-10, zero false positives). Completions and Feed gates from a detached child are
            # silently lost, so the parent hears about it here. Remedy: a reseat (recycle resume).
            att = rs.attachment(surface, st=st, ws_map=ws_map, now=now)
            if att["attached"] is False:
                _emit("detached", label, entry, surface, {
                    "evidence": att["reasons"],
                    "record_frozen_min": int((att["record_age_s"] or 0) / 60),
                    "transcript_age_min": (int((att["transcript_age_s"] or 0) / 60)
                                           if att["transcript_age_s"] is not None else None),
                    "remedy": f"fleet recycle {label} (resume) reattaches in ~8s"})
            else:
                _rearm("detached", label, session)

            # #1 stall — bound 'running' record frozen in the RECENT window (STALL_S, STALL_WINDOW). A
            # missing/zero updatedAt never fires; a record stale for HOURS (a done-stuck ghost, not a live
            # stall) is above the window and skipped — see STALL_WINDOW. A real stall is caught fresh.
            # Gate on turn-end too: a 'running' record whose turn already CLOSED is cmux's lifecycle
            # LAGGING (idle-timer lag, or the cmux.swift:24200 hasPendingBackgroundWork branch that holds
            # 'running' while background work drains and never resets) — NOT a stall. features.turn_ended
            # fails closed (only ever clears a lagged 'running'); hadPendingBackgroundWorkAtStop is cmux's
            # positive flag for the background-drain case. See doctor-rootcause.md.
            turn_done = (features.turn_ended((rec or {}).get("transcriptPath", ""))
                         or bool((rec or {}).get("hadPendingBackgroundWorkAtStop")))
            if life == "running" and ua and STALL_S < (now - ua) < STALL_WINDOW and not turn_done:
                _emit("stall", label, entry, surface, {"stalled_s": int(now - ua)})
            else:
                _rearm("stall", label, session)                        # re-arm when it clears/ages out

            # #3 needs-input — bound record at needsInput. NO freshness gate: a genuine wait freezes
            # updatedAt for DAYS (loom-dev sat 46h; the live store holds a 46.3h needsInput record), so
            # 'fresh updatedAt' would MISS exactly what #3 exists to catch. Orphan needsInput records are
            # excluded by resolving the BOUND record of a LIVE member — not by age (see resolve_bound_record).
            if life == "needsInput":
                if _recent_completion_for(label, session, now, rec=rec):
                    _doctor_fired.add(("needs-input", label, session))
                    log(f"[fleet-doctor] {label}: needs-input suppressed after recent completion")
                    continue
                # cmux stamps needsInput for a DONE-IDLE turn too (Claude's idle Notification fires ~60s
                # after a turn ends), indistinguishable from a real gate by the lifecycle string alone —
                # the 100%-FP class (fleet-doctor #iii, timing-test 2026-07-07). The transcript IS
                # distinguishable: only a trailing UNANSWERED AskUserQuestion/ExitPlanMode is a genuine
                # wait. Suppress everything else (done-idle, the feedback survey #iv, unreadable) +
                # dedup like the recent-completion path; leaving needsInput re-arms (below).
                if not features.pending_interactive_gate(rec.get("transcriptPath", "") if rec else ""):
                    _doctor_fired.add(("needs-input", label, session))
                    log(f"[fleet-doctor] {label}: needs-input suppressed — no pending interactive gate (done-idle)")
                    continue
                _emit("needs-input", label, entry, surface, {})
            else:
                _rearm("needs-input", label, session)                   # re-arm on leaving needsInput

            # #2 low-ctx — context-remaining <= LOW_CTX_PCT (vitals' exact math). used=None
            # (codex/unparseable transcript) -> skip: no false alarm on an unknowable window.
            used, tmodel = features._context_used(rec.get("transcriptPath", "") if rec else "")
            # window from the LAUNCHED model (carries the [1m] flavor), same as vitals' snapshot() — so the
            # sweep's low-ctx % matches what `fleet vitals` shows (no divergence between alarm and table).
            lmodel, _eff = features._launched_prefs(rec, entry.get("tool", "")) if rec else ("", "")
            window = features._context_window(lmodel or tmodel or entry.get("tool", ""))
            pct = max(0, round(100 * (1 - used / window))) if (used is not None and window) else None
            if pct is not None and pct <= LOW_CTX_PCT:
                _emit("low-ctx", label, entry, surface, {"ctx_pct_remaining": pct})
            else:
                _rearm("low-ctx", label, session)                      # re-arm above threshold / unknown
        except Exception as e:
            log(f"[fleet-doctor] {label}: sweep error {e}")

    # prune dedup keys for members no longer live (removed) or whose session changed (recycle/rebind) —
    # bounds the set AND lets a recycled-while-still-bad member re-alert fresh under its new session.
    # _rearm (not bare discard) so the event-level ack goes too: once we can no longer observe the
    # condition, all its state is forgotten — a future observation starts fresh.
    for k in [k for k in _doctor_fired if (k[1], k[2]) not in live_keys]:
        _rearm(*k)
    # prune the conductor seen-live clock for conductors no longer in the registry, so a removed label
    # can't carry a stale 'seen live' timestamp into a later reuse and false-fire DOWN on a fresh husk.
    for lbl in [l for l in _conductor_live_seen
                if members.get(l, {}).get("kind") != "conductor"]:
        _conductor_live_seen.pop(lbl, None)
    if _doctor_fired != persisted:
        try:
            fs.doctor_dedup_save(_doctor_fired, now=now)
        except Exception as e:
            log(f"[fleet-doctor] dedup persistence error: {e}")
    return fired


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
        deliver(parent_surface, parent, entry, surface, ev.get("occurred_at") or "")
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
