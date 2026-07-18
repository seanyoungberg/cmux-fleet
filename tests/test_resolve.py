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


# --- 1c. the by-session / by-fragment primitives (5b-2 finish: routed off the cli/router/helpers raw reads) ---
def test_by_session_primitives_match_the_old_inline_reads():
    """record_by_session / active_entry / session_transcript are exact ports of the hand-rolled reads that
    lived in router._rec_by_session, cli._live_session_for, and helpers.cmd_child_digest. Pin the semantics
    the ports must preserve: exact by-session record match, the FULL active-pointer entry (not just its sid,
    case-insensitive), and first-transcript by session-id fragment gated on a recorded transcriptPath."""
    st = {
        "activeSessionsBySurface": {"S-UP": {"sessionId": "claude-abc", "extra": "keep-me"}},
        "sessions": {
            "r1": {"sessionId": "claude-abc", "surfaceId": "S-UP", "pid": 111, "transcriptPath": "/t/abc.jsonl"},
            "r2": {"sessionId": "codex-def", "surfaceId": "S-DN", "pid": 222, "transcriptPath": "/t/def.jsonl"},
            "r3": {"sessionId": "claude-ghi", "surfaceId": "S-DN", "pid": 333},   # no transcriptPath
        },
    }
    # record_by_session: exact sessionId match; {} when absent
    assert rs.record_by_session("codex-def", st=st)["surfaceId"] == "S-DN"
    assert rs.record_by_session("nope", st=st) == {}
    # active_entry: the FULL pointer entry (not just the sid), case-insensitive surface, {} when absent
    assert rs.active_entry("S-UP", st=st) == {"sessionId": "claude-abc", "extra": "keep-me"}
    assert rs.active_entry("s-up", st=st).get("extra") == "keep-me"     # case-insensitive
    assert rs.active_entry("S-NONE", st=st) == {}
    # session_transcript: first record whose sessionId CONTAINS the fragment AND has a transcriptPath
    assert rs.session_transcript("abc", st=st) == "/t/abc.jsonl"
    assert rs.session_transcript("def", st=st) == "/t/def.jsonl"
    assert rs.session_transcript("ghi", st=st) == ""                   # id matches but no transcriptPath -> skip
    assert rs.session_transcript("", st=st) == ""                     # empty fragment matches nothing
    assert rs.session_transcript("zzz", st=st) == ""


# --- 2. the attachment axis (invariant I4) --------------------------------------------------------------
def _turn_transcript(path, now, age_s):
    """Write a REAL-SHAPED transcript whose last TURN is `age_s` old, plus fresh bookkeeping lines.

    The activity signal is the last assistant/user turn, NOT the file mtime: Claude Code appends
    `system` / `permission-mode` lines to an idle agent's transcript long after its last turn. This
    fixture reproduces that text shape deliberately (judgment 23: un-stubbing a seam is not enough,
    the fixture must reproduce the real shape of the boundary), so an mtime-based rule cannot pass.
    """
    import calendar as _cal
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - age_s)) + ".000Z"
    path.write_text(json.dumps({"type": "assistant", "timestamp": ts}) + "\n"
                    + json.dumps({"type": "system", "timestamp": ts}) + "\n")
    os.utime(path, (now, now))          # mtime deliberately FRESH — only the turn ts should matter
    return str(path)


def _attach_env(monkeypatch, tmp_path, *, record_age, transcript_age, env_ws="", active="",
                pid=None, ws_tree="WS-TREE"):
    """One seat with controllable clocks. Returns (surface, store, ws_map, now)."""
    now = time.time()
    pid = os.getpid() if pid is None else pid
    tpath = ""
    if transcript_age is not None:
        tpath = _turn_transcript(tmp_path / "t.jsonl", now, transcript_age)
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
PS_FIXTURE = """  PID   TT  STAT      TIME COMMAND
  501   ??  Ss     0:12.00 /sbin/launchd HOME=/ SHELL=/bin/zsh
98942 s003  S+     0:01.38 claude --resume abc CMUX_SURFACE_ID=S-ORPH CMUX_WORKSPACE_ID=WS-1 CMUX_CLAUDE_PID=98942 TERM=xterm
98999   ??  S      0:00.20 claude -p summarize CMUX_SURFACE_ID=S-ORPH CMUX_CLAUDE_PID=98942 TERM=xterm
77001 s004  S+     0:00.10 claude CMUX_SURFACE_ID=S-OTHER CMUX_CLAUDE_PID=77001 TERM=xterm
66001 s018  S+     0:01.38 codex --dangerously-bypass-approvals-and-sandbox CMUX_SURFACE_ID=S-CDX TERM=xterm
"""


