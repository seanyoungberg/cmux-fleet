#!/usr/bin/env python3
# cmux_fleet/state.py (was fleet_state.py) — the ONE shared state module for the cmux fleet. Folds child-completions and
# peer-messages into a single inbox. CODE lives in the `fleet` APP (the plugin ships only thin hook
# wiring that shells into it); STATE under $CMUX_STATE_DIR (default $XDG_STATE_HOME/cmux-fleet).
#
# Stores (one inbox mechanism):
#   inbox.jsonl        unified append-only message stream. One line: {seq, ts, kind, to, event_key,
#                      **payload}. kind = "completion" | "peer" | "stale" | "doctor".
#   inbox.seq          atomic monotonic counter behind `seq` (router AND peer-msg both append).
#   inbox-cursors.json {surface: {kind: seq}} — per-(surface,kind) ack high-water (batch clears).
#   inbox-acked.json   {surface: {event_key: ts}} — EVENT-level ack ledger: one ack clears that event
#                      on every presentation path AND refuses a producer re-put of the same event.
#   inbox-presented.json {surface: {event_key: {ts,via}}} — presentation cooldown (shown-recently
#                      state, distinct from ack): the heartbeat reminds instead of re-nudging.
#   doctor-dedup.json  durable fleet-doctor condition keys, so daemon restarts do not re-alert
#                      steady-state conditions already seen in a prior process.
#   fleet.json         the LIVE fleet, label-keyed. Ship 5 thin-registry: v1 was flat {role,kind,tool,cwd,
#                      parent,place,status:"live",surface,session}; v2 (after `fleet migrate`) shrinks to
#                      identity {role,kind,parent,tool,gen} + spec (intent) + binding {surface,session_hint}
#                      and DERIVES workspace/status from cmux. Code reads BOTH shapes via the e_* accessors;
#                      live_all()/live_get() return a flat working VIEW regardless of on-disk version.
#   archive.json       PARKED agents, label-keyed: identity + spec + {last_session,binding_cmd,archived_at}.
#   log.jsonl          append-only EVENT ledger: {ts,event,label,role,...}. Source-of-truth timeline.
#   notify-mode        the wake dial, now a MUTE switch: passive (mute) | auto (default, wake-now).
#   router.seq         bus cursor (cmux events --cursor-file). router.log — the router trace.
#
# Identity: kind(child|conductor) / role(type, ->AGENT_ROLE, owns the dir) / label(unique instance,
# ->AGENT_LABEL, the registry key, durable across recycles) / surfaceId(current seat, a mutable field).
import contextlib, fcntl, glob, json, os, re, subprocess, sys, tempfile, time

from .config import STATE, CMUX, HOOKSTORE  # path resolver

INBOX = os.path.join(STATE, "inbox.jsonl")
INBOX_SEQ = os.path.join(STATE, "inbox.seq")
CURSORS = os.path.join(STATE, "inbox-cursors.json")     # DURABLE per-(surface,kind) ack high-water
BLOCKS = os.path.join(STATE, "inbox-blocks.json")       # EPHEMERAL drain loop-guard (nukeable on restart)
LIVE = os.path.join(STATE, "fleet.json")
ARCHIVE = os.path.join(STATE, "archive.json")
LOG = os.path.join(STATE, "log.jsonl")
MODEFILE = os.path.join(STATE, "notify-mode")
DRAFTMODE = os.path.join(STATE, "draft-through")        # policy override: 'clobber' | 'preserve' (default 'stale')
DRAFTMARKS = os.path.join(STATE, "draft-marks.json")    # {surface: {text, since}} — stale-draft-gate age tracking
EXPECTED_CLOSE = os.path.join(STATE, "expected-close.json")  # short-lived CLI-close tombstones (list of {surface_id, ts})
EXPECTED_CLOSE_S = 10   # a CLI-written close tombstone shields _archive_closed_surface from this surface this long
DOCTOR_DEDUP = os.path.join(STATE, "doctor-dedup.json")
ACKED = os.path.join(STATE, "inbox-acked.json")          # DURABLE {surface: {event_key: ts}} event-ack ledger
PRESENTED = os.path.join(STATE, "inbox-presented.json")  # {surface: {event_key: {ts,via}}} presentation cooldown
LEDGER_TTL_S = 14 * 86400   # acked/presented entries older than this are pruned on write (bounded files)
PROVIDER_USAGE = os.path.join(STATE, "provider-usage.json")  # last usage poll snapshot (providers feature)
CODEX_HEALTH = os.path.join(STATE, "codex-health.json")      # per-account token health (edge-trigger dedup)
SCHEMA = os.path.join(STATE, "schema.json")   # {"fleet":2,"archive":2}; absent/1 => pre-migrate v1. Flipped by `fleet migrate`.
SCHEMA_CURRENT = 2                            # Ship 5 thin-registry: identity + spec + binding; workspace/status derived.

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


# --- provider usage snapshot (the providers feature; written by the daemon poller) ---------
def provider_usage_write(data):
    """Persist the latest usage poll (one atomic write; no secrets — utilization %/resets only)."""
    _atomic_write(PROVIDER_USAGE, json.dumps(data, indent=2))


