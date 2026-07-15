#!/usr/bin/env python3
# cmux_fleet/reconcile.py — restore reconciliation (redesign Ship 2).
#
# THE DRIFT THIS CLOSES. cmux crash-restore is driven by cmux's OWN app snapshot
# (~/Library/Application Support/cmux/session-<bundleId>.json), which the fleet does not write or read.
# On relaunch cmux auto-resumes every snapshot surface whose `wasAgentRunning != false` (unknown => true)
# as `claude --resume <id>`, and recreates every other snapshot surface as a bare-shell HUSK. The fleet's
# registry never participated in that snapshot, so after a relaunch the live tree carries:
#   • husks           — bare shells (snapshot agent=nil) for agents that had exited; cmux never reaps a
#                       grandchild-under-a-shell (the fleet's own `; exec /bin/zsh` keeps the pane alive),
#                       so they accumulate (measured: 69/83 prod snapshot surfaces were husk residue).
#   • resume-orphans  — agents cmux resumed under NEW pids that the fleet registry doesn't track (the
#                       "14 live pids vs 12 registry" class), gated only by wasAgentRunning=None.
#
# THE RECONCILIATION. Run on daemon start and on the post-relaunch surface.created burst. Classify every
# live TERMINAL surface against a DETERMINISTIC signal (never the gated pane-content husk heuristic
# `cli._husk_evidence`):
#   tracked        in the fleet registry                                   -> keep (adopt: reconcile ws)
#   resume-orphan  a LIVE agent not in the registry                        -> FLAG (never close a live agent)
#   husk           snapshot agent=nil AND no live agent AND not in registry
#                  AND provably FLEET-ORIGIN                                -> CLOSE (archive-first)
#   human-shell    no live agent, not in registry, NOT fleet-origin        -> LEAVE (safety floor)
#
# WHY A FLEET-ORIGIN GATE IS MANDATORY. cmux restore recreates EVERY surface as agent=nil, including a
# human's shell. So `agent=nil + no pid + no registry` alone would sweep a human's restored shell. The
# fleet-origin gate — the surface's persisted scrollback carries the fleet launch signature
# (AGENT_LABEL=/AGENT_ROLE=/CMUX_FLEET_*, which a human's shell NEVER contains), or the surface sits in a
# fleet-managed workspace — is the deterministic thing that makes closing safe. This is an EXACT env-var
# match, not the tail-analysis heuristic reap-surfaces --close is gated on; that gate stays closed.
#
# ARCHIVE-FIRST + RECOVERABLE. A closed husk is archived first (harvested label + last-session), so a
# human can `fleet revive` it. Every close writes an expected-close tombstone (so the router doesn't
# re-alert it) and is verified against a fresh tree. cmux is the authority for restore: closing a surface
# removes it from the snapshot by ABSENCE (the snapshot is a live-tree projection), so a reconciled-away
# husk does not come back on the next relaunch.
import glob
import os
import plistlib
import re
import time

from .config import CMUX
from . import state as fs

# --- snapshot location (deterministic: the CMUX app's own bundle id) -------------------------------
_SNAP_DIR = os.path.expanduser("~/Library/Application Support/cmux")


def _bundle_id(cmux_bin=None):
    """The CFBundleIdentifier of the cmux app behind `cmux_bin` (walk the binary up to the .app, read its
    Info.plist). '' when cmux is a bare PATH command (Linux) or the plist can't be read — the caller then
    falls back to a glob. Deterministic: it names EXACTLY this instance's snapshot (prod vs nightly), so
    the test-env's nightly reconcile never reads prod's snapshot and vice-versa."""
    p = os.path.realpath(cmux_bin or CMUX)
    while p and p != "/":
        if p.endswith(".app"):
            try:
                with open(os.path.join(p, "Contents", "Info.plist"), "rb") as f:
                    return str(plistlib.load(f).get("CFBundleIdentifier") or "")
            except Exception:
                return ""
        p = os.path.dirname(p)
    return ""


