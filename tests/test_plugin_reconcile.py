"""Layer 3 — the reconcile ENGINE (cmux_fleet/plugins.py), Phase-2 of the plugin redesign (design §6).

Pure in-process tests: they hand the engine FIXTURE marketplace dirs (subdirs carrying .claude-plugin/
.codex-plugin manifests + a marketplace.json) and a FIXTURE claude-settings JSON, then assert the
derivation + merge. Nothing here touches the live ~/.config/cmux-fleet or ~/.claude — every source is a
tmp_path fixture. The CLI wiring (--dry-run/--prune/--json, ls/show/describe) is covered end-to-end by
test_plugin_discovery.py.

Proven here:
  - tools DERIVED from which manifests a plugin dir carries (both -> [claude,codex]; one -> that one)
  - origin DERIVED from marketplace.json source-kind (bare path -> "path", git object -> "url")
  - description DERIVED from marketplace.json, falling back to the plugin's own plugin.json
  - enabled entries DERIVED from settings enabledPlugins (disabled -> install="global-disabled")
  - PRESERVE a curated description + REPORT drift; UPDATE a non-curated drifted description
  - PRESERVE a hand [plugin.<n>.<tool>] block and a hand-added entry (pruned only under --prune)
  - IDEMPOTENT: a second reconcile right after the first is byte-identical (no changes)
"""
import tomllib

import pytest

from cmux_fleet import plugins as fp


# --- fixture builders ----------------------------------------------------------------------------
def _mkplugin(plugins_dir, name, *, tools):
    """Create a plugin dir under a marketplace's plugins/ dir, carrying the manifests for `tools`
    (a `.claude-plugin`/`.codex-plugin` subdir with a plugin.json). Returns the dir path."""
    p = plugins_dir / name
    for tool, manifest in (("claude", ".claude-plugin"), ("codex", ".codex-plugin")):
        if tool in tools:
            mdir = p / manifest
            mdir.mkdir(parents=True, exist_ok=True)
            (mdir / "plugin.json").write_text(
                f'{{"name":"{name}","version":"0.0.1","description":"{name} via plugin.json"}}')
    p.mkdir(parents=True, exist_ok=True)
    return p


def _mkmarketplace(root, plugins, *, mjson=None):
    """Build a marketplace at `root` (root/plugins/<name>/... + root/.claude-plugin/marketplace.json).
    `plugins` = {name: [tools]}. `mjson` (optional) = the marketplace.json `plugins` array entries
    (each {name, description, source}). Returns the plugins dir path (what [marketplace.*].path points at)."""
    plugins_dir = root / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    for name, tools in plugins.items():
        _mkplugin(plugins_dir, name, tools=tools)
    if mjson is not None:
        import json
        cp = root / ".claude-plugin"
        cp.mkdir(parents=True, exist_ok=True)
        (cp / "marketplace.json").write_text(json.dumps({"name": "fixture", "plugins": mjson}))
    return plugins_dir


def _local(path):
    return {"kind": "local", "path": str(path)}


# --- derivation: tools from manifest presence (THE headline) -------------------------------------
def test_tools_derived_from_manifest_presence(tmp_path):
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {
        "dual": ["claude", "codex"],          # both manifests -> ["claude","codex"]
        "claude-only": ["claude"],            # one manifest   -> ["claude"]
        "codex-only": ["codex"],              # one manifest   -> ["codex"]
        "empty": [],                          # no manifest    -> skipped (not a plugin)
    })
    derived, _coll = fp.derive_entries({"berg": _local(plugins_dir)}, [])
    assert derived["dual"]["tools"] == ["claude", "codex"]
    assert derived["claude-only"]["tools"] == ["claude"]
    assert derived["codex-only"]["tools"] == ["codex"]
    assert "empty" not in derived                        # no manifest -> not derived
    # every linked entry carries type + source(marketplace name)
    for name in ("dual", "claude-only", "codex-only"):
        assert derived[name]["type"] == "linked"
        assert derived[name]["source"] == "berg"