def provider_usage_read():
    """The last usage snapshot, or {} if the poller has never run."""
    return _read_json(PROVIDER_USAGE, {})


def codex_health_write(data):
    """Persist per-account codex token health {acct: {status, email, checked_at}} (edge-trigger dedup)."""
    _atomic_write(CODEX_HEALTH, json.dumps(data, indent=2))


def codex_health_read():
    """The last codex health map, or {} if never checked."""
    return _read_json(CODEX_HEALTH, {})


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


def inbox_put(kind, to_surface, payload, event_key=None):
    """Append one message addressed to a surface. kind: 'completion' | 'peer' | 'stale' | 'doctor'. ALL
    carry a single `to` field (normalized, critic issue #7) so one reader selects 'for me'.

    `event_key` is the row's durable EVENT identity (see row_event_key). A put whose key `to_surface`
    already ACKED is REFUSED (returns 0, no row): the agent handled that event, so a producer replaying
    it — a re-swept doctor condition after dedup-state loss, a re-delivered bus frame — must not
    resurrect it on any presentation path. Only stable keys can pre-exist; the per-row fallback never
    collides. Returns the seq (0 = refused)."""
    if event_key and event_acked(to_surface, event_key):
        return 0
    seq = inbox_next_seq()
    rec = {"seq": seq, "ts": time.time(), "kind": kind, "to": to_surface}
    rec.update(payload)
    rec["event_key"] = event_key or f"{kind}:seq-{seq}"
    _append(INBOX, rec)
    return seq


def row_event_key(r):
    """The durable event identity of an inbox row — 'what happened', independent of which path presents
    it (awareness / drain / wake / heartbeat / `fleet inbox`) and of the row's seq. Producers with a
    stable cross-row identity pass event_key at put time (doctor: reason+label+session; peer: msg_id;
    stale: label+surface; completion: label+session+occurred_at); anything else — including legacy rows
    written before event keys — degrades to a per-row key, which still gives every presentation path ONE
    shared identity to ack/cool-down against, it just can't dedup a re-put."""
    return r.get("event_key") or f"{r.get('kind')}:seq-{r.get('seq')}"


def inbox_read():
    return _read_jsonl(INBOX)


def inbox_pending(surface, kind=None):
    """Unacked messages to this surface, oldest first. TWO ack layers filter here: the per-(surface,kind)
    high-water cursor (critic issue #3: a conductor can ack completions without swallowing an unread
    peer) and the EVENT-key ledger (an acked event stays cleared even if a producer re-put it under a
    new seq the cursor hasn't reached). Every presentation path — awareness, drain, heartbeat, router
    wake gate, `fleet inbox` — reads pending through here, so one ack clears them all at once. Pass
    `kind` to select one stream for display grouping; omit it for the full inbox view."""
    cur = _cursors().get(surface, {})
    acked = acked_events(surface)
    rows = []
    for r in inbox_read():
        if r.get("to") != surface:
            continue
        k = r.get("kind")
        if kind is not None and k != kind:
            continue
        if int(r.get("seq", 0)) <= int(cur.get(k, 0)):
            continue
        if row_event_key(r) in acked:
            continue
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


# --- DURABLE event-level ack (one ack clears every presentation path) ------------------------
def acked_events(surface):
    """{event_key: ts_acked} for `surface` — the event-level ack ledger."""
    return _read_json(ACKED, {}).get(surface, {})


def event_acked(surface, event_key):
    """Has `surface` already acked this event? Producers consult this via inbox_put's refusal."""
    return event_key in acked_events(surface)


def ack_events(surface, rows, now=None):
    """Record `rows`' event keys as HANDLED by `surface`. THE event-level ack: inbox_pending drops an
    acked key from every reader (awareness, drain, heartbeat, router wake gate, `fleet inbox`) and
    inbox_put refuses a producer re-put of it — one ack clears every presentation of that event,
    current AND future, where the per-kind cursor only clears rows the seq high-water has reached.
    Entries older than LEDGER_TTL_S are pruned on write so the file stays bounded (the doctor dedup set
    still blocks steady-state re-emission long after the ledger forgets)."""
    if not rows:
        return
    now = time.time() if now is None else now
    m = _read_json(ACKED, {})
    m.setdefault(surface, {}).update({row_event_key(r): now for r in rows})
    m = {s: {k: t for k, t in ev.items() if now - float(t or 0) < LEDGER_TTL_S} for s, ev in m.items()}
    _atomic_write(ACKED, json.dumps({s: ev for s, ev in m.items() if ev}, indent=2))


# --- presentation cooldown (shown-recently state; distinct from ack) --------------------------
def presented_mark(surface, rows, via, now=None):
    """Stamp `rows` as PRESENTED to `surface` by path `via` (awareness|drain|wake|heartbeat).
    Presentation state, NOT ack: the row stays pending until acked; this only answers 'was this agent
    already shown this event recently?' — the ledger the heartbeat consults so it REMINDS on a
    deliberate interval instead of re-nudging every tick for rows a wake/drain/awareness pass already
    surfaced. Entries older than LEDGER_TTL_S are pruned on write. Best-effort (never raises into a
    wake/hook path)."""
    if not rows:
        return
    try:
        now = time.time() if now is None else now
        m = _read_json(PRESENTED, {})
        m.setdefault(surface, {}).update({row_event_key(r): {"ts": now, "via": via} for r in rows})
        m = {s: {k: v for k, v in ev.items() if now - float((v or {}).get("ts") or 0) < LEDGER_TTL_S}
             for s, ev in m.items()}
        _atomic_write(PRESENTED, json.dumps({s: ev for s, ev in m.items() if ev}, indent=2))
    except Exception:
        pass


