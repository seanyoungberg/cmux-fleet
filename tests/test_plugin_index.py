"""Layer 3 — the plugin INDEX spine (plugins.toml) + index-aware `use` resolution.

Phase-1 build of the plugin-management redesign (design §2b/§2c/§3). Everything here is exercised
through the real `fleet` CLI's `--dry-run` compose path (same harness as test_e2e_cli.py) against
SCRATCH tomls — the live ~/.config/cmux-fleet/fleet.toml and prod state are never touched.

Coverage:
  - a `use` list (linked + enabled) composes BOTH native channels in one command (design receipt #1/#2)
  - multi-marketplace: a plugin from each of two [marketplace.*] resolves to the right --plugin-dir
  - a [plugin.<n>.<tool>] per-tool block parses + is retrievable (no crash, stored)
  - an unindexed `use` name falls back to today's behavior (abs-path as-is / bare name under default mkt)
  - THE CRITICAL ONE: a legacy roster (plugins/enable_plugins/marketplace) composes BYTE-IDENTICALLY
    whether or not an (unrelated) plugins.toml is present — the additive change didn't move legacy comp.
"""
import os

from test_e2e_cli import run_fleet


# --- fixtures-as-helpers -------------------------------------------------------------------------
def _mkplugin(mkt_dir, name):
    """Create an (empty) plugin dir under a marketplace. Resolution only checks os.path.exists, so an
    empty dir is enough to exercise --plugin-dir composition without a real plugin checkout."""
    p = os.path.join(mkt_dir, name)
    os.makedirs(p, exist_ok=True)
    return p


def _launch_argv(stdout):
    """Extract the composed tool argv (the `claude ...` tail) from a `[fleet] launch:` dry-run line."""
    line = next(l for l in stdout.splitlines() if l.startswith("[fleet] launch:"))
    return line[line.index(" claude ") + 1:]


def _env(cli_env, tmp_path, *, index=None, marketplace=None):
    e = {**cli_env, "CMUX_FLEET_TOML": str(tmp_path / "fleet.toml")}
    # Keep the index pointer explicit + hermetic (never fall back to the default <toml-dir>/plugins.toml
    # unless a test wants that). Point at an absent path when a test wants NO index.
    e["CMUX_FLEET_PLUGIN_INDEX"] = str(index) if index is not None else str(tmp_path / "__no_index__.toml")
    if marketplace is not None:
        e["CMUX_FLEET_MARKETPLACE"] = str(marketplace)
    else:
        e.pop("CMUX_FLEET_MARKETPLACE", None)
    return e


# --- (a) use = linked + enabled composes both channels in ONE command (receipt #1/#2) ------------
def test_use_linked_and_enabled_compose_together(cli_env, tmp_path):
    berg = tmp_path / "mkt-berg"
    cmux_fleet_dir = _mkplugin(berg, "cmux-fleet")
    memsearch_dir = _mkplugin(berg, "memsearch")
    (tmp_path / "plugins.toml").write_text(
        '[marketplace.berg]\npath = "mkt-berg"\n'
        '[marketplace.obs]\nkind = "global"\n'
        '[plugin.cmux-fleet]\ntype = "linked"\nsource = "berg"\ntools = ["claude"]\n'
        '[plugin.memsearch]\ntype = "linked"\nsource = "berg"\ntools = ["claude","codex"]\n'
        '[plugin.obsidian]\ntype = "enabled"\nsource = "obs"\ninstall = "global-disabled"\ntools = ["claude"]\n')
    (tmp_path / "fleet.toml").write_text(
        '[tool.claude]\nuse = ["cmux-fleet"]\n'                       # floor use
        '[role.researcher]\nkind = "child"\ncwd = "workers/r"\n'
        '[role.researcher.claude]\nuse = ["memsearch", "obsidian"]\n')  # role use (unioned onto floor)
    env = _env(cli_env, tmp_path, index=tmp_path / "plugins.toml")
    p = run_fleet(env, "launch", "researcher", "--label", "r1", "--parent", "FAKE", "--dry-run")
    argv = _launch_argv(p.stdout)
    # linked channel: floor ∪ role, both from the berg marketplace
    assert f"--plugin-dir {cmux_fleet_dir}" in argv
    assert f"--plugin-dir {memsearch_dir}" in argv
    # enabled channel: obsidian flipped on via the merged --settings enabledPlugins
    assert '"enabledPlugins": {"obsidian@obs": true}' in argv
    # the two channels are DISTINCT: obsidian is NOT a --plugin-dir; cmux-fleet is NOT in enabledPlugins
    assert "--plugin-dir" not in argv.split('"enabledPlugins"')[1]  # nothing after the settings blob
    assert "cmux-fleet@" not in argv


