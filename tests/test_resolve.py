"""resolve.py — step 1 of the v2 migration. Two jobs, per the ratified design (vault entity
"Agent management v2", sections 2 and 10) and Berg's build conditions:

1. SIDE-BY-SIDE ACCEPTANCE: the old predicates vs the resolver, field by field, across (a) a
   synthetic matrix of every seat shape the incident record produced and (b) the LIVE fixture corpus
   captured from the real fleet on 2026-07-10 (2 hook-store files, 45 session records, 11 registry
   rows, the real tree). Where a predicate's old body was replaced by delegation (pids, live_sid,
   ws-from-store), the OLD implementation is embedded here VERBATIM from main@7e6386a as a frozen
   reference, so the comparison stays honest even after step 3 deletes the originals.

2. THE ATTACHMENT AXIS (invariant I4): detached requires record-frozen AND evidence (behavioral skew
   or an env/pointer confirm), never record-frozen alone — an idle agent (both clocks frozen
   together) must NEVER read detached. Live-validated 2026-07-10 across 11 agents, zero false
   positives; these tests lock the exact shapes: berg-sandbox (detached, behavioral), resume-research
   (idle, NOT detached), usage-ops (env mismatch), the fresh-bind grace, and the pointer age gate.
"""
import json
import os
import time

import pytest

from cmux_fleet import cli

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "live_corpus_20260710")

# fs/rs/ff are rebound per test: test_features.py's _reset_pkg_modules pops and re-imports
# config/state/resolve/features, so a collection-time handle goes stale mid-suite and patches on it
# would never reach the code under test (the exact split-brain this branch fixed in the reset list).
fs = rs = ff = None


@pytest.fixture(autouse=True)
def _fresh_handles():
    global fs, rs, ff
    import cmux_fleet.state as _s
    import cmux_fleet.resolve as _r
    import cmux_fleet.features as _f
    fs, rs, ff = _s, _r, _f


# --- frozen reference implementations (verbatim from main@7e6386a; do not "fix" these) -------------
def ref_surface_pids(surface):
    # cli._surface_pids @7e6386a (case-sensitive surface match, as shipped)
    return {s.get("pid") for s in (fs.read_hook_store().get("sessions") or {}).values()
            if s.get("surfaceId") == surface and fs.pid_alive(s.get("pid"))}


def ref_live_bound_sid(surf):
    # cli._live_bound_sid @7e6386a
    best, best_ts = "", -1.0
    for s in (fs.read_hook_store().get("sessions") or {}).values():
        if (s.get("surfaceId") or "").upper() != (surf or "").upper():
            continue
        if not fs.pid_alive(s.get("pid")):
            continue
        ts = s.get("updatedAt") or 0
        if ts >= best_ts:
            best, best_ts = s.get("sessionId", ""), ts
    return best


def ref_ws_uuid_for_surface(surf):
    # cli.ws_uuid_for_surface @7e6386a (store-based, live-first)
    live_ws, live_ts, any_ws, any_ts = "", -1.0, "", -1.0
    for s in (fs.read_hook_store().get("sessions") or {}).values():
        if (s.get("surfaceId") or "").upper() != (surf or "").upper():
            continue
        ts = s.get("updatedAt") or 0
        if ts >= any_ts:
            any_ws, any_ts = s.get("workspaceId", ""), ts
        if fs.pid_alive(s.get("pid")) and ts >= live_ts:
            live_ws, live_ts = s.get("workspaceId", ""), ts
    return live_ws or any_ws


# --- fixture plumbing --------------------------------------------------------------------------------
def _store_of(sessions, active=None):
    return {"sessions": sessions, "activeSessionsBySurface": active or {}}


def _rec(surf, sid, pid, life, updated, ws="WS-000", transcript=""):
    return {"surfaceId": surf, "sessionId": sid, "pid": pid, "agentLifecycle": life,
            "updatedAt": updated, "workspaceId": ws, "transcriptPath": transcript}


@pytest.fixture
def live_corpus(monkeypatch):
    """The captured real-fleet corpus, wired through the same seams production uses."""
    stores = json.load(open(os.path.join(FIXTURES, "hookstores.json")))
    # rebuild the union exactly like state.read_hook_store does over the per-tool files
    sessions, active = {}, {}
    for name in sorted(stores):
        d = stores[name]
        for sid, s in (d.get("sessions") or {}).items():
            old = sessions.get(sid)
            if not old or (s.get("updatedAt") or 0) >= (old.get("updatedAt") or 0):
                sessions[sid] = s
        active.update(d.get("activeSessionsBySurface") or {})
    st = _store_of(sessions, active)
    monkeypatch.setattr(fs, "read_hook_store", lambda: st)
    tree = open(os.path.join(FIXTURES, "tree.txt")).read()
    fleet = json.load(open(os.path.join(FIXTURES, "fleet.json")))
    return st, tree, fleet


