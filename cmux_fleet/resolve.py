# cmux_fleet/resolve.py — THE one resolver: every question about current reality is answered here.
#
# Design-of-record: the vault entity "Agent management v2: store decisions, derive observations"
# (domains/ai-workflows/cmux/_artifacts/fleet-agent-management-v2.md), sections 2 (state model,
# liveness rule, attachment rule) and "The four axes". Step 1 of the ratified migration.
#
# WHAT THIS MODULE IS IN STEP 1 (read this before "simplifying" it):
#   - The single INTERFACE consumers call: seat(), snapshot(), and the named predicates.
#   - The composition layer: seat() assembles presence + lifecycle + session + workspace + ATTACHMENT
#     into one dict, so no caller ever assembles its own judgment from raw store records again.
#   - The home of the NEW capabilities: the attachment axis (invariant I4), the detached detector,
#     group-membership-from-cmux, and derived placement.
#
# WHAT IT DELIBERATELY IS NOT YET: the physical home of the predicate bodies. The liveness bodies
# (surface_has_live_agent / surface_has_live_pid / lifecycle / surface_busy / resolve_bound_record)
# stay canonical in state.py for this step and are DELEGATED to, because (a) they were already
# consolidated there by the 2026-07-06..10 pid-authority fixes, and (b) the test suite's dominant
# patch seams are state.read_hook_store and those state.* names — delegation keeps every existing
# test seam live while call sites move onto THIS interface. Step 3 (schema v2) physically in-lines
# the bodies here and deletes the state.py names. Do not add a new raw hook-store read anywhere
# outside this module: that is the review's stale-ghost class (six instances, all fixed 2026-07-10).
#
# LIVENESS (invariant I2, stated once): an agent is PRESENT on a surface iff the surface has a
# hook-store record whose pid is alive (and, at kill sites, whose ps identity matches its tool).
# The freshest live-pid record IS the agent. A dead pid is absence, whatever the lifecycle string
# says. lifecycle/updatedAt are advisory display.
#
# ATTACHMENT (invariant I4, stated once): a PRESENT agent is DETACHED when its hook channel is dead
# while its process lives. Detached requires record-frozen AND evidence, never record-frozen alone
# (a frozen record alone describes every idle agent — live-measured: it flags 4 of 11, all merely
# idle; an idle agent must never read detached). The evidence, correctly weighted after
# cmux-advisor's independent test (findings-v2-independent-test-2026-07-10):
#   behavioral — THE detector: transcript mtime advances while the record's updatedAt is frozen (a
#                working agent cmux is deaf to; live-proven on berg-sandbox: record 213.8 min,
#                transcript 0.2 min; zero false positives across 11 agents);
#   env        — the one nameable deterministic CAUSE: ps eww shows a CMUX_WORKSPACE_ID different
#                from the tree's workspace (Gap A, the moved-live-process mechanism; conclusive at
#                any idleness).
# The active-session pointer is NOT a signal: it is written by the Stop hook (measured: absent on a
# fresh launch until the first completed turn), so "pointer stale" is the same bit as "no
# post-SessionStart hook ever fired" — a lagging SYMPTOM of hook death with a false-positive window
# on every fresh seat and a systematic false-negative on any agent that turned once before dying.
# It is exposed as a diagnostic (`ever_heard`), never as proof. Consequence, stated honestly: a
# genuinely idle, genuinely detached, env-correct agent is PASSIVELY UNDETECTABLE; settling it needs
# a driven probe, which belongs to the agent's PARENT (dispatch doctrine applies to diagnostics).
# Remedy for detached is a reseat (recycle resume); the fleet never auto-reseats.
#
# NEVER-ORPHAN (the teardown floor, measured 2026-07-10): SessionEnd REMOVES the hook-store record
# ~0.3s BEFORE the process exits, so "no record" does NOT imply "no process" — the converse of the
# liveness rule is false, and teardown depends on the converse. Kill-target and safe-to-close sets
# therefore come from kill_targets() (store pids UNION ps-env pids), never from store pids alone.
import json
import os
import re
import subprocess
import time

from . import state as fs

# --- attachment thresholds -----------------------------------------------------------------------
# ATTACH_SKEW_S: the behavioral signal fires when the record has been frozen at least this much
# LONGER than the transcript has been quiet (record_age - transcript_age). A live turn re-stamps the
# record every ~1-35s while the transcript advances, so healthy skew is under a minute (validated
# across all 11 live agents, 2026-07-10, zero false positives); the confirmed detached case showed
# 213.8 minutes of skew. 600s clears any legitimate single long tool call (during which BOTH clocks
# freeze together, skew ~0) without delaying detection meaningfully.
ATTACH_SKEW_S = 600


