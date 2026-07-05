"""Layer 3 — `fleet plugins add <ref>` (install-from-URL at the SAFE default), Phase 4b (design §5b).

THE SAFETY CONTRACT (Berg-ratified — the whole point of this phase): `add` may auto-clone a NEW plugin and
wire the index, but it NEVER enables it, NEVER adds it to a role's `use`, and NEVER runs its hooks. The
human flips it on later with `fleet recycle <agent> --use <name>`. These tests PROVE that contract:

  - the PURE engine (classify/infer/name/add_enabled_index_text) makes only the linked-vs-enabled call and
    REFUSES (STOPs) on an ambiguous ref instead of defaulting a security-relevant choice;
  - a real `add` of a fixture plugin leaves the claude settings file byte-identical (never an enabledPlugins
    entry), the roster byte-identical (never a role `use` edit), and never fires the plugin's hook;
  - the added plugin is AVAILABLE (resolvable via `--use`) but NOT auto-loaded by a role launch;
  - `--dry-run` clones/writes NOTHING; an already-indexed ref is an idempotent no-op that points at `--use`.

Every source is a tmp_path fixture (a local plugin dir or a throwaway `git init` repo) — NOTHING here clones
from the network or touches the live ~/.config/cmux-fleet, ~/.claude, or prod state (claude settings are
redirected via $CMUX_FLEET_CLAUDE_SETTINGS; the marketplace/index/roster are all under tmp_path).
"""
import json
import os
import subprocess
import tomllib

from cmux_fleet import plugins as fp
from test_e2e_cli import run_fleet


# --- fixture builders ----------------------------------------------------------------------------
def _mkplugin_src(root, name, *, tools=("claude",), sentinel=None):
    """A SOURCE plugin dir (what an `add` copies/clones): a .claude-plugin/plugin.json (+ optional codex),
    a skills/ dir, and — if `sentinel` is given — a hook whose command touches that path IF it ever runs
    (it must not: `add` never spawns claude, so the sentinel proves no hook fired)."""
    p = root / name
    for tool, manifest in (("claude", ".claude-plugin"), ("codex", ".codex-plugin")):
        if tool in tools:
            md = p / manifest
            md.mkdir(parents=True, exist_ok=True)
            (md / "plugin.json").write_text(
                json.dumps({"name": name, "version": "0.0.1", "description": f"{name} desc"}))
    if sentinel is not None:
        hd = p / ".claude-plugin" / "hooks"
        hd.mkdir(parents=True, exist_ok=True)
        (hd / "hooks.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": f"touch {sentinel}"}]}]}}))
    (p / "skills").mkdir(parents=True, exist_ok=True)
    return p


def _mkgitrepo(root, name):
    """A throwaway LOCAL git repo carrying a plugin (a `.claude-plugin` manifest), committed once — the
    offline stand-in for a remote git URL. Returned so a test can `add file://<repo>` with NO network."""
    repo = root / name
    md = repo / ".claude-plugin"
    md.mkdir(parents=True)
    (md / "plugin.json").write_text(json.dumps({"name": name, "description": f"{name} desc"}))
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "init"], check=True)
    return repo


def _env(cli_env, tmp_path, *, index, marketplace=None, settings=None):
    """CLI env pinned at tmp_path: the roster, the plugin index, an optional local marketplace, and an
    optional REDIRECTED claude-settings file (so nothing reads/writes the host's real ~/.claude)."""
    e = {**cli_env, "CMUX_FLEET_TOML": str(tmp_path / "fleet.toml"),
         "CMUX_FLEET_PLUGIN_INDEX": str(index)}
    if marketplace is not None:
        e["CMUX_FLEET_MARKETPLACE"] = str(marketplace)
    else:
        e.pop("CMUX_FLEET_MARKETPLACE", None)
    if settings is not None:
        e["CMUX_FLEET_CLAUDE_SETTINGS"] = str(settings)
    return e