# --- (b) multi-marketplace: a plugin from each of two marketplaces resolves correctly ------------
def test_multi_marketplace_linked_paths(cli_env, tmp_path):
    one, two = tmp_path / "mkt-one", tmp_path / "mkt-two"
    a_dir = _mkplugin(one, "alpha")
    b_dir = _mkplugin(two, "beta")
    (tmp_path / "plugins.toml").write_text(
        '[marketplace.one]\npath = "mkt-one"\n'
        '[marketplace.two]\npath = "mkt-two"\n'
        '[plugin.alpha]\ntype = "linked"\nsource = "one"\n'
        '[plugin.beta]\ntype = "linked"\nsource = "two"\n')
    (tmp_path / "fleet.toml").write_text(
        '[role.w]\nkind = "child"\ncwd = "w"\n'
        '[role.w.claude]\nuse = ["alpha", "beta"]\n')
    env = _env(cli_env, tmp_path, index=tmp_path / "plugins.toml")
    p = run_fleet(env, "launch", "w", "--label", "w1", "--parent", "FAKE", "--dry-run")
    argv = _launch_argv(p.stdout)
    assert f"--plugin-dir {a_dir}" in argv            # from marketplace `one`
    assert f"--plugin-dir {b_dir}" in argv            # from marketplace `two` — proves >1 marketplace
    assert a_dir != b_dir and one != two


# --- (c) a per-tool [plugin.<n>.<tool>] block parses + is retrievable (stored, no crash) ---------
def test_per_tool_block_parses_and_is_stored(cli_env, tmp_path):
    # In-process: this is a pure loader assertion (does the schema EXPRESS codex per-tool overrides?).
    from cmux_fleet import config
    idx_path = tmp_path / "plugins.toml"
    idx_path.write_text(
        '[plugin.memsearch]\ntype = "linked"\nsource = "berg"\ntools = ["claude","codex"]\n'
        'description = "memory"\n'
        '[plugin.memsearch.codex]\nnotes = "reads hooks/codex-hooks.json"\nhook = "codex-hooks.json"\n')
    idx = config.load_plugin_index(str(idx_path))
    ms = idx["plugins"]["memsearch"]
    assert ms["type"] == "linked" and ms["source"] == "berg"
    assert ms["tools"] == ["claude", "codex"]          # tools list expressible
    assert ms["description"] == "memory"
    # the per-tool override block is parsed + retrievable under tool_overrides (Phase 1: stored, not consumed)
    assert ms["tool_overrides"]["codex"]["notes"] == "reads hooks/codex-hooks.json"
    assert ms["tool_overrides"]["codex"]["hook"] == "codex-hooks.json"


# --- (d) an unindexed `use` name falls back to today's behavior (back-compat fall-through) -------
def test_unindexed_use_falls_back_to_abspath_and_default_marketplace(cli_env, tmp_path):
    mkt = tmp_path / "default-mkt"
    bare_dir = _mkplugin(mkt, "bareplug")            # a bare name resolvable under the default marketplace
    abs_dir = tmp_path / "loose" / "abs-plug"
    abs_dir.mkdir(parents=True)
    # plugins.toml exists but does NOT list these names -> they take the not-in-index fall-through
    (tmp_path / "plugins.toml").write_text('[plugin.somethingelse]\ntype = "linked"\nsource = "x"\n')
    (tmp_path / "fleet.toml").write_text(
        '[role.w]\nkind = "child"\ncwd = "w"\n'
        f'[role.w.claude]\nuse = ["bareplug", "{abs_dir}"]\n')
    env = _env(cli_env, tmp_path, index=tmp_path / "plugins.toml", marketplace=mkt)
    p = run_fleet(env, "launch", "w", "--label", "w1", "--parent", "FAKE", "--dry-run")
    argv = _launch_argv(p.stdout)
    assert f"--plugin-dir {bare_dir}" in argv          # bare name -> default marketplace (today's path)
    assert f"--plugin-dir {abs_dir}" in argv           # abs path -> used as-is (today's bypass)


def test_absent_index_is_empty_not_error(cli_env, tmp_path):
    # No plugins.toml at all -> empty index, no error; an unindexed `use` bare name with no marketplace
    # simply warns + skips (exactly the legacy --plugin-dir loop behavior).
    (tmp_path / "fleet.toml").write_text(
        '[role.w]\nkind = "child"\ncwd = "w"\n[role.w.claude]\nuse = ["ghost"]\n')
    env = _env(cli_env, tmp_path)                      # index points at an absent file
    p = run_fleet(env, "launch", "w", "--label", "w1", "--parent", "FAKE", "--dry-run")
    assert "dry-run" in p.stdout.lower()
    assert "ghost" in p.stdout and "not resolvable" in p.stdout   # warned + skipped, no crash


