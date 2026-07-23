"""Recycle quiet-gate pair (cmux-advisor v0.15.4 batch; berg-sandbox bug report).

Two defects, both from the graph-view incident (a lavish long-poll held the gate 180s to an ABORT while the
agent was done-idle at its prompt, and the calling conductor only ever saw 'SCHEDULED'):

  DEFECT 1 — the quiet-gate conflated the TUI's own turn state with a background SHELL. A background poll
  keeps the session PID alive for hours, so the live-pid check alone could never clear. The gate now honors
  the transcript's turn-close signal (`turn_ended`): a done-at-prompt seat is quiet even while cmux's
  lifecycle lags at 'running' and a poll runs (child processes die with the respawn — Berg's ruling).

  DEFECT 2 — a detached recycle's terminal result never reached the caller (it just logged). The invoker is
  recorded at schedule time and notified (DONE and every ABORT) completion-style, on their own surface.

Pure unit tests: lifecycle / pids / transcript / time.sleep / wake are monkeypatched, no cmux.
"""
from cmux_fleet import cli as fleet           # noqa: E402


# ============================ DEFECT 1 — turn-ended clears a poll-armed 'running' seat ==============
def _gate_env(rs, monkeypatch, *, lc, live_pid, turn_ended, draft):
    # fetch `features` FRESH at test time (not a top-level bind): test_features pops it from sys.modules,
    # so a collection-time import would patch a stale object `_quiet_gate`'s own `from . import features`
    # no longer resolves to (same reason conftest fetches rs/fs fresh).
    import cmux_fleet.features as ff
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(rs, "lifecycle", lambda surf: lc)
    monkeypatch.setattr(rs, "surface_has_live_pid", lambda surf: live_pid)   # has_live_pid delegates here
    monkeypatch.setattr(rs, "freshest", lambda surf, st=None: {"transcriptPath": "/t.jsonl"})
    monkeypatch.setattr(ff, "turn_ended", lambda path: turn_ended)
    monkeypatch.setattr(fleet, "_input_draft_nonempty", lambda surf: draft)


def test_quiet_gate_running_but_turn_ended_is_quiet(rs, monkeypatch):
    # THE fix: lifecycle lags at 'running' and a background shell keeps the PID alive (live_pid=True), but
    # the transcript proves the turn CLOSED -> done-at-prompt, QUIET. Before this the gate burned 180s to an
    # ABORT on every poll-armed review-driver.
    _gate_env(rs, monkeypatch, lc="running", live_pid=True, turn_ended=True, draft=False)
    assert fleet._quiet_gate("S", 0.05, force=False) is True


def test_quiet_gate_running_turn_open_still_blocks(rs, monkeypatch):
    # UNCHANGED no-half-kill guard: a genuinely mid-turn agent (running + live pid + turn NOT closed) still
    # blocks to the timeout. turn_ended fails CLOSED, so this is the conservative default.
    _gate_env(rs, monkeypatch, lc="running", live_pid=True, turn_ended=False, draft=False)
    assert fleet._quiet_gate("S", 0.05, force=False) is False


def test_quiet_gate_turn_ended_but_draft_still_blocks(rs, monkeypatch):
    # a closed turn is quiet, but a human DRAFT in the box still blocks — never respawn over someone's typing.
    _gate_env(rs, monkeypatch, lc="running", live_pid=True, turn_ended=True, draft=True)
    assert fleet._quiet_gate("S", 0.05, force=False) is False


def test_quiet_gate_dead_pid_ghost_still_quiet(rs, monkeypatch):
    # regression: the frozen-'running'-dead-pid ghost path is independent of turn_ended and still clears.
    _gate_env(rs, monkeypatch, lc="running", live_pid=False, turn_ended=False, draft=False)
    assert fleet._quiet_gate("S", 0.05, force=False) is True


# ============================ DEFECT 2 — the caller learns the terminal result =====================
def test_recycle_plan_records_invoker(fs, monkeypatch):
    from cmux_fleet import state as fleet_state
    monkeypatch.setenv("CMUX_SURFACE_ID", "CALLER")
    fleet_state.live_put("caller-cond", {"role": "c", "kind": "conductor", "surface": "CALLER"})
    monkeypatch.setattr(fleet, "_compose_recycle_cmd", lambda *a, **k: ("cd /x && claude", ""))
    monkeypatch.setattr(fleet, "_is_roster", lambda r: False)               # no toml resolve
    entry = {"surface": "S", "session": "claude-OLD", "tool": "claude", "role": "w"}
    p = fleet._recycle_plan("w", entry, [], [], "resume", None, False, None, False)
    assert p["invoker_surface"] == "CALLER"                                 # recorded at schedule time
    assert p["invoker_label"] == "caller-cond"                              # resolved to the caller's label