def _cmux(*args):
    """Run a cmux subcommand, return stdout ('' on any error). resolve's ONE shell-out seam; tests
    monkeypatch this (or the higher-level helpers) rather than the live cmux."""
    try:
        p = subprocess.run([fs.CMUX, *args], capture_output=True, text=True,
                           env=dict(os.environ, CMUX_QUIET="1"))
        return p.stdout or ""
    except Exception:
        return ""


# --- record selection (store reads; every one goes through fs.read_hook_store) --------------------
def records(surface, st=None):
    """All hook-store session records claiming `surface`, any liveness."""
    st = fs.read_hook_store() if st is None else st
    surf = (surface or "").upper()
    return [s for s in (st.get("sessions") or {}).values()
            if (s.get("surfaceId") or "").upper() == surf]


def live_records(surface, st=None):
    """The pid-alive subset of records(surface)."""
    return [s for s in records(surface, st) if fs.pid_alive(s.get("pid"))]


def freshest(surface, st=None):
    """The newest record on `surface` by updatedAt, any liveness ({} if none). Display-grade: use
    freshest_live for anything that acts."""
    recs = records(surface, st)
    return max(recs, key=lambda s: s.get("updatedAt") or 0) if recs else {}


def freshest_live(surface, st=None):
    """The newest record on `surface` with an ALIVE pid ({} if none) — the record that IS the agent
    (the liveness rule). This is _live_bound_sid's selection, exposed as the record."""
    recs = live_records(surface, st)
    return max(recs, key=lambda s: s.get("updatedAt") or 0) if recs else {}


def active_ptr(surface, st=None):
    """cmux's activeSessionsBySurface session id for `surface` ('' if none). This pointer is a cmux
    cache and CAN lie (it named a dead record on berg-sandbox); it is read here only as attachment
    evidence, never to resolve the agent."""
    st = fs.read_hook_store() if st is None else st
    e = (st.get("activeSessionsBySurface") or {}).get(surface) \
        or (st.get("activeSessionsBySurface") or {}).get((surface or "").upper()) or {}
    sid = e.get("sessionId") if isinstance(e, dict) else e
    return sid or ""


# --- the named predicates (delegates to the canonical state.py bodies; see module docstring) -------
def lifecycle(surface):
    """Advisory lifecycle string for display (freshest record of any liveness). Never act on it."""
    return fs.lifecycle(surface)


def has_live_pid(surface):
    """Any live pid attached to the surface (the raw pid floor under `present`)."""
    return fs.surface_has_live_pid(surface)


def present(surface):
    """THE liveness answer (invariant I2): a genuinely-live agent occupies the surface."""
    return fs.surface_has_live_agent(surface)


def busy(surface, now=None):
    """The wake gate's question: genuinely mid-turn, must not interrupt. Leans not-busy on doubt;
    the screen check in wake_if_idle is the second gate."""
    return fs.surface_busy(surface, now=now)


def bound_record(surface, st=None, bound=None):
    """The record for the fleet-BOUND session (registry truth), freshest fallback. The doctor's
    record resolution."""
    return fs.resolve_bound_record(surface, st=st, bound=bound)


def pids(surface, st=None):
    """Every ALIVE pid on `surface` — the kill-target set (canonical body here; cli._surface_pids
    delegates). Dead pids are never targets."""
    return {s.get("pid") for s in live_records(surface, st)}


def live_sid(surface, st=None):
    """The sessionId of the freshest ALIVE record ('' if none) — 'which session is actually running
    on this seat right now' (canonical body here; cli._live_bound_sid delegates)."""
    return freshest_live(surface, st).get("sessionId", "")


def _ps_axeww():
    """The raw full-process-table-with-env text (one sweep), '' on any error. Split out so the test
    suite can stub the SWEEP hermetically while pids_ps's parsing stays real code under test."""
    try:
        return subprocess.run(["ps", "axeww"], capture_output=True, text=True,
                              timeout=10).stdout or ""
    except Exception:
        return ""


def pids_ps(surface, ps_out=None):
    """Live pids whose process ENVIRONMENT carries CMUX_SURFACE_ID=<surface>, from one `ps axeww`
    sweep — the store-independent view of who actually sits on this seat. This exists because
    SessionEnd removes the hook-store record ~0.3s before the process exits (measured), and an agent
    that fires SessionEnd then hangs is invisible to the store forever: the never-orphan check must
    not depend on a record existing. `ps_out` is injectable for tests."""
    surf = (surface or "").upper()
    if not surf:
        return set()
    if ps_out is None:
        ps_out = _ps_axeww()
    out = set()
    needle = f"CMUX_SURFACE_ID={surf}"
    for line in ps_out.splitlines():
        if needle not in line.upper():
            continue
        m = re.match(r"\s*(\d+)\s", line)
        if m:
            out.add(int(m.group(1)))
    return {p for p in out if fs.pid_alive(p)}


