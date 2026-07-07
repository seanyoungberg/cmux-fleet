"""Layer 3 — the dynamic `--plugin` verb on LAUNCH and RECYCLE (design §5a).

`--plugin` is the ONE index-aware plugin-add flag (it replaced the split `--use` / `--plugins` / `--add-plugin`
surfaces). Because it routes through the index (`_resolve_plugins`), one flag reaches BOTH plugin types
automatically: a `linked` name composes an extra `--plugin-dir`, an `enabled` name composes an extra
`enabledPlugins` entry. A name NOT in the index loads as a linked `--plugin-dir` (default marketplace /
absolute path).

All exercised through the real `fleet` CLI's `--dry-run` compose path against SCRATCH tomls + a seeded
throwaway registry — the live ~/.config/cmux-fleet/fleet.toml and prod state are never touched. The
recycle tests seed a LIVE registry entry (a real recycle needs a live cmux) and drive `recycle --dry-run`,
which re-resolves the roster toml exactly like a real recycle and prints the composed command.

Coverage:
  launch  --plugin <linked>            -> the right --plugin-dir
  launch  --plugin <enabled>           -> the right enabledPlugins entry
  launch  --plugin + role `plugins`    -> union + dedupe, both channels in one command
  launch  --plugin <unindexed>         -> linked fall-back (abs-path / default marketplace)
  launch  --plugin shape               -> repeatable AND comma-sep
  launch  --plugin spans channels      -> linked + enabled + unindexed compose in one command
  recycle --plugin <linked>            -> --plugin-dir on the roster re-resolve
  recycle --plugin <enabled>           -> enabledPlugins entry (reaches the enabled channel on recycle)
  recycle --plugin <linked>+<enabled>  -> BOTH channels compose in one recycled command
"""
import os

from test_e2e_cli import run_fleet
from cmux_fleet import cli as fleet


# --- fixtures-as-helpers (mirror test_plugin_index) ----------------------------------------------
def _mkplugin(mkt_dir, name):
    """An (empty) plugin dir under a marketplace. Resolution only checks os.path.exists, so an empty dir
    is enough to exercise --plugin-dir composition without a real plugin checkout."""
    p = os.path.join(mkt_dir, name)
    os.makedirs(p, exist_ok=True)
    return p


def _launch_argv(stdout):
    """The composed tool argv (the `claude ...` tail) from a `[fleet] launch:` dry-run line (launch AND
    recycle both print `[fleet] launch: cd <cwd> && ... claude ...`)."""
    line = next(l for l in stdout.splitlines() if l.startswith("[fleet] launch:"))
    return line[line.index(" claude ") + 1:]


def _env(cli_env, tmp_path, *, index=None):
    e = {**cli_env, "CMUX_FLEET_TOML": str(tmp_path / "fleet.toml")}
    # Hermetic index pointer: never fall back to the default <toml-dir>/plugins.toml unless a test wants it.
    e["CMUX_FLEET_PLUGIN_INDEX"] = str(index) if index is not None else str(tmp_path / "__no_index__.toml")
    return e


def _index_two_plugins(tmp_path):
    """A scratch plugins.toml with one linked + one enabled plugin, and the linked one's dir on disk.
    Returns (env-ready index path, linked_dir)."""
    berg = tmp_path / "mkt-berg"
    linked_dir = _mkplugin(berg, "memsearch")
    (tmp_path / "plugins.toml").write_text(
        '[marketplace.berg]\npath = "mkt-berg"\n'
        '[marketplace.obs]\nkind = "global"\n'
        '[plugin.memsearch]\ntype = "linked"\nsource = "berg"\ntools = ["claude","codex"]\n'
        '[plugin.obsidian]\ntype = "enabled"\nsource = "obs"\ninstall = "global-disabled"\ntools = ["claude"]\n')
    return tmp_path / "plugins.toml", linked_dir