def test_pids_ps_returns_only_the_seat_agent(monkeypatch):
    monkeypatch.setattr(fs, "pid_alive", lambda pid: pid in (98942, 98999, 66001))
    # the seat agent: CMUX_CLAUDE_PID equals its own pid
    assert rs.pids_ps("S-ORPH", ps_out=PS_FIXTURE) == {98942}
    # the memsearch `claude -p` summarizer inherits the PARENT's CMUX_CLAUDE_PID: excluded exactly
    # (this was the accepted residual under the basename rule; the self-referential rule closes it)
    assert 98999 not in rs.pids_ps("S-ORPH", ps_out=PS_FIXTURE)
    assert rs.pids_ps("S-OTHER", ps_out=PS_FIXTURE) == set()   # 77001 not alive
    # codex has no wrapper env: argv0-basename fallback — argv0 is FIELD 5 of a real ps axeww line
    # (PID TT STAT TIME COMMAND); parsing field 2 returns the TTY ('s018') and made this set
    # permanently empty for codex (the shipped bug this real-shaped fixture exists to catch)
    assert rs.pids_ps("S-CDX", ps_out=PS_FIXTURE, tool="codex") == {66001}
    assert rs.pids_ps("S-CDX", ps_out=PS_FIXTURE, tool="claude") == set()
    assert rs.pids_ps("", ps_out=PS_FIXTURE) == set()


def test_kill_targets_unions_store_and_ps(monkeypatch):
    # store knows one live pid; ps-env knows another whose record SessionEnd already reaped
    st = _store_of({"sid-a": _rec("S-KT", "sid-a", 11111, "idle", 1000.0)})
    monkeypatch.setattr(fs, "read_hook_store", lambda: st)
    monkeypatch.setattr(fs, "pid_alive", lambda pid: pid in (11111, 22222))
    monkeypatch.setattr(rs, "pids_ps", lambda surface, ps_out=None, tool="claude": {22222})
    assert rs.kill_targets("S-KT", st=st) == {11111, 22222}


def test_stop_for_close_refuses_while_a_signalled_pid_survives_store_reap(monkeypatch):
    # SessionEnd reaps the record BEFORE the process dies: the store empties instantly, the pid lives.
    # The old wait ('while _surface_pids(surf)') read that as safe-to-close; the fixed wait observes
    # the SIGNALLED pids and refuses. Live corroboration: rm ptrprobe printed 'removed' with pid 98942
    # still alive.
    monkeypatch.setattr(cli, "_surface_pids", lambda surf: set())          # record already reaped
    monkeypatch.setattr(cli, "_agent_pid_check", lambda pid, tool: True)
    monkeypatch.setattr(rs, "pids_ps", lambda surface, ps_out=None, tool="claude": {98942})  # ps still sees it
    monkeypatch.setattr(fs, "pid_alive", lambda pid: True)                  # ...and it stays alive
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cli, "_STOP_WAIT_S", 0.01)
    ok, note = cli._stop_agent_for_close("S-ORPH", "claude", "orphanprobe", "rm")
    assert ok is False and "98942" in note


def test_identity_rule_is_argv0_basename_not_substring():
    # the substring rule passed marketplace hook scripts ('claude' in the PATH) — the class that let
    # pids_ps feed foreign processes into the kill path (cmux-advisor blocker)
    assert cli._identifies_as("/Users/b/.local/bin/claude --resume abc", "claude") is True
    assert cli._identifies_as("/Users/b/.cmux/cmux-cli-shims/S123/claude", "claude") is True
    assert cli._identifies_as("claude -p", "claude") is True            # accepted residual: ephemeral
    assert cli._identifies_as("codex resume abc", "codex") is True
    assert cli._identifies_as(
        "python3 /Users/b/.claude/plugins/claude-marketplace/memsearch/hooks/stop.py", "claude") is False
    assert cli._identifies_as(
        "/x/.worktrees/resolve-v2/.venv/bin/python -m cmux_fleet.daemon", "claude") is False
    assert cli._identifies_as("node /Users/b/lavish-axi/server.js", "claude") is False
    assert cli._identifies_as("", "claude") is False