def kill_targets(surface, st=None):
    """THE teardown set: store-derived live pids UNION ps-env live pids. Sound where either source
    alone is not — the store misses a process whose record SessionEnd already reaped (the measured
    0.3s window, or forever for a hung shutdown); ps-env misses nothing that still runs with the
    seat's surface id in its environment. Every kill site and every safe-to-close check reads this,
    never store pids alone (cmux-advisor finding 2, 2026-07-10)."""
    return pids(surface, st) | pids_ps(surface)


# --- topology (tree-derived; the tree is the only never-stale source) ------------------------------
def surface_ws_map(ttl=2.0):
    """{SURFACE_UUID_UPPER: workspace_uuid} from the live cmux tree, memoized. Canonical body lives
    in features._surface_ws_map for this step (its tests drive the memo directly); this is the one
    interface callers use."""
    from . import features as ff          # lazy: features is view-layer, import cycle-free
    return ff._surface_ws_map(ttl=ttl)


def _ws_from_store(surface, st=None):
    """Store-based workspace fallback: the freshest ALIVE record's workspaceId, else the freshest of
    any. Same live-first rule as everything else (the 519e25c fix); only used when the tree can't be
    read, because a moved surface's store workspaceId freezes."""
    rec = freshest_live(surface, st) or freshest(surface, st)
    return rec.get("workspaceId", "") or ""


def workspace(surface, st=None, ws_map=None):
    """The workspace UUID that currently contains `surface`: live tree first, store fallback, '' if
    unlocatable. None-safe: '' also covers 'surface not in tree' (closed); callers that need to
    distinguish closed-vs-unreadable use the router's move arbiter, which fails closed on purpose."""
    m = surface_ws_map() if ws_map is None else ws_map
    return m.get((surface or "").upper()) or _ws_from_store(surface, st)


def group_members(name_or_ref):
    """The REAL workspace-uuid membership of a group per cmux (None if unreadable/absent) — never the
    registry. Delegates to the cli bodies (their name-to-ref resolution is patched by existing tests);
    step 3 moves them here."""
    from . import cli                      # lazy: avoids the import cycle (cli imports resolve)
    gref = cli._group_ref(name_or_ref)
    if not gref:
        return None
    return cli._group_member_workspaces(gref)


def place_of(entry, parent_entry=None, ws_map=None):
    """DERIVED placement for a live registry entry: 'workspace' when the agent's surface sits in a
    workspace no other tracked agent's parent shares, 'shared' when it sits in its parent conductor's
    workspace (a tab/pane cockpit seat). Step-1 coarseness: distinguishing tab from pane needs
    pane-level tree data and no step-1 consumer needs it; the registry's stored `place` remains the
    display value until step 3."""
    m = surface_ws_map() if ws_map is None else ws_map
    my_ws = m.get((entry.get("surface") or "").upper(), "")
    if not my_ws:
        return None
    if parent_entry:
        parent_ws = m.get((parent_entry.get("surface") or "").upper(), "")
        if parent_ws and parent_ws == my_ws:
            return "shared"
    return "workspace"


