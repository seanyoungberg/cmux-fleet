#!/usr/bin/env python3
# cmux_fleet/state.py (was fleet_state.py) — the ONE shared state module for the cmux fleet. Folds child-completions and
# peer-messages into a single inbox. CODE lives in the `fleet` APP (the plugin ships only thin hook
# wiring that shells into it); STATE under $CMUX_STATE_DIR (default $XDG_STATE_HOME/cmux-fleet).
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
#   notify-mode        the wake dial, now a MUTE switch: passive (mute) | auto (default, wake-now).
#   router.seq         bus cursor (cmux events --cursor-file). router.log — the router trace.
#
# Identity: kind(child|conductor) / role(type, ->AGENT_ROLE, owns the dir) / label(unique instance,
# ->AGENT_LABEL, the registry key, durable across recycles) / surfaceId(current seat, a mutable field).
import fcntl, glob, json, os, re, subprocess, sys, tempfile, time

from .config import STATE, CMUX, HOOKSTORE  # path resolver

INBOX = os.path.join(STATE, "inbox.jsonl")
INBOX_SEQ = os.path.join(STATE, "inbox.seq")
CURSORS = os.path.join(STATE, "inbox-cursors.json")     # DURABLE per-(surface,kind) ack high-water
BLOCKS = os.path.join(STATE, "inbox-blocks.json")       # EPHEMERAL drain loop-guard (nukeable on restart)
LIVE = os.path.join(STATE, "fleet.json")
ARCHIVE = os.path.join(STATE, "archive.json")
LOG = os.path.join(STATE, "log.jsonl")
MODEFILE = os.path.join(STATE, "notify-mode")
DRAFTMODE = os.path.join(STATE, "draft-through")        # opt-in: 'clobber' wakes THROUGH a human draft (E1)

# Agent-helper command hints emitted into conductor context by the awareness/drain hooks. Phase 2
# folded the four standalone plugin scripts into `fleet <verb>` subcommands, so these are now the app
# command strings a conductor runs directly (`fleet` on PATH -> THIS build, via the profile pin). The
# hooks interpolate them as-is: `f"{DIGEST} {frag} 5"` -> `fleet child-digest <frag> 5`.
DIGEST = "fleet child-digest"
ACK = "fleet inbox-ack"
PEERMSG = "fleet peer-msg"


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


# --- the dial (DEMOTED to a mute switch — design 2.1) --------------------------------------
# Wake-now is the DEFAULT. The dial's ONLY job now is the 'passive' override, which suppresses
# idle-wake AND auto-drain fleet-wide (notify + inbox only). Everything non-'passive' — including no
# file and the retired 'autodrain' value — normalizes to 'auto' (wake-now). This INVERTS the old
# default (was 'passive'; wake needed an explicit 'auto'); see NOTIFICATIONS-REDESIGN 2.1 and the loud
# behavior-change note in the report.
def mode():
    try:
        v = open(MODEFILE).read().strip()
    except OSError:
        v = ""
    return "passive" if v == "passive" else "auto"


def autodrain_on():
    # Auto-drain (the Stop hook auto-continuing the turn to process pending completions) is on by
    # default; only 'passive' mutes it. The old distinct 'autodrain' value (drain-without-wake) is
    # retired — with wake-now default the single override is 'passive'.
    return mode() != "passive"


def idlewake_on():
    # Idle-wake is on by default; only 'passive' mutes it. maybe_idle_wake (router) and the heartbeat
    # backstop both gate on this, so 'passive' is a coherent fleet-wide wake mute.
    return mode() != "passive"


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
# Child drain is mode-gated (on unless 'passive'); peer drain fires ALWAYS (critic issue #4). The two
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


