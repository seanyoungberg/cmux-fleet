# tests/test_lifecycle_owner_guard.py — item 9: the cross-conductor lifecycle guard on archive/rm, plus
# the revive parent-preservation rep. A DIFFERENT identified conductor (invoker != the target's registry
# parent) acting on another conductor's child is REFUSED without --force and the parent is notified EITHER
# WAY; an operator driving the CLI directly (no $CMUX_SURFACE_ID) keeps full unguarded control.
import pytest

from cmux_fleet import cli as fleet


def _seed_child(fs, label="kid", parent="condA"):
    fs.live_put(label, {"role": "worker", "kind": "child", "tool": "claude", "cwd": "/x",
                        "place": "tab", "surface": "KIDSURF", "session": "claude-k", "parent": parent})


def _seed_conductor(fs, label, surface):
    fs.live_put(label, {"role": "lead", "kind": "conductor", "tool": "claude", "cwd": "/y",
                        "place": "workspace", "group": label, "surface": surface, "session": "claude-c"})


def _as(monkeypatch, surface):
    monkeypatch.setenv("CMUX_SURFACE_ID", surface)


# --- the guard decision matrix (unit, no cmux teardown) -----------------------------------------
def test_guard_refuses_other_conductor_no_force(fs, monkeypatch):
    _seed_child(fs); _seed_conductor(fs, "condA", "SA"); _seed_conductor(fs, "condB", "SB")
    _as(monkeypatch, "SB")                                    # condB (not the parent) invokes
    with pytest.raises(SystemExit) as ei:
        fleet._lifecycle_owner_guard("kid", "archive", force=False)
    assert "REFUSED" in str(ei.value) and "condA" in str(ei.value)
    # parent condA was notified of the attempt
    pend = fs.inbox_pending("SA", "peer")
    assert pend and "kid" in pend[-1]["body"] and "TRIED to archive" in pend[-1]["body"]


def test_guard_forces_through_and_notifies(fs, monkeypatch):
    _seed_child(fs); _seed_conductor(fs, "condA", "SA"); _seed_conductor(fs, "condB", "SB")
    _as(monkeypatch, "SB")
    fleet._lifecycle_owner_guard("kid", "remove", force=True)  # --force: no refusal
    pend = fs.inbox_pending("SA", "peer")
    assert pend and "removed your child 'kid'" in pend[-1]["body"] and "forced" in pend[-1]["body"]


def test_guard_noop_for_the_parent_itself(fs, monkeypatch):
    _seed_child(fs); _seed_conductor(fs, "condA", "SA")
    _as(monkeypatch, "SA")                                    # the parent archives its own child
    fleet._lifecycle_owner_guard("kid", "archive", force=False)   # no raise
    assert not fs.inbox_pending("SA", "peer")                 # ...and no self-notification


def test_guard_noop_for_anonymous_operator(fs, monkeypatch):
    # no $CMUX_SURFACE_ID = an operator (Berg) at the CLI: full control, no guard, no notify.
    _seed_child(fs); _seed_conductor(fs, "condA", "SA")
    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
    fleet._lifecycle_owner_guard("kid", "archive", force=False)   # no raise
    assert not fs.inbox_pending("SA", "peer")


def test_guard_noop_for_the_target_itself(fs, monkeypatch):
    # rm/archive invoked FROM the target's own surface (self-removal, or an operator inside its pane):
    # invoker resolves to the target label -> not a cross-conductor grab, so no guard.
    _seed_child(fs, label="kid", parent="condA")
    fs.live_put("kid", {**fs.live_get("kid"), "surface": "KIDSURF"})
    _seed_conductor(fs, "condA", "SA")
    _as(monkeypatch, "KIDSURF")                               # invoker == 'kid' (the target)
    fleet._lifecycle_owner_guard("kid", "remove", force=False)   # no raise
    assert not fs.inbox_pending("SA", "peer")


def test_guard_noop_for_top_level_target(fs, monkeypatch):
    fs.live_put("orphan", {"role": "lead", "kind": "conductor", "tool": "claude", "surface": "SO",
                           "session": "claude-o"})            # no parent -> top-level
    _seed_conductor(fs, "condB", "SB")
    _as(monkeypatch, "SB")
    fleet._lifecycle_owner_guard("orphan", "remove", force=False)  # no raise (nobody owns a top-level agent)


# --- wiring: the refuse path fires BEFORE any teardown (exits early, so no cmux needed) ----------
def test_cmd_archive_wires_the_guard(fs, monkeypatch):
    _seed_child(fs); _seed_conductor(fs, "condA", "SA"); _seed_conductor(fs, "condB", "SB")
    _as(monkeypatch, "SB")
    with pytest.raises(SystemExit) as ei:
        fleet.cmd_archive(["kid"])
    assert "REFUSED" in str(ei.value)
    assert fs.live_get("kid") is not None                     # untouched: refused before teardown
    assert fs.archive_get("kid") is None


def test_cmd_rm_wires_the_guard(fs, monkeypatch):
    _seed_child(fs); _seed_conductor(fs, "condA", "SA"); _seed_conductor(fs, "condB", "SB")
    _as(monkeypatch, "SB")
    with pytest.raises(SystemExit) as ei:
        fleet.cmd_rm(["kid"])
    assert "REFUSED" in str(ei.value)
    assert fs.live_get("kid") is not None                     # untouched


# --- revive parent-preservation rep (item 9) ----------------------------------------------------
def test_register_parent_label_override_preserves_relationship(fs, tmp_path):
    spec = {"label": "revived", "role": "worker", "kind": "child", "tool": "claude",
            "abs_cwd": str(tmp_path), "place": "tab", "group": "",
            "plugins": [], "flags": [], "settings": ""}
    # PSURF resolves to some other conductor, but the archived parent 'condA' must win.
    fleet.register("SURF", spec, "PSURF", "sess", "WS", parent_label="condA")
    assert fs.live_get("revived")["parent"] == "condA"

    # None override falls back to deriving from the parent surface (the launch path, unchanged).
    fs.live_put("condA", {"role": "lead", "kind": "conductor", "surface": "PSURF", "session": "claude-c"})
    fleet.register("SURF2", {**spec, "label": "kid2"}, "PSURF", "sess", "WS")   # parent_label defaults None
    assert fs.live_get("kid2")["parent"] == "condA"           # derived from PSURF -> condA's label


def test_revive_inherits_placement_parent_group(fs, monkeypatch, tmp_path):
    # item 9: a place=workspace child revived with no group of its own joins the PLACEMENT parent's group,
    # exactly like `launch --place workspace`. create_surface doubles as the capture + early-exit tripwire.
    fs.archive_put("kid", {"role": "adhoc-x", "kind": "child", "tool": "claude", "cwd": str(tmp_path),
                           "place": "workspace", "group": "", "parent": "condA",
                           "plugins": [], "flags": [], "settings": "", "last_session": "claude-k"})
    fs.live_put("condA", {"role": "lead", "kind": "conductor", "surface": "PSURF", "group": "pgrp",
                          "place": "workspace", "session": "claude-c"})
    monkeypatch.setattr(fleet, "_resolve_recycle_provider", lambda *a: (None, "", ""))   # no keychain in test
    seen = {}

    def cap(spec, parent, direction):
        seen["group"] = spec["group"]
        return None, None

    monkeypatch.setattr(fleet, "create_surface", cap)
    _as(monkeypatch, "PSURF")                                 # revive from condA's terminal (placement parent)
    with pytest.raises(SystemExit):
        fleet.cmd_revive(["kid"])
    assert seen["group"] == "pgrp"