def session_snapshot_path(cmux_bin=None, snap_dir=None):
    """Path to THIS cmux instance's restore snapshot, or '' if none is found. Prefers the bundle-id-exact
    file (`session-<bundleId>.json`); falls back to the single non-'previous' `session-*.json` when the
    bundle id can't be resolved AND exactly one candidate exists (an unambiguous glob). Never guesses
    among multiple candidates — a wrong snapshot would misclassify surfaces."""
    d = snap_dir or _SNAP_DIR
    bid = _bundle_id(cmux_bin)
    if bid:
        p = os.path.join(d, f"session-{bid}.json")
        if os.path.exists(p):
            return p
    cands = [x for x in glob.glob(os.path.join(d, "session-*.json")) if "-previous" not in x]
    return cands[0] if len(cands) == 1 else ""


# --- snapshot parse (predictive: what cmux would resume / recreate) --------------------------------
_FLEET_SIG = re.compile(r"AGENT_ROLE=|AGENT_LABEL=|CMUX_FLEET_(?:STATE_DIR|TOML|ROOT|MARKETPLACE)")
_LABEL_RE = re.compile(r"AGENT_LABEL=([A-Za-z0-9._-]+)")
_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_RESUME_RE = re.compile(r"--resume\s+(" + _UUID_RE + r")")


def parse_snapshot(path):
    """{SURFACE_ID_UPPER: rec} for every terminal surface cmux persisted, where rec carries the fields the
    restore gate and the deterministic husk signal need:
        has_agent     terminal.agent != nil           (False => cmux recreates this as a bare-shell husk)
        was_running   terminal.wasAgentRunning          (None/True => cmux WOULD auto-resume; False => not)
        session       agent.sessionId (bare uuid)
        fleet_origin  the persisted scrollback carries the fleet launch signature (deterministic origin)
        label         harvested AGENT_LABEL from the scrollback (for the archive row)
        resume_id     harvested `--resume <id>` from the scrollback (last session, for revive)
        directory     the surface cwd
        title         the surface/panel title
    Returns {} on any read/parse failure (reconcile then degrades to flag-only — it never closes without
    the snapshot's agent=nil evidence). Pure except the file read."""
    try:
        import json
        with open(path) as f:
            d = json.load(f)
    except Exception:
        return {}
    out = {}
    for win in d.get("windows", []) or []:
        tm = win.get("tabManager") or {}
        for w in tm.get("workspaces", []) or []:
            for p in w.get("panels", []) or []:
                # KEY ON THE PANEL `id` — that is the surface id restore rehydrates verbatim
                # (Workspace.swift:1462 reuses `snapshot.id`), == CMUX_SURFACE_ID == the live-tree surface
                # == the fleet registry `surface`. `stableSurfaceId` is a SEPARATE id and never matches the
                # registry (verified live: 13/13 registry surfaces == panel id, 0/13 == stableSurfaceId).
                surf = (p.get("id") or p.get("stableSurfaceId") or "").upper()
                if not surf:
                    continue
                term = p.get("terminal") or {}
                agent = term.get("agent")
                scroll = term.get("scrollback") or ""
                lab = _LABEL_RE.search(scroll)
                rid = _RESUME_RE.search(scroll)
                sess = ""
                if isinstance(agent, dict):
                    sess = fs.bare_uuid(agent.get("sessionId") or "")
                out[surf] = {
                    "has_agent": agent is not None,
                    "was_running": term.get("wasAgentRunning"),
                    "session": sess,
                    "fleet_origin": bool(_FLEET_SIG.search(scroll)),
                    "label": lab.group(1) if lab else "",
                    "resume_id": fs.bare_uuid(rid.group(1)) if rid else "",
                    "directory": p.get("directory") or term.get("workingDirectory") or "",
                    "title": p.get("title") or "",
                }
    return out


