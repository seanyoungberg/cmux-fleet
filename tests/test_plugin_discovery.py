"""Layer 3 — the `fleet plugins` discovery + reconcile CLI (design §4/§6), Phase 2.

End-to-end through the real `fleet` CLI as a subprocess (same harness as test_plugin_index.py) against
SCRATCH tomls + FIXTURE marketplaces/settings — the live ~/.config/cmux-fleet and ~/.claude are never
touched (reconcile's settings source is redirected via $CMUX_FLEET_CLAUDE_SETTINGS).

Covers:
  - `plugins ls` renders the index (table + --json)
  - `plugins show <name>` gives the resolved --plugin-dir path and finds the ROLES that use it
    (every role/floor `plugins` reference)
  - `plugins describe <name>` lists the skills a plugin exposes, FOLLOWING symlinked skill dirs
  - `plugins reconcile --dry-run` writes NOTHING; a real run then a re-run is idempotent (--json counts)
  - `plugins show <missing>` is a clean non-zero, not a crash
"""
import json
import os

from test_e2e_cli import run_fleet


def _mkmarketplace(root, plugins, *, mjson=None):
    """root/plugins/<name>/.<tool>-plugin + root/.claude-plugin/marketplace.json. plugins={name:[tools]}.
    Returns the plugins dir (a [marketplace.*].path / $CMUX_FLEET_MARKETPLACE value)."""
    plugins_dir = root / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    for name, tools in plugins.items():
        for tool, manifest in (("claude", ".claude-plugin"), ("codex", ".codex-plugin")):
            if tool in tools:
                mdir = plugins_dir / name / manifest
                mdir.mkdir(parents=True, exist_ok=True)
                (mdir / "plugin.json").write_text(
                    f'{{"name":"{name}","version":"0.0.1","description":"{name} via plugin.json"}}')
        (plugins_dir / name).mkdir(parents=True, exist_ok=True)
    if mjson is not None:
        cp = root / ".claude-plugin"
        cp.mkdir(parents=True, exist_ok=True)
        (cp / "marketplace.json").write_text(json.dumps({"name": "fixture", "plugins": mjson}))
    return plugins_dir


def _env(cli_env, tmp_path, *, index=None, settings=None):
    e = {**cli_env, "CMUX_FLEET_TOML": str(tmp_path / "fleet.toml")}
    e["CMUX_FLEET_PLUGIN_INDEX"] = str(index) if index is not None else str(tmp_path / "__no_index__.toml")
    if settings is not None:
        e["CMUX_FLEET_CLAUDE_SETTINGS"] = str(settings)
    return e


# --- ls ------------------------------------------------------------------------------------------
def test_plugins_ls_table_and_json(cli_env, tmp_path):
    (tmp_path / "plugins.toml").write_text(
        '[plugin.cmux-fleet]\ntype = "linked"\nsource = "berg"\ntools = ["claude"]\n'
        'description = "native orchestration"\n'
        '[plugin.obsidian]\ntype = "enabled"\nsource = "obs"\ntools = ["claude"]\n'
        'install = "global-disabled"\ndescription = "vault skills"\n')
    env = _env(cli_env, tmp_path, index=tmp_path / "plugins.toml")
    p = run_fleet(env, "plugins", "ls")
    assert "cmux-fleet" in p.stdout and "obsidian" in p.stdout
    assert "linked" in p.stdout and "enabled" in p.stdout

    data = json.loads(run_fleet(env, "plugins", "ls", "--json").stdout)
    assert {e["name"] for e in data} == {"cmux-fleet", "obsidian"}
    obs = next(e for e in data if e["name"] == "obsidian")
    assert obs["type"] == "enabled" and obs["tools"] == ["claude"] and obs["source"] == "obs"


def test_plugins_ls_truncates_long_description_but_show_and_json_keep_full(cli_env, tmp_path):
    # `ls` is a scan table: a verbose multi-paragraph marketplace.json description must collapse to ONE
    # truncated line there, while `show` and `--json` keep the FULL text. (Phase-3 polish.)
    tail = "z" * 200                                            # a long first line, well past the ~60c cap
    full_desc = f"First line is very long and keeps going {tail}\n\nSecond paragraph should never show in ls."
    (tmp_path / "plugins.toml").write_text(
        '[plugin.verbose]\ntype = "linked"\nsource = "berg"\ntools = ["claude"]\n'
        f'description = {json.dumps(full_desc)}\n')
    env = _env(cli_env, tmp_path, index=tmp_path / "plugins.toml")

    ls = run_fleet(env, "plugins", "ls")
    ls_row = next(l for l in ls.stdout.splitlines() if "verbose" in l and "linked" in l)
    assert "…" in ls_row                                       # truncated with an ellipsis
    assert tail not in ls_row                                  # the long tail is cut
    assert "Second paragraph" not in ls.stdout                 # only the FIRST line ever reaches ls
    assert len(ls_row) < 120                                   # the row is a scannable single line

    # show + --json keep the full, untruncated description
    show = run_fleet(env, "plugins", "show", "verbose")
    assert tail in show.stdout                                 # full first line intact in show
    data = json.loads(run_fleet(env, "plugins", "ls", "--json").stdout)
    assert data[0]["description"] == full_desc                 # --json is never truncated