# =================================================================================================
# LAUNCH --plugin
# =================================================================================================
def test_launch_plugin_linked_adds_plugin_dir(cli_env, tmp_path):
    index, linked_dir = _index_two_plugins(tmp_path)
    (tmp_path / "fleet.toml").write_text('[role.w]\nkind = "child"\ncwd = "w"\n[role.w.claude]\n')
    env = _env(cli_env, tmp_path, index=index)
    p = run_fleet(env, "launch", "w", "--label", "w1", "--parent", "FAKE", "--plugin", "memsearch", "--dry-run")
    argv = _launch_argv(p.stdout)
    assert f"--plugin-dir {linked_dir}" in argv        # linked -> --plugin-dir, routed through the index
    assert "enabledPlugins" not in argv                # a linked add never touches the enabled channel


def test_launch_plugin_enabled_adds_enabledplugins(cli_env, tmp_path):
    # An enabled plugin composes an enabledPlugins entry (the index picks the channel; no --plugin-dir).
    index, _ = _index_two_plugins(tmp_path)
    (tmp_path / "fleet.toml").write_text('[role.w]\nkind = "child"\ncwd = "w"\n[role.w.claude]\n')
    env = _env(cli_env, tmp_path, index=index)
    p = run_fleet(env, "launch", "w", "--label", "w1", "--parent", "FAKE", "--plugin", "obsidian", "--dry-run")
    argv = _launch_argv(p.stdout)
    assert '"enabledPlugins": {"obsidian@obs": true}' in argv   # enabled -> enabledPlugins via --settings
    assert "--plugin-dir" not in argv                            # enabled is NOT a --plugin-dir


def test_launch_plugin_unions_and_dedupes_with_role_plugins(cli_env, tmp_path):
    # `--plugin` unions onto the ROLE's own `plugins`; a name already in the role is deduped (not doubled),
    # and a DISTINCT --plugin name adds its channel. Proves both channels coexist in one composed command.
    index, linked_dir = _index_two_plugins(tmp_path)
    (tmp_path / "fleet.toml").write_text(
        '[role.w]\nkind = "child"\ncwd = "w"\n'
        '[role.w.claude]\nplugins = ["memsearch"]\n')          # role already links memsearch
    env = _env(cli_env, tmp_path, index=index)
    # --plugin memsearch (dup of the role) + --plugin obsidian (new enabled)
    p = run_fleet(env, "launch", "w", "--label", "w1", "--parent", "FAKE",
                  "--plugin", "memsearch", "--plugin", "obsidian", "--dry-run")
    argv = _launch_argv(p.stdout)
    assert argv.count(f"--plugin-dir {linked_dir}") == 1        # deduped: memsearch appears exactly ONCE
    assert '"enabledPlugins": {"obsidian@obs": true}' in argv   # obsidian added onto the role's loadout


def test_launch_plugin_unindexed_abspath_resolves_bare_skips(cli_env, tmp_path):
    # There is NO implicit default marketplace: an unindexed ABSOLUTE path still loads (used as-is), but an
    # unindexed BARE name has no marketplace to resolve under, so it is warned + skipped.
    abs_dir = tmp_path / "loose" / "abs-plug"
    abs_dir.mkdir(parents=True)
    (tmp_path / "plugins.toml").write_text('[plugin.somethingelse]\ntype = "linked"\nsource = "x"\n')
    (tmp_path / "fleet.toml").write_text('[role.w]\nkind = "child"\ncwd = "w"\n[role.w.claude]\n')
    env = _env(cli_env, tmp_path, index=tmp_path / "plugins.toml")
    p = run_fleet(env, "launch", "w", "--label", "w1", "--parent", "FAKE",
                  "--plugin", str(abs_dir), "--plugin", "bareghost", "--dry-run")
    argv = _launch_argv(p.stdout)
    assert f"--plugin-dir {abs_dir}" in argv            # abs path -> used as-is
    assert "bareghost" in p.stdout and "not resolvable" in p.stdout   # bare unindexed -> warned + skipped
    assert "bareghost" not in argv                      # ...and never composed onto the command