def test_origin_and_description_from_marketplace_json(tmp_path):
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"loc": ["claude"], "git": ["claude"]}, mjson=[
        {"name": "loc", "description": "a local one", "source": "./plugins/loc"},
        {"name": "git", "description": "a git one",
         "source": {"source": "url", "url": "https://x/git.git", "ref": "main"}},
    ])
    derived, _coll = fp.derive_entries({"berg": _local(plugins_dir)}, [])
    assert derived["loc"]["origin"] == "path" and derived["loc"]["description"] == "a local one"
    assert derived["git"]["origin"] == "url" and derived["git"]["description"] == "a git one"


def test_description_falls_back_to_plugin_json(tmp_path):
    # a plugin dir the marketplace.json does NOT list -> description from its own plugin.json.
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"orphan": ["claude"]},
                                 mjson=[{"name": "other", "description": "x", "source": "./plugins/other"}])
    derived, _coll = fp.derive_entries({"berg": _local(plugins_dir)}, [])
    assert derived["orphan"]["description"] == "orphan via plugin.json"
    assert derived["orphan"]["origin"] == "path"          # default when not in marketplace.json


# --- derivation: enabled entries from settings ---------------------------------------------------
def test_enabled_entries_derived_from_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"enabledPlugins": {"obsidian@obs": false, "live@lw": true}}')
    derived, _coll = fp.derive_entries({}, [str(settings)])
    assert derived["obsidian"]["type"] == "enabled"
    assert derived["obsidian"]["source"] == "obs"
    assert derived["obsidian"]["tools"] == ["claude"]
    assert derived["obsidian"]["install"] == "global-disabled"   # disabled -> per-agent-flip candidate
    assert derived["live"]["install"] == ""                       # actively enabled globally -> no flip needed


def test_linked_dir_wins_over_enabled_ref_of_same_name(tmp_path):
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"dup": ["claude", "codex"]})
    settings = tmp_path / "settings.json"
    settings.write_text('{"enabledPlugins": {"dup@somewhere": false}}')
    derived, _coll = fp.derive_entries({"berg": _local(plugins_dir)}, [str(settings)])
    assert derived["dup"]["type"] == "linked"             # the local checkout is the stronger signal
    assert derived["dup"]["tools"] == ["claude", "codex"]


# --- merge: add on an empty index, then IDEMPOTENT ------------------------------------------------
def test_add_then_idempotent(tmp_path):
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"a": ["claude"], "b": ["claude", "codex"]}, mjson=[
        {"name": "a", "description": "plugin a", "source": "./plugins/a"},
        {"name": "b", "description": "plugin b", "source": "./plugins/b"},
    ])
    index = tmp_path / "plugins.toml"
    index.write_text('[marketplace.berg]\npath = "mkt/plugins"\n')   # marketplace declared, no plugins yet
    mkts = {"berg": _local(plugins_dir)}

    text1, diff1, _ = fp.run_reconcile(str(index), mkts, [], prune=False)
    assert diff1.counts()["add"] == 2 and not diff1.notes
    index.write_text(text1)                                # apply run 1

    text2, diff2, existing2 = fp.run_reconcile(str(index), mkts, [], prune=False)
    assert not diff2.has_changes                           # second run: NO add/update/prune
    assert text2 == existing2 == text1                     # byte-identical -> idempotent
    # the marketplace block survived verbatim (hand-owned, relative path preserved)
    assert tomllib.loads(text1)["marketplace"]["berg"]["path"] == "mkt/plugins"


