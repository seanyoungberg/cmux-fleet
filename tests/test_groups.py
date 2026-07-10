# tests/test_groups.py — built-in workspace-group handling (one conductor = one group). Covers the
# logic that decides join-vs-bootstrap and the name->ref resolution, with cmux shelled calls captured
# via a fake cmuxq (no real cmux). Plus an e2e dry-run for the conductor group default.
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from cmux_fleet import cli as fleet  # noqa: E402  (not popped by other test files)


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
               CMUX_FLEET_ROOT=str(tmp_path), CMUX_FLEET_MARKETPLACE="",
               PYTHONPATH=REPO + os.pathsep + os.environ.get("PYTHONPATH", ""))
    p = subprocess.run([sys.executable, "-m", "cmux_fleet",
                        "launch", "solo", "--parent", "FAKE", "--dry-run"],
                       capture_output=True, text=True, env=env)
    assert "group=solo" in p.stdout, p.stdout + p.stderr        # defaulted to the conductor's label


def test_rm_with_group_dissolves_by_ref(monkeypatch):
    # registry and cmux AGREE on membership (workspace ids match member_workspace_refs) -> dissolve proceeds.
    from cmux_fleet import state as fs
    fs.live_put("cond", {"role": "r", "kind": "conductor", "tool": "claude", "group": "gg",
                         "surface": "", "workspace": "WS-COND", "status": "live"})
    calls = []

    def fake_cmuxq(*a):
        calls.append(a)
        if a[:2] == ("workspace-group", "list"):
            return '{"groups":[{"ref":"workspace_group:4","member_workspace_refs":["workspace:1"]}]}'
        return ""

    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:4")
    monkeypatch.setattr(fleet, "_ref_to_uuid", lambda kind, ref: "WS-COND")
    fleet.cmd_rm(["cond", "--with-group"])
    assert ("workspace-group", "delete", "workspace_group:4") in calls   # delete by REF
    assert fs.live_get("cond") is None


def test_rm_with_group_sweeps_all_members(monkeypatch):
    # the orphan bug: dissolving the group closed every member surface, but only the SELECTED label was
    # cleared from the registry, leaving siblings as stale rows. rm --with-group must sweep them all.
    # (registry and cmux agree here too -- the mismatch-refusal case is covered separately below.)
    from cmux_fleet import state as fs
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "group": "g",
                         "surface": "SC", "workspace": "WS-C", "status": "live"})
    fs.live_put("child", {"role": "w", "kind": "child", "tool": "claude", "group": "g",
                          "parent": "cond", "surface": "SW", "workspace": "WS-W", "status": "live"})
    fs.live_put("other", {"role": "w", "kind": "child", "tool": "claude", "group": "other-g",
                          "surface": "SX", "workspace": "WS-X", "status": "live"})

    def fake_cmuxq(*a):
        if a[:2] == ("workspace-group", "list"):
            return ('{"groups":[{"ref":"workspace_group:1",'
                    '"member_workspace_refs":["workspace:c","workspace:w"]}]}')
        return ""

    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:1")
    monkeypatch.setattr(fleet, "_ref_to_uuid",
                        lambda kind, ref: {"workspace:c": "WS-C", "workspace:w": "WS-W"}[ref])
    fleet.cmd_rm(["cond", "--with-group"])
    assert fs.live_get("cond") is None         # the selected conductor is gone
    assert fs.live_get("child") is None         # ...and so is its group sibling (the swept orphan)
    assert fs.live_get("other") is not None     # a DIFFERENT group is untouched


def test_rm_with_group_refuses_on_membership_mismatch(monkeypatch):
    # 2026-07-02 incident shape: the registry believes a small/wrong membership for the group NAME on
    # this row, but cmux's REAL group (resolved by ref) reports totally different members -- refuse
    # instead of dissolving strangers. No dissolve, no sweep; the label stays untouched.
    from cmux_fleet import state as fs
    fs.live_put("staging-conductor", {"role": "c", "kind": "conductor", "tool": "claude",
                                       "group": "AD - Berg Sandbox", "surface": "S1",
                                       "workspace": "WS-1", "status": "live"})
    calls = []

    def fake_cmuxq(*a):
        calls.append(a)
        if a[:2] == ("workspace-group", "list"):
            return ('{"groups":[{"ref":"workspace_group:3",'
                    '"member_workspace_refs":["workspace:10","workspace:11","workspace:12"]}]}')
        return ""

    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:3")
    monkeypatch.setattr(fleet, "_ref_to_uuid", lambda kind, ref: "WS-" + ref.split(":")[1])
    with pytest.raises(SystemExit):
        fleet.cmd_rm(["staging-conductor", "--with-group"])
    assert not [c for c in calls if c[:2] == ("workspace-group", "delete")]   # refused BEFORE dissolving
    assert fs.live_get("staging-conductor") is not None   # nothing swept -- refusal touches no registry row