def resume_set(snap):
    """The surfaces cmux WOULD auto-resume on the next relaunch: agent present AND wasAgentRunning != False
    (None counts as running — the `?? true` gate). This is the predictive orphan-to-be set."""
    return {s: r for s, r in snap.items() if r["has_agent"] and r["was_running"] is not False}


def husk_set(snap):
    """The surfaces cmux would recreate as a BARE-SHELL husk: no agent record. The deterministic close
    signal starts here and is narrowed by liveness + registry + fleet-origin in reconcile_restore."""
    return {s: r for s, r in snap.items() if not r["has_agent"]}


# --- the reconciliation ----------------------------------------------------------------------------
_last_run = {"ts": 0.0}
DEBOUNCE_S = 8.0        # collapse a relaunch's surface.created burst + a near-simultaneous daemon-start run


def _managed_workspaces():
    """Workspace uuids (UPPER) that host a live OR archived fleet member — a deterministic fleet-origin
    corroborator for a husk whose scrollback has aged past the launch line."""
    ws = set()
    for tbl in (fs.live_all(), fs.archive_all()):
        for e in tbl.values():
            w = (e.get("workspace") or "").upper()
            if w:
                ws.add(w)
    return ws


def _hook_fleet_origin(surface):
    """True iff a cmux hook-store record for `surface` carries a fleet-origin launchCommand (the argv
    holds the fleet plugin-dir / AGENT_LABEL env). A DETERMINISTIC fleet-origin corroborator that survives
    scrollback truncation: a SessionEnd-less agent death (the exact frozen-record class the fleet
    documents) leaves the record — and thus its launchCommand — behind, proving the surface was
    fleet-launched even when the persisted scrollback has scrolled past the boot line. (A clean SessionEnd
    consumes the record, so this won't corroborate a cleanly-exited long-session husk — those stay
    unprovable and are left alone.) Routed through resolve.records — the ONE module allowed to read the
    store — so the fleet's single-store-reader invariant holds."""
    import json as _json
    from . import resolve as rs
    return any(_FLEET_SIG.search(_json.dumps(r.get("launchCommand") or "")) for r in rs.records(surface))


def _classify(tree, snap, cmuxq=None):
    """Pure-ish classifier: walk the live tree's terminal surfaces and bucket each. `cmuxq` is injected
    for tests; liveness/registry come from state (fs). Returns {tracked, resume_orphan, husk,
    human_shell, unknown}. A `husk` row is the DETERMINISTIC close candidate; nothing here mutates."""
    from . import cli
    live = fs.live_all()
    reg_surf = {(e.get("surface") or "").upper(): lbl for lbl, e in live.items() if e.get("surface")}
    arch_by_session = {}
    for lbl, e in fs.archive_all().items():
        sid = fs.bare_uuid(e.get("last_session") or "")
        if sid:
            arch_by_session[sid] = lbl
    managed = _managed_workspaces()
    # surfaces per workspace (to decide close-surface vs close-workspace for a sole-occupant husk)
    ws_surf_count = {}
    for surf, ws, _title in cli._iter_terminal_surfaces(tree):
        if ws:
            ws_surf_count[ws.upper()] = ws_surf_count.get(ws.upper(), 0) + 1

    buckets = {"tracked": [], "resume_orphan": [], "husk": [], "human_shell": [], "unknown": []}
    for surf, ws, title in cli._iter_terminal_surfaces(tree):
        u = surf.upper()
        rec = snap.get(u, {})
        row = {"surface": surf, "workspace": ws, "title": title,
               "sole_in_ws": ws_surf_count.get((ws or "").upper(), 0) <= 1,
               "label": rec.get("label", ""), "resume_id": rec.get("resume_id", ""),
               "session": rec.get("session", ""), "cwd": rec.get("directory", "")}
        if u in reg_surf:
            row["label"] = reg_surf[u]
            buckets["tracked"].append(row)
            continue
        if fs.surface_has_live_agent(surf):
            # a LIVE agent the registry doesn't know — cmux resumed it, or it drifted off-registry.
            # NEVER close a live agent. Adoptable when its session matches an archived label.
            row["adopt_label"] = arch_by_session.get(fs.bare_uuid(rec.get("session") or ""), "")
            buckets["resume_orphan"].append(row)
            continue
        # no live agent, not in the registry. The deterministic husk close needs: snapshot says agent=nil
        # (a bare shell, not a dead-but-resumable agent) AND provably fleet-origin (never a human's shell).
        # Three deterministic origin signals (any one suffices): the persisted scrollback carries the fleet
        # boot signature; a hook record's launchCommand does (survives scrollback truncation); or the
        # surface sits in a fleet-managed workspace. A surface matching NONE is left alone (safety floor).
        fleet_origin = rec.get("fleet_origin") or _hook_fleet_origin(surf) or ((ws or "").upper() in managed)
        if not snap:
            buckets["unknown"].append(row)          # no snapshot => no agent=nil evidence => never close
        elif rec.get("has_agent"):
            # snapshot HAS an agent record but no live pid: a dead/exited agent cmux may still try to
            # resume. Flag (revive-able), do not sweep as a bare-shell husk.
            row["adopt_label"] = arch_by_session.get(fs.bare_uuid(rec.get("session") or ""), "")
            buckets["resume_orphan"].append(row)
        elif fleet_origin:
            buckets["husk"].append(row)             # DETERMINISTIC: agent=nil + no live agent + not reg + fleet-origin
        else:
            buckets["human_shell"].append(row)      # safety floor: not fleet-origin -> leave it alone
    return buckets