def test_conductor_close_never_blocked_by_foreign_env_carriers(monkeypatch):
    """The conductor-wedge regression (cmux-advisor blocker): a conductor's surface env is inherited
    by never-dying daemons/routers/servers. With the agent counterfactually dead, the close must
    proceed — a store pid blocks as-is, a ps-env pid blocks ONLY if it identifies as the tool. This
    test deliberately un-stubs the ps sweep (conftest blanks it, which is exactly why the wedge
    shipped) and runs the REAL pids_ps parse over a mixed process table."""
    AGENT, DAEMON, ROUTER = 55001, 55002, 55003
    # daemon/router carry the surface env AND a CMUX_CLAUDE_PID naming some OTHER (even dead prior)
    # agent pid — the live shape on all three real conductors. Only the agent is self-referential.
    ps_table = (f"  PID   TT  STAT      TIME COMMAND\n"
                f"{AGENT} s001  S+     0:05.00 /Users/b/.local/bin/claude --resume abc CMUX_SURFACE_ID=S-COND CMUX_CLAUDE_PID={AGENT} X=1\n"
                f"{DAEMON}   ??  Ss     1:00.00 /x/.venv/bin/python -m cmux_fleet.daemon CMUX_SURFACE_ID=S-COND CMUX_CLAUDE_PID={AGENT}\n"
                f"{ROUTER}   ??  Ss     1:00.00 /x/.venv/bin/python -m cmux_fleet.router --live CMUX_SURFACE_ID=S-COND CMUX_CLAUDE_PID=99999\n")
    monkeypatch.setattr(rs, "_ps_axeww", lambda: ps_table)              # real parse, mixed table
    alive = {AGENT: False, DAEMON: True, ROUTER: True}                   # agent counterfactually DEAD
    monkeypatch.setattr(fs, "pid_alive", lambda pid: alive.get(pid, False))
    identities = {AGENT: True, DAEMON: False, ROUTER: False}             # store-loop ps identity per pid
    monkeypatch.setattr(cli, "_agent_pid_check", lambda pid, tool: identities.get(pid, False))
    monkeypatch.setattr(cli, "_surface_pids", lambda surf: set())        # record already reaped
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cli, "_STOP_WAIT_S", 0.01)
    ok, note = cli._stop_agent_for_close("S-COND", "claude", "conductor-x", "rm")
    assert ok is True, note                                              # the daemons never block the close
    assert DAEMON not in killed and ROUTER not in killed                 # and are never signalled
    # ...and with the agent ALIVE, it (alone) still blocks:
    alive[AGENT] = True
    ok, note = cli._stop_agent_for_close("S-COND", "claude", "conductor-x", "rm")
    assert ok is False and str(AGENT) in note
    assert DAEMON not in killed and ROUTER not in killed


def test_stop_for_close_ok_when_signalled_pid_actually_dies(monkeypatch):
    alive = {98942: True}
    monkeypatch.setattr(cli, "_surface_pids", lambda surf: set())
    monkeypatch.setattr(cli, "_agent_pid_check", lambda pid, tool: True)
    monkeypatch.setattr(rs, "pids_ps", lambda surface, ps_out=None, tool="claude": {98942} if alive[98942] else set())
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
    _turn_transcript(tpath, now, transcript_age)
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