def test_notify_caller_delivers_result_to_invoker(fs, monkeypatch):
    from cmux_fleet import state as fleet_state
    woke = []
    monkeypatch.setattr(fleet_state, "wake_if_idle", lambda surf, msg: woke.append(surf) or True)
    monkeypatch.setattr(fleet_state, "idlewake_on", lambda: True)
    p = {"label": "w", "surface": "S", "mode": "fresh", "invoker_surface": "CALLER", "invoker_label": "cond"}
    fleet._recycle_notify_caller(p, "ABORT", "still mid-turn after 180s")
    rows = fleet_state.inbox_pending("CALLER", kind="peer")
    assert len(rows) == 1
    r = rows[0]
    assert r["ptype"] == "peer-msg" and r["from_label"] == "fleet-recycle"
    assert "ABORT" in r["body"] and "w" in r["body"] and "still mid-turn" in r["body"]
    assert woke == ["CALLER"]                                               # the idle caller was pulled forward


def test_notify_caller_skips_when_no_invoker(fs, monkeypatch):
    # an operator driving the CLI directly (no $CMUX_SURFACE_ID) -> no invoker recorded -> nobody notified.
    from cmux_fleet import state as fleet_state
    fleet._recycle_notify_caller({"label": "w", "surface": "S", "mode": "fresh"}, "DONE", "ok")
    assert fleet_state.inbox_pending("S", kind="peer") == []


def test_notify_caller_skips_self_recycle_on_done(fs, monkeypatch):
    # a self-recycle that SUCCEEDED: the seat is being respawned away, and the new instance boots fresh from
    # its handover -> a completion note in an inbox that is going away is pure noise. Stay quiet.
    from cmux_fleet import state as fleet_state
    p = {"label": "w", "surface": "S", "mode": "fresh", "invoker_surface": "S", "invoker_label": "w"}
    fleet._recycle_notify_caller(p, "DONE", "ok")
    assert fleet_state.inbox_pending("S", kind="peer") == []


def test_notify_caller_self_recycle_abort_notifies_self(fs, monkeypatch):
    # THE fix: on ABORT the recycle explicitly does NOT respawn (no half-kill), so the seat is still alive
    # with an intact inbox -- and it is precisely the party that must learn its own recycle failed. Before
    # this it saw 'SCHEDULED' and nothing, ever (defect 2, verbatim, for the most common recycle shape).
    from cmux_fleet import state as fleet_state
    woke = []
    monkeypatch.setattr(fleet_state, "wake_if_idle", lambda surf, msg: woke.append(surf) or True)
    monkeypatch.setattr(fleet_state, "idlewake_on", lambda: True)
    p = {"label": "w", "surface": "S", "mode": "fresh", "invoker_surface": "S", "invoker_label": "w"}
    fleet._recycle_notify_caller(p, "ABORT", "still mid-turn after 180s")
    rows = fleet_state.inbox_pending("S", kind="peer")
    assert len(rows) == 1
    assert rows[0]["from_label"] == "fleet-recycle" and "ABORT" in rows[0]["body"]
    assert woke == ["S"]                                                    # wake is gated by wake_if_idle


def test_recycle_exec_abort_notifies_caller(fs, rs, monkeypatch):
    # integration: the quiet-gate ABORT (the exact silent path from the incident) now reaches the caller.
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet, "_quiet_gate", lambda *a, **k: False)        # never goes quiet -> ABORT
    monkeypatch.setattr(fleet_state, "wake_if_idle", lambda surf, msg: True)
    monkeypatch.setattr(fleet_state, "idlewake_on", lambda: True)
    payload = {"label": "w", "surface": "S", "send_cmd": "cd /x && claude", "mode": "fresh",
               "tool": "claude", "force": False, "prime": None, "old_session": "OLD", "cwd": "/x",
               "invoker_surface": "CALLER", "invoker_label": "cond"}
    assert fleet._recycle_exec_one(payload) == 1                            # ABORT preserved
    rows = fleet_state.inbox_pending("CALLER", kind="peer")
    assert len(rows) == 1 and "ABORT" in rows[0]["body"] and "w" in rows[0]["body"]


def test_recycle_exec_done_notifies_caller(fs, rs, monkeypatch):
    # the happy path notifies too (DONE reaches the caller, not just failures).
    from cmux_fleet import state as fleet_state
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "cmuxq", lambda *a, **k: "OK" if a and a[0] == "respawn-pane" else "")
    monkeypatch.setattr(fleet, "_quiet_gate", lambda *a, **k: True)
    monkeypatch.setattr(rs, "lifecycle", lambda surf: "ended")
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: False)
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")
    monkeypatch.setattr(fleet_state, "wake_if_idle", lambda surf, msg: True)
    monkeypatch.setattr(fleet_state, "idlewake_on", lambda: True)
    payload = {"label": "w", "surface": "S", "send_cmd": "cd /x && claude", "mode": "fresh",
               "tool": "claude", "force": True, "prime": None, "old_session": "OLD", "cwd": "/x",
               "invoker_surface": "CALLER", "invoker_label": "cond"}
    assert fleet._recycle_exec_one(payload) == 0
    rows = fleet_state.inbox_pending("CALLER", kind="peer")
    assert len(rows) == 1 and "DONE" in rows[0]["body"] and "w" in rows[0]["body"]