def _close_husk(row, log):
    """Archive-first close of ONE deterministic husk. Harvest label+last-session into an archive row (so a
    human can `fleet revive`), tombstone the surface (so the router's surface.closed handler treats it as
    an expected close, no spurious revive alert), then close — the WORKSPACE if the husk is its sole
    occupant (close-surface refuses the last surface), else just the surface. Verified against a fresh
    tree. Returns (closed: bool, note)."""
    from . import cli
    surf, ws = row["surface"], row.get("workspace") or ""
    label = row.get("label") or ""
    # archive-first: only when we harvested a label (else there is nothing meaningful to revive; still
    # close it, but as an anonymous husk).
    if label and not fs.archive_get(label):
        entry = {"role": "", "kind": "child", "tool": "claude", "cwd": row.get("cwd", ""),
                 "last_session": row.get("resume_id") or row.get("session") or "",
                 "archived_at": time.time(), "via": "reconcile-restore"}
        fs.archive_put(label, entry)
        fs.log_event("archived", label=label, via="reconcile-restore", surface=surf)
    fs.expected_close_put(surf)
    if row.get("sole_in_ws") and ws:
        out = cli.cmuxq("close-workspace", "--workspace", ws)
        verb = "close-workspace"
    else:
        out = cli.cmuxq("close-surface", "--surface", surf, *(("--workspace", ws) if ws else ()))
        verb = "close-surface"
    # verify against a FRESH tree: cmuxq swallows cmux's exit code, so a refusal reads like success.
    fresh = cli.cmuxq("tree", "--all", "--id-format", "both")
    still = surf.upper() in {s.upper() for s, _w, _t in cli._iter_terminal_surfaces(fresh)}
    if still:
        log(f"[reconcile] husk {surf[:8]} did NOT close ({verb}) — cmux said: {(out or '').strip()[:100]!r}")
        return False, "residue"
    log(f"[reconcile] closed husk {surf[:8]}"
        + (f" (archived {label}, revive-able)" if label else " (anonymous)"))
    return True, "closed"