# --- the activity signal is the last TURN, never the file mtime -------------------------------------
def test_idle_agent_with_bookkeeping_writes_is_attached_not_detached(tmp_path, monkeypatch):
    """The step-1 detector used the transcript's MTIME. Claude Code appends `system` /
    `permission-mode` / `bridge-session` lines to an idle agent's transcript long after its last turn,
    so mtime advanced while the agent sat at the prompt. On the live fleet that read usage-ops
    (record 177.9m, mtime 59.0m) and sidebar-build (record 47.4m, mtime 13.4m) as DETACHED while both
    were merely idle. Only the last real TURN distinguishes idle from detached."""
    import json as _json, os as _os, time as _time
    from cmux_fleet import resolve as rs

    now = _time.time()
    def iso(age_s):
        return _time.strftime("%Y-%m-%dT%H:%M:%S", _time.gmtime(now - age_s)) + ".000Z"

    # an IDLE agent: last real turn 60 min ago, bookkeeping appended 1 min ago (mtime is fresh)
    idle = tmp_path / "idle.jsonl"
    idle.write_text(
        _json.dumps({"type": "assistant", "timestamp": iso(3600)}) + "\n" +
        _json.dumps({"type": "system", "timestamp": iso(60)}) + "\n" +
        _json.dumps({"type": "permission-mode"}) + "\n")
    _os.utime(idle, (now - 60, now - 60))

    # a DETACHED agent: a real turn 1 min ago, but cmux's record froze 60 min ago
    det = tmp_path / "det.jsonl"
    det.write_text(_json.dumps({"type": "assistant", "timestamp": iso(60)}) + "\n")

    idle_age = rs._transcript_age({"transcriptPath": str(idle)}, now)
    det_age = rs._transcript_age({"transcriptPath": str(det)}, now)

    assert 3500 < idle_age < 3700, f"idle agent's activity age must be its last TURN (~3600s), got {idle_age}"
    assert det_age < 120, f"detached agent's last turn is recent, got {det_age}"

    record_age = 3600.0
    assert (record_age - idle_age) <= rs.ATTACH_SKEW_S      # idle -> attached
    assert (record_age - det_age) > rs.ATTACH_SKEW_S        # working, unheard -> detached


def test_transcript_age_is_none_when_no_turn_found(tmp_path):
    """Fail safe: an unparseable / turn-less transcript must abstain, never manufacture a detach."""
    from cmux_fleet import resolve as rs
    p = tmp_path / "junk.jsonl"
    p.write_text('{"type":"system"}\nnot json at all\n')
    assert rs._transcript_age({"transcriptPath": str(p)}, __import__("time").time()) is None
    assert rs._transcript_age({"transcriptPath": "/nope/missing.jsonl"}, 0) is None


# --- the I4 axis must reach the STATUS VOCABULARY, not just --json ----------------------------------
def test_detached_or_names_the_state_and_never_masks_a_gate():
    """STATE_STYLE carried a violet `detached` glyph and resolve.attachment() computed the axis, but
    nothing ever ASSIGNED the state: a detached agent rendered as `ready` in `fleet ls` and `fleet
    vitals`, with its env-mismatch reason sitting unused one field away. Live-proven 2026-07-10 on a
    moved agent and on berg-sandbox. `detached_or` is the one place the axis meets the vocabulary."""
    from cmux_fleet import features as ff

    # time-based readings of a frozen record are lies -> say detached
    for masked in ("working", "ready", "idle", "done", "stale"):
        assert ff.detached_or(masked, False) == "detached", masked

    # actionable, live-Feed / seat states are NEVER masked
    for preserved in ("needs-input", "review", "error", "pending"):
        assert ff.detached_or(preserved, False) == preserved, preserved

    # attached, or unjudgeable (no live agent), changes nothing
    for att in (True, None):
        assert ff.detached_or("ready", att) == "ready"
        assert ff.detached_or("working", att) == "working"

    # the state must be renderable: it needs a glyph and a rank, or vitals sorts it to the bottom
    assert "detached" in ff.STATE_STYLE
    assert ff.STATE_STYLE["detached"][2] < ff.STATE_STYLE["ready"][2]   # ranks ahead of ready

# ================= the workspace-teardown primitives (Berg's remove-the-workspace ruling) ===========
# `occupants` is kill_targets() for a surface whose TOOL IS UNKNOWN — a bystander the fleet is about to
# close as collateral when it closes a workspace. `workspace_surfaces` answers "who else lives here" from
# the TREE, because the registry's `workspace` field goes stale the moment cmux re-homes a surface.