def reconcile_session(label, sid_bare, tool=None, event_tool=None):
    """Refresh a live member's stored `session` to the GROUND-TRUTH bound id `sid_bare` when it's either
    empty (lazy first-turn backfill: codex binds on its 1st turn) OR DIVERGED from what the surface
    actually carries. Divergence is the "No conversation found" class: a fresh respawn re-issues the
    conversation id on its first turn, and a bridge id can get stored at bind — so the registry `session`
    drifts from cmux's real live id. The router sees the real id on every Stop, so calling this there keeps
    the registry honest continuously. No-op (returns '') when already in sync or `sid_bare` is empty, so
    it's safe to call unconditionally.
    TOOL-AWARE: `event_tool` (the tool that produced `sid_bare`, via bus_tool) guards cross-tool writes —
    when set and it disagrees with the entry's tool, this REFUSES to write (returns 'skip-tool'), so a
    codex-store id never overwrites a claude agent's session (the berg-sandbox 019f144d trap). `tool`
    defaults to the entry's own tool (claude sessions store the `claude-` prefixed form).
    Returns the action: 'backfill' | 'reconcile' | 'skip-tool' | ''."""
    e = live_get(label)
    if not e or not sid_bare:
        return ""
    t = tool or e.get("tool", "claude")
    if event_tool and event_tool != t:
        return "skip-tool"                              # cross-tool id -> never write it (no contamination)
    stored = bare_uuid(e.get("session") or "")
    if stored == sid_bare:
        return ""
    e["session"] = f"claude-{sid_bare}" if t == "claude" else sid_bare
    live_put(label, e)
    return "backfill" if not stored else "reconcile"


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


_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def bare_uuid(sid):
    """The canonical 36-char session uuid, stripping any '<tool>-' prefix cmux's BUS adds
    (claude-<uuid>, codex-<uuid>, ...). The per-agent STORE keys on the bare uuid; the BUS event's
    session_id is tool-prefixed. Returns sid unchanged if it carries no uuid."""
    m = re.search(_UUID_RE, sid or "")
    return m.group(0) if m else (sid or "")


def bus_tool(sid_raw):
    """The tool prefix cmux's BUS puts on a session_id: 'claude-<uuid>' -> 'claude', 'codex-<uuid>' ->
    'codex'. '' when the id is already bare (no prefix) OR carries no uuid. Keeps session reconciliation
    TOOL-AWARE: a codex-store bridge id must never overwrite a CLAUDE agent's registry session (the live
    berg-sandbox trap — stale 019f144d was a codex id on a now-claude agent)."""
    m = re.search(_UUID_RE, sid_raw or "")
    if not m or m.start() == 0:
        return ""
    return sid_raw[:m.start()].rstrip("-")


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


# --- wake-gate liveness: staleness + bound-session cross-check (design 2.2a) ----------------
# lifecycle() above returns the freshest record's RAW agentLifecycle (for display: `fleet ls`, vitals,
# the cli.py liveness checks). The WAKE GATE needs a stricter question — "is this surface in a GENUINE,
# live, mid-turn 'running' state I must not interrupt?" — because a STALE/orphaned 'running' record
# (a cmux reboot-replay, a move-surface binding desync, a pre-fix summarizer stomp) must NOT read as
# busy: if it does, guard #1 trips on every event and the surface is silenced forever (the cmux-advisor
# stall, root-caused 2026-07-01). We keep lifecycle()'s contract intact (cli.py/features.py depend on
# it) and answer the wake question with a dedicated, hardened predicate.
LIFECYCLE_STALE_S = 90   # a 'running' record that has not ticked within this window is not a live turn


def surface_busy(surface, now=None):
    """True ONLY when `surface` is genuinely mid-turn (a fresh, live 'running' record) — the single case
    the wake gate must never interrupt. Hardened two ways against the stale read that stalled
    cmux-advisor:
      - liveness cross-check: prefer the record for the fleet's OWN bound session (registry truth, kept
        honest by the reconciliation lane) over 'max updatedAt across all records on the surface'. The
        pointer that lied in the incident was cmux's activeSessionsBySurface, not the fleet binding, so
        resolving against the binding defeats the orphaned-'running' record directly.
      - staleness guard: a 'running' record that has not ticked within LIFECYCLE_STALE_S is treated as
        NOT a live turn (a real turn re-stamps continuously; a frozen one is an orphan).
    Leans to NOT-busy on any ambiguity/read error: wake_if_idle then confirms an actual clean idle
    prompt on screen before injecting, so a false 'not busy' can never corrupt a real turn, while a
    false 'busy' — the failure we are killing — can never silence an idle surface."""
    now = time.time() if now is None else now
    try:
        recs = [s for s in (read_hook_store().get("sessions") or {}).values()
                if s.get("surfaceId") == surface]
        if not recs:
            return False                        # nothing claims this surface -> not provably busy
        bound = bare_uuid((entry_for_surface(surface) or {}).get("session", ""))
        rec = None
        if bound:
            rec = next((s for s in recs if bare_uuid(s.get("sessionId", "")) == bound), None)
        if rec is None:                         # no fleet-bound record -> fall back to the freshest
            rec = max(recs, key=lambda s: s.get("updatedAt") or 0)
        if rec.get("agentLifecycle") != "running":
            return False
        return (now - (rec.get("updatedAt") or 0)) <= LIFECYCLE_STALE_S
    except Exception:
        return False                            # fail-open to not-busy; the screen read is the arbiter


