# tests/test_launch_placement.py — cmd_launch's none-vs-unset placement axes (Ship 5d R-5d-1 + item
# 7-creation). The launch arg spec must express an EXPLICIT "none" DISTINCT from unset/default on two
# axes:
#   --group none  -> a STANDALONE workspace (opt out of the own/parent-group default), != --group NAME.
#   --parent none -> a TOP-LEVEL agent (registry parent=None), != unset (=$CMUX_SURFACE_ID).
# Same in-process seam as test_launch_guard: load_config + create_surface stubbed. create_surface doubles
# as (a) the capture of what the launch actually resolved and (b) the early-exit tripwire (returns
# (None, None) -> cmd_launch sys.exit(1) immediately after).
import pytest

from cmux_fleet import cli as fleet

# --adhoc resolves against this (5d: --adhoc is an alias for the rostered `adhoc` role). --cwd overrides
# the home per-launch; place defaults to tab so the workspace tests pass --place workspace explicitly.
ROLES = {"role": {"adhoc": {"cwd": "agents/ad-hoc", "claude": {}}}, "defaults": {"tool": "claude"}}


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(fleet, "load_config", lambda: ROLES)


def _capture(monkeypatch):
    """Stub create_surface to record (spec, parent) it was called with, then trip the exit."""
    seen = {}

    def cap(spec, parent, direction):
        seen["spec"] = dict(spec)
        seen["parent"] = parent
        return None, None                                    # -> `if not ws or not surf: sys.exit(1)`

    monkeypatch.setattr(fleet, "create_surface", cap)
    return seen


def _launch(tmp_path, *extra):
    return fleet.cmd_launch(["--adhoc", "probe", "--place", "workspace", "--cwd", str(tmp_path), *extra])


# --- the --group axis ---------------------------------------------------------------------------
def test_group_none_is_standalone(fs, monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        _launch(tmp_path, "--parent", "FAKEP", "--group", "none")
    assert seen["spec"]["group"] == ""                       # no group -> standalone workspace


def test_group_unset_child_joins_parent_group(fs, monkeypatch, tmp_path):
    # CONTROL for the sentinel: WITHOUT --group, a place=workspace child auto-joins its parent's group.
    # This is exactly the default `--group none` opts out of.
    fs.live_put("theparent", {"role": "lead", "kind": "conductor", "tool": "claude",
                              "surface": "PSURF", "group": "leadgrp", "session": "claude-x"})
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        _launch(tmp_path, "--parent", "PSURF")
    assert seen["spec"]["group"] == "leadgrp"                # inherited from the parent surface's row


def test_group_named_override_joins_that_group(fs, monkeypatch, tmp_path):
    # the third distinct value: --group NAME joins/bootstraps a DIFFERENT named group.
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        _launch(tmp_path, "--parent", "FAKEP", "--group", "othergrp")
    assert seen["spec"]["group"] == "othergrp"


# --- the --parent axis --------------------------------------------------------------------------
def test_parent_none_bypasses_abort_and_stays_groupless(fs, monkeypatch, tmp_path):
    # --parent none reaches create_surface (the no-parent abort did NOT fire) with an empty parent, and
    # a top-level child auto-joins nothing (no parent surface to inherit a group from).
    seen = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        _launch(tmp_path, "--parent", "none")
    assert seen["parent"] == ""
    assert seen["spec"]["group"] == ""


def test_parent_none_requires_workspace_place(fs, monkeypatch, tmp_path):
    _capture(monkeypatch)                                    # never reached: guard fires first
    with pytest.raises(SystemExit) as ei:
        fleet.cmd_launch(["--adhoc", "probe", "--place", "tab", "--cwd", str(tmp_path), "--parent", "none"])
    assert "top-level agent" in str(ei.value)


def test_missing_parent_still_aborts(fs, monkeypatch, tmp_path):
    # regression guard: dropping $CMUX_SURFACE_ID with no --parent and NO `none` must still abort (the
    # accidental-orphan guard), and must POINT at --parent none as the deliberate opt-in.
    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
    _capture(monkeypatch)
    with pytest.raises(SystemExit) as ei:
        fleet.cmd_launch(["--adhoc", "probe", "--place", "workspace", "--cwd", str(tmp_path)])
    assert "no --parent" in str(ei.value) and "--parent none" in str(ei.value)


# --- the registry rep (Berg-ruled: durable parent=None, no sentinel) ----------------------------
def test_register_top_level_stores_none(fs, tmp_path):
    spec = {"label": "topper", "role": "adhoc", "kind": "child", "tool": "claude",
            "abs_cwd": str(tmp_path), "place": "workspace", "group": "",
            "plugins": [], "flags": [], "settings": ""}
    fleet.register("SURF", spec, "", "sess", "WS")           # empty parent surface = top-level
    assert fs.is_top_level(fs.live_get("topper"))            # derives top-level -> parent is None/absent

    # control: a real parent surface stores a parent, so the same predicate reads NOT top-level.
    fleet.register("SURF2", {**spec, "label": "kid"}, "PSURF", "sess", "WS")
    assert not fs.is_top_level(fs.live_get("kid"))