def test_register_scrubs_group_for_non_workspace_placement(fs):
    # Item 2 point 3 (launcher-misplacement discovery): a role's toml (or a caller --group) can carry a
    # `group` value alongside place="tab"/"pane" (e.g. a --place override away from a workspace-default
    # role) -- but create_surface() only ever performs REAL cmux workspace-group membership when
    # place=="workspace". Persisting the group value anyway let a registry row claim membership its
    # surface never actually joined (the 2026-07-02 root cause: staging-conductor's row said
    # group="AD - Berg Sandbox" though it was never placed in that visual group, and `rm --with-group`
    # trusted the claim). register() must scrub it for any non-workspace placement.
    spec = {"role": "berg-sandbox", "kind": "conductor", "tool": "claude", "abs_cwd": "/x",
            "place": "tab", "group": "AD - Berg Sandbox", "label": "staging-conductor",
            "plugins": [], "flags": [], "settings": ""}
    fleet.register("SURF-1", spec, "", "SESSID", "WS-1")
    assert fs.live_get("staging-conductor")["group"] == ""


def test_register_keeps_group_for_workspace_placement(fs):
    spec = {"role": "berg-sandbox", "kind": "conductor", "tool": "claude", "abs_cwd": "/x",
            "place": "workspace", "group": "AD - Berg Sandbox", "label": "berg-sandbox",
            "plugins": [], "flags": [], "settings": ""}
    fleet.register("SURF-2", spec, "", "SESSID", "WS-2")
    assert fs.live_get("berg-sandbox")["group"] == "AD - Berg Sandbox"


def test_rm_without_group_leaves_group_intact(monkeypatch):
    from cmux_fleet import state as fs
    fs.live_put("cond2", {"role": "r", "kind": "conductor", "tool": "claude", "group": "gg",
                          "surface": "", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:4")
    fleet.cmd_rm(["cond2"])
    assert not [c for c in calls if c[:2] == ("workspace-group", "delete")]  # group untouched
    assert fs.live_get("cond2") is None


# --- --with-group CONFIRM GATE (recovery-safety #3) --------------------------------------------------
# After the membership cross-check AGREES, a dissolve that would close LIVE collateral (any live agent
# besides the named target) is a mass-close: it PREVIEWS the blast radius and REFUSES (return 3, nothing
# mutated) until --yes. A solo/target-only or all-stale group needs no --yes. These stub surface_has_live_
# agent (the liveness authority) so the fixture doesn't depend on the machine's real cmux hook store.
def _dissolve_stubs(monkeypatch, live_surfaces, member_refs, gref="workspace_group:7"):
    from cmux_fleet import state as fs
    calls = []

    def fake_cmuxq(*a):
        calls.append(a)
        if a[:2] == ("workspace-group", "list"):
            refs = ",".join(f'"{r}"' for r in member_refs)
            return '{"groups":[{"ref":"%s","member_workspace_refs":[%s]}]}' % (gref, refs)
        return ""

    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "_group_ref", lambda g: gref)
    monkeypatch.setattr(fleet, "_ref_to_uuid", lambda kind, ref: member_refs[ref])
    monkeypatch.setattr(fleet, "_resume_binding", lambda s: {})
    monkeypatch.setattr(fs, "surface_has_live_agent", lambda s: s in live_surfaces)
    return calls


def _seed_cond_child(fs):
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "group": "g",
                         "surface": "SC", "workspace": "WS-C", "status": "live"})
    fs.live_put("child", {"role": "w", "kind": "child", "tool": "claude", "group": "g", "parent": "cond",
                          "surface": "SW", "workspace": "WS-W", "status": "live"})