def unpresented(surface, rows, within, now=None):
    """The subset of `rows` NOT shown to `surface` within the last `within` seconds by ANY path — the
    heartbeat's re-nudge filter. A NEW row (never presented) passes at once; an already-shown row passes
    again only after `within` elapses (the deliberate reminder backstop for a row nobody acked). Fails
    open to 'not presented' so a garbage ledger can only cause an extra nudge, never a silent stall."""
    now = time.time() if now is None else now
    seen = _read_json(PRESENTED, {}).get(surface, {})
    out = []
    for r in rows:
        v = seen.get(row_event_key(r))
        try:
            fresh = v is not None and (now - float((v or {}).get("ts") or 0)) < within
        except (TypeError, ValueError):
            fresh = False
        if not fresh:
            out.append(r)
    return out


def inbox_event_rearm(event_key):
    """Forget `event_key` in every surface's acked AND presented ledgers. For condition-keyed events
    (fleet-doctor): the sweep observed the condition CLEAR, so its next occurrence is a NEW event that
    must alert again — without this, the acked key would suppress a genuine re-alert forever. One-shot
    keys (peer msg ids, stale closes, completions) never re-arm; they age out via LEDGER_TTL_S."""
    for path in (ACKED, PRESENTED):
        m = _read_json(path, {})
        if not any(event_key in ev for ev in m.values()):
            continue
        for ev in m.values():
            ev.pop(event_key, None)
        _atomic_write(path, json.dumps({s: ev for s, ev in m.items() if ev}, indent=2))


# --- DURABLE fleet-doctor condition dedup --------------------------------------------------
def doctor_dedup_load():
    """Return the persisted fleet-doctor condition keys as {(reason, label, session)}.

    This is condition state, not ack state: it records bad conditions the sweep has already seen so a
    daemon restart does not produce a new doctor row for the same steady-state member. The router prunes
    it when a condition clears, a member leaves live state, or the member's bound session changes.
    """
    rows = _read_json(DOCTOR_DEDUP, [])
    out = set()
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        reason = str(r.get("reason") or "")
        label = str(r.get("label") or "")
        session = str(r.get("session") or "")
        if reason and label:
            out.add((reason, label, session))
    return out


def doctor_dedup_save(keys, now=None):
    """Persist fleet-doctor condition keys. `now` stamps the write for operator inspection only."""
    now = time.time() if now is None else now
    norm = sorted({(str(r or ""), str(l or ""), str(s or "")) for r, l, s in keys
                   if str(r or "") and str(l or "")})
    rows = [{"reason": r, "label": l, "session": s, "ts": now} for r, l, s in norm]
    _atomic_write(DOCTOR_DEDUP, json.dumps(rows, indent=2))


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


# --- schema v2 (Ship 5 thin-registry): identity + spec + binding, workspace/status derived ----------
# The row shrinks to what cmux has NO concept of. `to_v2` is the one shape; `to_v1` denormalizes back so a
# not-yet-migrated state dir keeps writing v1 (reversible until Berg runs `fleet migrate`). Accessors read
# BOTH shapes, so read sites convert incrementally with the suite staying green on the live v1 fixtures.
_ID_KEYS = ("role", "kind", "parent", "tool", "gen")
_SPEC_KEYS = ("cwd", "place", "group", "muted", "provider", "worktree", "plugins", "flags", "settings")
_BINDING_KEYS = ("surface", "session_hint", "launchedAt")
_ARCHIVE_KEEP = ("last_session", "binding_cmd", "binding_cwd", "archived_at")  # archive-row metadata: stays top-level
_DERIVED_DROP = ("workspace", "status")          # cmux owns these now; deleted from the row (resolve.py derives)
_KNOWN_TOP = set(_ID_KEYS) | set(_SPEC_KEYS) | set(_ARCHIVE_KEEP) | {
    "surface", "session", "session_hint", "launchedAt", "spec", "binding", "v"} | set(_DERIVED_DROP)


def is_v2(row):
    return isinstance(row, dict) and isinstance(row.get("spec"), dict) and isinstance(row.get("binding"), dict)