# --- 1a. side-by-side on the live corpus ---------------------------------------------------------------
def test_side_by_side_live_corpus_predicates(live_corpus):
    st, tree, fleet = live_corpus
    surfaces = [v.get("surface", "") for v in fleet.values()] + ["NOT-A-SURFACE"]
    for surf in surfaces:
        # delegated fields must equal the canonical predicates verbatim
        s = rs.seat(surf, st=st, ws_map={})           # empty ws_map -> exercises the store fallback
        assert s["present"] == fs.surface_has_live_agent(surf), surf
        assert s["lifecycle"] == (ff._freshest_session(st, surf).get("agentLifecycle", "")), surf
        assert s["pids"] == ref_surface_pids(surf), surf
        assert rs.live_sid(surf, st=st) == ref_live_bound_sid(surf), surf
        assert s["session"] == fs.bare_uuid(ref_live_bound_sid(surf)), surf
        assert rs._ws_from_store(surf, st=st) == ref_ws_uuid_for_surface(surf), surf
        assert rs.freshest(surf, st=st) == ff._freshest_session(st, surf), surf
        assert rs.bound_record(surf, st=st) == fs.resolve_bound_record(surf, st=st), surf
        assert rs.busy(surf) == fs.surface_busy(surf), surf


def test_side_by_side_live_corpus_tree_workspace(live_corpus, monkeypatch):
    st, tree, fleet = live_corpus
    monkeypatch.setattr(ff, "_cmux", lambda *a: tree)
    ff._WS_MAP.update({"at": 0.0, "map": {}})                  # defeat the memo
    m = ff._surface_ws_map(ttl=0)
    for label, v in fleet.items():
        surf = v.get("surface", "")
        # the resolver's tree-first workspace must agree with the OLD tree parser, per surface
        assert (rs.workspace(surf, st=st, ws_map=m) or "") == \
            (cli.surface_ws_from_tree(tree, surf) or ref_ws_uuid_for_surface(surf) or ""), label


# --- 1b. side-by-side on the synthetic seat matrix ------------------------------------------------------
def test_side_by_side_synthetic_matrix(monkeypatch):
    ME = os.getpid()                                   # a genuinely alive pid
    surfaces = {
        "S-EMPTY": [],
        "S-LIVE": [_rec("S-LIVE", "sid-live", ME, "idle", 1000.0)],
        "S-DEAD": [_rec("S-DEAD", "sid-dead", 999999, "idle", 1000.0)],
        # the stale-ghost shape: three corpses + one live, corpses fresher in dict order
        "S-MIXED": [_rec("S-MIXED", "sid-g1", 999998, "unknown", 3000.0),
                    _rec("S-MIXED", "sid-g2", 999997, "unknown", 2000.0),
                    _rec("S-MIXED", "sid-real", ME, "running", 1500.0)],
        # the SessionEnd-less brick: frozen 'running' on a dead pid
        "S-BRICK": [_rec("S-BRICK", "sid-brick", 999996, "running", 4000.0)],
    }
    sessions = {}
    for recs in surfaces.values():
        for i, r in enumerate(recs):
            sessions[f"{r['sessionId']}"] = r
    st = _store_of(sessions)
    monkeypatch.setattr(fs, "read_hook_store", lambda: st)
    for surf in surfaces:
        s = rs.seat(surf, st=st, ws_map={})
        assert s["present"] == fs.surface_has_live_agent(surf), surf
        assert s["pids"] == ref_surface_pids(surf), surf
        assert rs.live_sid(surf, st=st) == ref_live_bound_sid(surf), surf
        assert rs._ws_from_store(surf, st=st) == ref_ws_uuid_for_surface(surf), surf
    # semantics locks (not just equality): the ghost shape resolves to the LIVE record
    assert rs.live_sid("S-MIXED", st=st) == "sid-real"
    assert rs.pids("S-MIXED", st=st) == {ME}
    # the brick is not present (dead pid outranks the frozen string)
    assert rs.seat("S-BRICK", st=st, ws_map={})["present"] is False


# --- 2. the attachment axis (invariant I4) --------------------------------------------------------------
def _attach_env(monkeypatch, tmp_path, *, record_age, transcript_age, env_ws="", active="",
                pid=None, ws_tree="WS-TREE"):
    """One seat with controllable clocks. Returns (surface, store, ws_map, now)."""
    now = time.time()
    pid = os.getpid() if pid is None else pid
    tpath = ""
    if transcript_age is not None:
        p = tmp_path / "t.jsonl"
        p.write_text("{}")
        os.utime(p, (now - transcript_age, now - transcript_age))
        tpath = str(p)
    rec = _rec("S-ATT", "sid-att", pid, "unknown", now - record_age, transcript=tpath)
    st = _store_of({"sid-att": rec}, {"S-ATT": {"sessionId": active or "sid-att"}})
    monkeypatch.setattr(fs, "read_hook_store", lambda: st)
    monkeypatch.setattr(rs, "_env_workspace", lambda _pid: env_ws)
    return "S-ATT", st, {"S-ATT": ws_tree}, now