# ================================================================ PURE ENGINE (the safety hinge) =====
def test_classify_ref_covers_all_kinds():
    c = fp.classify_ref
    assert c("https://github.com/a/b.git") == "git-url"
    assert c("https://github.com/a/b") == "git-url"
    assert c("git@github.com:a/b.git") == "git-url"          # scp shorthand is a URL, not name@marketplace
    assert c("ssh://git@h/a.git") == "git-url"
    assert c("file:///tmp/a") == "git-url"
    assert c("/abs/dir") == "path"
    assert c("~/dir") == "path"
    assert c("./dir") == "path" and c("../dir") == "path"
    assert c("obsidian@obsidian-skills") == "name@marketplace"
    assert c("loom") == "bare"                               # a lone name -> caller STOPs
    assert c("foo@bar.com") == "bare"                        # a dot in the '@'-tail is not a clean marketplace
    assert c("") == "bare"


def test_plugin_name_from_ref():
    n = fp.plugin_name_from_ref
    assert n("https://github.com/foo/bar.git") == "bar"
    assert n("git@github.com:foo/bar.git") == "bar"
    assert n("/x/y/myplug/") == "myplug"
    assert n("./plug") == "plug"
    assert n("obsidian@obsidian-skills") == "obsidian"


def test_infer_technique_git_url_and_path_are_linked():
    assert fp.infer_technique("https://x/y.git", None, None, {})[0] == "linked"
    assert fp.infer_technique("/abs/p", None, None, {})[0] == "linked"


def test_infer_technique_name_at_marketplace_is_enabled():
    assert fp.infer_technique("obsidian@obs", None, None, {})[0] == "enabled"


def test_infer_technique_bare_is_ambiguous_and_stops():
    # THE safety hinge: a bare name is never defaulted to a technique — it is refused so the caller STOPs.
    tech, reason = fp.infer_technique("loom", None, None, {})
    assert tech == "ambiguous" and "loom" in reason


def test_infer_technique_honors_as_flag_over_ref():
    assert fp.infer_technique("https://x/y.git", None, "enabled", {})[0] == "enabled"
    assert fp.infer_technique("obsidian@obs", None, "linked", {})[0] == "linked"


def test_infer_technique_marketplace_kind_decides():
    mkts = {"loc": {"kind": "local", "path": "/p"}, "glob": {"kind": "global"}}
    assert fp.infer_technique("x", "loc", None, mkts)[0] == "linked"       # local marketplace -> linked
    assert fp.infer_technique("x", "glob", None, mkts)[0] == "enabled"     # global marketplace -> enabled
    assert fp.infer_technique("x", "nope", None, mkts)[0] == "error"       # unknown -> STOP


def test_infer_technique_invalid_as_is_error():
    assert fp.infer_technique("x", None, "bogus", {})[0] == "error"


def test_add_enabled_index_text_records_disabled_and_preserves(tmp_path):
    index = tmp_path / "plugins.toml"
    index.write_text('[marketplace.pub]\nkind = "global"\n'
                     '[plugin.existing]\ntype = "linked"\nsource = "berg"\ntools = ["claude"]\n')
    new_text, existing = fp.add_enabled_index_text(str(index), "obsidian", "pub")
    doc = tomllib.loads(new_text)
    assert doc["plugin"]["obsidian"]["type"] == "enabled"
    assert doc["plugin"]["obsidian"]["install"] == "global-disabled"      # recorded DISABLED, not enabled
    assert doc["plugin"]["existing"]["type"] == "linked"                  # existing entry preserved
    assert doc["marketplace"]["pub"]["kind"] == "global"                  # marketplace preserved verbatim
    assert "enabledPlugins" not in new_text                               # the index never carries an enable map
    assert existing == index.read_text()                                  # returns prior text; writes NOTHING


