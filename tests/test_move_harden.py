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


# ================= P0-3: tool-aware launch flags (claude-isms must not kill codex) ==================
# The 2026-07-07 incident: `--effort` (a claude flag) was forwarded VERBATIM to codex, which aborted on
# "unexpected argument '--effort' found". _codex_flags is the ONE place that owns claude->codex flag
# translation at the adapter boundary. These are pure units on the token mapping + adapter wiring.

def test_codex_flags_translates_effort_to_config_override():
    assert fleet._codex_flags(["--effort", "high"]) == ["-c", "model_reasoning_effort=high"]
    assert fleet._codex_flags(["--effort", "low"]) == ["-c", "model_reasoning_effort=low"]


def test_codex_flags_passes_the_effort_LEVEL_through_verbatim():
    # The LEVEL is passed through, not clamped: codex's tiers overlap the fleet's (its TUI shows xhigh),
    # so clamping would SILENTLY DOWNGRADE reasoning. A value codex rejects fails LOUD via the P0-4a
    # launch verify instead. Inline (--effort=X) and separate (--effort X) forms both work.
    assert fleet._codex_flags(["--effort", "xhigh"]) == ["-c", "model_reasoning_effort=xhigh"]
    assert fleet._codex_flags(["--effort=max"]) == ["-c", "model_reasoning_effort=max"]


def test_codex_flags_translates_dangerous_bypass():
    assert fleet._codex_flags(["--dangerously-skip-permissions"]) \
        == ["--dangerously-bypass-approvals-and-sandbox"]


def test_codex_flags_drops_claude_only_flags_with_values():
    # --setting-sources / --permission-mode / --plugin-dir have no codex analog -> drop (consume value).
    assert fleet._codex_flags(["--setting-sources", "user,project", "--model", "gpt-5-codex"]) \
        == ["--model", "gpt-5-codex"]                                    # setting-sources gone, model kept
    assert fleet._codex_flags(["--permission-mode", "plan"]) == []
    assert fleet._codex_flags(["--plugin-dir", "/x/p"]) == []


def test_codex_flags_passes_codex_native_flags_through():
    # a codex floor's own flags (and any -- passthrough that's already codex-shaped) are untouched.
    assert fleet._codex_flags(["--effort", "high", "-c", "sandbox=danger", "--search"]) \
        == ["-c", "model_reasoning_effort=high", "-c", "sandbox=danger", "--search"]


def test_codex_flags_effort_as_last_bare_token_is_dropped_not_crashed():
    assert fleet._codex_flags(["--effort"]) == []                       # no value -> emit nothing, no IndexError


def test_adapter_compile_codex_translates_effort_and_claude_stays_verbatim():
    spec = {"tool": "codex", "role": "w", "label": "w", "flags": [], "env": {},
            "plugins": [], "settings": "", "setting_sources": ""}
    binn, args, _ = fleet.adapter_compile("codex", spec, ["--effort", "high"])
    assert binn == "codex"
    assert "--effort" not in args and "-c" in args and "model_reasoning_effort=high" in args
    # the SAME caller tokens on a claude spec are forwarded AS-IS (claude owns --effort natively).
    cspec = dict(spec, tool="claude")
    _, cargs, _ = fleet.adapter_compile("claude", cspec, ["--effort", "high"])
    assert "--effort" in cargs and "high" in cargs                      # claude path unchanged (regression guard)


# ================= P0-4a: launch verification (a dead-on-arrival lazy child != DONE) ================
# launch_error_line is the PURE scanner (shared with the router never-bound sweep) that tells a launch
# that DIED on spawn (a bad flag / missing binary / crash) from a healthy agent TUI. _launch_failure_line
# wires it to a live pane via cmuxq. cmd_launch uses these to FAIL LOUD instead of a false "DONE".

def test_launch_error_line_catches_the_bad_flag_death():
    pane = ("user@host cmux $ codex --effort high\n"
            "error: unexpected argument '--effort' found\n"
            "\n  tip: a similar argument exists: '--config'\n"
            "user@host cmux $ ")
    assert "unexpected argument" in fleet.launch_error_line(pane)


def test_launch_error_line_catches_missing_binary():
    assert "command not found" in fleet.launch_error_line("bash: codex: command not found")


def test_launch_error_line_is_quiet_on_a_healthy_tui():
    # a booted agent shows its chrome, NOT a CLI error -> no false failure verdict.
    healthy = "  Context Remaining: 100%   ? for shortcuts   esc to interrupt\n> \n"
    assert fleet.launch_error_line(healthy) == ""
    assert fleet.launch_error_line("") == ""


def test_launch_failure_line_reads_the_pane_via_cmuxq(monkeypatch):
    monkeypatch.setattr(fleet, "cmuxq",
                        lambda *a: "error: unexpected argument '--effort' found" if a[:1] == ("capture-pane",) else "")
    assert "unexpected argument" in fleet._launch_failure_line("SURF")
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: "> healthy prompt")
    assert fleet._launch_failure_line("SURF") == ""