def test_detached_behavioral_bergsandbox_shape(monkeypatch, tmp_path):
    # record frozen 3.5h, transcript minutes old -> DETACHED on the behavioral conjunction
    surf, st, ws_map, now = _attach_env(monkeypatch, tmp_path, record_age=12800, transcript_age=30)
    att = rs.attachment(surf, st=st, ws_map=ws_map, now=now)
    assert att["attached"] is False
    assert any(r.startswith("behavioral") for r in att["reasons"])


def test_idle_agent_never_reads_detached_resume_research_shape(monkeypatch, tmp_path):
    # BOTH clocks frozen by the same 4.3h -> idle, attached=True (Berg's condition 4)
    surf, st, ws_map, now = _attach_env(monkeypatch, tmp_path, record_age=15600, transcript_age=15600)
    att = rs.attachment(surf, st=st, ws_map=ws_map, now=now)
    assert att["attached"] is True
    assert att["reasons"] == []


def test_idle_with_env_mismatch_is_detached_usage_ops_shape(monkeypatch, tmp_path):
    # idle clocks, but the process env names a different workspace than the tree: the deterministic
    # confirm is REQUIRED for an idle agent and suffices (Gap A is physically conclusive)
    surf, st, ws_map, now = _attach_env(monkeypatch, tmp_path, record_age=5000, transcript_age=5000,
                                        env_ws="WS-OLD")
    att = rs.attachment(surf, st=st, ws_map=ws_map, now=now)
    assert att["attached"] is False
    assert any(r.startswith("env") for r in att["reasons"])


def test_stale_pointer_alone_never_detaches(monkeypatch, tmp_path):
    # The pointer is written by the STOP hook (measured: absent until the first completed turn), so a
    # mismatch is a lagging symptom, not proof — treating it as proof false-positives every fresh seat
    # (a probe sat pointer-less for 105s post-launch) and false-negatives every agent that turned once
    # before dying. It is exposed only as the ever_heard diagnostic.
    surf, st, ws_map, now = _attach_env(monkeypatch, tmp_path, record_age=300, transcript_age=300,
                                        active="sid-other")
    att = rs.attachment(surf, st=st, ws_map=ws_map, now=now)
    assert att["attached"] is True and att["reasons"] == []
    assert att["ever_heard"] is False                     # never heard from -> diagnostic, not detached
    surf, st, ws_map, now = _attach_env(monkeypatch, tmp_path, record_age=300, transcript_age=300)
    assert rs.attachment(surf, st=st, ws_map=ws_map, now=now)["ever_heard"] is True


def test_absent_agent_has_no_attachment(monkeypatch, tmp_path):
    surf, st, ws_map, now = _attach_env(monkeypatch, tmp_path, record_age=9999, transcript_age=0,
                                        pid=999995)
    assert rs.attachment(surf, st=st, ws_map=ws_map, now=now)["attached"] is None


def test_missing_transcript_falls_back_to_deterministic_only(monkeypatch, tmp_path):
    # unknowable transcript: behavioral path silent; clean env -> attached
    surf, st, ws_map, now = _attach_env(monkeypatch, tmp_path, record_age=12800, transcript_age=None)
    att = rs.attachment(surf, st=st, ws_map=ws_map, now=now)
    assert att["attached"] is True and att["transcript_age_s"] is None


# --- 4. never-orphan: the teardown set is store UNION ps-env (cmux-advisor finding 2) -----------------
PS_FIXTURE = """  PID COMMAND
  501 /sbin/launchd HOME=/ SHELL=/bin/zsh
98942 claude --resume abc CMUX_SURFACE_ID=S-ORPH CMUX_WORKSPACE_ID=WS-1 TERM=xterm
77001 claude CMUX_SURFACE_ID=S-OTHER TERM=xterm
"""


def test_pids_ps_extracts_env_matching_live_pids(monkeypatch):
    monkeypatch.setattr(fs, "pid_alive", lambda pid: pid == 98942)
    assert rs.pids_ps("S-ORPH", ps_out=PS_FIXTURE) == {98942}
    assert rs.pids_ps("S-OTHER", ps_out=PS_FIXTURE) == set()   # 77001 not alive
    assert rs.pids_ps("", ps_out=PS_FIXTURE) == set()