def test_rm_with_group_confirm_gate_blocks_live_collateral(monkeypatch, capsys):
    from cmux_fleet import state as fs
    _seed_cond_child(fs)
    calls = _dissolve_stubs(monkeypatch, live_surfaces={"SC", "SW"},
                            member_refs={"workspace:c": "WS-C", "workspace:w": "WS-W"})
    rc = fleet.cmd_rm(["cond", "--with-group"])                     # NO --yes -> gated
    assert rc == 3                                                  # distinct 'confirmation needed' code
    assert not [c for c in calls if c[:2] == ("workspace-group", "delete")]  # NOTHING dissolved
    assert fs.live_get("cond") is not None and fs.live_get("child") is not None  # both rows intact
    out = capsys.readouterr().out
    assert "MASS-CLOSE" in out and "--yes" in out and "child" in out  # list-what-dies + how to confirm


def test_rm_with_group_yes_bypasses_gate(monkeypatch):
    from cmux_fleet import state as fs
    _seed_cond_child(fs)
    calls = _dissolve_stubs(monkeypatch, live_surfaces={"SC", "SW"},
                            member_refs={"workspace:c": "WS-C", "workspace:w": "WS-W"})
    fleet.cmd_rm(["cond", "--with-group", "--yes"])                 # explicit confirmation
    assert ("workspace-group", "delete", "workspace_group:7") in calls   # dissolved
    assert fs.live_get("cond") is None and fs.live_get("child") is None  # swept


def test_rm_with_group_no_gate_when_collateral_is_stale(monkeypatch):
    # a member whose surface is NOT live is not surprise collateral -> proceed without --yes.
    from cmux_fleet import state as fs
    _seed_cond_child(fs)
    calls = _dissolve_stubs(monkeypatch, live_surfaces={"SC"},       # child (SW) is stale
                            member_refs={"workspace:c": "WS-C", "workspace:w": "WS-W"})
    fleet.cmd_rm(["cond", "--with-group"])                          # no --yes needed
    assert ("workspace-group", "delete", "workspace_group:7") in calls
    assert fs.live_get("cond") is None and fs.live_get("child") is None


def test_rm_with_group_solo_target_no_gate(monkeypatch):
    # a group with only the named target (no members) is no surprise -> no --yes needed.
    from cmux_fleet import state as fs
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "group": "g",
                         "surface": "SC", "workspace": "WS-C", "status": "live"})
    calls = _dissolve_stubs(monkeypatch, live_surfaces={"SC"}, member_refs={"workspace:c": "WS-C"})
    fleet.cmd_rm(["cond", "--with-group"])
    assert ("workspace-group", "delete", "workspace_group:7") in calls
    assert fs.live_get("cond") is None


def test_rm_with_group_confirm_alias_also_bypasses(monkeypatch):
    from cmux_fleet import state as fs
    _seed_cond_child(fs)
    calls = _dissolve_stubs(monkeypatch, live_surfaces={"SC", "SW"},
                            member_refs={"workspace:c": "WS-C", "workspace:w": "WS-W"})
    fleet.cmd_rm(["cond", "--with-group", "--confirm"])             # --confirm is the --yes alias
    assert ("workspace-group", "delete", "workspace_group:7") in calls
    assert fs.live_get("cond") is None and fs.live_get("child") is None


# --- with-group dissolve adopts the kill path (the last leak site): stop EVERY member, all-or-nothing.
# `workspace-group delete` closes every member surface and close-surface does not kill the pane's agent,
# so a dissolve without stops leaked every live member at once. The invariant: any member whose live
# agent won't die (or can't be identified) refuses the WHOLE dissolve — never strand one agent while
# tearing down its neighbours.
def _seed_group(fs):
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "group": "g",
                         "surface": "SC", "workspace": "WS-C", "status": "live"})
    fs.live_put("child", {"role": "w", "kind": "child", "tool": "claude", "group": "g",
                          "parent": "cond", "surface": "SW", "workspace": "WS-W", "status": "live"})