# --- show: resolved path + which roles use it ----------------------------------------------------
def test_plugins_show_resolves_path_and_finds_roles(cli_env, tmp_path):
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"memsearch": ["claude", "codex"]})
    (tmp_path / "plugins.toml").write_text(
        f'[marketplace.berg]\npath = "{plugins_dir}"\n'
        '[plugin.memsearch]\ntype = "linked"\nsource = "berg"\ntools = ["claude","codex"]\n'
        'description = "memory"\norigin = "path"\n')
    # roster: the floor references it via `plugins`; a role references it via `plugins` too (both surface)
    (tmp_path / "fleet.toml").write_text(
        '[tool.claude]\nplugins = ["memsearch"]\n'
        '[role.researcher]\nkind = "child"\ncwd = "w"\n'
        '[role.researcher.claude]\nplugins = ["memsearch"]\n')
    env = _env(cli_env, tmp_path, index=tmp_path / "plugins.toml")

    p = run_fleet(env, "plugins", "show", "memsearch")
    assert str(plugins_dir / "memsearch") in p.stdout          # resolved --plugin-dir path
    assert "tool.claude" in p.stdout                            # floor `plugins`
    assert "role.researcher.claude" in p.stdout                 # role `plugins`

    data = json.loads(run_fleet(env, "plugins", "show", "memsearch", "--json").stdout)
    assert data["found"] and data["resolved_dir"] == str(plugins_dir / "memsearch")
    used = {(u["scope"], u["key"]) for u in data["used_by"]}
    assert ("tool.claude", "plugins") in used
    assert ("role.researcher.claude", "plugins") in used       # both references surfaced


def test_plugins_show_missing_is_clean_error(cli_env, tmp_path):
    (tmp_path / "plugins.toml").write_text('[plugin.real]\ntype = "linked"\nsource = "x"\n')
    env = _env(cli_env, tmp_path, index=tmp_path / "plugins.toml")
    p = run_fleet(env, "plugins", "show", "ghost", expect=1)
    assert "not in the index" in p.stdout


# --- describe: skills a plugin exposes (following symlinks) ---------------------------------------
def test_plugins_describe_lists_skills_following_symlinks(cli_env, tmp_path):
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"toolkit": ["claude"]})
    skills = plugins_dir / "toolkit" / "skills"
    for s in ("alpha", "beta"):
        (skills / s).mkdir(parents=True)
        (skills / s / "SKILL.md").write_text(f"# {s}")
    # a SYMLINKED skill dir (mirrors the cmux plugin's symlinked skills) -> must be followed
    real = tmp_path / "external" / "gamma"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("# gamma")
    os.symlink(real, skills / "gamma")

    (tmp_path / "plugins.toml").write_text(
        f'[marketplace.berg]\npath = "{plugins_dir}"\n'
        '[plugin.toolkit]\ntype = "linked"\nsource = "berg"\ntools = ["claude"]\ndescription = "a toolkit"\n')
    env = _env(cli_env, tmp_path, index=tmp_path / "plugins.toml")

    p = run_fleet(env, "plugins", "describe", "toolkit")
    assert "a toolkit" in p.stdout
    for s in ("alpha", "beta", "gamma"):
        assert s in p.stdout                                    # gamma proves symlinks are followed

    data = json.loads(run_fleet(env, "plugins", "describe", "toolkit", "--json").stdout)
    assert sorted(data["skills"]) == ["alpha", "beta", "gamma"]


# --- reconcile CLI: --dry-run writes nothing; real run then re-run is idempotent ------------------
def test_plugins_reconcile_cli_dry_run_then_idempotent(cli_env, tmp_path):
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"a": ["claude"], "b": ["claude", "codex"]}, mjson=[
        {"name": "a", "description": "plugin a", "source": "./plugins/a"},
        {"name": "b", "description": "plugin b", "source": {"source": "url", "url": "https://x/b.git"}},
    ])
    settings = tmp_path / "settings.json"
    settings.write_text('{"enabledPlugins": {"obsidian@obs": false}}')
    index = tmp_path / "plugins.toml"                          # declares the marketplace, no plugins yet
    index.write_text(f'[marketplace.local]\npath = "{plugins_dir}"\n')
    before = index.read_text()
    env = _env(cli_env, tmp_path, index=index, settings=settings)

    dry = run_fleet(env, "plugins", "reconcile", "--dry-run")
    assert "add" in dry.stdout and "[dry-run]" in dry.stdout
    assert index.read_text() == before                         # --dry-run wrote NOTHING (marketplace-only)

    wrote = run_fleet(env, "plugins", "reconcile")
    assert index.exists() and "wrote" in wrote.stdout
    # a + b (linked, tools from manifests) + obsidian (enabled from settings) all landed
    body = index.read_text()
    assert "[plugin.a]" in body and "[plugin.b]" in body and "[plugin.obsidian]" in body
    assert 'origin = "url"' in body                            # b derived a git origin

    again = json.loads(run_fleet(env, "plugins", "reconcile", "--json").stdout)
    assert again["counts"]["add"] == 0 and again["counts"]["update"] == 0 and again["counts"]["prune"] == 0
    assert again["wrote"] is False                             # idempotent: nothing to write on re-run