# --- draft-through: tier 3 of the wake ladder (design 2.3, E1) ------------------------------
DRAFT_STALE_S = 90   # (reserved for the 3c gate) a draft idle this long reads as walked-away, not active typing


def draft_through():
    """Draft-through policy for tier 3 of the wake ladder — OPT-IN, default OFF:
      'preserve' (default) — never clobber a human draft; it holds, the item waits in the inbox.
      'clobber'  (set `$CMUX_STATE_DIR/draft-through` to 'clobber') — Berg's 'clobber > silence': clear
                 the draft, wake, and LOG the overwrite so a walked-away draft can't silence the surface.
    Default is 'preserve' because the input-CLEAR step is not yet validated against the live cmux TUI for
    multi-line / pasted-image drafts (design 2.3: prototype before committing). Both follow-ups are gated
    on that prototype: 3a save/clear/wake/RESTORE (preserve the draft across the wake, borrowing
    drive-child's settle/verify) and 3c a stale-draft gate (only clobber a draft idle > DRAFT_STALE_S)."""
    try:
        return "clobber" if open(DRAFTMODE).read().strip() == "clobber" else "preserve"
    except OSError:
        return "preserve"


def _wake_through_draft(surface, msg):
    """Tier 3: the surface is idle but a human draft sits in the input box. Preserve it (default) or —
    when draft-through is opted into 'clobber' — best-effort CLEAR the input, wake, and audit the
    overwrite. The clear (send-key ctrl+u = kill-line) is best-effort and wants a live-TUI prototype for
    multi-line / pasted-image drafts; worst case it degrades to a mashed premature submit — never a
    silent stall. Returns True iff it woke."""
    if draft_through() != "clobber":
        return False                                   # preserve — never clobber a draft
    _cmux("send-key", "--surface", surface, "ctrl+u")  # best-effort clear (TUI-validate before enabling)
    _cmux("send", "--surface", surface, msg)
    _cmux("send-key", "--surface", surface, "enter")
    log_event("draft_clobbered", surface=surface)      # audit: a human draft was overwritten to wake
    return True


def wake_if_idle(surface, msg):
    """Inject+submit a wake ONLY when the surface is sitting at a clean prompt with an empty draft;
    otherwise leave it (the item is already durable in the inbox — it is seen next turn). Returns True
    iff it woke. The ONE wake gate, shared by the router idle-wake, peer-msg, broadcast, and the
    heartbeat backstop, so its tier ladder (design 2.3) covers every path:
      1. genuinely mid-turn (surface_busy) -> queue, never interrupt;
      2. idle + empty draft                -> wake now (the common path);
      3. idle + non-empty draft            -> draft-through policy: preserve by default; opt-in
                                              clobber-with-log (design 2.3 tier 3; see draft_through()).
    The SCREEN is ground truth for 'idle at a prompt': a stale/empty/garbage store read must never
    outrank a visibly-idle prompt (that is the whole stall fix), and — the converse — we never inject
    when NO clean prompt is visible (mid-render, a running tool, needsInput), which keeps a wake off a
    busy pane even when surface_busy leaned not-busy on a bad read."""
    if surface_busy(surface):
        return False                                   # tier 1 — never interrupt a live turn
    screen = _cmux("read-screen", "--surface", surface, "--lines", "40")
    prompts = [ln for ln in screen.splitlines() if "❯" in ln]   # ❯ = the compose-prompt marker
    if not prompts:
        return False                                   # no visible prompt -> not idle-at-prompt -> don't inject
    if prompts[-1].split("❯", 1)[1].strip():
        return _wake_through_draft(surface, msg)       # tier 3 — human draft present
    _cmux("send", "--surface", surface, msg)           # tier 2 — clean empty prompt -> wake now
    _cmux("send-key", "--surface", surface, "enter")
    return True