def test_launch_plugin_repeatable_and_comma_sep_shapes(cli_env, tmp_path):
    # The flag reads BOTH shapes: `--plugin a,b` (comma) and `--plugin a --plugin b` (repeatable). This
    # test uses comma-sep for two linked names in one flag.
    berg = tmp_path / "mkt-berg"
    a_dir = _mkplugin(berg, "alpha")
    b_dir = _mkplugin(berg, "beta")
    (tmp_path / "plugins.toml").write_text(
        '[marketplace.berg]\npath = "mkt-berg"\n'
        '[plugin.alpha]\ntype = "linked"\nsource = "berg"\n'
        '[plugin.beta]\ntype = "linked"\nsource = "berg"\n')
    (tmp_path / "fleet.toml").write_text('[role.w]\nkind = "child"\ncwd = "w"\n[role.w.claude]\n')
    env = _env(cli_env, tmp_path, index=tmp_path / "plugins.toml")
    p = run_fleet(env, "launch", "w", "--label", "w1", "--parent", "FAKE", "--plugin", "alpha,beta", "--dry-run")
    argv = _launch_argv(p.stdout)
    assert f"--plugin-dir {a_dir}" in argv and f"--plugin-dir {b_dir}" in argv   # comma-sep split into two


def test_launch_plugin_spans_both_channels_and_abspath(cli_env, tmp_path):
    # One launch: an indexed linked name, an indexed enabled name, and an unindexed ABSOLUTE path all
    # compose into one command across BOTH native channels.
    berg = tmp_path / "mkt-berg"
    ms_dir = _mkplugin(berg, "memsearch")                 # index -> linked
    abs_dir = _mkplugin(tmp_path / "loose", "abs-plug")   # unindexed absolute path -> used as-is
    (tmp_path / "plugins.toml").write_text(
        '[marketplace.berg]\npath = "mkt-berg"\n[marketplace.obs]\nkind = "global"\n'
        '[plugin.memsearch]\ntype = "linked"\nsource = "berg"\n'
        '[plugin.obsidian]\ntype = "enabled"\nsource = "obs"\ntools = ["claude"]\n')
    (tmp_path / "fleet.toml").write_text('[role.w]\nkind = "child"\ncwd = "w"\n[role.w.claude]\n')
    env = _env(cli_env, tmp_path, index=tmp_path / "plugins.toml")
    p = run_fleet(env, "launch", "w", "--label", "w1", "--parent", "FAKE",
                  "--plugin", str(abs_dir), "--plugin", "obsidian", "--plugin", "memsearch", "--dry-run")
    argv = _launch_argv(p.stdout)
    assert f"--plugin-dir {abs_dir}" in argv                    # unindexed absolute path -> used as-is
    assert f"--plugin-dir {ms_dir}" in argv                     # indexed linked
    assert '"enabledPlugins": {"obsidian@obs": true}' in argv   # indexed enabled


# =================================================================================================
# RECYCLE --plugin  (roster re-resolve; enabled reaches the enabled channel too)
# =================================================================================================
def _seed_roster_recycle(fs, cli_env, tmp_path, *, extra_index=""):
    """Seed a LIVE roster agent + a scratch roster toml + a 2-plugin index. Returns the CLI env. Recycle
    re-resolves the toml for a roster role (toml-authoritative), so `--plugin` injects into the composed
    `plugins` and routes through the index exactly like a real recycle."""
    index, linked_dir = _index_two_plugins(tmp_path)
    (tmp_path / "fleet.toml").write_text(
        '[role.worker]\nkind = "child"\ncwd = "workers/w"\n[role.worker.claude]\n' + extra_index)
    fs.live_put("wkr", {"role": "worker", "kind": "child", "tool": "claude",
                        "cwd": "workers/w", "parent": "P", "place": "tab",
                        "surface": "SURF-SEED", "session": "claude-deadbeef00"})
    return _env(cli_env, tmp_path, index=index), linked_dir