# ================================================================ E2E — the never-auto-enable proofs ==
def test_add_linked_local_path_never_enables(cli_env, tmp_path):
    """THE safety test. `add` a local plugin dir -> the index gains a type=linked entry and NOTHING else:
    no enabledPlugins, no role `use` edit, no hook run; the plugin is available via `--use` but a plain
    role launch does NOT auto-load it."""
    sentinel = tmp_path / "HOOK_RAN"
    src = _mkplugin_src(tmp_path / "src", "newplug", sentinel=sentinel)
    mkt = tmp_path / "mkt" / "plugins"
    mkt.mkdir(parents=True)
    index = tmp_path / "plugins.toml"                        # absent to start
    settings = tmp_path / "settings.json"
    settings.write_text('{"enabledPlugins": {"unrelated@x": false}}')
    fleet = tmp_path / "fleet.toml"
    fleet.write_text('[tool.claude]\n'
                     '[role.worker]\nkind = "child"\ncwd = "workers/w"\n[role.worker.claude]\n')
    env = _env(cli_env, tmp_path, index=index, marketplace=mkt, settings=settings)

    settings_before, fleet_before = settings.read_text(), fleet.read_text()
    p = run_fleet(env, "plugins", "add", str(src))
    assert "LINKED" in p.stdout
    assert "recycle <agent> --use newplug" in p.stdout       # told the human how to enable (didn't do it)

    # the index gained a type=linked entry; linked never records an `install`; no enable map anywhere
    doc = tomllib.loads(index.read_text())
    assert doc["plugin"]["newplug"]["type"] == "linked"
    assert doc["plugin"]["newplug"]["source"] == "default"
    assert "install" not in doc["plugin"]["newplug"]
    assert "enabledPlugins" not in index.read_text()
    # NEVER enabled: the claude settings file is byte-identical (no {newplug@...: true} was ever written)
    assert settings.read_text() == settings_before
    # NEVER added to a role: the roster is byte-identical
    assert fleet.read_text() == fleet_before
    # NEVER ran a hook
    assert not sentinel.exists()

    # AVAILABLE but not loaded: it was cloned in, but a plain role launch does NOT wire it into the loadout
    assert (mkt / "newplug" / ".claude-plugin" / "plugin.json").exists()
    plain = run_fleet(env, "launch", "worker", "--label", "w1", "--parent", "FAKE", "--dry-run")
    assert "newplug" not in plain.stdout                     # add did NOT auto-load it
    # control: it IS loadable on demand via --use (proves add only made it available, didn't enable it)
    ctl = run_fleet(env, "launch", "worker", "--label", "w2", "--parent", "FAKE",
                    "--use", "newplug", "--dry-run")
    assert str(mkt / "newplug") in ctl.stdout                # --plugin-dir <mkt>/newplug appears ONLY with --use


def test_add_linked_from_git_repo_never_enables(cli_env, tmp_path):
    """A git-URL add clones (offline, from a throwaway `file://` repo) and still only wires the index —
    never enables. Proves the git-clone path, not just the local-copy path."""
    repo = _mkgitrepo(tmp_path / "gitplug", "gitplug")       # dir name == plugin name
    mkt = tmp_path / "mkt" / "plugins"
    mkt.mkdir(parents=True)
    index = tmp_path / "plugins.toml"
    settings = tmp_path / "settings.json"
    settings.write_text('{"enabledPlugins": {}}')
    env = _env(cli_env, tmp_path, index=index, marketplace=mkt, settings=settings)

    before = settings.read_text()
    p = run_fleet(env, "plugins", "add", f"file://{repo}")
    assert "LINKED" in p.stdout
    assert (mkt / "gitplug" / ".claude-plugin" / "plugin.json").exists()   # clone landed
    assert tomllib.loads(index.read_text())["plugin"]["gitplug"]["type"] == "linked"
    assert "enabledPlugins" not in index.read_text()
    assert settings.read_text() == before                                  # never touched claude settings


def test_add_enabled_records_global_disabled_never_enables(cli_env, tmp_path):
    """An enabled add (name@marketplace) wires a type=enabled/install=global-disabled index entry, prints
    the enable one-liner, and NEVER writes any claude settings file (so it can't emit an enabledPlugins
    entry) and NEVER edits a role."""
    index = tmp_path / "plugins.toml"
    settings = tmp_path / "settings.json"
    settings.write_text('{"enabledPlugins": {}}')
    fleet = tmp_path / "fleet.toml"
    fleet.write_text('[tool.claude]\n[role.worker]\nkind = "child"\ncwd = "w"\n[role.worker.claude]\n')
    env = _env(cli_env, tmp_path, index=index, settings=settings)
    sb, fb = settings.read_text(), fleet.read_text()

    p = run_fleet(env, "plugins", "add", "obsidian@obsidian-skills")
    assert "ENABLED" in p.stdout and "global-disabled" in p.stdout
    assert "recycle <agent> --use obsidian" in p.stdout

    e = tomllib.loads(index.read_text())["plugin"]["obsidian"]
    assert e["type"] == "enabled" and e["source"] == "obsidian-skills"
    assert e["install"] == "global-disabled"                 # DISABLED intent recorded, not flipped true
    assert "enabledPlugins" not in index.read_text()
    assert settings.read_text() == sb                        # claude settings untouched -> never enabled
    assert fleet.read_text() == fb                           # roster untouched -> no role edit