def test_fleet_plugin_index_pointer(cli_env, tmp_path):
    # [fleet].plugin_index overrides the default location (env > [fleet].plugin_index > <toml-dir>/plugins.toml).
    mkt = tmp_path / "m"
    d = _mkplugin(mkt, "p")
    (tmp_path / "custom-index.toml").write_text(
        '[marketplace.m]\npath = "m"\n[plugin.p]\ntype = "linked"\nsource = "m"\n')
    (tmp_path / "fleet.toml").write_text(
        '[fleet]\nplugin_index = "custom-index.toml"\n'          # relative -> anchors to the toml's dir
        '[role.w]\nkind="child"\ncwd="w"\n[role.w.claude]\nuse=["p"]\n')
    env = {**cli_env, "CMUX_FLEET_TOML": str(tmp_path / "fleet.toml")}
    env.pop("CMUX_FLEET_PLUGIN_INDEX", None)                    # no env override -> the [fleet] pointer wins
    env.pop("CMUX_FLEET_MARKETPLACE", None)
    p = run_fleet(env, "launch", "w", "--label", "w1", "--parent", "FAKE", "--dry-run")
    assert f"--plugin-dir {d}" in _launch_argv(p.stdout)


def test_default_index_location_next_to_fleet_toml(cli_env, tmp_path):
    # The [fleet].plugin_index default is <toml-dir>/plugins.toml — drop one next to fleet.toml and it
    # is picked up WITHOUT an explicit CMUX_FLEET_PLUGIN_INDEX.
    mkt = tmp_path / "m"
    d = _mkplugin(mkt, "p")
    (tmp_path / "plugins.toml").write_text(
        '[marketplace.m]\npath = "m"\n[plugin.p]\ntype = "linked"\nsource = "m"\n')
    (tmp_path / "fleet.toml").write_text('[role.w]\nkind="child"\ncwd="w"\n[role.w.claude]\nuse=["p"]\n')
    env = {**cli_env, "CMUX_FLEET_TOML": str(tmp_path / "fleet.toml")}
    env.pop("CMUX_FLEET_PLUGIN_INDEX", None)           # rely on the <toml-dir>/plugins.toml default
    env.pop("CMUX_FLEET_MARKETPLACE", None)
    p = run_fleet(env, "launch", "w", "--label", "w1", "--parent", "FAKE", "--dry-run")
    assert f"--plugin-dir {d}" in _launch_argv(p.stdout)


# --- THE CRITICAL BACK-COMPAT TEST: legacy composition is byte-identical, index present or not ----
def test_legacy_composition_unchanged_by_index(cli_env, tmp_path):
    """The live fleet.toml uses plugins/enable_plugins/marketplace (NO `use`). Prove the additive index
    machinery does not alter its composition: the composed claude argv must be byte-identical whether an
    (unrelated) plugins.toml exists or not. This is the load-bearing back-compat guarantee."""
    mkt = tmp_path / "legacy-mkt"
    _mkplugin(mkt, "legacyplug")
    # a roster shaped exactly like the current live fleet.toml: legacy keys only, no `use` anywhere
    (tmp_path / "fleet.toml").write_text(
        '[tool.claude]\nplugins = ["legacyplug"]\n'
        '[role.worker]\nkind = "child"\ncwd = "workers/w"\n'
        '[role.worker.claude]\nplugins = ["legacyplug"]\nenable_plugins = ["ext@berg-plugins"]\n'
        'setting_sources = "user,local"\n')
    # an unrelated index that the legacy roster never references
    (tmp_path / "plugins.toml").write_text(
        '[marketplace.berg]\npath = "elsewhere"\n'
        '[plugin.unused]\ntype = "linked"\nsource = "berg"\ntools = ["claude"]\n')

    with_index = _env(cli_env, tmp_path, index=tmp_path / "plugins.toml", marketplace=mkt)
    without_index = _env(cli_env, tmp_path, marketplace=mkt)   # index -> absent path

    a = _launch_argv(run_fleet(with_index, "launch", "worker", "--label", "w1", "--parent", "FAKE", "--dry-run").stdout)
    b = _launch_argv(run_fleet(without_index, "launch", "worker", "--label", "w1", "--parent", "FAKE", "--dry-run").stdout)
    assert a == b, f"index presence changed legacy composition:\n WITH: {a}\n WITHOUT: {b}"
    # and the legacy channels are exactly what they were pre-change
    assert f"--plugin-dir {os.path.join(str(mkt), 'legacyplug')}" in a
    assert '"enabledPlugins": {"ext@berg-plugins": true}' in a
    assert "--setting-sources user,local" in a
