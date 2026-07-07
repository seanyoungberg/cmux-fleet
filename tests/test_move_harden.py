# tests/test_move_harden.py — the move-harden pass (2026-07-07): `fleet move` (atomic relocate) and
# `fleet group init|add` (one-conductor-one-group). cmux shell-outs are faked via a capturing cmuxq;
# the tree/group resolvers are monkeypatched so the units never touch a live cmux. Registry side runs
# against the throwaway $CMUX_STATE_DIR. Companion to the router move-vs-close tests in test_router.py.
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from cmux_fleet import cli as fleet  # noqa: E402
from cmux_fleet import state as fs   # noqa: E402


def _seq(*vals):
    """A stand-in for current_ws_for_surface: returns vals[0], vals[1], ... on successive calls."""
    it = iter(vals)
    return lambda *a, **k: next(it)


# =============================== fleet group init / add =========================================

def test_group_init_bootstraps_and_records(fs, monkeypatch):
    # group ABSENT -> create --from MY ws, set-anchor MY ws, close the empty scaffold anchor, record group.
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "surface": "COND-S",
                         "workspace": "WS-COND", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "current_ws_for_surface", lambda surf: "WS-COND")
    grefs = iter(["", "workspace_group:7"])                 # absent on first check, present after create
    monkeypatch.setattr(fleet, "_group_ref", lambda g: next(grefs))
    wsets = iter([{"WS-COND"}, {"WS-COND", "WS-SCAFFOLD"}])  # a NEW empty anchor appears after create
    monkeypatch.setattr(fleet, "_all_workspace_uuids", lambda txt: next(wsets))
    monkeypatch.setattr(fleet, "_term_surface_in", lambda ws, pane=None: "")   # scaffold is EMPTY

    assert fleet.cmd_group(["init", "--name", "AD - Berg Sandbox", "--surface", "COND-S"]) == 0

    create = [c for c in calls if c[:2] == ("workspace-group", "create")][0]
    assert "--from" in create and "WS-COND" in create and "AD - Berg Sandbox" in create
    setanchor = [c for c in calls if c[:2] == ("workspace-group", "set-anchor")][0]
    assert "WS-COND" in setanchor and "workspace_group:7" in setanchor
    assert ("close-workspace", "--workspace", "WS-SCAFFOLD") in calls   # empty scaffold reaped
    assert fs.live_get("cond")["group"] == "AD - Berg Sandbox"          # recorded in the registry
    assert fs.live_get("cond")["place"] == "workspace"


def test_group_init_keeps_nonempty_scaffold(fs, monkeypatch):
    # SAFETY: a new workspace that is NOT provably empty is never closed (avoid clobbering a real ws).
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "surface": "COND-S",
                         "workspace": "WS-COND", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "current_ws_for_surface", lambda surf: "WS-COND")
    monkeypatch.setattr(fleet, "_group_ref", _seq("", "workspace_group:7"))
    monkeypatch.setattr(fleet, "_all_workspace_uuids", _seq({"WS-COND"}, {"WS-COND", "WS-REAL"}))
    monkeypatch.setattr(fleet, "_term_surface_in", lambda ws, pane=None: "SURF-X")   # NOT empty

    assert fleet.cmd_group(["init", "--surface", "COND-S"]) == 0
    assert not [c for c in calls if c[0] == "close-workspace"]          # nothing closed
    assert fs.live_get("cond")["group"] == "cond"                      # name defaulted to the label


def test_group_init_existing_group_just_records(fs, monkeypatch):
    # group ALREADY exists -> record it on the conductor, no create/set-anchor/close.
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "surface": "COND-S",
                         "workspace": "WS-COND", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "current_ws_for_surface", lambda surf: "WS-COND")
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:3")   # already exists

    assert fleet.cmd_group(["init", "--name", "grp", "--surface", "COND-S"]) == 0
    assert not [c for c in calls if c[:2] == ("workspace-group", "create")]   # did NOT recreate
    assert fs.live_get("cond")["group"] == "grp"