def test_add_enabled_via_marketplace_flag_on_bare_ref(cli_env, tmp_path):
    """A bare name is ambiguous ALONE, but `--marketplace <global>` resolves it to an enabled add — and
    the pre-existing marketplace block survives."""
    index = tmp_path / "plugins.toml"
    index.write_text('[marketplace.pubmkt]\nkind = "global"\n')
    env = _env(cli_env, tmp_path, index=index)
    p = run_fleet(env, "plugins", "add", "coolplug", "--marketplace", "pubmkt")
    assert "ENABLED" in p.stdout
    doc = tomllib.loads(index.read_text())
    assert doc["plugin"]["coolplug"]["type"] == "enabled"
    assert doc["plugin"]["coolplug"]["source"] == "pubmkt"
    assert doc["marketplace"]["pubmkt"]["kind"] == "global"  # marketplace preserved


def test_add_already_indexed_is_noop_pointer(cli_env, tmp_path):
    """Idempotency: an already-indexed ref (matched by its derived NAME) is a no-op that points at the
    enable one-liner and re-clones/re-writes NOTHING."""
    index = tmp_path / "plugins.toml"
    index.write_text('[plugin.loom]\ntype = "linked"\nsource = "berg"\ntools = ["claude"]\n')
    before = index.read_text()
    env = _env(cli_env, tmp_path, index=index)
    p = run_fleet(env, "plugins", "add", "https://github.com/x/loom.git")   # basename -> 'loom' (indexed)
    assert "already indexed" in p.stdout and "--use loom" in p.stdout
    assert index.read_text() == before                       # byte-identical: no write, no clone attempted


def test_add_bare_ref_stops_writes_nothing(cli_env, tmp_path):
    """A bare, un-inferable ref STOPs (rc 2) and writes nothing — it refuses to guess a security call."""
    index = tmp_path / "plugins.toml"                        # absent
    env = _env(cli_env, tmp_path, index=index)
    p = run_fleet(env, "plugins", "add", "mysteryplug", expect=2)
    assert "STOP" in (p.stdout + p.stderr)
    assert not index.exists()


def test_add_linked_dry_run_writes_nothing(cli_env, tmp_path):
    src = _mkplugin_src(tmp_path / "src", "dryplug")
    mkt = tmp_path / "mkt" / "plugins"
    mkt.mkdir(parents=True)
    index = tmp_path / "plugins.toml"
    env = _env(cli_env, tmp_path, index=index, marketplace=mkt)
    p = run_fleet(env, "plugins", "add", str(src), "--dry-run")
    assert "[dry-run]" in p.stdout and "LINKED" in p.stdout
    assert not index.exists()                                # no index write
    assert not (mkt / "dryplug").exists()                    # no clone/copy


def test_add_enabled_dry_run_writes_nothing(cli_env, tmp_path):
    index = tmp_path / "plugins.toml"
    env = _env(cli_env, tmp_path, index=index)
    p = run_fleet(env, "plugins", "add", "obsidian@obs", "--dry-run")
    assert "[dry-run]" in p.stdout and "ENABLED" in p.stdout
    assert not index.exists()


def test_add_as_linked_on_name_at_marketplace_stops(cli_env, tmp_path):
    """`--as linked` on a name@marketplace ref STOPs — a linked add needs something to clone/copy."""
    mkt = tmp_path / "mkt" / "plugins"
    mkt.mkdir(parents=True)
    index = tmp_path / "plugins.toml"
    env = _env(cli_env, tmp_path, index=index, marketplace=mkt)
    p = run_fleet(env, "plugins", "add", "obsidian@obs", "--as", "linked", expect=2)
    assert "needs a git URL or a local path" in (p.stdout + p.stderr)
    assert not index.exists()


def test_add_as_enabled_without_marketplace_stops(cli_env, tmp_path):
    """`--as enabled` on a path ref (no marketplace anywhere) STOPs — an enabled add needs a marketplace."""
    src = _mkplugin_src(tmp_path / "src", "pluglocal")
    index = tmp_path / "plugins.toml"
    env = _env(cli_env, tmp_path, index=index)
    p = run_fleet(env, "plugins", "add", str(src), "--as", "enabled", expect=2)
    assert "needs a marketplace" in (p.stdout + p.stderr)
    assert not index.exists()