def to_v2(row):
    """v1-flat OR v2-nested -> v2-nested. PURE + IDEMPOTENT. Drops the derived fields (workspace, status);
    `session` -> binding.session_hint (kept for display + the router's reconcile target, never as the acting
    id); `gen` defaults to 1. Unknown legacy top-level keys are preserved INTO spec so nothing is lost.

    ABSORBS top-level overrides even when `row` is already v2: a caller doing `{**v2row, "place": x}`
    (the common merge-write idiom) lands `place` in spec, `surface` in binding, etc. — so write sites keep
    using the flat-merge idiom and this normalizes it on the way to disk."""
    if not isinstance(row, dict):
        return row
    spec = dict(row.get("spec") or {})           # start from any existing nested blocks...
    binding = dict(row.get("binding") or {})
    # preserve every identity key that is PRESENT (even parent=None: a top-level conductor is meaningful,
    # and the migrator must be faithful — never drop or invent parentage, §2.G).
    r = {k: row[k] for k in ("role", "kind", "parent", "tool") if k in row}
    r["gen"] = row.get("gen", 1)
    for k in _ARCHIVE_KEEP:                       # archive metadata stays top-level (revive reads it there)
        if k in row:
            r[k] = row[k]
    for k in _SPEC_KEYS:                          # ...then overlay any top-level spec keys (the merge idiom)
        if k in row:
            spec[k] = row[k]
    for k, v in row.items():                      # forward-safe: keep any unknown legacy key in spec
        if k not in _KNOWN_TOP:
            spec[k] = v
    if row.get("surface") is not None:
        binding["surface"] = row["surface"]
    if "session_hint" in row:
        binding["session_hint"] = row["session_hint"]
    elif "session" in row:
        binding["session_hint"] = row.get("session")
    if "launchedAt" in row:
        binding["launchedAt"] = row["launchedAt"]
    r["spec"], r["binding"] = spec, binding
    return r


def to_v1(row):
    """v2-nested OR v1-flat -> v1-flat. The inverse used to persist under an UN-migrated (v1) state dir, so
    adopting Ship 5 does not rewrite the on-disk shape until `fleet migrate` runs (reversible). Flattens
    identity+spec+binding to top level; session_hint -> session; keeps `gen` (an unknown extra a v1 reader
    ignores). Does NOT resurrect workspace/status (already derived + ignored by every reader since step 1)."""
    if not isinstance(row, dict) or not is_v2(row):
        return dict(row) if isinstance(row, dict) else row
    flat = {k: row[k] for k in ("role", "kind", "parent", "tool") if k in row}
    flat["gen"] = row.get("gen", 1)
    for k in _ARCHIVE_KEEP:
        if k in row:
            flat[k] = row[k]
    flat.update(row.get("spec") or {})
    b = row.get("binding") or {}
    if b.get("surface") is not None:
        flat["surface"] = b["surface"]
    if "session_hint" in b:
        flat["session"] = b.get("session_hint")
    if "launchedAt" in b:
        flat["launchedAt"] = b["launchedAt"]
    return flat


# --- row-field accessors: read a registry row REGARDLESS of shape (v1 flat OR v2 nested) --------------
def e_surface(e):
    if not isinstance(e, dict):
        return ""
    return (e.get("binding") or {}).get("surface") if is_v2(e) else e.get("surface") or ""


def e_session(e):
    """The session HINT (display + router reconcile target; verbs that ACT resolve live)."""
    if not isinstance(e, dict):
        return ""
    return ((e.get("binding") or {}).get("session_hint") if is_v2(e) else e.get("session")) or ""


def e_spec(e, key, default=None):
    """A spec (intent) field: cwd/place/group/muted/provider/worktree/plugins/flags/settings."""
    if not isinstance(e, dict):
        return default
    if is_v2(e):
        return (e.get("spec") or {}).get(key, default)
    return e.get(key, default)


def e_gen(e):
    return int((e or {}).get("gen", 1) or 1) if isinstance(e, dict) else 1


# --- schema version marker + `fleet migrate` (Berg-run; the build never runs it against live) ---------
def schema_ver(which="fleet"):
    """On-disk schema version ('fleet'|'archive'): 1 pre-migrate, 2 after `fleet migrate`. Absent file/key
    => 1, so an un-migrated dir stays v1 and reversible."""
    try:
        return int(_read_json(SCHEMA, {}).get(which, 1) or 1)
    except Exception:
        return 1


def _schema_set(which, ver):
    m = _read_json(SCHEMA, {})
    m[which] = ver
    _atomic_write(SCHEMA, json.dumps(m, indent=2))


def _persist_shape(entry, which):
    """Coerce `entry` to the shape the on-disk store is at: v2 once migrated, else v1-flat (reversible)."""
    return to_v2(entry) if schema_ver(which) >= 2 else to_v1(entry)