def test_occupants_finds_a_live_agent_of_either_tool_on_an_unlabelled_surface():
    """The real-shape ps table: a claude agent is self-referential (CMUX_CLAUDE_PID == own pid); a codex
    agent has no wrapper and is identified by argv0 in FIELD 5 of `ps axeww`; a daemon carrying the same
    surface env is neither. A workspace close must be blocked by the first two and not by the third."""
    from cmux_fleet import resolve as rs
    CL, CX, DAEMON = 61001, 61002, 61003
    ps_table = (f"  PID   TT  STAT      TIME COMMAND\n"
                f"{CL} s001  S+     0:05.00 /Users/b/.local/bin/claude --resume abc "
                f"CMUX_SURFACE_ID=S-CL CMUX_CLAUDE_PID={CL}\n"
                f"{CX} s002  S+     0:02.00 /opt/homebrew/bin/codex -a never CMUX_SURFACE_ID=S-CX\n"
                f"{DAEMON}   ??  Ss     1:00.00 /x/.venv/bin/python -m cmux_fleet.daemon "
                f"CMUX_SURFACE_ID=S-CL CMUX_CLAUDE_PID={CL}\n")
    import cmux_fleet.state as _fs
    old_alive, old_store = _fs.pid_alive, _fs.read_hook_store
    _fs.pid_alive = lambda pid: pid in (CL, CX, DAEMON)
    _fs.read_hook_store = lambda: {"sessions": {}, "activeSessionsBySurface": {}}
    try:
        assert rs.occupants("S-CL", ps_out=ps_table) == {CL}      # the agent blocks; its daemon does not
        assert rs.occupants("S-CX", ps_out=ps_table) == {CX}      # codex found without naming its tool
        assert rs.occupants("S-EMPTY", ps_out=ps_table) == set()  # a bare shell / view pane is free
    finally:
        _fs.pid_alive, _fs.read_hook_store = old_alive, old_store


def test_workspace_surfaces_reads_membership_from_the_tree():
    from cmux_fleet import cli, resolve as rs
    tree = ('window window:1 9FBB70C6-7B17-4DA5-B54D-8FF3641D24E2 [current] ◀ active\n'
            '├── workspace workspace:6 B1656D4C-22E7-438F-9797-B62A92B7AF81 "resume-research"\n'
            '│   └── pane pane:8 CD82F4D9-4D4C-4177-A985-51950E26B88A [focused]\n'
            '│       ├── surface surface:25 2694CB4C-706E-45A7-BB64-EA572AA9421C [terminal] "✳ agent" [selected]\n'
            '│       └── surface surface:24 5CD821D0-5098-4C77-941B-32352BD866FD [markdown] "notes.md"\n'
            '├── workspace workspace:23 022AF32C-AF42-4603-B610-E4DC16F40717 "graph-view"\n'
            '│   └── pane pane:29 A2146F3B-4772-42F3-B31B-033EB72CB239 [focused]\n'
            '│       └── surface surface:83 4E496E4B-A010-482B-BB04-A7DF9929EBCD [terminal] "✳ solo" [selected]\n')
    m = cli.surface_ws_map_from_tree(tree)
    assert rs.workspace_surfaces("B1656D4C-22E7-438F-9797-B62A92B7AF81", ws_map=m) == \
        ["2694CB4C-706E-45A7-BB64-EA572AA9421C", "5CD821D0-5098-4C77-941B-32352BD866FD"]
    assert len(rs.workspace_surfaces("022AF32C-AF42-4603-B610-E4DC16F40717", ws_map=m)) == 1
    assert rs.workspace_surfaces("", ws_map=m) == []
    # every surface KIND is seen: a workspace close takes the markdown viewer with it, so a survey that
    # only counted terminals would call a two-surface workspace "empty apart from the agent".
    kinds = sorted(k for _s, _w, k, _t in cli._iter_tree_surfaces(tree))
    assert kinds == ["markdown", "terminal", "terminal"]
    assert [s for s, _w, _t in cli._iter_terminal_surfaces(tree)] == \
        ["2694CB4C-706E-45A7-BB64-EA572AA9421C", "4E496E4B-A010-482B-BB04-A7DF9929EBCD"]
