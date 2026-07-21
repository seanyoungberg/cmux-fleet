# tests/test_mint.py — `fleet mint`: the agent-definition / mint scaffold (the fix for the
# can't-spawn-a-new-top-level-conductor gap). Pure units — `load_config` is stubbed so no test reads the
# host's real ~/.config/cmux-fleet/fleet.toml, and ROOT is repointed at tmp_path so the one real side
# effect (home-dir creation) lands in a throwaway.
#
# SCAFFOLD SCOPE: the two forky steps (the fleet.toml roster append + the identity seed) are held for
# design sign-off and are NOT wired, so these tests pin ONLY what the scaffold does: name validation,
# duplicate refusal, convention resolution, block/argv rendering, and home-dir creation.
import os

import pytest

from cmux_fleet import cli as fleet


def _stub_cfg(monkeypatch, roles=None, default_tool="claude"):
    """Pin the roster mint reads. Keeps tests off the host fleet.toml (hermeticity, same as test_register)."""
    cfg = {"defaults": {"tool": default_tool}, "role": roles or {}}
    monkeypatch.setattr(fleet, "load_config", lambda: cfg)


# --- pure convention helpers ----------------------------------------------------------------------
def test_conductor_conventions_own_group_and_conductor_home():
    orch = fleet._mint_conventions("payments", "conductor")
    assert orch == {"kind": "conductor", "place": "workspace",
                    "group": "Conductor - payments", "cwd": "_meta/agents/conductors/payments"}


def test_child_conventions_shared_group_and_flat_home():
    orch = fleet._mint_conventions("widget", "child")
    # child joins the parent's group at launch -> no own group; home under _meta/agents/<name>
    assert orch["kind"] == "child" and orch["group"] == "" and orch["cwd"] == "_meta/agents/widget"


def test_render_block_omits_default_child_kind():
    orch = fleet._mint_conventions("widget", "child")
    block = fleet._render_role_block("widget", orch, "claude")
    assert "[role.widget]" in block and "[role.widget.claude]" in block
    assert 'cwd   = "_meta/agents/widget"' in block
    assert "kind" not in block and "group" not in block          # child kind + empty group are omitted


def test_render_block_conductor_carries_kind_and_group():
    orch = fleet._mint_conventions("payments", "conductor")
    block = fleet._render_role_block("payments", orch, "claude")
    assert 'kind  = "conductor"' in block
    assert 'group = "Conductor - payments"' in block
    assert '[role.payments.claude]' in block


def test_launch_argv_conductor_is_top_level():
    orch = fleet._mint_conventions("payments", "conductor")
    assert fleet._mint_launch_argv("payments", orch) == \
        ["payments", "--parent", "none", "--place", "workspace"]


def test_launch_argv_child_places_workspace():
    orch = fleet._mint_conventions("widget", "child")
    assert fleet._mint_launch_argv("widget", orch) == ["widget", "--place", "workspace"]


# --- name validation ------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["Payments", "pay_ments", "-pay", "pay-", "pay--x", "1pay", "pay.x", ""])
def test_invalid_names_refused(monkeypatch, bad):
    _stub_cfg(monkeypatch)
    with pytest.raises(SystemExit):
        fleet.cmd_mint([bad, "--dry-run"])


def test_duplicate_role_refused(monkeypatch, capsys):
    _stub_cfg(monkeypatch, roles={"cmux-dev": {"cwd": "x"}})
    with pytest.raises(SystemExit) as ei:
        fleet.cmd_mint(["cmux-dev", "--dry-run"])
    assert "already exists" in str(ei.value)


# --- dry-run previews, no side effects ------------------------------------------------------------
def test_dry_run_previews_block_and_writes_nothing(monkeypatch, tmp_path, capsys):
    _stub_cfg(monkeypatch)
    monkeypatch.setattr(fleet, "ROOT", str(tmp_path))
    rc = fleet.cmd_mint(["payments", "--kind", "conductor", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[role.payments]" in out and 'kind  = "conductor"' in out
    assert "dry-run — nothing written" in out
    assert not os.path.exists(tmp_path / "_meta/agents/conductors/payments")   # dry-run makes no dir


# --- real run: the one sanctioned side effect is the home dir -------------------------------------
def test_real_run_creates_home_dir_but_holds_roster_write(monkeypatch, tmp_path, capsys):
    _stub_cfg(monkeypatch)
    monkeypatch.setattr(fleet, "ROOT", str(tmp_path))
    rc = fleet.cmd_mint(["payments", "--kind", "conductor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert (tmp_path / "_meta/agents/conductors/payments").is_dir()   # home dir created
    assert "HELD for design sign-off" in out                          # roster write not wired


def test_cwd_override_and_absolute_home(monkeypatch, tmp_path, capsys):
    _stub_cfg(monkeypatch)
    abs_home = tmp_path / "custom-home"
    rc = fleet.cmd_mint(["widget", "--cwd", str(abs_home)])
    assert rc == 0 and abs_home.is_dir()


def test_launch_flag_previews_but_holds(monkeypatch, tmp_path, capsys):
    _stub_cfg(monkeypatch)
    monkeypatch.setattr(fleet, "ROOT", str(tmp_path))
    rc = fleet.cmd_mint(["payments", "--kind", "conductor", "--launch"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "would run:  fleet launch payments --parent none --place workspace" in out
    assert "launch is HELD" in out