@contextlib.contextmanager
def _flock(path):
    """Exclusive cross-process lock around a read-modify-write of `path` (mirrors inbox_next_seq). Closes
    R1: two conductors reparenting concurrently used to lost-update the whole-file rewrite (writer A reads
    {r1,r2}, B reads {r1,r2}, A writes {r1',r2}, B writes {r1,r2'} -> r1' lost -> the 07-16 graph flicker)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def migrate_state(backup=True):
    """The one-shot v1->v2 migrator (`fleet migrate`). Idempotent, flocked, backs up first. Rewrites every
    fleet.json + archive.json row via to_v2 and flips the schema marker. Berg-run — the build NEVER calls
    this against the live registry. Returns {fleet, archive, backups}."""
    out = {"fleet": 0, "archive": 0, "backups": []}
    for path, key in ((LIVE, "fleet"), (ARCHIVE, "archive")):
        with _flock(path):
            m = _read_json(path, {})
            if not isinstance(m, dict):
                continue
            if backup and os.path.exists(path):
                bak = f"{path}.v1.bak-{int(time.time())}"
                _atomic_write(bak, json.dumps(m, indent=2))
                out["backups"].append(bak)
            migrated = {label: to_v2(row) for label, row in m.items()}
            _atomic_write(path, json.dumps(migrated, indent=2))
            _schema_set(key, SCHEMA_CURRENT)
            out[key] = len(migrated)
    return out


# --- identity: the LIVE fleet (label-keyed) ------------------------------------------------
def live_all():
    """Every live row as a FLAT working view (identity+spec+binding flattened to top level), regardless of
    the on-disk schema. This is a fresh derive per read — NOT persisted duplication, so it cannot drift —
    and it is what lets existing `e.get("surface")`/`("cwd")`/`("place")` readers keep working across the v2
    cutover unchanged. The DERIVED topology (workspace/status) is NOT in the view: those are gone from a v2
    row and their readers go through resolve.py. `live_put` re-nests to the on-disk shape on the way back."""
    return {k: to_v1(v) for k, v in _read_json(LIVE, {}).items()}


def live_get(label):
    return live_all().get(label)


def live_put(label, entry):
    """Flocked whole-row write (R1). Persists in the on-disk schema version so an un-migrated dir stays v1
    and reversible; a migrated dir stores v2."""
    with _flock(LIVE):
        m = _read_json(LIVE, {})
        m[label] = _persist_shape(entry, "fleet")
        _atomic_write(LIVE, json.dumps(m, indent=2))


def live_del(label):
    with _flock(LIVE):
        m = _read_json(LIVE, {})
        e = m.pop(label, None)
        _atomic_write(LIVE, json.dumps(m, indent=2))
        return e


def live_update(label, mutate):
    """Flocked read-modify-write of ONE row (the correct R1 primitive for a merge/conditional write):
    mutate(entry_or_None) -> entry_or_None (None deletes). Two writers touching the SAME row serialize."""
    with _flock(LIVE):
        m = _read_json(LIVE, {})
        new = mutate(m.get(label))
        if new is None:
            m.pop(label, None)
        else:
            m[label] = _persist_shape(new, "fleet")
        _atomic_write(LIVE, json.dumps(m, indent=2))
        return new


def label_for_surface(surface):
    for label, v in live_all().items():
        if e_surface(v) == surface:
            return label
    return ""


def surface_for_label(label):
    return e_surface(live_get(label) or {})


def entry_for_surface(surface):
    for v in live_all().values():
        if e_surface(v) == surface:
            return v
    return None


# --- unified --scope model (ratified 2026-07-07) -------------------------------------------------
# ONE scoping vocabulary on every scope-aware verb. `mine|all|conductors|children` are the SET values
# (a bare <label> is a single-target scope the verb resolves itself); the only thing that varies per
# verb is the DEFAULT (reads default `mine`, acts require an explicit scope). scope_matches is the ONE
# predicate behind every selector, so a read's view set and an act's target set can never drift.
SCOPE_SETS = ("mine", "all", "conductors", "children")


def is_my_child(entry, parent_label):
    """The `mine` parent-match: a CHILD whose parent label == mine. `entry` is any dict carrying
    `kind`+`parent` (a live registry entry, an archive row, or a vitals snapshot row)."""
    return entry.get("kind") == "child" and entry.get("parent") == parent_label


def scope_matches(scope, entry, label, caller_label, *, include_self):
    """Does `entry` (registry key `label`) fall in a SET-valued --scope, relative to caller_label?
      all         -> everyone
      conductors  -> kind == conductor          children -> kind == child
      mine        -> caller's direct children (+ caller itself iff include_self)
    Reads pass include_self=True (show yourself in context); acts pass False (never fan out onto self).
    A non-set scope (a specific label) returns False here — set-scope verbs validate the value first."""
    kind = entry.get("kind")
    if scope == "all":
        return True
    if scope == "conductors":
        return kind == "conductor"
    if scope == "children":
        return kind == "child"
    if scope == "mine":
        return is_my_child(entry, caller_label) or (include_self and label == caller_label)
    return False


def scope_members(scope, caller_label, *, include_self):
    """The live (label, entry) members a SET-valued --scope selects, relative to caller_label."""
    return [(l, v) for l, v in live_all().items()
            if scope_matches(scope, v, l, caller_label, include_self=include_self)]


def pop_scope(argv, default=None):
    """Pull `--scope <value>` out of a manually-parsed argv list. Returns (value_or_default, remaining).
    Manual-parse verbs (ls/vitals/graph/inbox/broadcast) use this; recycle uses argparse instead."""
    args = list(argv)
    if "--scope" in args:
        i = args.index("--scope")
        val = args[i + 1] if i + 1 < len(args) else ""
        del args[i:i + 2]
        return val, args
    return default, args


def only_self_hint(verb):
    """The one-line 'you have no children yet' nudge a read prints when `--scope mine` resolves to just
    you, so nobody thinks the fleet is empty when it's only their corner of it that is."""
    return f"(only you — no children. fleet {verb} --scope all for the whole fleet.)"