def test_group_add_retrofits_child_without_moving_surface(fs, monkeypatch):
    # `group add`: the SAFE lane -- workspace-group add (no surface move), child stays live, group recorded.
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "surface": "COND-S",
                         "workspace": "WS-COND", "group": "grp", "status": "live"})
    fs.live_put("kid", {"role": "w", "kind": "child", "tool": "claude", "parent": "cond",
                        "surface": "KID-S", "workspace": "WS-KID", "place": "tab", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:4")
    monkeypatch.setattr(fleet, "current_ws_for_surface", lambda surf: "WS-KID")

    assert fleet.cmd_group(["add", "kid", "--surface", "COND-S"]) == 0
    add = [c for c in calls if c[:2] == ("workspace-group", "add")][0]
    assert "workspace_group:4" in add and "WS-KID" in add
    assert not [c for c in calls if c[0] in ("move-surface", "move-tab-to-new-workspace")]  # NO move
    kid = fs.live_get("kid")
    assert kid["group"] == "grp" and kid["place"] == "workspace"       # child row now claims the group


def test_group_add_refuses_without_conductor_group(fs, monkeypatch):
    fs.live_put("cond", {"role": "c", "kind": "conductor", "surface": "COND-S", "status": "live"})  # no group
    fs.live_put("kid", {"role": "w", "kind": "child", "parent": "cond", "surface": "KID-S",
                        "status": "live"})
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: "")
    with pytest.raises(SystemExit):
        fleet.cmd_group(["add", "kid", "--surface", "COND-S"])         # run `group init` first


# =============================== fleet move ====================================================

def test_move_to_workspace_reconciles_registry(fs, monkeypatch):
    fs.live_put("kid", {"role": "w", "kind": "child", "tool": "claude", "parent": "cond",
                        "surface": "KID-S", "workspace": "WS-OLD", "place": "tab",
                        "session": "claude-k", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "current_ws_for_surface",
                        _seq("WS-OLD", "22222222-2222-2222-2222-222222222222"))  # cur, then new
    monkeypatch.setattr(fs, "surface_has_live_agent", lambda s: True)

    rc = fleet.cmd_move(["kid", "--to-workspace", "22222222-2222-2222-2222-222222222222"])
    assert rc == 0
    mv = [c for c in calls if c[0] == "move-surface"][0]
    assert "KID-S" in mv and "22222222-2222-2222-2222-222222222222" in mv
    kid = fs.live_get("kid")
    assert kid["workspace"] == "22222222-2222-2222-2222-222222222222"  # reconciled from tree ground truth
    assert kid["place"] == "workspace"
    assert kid["surface"] == "KID-S" and kid["session"] == "claude-k"  # surface + session UNCHANGED
    # the expected-close tombstone was stamped BEFORE the move (router archive-suppression belt)
    assert fs.expected_close_recent("KID-S")


def test_move_own_workspace_joins_conductor_group(fs, monkeypatch):
    fs.live_put("cond", {"role": "c", "kind": "conductor", "surface": "COND-S", "group": "G",
                         "status": "live"})
    fs.live_put("kid", {"role": "w", "kind": "child", "tool": "claude", "parent": "cond",
                        "surface": "KID-S", "workspace": "WS-OLD", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "current_ws_for_surface", _seq("WS-OLD", "WS-NEW", "WS-NEW"))
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:5")
    monkeypatch.setattr(fs, "surface_has_live_agent", lambda s: True)

    assert fleet.cmd_move(["kid", "--own-workspace"]) == 0
    assert [c for c in calls if c[0] == "move-tab-to-new-workspace"]   # fresh workspace
    add = [c for c in calls if c[:2] == ("workspace-group", "add")][0]
    assert "workspace_group:5" in add and "WS-NEW" in add             # joined the conductor's group
    kid = fs.live_get("kid")
    assert kid["workspace"] == "WS-NEW" and kid["group"] == "G" and kid["place"] == "workspace"


def test_move_refuses_when_surface_not_in_tree(fs, monkeypatch):
    fs.live_put("kid", {"role": "w", "kind": "child", "surface": "KID-S", "workspace": "WS-OLD",
                        "status": "live"})
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: "")
    monkeypatch.setattr(fleet, "current_ws_for_surface", lambda s: "")   # surface GONE from the tree
    with pytest.raises(SystemExit):
        fleet.cmd_move(["kid", "--own-workspace"])                       # -> revive, not move


def test_move_requires_exactly_one_target(fs, monkeypatch):
    fs.live_put("kid", {"role": "w", "kind": "child", "surface": "KID-S", "status": "live"})
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: "")
    with pytest.raises(SystemExit):
        fleet.cmd_move(["kid"])                                          # neither flag
    with pytest.raises(SystemExit):
        fleet.cmd_move(["kid", "--own-workspace", "--to-workspace", "WS"])  # both


def test_move_noop_when_already_in_target(fs, monkeypatch):
    fs.live_put("kid", {"role": "w", "kind": "child", "surface": "KID-S",
                        "workspace": "11111111-1111-1111-1111-111111111111", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "current_ws_for_surface",
                        lambda s: "11111111-1111-1111-1111-111111111111")
    assert fleet.cmd_move(["kid", "--to-workspace", "11111111-1111-1111-1111-111111111111"]) == 0
    assert not [c for c in calls if c[0] == "move-surface"]              # nothing moved
    assert not fs.expected_close_recent("KID-S")                        # and no tombstone stamped
