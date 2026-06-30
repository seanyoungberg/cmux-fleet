#!/usr/bin/env python3
# fleet_state.py — the ONE shared state module for the cmux fleet. Folds child-completions and
# peer-messages into a single inbox. CODE lives in the plugin; STATE under $CMUX_STATE_DIR
# (default $XDG_STATE_HOME/cmux-fleet).
#
# Stores (9 files, down from 12; one inbox mechanism instead of two):
#   inbox.jsonl        unified append-only message stream. One line: {seq, ts, kind, to, **payload}.
#                      kind = "completion" (child finished) | "peer" (deliberate conductor->peer send).
#   inbox.seq          atomic monotonic counter behind `seq` (router AND peer-msg both append).
#   inbox-cursors.json {surface: {acked, blocked}} — ONE high-water ack + drain-guard per surface,
#                      across both kinds (seq is global). Replaces 4 old files.
#   fleet.json         the LIVE fleet, label-keyed: label -> {role,kind,tool,cwd,parent,place,
#                      status:"live",surface,session}. Only running agents.
#   archive.json       PARKED agents, label-keyed: {role,kind,tool,cwd,last_session,parent,archived_at}.
#   log.jsonl          append-only EVENT ledger: {ts,event,label,role,...}. Source-of-truth timeline.
#   notify-mode        the dial: passive | autodrain | auto.
#   router.seq         bus cursor (cmux events --cursor-file). router.log — the router trace.
#
# Identity: kind(child|conductor) / role(type, ->AGENT_ROLE, owns the dir) / label(unique instance,
# ->AGENT_LABEL, the registry key, durable across recycles) / surfaceId(current seat, a mutable field).
import fcntl, glob, json, os, re, subprocess, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import STATE, CMUX, HOOKSTORE  # path resolver

INBOX = os.path.join(STATE, "inbox.jsonl")
INBOX_SEQ = os.path.join(STATE, "inbox.seq")
CURSORS = os.path.join(STATE, "inbox-cursors.json")     # DURABLE per-(surface,kind) ack high-water
BLOCKS = os.path.join(STATE, "inbox-blocks.json")       # EPHEMERAL drain loop-guard (nukeable on restart)
LIVE = os.path.join(STATE, "fleet.json")
ARCHIVE = os.path.join(STATE, "archive.json")
LOG = os.path.join(STATE, "log.jsonl")
MODEFILE = os.path.join(STATE, "notify-mode")

HERE = os.path.dirname(os.path.abspath(__file__))
DIGEST = os.path.join(HERE, "child-digest.py")
ACK = os.path.join(HERE, "inbox-ack.py")
PEERMSG = os.path.join(HERE, "peer-msg.py")


# --- primitives ----------------------------------------------------------------------------
def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default if default is not None else {}