# --- merge: preserve a curated description + report drift; update a non-curated one ---------------
def test_curated_description_preserved_and_drift_reported(tmp_path):
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"x": ["claude"]},
                                 mjson=[{"name": "x", "description": "SOURCE desc", "source": "./plugins/x"}])
    index = tmp_path / "plugins.toml"
    # seed machine fields (origin) matching source so the ONLY divergence under test is the description
    index.write_text('[marketplace.berg]\npath = "mkt/plugins"\n'
                     '[plugin.x]\ntype = "linked"\nsource = "berg"\ntools = ["claude"]\norigin = "path"\n'
                     'description = "MY curated desc"\ncurated = true\n')
    text, diff, _ = fp.run_reconcile(str(index), {"berg": _local(plugins_dir)}, [], prune=False)
    merged = tomllib.loads(text)["plugin"]["x"]
    assert merged["description"] == "MY curated desc"      # PRESERVED, not clobbered
    assert merged["curated"] is True
    assert ("drift", "x") in {(k, n) for k, n, _ in diff.notes}   # REPORTED: index vs source
    assert not diff.has_changes                            # curated -> no file-mutating change (drift is a note)


def test_noncurated_drifted_description_is_updated(tmp_path):
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"x": ["claude"]},
                                 mjson=[{"name": "x", "description": "fresh source desc", "source": "./plugins/x"}])
    index = tmp_path / "plugins.toml"
    index.write_text('[marketplace.berg]\npath = "mkt/plugins"\n'
                     '[plugin.x]\ntype = "linked"\nsource = "berg"\ntools = ["claude"]\norigin = "path"\n'
                     'description = "stale desc"\n')          # NOT curated -> machine owns the description
    text, diff, _ = fp.run_reconcile(str(index), {"berg": _local(plugins_dir)}, [], prune=False)
    assert tomllib.loads(text)["plugin"]["x"]["description"] == "fresh source desc"
    assert any(a == "update" and n == "x" and "description" in d for a, n, d in diff.changes)


# --- merge: preserve a hand per-tool block --------------------------------------------------------
def test_per_tool_block_preserved(tmp_path):
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"memsearch": ["claude", "codex"]})
    index = tmp_path / "plugins.toml"
    # seed a fully in-sync entry (scalars match what the manifests + plugin.json derive) so the only
    # thing left to prove is that the hand [plugin.<n>.<tool>] block survives untouched.
    index.write_text('[marketplace.berg]\npath = "mkt/plugins"\n'
                     '[plugin.memsearch]\ntype = "linked"\nsource = "berg"\ntools = ["claude", "codex"]\n'
                     'origin = "path"\ndescription = "memsearch via plugin.json"\n'
                     '[plugin.memsearch.codex]\nnotes = "reads codex-hooks.json"\n')
    text, diff, _ = fp.run_reconcile(str(index), {"berg": _local(plugins_dir)}, [], prune=False)
    merged = tomllib.loads(text)["plugin"]["memsearch"]
    assert merged["codex"]["notes"] == "reads codex-hooks.json"   # per-tool block survives verbatim
    assert not diff.has_changes                                   # nothing drifted -> in sync


# --- merge: prune only removes an unbacked entry, and only under --prune --------------------------
def test_prune_only_removes_unbacked_and_only_under_flag(tmp_path):
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"real": ["claude"]})
    seed = ('[marketplace.berg]\npath = "mkt/plugins"\n'
            '[plugin.real]\ntype = "linked"\nsource = "berg"\ntools = ["claude"]\n'
            '[plugin.ghost]\ntype = "linked"\nsource = "berg"\ntools = ["claude"]\n'
            'description = "hand-added, no dir"\n')
    index = tmp_path / "plugins.toml"
    mkts = {"berg": _local(plugins_dir)}

    index.write_text(seed)
    text_keep, diff_keep, _ = fp.run_reconcile(str(index), mkts, [], prune=False)
    assert "ghost" in tomllib.loads(text_keep)["plugin"]          # preserved without --prune
    assert ("preserve", "ghost") in {(k, n) for k, n, _ in diff_keep.notes}

    index.write_text(seed)
    text_prune, diff_prune, _ = fp.run_reconcile(str(index), mkts, [], prune=True)
    assert "ghost" not in tomllib.loads(text_prune)["plugin"]     # dropped under --prune
    assert "real" in tomllib.loads(text_prune)["plugin"]          # a backed entry is kept
    assert ("prune", "ghost") in {(a, n) for a, n, _ in diff_prune.changes}