def read_scope(scope_arg, verb, *, sets_only=True):
    """Normalize + resolve a READ verb's --scope (None = the default `mine`) and its caller label.
    Returns (scope, caller_label). Two rules make the default humane:
      * The graceful no-surface fallback — an OMITTED scope with no $CMUX_SURFACE_ID falls back to `all`
        (a human at a plain shell / CI just wants the fleet). An agent always carries a surface, so it
        still gets its own scope; only an identity-less shell widens to the world.
      * An EXPLICIT `--scope mine` with no surface is a usage error (you named identity-relative scope but
        have no identity) — mirrors broadcast/recycle's message.
    With sets_only (ls/vitals), a bare <label> is rejected; graph/inbox pass sets_only=False and resolve a
    label themselves."""
    explicit = scope_arg is not None
    scope = scope_arg if explicit else "mine"
    surface = os.environ.get("CMUX_SURFACE_ID", "")
    if scope == "mine" and not surface:
        if explicit:
            sys.exit(f"[fleet] {verb} --scope mine needs $CMUX_SURFACE_ID (run inside a conductor); use --scope all")
        scope = "all"                                          # identity-less shell -> show the world
    if sets_only and scope not in SCOPE_SETS:
        sys.exit(f"[fleet] {verb}: --scope must be one of {list(SCOPE_SETS)} (a bare label isn't a listing scope)")
    return scope, (label_for_surface(surface) if surface else "")


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
    action = [""]

    def mut(e):
        if not e or not sid_bare:
            return e
        t = tool or e.get("tool", "claude")
        if event_tool and event_tool != t:
            action[0] = "skip-tool"                     # cross-tool id -> never write it (no contamination)
            return e
        stored = bare_uuid(e_session(e) or "")
        if stored == sid_bare:
            return e
        val = f"claude-{sid_bare}" if t == "claude" else sid_bare
        if is_v2(e):
            e.setdefault("binding", {})["session_hint"] = val
        else:
            e["session"] = val
        action[0] = "backfill" if not stored else "reconcile"
        return e

    live_update(label, mut)                             # flocked read-modify-write (R1)
    return action[0]


# --- expected-close tombstones (CLI <-> router registry-hygiene handshake) ------------------------
# A DELIBERATE close (fleet rm / archive / --with-group dissolve) races the router's surface.closed
# handler across processes: the CLI's live_del is meant to land before the router resolves the closed
# surface, but registry() is mtime-cached at 1s granularity so the close can beat live_del's visibility
# and _archive_closed_surface then mis-reads the intentional retirement as an accidental external close
# (spurious `kind='stale'` "revive?" alert to the parent). The CLI stamps a short-lived tombstone BEFORE
# it closes, and the router checks it — a deterministic signal that doesn't depend on lookup timing.
def expected_close_put(surface_id, now=None):
    """Tombstone `surface_id` as a DELIBERATE CLI close. Appends {surface_id, ts} and prunes expired rows
    on write, so the file can't grow. No-op on an empty surface id. Best-effort (never raises into a
    close path)."""
    if not surface_id:
        return
    now = time.time() if now is None else now
    try:
        rows = [r for r in _read_json(EXPECTED_CLOSE, [])
                if isinstance(r, dict) and (now - float(r.get("ts") or 0)) < EXPECTED_CLOSE_S]
        rows.append({"surface_id": surface_id, "ts": now})
        _atomic_write(EXPECTED_CLOSE, json.dumps(rows))
    except Exception:
        pass


def expected_close_recent(surface_id, now=None):
    """True iff `surface_id` was CLI-close-tombstoned within EXPECTED_CLOSE_S. Read-only + fail-open: a
    missing/garbage file returns False, so a GENUINE external close (the path we must never suppress)
    still archives + alerts."""
    if not surface_id:
        return False
    now = time.time() if now is None else now
    for r in _read_json(EXPECTED_CLOSE, []):
        if isinstance(r, dict) and r.get("surface_id") == surface_id \
                and (now - float(r.get("ts") or 0)) < EXPECTED_CLOSE_S:
            return True
    return False


# --- identity: the ARCHIVE shelf (parked, revivable) ---------------------------------------
def archive_all():
    """Archive rows as a FLAT working view (same rationale as live_all): revive/ls read `last_session`,
    `cwd`, `place`, `binding_cmd` etc. by top-level key across the v2 cutover unchanged."""
    return {k: to_v1(v) for k, v in _read_json(ARCHIVE, {}).items()}


def archive_get(label):
    return archive_all().get(label)


def archive_put(label, entry):
    with _flock(ARCHIVE):
        m = _read_json(ARCHIVE, {})
        m[label] = _persist_shape(entry, "archive")
        _atomic_write(ARCHIVE, json.dumps(m, indent=2))


def archive_del(label):
    with _flock(ARCHIVE):
        m = _read_json(ARCHIVE, {})
        e = m.pop(label, None)
        _atomic_write(ARCHIVE, json.dumps(m, indent=2))
        return e