def test_add_linked_no_marketplace_stops(cli_env, tmp_path):
    """A linked add with no local marketplace to clone into STOPs rather than dumping the checkout anywhere."""
    src = _mkplugin_src(tmp_path / "src", "plugx")
    index = tmp_path / "plugins.toml"                        # no [marketplace.default], no $CMUX_FLEET_MARKETPLACE
    env = _env(cli_env, tmp_path, index=index)
    p = run_fleet(env, "plugins", "add", str(src), expect=2)
    assert "no LOCAL marketplace" in (p.stdout + p.stderr)
    assert not index.exists()


# ================================================================ SAFETY — malformed index ABORTS ===
# A PRESENT-but-unparseable plugins.toml must ABORT the write (rc 2, file byte-unchanged) rather than
# regenerate from sources — which would silently discard every hand-authored entry (review Finding 1).
# `_MALFORMED` is a POPULATED index (a curated entry + a marketplace def) plus one junk line that breaks
# the TOML parse — so the assertion "file byte-unchanged" proves the curated data was NOT lost.
_MALFORMED = ('[marketplace.berg]\npath = "mkt/plugins"\n'
              '[plugin.handmade]\ntype = "linked"\nsource = "berg"\ntools = ["claude"]\n'
              'description = "curated by hand"\ncurated = true\n'
              'this line is a typo that breaks the parse\n')


def test_reconcile_aborts_on_malformed_index_file_unchanged(cli_env, tmp_path):
    """`fleet plugins reconcile` on a malformed populated index -> rc 2, message names the file, and the
    file is BYTE-UNCHANGED (the pre-fix behavior regenerated it from sources, losing the curated data)."""
    mkt = tmp_path / "mkt" / "plugins"
    mkt.mkdir(parents=True)
    index = tmp_path / "plugins.toml"
    index.write_text(_MALFORMED)
    env = _env(cli_env, tmp_path, index=index, marketplace=mkt)
    p = run_fleet(env, "plugins", "reconcile", expect=2)
    assert "malformed" in (p.stdout + p.stderr) and str(index) in (p.stdout + p.stderr)
    assert index.read_text() == _MALFORMED                   # curated data untouched


def test_add_linked_aborts_on_malformed_index_nothing_cloned(cli_env, tmp_path):
    """`add --as linked` on a malformed populated index -> rc 2, index BYTE-UNCHANGED, and NOTHING cloned
    (the abort fires before the clone, so no stray checkout is left behind)."""
    src = _mkplugin_src(tmp_path / "src", "newplug")
    mkt = tmp_path / "mkt" / "plugins"
    mkt.mkdir(parents=True)
    index = tmp_path / "plugins.toml"
    index.write_text(_MALFORMED)
    env = _env(cli_env, tmp_path, index=index, marketplace=mkt)
    p = run_fleet(env, "plugins", "add", str(src), "--as", "linked", expect=2)
    assert "malformed" in (p.stdout + p.stderr) and str(index) in (p.stdout + p.stderr)
    assert index.read_text() == _MALFORMED                   # curated data untouched
    assert not (mkt / "newplug").exists()                    # bailed BEFORE cloning


def test_add_enabled_aborts_on_malformed_index_file_unchanged(cli_env, tmp_path):
    """`add name@marketplace` (enabled) on a malformed populated index -> rc 2, index BYTE-UNCHANGED."""
    index = tmp_path / "plugins.toml"
    index.write_text(_MALFORMED)
    env = _env(cli_env, tmp_path, index=index)
    p = run_fleet(env, "plugins", "add", "obsidian@obsidian-skills", expect=2)
    assert "malformed" in (p.stdout + p.stderr) and str(index) in (p.stdout + p.stderr)
    assert index.read_text() == _MALFORMED                   # curated data untouched


def test_add_on_absent_index_starts_fresh(cli_env, tmp_path):
    """Regression guard: the abort must NOT fire on the normal first-run path — an ABSENT index still lets
    an enabled add create a fresh file with the new entry."""
    index = tmp_path / "plugins.toml"                        # never created
    env = _env(cli_env, tmp_path, index=index)
    p = run_fleet(env, "plugins", "add", "obsidian@obsidian-skills")
    assert "ENABLED" in p.stdout
    assert tomllib.loads(index.read_text())["plugin"]["obsidian"]["type"] == "enabled"