# --- attachment (invariant I4) ---------------------------------------------------------------------
def _env_workspace(pid):
    """CMUX_WORKSPACE_ID from the live process's environment (`ps eww`), '' if unreadable. This is
    the launch-time env a hook resolves by; a live process's env cannot be rewritten, which is the
    Gap A mechanism."""
    try:
        out = subprocess.run(["ps", "eww", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=5).stdout or ""
    except Exception:
        return ""
    m = re.search(r"CMUX_WORKSPACE_ID=([0-9A-Fa-f-]{36})", out)
    return m.group(1) if m else ""


def _transcript_age(rec, now):
    """Seconds since the record's transcript file last advanced, None if unknowable. The one
    activity signal that trusts no cmux state."""
    path = rec.get("transcriptPath") or ""
    if not path:
        return None
    try:
        return max(0.0, now - os.stat(path).st_mtime)
    except OSError:
        return None


def attachment(surface, st=None, ws_map=None, now=None):
    """The I4 answer for a surface. Returns
        {attached: bool|None, reasons: [str], record_age_s, transcript_age_s, env_workspace,
         active_ptr, ever_heard: bool|None}
    attached is None when no live agent is present (no channel to judge). Detached iff:
        behavioral: record frozen while the transcript advances (skew > ATTACH_SKEW_S), OR
        env:        process env workspace != tree workspace (conclusive, any idleness).
    An idle agent (both clocks frozen together) can never trip the behavioral path by construction,
    and the env check is the only sound confirm for one. The active pointer is DIAGNOSTIC only
    (`ever_heard`: has cmux heard any post-SessionStart hook from this session?) — it is written by
    the Stop hook, so treating a stale pointer as proof would false-positive every fresh seat and
    false-negative every agent that turned once before dying (cmux-advisor finding 1). A genuinely
    idle, env-correct, detached agent is passively undetectable; its PARENT settles it with a driven
    probe."""
    now = time.time() if now is None else now
    st = fs.read_hook_store() if st is None else st
    rec = freshest_live(surface, st)
    out = {"attached": None, "reasons": [], "record_age_s": None, "transcript_age_s": None,
           "env_workspace": "", "active_ptr": "", "ever_heard": None}
    if not rec:
        return out
    record_age = max(0.0, now - (rec.get("updatedAt") or 0))
    tage = _transcript_age(rec, now)
    out["record_age_s"] = record_age
    out["transcript_age_s"] = tage
    reasons = []
    # behavioral: working while cmux is deaf. Idle agents freeze BOTH clocks, so skew stays ~0.
    if tage is not None and (record_age - tage) > ATTACH_SKEW_S:
        reasons.append("behavioral: transcript advancing while record frozen "
                       f"({record_age/60:.1f}m vs {tage/60:.1f}m)")
    # env: conclusive (Gap A). Needs the tree to know where the surface actually is.
    tree_ws = workspace(surface, st=st, ws_map=ws_map)
    env_ws = _env_workspace(rec.get("pid"))
    out["env_workspace"] = env_ws
    if env_ws and tree_ws and env_ws.upper() != tree_ws.upper():
        reasons.append(f"env: CMUX_WORKSPACE_ID {env_ws[:8]} != tree workspace {tree_ws[:8]}")
    # diagnostic, never proof: has any post-SessionStart hook been heard from the live session?
    aptr = active_ptr(surface, st)
    out["active_ptr"] = aptr
    rec_sid = rec.get("sessionId") or ""
    out["ever_heard"] = bool(aptr and rec_sid and fs.bare_uuid(aptr) == fs.bare_uuid(rec_sid))
    out["reasons"] = reasons
    out["attached"] = not reasons
    return out


# --- the composed views ------------------------------------------------------------------------------
def seat(surface, st=None, ws_map=None, now=None, deep=False):
    """Everything the fleet may know about one seat, composed from the rules above. `deep=True` adds
    the attachment fields (a ps call per seat); reads that only need presence/topology skip it."""
    st = fs.read_hook_store() if st is None else st
    rec_live = freshest_live(surface, st)
    rec_any = freshest(surface, st)
    out = {
        "surface": surface,
        "present": present(surface),
        "record": rec_live or None,
        "pids": pids(surface, st),
        "lifecycle": (rec_any or {}).get("agentLifecycle", ""),
        "session": fs.bare_uuid(rec_live.get("sessionId", "")) if rec_live else "",
        "workspace": workspace(surface, st=st, ws_map=ws_map) or None,
        "attached": None, "attach_reasons": [],
        "record_age_s": None, "transcript_age_s": None, "env_workspace": "", "active_ptr": "",
    }
    if deep and out["present"]:
        att = attachment(surface, st=st, ws_map=ws_map, now=now)
        out.update({"attached": att["attached"], "attach_reasons": att["reasons"],
                    "record_age_s": att["record_age_s"], "transcript_age_s": att["transcript_age_s"],
                    "env_workspace": att["env_workspace"], "active_ptr": att["active_ptr"]})
    return out


def snapshot(deep=False):
    """{label: row} for the whole live fleet: identity + spec passthrough from the registry (v1 rows,
    unchanged schema) + the seat's derived truth. One store read, one tree read, shared across all
    members. This is the read `fleet ls`/vitals/doctor/sidebar views build on, and the staging
    acceptance runs it against a broken world to prove the derived fields tell the truth where the
    cached row lies."""
    st = fs.read_hook_store()
    ws_map = surface_ws_map()
    now = time.time()
    out = {}
    for label, e in fs.live_all().items():
        surf = e.get("surface", "")
        row = {
            "label": label,
            "role": e.get("role"), "kind": e.get("kind"), "parent": e.get("parent"),
            "tool": e.get("tool", "claude"), "muted": bool(e.get("muted")),
            "spec_cwd": e.get("cwd", ""), "spec_place": e.get("place", ""),
            "spec_group": e.get("group", ""),
            "session_hint": e.get("session", ""),
            "seat": seat(surf, st=st, ws_map=ws_map, now=now, deep=deep),
        }
        out[label] = row
    return out