def test_recycle_plugin_linked_adds_plugin_dir(cli_env, tmp_path, fs, state_dir):
    env, linked_dir = _seed_roster_recycle(fs, cli_env, tmp_path)
    p = run_fleet(env, "recycle", "wkr", "--plugin", "memsearch", "--dry-run")
    assert "dry-run" in p.stdout.lower()
    argv = _launch_argv(p.stdout)
    assert f"--plugin-dir {linked_dir}" in argv        # linked --plugin reaches --plugin-dir on recycle


def test_recycle_plugin_enabled_reaches_enabledplugins(cli_env, tmp_path, fs, state_dir):
    # `recycle --plugin <enabled>` produces an enabledPlugins entry — the enabled channel is reachable at
    # recycle (a linked-only add-surface never could).
    env, _ = _seed_roster_recycle(fs, cli_env, tmp_path)
    p = run_fleet(env, "recycle", "wkr", "--plugin", "obsidian", "--dry-run")
    assert "dry-run" in p.stdout.lower()
    argv = _launch_argv(p.stdout)
    assert '"enabledPlugins": {"obsidian@obs": true}' in argv   # enabled reaches enabledPlugins on recycle
    assert "--plugin-dir" not in argv                            # and is NOT mis-routed to --plugin-dir


def test_recycle_plugin_both_channels_in_one_command(cli_env, tmp_path, fs, state_dir):
    # A linked + an enabled name on the SAME recycle compose BOTH channels into one recycled command.
    env, linked_dir = _seed_roster_recycle(fs, cli_env, tmp_path)
    p = run_fleet(env, "recycle", "wkr", "--plugin", "memsearch,obsidian", "--dry-run")
    argv = _launch_argv(p.stdout)
    assert f"--plugin-dir {linked_dir}" in argv                 # linked channel
    assert '"enabledPlugins": {"obsidian@obs": true}' in argv   # enabled channel
    # and the resume identity is preserved (recycle re-adds the session) — proves it's a real recycle compose
    assert "--resume deadbeef00" in argv


def test_recycle_plugin_unions_with_role_plugins_and_dedupes(cli_env, tmp_path, fs, state_dir):
    # The role already links memsearch via `plugins`; `recycle --plugin memsearch` must dedupe (appear
    # once), while `--plugin obsidian` adds the enabled channel on top of the re-resolved roster loadout.
    env, linked_dir = _seed_roster_recycle(fs, cli_env, tmp_path, extra_index='plugins = ["memsearch"]\n')
    p = run_fleet(env, "recycle", "wkr", "--plugin", "memsearch", "--plugin", "obsidian", "--dry-run")
    argv = _launch_argv(p.stdout)
    assert argv.count(f"--plugin-dir {linked_dir}") == 1        # deduped against the role's own `plugins`
    assert '"enabledPlugins": {"obsidian@obs": true}' in argv


def test_replay_binding_argv_plugin_reaches_both_channels(monkeypatch):
    # The AD-HOC replay path (a live-binding recycle of an OFF-ROSTER agent) has no toml to re-resolve, so
    # it appends `--plugin` results onto the captured argv directly. Prove both channels land: linked ->
    # --plugin-dir (deduped against an existing one), enabled -> an appended enabledPlugins --settings.
    monkeypatch.setattr(fleet, "load_plugin_index", lambda *a, **k: {"plugins": {}, "marketplaces": {}})
    monkeypatch.setattr(fleet, "_resolve_plugins",
                        lambda names, index: (["/mkt/memsearch", "/mkt/dup"], ["obsidian@obs"], ["ghost"]))
    monkeypatch.setattr(fleet, "_profile_env", lambda: {})
    argv = ["--plugin-dir", "/mkt/dup", "--model", "opus"]      # binding already carries /mkt/dup
    send = fleet._replay_binding_argv(argv, "claude", "adhoc", "adhoc", "/x", [],
                                      ["memsearch", "obsidian", "ghost"], "SID")
    assert "--plugin-dir /mkt/memsearch" in send                # linked appended
    assert send.count("--plugin-dir /mkt/dup") == 1             # deduped against the binding's own
    assert '"enabledPlugins": {"obsidian@obs": true}' in send   # enabled appended as a fresh --settings
    assert "--resume SID" in send                               # identity preserved