def _stub_group_cmux(monkeypatch, calls):
    def fake_cmuxq(*a):
        calls.append(a)
        if a[:2] == ("workspace-group", "list"):
            return ('{"groups":[{"ref":"workspace_group:1",'
                    '"member_workspace_refs":["workspace:c","workspace:w"]}]}')
        return ""
    monkeypatch.setattr(fleet, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:1")
    monkeypatch.setattr(fleet, "_ref_to_uuid",
                        lambda kind, ref: {"workspace:c": "WS-C", "workspace:w": "WS-W"}[ref])
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "_STOP_WAIT_S", 0.05)


def test_with_group_all_dead_members_dissolves(fs, monkeypatch):
    # (a) nothing live anywhere (only dead-pid ghosts on the member surfaces) -> the dissolve proceeds.
    _seed_group(fs)
    calls = []
    _stub_group_cmux(monkeypatch, calls)
    monkeypatch.setattr(fleet, "_surface_pids", lambda s: set())          # ghosts are dead = no targets
    assert fleet.cmd_rm(["cond", "--with-group"]) == 0
    assert any(c[:2] == ("workspace-group", "delete") for c in calls)     # dissolved
    assert fs.live_get("cond") is None and fs.live_get("child") is None   # swept


def test_with_group_one_survivor_refuses_whole_dissolve(fs, monkeypatch):
    # (b) the cond's agent dies on SIGINT; the child's SURVIVES -> the WHOLE dissolve refuses: no group
    # delete, zero surfaces closed, registry intact, and the blocking member is named for the operator.
    _seed_group(fs)
    calls = []
    _stub_group_cmux(monkeypatch, calls)
    alive = {111: True, 222: True}                                        # cond pid 111, child pid 222
    monkeypatch.setattr(fleet, "_surface_pids",
                        lambda s: ({111} if s == "SC" and alive[111] else set()) |
                                  ({222} if s == "SW" and alive[222] else set()))
    monkeypatch.setattr(fleet, "_agent_pid_check", lambda pid, tool: True)
    def fake_kill(pid, sig):
        if pid == 111:
            alive[111] = False                                            # cond exits cleanly
    monkeypatch.setattr(fleet.os, "kill", fake_kill)                      # 222 SURVIVES the SIGINTs
    rc = fleet.cmd_rm(["cond", "--with-group", "--force"])                # --force must NOT bypass
    assert rc == 1
    assert not any(c[:2] == ("workspace-group", "delete") for c in calls)  # group intact
    assert not any(c[0] == "close-surface" for c in calls)                 # zero surfaces closed
    assert fs.live_get("cond") is not None and fs.live_get("child") is not None   # registry untouched


def test_with_group_one_survivor_names_the_blocker(fs, monkeypatch, capsys):
    _seed_group(fs)
    calls = []
    _stub_group_cmux(monkeypatch, calls)
    monkeypatch.setattr(fleet, "_surface_pids", lambda s: {222} if s == "SW" else set())
    monkeypatch.setattr(fleet, "_agent_pid_check", lambda pid, tool: True)
    monkeypatch.setattr(fleet.os, "kill", lambda pid, sig: None)          # the child never dies
    assert fleet.cmd_rm(["cond", "--with-group"]) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "child:" in out                           # the operator knows which seat


def test_with_group_unidentifiable_pid_refuses_with_zero_signals(fs, monkeypatch):
    # (c) PRE-FLIGHT: a live pid on ANY member that doesn't identify as its tool refuses the whole
    # dissolve before a single SIGINT is fired anywhere in the group (never half-kill then discover).
    _seed_group(fs)
    calls = []
    _stub_group_cmux(monkeypatch, calls)
    monkeypatch.setattr(fleet, "_surface_pids", lambda s: {333} if s == "SW" else set())
    monkeypatch.setattr(fleet, "_agent_pid_check", lambda pid, tool: False)
    killed = []
    monkeypatch.setattr(fleet.os, "kill", lambda pid, sig: killed.append(pid))
    assert fleet.cmd_rm(["cond", "--with-group"]) == 1
    assert killed == []                                                    # ZERO signals fired
    assert not any(c[:2] == ("workspace-group", "delete") for c in calls)
    assert not any(c[0] == "close-surface" for c in calls)
    assert fs.live_get("cond") is not None and fs.live_get("child") is not None