def _append(path, rec):
    os.makedirs(STATE, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _read_jsonl(path):
    out = []
    try:
        for line in open(path):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except OSError:
        pass
    return out


# --- the dial ------------------------------------------------------------------------------
def mode():
    try:
        return open(MODEFILE).read().strip() or "passive"
    except OSError:
        return "passive"


def autodrain_on():
    return mode() in ("autodrain", "auto")


def idlewake_on():
    return mode() == "auto"


# --- inbox (unified completions + peer) ----------------------------------------------------
def inbox_next_seq():
    """Cross-process-atomic counter. The router (completions) and peer-msg (peers) both allocate seqs
    concurrently, so a read-incr-write WITHOUT a lock duplicates seqs (critic issue #2). flock the
    seq file around the whole read+write. Append order may interleave; pending() re-sorts by seq."""
    os.makedirs(STATE, exist_ok=True)
    fd = os.open(INBOX_SEQ, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            n = int(os.pread(fd, 64, 0).decode().strip() or "0")
        except Exception:
            n = 0
        n += 1
        os.ftruncate(fd, 0)
        os.pwrite(fd, str(n).encode(), 0)
        return n
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def inbox_put(kind, to_surface, payload):
    """Append one message addressed to a surface. kind: 'completion' | 'peer'. BOTH carry a single `to`
    field (normalized, critic issue #7) so one reader selects 'for me'. Returns the seq."""
    seq = inbox_next_seq()
    rec = {"seq": seq, "ts": time.time(), "kind": kind, "to": to_surface}
    rec.update(payload)
    _append(INBOX, rec)
    return seq


def inbox_read():
    return _read_jsonl(INBOX)


def inbox_pending(surface, kind=None):
    """Unacked messages to this surface, oldest first. Ack high-water is PER-KIND (critic issue #3):
    a conductor can handle completions and acked them without swallowing an unread peer. Pass `kind`
    to select one stream for display grouping; omit it for the full inbox view."""
    cur = _cursors().get(surface, {})
    rows = []
    for r in inbox_read():
        if r.get("to") != surface:
            continue
        k = r.get("kind")
        if kind is not None and k != kind:
            continue
        if int(r.get("seq", 0)) > int(cur.get(k, 0)):
            rows.append(r)
    return sorted(rows, key=lambda r: int(r.get("seq", 0)))


def max_seq(rows):
    return max((int(r.get("seq", 0)) for r in rows), default=0)


# --- DURABLE per-(surface,kind) ack high-water (cursors) ------------------------------------
def _cursors():
    return _read_json(CURSORS, {})


def inbox_ack(surface, kind, seq):
    """Per-kind high-water ack. acked is durable correctness state; kept SEPARATE from the throwaway
    drain block-marks (critic simplify #3) so a block reset never disturbs ack history."""
    m = _cursors()
    e = m.setdefault(surface, {})
    e[kind] = max(int(e.get(kind, 0)), int(seq))
    _atomic_write(CURSORS, json.dumps(m, indent=2))
    return e[kind]


# --- EPHEMERAL drain loop-guard (blocks; per-kind, separate nukeable file) ------------------
# Child drain is mode-gated (autodrain/auto); peer drain fires ALWAYS (critic issue #4). The two
# advance on different triggers, so the block-mark is per-kind, and lives apart from the durable ack.
def block_get(surface, kind):
    return int(_read_json(BLOCKS, {}).get(surface, {}).get(kind, 0))


def block_set(surface, kind, seq):
    m = _read_json(BLOCKS, {})
    m.setdefault(surface, {})[kind] = int(seq)
    _atomic_write(BLOCKS, json.dumps(m, indent=2))
    return int(seq)


# --- identity: the LIVE fleet (label-keyed) ------------------------------------------------
def live_all():
    return _read_json(LIVE, {})


def live_get(label):
    return live_all().get(label)


def live_put(label, entry):
    m = live_all()
    m[label] = entry
    _atomic_write(LIVE, json.dumps(m, indent=2))


def live_del(label):
    m = live_all()
    e = m.pop(label, None)
    _atomic_write(LIVE, json.dumps(m, indent=2))
    return e


def label_for_surface(surface):
    for label, v in live_all().items():
        if v.get("surface") == surface:
            return label
    return ""


def surface_for_label(label):
    return (live_get(label) or {}).get("surface", "")


def entry_for_surface(surface):
    for v in live_all().values():
        if v.get("surface") == surface:
            return v
    return None


# --- identity: the ARCHIVE shelf (parked, revivable) ---------------------------------------
def archive_all():
    return _read_json(ARCHIVE, {})


def archive_get(label):
    return archive_all().get(label)


def archive_put(label, entry):
    m = archive_all()
    m[label] = entry
    _atomic_write(ARCHIVE, json.dumps(m, indent=2))


def archive_del(label):
    m = archive_all()
    e = m.pop(label, None)
    _atomic_write(ARCHIVE, json.dumps(m, indent=2))
    return e


# --- the event ledger ----------------------------------------------------------------------
def log_event(event, **fields):
    rec = {"ts": time.time(), "event": event}
    rec.update(fields)
    _append(LOG, rec)


# --- the ONE input-safe wake (shared by router idle-wake AND peer-msg) ----------------------
def _cmux(*args):
    return subprocess.run([CMUX, *args], capture_output=True, text=True,
                          env=dict(os.environ, CMUX_QUIET="1")).stdout or ""


def read_hook_store():
    """Union cmux's PER-AGENT hook stores (~/.cmuxterm/<agent>-hook-sessions.json). cmux writes ONE
    store per agent kind (claude, codex, grok, ...), each with the SAME schema (sessions{},
    activeSessionsBySurface{}, fields surfaceId/sessionId/agentLifecycle/pid/cwd/launchCommand). A
    surface hosts exactly one agent, so the union is collision-free in the normal case; on the rare
    cross-tool reuse of a surface, the newer-updatedAt session entry wins. THE single read that makes
    the whole fleet (poll/lifecycle/ls/router) tool-agnostic — replaces every bare claude-store read."""
    sessions, active = {}, {}
    for path in sorted(glob.glob(os.path.join(HOOKSTORE, "*-hook-sessions.json"))):
        try:
            d = json.load(open(path))
        except Exception:
            continue
        for sid, s in (d.get("sessions") or {}).items():
            old = sessions.get(sid)
            if not old or (s.get("updatedAt") or 0) >= (old.get("updatedAt") or 0):
                sessions[sid] = s
        active.update(d.get("activeSessionsBySurface") or {})
    return {"sessions": sessions, "activeSessionsBySurface": active}


def bare_uuid(sid):
    """The canonical 36-char session uuid, stripping any '<tool>-' prefix cmux's BUS adds
    (claude-<uuid>, codex-<uuid>, ...). The per-agent STORE keys on the bare uuid; the BUS event's
    session_id is tool-prefixed. Returns sid unchanged if it carries no uuid."""
    m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", sid or "")
    return m.group(0) if m else (sid or "")


def last_agent_text(path, cap=160):
    """The agent's REAL last message from its transcript, tool-agnostic. cmux's `lastBody` is clobbered
    by the post-Stop Notification hook, so we read the JSONL. Two transcript dialects:
      claude -> {"type":"assistant","message":{"content": str | [{"type":"text","text":...}]}}
      codex  -> {"type":"event_msg","payload":{"type":"agent_message","message":...}} and a final
                {"type":"event_msg","payload":{"type":"task_complete","last_agent_message":...}}
    Whichever assistant line is LAST wins (for codex that is task_complete = the canonical final answer)."""
    if not path:
        return ""
    try:
        text = ""
        for line in open(path):
            try:
                e = json.loads(line)
            except Exception:
                continue
            typ = e.get("type")
            t = ""
            if typ == "assistant":                                       # claude
                c = (e.get("message") or {}).get("content")
                t = c if isinstance(c, str) else (
                    "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                    if isinstance(c, list) else "")
            elif typ == "event_msg":                                     # codex
                pl = e.get("payload") or {}
                if pl.get("type") == "agent_message":
                    t = pl.get("message", "")
                elif pl.get("type") == "task_complete":
                    t = pl.get("last_agent_message", "")
            if t and t.strip():
                text = t.strip()
        return text.replace("\n", " ")[:cap]
    except Exception:
        return ""


def lifecycle(surface):
    """agentLifecycle for a surface from cmux's hook stores (idle|running|needsInput|unknown|''),
    tool-agnostic via read_hook_store. Picks the freshest entry for the surface (an agent can leave
    more than one session record on a surface; the newest updatedAt is the live one)."""
    best, best_ts = "", -1.0
    try:
        for s in (read_hook_store().get("sessions") or {}).values():
            if s.get("surfaceId") == surface:
                ts = s.get("updatedAt") or 0
                if ts >= best_ts:
                    best, best_ts = s.get("agentLifecycle", ""), ts
    except Exception:
        pass
    return best


def wake_if_idle(surface, msg):
    """Inject+submit a wake ONLY if the surface is at the prompt with an empty draft; else leave it
    (it sees the inbox next turn). Returns True if it woke. The ONE copy of the wake gate, shared by
    the router's idle-wake and peer-msg (simplify #4). Two guards: (1) busy = lifecycle 'running' ->
    never interrupt; (2) a human draft (bottom-most prompt line with text after the marker) -> never
    clobber."""
    if lifecycle(surface) == "running":
        return False
    screen = _cmux("read-screen", "--surface", surface, "--lines", "40")
    prompts = [ln for ln in screen.splitlines() if "❯" in ln]   # ❯
    if prompts and prompts[-1].split("❯", 1)[1].strip():
        return False
    _cmux("send", "--surface", surface, msg)
    _cmux("send-key", "--surface", surface, "enter")
    return True