# --- the event ledger ----------------------------------------------------------------------
def _invoker():
    """Best-effort attribution for WHO ran the `fleet` command behind this event: a fleet AGENT's own
    identity (AGENT_LABEL/AGENT_ROLE env, set on every agent-issued fleet call) if one is driving, else a
    breadcrumb for a bare human shell (whoami@tty). Diagnostic only, not an audit system -- the
    2026-07-02 incident forensics dead-ended trying to figure out WHO ran a destructive command by
    correlating external logs by hand because no event recorded an invoker at all."""
    label = os.environ.get("AGENT_LABEL")
    if label:
        return f"agent:{label}"
    role = os.environ.get("AGENT_ROLE")
    if role:
        return f"agent-role:{role}"
    try:
        import pwd
        who = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        who = os.environ.get("USER") or os.environ.get("LOGNAME") or "?"
    try:
        tty = os.ttyname(0)
    except OSError:
        tty = "no-tty"
    return f"shell:{who}@{tty} ppid={os.getppid()}"


def log_event(event, **fields):
    rec = {"ts": time.time(), "event": event, "invoker": _invoker()}
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


# NOTE: lifecycle() moved to resolve.py (finish-5b-2 step 3 — resolve owns the liveness bodies now;
# this module keeps the store I/O they read: read_hook_store / pid_alive / bare_uuid / entry_for_surface).


# --- pid liveness + dead-record reaping (the SessionEnd-freeze backstop, 2026-07-06) --------
# WHY: `lifecycle()` returns cmux's raw agentLifecycle, which is SessionEnd-driven. Empirically
# (sandbox matrix, see the recycle-sessionend brief) an agent killed WITHOUT a clean SessionEnd —
# SIGKILL/abrupt death, OR the incident's SessionEnd-store-write race under cmux load — leaves the
# record FROZEN at its last value ('running'/'idle'/'unknown') with a DEAD or None pid. That string
# is then a permanent lie: recycle's terminal-check never passes, ls shows a false 'live', the doctor
# trusts a dead 'running'. The one signal that never lies is the PID: a dead/None pid == the agent is
# gone, regardless of the lifecycle string. These helpers make the pid the authority for "is it gone".
def pid_alive(pid):
    """True iff `pid` is a live process. None/0/garbage -> False. os.kill(pid, 0) sends no signal but
    raises ProcessLookupError for a dead pid; EPERM means it exists under another uid (still alive)."""
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError):
        return False            # None / non-numeric
    except ProcessLookupError:
        return False            # no such process -> dead
    except PermissionError:
        return True             # exists, owned by another uid -> alive
    except OSError:
        return False
    return True


# NOTE: surface_has_live_pid() + surface_has_live_agent() moved to resolve.py (finish-5b-2 step 3).
# They compose pid_alive() (kept here) with read_hook_store()/lifecycle() reads; resolve owns the
# liveness rule now, this module owns the store I/O they call back into via fs.*.


def reap_dead_surface_records(surface, dry_run=False):
    """Remove FROZEN, provably-dead session records for `surface` from cmux's per-tool hook stores
    (~/.cmuxterm/<tool>-hook-sessions.json) — the ghost a SessionEnd-less death leaves behind. SAFETY:
    a record whose pid is ALIVE is NEVER touched (that is a real live agent; a surface can legitimately
    hold a live record AND a dead ghost — e.g. a recovered seat — and only the ghost is reaped). Also
    clears activeSessionsBySurface / activeSessionsByWorkspace pointers that referenced a reaped session
    so the surface reads as free. Returns {'reaped': [...], 'live_kept': [...], 'files': [...]}. Pure
    read on dry_run. Note: cmux may hold its own in-memory copy; this fixes what the FLEET reads (the
    file), which is what recycle/ls/doctor consult — a fresh SessionStart or cmux's own reap reconciles
    cmux's UI afterward."""
    surf = (surface or "").upper()
    reaped, live_kept, files = [], [], []
    for path in sorted(glob.glob(os.path.join(HOOKSTORE, "*-hook-sessions.json"))):
        try:
            d = json.load(open(path))
        except Exception:
            continue
        sessions = d.get("sessions") or {}
        drop = []
        for sid, s in list(sessions.items()):
            if (s.get("surfaceId") or "").upper() != surf:
                continue
            if pid_alive(s.get("pid")):
                live_kept.append({"sid": sid, "pid": s.get("pid"), "life": s.get("agentLifecycle")})
            else:
                drop.append(sid)
        if not drop:
            continue
        for sid in drop:
            reaped.append({"sid": sid, "pid": sessions[sid].get("pid"),
                           "life": sessions[sid].get("agentLifecycle"), "file": os.path.basename(path)})
        if dry_run:
            files.append(path)
            continue
        dropped_ids = set(drop) | {bare_uuid(x) for x in drop}
        for sid in drop:
            sessions.pop(sid, None)

        def _points_at_dropped(v):
            # cmux's active-pointer VALUE is a dict ({"sessionId": ..., "updatedAt": ...}); older/other
            # shapes may store a bare sid string. Handle both, then match against the reaped sessions.
            sid = v.get("sessionId") if isinstance(v, dict) else v
            return sid in dropped_ids or bare_uuid(sid or "") in dropped_ids

        abs_ = d.get("activeSessionsBySurface") or {}
        for k in [k for k, v in abs_.items() if k.upper() == surf or _points_at_dropped(v)]:
            abs_.pop(k, None)
        abw = d.get("activeSessionsByWorkspace") or {}
        for k in [k for k, v in abw.items() if _points_at_dropped(v)]:
            abw.pop(k, None)
        _atomic_write(path, json.dumps(d, indent=2))
        files.append(path)
    return {"reaped": reaped, "live_kept": live_kept, "files": files}


