# tests/test_mint.py — `fleet mint`: the agent-definition / mint verb (the fix for the
# can't-spawn-a-new-top-level-conductor gap). Pure units — `load_config` is stubbed so no test reads the
# host's real ~/.config/cmux-fleet/fleet.toml, and both ROOT and FLEET_TOML are repointed into tmp_path so
# the real side effects (home-dir creation, identity seed, roster append) land in a throwaway.
#
# Covers all three ratified forks: A (append-only text roster write), B (thin identity seed), C (define-core
# + --launch opt-in delegating to cmd_launch). load_config is stubbed for the dup-check; the APPEND is
# verified by re-parsing the written file with tomllib (the effect, not the artifact).
import os
import tomllib

import pytest

from cmux_fleet import cli as fleet


def _stub_cfg(monkeypatch, roles=None, default_tool="claude"):
    """Pin the roster mint reads. Keeps tests off the host fleet.toml (hermeticity, same as test_register)."""
    cfg = {"defaults": {"tool": default_tool}, "role": roles or {}}
    monkeypatch.setattr(fleet, "load_config", lambda: cfg)


def _roster(monkeypatch, tmp_path, text="[fleet]\nroot = \".\"\n\n[role.cmux-advisor]\nkind = \"conductor\"\ncwd = \"x\"\n"):
    """Point FLEET_TOML + ROOT at a throwaway roster and return its path. mint APPENDS to this file."""
    toml = tmp_path / "fleet.toml"
    toml.write_text(text)
    monkeypatch.setattr(fleet, "FLEET_TOML", str(toml))
    monkeypatch.setattr(fleet, "ROOT", str(tmp_path))
    return toml


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


# --- real run (define): home dir + identity seed + roster append ----------------------------------
def test_define_creates_home_seeds_stub_and_appends_role(monkeypatch, tmp_path, capsys):
    _stub_cfg(monkeypatch)
    toml = _roster(monkeypatch, tmp_path)
    rc = fleet.cmd_mint(["payments", "--kind", "conductor"])
    out = capsys.readouterr().out
    assert rc == 0
    home = tmp_path / "_meta/agents/conductors/payments"
    assert home.is_dir()                                             # home dir created
    stub = (home / "CLAUDE.md").read_text()                         # thin identity stub seeded
    assert "**payments**" in stub and "/loom:prime" in stub and "conductor" in stub
    # the APPEND is real + parseable — verify the EFFECT by re-parsing the written roster (Fork A)
    doc = tomllib.loads(toml.read_text())
    assert doc["role"]["payments"]["kind"] == "conductor"
    assert doc["role"]["payments"]["group"] == "Conductor - payments"
    assert doc["role"]["payments"]["cwd"] == "_meta/agents/conductors/payments"
    assert "claude" in doc["role"]["payments"]                      # empty [role.payments.claude] sub-block
    assert "defined (not launched)" in out


def test_append_is_append_only_preserving_prior_content(monkeypatch, tmp_path):
    """Fork A's core guarantee: hand-authored roles + comments survive byte-for-byte; we only add."""
    _stub_cfg(monkeypatch)
    original = ("[fleet]\nroot = \".\"  # a hand comment\n\n"
                "[role.cmux-advisor]  # keep me\nkind = \"conductor\"\ncwd = \"x\"\n")
    toml = _roster(monkeypatch, tmp_path, text=original)
    fleet.cmd_mint(["widget"])
    after = toml.read_text()
    assert after.startswith(original)                               # every prior byte is intact, in place
    assert "# a hand comment" in after and "# keep me" in after     # comments preserved
    assert "# minted by `fleet mint`" in after and "[role.widget]" in after


def test_no_roster_file_refuses_with_seed_hint(monkeypatch, tmp_path):
    _stub_cfg(monkeypatch)
    monkeypatch.setattr(fleet, "ROOT", str(tmp_path))
    monkeypatch.setattr(fleet, "FLEET_TOML", str(tmp_path / "does-not-exist.toml"))
    with pytest.raises(SystemExit) as ei:
        fleet.cmd_mint(["widget"])
    assert "no roster" in str(ei.value)


def test_identity_seed_never_clobbers_existing_claudemd(monkeypatch, tmp_path):
    _stub_cfg(monkeypatch)
    _roster(monkeypatch, tmp_path)
    home = tmp_path / "_meta/agents/widget"
    home.mkdir(parents=True)
    (home / "CLAUDE.md").write_text("HAND AUTHORED — do not touch\n")
    fleet.cmd_mint(["widget"])
    assert (home / "CLAUDE.md").read_text() == "HAND AUTHORED — do not touch\n"


def test_identity_template_config_override(monkeypatch, tmp_path):
    _stub_cfg(monkeypatch)
    _roster(monkeypatch, tmp_path)
    tmpl = tmp_path / "id.tmpl"
    tmpl.write_text("ROLE={name} KIND={kind}\n")
    monkeypatch.setattr(fleet, "MINT_IDENTITY_TEMPLATE", str(tmpl))
    fleet.cmd_mint(["widget"])
    seeded = (tmp_path / "_meta/agents/widget/CLAUDE.md").read_text()
    assert seeded == "ROLE=widget KIND=child\n"


# --- launch opt-in (Fork C): delegate to the existing cmd_launch -----------------------------------
def test_default_is_define_only_no_launch(monkeypatch, tmp_path):
    _stub_cfg(monkeypatch)
    _roster(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(fleet, "cmd_launch", lambda argv: called.append(argv))
    fleet.cmd_mint(["widget"])
    assert called == []                                             # define-only default never launches


def test_launch_flag_delegates_to_cmd_launch(monkeypatch, tmp_path):
    _stub_cfg(monkeypatch)
    _roster(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(fleet, "cmd_launch", lambda argv: called.append(argv) or 0)
    rc = fleet.cmd_mint(["payments", "--kind", "conductor", "--launch", "--", "--model", "opus"])
    assert rc == 0
    # mint builds the launch argv (conductor = top-level) + forwards the `--` passthrough; cmd_launch owns
    # the actual group-join + spawn (mint never name-keys a group itself).
    assert called == [["payments", "--parent", "none", "--place", "workspace", "--", "--model", "opus"]]


def test_dry_run_launch_previews_argv_without_calling(monkeypatch, tmp_path, capsys):
    _stub_cfg(monkeypatch)
    monkeypatch.setattr(fleet, "ROOT", str(tmp_path))
    called = []
    monkeypatch.setattr(fleet, "cmd_launch", lambda argv: called.append(argv))
    fleet.cmd_mint(["payments", "--kind", "conductor", "--launch", "--dry-run"])
    out = capsys.readouterr().out
    assert called == [] and "would run:  fleet launch payments --parent none --place workspace" in out
