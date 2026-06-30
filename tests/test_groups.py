# tests/test_groups.py — built-in workspace-group handling (one conductor = one group). Covers the
# logic that decides join-vs-bootstrap and the name->ref resolution, with cmux shelled calls captured
# via a fake cmuxq (no real cmux). Plus an e2e dry-run for the conductor group default.
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import fleet  # noqa: E402  (not popped by other test files)


def test_group_ref_resolves_name_passthrough_and_missing(monkeypatch):
    monkeypatch.setattr(fleet, "cmuxq",
                        lambda *a: '{"groups":[{"name":"alpha","ref":"workspace_group:2"}]}')
    assert fleet._group_ref("alpha") == "workspace_group:2"      # name -> ref
    assert fleet._group_ref("workspace_group:9") == "workspace_group:9"  # ref passthrough (no lookup)
    assert fleet._group_ref("missing") == ""
    assert fleet._group_ref("") == ""


def test_workspace_bootstrap_uses_explicit_from(monkeypatch):
    # group ABSENT -> create a standalone anchor workspace, then workspace-group create --from <that ref>.
    calls = []
    def fake_cmuxq(*args):
        calls.append(args)
        return "created workspace:7\n" if args[:1] == ("new-workspace",) else ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    refs = iter(["", "workspace_group:3"])                       # absent on first check, present after create
    monkeypatch.setattr(fleet, "_group_ref", lambda g: next(refs))
    monkeypatch.setattr(fleet, "_ref_to_uuid", lambda kind, ref: "WS")
    monkeypatch.setattr(fleet, "_term_surface_in", lambda ws, pane=None: "SF")

    ws, surf = fleet.create_surface(
        {"place": "workspace", "group": "g1", "label": "cond", "abs_cwd": "/tmp/x"}, "PARENT", "down")
    assert (ws, surf) == ("WS", "SF")
    anchor = [c for c in calls if c[0] == "new-workspace"][0]
    assert "--group" not in anchor                              # the anchor is created STANDALONE, not joined
    create = [c for c in calls if c[:2] == ("workspace-group", "create")][0]
    assert "--from" in create and "workspace:7" in create       # ALWAYS explicit --from (never implicit)
    assert "--name" in create and "g1" in create


def test_workspace_joins_existing_group(monkeypatch):
    calls = []
    def fake_cmuxq(*args):
        calls.append(args)
        return "created workspace:9\n" if args[:1] == ("new-workspace",) else ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:5")   # group EXISTS
    monkeypatch.setattr(fleet, "_ref_to_uuid", lambda kind, ref: "WS")
    monkeypatch.setattr(fleet, "_term_surface_in", lambda ws, pane=None: "SF")

    fleet.create_surface({"place": "workspace", "group": "g1", "label": "w", "abs_cwd": "/tmp/x"}, "P", "down")
    nw = [c for c in calls if c[0] == "new-workspace"][0]
    assert "--group" in nw and "workspace_group:5" in nw        # JOINED the existing group
    assert not [c for c in calls if c[:2] == ("workspace-group", "create")]   # did NOT create a new one


def test_conductor_group_defaults_to_label(tmp_path):
    # e2e dry-run: a conductor role with NO explicit group defaults the group to its label.
    toml = tmp_path / "f.toml"
    toml.write_text('[tool.claude]\nflags=""\n[role.solo]\nkind="conductor"\nplace="workspace"\ncwd="x"\n')
    env = dict(os.environ, CMUX_FLEET_TOML=str(toml), CMUX_STATE_DIR=str(tmp_path / "st"),
               CMUX_FLEET_ROOT=str(tmp_path), CMUX_FLEET_MARKETPLACE="")
    p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "fleet.py"),
                        "launch", "solo", "--parent", "FAKE", "--dry-run"],
                       capture_output=True, text=True, env=env)
    assert "group=solo" in p.stdout, p.stdout + p.stderr        # defaulted to the conductor's label


def test_rm_with_group_dissolves_by_ref(monkeypatch):
    import fleet_state as fs
    fs.live_put("cond", {"role": "r", "kind": "conductor", "tool": "claude", "group": "gg",
                         "surface": "", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:4")
    fleet.cmd_rm(["cond", "--with-group"])
    assert ("workspace-group", "delete", "workspace_group:4") in calls   # delete by REF
    assert fs.live_get("cond") is None


def test_rm_without_group_leaves_group_intact(monkeypatch):
    import fleet_state as fs
    fs.live_put("cond2", {"role": "r", "kind": "conductor", "tool": "claude", "group": "gg",
                          "surface": "", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:4")
    fleet.cmd_rm(["cond2"])
    assert not [c for c in calls if c[:2] == ("workspace-group", "delete")]  # group untouched
    assert fs.live_get("cond2") is None