def test_kill_targets_unions_store_and_ps(monkeypatch):
    # store knows one live pid; ps-env knows another whose record SessionEnd already reaped
    st = _store_of({"sid-a": _rec("S-KT", "sid-a", 11111, "idle", 1000.0)})
    monkeypatch.setattr(fs, "read_hook_store", lambda: st)
    monkeypatch.setattr(fs, "pid_alive", lambda pid: pid in (11111, 22222))
    monkeypatch.setattr(rs, "pids_ps", lambda surface, ps_out=None: {22222})
    assert rs.kill_targets("S-KT", st=st) == {11111, 22222}


def test_stop_for_close_refuses_while_a_signalled_pid_survives_store_reap(monkeypatch):
    # SessionEnd reaps the record BEFORE the process dies: the store empties instantly, the pid lives.
    # The old wait ('while _surface_pids(surf)') read that as safe-to-close; the fixed wait observes
    # the SIGNALLED pids and refuses. Live corroboration: rm ptrprobe printed 'removed' with pid 98942
    # still alive.
    monkeypatch.setattr(cli, "_surface_pids", lambda surf: set())          # record already reaped
    monkeypatch.setattr(cli, "_agent_pid_check", lambda pid, tool: True)
    monkeypatch.setattr(rs, "pids_ps", lambda surface, ps_out=None: {98942})  # ps still sees it
    monkeypatch.setattr(fs, "pid_alive", lambda pid: True)                  # ...and it stays alive
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cli, "_STOP_WAIT_S", 0.01)
    ok, note = cli._stop_agent_for_close("S-ORPH", "claude", "orphanprobe", "rm")
    assert ok is False and "98942" in note


def test_stop_for_close_ok_when_signalled_pid_actually_dies(monkeypatch):
    alive = {98942: True}
    monkeypatch.setattr(cli, "_surface_pids", lambda surf: set())
    monkeypatch.setattr(cli, "_agent_pid_check", lambda pid, tool: True)
    monkeypatch.setattr(rs, "pids_ps", lambda surface, ps_out=None: {98942} if alive[98942] else set())
    monkeypatch.setattr(fs, "pid_alive", lambda pid: alive.get(pid, False))

    def _kill(pid, sig):
        alive[pid] = False                                  # SIGINT actually lands
    monkeypatch.setattr(cli.os, "kill", _kill)
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cli, "_STOP_WAIT_S", 0.01)
    ok, note = cli._stop_agent_for_close("S-ORPH", "claude", "orphanprobe", "rm")
    assert ok is True


# --- 3. the doctor 'detached' condition ------------------------------------------------------------------
def _doctor_world(monkeypatch, tmp_path, *, record_age, transcript_age):
    from cmux_fleet import router
    # router binds fs/rs at ITS import; re-point them at the current modules (same consistency dance
    # as test_fleet_doctor._sync) so the patches below reach the sweep after any module reset.
    monkeypatch.setattr(router, "fs", fs)
    monkeypatch.setattr(router, "rs", rs)
    now = time.time()
    tpath = tmp_path / "t.jsonl"
    tpath.write_text("{}")
    os.utime(tpath, (now - transcript_age, now - transcript_age))
    rec = _rec("S-CH", "sid-ch", os.getpid(), "unknown", now - record_age, transcript=str(tpath))
    st = _store_of({"sid-ch": rec}, {"S-CH": {"sessionId": "sid-ch"}})
    monkeypatch.setattr(fs, "read_hook_store", lambda: st)
    monkeypatch.setattr(rs, "_env_workspace", lambda _pid: "")
    monkeypatch.setattr(rs, "surface_ws_map", lambda ttl=2.0: {})
    monkeypatch.setattr(fs, "wake_if_idle", lambda *a, **k: False)
    router._doctor_fired.clear()
    fs.live_put("parent-c", {"role": "lead", "kind": "conductor", "surface": "S-PAR", "session": "x"})
    fs.live_put("child-a", {"role": "worker", "kind": "child", "parent": "parent-c",
                            "surface": "S-CH", "session": "claude-sid-ch", "tool": "claude"})
    return router, now


def test_doctor_emits_detached_for_frozen_but_working_child(monkeypatch, tmp_path):
    router, now = _doctor_world(monkeypatch, tmp_path, record_age=12800, transcript_age=10)
    fired = router.fleet_doctor_sweep(now=now)
    assert fired == 1
    rows = [r for r in fs.inbox_read() if r.get("kind") == "doctor"]
    assert len(rows) == 1 and rows[0]["reason"] == "detached" and rows[0]["label"] == "child-a"
    assert rows[0]["to"] == "S-PAR"
    # dedup: a second sweep does not re-fire the same occurrence
    assert router.fleet_doctor_sweep(now=now) == 0


def test_doctor_never_flags_an_idle_child_detached(monkeypatch, tmp_path):
    router, now = _doctor_world(monkeypatch, tmp_path, record_age=12800, transcript_age=12800)
    assert router.fleet_doctor_sweep(now=now) == 0
    assert [r for r in fs.inbox_read() if r.get("kind") == "doctor"] == []