# --- SAFETY: present-but-unparseable index ABORTS; absent/empty starts fresh (review Finding 1) ---
def test_run_reconcile_aborts_on_malformed_populated_index(tmp_path):
    """The data-loss regression guard: a POPULATED index that no longer parses must NOT be treated as
    empty and regenerated from sources — run_reconcile raises IndexParseError so the caller writes
    nothing. Before the fix this returned new_text derived from an empty existing set, dropping every
    hand-authored entry, curated description, per-tool block, and [marketplace.*] block."""
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"real": ["claude"]})
    index = tmp_path / "plugins.toml"
    populated = ('[marketplace.berg]\npath = "mkt/plugins"\n'
                 '[plugin.handmade]\ntype = "linked"\nsource = "berg"\ntools = ["claude"]\n'
                 'description = "curated by hand"\ncurated = true\n'
                 'this is a typo that breaks the parse\n')       # a lone junk line -> TOMLDecodeError
    index.write_text(populated)
    with pytest.raises(fp.IndexParseError) as ei:
        fp.run_reconcile(str(index), {"berg": _local(plugins_dir)}, [], prune=False)
    assert str(index) in str(ei.value)                            # the message names the offending file
    assert index.read_text() == populated                         # byte-unchanged (run_reconcile never writes)


def test_run_reconcile_starts_fresh_on_absent_index(tmp_path):
    """Regression guard the OTHER way: an ABSENT index is the normal first-run path — reconcile must NOT
    abort, it derives from sources into a fresh file."""
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"real": ["claude"]})
    index = tmp_path / "plugins.toml"                             # never created
    text, diff, existing = fp.run_reconcile(str(index), {"berg": _local(plugins_dir)}, [], prune=False)
    assert existing == ""
    assert "real" in tomllib.loads(text)["plugin"]                # derived fresh, no error


def test_run_reconcile_starts_fresh_on_empty_index(tmp_path):
    """An EMPTY / whitespace-only file carries no curated data at risk, so it is treated as absent (start
    fresh) rather than an abort — proves the abort keys on unparseable-CONTENT, not mere presence."""
    plugins_dir = _mkmarketplace(tmp_path / "mkt", {"real": ["claude"]})
    index = tmp_path / "plugins.toml"
    index.write_text("   \n\n")                                   # present but blank
    text, diff, _ = fp.run_reconcile(str(index), {"berg": _local(plugins_dir)}, [], prune=False)
    assert "real" in tomllib.loads(text)["plugin"]                # no IndexParseError; derived fresh


# --- cross-marketplace name collision is SURFACED, not silent (review Finding 2) ------------------
def test_cross_marketplace_collision_reported(tmp_path):
    """Two LOCAL marketplaces both carry a plugin named `alpha`. The winner stays last-alphabetically
    (mkt2), but derive_entries reports the collision and run_reconcile emits a `collision` note + count —
    so *which* marketplace's code loads is visible, never silent."""
    dir1 = _mkmarketplace(tmp_path / "m1", {"alpha": ["claude"], "solo1": ["claude"]})
    dir2 = _mkmarketplace(tmp_path / "m2", {"alpha": ["claude"], "solo2": ["claude"]})
    mkts = {"mkt1": _local(dir1), "mkt2": _local(dir2)}

    derived, collisions = fp.derive_entries(mkts, [])
    assert derived["alpha"]["source"] == "mkt2"                   # last-wins policy unchanged
    assert collisions["alpha"] == ["mkt1", "mkt2"]               # declaration order; last is the winner
    assert "solo1" not in collisions and "solo2" not in collisions   # non-colliding names untouched

    index = tmp_path / "plugins.toml"                             # absent -> a clean fresh reconcile
    _text, diff, _ = fp.run_reconcile(str(index), mkts, [], prune=False)
    assert ("collision", "alpha") in {(k, n) for k, n, _ in diff.notes}
    assert diff.counts()["collision"] == 1
    detail = next(d for k, n, d in diff.notes if k == "collision" and n == "alpha")
    assert "mkt1" in detail and "mkt2" in detail and "using mkt2" in detail
