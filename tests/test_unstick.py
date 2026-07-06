# tests/test_unstick.py — the dead-agent reap: fs.reap_dead_surface_records + `fleet unstick`.
# The SessionEnd-freeze backstop (root-caused 2026-07-06): a process that dies WITHOUT a clean SessionEnd
# (SIGKILL / abrupt kill / the SessionEnd store-write race) leaves a hook-store record frozen at a
# non-terminal lifecycle with a dead/None pid. unstick reaps that ghost — and ONLY that ghost — from
# cmux's per-tool store, never a record whose pid is alive.
import json
import os

from cmux_fleet import state as fs
from cmux_fleet import cli as fleet

DEAD = "deadbeef-1111-2222-3333-444444444444"
LIVE = "11111111-5555-6666-7777-888888888888"


def _write_store(hookdir, tool, sessions, active=None):
    path = os.path.join(hookdir, f"{tool}-hook-sessions.json")
    json.dump({"sessions": sessions, "activeSessionsBySurface": active or {}}, open(path, "w"))
    return path


def _read_store(hookdir, tool="claude"):
    return json.load(open(os.path.join(hookdir, f"{tool}-hook-sessions.json")))


def test_reap_removes_dead_pid_ghost_keeps_live(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "HOOKSTORE", str(tmp_path))
    _write_store(str(tmp_path), "claude", {
        DEAD: {"sessionId": DEAD, "surfaceId": "SURF", "agentLifecycle": "running", "pid": None, "updatedAt": 1},
        LIVE: {"sessionId": LIVE, "surfaceId": "SURF", "agentLifecycle": "idle", "pid": os.getpid(), "updatedAt": 2},
    }, active={"SURF": {"sessionId": DEAD, "updatedAt": 1}})   # cmux's REAL active-pointer shape (a dict)
    res = fs.reap_dead_surface_records("SURF")
    assert [r["sid"] for r in res["reaped"]] == [DEAD]
    assert [k["sid"] for k in res["live_kept"]] == [LIVE]
    d = _read_store(str(tmp_path))
    assert DEAD not in d["sessions"] and LIVE in d["sessions"]     # ghost gone, live agent kept
    assert d["activeSessionsBySurface"] == {}                       # dead session's active pointer cleared


def test_reap_never_touches_an_all_live_surface(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "HOOKSTORE", str(tmp_path))
    _write_store(str(tmp_path), "claude", {
        LIVE: {"sessionId": LIVE, "surfaceId": "SURF", "agentLifecycle": "running", "pid": os.getpid(), "updatedAt": 2},
    })
    res = fs.reap_dead_surface_records("SURF")
    assert res["reaped"] == [] and len(res["live_kept"]) == 1
    assert LIVE in _read_store(str(tmp_path))["sessions"]           # a live record is never reaped


def test_reap_matches_surface_case_insensitively(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "HOOKSTORE", str(tmp_path))
    _write_store(str(tmp_path), "claude", {
        DEAD: {"sessionId": DEAD, "surfaceId": "surf-abc", "agentLifecycle": "unknown", "pid": None, "updatedAt": 1},
    })
    res = fs.reap_dead_surface_records("SURF-ABC")                  # caller passes upper; record is lower
    assert [r["sid"] for r in res["reaped"]] == [DEAD]


def test_reap_dry_run_reports_but_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "HOOKSTORE", str(tmp_path))
    _write_store(str(tmp_path), "claude", {
        DEAD: {"sessionId": DEAD, "surfaceId": "SURF", "agentLifecycle": "running", "pid": None, "updatedAt": 1},
    })
    res = fs.reap_dead_surface_records("SURF", dry_run=True)
    assert [r["sid"] for r in res["reaped"]] == [DEAD]
    assert DEAD in _read_store(str(tmp_path))["sessions"]           # dry-run: still on disk


def test_cmd_unstick_reaps_by_label(tmp_path, monkeypatch, capsys):
    # cmd_unstick does `from . import state` INTERNALLY; resolve the SAME live module here (test_features
    # pops/re-imports cmux_fleet.state, so this file's top-level `fs` can be stale — see conftest).
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet_state, "HOOKSTORE", str(tmp_path))
    fleet_state.live_put("w", {"surface": "SURF", "role": "w", "kind": "child", "session": "claude-dead"})
    _write_store(str(tmp_path), "claude", {
        DEAD: {"sessionId": DEAD, "surfaceId": "SURF", "agentLifecycle": "running", "pid": None, "updatedAt": 1},
    })
    assert fleet.cmd_unstick(["w"]) == 0
    out = capsys.readouterr().out.lower()
    assert "reaped" in out
    assert DEAD not in _read_store(str(tmp_path))["sessions"]       # cleared from cmux's store


def test_cmd_unstick_refuses_to_touch_a_live_agent(tmp_path, monkeypatch, capsys):
    from cmux_fleet import state as fleet_state
    monkeypatch.setattr(fleet_state, "HOOKSTORE", str(tmp_path))
    fleet_state.live_put("w", {"surface": "SURF", "role": "w", "kind": "child", "session": f"claude-{LIVE}"})
    _write_store(str(tmp_path), "claude", {
        LIVE: {"sessionId": LIVE, "surfaceId": "SURF", "agentLifecycle": "running", "pid": os.getpid(), "updatedAt": 2},
    })
    assert fleet.cmd_unstick(["w"]) == 0
    out = capsys.readouterr().out.lower()
    assert "no frozen" in out or "live record" in out              # nothing reaped; live record surfaced
    assert LIVE in _read_store(str(tmp_path))["sessions"]           # untouched