# --- wake-gate staleness window (the predicates that use it live in resolve.py) -------------
# LIFECYCLE_STALE_S bounds "is this a live turn?": a 'running' record that has not ticked within it is
# treated as NOT mid-turn (a real turn re-stamps continuously; a frozen one is an orphan). The wake-gate
# predicates that apply it — resolve.surface_busy + its resolver resolve.resolve_bound_record — moved to
# resolve.py (finish-5b-2 step 3, root incident: the 2026-07-01 cmux-advisor stall from an orphaned
# 'running' record). The constant stays here as shared state config; resolve reads it via fs.LIFECYCLE_STALE_S.
LIFECYCLE_STALE_S = 90   # a 'running' record that has not ticked within this window is not a live turn


# --- draft-through: tier 3 of the wake ladder (design 2.3 / 3c, E1) -------------------------
DRAFT_STALE_S = 90   # a draft UNCHANGED this long reads as walked-away (abandoned), not active typing


def draft_through():
    """Draft-through policy for tier 3 of the wake ladder:
      'stale' (DEFAULT) — the stale-draft gate: clobber a WALKED-AWAY draft (unchanged >= DRAFT_STALE_S)
              so it can't silence the surface indefinitely, but PRESERVE a fresh draft (protects active
              typing). Meets the redesign's 'never an indefinite silent stall' WITHOUT depending on the
              unvalidated save/clear/RESTORE round-trip (Berg: clobber > silence for an abandoned draft).
      'clobber' — immediate clobber-with-log on ANY draft (no wait); the aggressive opt-in.
      'preserve' — never clobber (fully conservative; a walked-away draft just waits in the inbox).
    Override via `$CMUX_STATE_DIR/draft-through` = 'clobber' | 'preserve'; absent/other = 'stale'."""
    try:
        v = open(DRAFTMODE).read().strip()
    except OSError:
        v = ""
    return v if v in ("clobber", "preserve") else "stale"


def _draft_age(surface, draft, now):
    """Seconds `draft` has sat UNCHANGED in `surface`'s input box (0 when new or just changed). Persisted
    to DRAFTMARKS so the age accrues across router/heartbeat/peer calls; active typing (changed text)
    resets the clock, so only a genuinely walked-away draft ever crosses DRAFT_STALE_S."""
    m = _read_json(DRAFTMARKS, {})
    e = m.get(surface)
    if e and e.get("text") == draft:
        return max(0.0, now - float(e.get("since") or now))
    m[surface] = {"text": draft, "since": now}          # new/changed draft -> (re)start the clock
    _atomic_write(DRAFTMARKS, json.dumps(m, indent=2))
    return 0.0


def _clear_draft_mark(surface):
    m = _read_json(DRAFTMARKS, {})
    if surface in m:
        m.pop(surface, None)
        _atomic_write(DRAFTMARKS, json.dumps(m, indent=2))


def _wake_through_draft(surface, msg, draft):
    """Tier 3: the surface is idle but a human draft sits in the input box. Per draft_through():
    'preserve' never wakes; 'clobber' wakes immediately; 'stale' (default) waits until the draft is
    unchanged for DRAFT_STALE_S (walked away) then wakes — preserving a fresh draft. The clobber
    best-effort CLEARs the input (send-key ctrl+u; degrades to a mashed submit, never a silent stall)
    then wakes and audits the overwrite. Returns True iff it woke."""
    mode = draft_through()
    if mode == "preserve":
        return False                                   # never clobber a draft
    if mode == "stale" and _draft_age(surface, draft, time.time()) < DRAFT_STALE_S:
        return False                                   # fresh draft (maybe active typing) -> preserve for now
    _cmux("send-key", "--surface", surface, "ctrl+u")  # best-effort clear (TUI-validate for 3a; degrades to mashed submit)
    _cmux("send", "--surface", surface, msg)
    _cmux("send-key", "--surface", surface, "enter")
    _clear_draft_mark(surface)                          # draft consumed -> reset the age clock
    log_event("draft_clobbered", surface=surface, mode=mode)   # audit: a human draft was overwritten to wake
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
    from . import resolve as rs   # lazy: resolve imports state at module load; call-time breaks the cycle
    if rs.surface_busy(surface):
        return False                                   # tier 1 — never interrupt a live turn
    screen = _cmux("read-screen", "--surface", surface, "--lines", "40")
    prompts = [ln for ln in screen.splitlines() if "❯" in ln]   # ❯ = the compose-prompt marker
    if not prompts:
        return False                                   # no visible prompt -> not idle-at-prompt -> don't inject
    draft = prompts[-1].split("❯", 1)[1].strip()
    if draft:
        return _wake_through_draft(surface, msg, draft)   # tier 3 — human draft present
    _clear_draft_mark(surface)                         # clean prompt -> any prior draft is gone; reset the clock
    _cmux("send", "--surface", surface, msg)           # tier 2 — clean empty prompt -> wake now
    _cmux("send-key", "--surface", surface, "enter")
    return True