def reconcile_restore(close=False, log=None, reason="manual", cmuxq=None, force=False):
    """The reconciliation pass. Read cmux's session snapshot + the live tree, classify every terminal
    surface, and — when `close` — archive-first CLOSE every DETERMINISTIC husk (never a live agent, never
    a human's shell, never the gated pane-content heuristic). Resume-orphans are flagged (adopt hint when
    their session matches an archived label). Returns a structured report. Debounced (`force` overrides)
    so a daemon-start run and a surface.created burst don't double-sweep."""
    log = log or (lambda m: None)
    now = time.time()
    if not force and (now - _last_run["ts"]) < DEBOUNCE_S:
        return {"skipped": "debounced", "since_s": round(now - _last_run["ts"], 1)}
    _last_run["ts"] = now
    from . import cli
    tree = (cmuxq or cli.cmuxq)("tree", "--all", "--id-format", "both")
    snap = parse_snapshot(session_snapshot_path())
    buckets = _classify(tree, snap)
    closed, residue = [], []
    if close:
        for row in buckets["husk"]:
            ok, _note = _close_husk(row, log)
            (closed if ok else residue).append(row["surface"])
    report = {
        "reason": reason, "snapshot": bool(snap),
        "resume_set": len(resume_set(snap)), "husk_set": len(husk_set(snap)),
        "tracked": len(buckets["tracked"]),
        "resume_orphans": [{"surface": r["surface"], "adopt": r.get("adopt_label", "")}
                           for r in buckets["resume_orphan"]],
        "husks": [r["surface"] for r in buckets["husk"]],
        "human_shells": len(buckets["human_shell"]),
        "unknown": len(buckets["unknown"]),
        "closed": closed, "residue": residue,
    }
    log(f"[reconcile] ({reason}) snapshot={'yes' if snap else 'NO'} "
        f"tracked={report['tracked']} resume-orphans={len(report['resume_orphans'])} "
        f"husks={len(report['husks'])} closed={len(closed)} human-shells={report['human_shells']}")
    return report


def cmd_reconcile_restore(argv):
    """fleet reconcile-restore [--close] [--json]   Reconcile the fleet registry against what cmux's crash-
    restore snapshot would resurrect. DRY-RUN by default (closes NOTHING): surveys husks + resume-orphans.
    `--close` archives-first and closes the DETERMINISTIC husks (snapshot agent=nil + no live agent + not
    in the registry + fleet-origin) — never a live agent, never a human's shell, never the gated pane-
    content heuristic. The daemon runs this on start + on the post-relaunch surface.created burst."""
    import argparse
    import json as _json
    ap = argparse.ArgumentParser(prog="fleet reconcile-restore", add_help=True,
                                 description="reconcile the registry against cmux's restore snapshot (dry-run; --close acts)")
    ap.add_argument("--close", action="store_true",
                    help="archive-first close the deterministic husks (default: dry-run, closes nothing)")
    ap.add_argument("--json", action="store_true", help="machine output")
    a = ap.parse_args(argv)
    rep = reconcile_restore(close=a.close, log=(lambda m: None) if a.json else print,
                            reason="cli", force=True)
    if a.json:
        print(_json.dumps(rep, indent=2))
        return 0
    if not rep.get("snapshot"):
        print("[reconcile] WARN: cmux restore snapshot not found — flag-only (no agent=nil evidence to close on).")
    print(f"\n[reconcile-restore] {'CLOSED' if a.close else 'DRY-RUN'} — "
          f"cmux would resume {rep['resume_set']} / recreate {rep['husk_set']} husks on next restart.")
    print(f"  tracked (in registry)   : {rep['tracked']}")
    print(f"  resume-orphans (flagged): {len(rep['resume_orphans'])}"
          + (f"  -> {[r['surface'][:8] + (':' + r['adopt'] if r['adopt'] else '') for r in rep['resume_orphans']]}"
             if rep['resume_orphans'] else ""))
    print(f"  deterministic husks     : {len(rep['husks'])}"
          + (f"  closed={len(rep['closed'])} residue={len(rep['residue'])}" if a.close else " (use --close to sweep)"))
    print(f"  human shells (left)     : {rep['human_shells']}")
    if rep.get("unknown"):
        print(f"  unknown (no snapshot)   : {rep['unknown']}")
    return 0
