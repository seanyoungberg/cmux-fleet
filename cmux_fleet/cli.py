#!/usr/bin/env python3
# cmux_fleet/cli.py (was scripts/fleet.py) - the native-cmux fleet CLI. ONE tool, tool-agnostic. The `fleet` namespace is the
# umbrella for the rest of the scripts (state/drive/digest/ack).
#
#   fleet launch <role> [launcher flags] [-- <verbatim tool flags>]
#   fleet launch --adhoc <name> --tool claude [-- --model opus]   # off-roster dynamic agent
#
# DESIGN (see docs/architecture.md "Dispatch"): the launcher is a dumb command BUILDER over three
# NATIVE config channels, it invents no setting names of its own:
#   1. CLI flags  - everything the tool exposes as a flag (--model, --effort, --permission-mode ...).
#                   Highest controllable priority. FULL passthrough: anything after `--` is forwarded
#                   verbatim, the launcher never needs to know a flag exists.
#   2. env        - our orchestration vars (AGENT_ROLE, auto-set) + the tool's env-ONLY config
#                   (ANTHROPIC_BASE_URL, MAX_THINKING_TOKENS, ... no flag exists).
#   3. --settings - settings-ONLY config (permissions / hooks / statusLine). Optional, wrapper-merged.
# Prefer a flag when one exists; env only for env-only config + our vars; --settings only for what
# only settings can do. The ONLY config keys that aren't raw flags are the ones that need RESOLUTION
# (plugins: marketplace name -> --plugin-dir path) or aren't flags at all (cwd/place/group/kind/env/
# settings). Native flags stay raw in the `flags` string.
#
# CONFIG is role-first, tool-nested (CMUX_FLEET_TOML, default $XDG_CONFIG_HOME/cmux-fleet/fleet.toml):
#   [defaults]            tool="claude" + orchestration floor (kind/place/group)
#   [tool.<t>]            per-tool launch floor (plugins/flags/env/settings) -> the adapter's defaults
#   [role.<name>]         tool-agnostic orchestration (cwd/place/group/kind) + the role's default tool
#   [role.<name>.<t>]     that role's config for tool <t> (plugins/flags/env/settings)
# Resolution for `launch <role>`: tool = --tool | role.tool | defaults.tool. Merge
# [defaults] (orchestration) -> [role] scalars -> tool config [tool.<t>] -> [role.<t>] -> caller `--`.
import argparse, json, os, shlex, subprocess, sys, tempfile, time

from .config import ROOT, STATE, CMUX, MARKETPLACE, FLOOR, FLEET_TOML, ADHOC_SUBDIR, PLUGIN_INDEX, load_plugin_index  # path resolver

# The checkout/build root: the dir that holds bin/, .claude-plugin/, fleet.toml.example next to the
# cmux_fleet package. In a repo/editable install this is the repo root (unchanged from the flat layout,
# where it was dirname(dirname(scripts/fleet.py))). In a WHEEL/venv install it is site-packages — which
# holds NONE of bin/, .claude-plugin/, or a repo-root fleet.toml.example — so `fleet profile` must NOT
# derive its pins from it there (see _fleet_bin_dir / _marketplace_pin / _seed_example_text below, and
# the codex P1.1 fix). PLUGIN_ROOT stays only as the checkout-detection anchor + editable-install seed
# path; it is never emitted as a marketplace or bin dir unless it is provably a real plugin checkout.
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(STATE, "fleet-registry.json")


def _is_plugin_checkout(root=PLUGIN_ROOT):
    """True only when `root` is a real cmux-fleet plugin CHECKOUT (has .claude-plugin/marketplace.json
    next to the package). False for a wheel/venv install, where PLUGIN_ROOT is site-packages."""
    return os.path.exists(os.path.join(root, ".claude-plugin", "marketplace.json"))


def _fleet_bin_dir():
    """The dir to prepend to PATH so `fleet` (and its `python -m cmux_fleet`) resolve to THIS build.
    Three concepts kept separate from the plugin root (codex P1.1):
      - explicit override: $CMUX_FLEET_BIN (a fleet executable path OR its containing dir);
      - checkout: the repo's bin/ dev shim (bin/fleet), the historical multi-build-isolation pin;
      - wheel/venv: the dir of the INSTALLED `fleet` console script (never site-packages/bin, which
        does not exist). Falls back to which()/argv[0].
    Returns "" if no real app bin dir can be resolved (caller then omits the PATH pin rather than
    emitting a bogus site-packages path)."""
    env = os.environ.get("CMUX_FLEET_BIN", "").strip()
    if env:
        env = os.path.abspath(os.path.expanduser(env))
        return env if os.path.isdir(env) else os.path.dirname(env)
    checkout_bin = os.path.join(PLUGIN_ROOT, "bin")
    if _is_plugin_checkout() and os.path.exists(os.path.join(checkout_bin, "fleet")):
        return checkout_bin                            # dev shim, real checkout (not a build-cache copy)
    # Installed console script: sys.argv[0] IS the exact invoked `fleet` path -> the most reliable pin
    # (an absolute `.../bin/fleet`). Falls back to which() for a bare-name invocation.
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.sep in argv0 and os.path.basename(argv0).startswith("fleet"):
        return os.path.dirname(os.path.abspath(argv0))
    import shutil as _sh
    exe = _sh.which("fleet")
    return os.path.dirname(exe) if exe else ""


def _marketplace_pin():
    """The dir to emit as $CMUX_FLEET_MARKETPLACE (so a roster's plugins=["<build-name>"] resolves to
    THIS build's plugin). EXPLICIT config wins; else inferred ONLY from a real checkout — NEVER from a
    wheel's site-packages (codex P1.1). Returns "" -> caller omits the pin (internal --plugin-dir
    resolution stays disabled, which is correct for a wheel install with no bundled plugin)."""
    if MARKETPLACE:                                   # env CMUX_FLEET_MARKETPLACE / [fleet].marketplace
        return MARKETPLACE
    if _is_plugin_checkout():
        return os.path.dirname(PLUGIN_ROOT)           # parent holds the build dir; plugins=["<name>"] -> it
    return ""


def _seed_example_text():
    """The bundled fleet.toml.example seed roster text, or None. Read via importlib.resources for a
    WHEEL install (force-included at cmux_fleet/fleet.toml.example), falling back to the repo-root
    fleet.toml.example for a CHECKOUT/editable install (where it lives outside the package)."""
    try:
        from importlib.resources import files
        r = files("cmux_fleet").joinpath("fleet.toml.example")
        if r.is_file():
            return r.read_text()
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        pass
    p = os.path.join(PLUGIN_ROOT, "fleet.toml.example")
    if os.path.exists(p):
        with open(p) as f:
            return f.read()
    return None


def _profile_env():
    """The build/profile-pinning env injected into EVERY child launch so a child — and its hooks, and any
    sub-fleet it launches — resolve the SAME build state/config/marketplace as the launcher, instead of
    whatever its ambient shell happens to carry. This is what makes a profile HERMETIC across the
    parent->child boundary (the core multi-build-isolation requirement): without it a child could inherit
    a different build's CMUX_STATE_DIR and split-brain the registry/inbox."""
    e = {"CMUX_STATE_DIR": STATE, "CMUX_FLEET_TOML": FLEET_TOML, "CMUX_FLEET_ROOT": ROOT, "CMUX_BIN": CMUX}
    if MARKETPLACE:
        e["CMUX_FLEET_MARKETPLACE"] = MARKETPLACE
    return e

try:
    import tomllib
except ModuleNotFoundError:
    sys.exit("fleet: needs python3.11+ (tomllib)")


def cmuxq(*args):
    """Run a cmux subcommand, return stdout (str). Quiet stderr."""
    env = dict(os.environ, CMUX_QUIET="1")
    p = subprocess.run([CMUX, *args], capture_output=True, text=True, env=env)
    return (p.stdout or "") + (p.stderr or "")


# ---------------------------------------------------------------- config resolution
def load_config():
    # No roster file -> empty config. Ad-hoc launches need no roster, and a named-role launch then
    # fails with resolve()'s clean "role not found" message instead of a read error. A roster that
    # EXISTS but is malformed is a real error worth surfacing.
    if not os.path.exists(FLEET_TOML):
        return {}
    try:
        return tomllib.load(open(FLEET_TOML, "rb"))
    except Exception as e:
        sys.exit(f"fleet: cannot read {FLEET_TOML}: {e}")


def resolve(cfg, role, tool_override, adhoc_name):
    """Return the fully-merged launch spec for a role (or an ad-hoc name)."""
    defaults = cfg.get("defaults", {}) or {}
    tools = cfg.get("tool", {}) or {}
    roles = cfg.get("role", {}) or {}

    if adhoc_name:                                            # off-roster dynamic agent
        rblock = {}
        orch_scalars = {"cwd": os.path.join(ADHOC_SUBDIR, adhoc_name)}
        label = adhoc_name
    else:
        if role not in roles:
            sys.exit(f"fleet: role '{role}' not in {FLEET_TOML}")
        rblock = roles[role]
        orch_scalars = {k: v for k, v in rblock.items() if not isinstance(v, dict)}
        label = role

    tool = tool_override or orch_scalars.get("tool") or defaults.get("tool") or "claude"
    if not adhoc_name and tool not in rblock and not (tools.get(tool)):
        # role exists but neither it nor a [tool.<t>] floor defines this tool
        sys.exit(f"fleet: role '{role}' has no config for tool '{tool}' (no [role.{role}.{tool}] or [tool.{tool}])")

    tdef = tools.get(tool, {}) or {}                          # [tool.<t>] floor
    rtool = (rblock.get(tool) if isinstance(rblock.get(tool), dict) else {}) or {}  # [role.<name>.<t>]

    # orchestration: [defaults] (drop tool key) <- role scalars
    orch = {k: v for k, v in defaults.items() if k != "tool" and not isinstance(v, dict)}
    orch.update(orch_scalars)
    orch.pop("tool", None)

    # launch channels
    plugins = _dedup((tdef.get("plugins") or []) + (rtool.get("plugins") or []))
    # `use` = mechanism-agnostic plugin list resolved through the index (design §3). Unioned floor ∪ role
    # exactly like `plugins`; the index says linked vs enabled at compile time so a role author needn't.
    # Coexists with legacy `plugins`/`enable_plugins` (the composed set is their union — adapter_compile).
    use = _dedup((tdef.get("use") or []) + (rtool.get("use") or []))
    flags = _layer_tokens([shlex.split(tdef.get("flags", "")),           # tool-floor <- role
                           shlex.split(rtool.get("flags", ""))])
    env = {**(tdef.get("env") or {}), **(rtool.get("env") or {})}
    settings = rtool.get("settings") or tdef.get("settings") or ""
    # dynamic-loadout keys (claude): enable_plugins = EXTERNAL marketplace plugins to flip on per-agent
    # (-> enabledPlugins injected via inline --settings); INTERNAL plugins stay on --plugin-dir. They are
    # SEPARATE mechanisms (a --plugin-dir plugin is active without an enabledPlugins entry; verified).
    # setting_sources -> --setting-sources (which settings layers load; excluding 'project' is the
    # migration-compat lever so our launches ignore the agent's own .claude/, unlike AD launches).
    enable_plugins = _dedup((tdef.get("enable_plugins") or []) + (rtool.get("enable_plugins") or []))
    setting_sources = rtool.get("setting_sources") or tdef.get("setting_sources") or ""

    return {
        "tool": tool, "role": label, "label": label,   # role = behavioral type; label defaults to it
        "kind": orch.get("kind", "child"),
        "place": orch.get("place", "tab"),
        "group": orch.get("group", ""),
        "cwd": orch.get("cwd", ""),
        "plugins": plugins, "flags": flags, "env": env, "settings": settings,
        "use": use,
        "enable_plugins": enable_plugins, "setting_sources": setting_sources,
        # worktree (config-gated, default-off): isolate this agent in a git worktree off its repo cwd.
        "worktree": bool(orch.get("worktree", False)),
        "worktree_base": orch.get("worktree_base", ""),
        "worktree_dir": orch.get("worktree_dir", ".worktrees"),
        "worktree_branch_prefix": orch.get("worktree_branch_prefix", "fleet/"),
    }


def _dedup(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def _flatten_csv(values):
    """Flatten a repeatable + comma-sep flag into a clean name list — `--use a,b --use c` (append) and
    `--use a,b` (comma) both land as ['a','b','c']. Matches `--plugins`' comma shape AND `--add-plugin`'s
    repeatable shape in one flag, so `--use` reads either way."""
    out = []
    for v in (values or []):
        out += [s.strip() for s in str(v).split(",") if s.strip()]
    return out


def _flag_keys(tokens):
    return {t.split("=", 1)[0] for t in tokens if t.startswith("--")}


def _drop_keys(tokens, drop):
    """Remove `--key [value]` whose key is in `drop`. Heuristic (safe on OUR authored flag strings):
    a flag consumes the next token as its value iff that token does not start with '-'."""
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--") and t.split("=", 1)[0] in drop:
            if "=" not in t and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                i += 2                                        # skip flag + its value
            else:
                i += 1                                        # skip bool flag / --key=val
            continue
        out.append(t)
        i += 1
    return out


def _layer_tokens(layers):
    """Layer token-lists low->high precedence. A higher layer's --key overrides a lower one's
    (drops the lower --key and its value); repeatable flags the higher layer doesn't name are
    preserved. Operates on already-tokenized lists (no re-split, so multi-word values survive)."""
    merged = []
    for layer in layers:
        merged = _drop_keys(merged, _flag_keys(layer)) + list(layer)
    return merged


# ---------------------------------------------------------------- tool adapters
def _claude_settings_args(spec, extra_enabled=()):
    """`--settings` args for claude: the role's `settings` (a file path or inline JSON) plus an
    enabledPlugins object synthesized from `enable_plugins` (EXTERNAL marketplace plugins to flip on
    for this agent) UNIONED with `extra_enabled` (index-resolved `use` entries of type=enabled, as
    "<plugin>@<marketplace>" refs). enabledPlugins format is {"<plugin>@<marketplace>": true} (the same
    shape claude writes in settings.json). We emit ONE --settings when we can (role settings is inline
    JSON or absent -> fold them together); only when the role pins a settings FILE *and* also enables
    plugins do we emit two --settings, which is safe because the cmux-claude-wrapper deep-merges multiple
    --settings (and its own hooks) into a single one before claude ever sees them (verified in
    Resources/bin/cmux-claude-wrapper). The JSON must be valid or the wrapper warns + drops it."""
    base = (spec.get("settings") or "").strip()
    ep = {name: True for name in _dedup(list(spec.get("enable_plugins") or []) + list(extra_enabled))}
    if not ep:
        return ["--settings", base] if base else []
    if not base:
        return ["--settings", json.dumps({"enabledPlugins": ep})]
    if base.startswith("{"):                                  # inline JSON -> fold into one object
        try:
            obj = json.loads(base)
            obj.setdefault("enabledPlugins", {}).update(ep)
            return ["--settings", json.dumps(obj)]
        except Exception:
            pass                                              # malformed -> fall through to two flags
    return ["--settings", base, "--settings", json.dumps({"enabledPlugins": ep})]


def _plugin_dir(name):
    """Resolve an INTERNAL plugin name to a --plugin-dir path, or None. An absolute/~ path is used
    as-is (if it exists); a bare name is looked up under MARKETPLACE. With no marketplace configured,
    bare names resolve to None (the caller warns + skips) — so the engine needs no marketplace to run."""
    expanded = os.path.expanduser(name)
    if os.path.isabs(expanded):
        return expanded if os.path.exists(expanded) else None
    if not MARKETPLACE:
        return None
    pd = os.path.join(MARKETPLACE, name)
    return pd if os.path.exists(pd) else None


def _linked_dir(name, source, index):
    """Resolve a `type=linked` plugin name to a --plugin-dir path (or None to warn+skip). Generalizes
    `_plugin_dir` to a NAMED marketplace: an absolute/~ path is used as-is; else join the plugin under
    `[marketplace.<source>].path`. Falls back to `_plugin_dir` (the single default marketplace) when the
    source has no path (kind=global, unknown, or absent) — which keeps default-marketplace resolution
    byte-identical to today."""
    expanded = os.path.expanduser(name)
    if os.path.isabs(expanded):                              # abs/~ bypasses the marketplace (as today)
        return expanded if os.path.exists(expanded) else None
    mk = index["marketplaces"].get(source or "default")
    if mk and mk.get("path"):
        pd = os.path.join(mk["path"], name)
        return pd if os.path.exists(pd) else None
    return _plugin_dir(name)                                 # default marketplace / no source -> today's path


def _resolve_use(use_names, index):
    """Resolve the unioned `use` list through the index into the two native channels (design §3).
    Returns (linked_dirs, enabled_refs, unresolved):
      - in index & type=enabled -> "<name>@<source>" accumulated for enabledPlugins (via --settings).
      - in index & type=linked  -> a --plugin-dir path via the entry's source marketplace.
      - NOT in index            -> today's behavior: abs/~ path as-is, else bare name under the default
                                   marketplace (`_plugin_dir`). This fall-through preserves back-compat.
    A name that should resolve to a dir but doesn't (missing marketplace/dir) lands in `unresolved`
    (caller warns + skips, exactly as the legacy --plugin-dir loop does)."""
    linked, enabled, unresolved = [], [], []
    for name in use_names:
        entry = index["plugins"].get(name)
        if entry and entry.get("type") == "enabled":
            src = (entry.get("source") or "").strip()
            enabled.append(f"{name}@{src}" if src else name)
            continue
        source = entry.get("source") if entry else ""        # linked (indexed) or unindexed fall-through
        pd = _linked_dir(name, source, index)
        (linked if pd else unresolved).append(pd if pd else name)
    return linked, enabled, unresolved


def adapter_compile(tool, spec, caller_tokens):
    """Compile {plugins, flags, env, settings} + caller passthrough -> (bin, arg_tokens, env_map)
    for the given tool. Adding a tool = adding a branch here + a [tool.<t>] block."""
    # spec['flags'] is already a token list (tool-floor<-role); layer the caller passthrough on top.
    merged = _layer_tokens([spec["flags"], list(caller_tokens or [])])
    env = {**_profile_env(), **dict(spec["env"])}            # build/profile pins first; a role's env wins
    env["AGENT_ROLE"] = spec["role"]                          # behavioral type (exposed to the agent)
    env["AGENT_LABEL"] = spec["label"]                        # unique instance -> routing/recycle

    if tool == "claude":
        args = []
        if spec.get("setting_sources"):                      # which settings layers claude loads
            args += ["--setting-sources", spec["setting_sources"]]
        # index-aware `use` -> the SAME two channels the legacy keys feed (design §3). No `use` -> both
        # lists empty -> the composition below is byte-identical to the pre-index path.
        use_linked, use_enabled, use_unresolved = ([], [], [])
        if spec.get("use"):
            use_linked, use_enabled, use_unresolved = _resolve_use(spec["use"], load_plugin_index())
            for name in use_unresolved:
                print(f"[fleet] warn: plugin '{name}' (use) not resolvable (marketplace unset or not found); skipping")
        seen = set()
        for name in spec["plugins"]:                          # legacy INTERNAL plugins: load + auto-enable
            pd = _plugin_dir(name)
            if pd:
                args += ["--plugin-dir", pd]; seen.add(pd)
            else:
                print(f"[fleet] warn: plugin '{name}' not resolvable (marketplace unset or not found); skipping")
        for pd in use_linked:                                 # `use` linked plugins, unioned + deduped by path
            if pd not in seen:
                args += ["--plugin-dir", pd]; seen.add(pd)
        args += merged
        args += _claude_settings_args(spec, use_enabled)     # role `settings` + EXTERNAL/`use` enabledPlugins
        return "claude", args, env

    if tool == "codex":
        # Stub: codex has its own plugin/settings vocabulary; flags+env passthrough work today. The index
        # can EXPRESS codex plugins (tools=["codex"] + [plugin.<n>.codex] blocks) but this phase does NOT
        # provision CODEX_HOME (deferred) -> plugins/settings/use are still no-ops for codex, so warn.
        if spec["plugins"] or spec["settings"] or spec.get("use"):
            print("[fleet] warn: 'plugins'/'settings'/'use' are not yet provisioned for codex; ignored")
        return "codex", merged, env

    sys.exit(f"fleet: unknown tool '{tool}' (no adapter)")


# ---------------------------------------------------------------- cmux placement (ported, proven)
def _store():
    from . import state as fs                                  # union of all per-agent hook stores
    return fs.read_hook_store()                               # (claude/codex/... -> tool-agnostic poll/ls)


def surface_loc(surf):
    """(workspace_uuid, pane_uuid) for a surface UUID, parsed from the full tree."""
    import re
    txt = cmuxq("tree", "--all", "--id-format", "both")
    ws = pane = None
    UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    for line in txt.splitlines():
        mw = re.search(r"workspace\s+workspace:\d+\s+(" + UUID + ")", line)
        if mw:
            ws = mw.group(1); continue
        mp = re.search(r"pane\s+pane:\d+\s+(" + UUID + ")", line)
        if mp:
            pane = mp.group(1); continue
        ms = re.search(r"surface\s+surface:\d+\s+(" + UUID + ")", line)
        if ms and ms.group(1).upper() == surf.upper():
            return ws, pane
    return None, None


def ws_uuid_for_surface(surf):
    for s in (_store().get("sessions") or {}).values():
        if (s.get("surfaceId") or "").upper() == surf.upper():
            return s.get("workspaceId", "")
    return ""


def surface_ws_from_tree(tree_text, surf):
    """Parse `cmux tree` TEXT -> the workspace UUID that CONTAINS `surf`, or '' if `surf` is not in the
    tree (genuinely closed). PURE (no shell-out) so register's workspace derivation and the router's
    move-vs-close arbiter share ONE parser and both stay unit-testable without a live cmux. Walks the
    nested `window > workspace > pane > surface` lines exactly like surface_loc, tracking the most
    recent workspace line seen before the matching surface line."""
    import re
    UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    ws = ""
    for line in (tree_text or "").splitlines():
        mw = re.search(r"workspace\s+workspace:\d+\s+(" + UUID + ")", line)
        if mw:
            ws = mw.group(1); continue
        ms = re.search(r"surface\s+surface:\d+\s+(" + UUID + ")", line)
        if ms and ms.group(1).upper() == surf.upper():
            return ws
    return ""


def current_ws_for_surface(surf):
    """The workspace UUID that CURRENTLY contains `surf`, from cmux's live TREE (the visual ground
    truth), falling back to the hook store only when the tree can't be read. Prefer the TREE because the
    hook store's workspaceId FREEZES when a surface is MOVED across workspaces (root cause #3) -- so
    register/`move` record where the surface lives NOW, not where it was first bound. '' if unlocatable."""
    return surface_ws_from_tree(cmuxq("tree", "--all", "--id-format", "both"), surf) or ws_uuid_for_surface(surf)


def _all_workspace_uuids(tree_text):
    """Every workspace UUID present in `cmux tree` TEXT (pure). Used to snapshot the workspace set
    before/after a `workspace-group create` so `group init` can spot the empty scaffolding anchor cmux
    spawns and close it (the 2026-07-02 empty-anchor footgun)."""
    import re
    UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    return {m.group(1) for m in re.finditer(r"workspace\s+workspace:\d+\s+(" + UUID + ")", tree_text or "")}


def _ref_to_uuid(kind, ref):
    import re
    txt = cmuxq("tree", "--all", "--id-format", "both")
    UUID = r"[0-9A-Fa-f-]{36}"
    for line in txt.splitlines():
        m = re.search(rf"{kind}\s+{re.escape(ref)}\s+(" + UUID + ")", line)
        if m:
            return m.group(1)
    return ""


def _term_surface_in(ws_uuid, pane_ref=None):
    """The terminal surface UUID in a freshly-created pane/workspace (exactly one terminal)."""
    args = ["list-pane-surfaces", "--workspace", ws_uuid, "--json", "--id-format", "both"]
    if pane_ref:
        args += ["--pane", pane_ref]
    try:
        d = json.loads(cmuxq(*args))
    except Exception:
        return ""
    panes = d.get("panes") or [d]
    for p in panes:
        for s in (p.get("surfaces") or d.get("surfaces") or []):
            if s.get("type") == "terminal" and s.get("id"):
                return s["id"]
    return ""


def _group_ref(name_or_ref):
    """Resolve a workspace-group NAME to its ref (`workspace_group:N`), or '' if no such group exists.
    A value already in ref form is returned unchanged. cmux's `new-workspace --group` and
    `workspace-group delete` both take a REF, never a name, so every group op routes through here."""
    if not name_or_ref:
        return ""
    if name_or_ref.startswith("workspace_group:"):
        return name_or_ref
    try:
        gd = json.loads(cmuxq("workspace-group", "list", "--json"))
        return next((g["ref"] for g in gd.get("groups", []) if g.get("name") == name_or_ref), "")
    except Exception:
        return ""


def _group_member_workspaces(gref):
    """The REAL (cmux ground-truth) workspace-uuid membership of a group ref, for cross-checking the
    registry's belief before a destructive `--with-group` dissolve (root-cause #3 of the 2026-07-02
    incident: a registry `group` field can diverge from cmux's actual visual group). `workspace-group
    list --json` reports members as short refs (`member_workspace_refs`: ["workspace:11", ...]), not
    UUIDs -- resolve each through the same _ref_to_uuid `cmux tree` lookup used everywhere else in this
    file. Returns None (not an empty set) if the group data can't be read or the ref isn't listed at all
    -- the caller must treat that as fail-closed, not as 'zero real members'."""
    try:
        gd = json.loads(cmuxq("workspace-group", "list", "--json"))
    except Exception:
        return None
    g = next((x for x in (gd.get("groups") or []) if x.get("ref") == gref), None)
    if g is None:
        return None
    return {_ref_to_uuid("workspace", r) for r in (g.get("member_workspace_refs") or [])}


def create_surface(spec, parent_surf, direction):
    """Create the target surface per spec['place']; return (ws_uuid, surf_uuid). Aborts (None) on any
    unresolved UUID -- never send blind."""
    import re
    place = spec["place"]
    if place in ("tab", "pane"):
        cws = ws_uuid_for_surface(parent_surf)
        if not cws:
            print("[fleet] ABORT: cannot resolve conductor workspace from --parent"); return None, None
        if place == "tab":
            _, agents_pane = surface_loc(parent_surf)
            if not agents_pane:
                print("[fleet] ABORT: cannot resolve conductor agents-pane for tab placement"); return None, None
            out = cmuxq("new-surface", "--workspace", cws, "--pane", agents_pane,
                        "--type", "terminal", "--focus", "false")
            m = re.search(r"(surface:\d+)", out)
            if not m:
                print(f"[fleet] ABORT: new-surface gave no surface ref: {out.strip()}"); return None, None
            return cws, _ref_to_uuid("surface", m.group(1))
        out = cmuxq("new-pane", "--workspace", cws, "--type", "terminal",
                    "--direction", direction, "--focus", "false")
        m = re.search(r"(pane:\d+)", out)
        return cws, _term_surface_in(cws, m.group(1) if m else None)

    if place == "workspace":
        group = spec["group"]
        if not group:
            print("[fleet] ABORT: place=workspace needs a group"); return None, None
        gref = _group_ref(group)
        if gref:                                              # group EXISTS -> join it
            out = cmuxq("new-workspace", "--group", gref, "--name", spec["label"],
                        "--cwd", spec["abs_cwd"], "--focus", "false")
            m = re.search(r"(workspace:\d+)", out)
            if not m:
                print(f"[fleet] ABORT: new-workspace gave no workspace ref: {out.strip()}"); return None, None
            ws = _ref_to_uuid("workspace", m.group(1))
            return ws, _term_surface_in(ws)
        # group does NOT exist -> BOOTSTRAP it, anchored on this agent's OWN new workspace (one
        # conductor = one group). Create the workspace standalone, THEN `workspace-group create --from
        # <that ref>` with an ALWAYS-EXPLICIT --from: the implicit form adopts the CALLER's workspace
        # (a known footgun). Closing this anchor later dissolves the whole group.
        out = cmuxq("new-workspace", "--name", spec["label"], "--cwd", spec["abs_cwd"], "--focus", "false")
        m = re.search(r"(workspace:\d+)", out)
        if not m:
            print(f"[fleet] ABORT: new-workspace (anchor) gave no workspace ref: {out.strip()}"); return None, None
        anchor_ref = m.group(1)
        cmuxq("workspace-group", "create", "--name", group, "--from", anchor_ref)
        if _group_ref(group):
            print(f"[fleet] created group '{group}' anchored on {spec['label']} ({anchor_ref})")
        else:
            print(f"[fleet] warn: group '{group}' did not register; {spec['label']} workspace is standalone")
        ws = _ref_to_uuid("workspace", anchor_ref)
        return ws, _term_surface_in(ws)

    print(f"[fleet] ABORT: unknown place '{place}'"); return None, None


def poll_session(surf, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        d = _store()
        e = (d.get("activeSessionsBySurface") or {}).get(surf) or {}
        sid = e.get("sessionId")
        if not sid:
            for s in (d.get("sessions") or {}).values():
                if (s.get("surfaceId") or "").upper() == surf.upper():
                    sid = s.get("sessionId"); break
        if sid:
            return sid
        time.sleep(1)
    return ""


def _surface_cwd(surf):
    """The working directory cmux recorded for a surface's bound session, or None. Used by placement
    reconciliation (the agent runs `cd <abs_cwd> && <tool>`, so a correctly-placed worktree session
    reports the worktree path here)."""
    d = _store()
    e = (d.get("activeSessionsBySurface") or {}).get(surf) or {}
    if e.get("cwd"):
        return e["cwd"]
    for s in (d.get("sessions") or {}).values():
        if (s.get("surfaceId") or "").upper() == surf.upper() and s.get("cwd"):
            return s["cwd"]
    return None


def _poll_surface_cwd(surf, want, timeout=10):
    """Poll _surface_cwd until it matches `want` (or times out); returns the last value seen."""
    end, last = time.time() + timeout, None
    while time.time() < end:
        last = _surface_cwd(surf)
        if last and os.path.realpath(last) == os.path.realpath(want):
            return last
        time.sleep(0.5)
    return last


def register(surf, spec, parent_surface, session, ws):
    from . import state as fs
    parent_label = fs.label_for_surface(parent_surface) or parent_surface   # store parent LABEL (durable)
    fs.live_put(spec["label"], {
        "role": spec["role"], "kind": spec["kind"], "tool": spec["tool"],
        "cwd": spec["abs_cwd"], "parent": parent_label, "place": spec["place"], "status": "live",
        "surface": surf, "workspace": ws,
        "session": f"claude-{session}" if spec["tool"] == "claude" else session,
        # carried so archive->revive can rebuild the launch without re-resolving the roster
        "plugins": spec["plugins"], "flags": spec["flags"], "settings": spec["settings"],
        # provider attribution (providers feature): "tool:name" the agent launched under, for `fleet usage`
        "provider": spec.get("provider", ""),
        # group is only ever REAL cmux-side membership when place=="workspace" -- that's the one branch
        # of create_surface() that touches workspace-group at all (join/bootstrap). A role's toml (or a
        # caller --group) can carry a `group` value alongside place="tab"/"pane" (e.g. a --place override
        # on a workspace-default role); persisting it there anyway is exactly the 2026-07-02 root cause
        # (Item 2 point 3): a registry row claims group membership its surface never actually joined, and
        # `fleet rm --with-group` trusted that claim without checking placement. Scrub it here so the
        # registry can never assert membership create_surface didn't enact.
        "group": spec["group"] if spec["place"] == "workspace" else "",
        # worktree bookkeeping (present only for worktree-isolated agents): repo/path/branch so
        # `fleet worktree ls|clean` and `rm --kill` can find + tear down the tree by label.
        "worktree": spec.get("worktree_meta")})


# ---------------------------------------------------------------- the launch command
def _link_floor_claudemd(abs_cwd):
    """Symlink the fleet floor CLAUDE.md into a freshly-created ad-hoc cwd so the agent inherits the
    lightweight orientation (run /cmux-fleet:ground, identity). Named roles get
    this symlink via their role setup; ad-hoc cwds are created at launch, so the launcher adds it.
    RELATIVE symlink (matches the role-cwd convention `<role>/CLAUDE.md -> ../_floor/CLAUDE.md`), and
    os.path.relpath keeps it correct at the deeper ad-hoc/<name>/ level. Skips if a CLAUDE.md already
    exists (a role with its own identity file is never clobbered)."""
    floor = FLOOR
    dst = os.path.join(abs_cwd, "CLAUDE.md")
    if not floor or not os.path.exists(floor) or os.path.lexists(dst):
        return
    try:
        os.symlink(os.path.relpath(floor, abs_cwd), dst)
    except OSError as e:
        print(f"[fleet] warn: could not link floor CLAUDE.md into {abs_cwd}: {e}")


def render_send_cmd(bin_name, args, env, abs_cwd, raw_env=None):
    parts = [f"cd {shlex.quote(abs_cwd)} &&"]
    for k, v in env.items():
        parts.append(f"{k}={shlex.quote(str(v))}")
    # raw_env values are emitted VERBATIM (NOT shlex-quoted): the caller guarantees shell-safety. This is
    # how a secret is injected as a spawn-time `$(cat 'path')` so the token itself never appears in the
    # rendered/printed command (the providers feature; only the path shows). Empty -> byte-identical output.
    for k, v in (raw_env or {}).items():
        parts.append(f"{k}={v}")
    parts.append(bin_name)
    # shlex.quote every arg: it's a no-op for safe tokens (flags, paths) but is REQUIRED for inline
    # JSON values like --settings '{"enabledPlugins":...}' — compact JSON has no spaces yet is full of
    # shell metacharacters ({ } " ), and the old space-only guard let the shell mangle it (brace
    # expansion / quote stripping) -> claude got malformed args and never bound a session.
    parts += [shlex.quote(a) for a in args]
    return " ".join(parts)


# markers that an agent TUI has taken over the surface (booting or up) — used to STOP re-kicking Enter
# into a launch that already started (so a slow-booting agent is never spammed with stray keystrokes).
_TUI_MARKERS = ("Context Remaining", "bypass permissions", "esc to interrupt",
                "auto-accept edits", "? for shortcuts", "Welcome to Claude")


def _agent_surfaced(surf):
    """True once an agent TUI is visible on the surface (booting or running). While False, the surface
    is still at the shell — an injected command that hasn't started, i.e. the enter-race symptom."""
    pane = cmuxq("capture-pane", "--surface", surf) or ""
    return any(m in pane for m in _TUI_MARKERS)


def _resume_menu_visible(surf):
    """True if claude's resume-summary menu (`1. Resume from summary` / `2. Resume full session as-is`,
    see _dismiss_resume_summary_prompt) is on-screen. Distinct from _agent_surfaced: none of _TUI_MARKERS
    match this screen, so the old blind-kick loop mistook it for 'still at the shell' and spammed a bare
    Enter into it -- which lands on the menu's cursor-default, LOSSY 'Resume from summary' option. The
    menu blocks the session bind, so the caller must gate/dismiss it (_resume_and_gate), never re-kick
    Enter into it."""
    pane = cmuxq("capture-pane", "--surface", surf) or ""
    return "Resume from summary" in pane


def _send_launch_and_confirm(ws, surf, send_cmd, lazy, timeout):
    """Inject the launch command + Enter, then VERIFY it actually started and RE-KICK Enter if the
    terminating newline lost the paste-settle race (the injected command sits unexecuted at the shell —
    the intermittent 'dead launch': surface exists, no agent). The success signal is the strongest
    readback available — a bound session for claude; for a lazy tool (binds on its first turn) an agent
    TUI appearing. Re-kicks are bounded and suppressed once a TUI is on-screen, so a slow boot is never
    spammed. Returns the bound sid, or '' (normal for a lazy tool, OR for a claude `--resume` launch that
    surfaced its resume-summary menu — the caller must gate/dismiss that separately, see cmd_launch).
    Retries the ENTER, never the paste."""
    cmuxq("send", "--workspace", ws, "--surface", surf, send_cmd + "\n")
    end = time.time() + timeout
    kicks, max_kicks = 0, 5
    while time.time() < end:
        sid = poll_session(surf, timeout=1)
        if sid:
            return sid                                       # claude bound -> definitively started
        if _resume_menu_visible(surf):
            return ""                # resume-summary menu is up -- caller must gate/dismiss it, not us
        surfaced = _agent_surfaced(surf)
        if lazy and surfaced:
            return ""                                        # lazy tool is up; it binds on its 1st turn
        if not surfaced and kicks < max_kicks:
            # still at the shell -> the Enter didn't land; re-kick it, then let the paste settle.
            cmuxq("send-key", "--surface", surf, "enter")
            kicks += 1
            time.sleep(2)
        else:
            time.sleep(1)
    return ""


def _bind_launched_session(ws, surf, send_cmd, tool, label, abs_cwd, caller, lazy, timeout):
    """The resume-aware bind step for `cmd_launch`. Confirms/re-kicks the enter-race as before
    (_send_launch_and_confirm), then, when the caller passthrough carries a claude `--resume <id>`, gates
    the bind on the SAME dismiss sequence `cmd_revive` uses (_resume_and_gate -> picks 'full session
    as-is', never the lossy cursor-default 'resume from summary') instead of trusting a blind re-kick to
    land correctly on the menu. Aborts via sys.exit on a resume-gate timeout, same as cmd_revive: NOT
    binding/registering behind an undismissed menu, surface left alone (nothing torn down -- it may still
    be salvageable). Finally, if the direct poll still came up empty, reconciles against the hook store by
    AGENT_LABEL/cwd (_discover_surface_for) instead of trusting the pre-bind surface uuid unconditionally
    -- claude occasionally binds its session to a DIFFERENT surface than the one launched into (its
    workspace is re-resolved too, so a swapped surface never leaves a mismatched (surface, workspace)
    pair in the registry). Returns (ws, surf, sid); sid is '' if unresolved (the caller decides whether
    that's fatal, e.g. lazy tools expect it)."""
    sid = _send_launch_and_confirm(ws, surf, send_cmd, lazy, timeout)
    resume_flag = _flag_val(caller, "--resume") if tool == "claude" else None
    if not sid and resume_flag not in (None, False):
        resume_sid = resume_flag if isinstance(resume_flag, str) else ""
        if not _resume_and_gate(surf, send_cmd, tool, resume_sid, lambda m: print(f"[fleet] {m}")):
            sys.exit(f"[fleet] ABORT: resume-summary menu never resolved for {label} (surface still "
                     f"booting or wedged at the menu); NOT registering. Re-run the launch once it "
                     f"settles. Inspect: cmux capture-pane --surface {surf}")
        sid = poll_session(surf)
    if not sid and not lazy:
        real_surf, _ = _discover_surface_for(label, abs_cwd)
        if real_surf and real_surf.upper() != surf.upper():
            real_sid = poll_session(real_surf, timeout=5)
            if real_sid:
                print(f"[fleet] note: session bound to surface {real_surf}, not the launched {surf} "
                      f"-- reconciled via AGENT_LABEL/cwd match in the hook store")
                ws, surf, sid = (ws_uuid_for_surface(real_surf) or ws), real_surf, real_sid
    return ws, surf, sid


def cmd_launch(argv):
    # split launcher args | verbatim tool passthrough on the first standalone `--`
    caller = []
    if "--" in argv:
        i = argv.index("--")
        argv, caller = argv[:i], argv[i + 1:]

    ap = argparse.ArgumentParser(prog="fleet launch", add_help=True)
    ap.add_argument("role", nargs="?", help="roster role name (omit with --adhoc)")
    ap.add_argument("--adhoc", metavar="NAME", help="off-roster dynamic agent; cwd=workers/<NAME>")
    ap.add_argument("--tool", help="override the resolved tool (claude|codex|...)")
    ap.add_argument("--parent", default=os.environ.get("CMUX_SURFACE_ID", ""),
                    help="conductor surfaceId (default $CMUX_SURFACE_ID)")
    ap.add_argument("--label", help="override the display label / registry label")
    ap.add_argument("--place", help="override placement (tab|pane|workspace)")
    ap.add_argument("--group", help="workspace group for --place workspace (name or workspace_group:<ref>); "
                                    "needed for an --adhoc agent, which has no toml group")
    ap.add_argument("--direction", default="down", help="split direction for --place pane")
    ap.add_argument("--cwd", help="override the launch cwd (absolute)")
    ap.add_argument("--plugins", help="comma-separated plugin names to UNION onto the composed loadout "
                                      "(role or floor) for THIS launch — the launch analog of recycle's "
                                      "--add-plugin; works with or without --adhoc (legacy: linked-only)")
    ap.add_argument("--use", action="append", default=[], metavar="NAME",
                    help="index-aware plugin to UNION onto the composed loadout for THIS launch "
                         "(repeatable or comma-sep). Routes through plugins.toml, so a `linked` name adds "
                         "a --plugin-dir and an `enabled` name adds an enabledPlugins entry — reaching "
                         "BOTH plugin types (unlike --plugins). Unindexed names fall back to today's "
                         "linked/marketplace/abs-path behavior")
    ap.add_argument("--effort", default="", metavar="LEVEL",
                    help="session-preference override (low|medium|high|xhigh|max); layers over the loadout")
    ap.add_argument("--model", default="", metavar="MODEL", help="session-preference override; layers over the loadout")
    ap.add_argument("--worktree", nargs="?", const=True, default=None, metavar="BRANCH",
                    help="isolate this agent in a git worktree off its repo cwd (overrides the roster; "
                         "optional branch name, else fleet/<label>)")
    ap.add_argument("--no-worktree", action="store_true",
                    help="force-disable worktree even if the role sets worktree=true")
    ap.add_argument("--worktree-base", help="base ref for a NEW worktree branch (default: repo default branch)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an already-LIVE label's registry row (only when you KNOW its old "
                         "surface is already dead by other means)")
    ap.add_argument("--provider", metavar="NAME", help="select an inference provider from "
                    "[providers.<tool>] for THIS launch (accepts NAME or tool:NAME). A subscription "
                    "token is injected per-launch so session logs stay in the tool's DEFAULT dir; no "
                    "config-dir swap. Omit to use the tool's configured default.")
    ap.add_argument("--dry-run", action="store_true", help="resolve + print, do NOT spawn")
    a = ap.parse_args(argv)
    if not a.role and not a.adhoc:
        ap.error("need a <role> or --adhoc <name>")
    # first-class session-preference overrides funnel into the caller-token layer (highest precedence),
    # so `fleet launch role --effort max` works without a `-- --effort max` passthrough.
    if a.effort:
        caller += ["--effort", a.effort]
    if a.model:
        caller += ["--model", a.model]

    cfg = load_config()
    spec = resolve(cfg, a.role, a.tool, a.adhoc)
    if a.place:
        spec["place"] = a.place
    if a.group:
        spec["group"] = a.group
    if a.label:
        spec["label"] = a.label
    if a.plugins:
        # UNION onto the composed loadout regardless of role/ad-hoc. Was gated behind `a.adhoc`, which
        # SILENTLY dropped `--plugins` on a ROSTER launch (the contract is "pass any valid flag at
        # launch/recycle and it takes"; recycle's --add-plugin already unions unconditionally, so this
        # aligns launch with it). A role that wants a plugin BY DEFAULT still belongs in the toml.
        spec["plugins"] = _dedup(spec["plugins"] + [p.strip() for p in a.plugins.split(",") if p.strip()])
    if a.use:
        # UNION index-aware names onto the resolved spec's `use` BEFORE adapter_compile, so they route
        # through the index EXACTLY like a role's own `use` — a `linked` name composes an extra
        # --plugin-dir, an `enabled` name an extra enabledPlugins entry (closing the gap where an external
        # enabled plugin had no launch add-surface). Coexists with legacy --plugins (linked-only).
        spec["use"] = _dedup(spec["use"] + _flatten_csv(a.use))
    # one conductor = one group: a place=workspace conductor with no explicit group anchors its OWN group
    # (named for its label); a place=workspace child with no explicit group joins its parent's group.
    if spec["place"] == "workspace" and not spec["group"]:
        if spec["kind"] == "conductor":
            spec["group"] = spec["label"]
        elif a.parent:
            from . import state as fs
            pe = fs.entry_for_surface(a.parent)
            if pe and pe.get("group"):
                spec["group"] = pe["group"]
    spec["abs_cwd"] = a.cwd or (spec["cwd"] if os.path.isabs(spec["cwd"]) else os.path.join(ROOT, spec["cwd"]))

    # --- worktree (config-gated, default-off) ---------------------------------------------------
    # Resolve whether this launch is worktree-isolated, then swap abs_cwd to the worktree path BEFORE
    # the cwd is baked into the send command, the surface, or the registry. The fleet OWNS the tree:
    # we strip any Claude `-w` passthrough so the agent never becomes a second owner.
    wt_on = spec["worktree"]
    wt_branch = None
    if a.no_worktree:
        wt_on = False
    elif a.worktree is not None:
        wt_on = True
        if isinstance(a.worktree, str):
            wt_branch = a.worktree
    if a.worktree_base:
        spec["worktree_base"] = a.worktree_base
    spec["worktree_active"] = wt_on
    if wt_on:
        from . import worktree as wt
        repo = wt.repo_root(spec["abs_cwd"])
        if not repo:
            sys.exit(f"[fleet] ABORT: --worktree set but cwd is not a git repo: {spec['abs_cwd']}")
        branch = wt_branch or f"{spec['worktree_branch_prefix']}{spec['label']}"
        wt_dir = spec["worktree_dir"] or ".worktrees"
        path = wt.worktree_path(repo, wt_dir, spec["label"])
        if not a.dry_run:                                    # dry-run never touches git
            wt.ensure_gitignored(repo, wt_dir)
            try:
                wt.ensure_worktree(repo, path, branch, spec.get("worktree_base") or "")
            except wt.WorktreeError as e:
                sys.exit(f"[fleet] ABORT: could not prepare worktree: {e}")
        spec["abs_cwd"] = path
        spec["worktree_meta"] = {"repo": repo, "path": path, "branch": branch}
        # one-owner guardrail: drop Claude's own worktree flags from the passthrough so the agent
        # CLI never creates a nested/competing worktree. (Subagent isolation:worktree is never enabled
        # by the fleet, so there is nothing to strip there.)
        caller, stripped = wt.strip_owner_flags(caller)
        if stripped:
            print("[fleet] note: stripped Claude -w/--worktree from passthrough (the fleet owns this worktree)")
        print(f"[fleet] worktree: {path} on branch {branch}")

    bin_name, args, env = adapter_compile(spec["tool"], spec, caller)
    # --- provider selection (the optional providers feature) -----------------------------------
    # Resolve the inference provider for THIS launch and merge its auth into the spawn env. A claude
    # subscription token is injected via raw_env ($(cat path) at spawn) so the secret is never printed;
    # the config dir is untouched, so logs stay in the tool's default place. When [providers] is
    # unconfigured, default_provider() returns "" and this whole block is inert (attribution stays empty).
    from . import providers as pv
    raw_env = {}
    if a.provider:
        pname = a.provider
        if ":" in pname:
            ptool, pname = pname.split(":", 1)
            if ptool != spec["tool"]:
                sys.exit(f"[fleet] --provider tool '{ptool}' != this launch's tool '{spec['tool']}'")
        try:
            pr = pv.resolve_launch(spec["tool"], pname)
        except pv.ProviderError as e:
            sys.exit(f"[fleet] --provider: {e}")
        env.update(pr["env"])
        raw_env.update(pr["raw_env"])
        spec["provider"] = pr["label"]
        print(f"[fleet] provider: {pr['label']}" + (f"  ({pr['note']})" if pr.get("note") else ""))
        if pr.get("provisional"):
            print(f"[fleet] WARN: {pr['label']} account selection is PROVISIONAL (codex mechanism "
                  f"verdict pending; not yet final)")
    else:
        dflt = pv.default_provider(spec["tool"])         # attribute default-account agents too
        if dflt:
            spec["provider"] = f"{spec['tool']}:{dflt}"
    send_cmd = render_send_cmd(bin_name, args, env, spec["abs_cwd"], raw_env)

    print(f"[fleet] tool={spec['tool']} role/label={spec['label']} kind={spec['kind']} place={spec['place']}"
          + (f" group={spec['group']}" if spec['place'] == 'workspace' else ""))
    print(f"[fleet] cwd={spec['abs_cwd']}")
    print(f"[fleet] launch: {send_cmd}")
    _eff = _flag_val(caller, "--effort"); _mdl = _flag_val(caller, "--model")
    provline, provwarn = _session_pref_provenance(
        spec.get("role"), spec["tool"], send_cmd,
        _eff if isinstance(_eff, str) else "", _mdl if isinstance(_mdl, str) else "")
    if provline:
        print(provline)                                          # effort/model + provenance (source)
    if provwarn:
        print(provwarn)                                          # no-pin warning (floor-inherited effort)
    if a.dry_run:
        print("[fleet] dry-run (omit --dry-run to spawn)")
        return 0
    if not a.parent:
        sys.exit("[fleet] ABORT: no --parent and no $CMUX_SURFACE_ID")

    # live-label guard (registry/surface 1:1 invariant, same family as the rm flip): register() is a
    # bare live_put overwrite, so launching into a label whose row still points at a live surface would
    # silently orphan that surface with NO trail at all (not even a "removed" event). Refuse unless the
    # old row is clearly STALE (dead lifecycle + a recorded session -- the same predicate `fleet ls`
    # flags); a pending/unverifiable row refuses too (fail closed). --force is the operator override
    # for "I KNOW the old surface is already dead by other means"; same spirit as cmd_register's
    # already-live-under-a-different-surface refusal.
    from . import state as fs
    prior = fs.live_get(spec["label"])
    if prior and prior.get("surface"):
        prior_surf = prior["surface"]
        # stale = the prior row's surface holds NO genuinely-live agent (lifecycle terminal, OR frozen
        # non-terminal on a DEAD pid -- the SessionEnd-less brick, 2026-07-06) AND it once bound a session.
        # Via surface_has_live_agent a dead-pid ghost now reads stale -> we overwrite the row (no bogus
        # "already LIVE" refusal); a genuinely-live surface (live pid) still refuses (fail-closed: the
        # orphan-a-live-surface guard this exists for). A pending row (no session) refuses as before.
        stale = not fs.surface_has_live_agent(prior_surf) and bool(prior.get("session"))
        if stale:
            print(f"[fleet] note: label '{spec['label']}' had a STALE registry row (surface "
                  f"{prior_surf[:8]} gone); overwriting it")
        elif a.force:
            print(f"[fleet] WARN: --force overwriting live label '{spec['label']}' -- if surface "
                  f"{prior_surf[:8]} is still alive it is now fully untracked")
        else:
            sys.exit(f"[fleet] launch: label '{spec['label']}' is already LIVE under surface "
                     f"{prior_surf}; refusing to overwrite its registry row (that would silently "
                     f"orphan the old surface, with no trace). `fleet rm {spec['label']}` it first, "
                     f"or re-run with --force if you KNOW the old surface is already dead.")

    os.makedirs(spec["abs_cwd"], exist_ok=True)
    if a.adhoc:                                          # ad-hoc cwds are created fresh at launch ->
        _link_floor_claudemd(spec["abs_cwd"])            # symlink the floor CLAUDE.md so they inherit it
    ws, surf = create_surface(spec, a.parent, a.direction)
    if not ws or not surf:
        sys.exit(1)
    print(f"[fleet] target ws={ws} surface={surf}")
    # claude binds a session at BOOT; codex (and the other cmux agents) register LAZILY on their first
    # turn. So poll briefly but DON'T fail if there's no session yet -> register the surface now and let
    # the session BACKFILL on the child's first turn (the router does this when it sees the first Stop).
    # _bind_launched_session injects the command, RE-KICKS the terminating Enter if it lost the
    # paste-settle race (the injected cmd otherwise sits unexecuted at the shell -> no agent ever starts),
    # and gates a `--resume <id>` passthrough on the real resume-menu dismiss instead of a blind re-kick.
    lazy = spec["tool"] != "claude"
    print(f"[fleet] waiting for cmux to bind a session to {surf} ...")
    ws, surf, sid = _bind_launched_session(ws, surf, send_cmd, spec["tool"], spec["label"], spec["abs_cwd"],
                                           caller, lazy, timeout=8 if lazy else 60)
    if not sid and not lazy:
        sys.exit(f"[fleet] timed out waiting for session binding; the injected command may not have "
                 f"started. Inspect the surface: cmux capture-pane --surface {surf}")
    register(surf, spec, a.parent, sid or "", ws)
    log_launch(spec, a.parent, surf, sid or "", send_cmd)
    # post-launch placement reconciliation: confirm the surface actually came up IN the worktree (a
    # collapsed/adopted workspace would land the agent in the wrong cwd). Fail loud with the cleanup +
    # rerun commands; never auto-tear-down (the wrong surface might be someone else's).
    if spec.get("worktree_active") and sid:
        actual = _poll_surface_cwd(surf, spec["abs_cwd"], timeout=10)
        if not actual or os.path.realpath(actual) != os.path.realpath(spec["abs_cwd"]):
            print(f"\n[fleet] !!! PLACEMENT MISMATCH for {spec['label']}")
            print(f"[fleet]   intended worktree cwd : {spec['abs_cwd']}")
            print(f"[fleet]   surface reports cwd    : {actual or '(none yet)'}")
            wm = spec.get("worktree_meta") or {}
            print(f"[fleet]   the launch likely collapsed into an existing surface. Clean up + retry:")
            print(f"[fleet]     fleet rm {spec['label']} --kill   # drops the agent, closes the surface, AND tears down its (clean) worktree")
            print(f"[fleet]       (if the tree shows changes, rm --kill refuses it; reclaim manually: "
                  f"git -C {wm.get('repo', '<repo>')} worktree remove {wm.get('path', spec['abs_cwd'])})")
            print(f"[fleet]     fleet launch {a.role or ('--adhoc ' + a.adhoc)} ...")
            return 2
    if sid:
        print(f"[fleet] DONE: {spec['label']} = surface {surf} (session {sid}, place {spec['place']}, tool {spec['tool']})")
    else:
        print(f"[fleet] DONE: {spec['label']} = surface {surf} (tool {spec['tool']}, place {spec['place']}); "
              f"session backfills on first turn ({spec['tool']} registers lazily) — drive it to bind.")
    return 0


def _flag_val(tokens, name):
    """Value of `--name V` (or `--name=V`) in a token list; True if present as a bare flag; else None."""
    for i, t in enumerate(tokens):
        if t == name:
            return tokens[i + 1] if i + 1 < len(tokens) and not tokens[i + 1].startswith("-") else True
        if t.startswith(name + "="):
            return t.split("=", 1)[1]
    return None


def compute_effective(spec, cwd):
    """The EFFECTIVE settings a session launches with (our flags applied over the base), plus a base
    snapshot for provenance. Key knobs only; precedence: our flags > settings files > env."""
    user = _settings_summary(_read_json(os.path.expanduser("~/.claude/settings.json"))) or {}
    f = spec["flags"]
    perm = ("bypassPermissions" if "--dangerously-skip-permissions" in f
            else _flag_val(f, "--permission-mode"))
    add_dirs = [f[i + 1] for i, t in enumerate(f) if t == "--add-dir" and i + 1 < len(f)]
    effective = {
        "model": _flag_val(f, "--model") or user.get("model") or os.environ.get("ANTHROPIC_MODEL"),
        "effort": _flag_val(f, "--effort") or user.get("effortLevel") or os.environ.get("CLAUDE_CODE_EFFORT_LEVEL"),
        "permissionMode": perm or user.get("defaultMode"),
        "plugins": _dedup((user.get("enabledPlugins") or []) + spec["plugins"]),
        "addDirs": (user.get("additionalDirectories") or []) + add_dirs,
        "tool": spec["tool"],
    }
    base = {"settings_files": [t for t, p in
                              (("user", os.path.expanduser("~/.claude/settings.json")),
                               ("project", os.path.join(cwd, ".claude/settings.json")),
                               ("local", os.path.join(cwd, ".claude/settings.local.json")))
                              if os.path.exists(p)],
            "user_settings": user,
            "env_only": {k: v for k, v in os.environ.items()
                         if k.startswith("CLAUDE_CODE_") or k.startswith("ANTHROPIC_")}}
    return effective, base


def log_launch(spec, parent, surf, session, send_cmd):
    """Append a `launched` event to the ledger (log.jsonl). Captures the EFFECTIVE settings the session
    launched with (resolved end-state, since settings drift) + a base snapshot for provenance + what
    fleet composed."""
    from . import state as fs
    effective, base = compute_effective(spec, spec["abs_cwd"])
    fs.log_event("launched", label=spec["label"], role=spec["role"], tool=spec["tool"],
                 kind=spec["kind"], place=spec["place"], cwd=spec["abs_cwd"], parent=parent,
                 surface=surf, session=session, effective=effective, base=base,
                 fleet={"plugins": spec["plugins"], "flags": spec["flags"],
                        "settings": spec["settings"], "env": list(spec["env"].keys())}, cmd=send_cmd)


# ---------------------------------------------------------------- effective config view
def _read_json(p):
    try:
        return json.load(open(p))
    except Exception:
        return None


def _settings_summary(s):
    """Pull the launch-relevant keys out of a settings.json blob."""
    if not s:
        return None
    perms = s.get("permissions") or {}
    hooks = s.get("hooks") or {}
    return {
        "model": s.get("model"),
        "effortLevel": s.get("effortLevel"),
        "defaultMode": perms.get("defaultMode"),
        "permissions": f"{len(perms.get('allow', []))} allow / {len(perms.get('deny', []))} deny / {len(perms.get('ask', []))} ask",
        "additionalDirectories": perms.get("additionalDirectories") or [],
        "env": list((s.get("env") or {}).keys()),
        "hooks": sorted(hooks.keys()),
        "enabledPlugins": list((s.get("enabledPlugins") or {}).keys()),
        "autoCompactEnabled": s.get("autoCompactEnabled"),
        "tui": s.get("tui"),
    }


def cmd_config(argv):
    """Show the EFFECTIVE config for a role: what claude already loads in that cwd (settings files +
    env + CLAUDE.md), then what fleet stacks on top. Claude has no native dump, so we read the stack.
    Precedence (high->low): managed > CLI flags (fleet's) > local > project > user settings > env."""
    ap = argparse.ArgumentParser(prog="fleet config")
    ap.add_argument("role", nargs="?")
    ap.add_argument("--adhoc", metavar="NAME")
    ap.add_argument("--tool")
    ap.add_argument("--cwd", help="inspect against this dir instead of the role's cwd (settings are cwd-dependent)")
    a = ap.parse_args(argv)
    if not a.role and not a.adhoc and not a.cwd:
        ap.error("need a <role>, --adhoc <name>, or --cwd <dir>")

    cfg = load_config()
    spec = resolve(cfg, a.role or "default-worker", a.tool, a.adhoc or (None if a.role else "_inspect"))
    cwd = os.path.abspath(os.path.expanduser(a.cwd)) if a.cwd \
        else (spec["cwd"] if os.path.isabs(spec["cwd"]) else os.path.join(ROOT, spec["cwd"]))
    bin_name, args, env = adapter_compile(spec["tool"], spec, [])

    print(f"=== fleet config: {spec['label']} (tool={spec['tool']}, cwd={cwd}) ===\n")
    print("BASE — what claude loads in this cwd BEFORE fleet (settings stack, high->low):")
    sources = [
        ("managed", "/Library/Application Support/ClaudeCode/managed-settings.json"),
        ("local", os.path.join(cwd, ".claude/settings.local.json")),
        ("project", os.path.join(cwd, ".claude/settings.json")),
        ("user", os.path.expanduser("~/.claude/settings.json")),
    ]
    for tier, path in sources:
        summ = _settings_summary(_read_json(path))
        if summ is None:
            print(f"  [{tier}] {path}  (absent)")
            continue
        print(f"  [{tier}] {path}")
        for k in ("model", "effortLevel", "defaultMode", "permissions", "autoCompactEnabled", "tui"):
            if summ.get(k) is not None:
                print(f"      {k}: {summ[k]}")
        if summ["env"]:
            print(f"      env: {', '.join(summ['env'])}")
        if summ["hooks"]:
            print(f"      hooks: {', '.join(summ['hooks'])}")
        if summ["enabledPlugins"]:
            print(f"      enabledPlugins: {', '.join(summ['enabledPlugins'])}")
        if summ["additionalDirectories"]:
            print(f"      additionalDirectories: {summ['additionalDirectories']}")

    # env-only config currently in this shell (inherited by the launch)
    relevant = {k: v for k, v in os.environ.items()
                if (k.startswith("CLAUDE_CODE_") or k.startswith("ANTHROPIC_") or k in
                    ("MAX_THINKING_TOKENS", "BASH_DEFAULT_TIMEOUT_MS", "API_TIMEOUT_MS"))}
    print("\n  env-only config in this shell (inherited, lowest priority):")
    if relevant:
        for k in sorted(relevant):
            v = relevant[k]
            if any(s in k for s in ("KEY", "TOKEN", "SECRET")):
                v = "***"
            print(f"      {k}={v}")
    else:
        print("      (none set)")

    # CLAUDE.md that applies
    mds = [p for p in (os.path.join(cwd, "CLAUDE.md"), os.path.join(ROOT, "CLAUDE.md"),
                       os.path.expanduser("~/.claude/CLAUDE.md")) if os.path.exists(p)]
    print(f"\n  CLAUDE.md applied: {', '.join(mds) if mds else '(none)'}")

    print("\nFLEET ADDS (CLI flags + env; flags OVERRIDE the settings/env above):")
    print(f"  plugins (--plugin-dir): {', '.join(spec['plugins']) or '(none)'}")
    if spec.get("setting_sources"):
        print(f"  --setting-sources: {spec['setting_sources']}")
    if spec.get("enable_plugins"):
        print(f"  enabledPlugins (via --settings): {', '.join(spec['enable_plugins'])}")
    if spec.get("use") and spec["tool"] == "claude":       # index-resolved -> the two channels, kept distinct
        u_linked, u_enabled, u_unres = _resolve_use(spec["use"], load_plugin_index())
        print(f"  use (index): {', '.join(spec['use'])}")
        if u_linked:
            print(f"      -> --plugin-dir: {', '.join(u_linked)}")
        if u_enabled:
            print(f"      -> enabledPlugins: {', '.join(u_enabled)}")
        if u_unres:
            print(f"      -> unresolved (skipped): {', '.join(u_unres)}")
    print(f"  flags: {' '.join(spec['flags']) or '(none)'}")
    if spec["settings"]:
        print(f"  --settings: {spec['settings']}")
    print(f"  env: {', '.join(f'{k}={v}' for k, v in env.items())}")

    # call out the highest-leverage override
    user = _settings_summary(_read_json(os.path.expanduser("~/.claude/settings.json"))) or {}
    our_effort = next((spec["flags"][i + 1] for i, t in enumerate(spec["flags"]) if t == "--effort"), None)
    if our_effort and user.get("effortLevel") and our_effort != user["effortLevel"]:
        print(f"\n  NOTE: settings effortLevel={user['effortLevel']} is OVERRIDDEN by fleet --effort {our_effort}.")
    return 0


# ---------------------------------------------------------------- plugins verb (index reconcile + discovery)
def _claude_settings_paths():
    """The claude settings JSONs whose `enabledPlugins` feed reconcile's enabled channel. Overridable
    via $CMUX_FLEET_CLAUDE_SETTINGS (os.pathsep-joined) so tests point at fixtures instead of the host's
    real ~/.claude — the default is the real user-scope settings + the legacy ~/.claude.json."""
    override = os.environ.get("CMUX_FLEET_CLAUDE_SETTINGS", "").strip()
    if override:
        return [p for p in override.split(os.pathsep) if p]
    return [os.path.expanduser("~/.claude/settings.json"), os.path.expanduser("~/.claude.json")]


def _roles_using(name):
    """Scan the roster for every place `name` is referenced, so `plugins show` can answer "which roles
    load this". Returns [(scope, key)] e.g. ("tool.claude","use"), ("role.researcher.claude","plugins").
    Matches the index-native `use` key AND the legacy `plugins`/`enable_plugins` keys (an enable_plugins
    ref is `name@mkt`, so match on the pre-@ name)."""
    cfg = load_config()
    hits = []

    def _check(block, scope):
        if not isinstance(block, dict):
            return
        for key in ("use", "plugins", "enable_plugins"):
            for ref in (block.get(key) or []):
                refname = str(ref).split("@")[0] if key == "enable_plugins" else str(ref)
                if refname == name:
                    hits.append((scope, key))

    for tname, tblock in (cfg.get("tool") or {}).items():
        _check(tblock, f"tool.{tname}")
    for rname, rblock in (cfg.get("role") or {}).items():
        if not isinstance(rblock, dict):
            continue
        _check(rblock, f"role.{rname}")                       # rare, but a role may carry keys directly
        for tname, tblock in rblock.items():
            if isinstance(tblock, dict):
                _check(tblock, f"role.{rname}.{tname}")
    return hits


def _plugin_skills(plugin_dir):
    """Skill names a plugin exposes = the subdirs of <dir>/skills that hold a SKILL.md. os.path.* follow
    symlinks, so the cmux plugin's SYMLINKED skills resolve correctly (design §4 / brief B)."""
    skills_dir = os.path.join(plugin_dir, "skills")
    if not os.path.isdir(skills_dir):
        return []
    out = []
    for s in sorted(os.listdir(skills_dir)):
        if os.path.exists(os.path.join(skills_dir, s, "SKILL.md")):
            out.append(s)
    return out


def _plugin_resolved_dir(name, entry, index):
    """The --plugin-dir path a `type=linked` entry resolves to (or None), via Phase-1 resolution. An
    `enabled` entry has no local dir (it's a global install), so None."""
    if entry.get("type") == "enabled":
        return None
    return _linked_dir(name, entry.get("source", ""), index)


def _cmd_plugins_reconcile(rest):
    from . import plugins as fp
    ap = argparse.ArgumentParser(prog="fleet plugins reconcile",
                                 description="scan marketplaces + claude settings, refresh the index")
    ap.add_argument("--dry-run", action="store_true", help="print the diff and write NOTHING")
    ap.add_argument("--prune", action="store_true", help="also drop index entries no longer backed by a source")
    ap.add_argument("--json", action="store_true", help="emit the diff as JSON")
    a = ap.parse_args(rest)

    index = load_plugin_index()
    try:
        new_text, diff, existing_text = fp.run_reconcile(
            PLUGIN_INDEX, index["marketplaces"], _claude_settings_paths(), prune=a.prune)
    except fp.IndexParseError as e:
        print(f"[fleet] plugins reconcile: existing index {e.path} is malformed ({e.cause}); "
              f"refusing to overwrite it — fix or delete it, then re-run.", file=sys.stderr)
        return 2
    wrote = False
    if not a.dry_run and new_text != existing_text:
        os.makedirs(os.path.dirname(os.path.abspath(PLUGIN_INDEX)), exist_ok=True)
        with open(PLUGIN_INDEX, "w", encoding="utf-8") as f:
            f.write(new_text)
        wrote = True

    counts = diff.counts()
    if a.json:
        print(json.dumps({
            "index": PLUGIN_INDEX,
            "changes": [{"action": act, "name": n, "detail": d} for act, n, d in diff.changes],
            "notes": [{"kind": k, "name": n, "detail": d} for k, n, d in diff.notes],
            "counts": counts, "dry_run": a.dry_run, "wrote": wrote,
        }, indent=2))
        return 0

    sym = {"add": "+", "update": "~", "prune": "-"}
    note_sym = {"preserve": "=", "drift": "!", "collision": "!"}
    print(f"fleet plugins reconcile — index {PLUGIN_INDEX}")
    for action, n, d in diff.changes:
        print(f"  {sym[action]} {action:8} {n:24} {d}")
    for kind, n, d in diff.notes:
        print(f"  {note_sym[kind]} {kind:8} {n:24} {d}")
    if not diff.changes and not diff.notes:
        print("  (index already in sync with sources)")
    print(f"  {counts['add']} added, {counts['update']} updated, {counts['prune']} pruned, "
          f"{counts['preserve']} preserved, {counts['drift']} drift, {counts['collision']} collision")
    if a.dry_run:
        print("  [dry-run] nothing written")
    elif wrote:
        print(f"  wrote {PLUGIN_INDEX}")
    else:
        print("  no changes; file untouched")
    return 0


def _one_line_desc(text, width=60):
    """First line of a (possibly multi-paragraph) description, truncated to `width` chars with an ellipsis
    — keeps `fleet plugins ls` a scannable one-row-per-plugin table when marketplace.json descriptions run
    long. The FULL text stays in `show`/`describe` and `--json`, which never call this."""
    line = (text or "").split("\n", 1)[0].strip()
    return line if len(line) <= width else line[: width - 1].rstrip() + "…"


def _cmd_plugins_ls(rest):
    ap = argparse.ArgumentParser(prog="fleet plugins ls", description="list the plugin index")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(rest)
    plugins = load_plugin_index()["plugins"]
    if a.json:
        print(json.dumps([{
            "name": n, "type": e["type"], "tools": e["tools"], "source": e["source"],
            "origin": e["origin"], "description": e["description"],
        } for n, e in sorted(plugins.items())], indent=2))
        return 0
    if not plugins:
        print(f"(empty index — no plugins.toml at {PLUGIN_INDEX}, or it has no [plugin.*] entries)")
        return 0
    rows = [("NAME", "TYPE", "TOOLS", "SOURCE", "DESCRIPTION")]
    for n, e in sorted(plugins.items()):
        rows.append((n, e["type"], ",".join(e["tools"]) or "-", e["source"] or "-",
                     _one_line_desc(e["description"])))          # first line, ~60c — full text in show/--json
    w = [max(len(r[i]) for r in rows) for i in range(4)]                 # size the first 4 cols; desc runs free
    for i, r in enumerate(rows):
        print(f"  {r[0]:<{w[0]}}  {r[1]:<{w[1]}}  {r[2]:<{w[2]}}  {r[3]:<{w[3]}}  {r[4]}")
        if i == 0:
            print(f"  {'-' * w[0]}  {'-' * w[1]}  {'-' * w[2]}  {'-' * w[3]}  {'-' * 11}")
    return 0


def _cmd_plugins_show(rest):
    ap = argparse.ArgumentParser(prog="fleet plugins show", description="full index entry + resolved path + roles")
    ap.add_argument("name")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(rest)
    index = load_plugin_index()
    entry = index["plugins"].get(a.name)
    if entry is None:
        if a.json:
            print(json.dumps({"name": a.name, "found": False}))
        else:
            print(f"plugin '{a.name}' is not in the index ({PLUGIN_INDEX})")
        return 1
    resolved = _plugin_resolved_dir(a.name, entry, index)
    roles = _roles_using(a.name)
    if a.json:
        print(json.dumps({
            "name": a.name, "found": True, "type": entry["type"], "source": entry["source"],
            "tools": entry["tools"], "origin": entry["origin"], "install": entry["install"],
            "description": entry["description"], "resolved_dir": resolved,
            "tool_overrides": entry["tool_overrides"],
            "used_by": [{"scope": s, "key": k} for s, k in roles],
        }, indent=2))
        return 0
    print(f"=== plugin: {a.name} ===")
    print(f"  type:        {entry['type']}")
    print(f"  source:      {entry['source'] or '-'}")
    print(f"  tools:       {', '.join(entry['tools']) or '-'}")
    print(f"  origin:      {entry['origin'] or '-'}")
    if entry["install"]:
        print(f"  install:     {entry['install']}")
    print(f"  description: {entry['description'] or '-'}")
    print(f"  resolved:    {resolved or ('(enabled: global install, no local dir)' if entry['type'] == 'enabled' else '(unresolved — marketplace unset or dir missing)')}")
    for tool, block in sorted(entry["tool_overrides"].items()):
        print(f"  [{a.name}.{tool}]: {', '.join(f'{k}={v}' for k, v in sorted(block.items()))}")
    if roles:
        print("  used by:")
        for scope, key in roles:
            legacy = "" if key == "use" else f"  (legacy `{key}`)"
            print(f"      {scope}  via {key}{legacy}")
    else:
        print("  used by:     (no role references it)")
    return 0


def _cmd_plugins_describe(rest):
    ap = argparse.ArgumentParser(prog="fleet plugins describe", description="description + skills a plugin exposes")
    ap.add_argument("name")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(rest)
    index = load_plugin_index()
    entry = index["plugins"].get(a.name)
    if entry is None:
        if a.json:
            print(json.dumps({"name": a.name, "found": False}))
        else:
            print(f"plugin '{a.name}' is not in the index ({PLUGIN_INDEX})")
        return 1
    resolved = _plugin_resolved_dir(a.name, entry, index)
    skills = _plugin_skills(resolved) if resolved else []
    if a.json:
        print(json.dumps({
            "name": a.name, "found": True, "description": entry["description"],
            "resolved_dir": resolved, "skills": skills,
        }, indent=2))
        return 0
    print(f"=== {a.name} ===")
    print(f"  {entry['description'] or '(no description)'}")
    if resolved:
        print(f"\n  skills ({len(skills)}): {', '.join(skills) if skills else '(none found under skills/)'}")
    elif entry["type"] == "enabled":
        print("\n  (enabled plugin: global install, skills not locally introspectable)")
    else:
        print("\n  (unresolved: marketplace unset or dir missing — skills not introspectable)")
    return 0


# ---------------------------------------------------------------- plugins add (install-from-URL, design §5b)
# THE SAFETY CONTRACT (Berg-ratified): `add` may auto-clone a NEW plugin and wire the index, but it NEVER
# enables it, NEVER adds it to a role's `use`, NEVER runs its hooks. Concretely, every code path below
# writes ONLY plugins.toml (the index) — never a claude settings file, never fleet.toml — so `add` is
# STRUCTURALLY incapable of producing an enabledPlugins entry or a role-use edit. The one decision it makes
# is linked-vs-enabled (where hook code comes from); an ambiguous call STOPs and reports (never guesses).
def _add_linked(a, name, note, marketplaces):
    """LINKED add: clone the git URL (or copy the local path) into a LOCAL marketplace's path, then
    reconcile the LINKED channel (settings_paths=[]) so the index gains a type=linked entry. Touches NO
    role and NO claude settings. --dry-run prints the plan and clones/writes NOTHING."""
    import shutil
    from . import plugins as fp
    kind = fp.classify_ref(a.ref)
    if kind not in ("git-url", "path"):                      # e.g. `--as linked` on a name@marketplace ref
        print(f"[fleet] plugins add: a LINKED add needs a git URL or a local path to clone/copy "
              f"(ref '{a.ref}' is neither); pass one, or use --as enabled.", file=sys.stderr)
        return 2
    target = a.marketplace or "default"
    mk = marketplaces.get(target)
    if not mk or mk.get("kind") == "global" or not mk.get("path"):
        print(f"[fleet] plugins add: no LOCAL marketplace '{target}' to clone into — declare "
              f"[marketplace.{target}] path=... in {PLUGIN_INDEX}, or set $CMUX_FLEET_MARKETPLACE.",
              file=sys.stderr)
        return 2
    dest = os.path.join(mk["path"], name)
    plan = (f"add '{name}' as LINKED ({note})\n"
            f"  clone/copy:  {a.ref}\n"
            f"          ->   {dest}\n"
            f"  then reconcile (type=linked, tools from manifests). NO role, NO enable, NO hook run.")
    if a.dry_run:
        print(f"[dry-run] {plan}\n[dry-run] nothing cloned or written")
        return 0
    try:                                                     # bail BEFORE cloning if the index won't parse
        fp.assert_index_parseable(PLUGIN_INDEX)
    except fp.IndexParseError as e:
        print(f"[fleet] plugins add: existing index {e.path} is malformed ({e.cause}); refusing to "
              f"overwrite it — fix or delete it, then re-run. (Nothing cloned.)", file=sys.stderr)
        return 2
    if os.path.exists(dest):
        print(f"[fleet] plugins add: destination already exists ({dest}); skipping clone, reconciling.")
    else:
        if kind == "path" and not os.path.isdir(os.path.expanduser(a.ref)):
            print(f"[fleet] plugins add: local path '{a.ref}' is not a directory.", file=sys.stderr)
            return 2
        os.makedirs(mk["path"], exist_ok=True)
        try:
            if kind == "git-url":
                subprocess.run(["git", "clone", "--depth", "1", a.ref, dest],
                               check=True, capture_output=True, text=True)
            else:                                            # local path -> copy the tree (no network)
                shutil.copytree(os.path.expanduser(a.ref), dest)
        except (subprocess.CalledProcessError, OSError) as e:
            detail = (getattr(e, "stderr", "") or "").strip() or str(e)
            print(f"[fleet] plugins add: clone/copy failed: {detail}", file=sys.stderr)
            return 1
    # reconcile the LINKED channel ONLY (settings_paths=[]): the enabled channel is never scanned or
    # written by an `add linked`, and existing enabled index entries are preserved untouched. (The index
    # was already checked parseable above, before the clone, so this cannot raise IndexParseError.)
    new_text, _diff, existing_text = fp.run_reconcile(PLUGIN_INDEX, marketplaces, [], prune=False)
    if new_text != existing_text:
        os.makedirs(os.path.dirname(os.path.abspath(PLUGIN_INDEX)), exist_ok=True)
        with open(PLUGIN_INDEX, "w", encoding="utf-8") as f:
            f.write(new_text)
    print(f"[fleet] plugins add: wired '{name}' as LINKED into the index ({PLUGIN_INDEX}).")
    print(f"  cloned into: {dest}")
    print(f"  NOT enabled — no role loads it and no hook has run. Enable it for ONE agent when ready:")
    print(f"      fleet recycle <agent> --use {name}")
    return 0


def _add_enabled(a, name, note):
    """ENABLED add: record a type=enabled entry as install=global-disabled and PRINT the exact steps to
    finish the global-DISABLED install + the per-agent enable. Writes ONLY plugins.toml — never a claude
    settings file — so it cannot emit an enabledPlugins entry. --dry-run prints the plan and writes NOTHING."""
    from . import plugins as fp
    source = a.marketplace or (a.ref.partition("@")[2] if "@" in a.ref else "")
    if not source:                                           # e.g. `--as enabled` on a bare/URL/path ref
        print(f"[fleet] plugins add: an ENABLED add needs a marketplace — pass name@marketplace or "
              f"--marketplace <name> (ref '{a.ref}' names none).", file=sys.stderr)
        return 2
    plan = (f"add '{name}' as ENABLED ({note})\n"
            f"  index:  [plugin.{name}] type=enabled source={source} install=global-disabled\n"
            f"  the global install is left to you (fleet never auto-runs plugin code). NO role, NO enable.")
    if a.dry_run:
        print(f"[dry-run] {plan}\n[dry-run] nothing installed or written")
        return 0
    try:
        new_text, _existing = fp.add_enabled_index_text(PLUGIN_INDEX, name, source)
    except fp.IndexParseError as e:
        print(f"[fleet] plugins add: existing index {e.path} is malformed ({e.cause}); refusing to "
              f"overwrite it — fix or delete it, then re-run.", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(PLUGIN_INDEX)), exist_ok=True)
    with open(PLUGIN_INDEX, "w", encoding="utf-8") as f:
        f.write(new_text)
    print(f"[fleet] plugins add: wired '{name}' as ENABLED (install=global-disabled) into {PLUGIN_INDEX}.")
    print(f"  NOT installed and NOT enabled — fleet does not auto-run third-party plugin code.")
    print(f"  Finish the global-DISABLED install yourself with claude's plugin CLI, e.g.:")
    print(f"      claude plugin marketplace add <{source}-source>   # if marketplace '{source}' isn't added yet")
    print(f"      claude plugin install {name}@{source}             # install; leave it DISABLED")
    print(f"  Then enable it for ONE agent when ready:")
    print(f"      fleet recycle <agent> --use {name}")
    return 0


def _cmd_plugins_add(rest):
    """`fleet plugins add <ref> [--as linked|enabled] [--marketplace <name>] [--dry-run]` — index a NEW
    plugin at the SAFE default: clone/wire it, but NEVER enable it, add it to a role, or run its hooks."""
    from . import plugins as fp
    ap = argparse.ArgumentParser(prog="fleet plugins add",
                                 description="index a NEW plugin (SAFE default: never enables it)")
    ap.add_argument("ref", help="a git URL, a local path, or a name@marketplace ref")
    ap.add_argument("--as", dest="as_", choices=["linked", "enabled"],
                    help="force the technique (default: inferred from the ref)")
    ap.add_argument("--marketplace", help="target (linked) / source (enabled) marketplace; default 'default'")
    ap.add_argument("--dry-run", action="store_true",
                    help="infer + print the plan; clone/install/write NOTHING")
    a = ap.parse_args(rest)

    index = load_plugin_index()
    name = fp.plugin_name_from_ref(a.ref)

    # 1. already indexed? -> a no-op that points at the enable one-liner (idempotent; never re-clones).
    if name in index["plugins"]:
        cur = index["plugins"][name]
        print(f"plugin '{name}' is already indexed (type={cur['type']}); nothing to do.")
        print(f"  enable it per-agent with:  fleet recycle <agent> --use {name}")
        return 0

    # 2. infer the technique — an ambiguous or invalid call STOPs (never defaults a security-relevant choice).
    technique, reason = fp.infer_technique(a.ref, a.marketplace, a.as_, index["marketplaces"])
    if technique == "ambiguous":
        print(f"[fleet] plugins add: STOP — {reason}.\n"
              f"  Loading a plugin runs its hook code, so linked-vs-enabled is a safety call and fleet will "
              f"not guess.\n  Re-run with an explicit --as linked|enabled (or --marketplace <name>).",
              file=sys.stderr)
        return 2
    if technique == "error":
        print(f"[fleet] plugins add: {reason}.", file=sys.stderr)
        return 2

    if technique == "linked":
        return _add_linked(a, name, reason, index["marketplaces"])
    return _add_enabled(a, name, reason)


def cmd_plugins(argv):
    """`fleet plugins <add|reconcile|ls|show|describe>` — the plugin index's install/reconcile helpers +
    on-demand discovery (design §4/§5b/§6). Discovery is NEVER auto-loaded; a conductor consults it when
    deciding what to dispatch a child with. `add` wires a NEW plugin but NEVER enables/loads it (the safe
    default). None of these verbs enable a plugin, edit a role, or run a plugin's hooks."""
    verbs = {"add": _cmd_plugins_add, "reconcile": _cmd_plugins_reconcile, "ls": _cmd_plugins_ls,
             "show": _cmd_plugins_show, "describe": _cmd_plugins_describe}
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in verbs:
        print("usage: fleet plugins <add|reconcile|ls|show|describe> ...\n"
              "  add <ref> [--as linked|enabled] [--marketplace N] [--dry-run]\n"
              "                                             index a NEW plugin (SAFE: never enables it / adds it to a role / runs a hook)\n"
              "  reconcile [--dry-run] [--prune] [--json]   scan marketplaces + ~/.claude settings; refresh the index\n"
              "  ls [--json]                                table: name · type · tools · source · description\n"
              "  show <name> [--json]                       full entry + resolved --plugin-dir path + roles that use it\n"
              "  describe <name> [--json]                   description + the skills the plugin exposes")
        return 0 if (argv and argv[0] in ("-h", "--help")) else (0 if not argv else 2)
    return verbs[argv[0]](argv[1:])


# ---------------------------------------------------------------- lifecycle verbs (the conductor's job)
def _store():
    from . import state as fs                                  # union of all per-agent hook stores
    return fs.read_hook_store()


def _pid_for_surface(surface):
    for s in (_store().get("sessions") or {}).values():
        if s.get("surfaceId") == surface:
            return s.get("pid")
    return None


def _surface_pids(surface):
    """The set of pids currently ALIVE on `surface` per the hook store — the pre-respawn safety
    snapshot for the recycle verify: any of these still alive AFTER the respawn means the old agent
    survived the kill, so we must NOT relaunch over it (the 'never type into a live TUI' invariant)."""
    from . import state as fs
    return {s.get("pid") for s in (_store().get("sessions") or {}).values()
            if s.get("surfaceId") == surface and fs.pid_alive(s.get("pid"))}


def cmd_ls(argv):
    """Reconcile the live registry against cmux's hook store. Flags STALE = registry says live but the
    surface has no live session (a closed tab / crash never fires an archive transition). Scoped like
    every read: defaults `--scope mine` (you + your direct children); `--scope all` opens the whole
    fleet; `conductors`/`children` filter by kind. When `mine` is just you, a one-line hint points at
    `--scope all` so nobody mistakes their corner for the empty fleet. `--json` emits the reconciled
    rows (live + archived, with the computed status/lifecycle) as machine output."""
    from . import state as fs
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    scope_arg, _ = fs.pop_scope(argv, default=None)
    scope, caller = fs.read_scope(scope_arg, "ls")
    live, arch = fs.live_all(), fs.archive_all()
    if scope != "all":
        live = {l: v for l, v in live.items() if fs.scope_matches(scope, v, l, caller, include_self=True)}
        arch = {l: v for l, v in arch.items() if fs.scope_matches(scope, v, l, caller, include_self=True)}
    # reconcile ONCE -> render as JSON or the text table (identical status/lifecycle either way).
    live_rows = []
    for label, v in sorted(live.items()):
        surf = v.get("surface", "")
        life = fs.lifecycle(surf)
        # STALE if NO genuinely-live agent holds the surface: lifecycle terminal, OR frozen non-terminal
        # on a DEAD pid (the SessionEnd-less brick, root-caused 2026-07-06). Routed through the shared
        # surface_has_live_agent predicate so the pid -- not the lifecycle string -- is the authority: a
        # dead 'running' ghost now reads STALE here (the "ls lies" symptom), consistent with bulk-recycle.
        if not fs.surface_has_live_agent(surf):
            # no live agent on the surface: PENDING = lazily-registered, not bound yet (codex binds
            # on its 1st turn -> drive it); STALE = had a session but the tab/process is gone.
            status = "pending" if not v.get("session") else "STALE"
        else:
            status = v.get("status", "live")
        live_rows.append({"label": label, "role": v.get("role"), "kind": v.get("kind"),
                          "status": status, "lifecycle": life or None, "surface": surf,
                          "muted": bool(v.get("muted"))})
    if as_json:
        arch_rows = [{"label": label, "role": v.get("role"), "kind": v.get("kind"),
                      "last_session": v.get("last_session") or None} for label, v in sorted(arch.items())]
        print(json.dumps({"scope": scope, "live": live_rows, "archived": arch_rows}, indent=2))
        return 0
    scope_tag = "" if scope == "all" else f"{scope}: "
    print(f"LIVE FLEET ({scope_tag}{len(live)}):  {'label':<24}{'role':<16}{'kind':<11}{'status':<8}{'lifecycle':<11}surface")
    for r in live_rows:
        muted = "  MUTED" if r["muted"] else ""
        print(f"  {r['label']:<24}{(r['role'] or '-'):<16}{(r['kind'] or '-'):<11}{r['status']:<8}{(r['lifecycle'] or '-'):<11}{r['surface'][:8]}{muted}")
    if arch:
        print(f"\nARCHIVED ({len(arch)}, revivable):")
        for label, v in sorted(arch.items()):
            print(f"  {label:<24}{v.get('role','-'):<16}{v.get('kind','-'):<11}last_session={(v.get('last_session') or '')[:14]}")
    if scope == "mine" and not any(l != caller for l in live):
        print(fs.only_self_hint("ls"))
    print("\n(STALE = surface gone, `fleet rm`/`revive`.  pending = launched, awaiting first turn to bind.)")
    return 0


def cmd_rm(argv):
    """Remove a label AND its live surface together (registry-active <=> live surface, 1:1, no
    exceptions by default). Default = the archive-equivalent teardown: force-archive a recovery row
    first, SIGINT the process, close the surface -- so a bare `rm` can never again silently abandon a
    still-live surface (the book-keeper zombie incident: a pane left running ~40h, invisible to `fleet
    ls`). A surface that is mid-turn ('running') is REFUSED by default; --force closes it anyway.
    --detach is the explicit opt-in for the OLD soft behavior: drop the registry row ONLY, never touch
    the surface -- for handing a pane to a human to drive directly without killing in-progress work.
    NOTE detach != mute: `fleet mute` is a notification-ROUTING concept (the child stays tracked,
    completions just aren't pushed); a detached label is fully untracked. --kill is accepted as an
    alias for the close+archive default; the one
    thing it still adds is worktree teardown for a worktree-isolated agent (refuse-if-dirty;
    --wip-commit to snapshot; branch always kept) -- `fleet worktree clean <label>` is the dedicated
    verb for that otherwise. --with-group also dissolves the agent's workspace-group: deleting the group by ref
    closes EVERY member surface, so we then SWEEP all live+archive entries in that group out of the
    registry (otherwise they linger as orphaned rows for dead surfaces). Before touching anything,
    --with-group cross-checks the registry's belief about that group's membership against cmux's REAL
    membership (`workspace-group list --json`) and REFUSES (no dissolve, no sweep) on any disagreement --
    a registry `group` field can desync from cmux's actual visual group (root cause of the 2026-07-02
    incident: dissolving a group the target only THOUGHT it belonged to swept 3 unrelated live agents). A
    swept member's worktree dir and branch are left UNMANAGED: their registry rows are gone, so `fleet
    worktree clean` (which discovers from the registry) cannot find them. Reclaim manually with `git
    worktree list` + `git worktree remove <path>` (and `git branch -D fleet/<label>` if you want the
    branch gone). WITHOUT --with-group, only this agent's own workspace goes and remaining members are
    left ungrouped."""
    from . import state as fs; import signal
    kill = "--kill" in argv
    detach = "--detach" in argv
    force = "--force" in argv
    wipc = "--wip-commit" in argv
    with_group = "--with-group" in argv
    args = [a for a in argv if a not in ("--kill", "--detach", "--force", "--wip-commit", "--with-group")]
    if not args:
        sys.exit("usage: fleet rm <label> [--detach] [--force] [--kill] [--wip-commit] [--with-group]")
    label = args[0]
    if detach and kill:
        sys.exit("[fleet] rm: --detach and --kill are contradictory (leave the surface running vs "
                 "tear everything down) -- pick one")
    if detach and with_group:
        sys.exit("[fleet] rm: --detach and --with-group are contradictory (a group dissolve closes "
                 "every member surface) -- pick one")
    e_live = fs.live_get(label)
    e = e_live or fs.archive_get(label)
    if not e:
        sys.exit(f"fleet rm: no such label '{label}'")
    # running-surface guard (ships WITH the default flip -- it's the flip's own footgun): the default
    # now CLOSES the surface, so a mid-turn agent would be killed half-way. A SYNCHRONOUS check +
    # refuse, deliberately NOT recycle's async quiet-gate: an async wait here would race the exact
    # rm-then-relaunch workflow that caused the incident (two surfaces transiently contending for one
    # label). idle/needsInput/unknown proceed as already-safe (_quiet_gate's own vocabulary of quiet).
    surf = (e_live or {}).get("surface", "")
    closing = not detach and bool(surf)
    # refuse only a GENUINELY mid-turn agent: lifecycle 'running' AND a live pid. A frozen 'running' ghost
    # on a DEAD pid (the SessionEnd-less brick, root-caused 2026-07-06) is not mid-turn -- there's no live
    # work to interrupt -- so it must NOT block a plain `rm` (Berg's gap: a dead ghost forced --force). The
    # string==running specificity is kept (idle/needsInput/unknown already proceed as safe per _quiet_gate's
    # vocabulary); surface_has_live_pid just strips the dead-ghost false-positive.
    if closing and not force and fs.lifecycle(surf) == "running" and fs.surface_has_live_pid(surf):
        sys.exit(f"[fleet] rm: '{label}' is mid-turn (lifecycle=running on surface {surf[:8]}). "
                 f"Use --force to close it anyway, or --detach to drop the registry row and leave "
                 f"the surface running.")
    if closing:
        fs.expected_close_put(surf)                     # tombstone BEFORE any close: mark this a DELIBERATE
                                                        # retirement so the router won't mis-read the
                                                        # surface.closed frame as an accidental external
                                                        # close and fire a spurious stale "revive?" alert
    group_note = ""
    if with_group and e.get("group"):
        gname = e["group"]
        gref = _group_ref(gname)
        if gref:
            # registry-believed membership: this label + every other live/archive row claiming the same
            # group NAME. Compare its workspace-uuid set against cmux's REAL membership for the ref
            # BEFORE doing anything destructive -- a mismatch means the registry can't be trusted here.
            members = {}
            for tbl in (fs.live_all(), fs.archive_all()):
                for lbl, v in tbl.items():
                    if lbl != label and v.get("group") == gname:
                        members.setdefault(lbl, v)
            registry_all = {label: e, **members}
            registry_ws = {lbl: v.get("workspace") for lbl, v in registry_all.items()}
            unverifiable = sorted(lbl for lbl, ws in registry_ws.items() if not ws)
            real_ws = _group_member_workspaces(gref)
            if real_ws is None or unverifiable or set(registry_ws.values()) != real_ws:
                real_display = sorted(real_ws) if real_ws is not None else "UNREADABLE (cmux group data unavailable)"
                sys.exit(
                    f"[fleet] ABORT --with-group: refusing to dissolve '{gname}' ({gref}) -- registry and "
                    f"cmux disagree about membership (this is a registry-integrity bug, not a --force case; "
                    f"see Item 2, 2026-07-02 incident).\n"
                    f"[fleet]   registry believes group '{gname}' = {sorted(registry_ws)}"
                    + (f"  (workspace id unknown for: {', '.join(unverifiable)} -- can't verify, treated as a "
                       f"mismatch)" if unverifiable else "") + "\n"
                    f"[fleet]   cmux reports group '{gref}' member workspaces = {real_display}\n"
                    f"[fleet] no dissolve, no sweep happened. Investigate before retrying "
                    f"(`fleet ls`, `cmux workspace-group list --json`).")
            # AGREEMENT confirmed -> observability BEFORE the irreversible act (not just an after-the-fact
            # report): print what's about to die, THEN dissolve. wt_kept only needs `members` (already
            # known), so it's computable up front too.
            wt_kept = sorted([lbl for lbl, v in members.items() if v.get("worktree")]
                             + ([label] if e.get("worktree") and not kill else []))
            group_note = f"\n[fleet] group '{gname}' dissolved ({gref}); closed + cleared {1 + len(members)} member(s)"
            if members:
                group_note += f" (also removed: {', '.join(sorted(members))})"
            if wt_kept:
                group_note += (f"\n[fleet]   worktree dirs/branches left UNMANAGED for {', '.join(wt_kept)} "
                               f"(registry rows gone; reclaim manually: git worktree list; "
                               f"git worktree remove <path>; git branch -D fleet/<label>)")
            print(f"[fleet] about to dissolve group '{gname}' ({gref}); closing {1 + len(members)} "
                  f"member(s): {', '.join(sorted(registry_all))}")
            for _m in members.values():                      # tombstone the OTHER members too (the group
                fs.expected_close_put(_m.get("surface", ""))  # delete closes every member surface; target
                                                             # surf is already tombstoned above via `closing`)
            cmuxq("workspace-group", "delete", gref)         # delete takes a REF -> closes ALL members
            for lbl, v in members.items():
                fs.live_del(lbl); fs.archive_del(lbl)
                fs.log_event("removed", label=lbl, role=v.get("role"), via="group-dissolve")
        else:
            group_note = f"\n[fleet] group '{gname}' not found live; nothing to dissolve"
    archived = False
    if closing:
        # close+archive is now the DEFAULT removal path (was --kill-only): capture the binding + write
        # the archive row BEFORE tearing the surface down, so a removed agent degrades to "recorded but
        # maybe-unresumable" rather than vanishing ("prune freely, agents are recoverable"). An
        # empty/pending last_session (never bound / wedged agent) is still a valid marker -- `fleet
        # revive` just relaunches fresh in that case; refusing to archive would block removing a wedged
        # agent that needs to stay removable.
        b = _resume_binding(surf)
        fs.archive_put(label, _build_archive_entry(e_live, b))
        fs.log_event("archived", label=label, role=e.get("role"), session=e.get("session"),
                     via="kill" if kill else "rm")
        archived = True
        pid = _pid_for_surface(surf)
        if pid:
            try:
                os.kill(pid, signal.SIGINT); time.sleep(0.4); os.kill(pid, signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                pass
        cmuxq("close-surface", "--surface", surf)
    wt_note = ""
    if kill and e.get("worktree"):
        from . import worktree as wt
        m = e["worktree"]
        removed, msg = wt.teardown(m["repo"], m["path"], label, wip_commit_flag=wipc)
        wt_note = f"\n[fleet] worktree: {msg}"
        if not removed:
            # the registry row is deleted (or, since --kill, re-parked in archive) just below, so `fleet
            # worktree clean` can no longer find it; the tree is dirty -> reclaim manually.
            wt_note += (f"\n[fleet]   ({label}'s tree is dirty; reclaim manually: "
                        f"git -C {m['repo']} worktree remove {m['path']} after committing/stashing)")
    fs.live_del(label)
    if not archived:
        fs.archive_del(label)
    fs.log_event("removed", label=label, role=e.get("role"), killed=kill, detached=detach,
                 with_group=with_group)
    if archived:
        tail = f" (closed + archived for recovery: fleet revive {label})"
    elif detach and surf:
        tail = " (DETACHED: registry row dropped, surface left running untracked -- detach != mute)"
    else:
        tail = ""
    print(f"[fleet] removed {label}{tail}{group_note}{wt_note}")
    return 0


def _worktree_entries():
    """(label -> {meta, where}) for every registry entry carrying worktree bookkeeping (live + archive)."""
    from . import state as fs
    out = {}
    for where, table in (("live", fs.live_all()), ("archive", fs.archive_all())):
        for label, v in table.items():
            m = v.get("worktree")
            if m:
                out[label] = {"meta": m, "where": where, "entry": v}
    return out


def cmd_worktree(argv):
    """Manage fleet-owned git worktrees. v0.1 verbs: `ls` (list + dirty/exists state) and
    `clean <label>` (teardown, refuse-if-dirty, keep branch)."""
    from . import worktree as wt
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: fleet worktree <ls | clean <label> [--wip-commit] [--force]>")
        return 0
    verb, rest = argv[0], argv[1:]

    if verb == "ls":
        ents = _worktree_entries()
        if not ents:
            print("no fleet worktrees registered.")
            return 0
        print(f"FLEET WORKTREES ({len(ents)}):  {'label':<24}{'branch':<22}{'state':<10}{'where':<9}path")
        # cache the per-repo registered-worktree set so we can flag ones git no longer knows about
        for label, info in sorted(ents.items()):
            m, where = info["meta"], info["where"]
            path, branch = m.get("path", "?"), m.get("branch", "?")
            if not os.path.exists(path):
                state = "GONE"
            elif not wt.find_worktree(m.get("repo", ""), path):
                state = "untracked"
            elif wt.has_changes(path):
                state = "dirty"
            else:
                state = "clean"
            print(f"  {label:<24}{branch:<22}{state:<10}{where:<9}{path}")
        print("\n(clean = removable; dirty = has changes (clean needs --wip-commit); GONE = dir missing.)")
        return 0

    if verb == "clean":
        ap = argparse.ArgumentParser(prog="fleet worktree clean")
        ap.add_argument("label")
        ap.add_argument("--wip-commit", action="store_true",
                        help="commit dirty changes as a WIP snapshot before removing (branch kept)")
        ap.add_argument("--force", action="store_true", help="force git worktree remove (e.g. locked)")
        a = ap.parse_args(rest)
        ents = _worktree_entries()
        info = ents.get(a.label)
        if not info:
            sys.exit(f"fleet worktree clean: no registered worktree for '{a.label}' (see `fleet worktree ls`)")
        from . import state as fs
        live = fs.live_get(a.label)
        # refuse only if a GENUINELY-live agent holds the surface (non-terminal lifecycle AND a live pid).
        # A dead-pid frozen 'running' ghost (the SessionEnd-less brick, 2026-07-06) must NOT block worktree
        # teardown -- there is no live work to protect. surface_has_live_agent is the shared authority.
        if info["where"] == "live" and live and fs.surface_has_live_agent(live.get("surface", "")):
            sys.exit(f"fleet worktree clean: '{a.label}' is still LIVE. Either `fleet archive {a.label}` "
                     f"then `fleet worktree clean {a.label}`, or `fleet rm {a.label} --kill` (which itself "
                     f"tears the worktree down).")
        m = info["meta"]
        removed, msg = wt.teardown(m["repo"], m["path"], a.label, wip_commit_flag=a.wip_commit, force=a.force)
        print(f"[fleet] {msg}")
        if removed:
            # the tree is gone; null the worktree marker on the entry so `ls` stops listing it (but keep
            # the agent entry itself — clean is about the tree, not the agent's archive record).
            entry = info["entry"]
            entry["worktree"] = None
            (fs.live_put if info["where"] == "live" else fs.archive_put)(a.label, entry)
        return 0 if removed else 1

    sys.exit(f"fleet worktree: unknown verb '{verb}' (use ls | clean)")


def _build_archive_entry(e, b):
    """Compose an archive.json row from a LIVE registry entry `e` + its captured cmux binding `b` (see
    _resume_binding) -- the resumable snapshot `fleet revive` reads. Shared by `fleet archive` and
    `fleet rm --kill` (force-archive-on-kill: --kill was the one removal path that left no recovery
    trace)."""
    arch = {k: e[k] for k in ("role", "kind", "tool", "cwd", "parent", "place",
                              "plugins", "flags", "settings", "group", "worktree") if k in e}
    # last_session = the id `fleet revive` will `--resume`. Prefer cmux's CHECKPOINT (ground truth, read
    # off the binding above) over the registry `session`, which can be a stale bridge id from bind time
    # (the registry-vs-real divergence -> "No conversation found" on revive). Falls back to the registry
    # session when cmux exposes no checkpoint; '' (empty/pending marker) if neither is known -- a killed
    # agent that never bound a session, still archived so it isn't vanished, just maybe-unresumable.
    arch["last_session"] = (b.get("checkpoint_id") or "").strip() or e.get("session") or ""
    arch["archived_at"] = time.time()
    if b.get("command"):                       # revive replays this like recycle (binding-first)
        arch["binding_cmd"] = b["command"]
        if b.get("cwd"):
            arch["binding_cwd"] = b["cwd"]
    # A sparse live entry (hand-bootstrapped conductors carry NO cwd/place) would archive without a cwd,
    # so revive composes abs_cwd = ROOT root and `claude --resume` can't find the session (it lives
    # under the role project dir). Backfill cwd/place/group from the authoritative source: the toml for a
    # roster role, else the captured binding cwd. Sanitize a bad place ("native" etc.) to a real one.
    if _is_roster(e.get("role")):
        try:
            r = resolve(load_config(), e.get("role"), e.get("tool", "claude"), None)
            if not arch.get("cwd"):   arch["cwd"]   = r.get("cwd", "")
            if not arch.get("place"): arch["place"] = r.get("place", "tab")
            if not arch.get("group"): arch["group"] = r.get("group", "")
        except SystemExit:
            pass
    if not arch.get("cwd") and b.get("cwd"):
        arch["cwd"] = b["cwd"]
    if arch.get("place") not in ("tab", "pane", "workspace"):
        arch["place"] = "tab"
    return arch


def cmd_archive(argv):
    """Park a live agent: stop its process (SIGINT x2 = clean TUI exit), close the tab, move it to the
    archive shelf with enough to `claude --resume` it later."""
    from . import state as fs; import signal
    if not argv:
        sys.exit("usage: fleet archive <label>")
    label = argv[0]
    e = fs.live_get(label)
    if not e:
        sys.exit(f"fleet archive: no LIVE label '{label}'")
    surf = e.get("surface", "")
    # capture cmux's GROUND-TRUTH launch binding BEFORE we tear the surface down — this is the same
    # source recycle replays, so revive can recompose the EXACT last command (caller passthrough +
    # post-launch overrides included) instead of the lossy registry-spec snapshot. The binding lives
    # on the surface; once close-surface runs it's gone, so read it first.
    b = _resume_binding(surf) if surf else {}
    if surf:
        fs.expected_close_put(surf)                     # deliberate archive: shield the router's
                                                        # surface.closed handler from a spurious stale alert
    pid = _pid_for_surface(surf)
    if pid:
        try:
            os.kill(pid, signal.SIGINT); time.sleep(0.5); os.kill(pid, signal.SIGINT)
        except (ProcessLookupError, PermissionError):
            pass
    cmuxq("close-surface", "--surface", surf)
    fs.archive_put(label, _build_archive_entry(e, b))
    fs.live_del(label)
    fs.log_event("archived", label=label, role=e.get("role"), session=e.get("session"))
    print(f"[fleet] archived {label} (session {e.get('session')}); revive with: fleet revive {label}")
    return 0


def cmd_revive(argv):
    """Bring a parked agent back into a fresh surface. Default RESUMES its last session (--fresh sheds it
    into a new session, auto-primed from the handover; --session targets an arbitrary prior one). Binding-
    first, like recycle: if archive captured cmux's launch binding, REPLAY it (--resume swapped to the
    parked session, caller `-- <flags>` / --add-plugin re-layered on top). Falls back to the registry-spec
    compose for entries archived before binding-capture existed (or with no binding)."""
    from . import state as fs
    caller = []
    if "--" in argv:
        i = argv.index("--"); argv, caller = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="fleet revive")
    ap.add_argument("label")
    ap.add_argument("--parent", default=os.environ.get("CMUX_SURFACE_ID", ""))
    ap.add_argument("--place")
    ap.add_argument("--fresh", action="store_true",
                    help="revive into a brand-new session (DROP the parked session), auto-primed from the "
                         "latest handover — the shed opt-in; default is RESUME the last session")
    ap.add_argument("--session", default="", metavar="ID",
                    help="resume an ARBITRARY prior session id (default: the archived last_session); "
                         "list with `fleet sessions <label>`")
    ap.add_argument("--force-session", action="store_true",
                    help="skip the --session existence check (id known-good but its projects dir can't be enumerated)")
    ap.add_argument("--add-plugin", action="append", default=[], metavar="NAME",
                    help="union a marketplace plugin into this identity (repeatable)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    e = fs.archive_get(a.label)
    if not e:
        sys.exit(f"fleet revive: no archived label '{a.label}'")
    tool = e.get("tool", "claude")
    if a.fresh and a.session:
        sys.exit("[fleet] revive: --fresh and --session are contradictory (fresh drops the session; --session resumes one)")
    # --fresh drops the session; an explicit --session targets an arbitrary prior one; else last_session.
    # FAIL CLOSED (archived -> no live surface, so the encoded cwd is the only source; if it can't be
    # enumerated we refuse rather than resume a possibly-dead id) unless --force-session.
    if a.session and not a.force_session and not _known_session(e, "", a.session):
        sys.exit(f"[fleet] revive: could not verify session '{a.session}' under {a.label}'s projects dir "
                 f"(bad id, or the dir couldn't be resolved/enumerated). `fleet sessions {a.label}` to list "
                 f"resumable ids; add --force-session to skip this check if you're sure the id is valid.")
    sess = "" if a.fresh else (a.session or e.get("last_session") or "").replace("claude-", "")   # bare uuid
    # Authoritative cwd/place/group: roster toml > archived entry > captured binding. NEVER let cwd fall
    # to "" -> os.path.join(ROOT,"") = ROOT root, which lands the agent off its session (claude --resume
    # then exits "No conversation found"). Self-heals a sparse shelf archived before the backfill existed.
    cwd, place, group = e.get("cwd", ""), e.get("place", ""), e.get("group", "")
    if _is_roster(e.get("role")):
        try:
            r = resolve(load_config(), e.get("role"), tool, None)
            cwd = cwd or r.get("cwd", ""); place = place or r.get("place", "tab"); group = group or r.get("group", "")
        except SystemExit:
            pass
    cwd = cwd or e.get("binding_cwd", "")
    if place not in ("tab", "pane", "workspace"):
        place = "tab"
    spec = {"tool": tool, "role": e.get("role"), "label": a.label, "kind": e.get("kind", "child"),
            "place": a.place or place, "group": group, "cwd": cwd,
            "plugins": _dedup(list(e.get("plugins", [])) + list(a.add_plugin or [])),
            "flags": e.get("flags", []), "env": {}, "settings": e.get("settings", "")}
    spec["abs_cwd"] = spec["cwd"] if os.path.isabs(spec["cwd"]) else os.path.join(ROOT, spec["cwd"])
    if not spec["cwd"]:
        print(f"[fleet] warn: revive {a.label} resolved no cwd (sparse shelf, not a roster role, no "
              f"binding) -> abs_cwd falls back to ROOT root; claude --resume may not find the session")

    binding_argv = _binding_argv(e.get("binding_cmd", ""))
    if _is_roster(e.get("role")):                                 # ROSTER -> re-resolve the toml (truth)
        # RESUME pins the archived (original) cwd so the session is findable; FRESH adopts the toml cwd.
        send_cmd = _compose_from_roster(e.get("role"), tool, a.label, caller, a.add_plugin, sess,
                                        cwd_override=(cwd if sess else ""))
        source = "toml"
    elif binding_argv:                                            # AD-HOC: replay the captured binding
        cwd = e.get("binding_cwd") or spec["cwd"]
        send_cmd = _replay_binding_argv(binding_argv, tool, spec["role"], a.label, cwd,
                                        caller, a.add_plugin, sess)   # _prepend_resume gates per tool
        source = "binding"
    else:                                                         # registry-spec fallback
        bin_name, args, env = adapter_compile(tool, spec, caller)
        args = _prepend_resume(args, tool, sess)                  # claude --resume flag | codex resume subcmd
        if sess and tool not in ("claude", "codex"):
            print(f"[fleet] note: tool '{tool}' has no resume in this flow; fresh launch")
        send_cmd = render_send_cmd(bin_name, args, env, spec["abs_cwd"])
        source = "registry-spec"
    if a.fresh:
        # PERSIST: a fresh revive creates a NEW session under the cwd the send cmd uses (the re-resolved
        # toml cwd for a roster role). register() records spec["abs_cwd"], so pin it to that same cwd —
        # else the registry keeps the OLD cwd and the next default RESUME can't find the new session.
        fresh_cwd = _cwd_of_sendcmd(send_cmd)
        if fresh_cwd:
            spec["cwd"] = fresh_cwd
            spec["abs_cwd"] = fresh_cwd if os.path.isabs(fresh_cwd) else os.path.join(ROOT, fresh_cwd)
    disp = "FRESH (no resume)" if a.fresh else f"resume {sess[:12] or '-'}"
    print(f"[fleet] revive {a.label} (tool={tool}, {disp}, source={source})\n[fleet] launch: {send_cmd}")
    # session-prefs provenance on the live output, for parity with launch/recycle (revive was the one
    # launch-composing verb that never printed it).
    _eff = _flag_val(caller, "--effort"); _mdl = _flag_val(caller, "--model")
    provline, provwarn = _session_pref_provenance(
        e.get("role"), tool, send_cmd,
        _eff if isinstance(_eff, str) else "", _mdl if isinstance(_mdl, str) else "")
    if provline:
        print(provline)                                          # effort/model + provenance (source)
    if provwarn:
        print(provwarn)                                          # no-pin warning (floor-inherited effort)
    if a.dry_run:
        print("[fleet] dry-run"); return 0
    if not a.parent:
        sys.exit("[fleet] ABORT: no --parent and no $CMUX_SURFACE_ID")
    ws, surf = create_surface(spec, a.parent, "down")
    if not ws or not surf:
        sys.exit(1)
    cmuxq("send", "--workspace", ws, "--surface", surf, send_cmd + "\n")
    # full-resume: dismiss the summary menu, and GATE the bind on it clearing. The menu blocks the
    # session bind, so binding behind an undismissed menu would register nothing and leave the agent
    # live-but-UNREGISTERED (invisible to `fleet ls`, still shown archived). On timeout we abort BEFORE
    # archive_del so the label stays parked and re-runnable rather than half-revived.
    if not _resume_and_gate(surf, send_cmd, tool, sess, lambda m: print(f"[fleet] {m}")):
        sys.exit(f"[fleet] ABORT: resume-summary menu never resolved for {a.label} (surface still "
                 f"booting or wedged at the menu); NOT registering. Re-run `fleet revive {a.label}`.")
    sid = poll_session(surf)
    if not sid:
        sys.exit("[fleet] timed out waiting for session binding")
    sid = _resume_binding(surf).get("checkpoint_id", "") or sid   # ground-truth session over a bridge poll id
    register(surf, spec, a.parent, sid, ws)
    fs.archive_del(a.label)
    fs.log_event("revived", label=a.label, role=spec["role"], surface=surf, session=sid, fresh=a.fresh,
                 # ledger parity with log_launch/recycled: ground-truth effort/model off the composed
                 # command; plugins deterministic from the entry + --add-plugin union (already in spec).
                 effective={**_sendcmd_session_prefs(send_cmd), "plugins": spec["plugins"]})
    if a.fresh:                                                   # shed -> prime from the handover (like a fresh recycle)
        ho = _latest_handover(spec["abs_cwd"])
        prime = (f"You were just REVIVED into a FRESH session (same identity: label '{a.label}', "
                 f"role '{spec.get('role')}'). Re-orient from your latest handover"
                 + (f" at {ho}" if ho else " under ./handover/") + ", then continue where it left off.")
        time.sleep(3)                                            # let the fresh TUI settle before input
        cmuxq("send", "--surface", surf, prime)
        cmuxq("send-key", "--surface", surf, "enter")
        print("[fleet]   primed (fresh revive)")
    print(f"[fleet] DONE: revived {a.label} = surface {surf} (session {sid}{', FRESH' if a.fresh else ''})")
    return 0


# ---------------------------------------------------------------- register (manual escape hatch)
def _launchcmd(rec):
    """The launchCommand of a session record as a STRING. cmux normally records a string, but some
    builds store a structured object (dict/list) — passing THAT to re.search raised 'expected string or
    bytes-like object, got dict', the `fleet register` crash. json.dumps a non-string so an AGENT_LABEL=/
    AGENT_ROLE= substring still matches, instead of exploding; a scalar with no command -> ''."""
    v = rec.get("launchCommand") if isinstance(rec, dict) else rec
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v)
        except Exception:
            return ""
    return ""


def _sessions_on_surface(d, surf):
    """All session records in store `d` bound to `surf`, freshest (updatedAt) first."""
    su = (surf or "").upper()
    recs = [s for s in (d.get("sessions") or {}).values() if (s.get("surfaceId") or "").upper() == su]
    recs.sort(key=lambda s: s.get("updatedAt") or 0, reverse=True)
    return recs


def _live_session_for(surf):
    """The single CURRENTLY-LIVE session record for `surf`, or None. Unlike poll_session (which happily
    returns a HISTORICAL sessions[] entry), this REFUSES an ended/stale surface — the register-specific
    live gate (codex P1) so `fleet register` can't bind + archive_del onto a dead surface. Order:
      1. activeSessionsBySurface[surf] — cmux's own 'bound right now' index (resolved to the full
         sessions[] record by sessionId for tool/cwd/role; the index entry itself is the fallback);
      2. else the freshest sessions[] record for the surface whose agentLifecycle is not in
         ('','-','ended') AND whose surface still resolves live in the tree (surface_loc()[0] present)."""
    from . import state as fs
    d = _store()
    active = d.get("activeSessionsBySurface") or {}
    ae = active.get(surf) or active.get((surf or "").upper()) or {}
    if ae.get("sessionId"):
        for s in (d.get("sessions") or {}).values():
            if (s.get("sessionId") or "") == ae["sessionId"]:
                # pid-aware live gate: cmux's active pointer can resolve to a FROZEN dead-pid record
                # (the SessionEnd-less brick, 2026-07-06). A dead pid == not live -> return None so
                # `register` refuses a dead surface instead of binding onto a ghost; a live pid returns
                # the record as before.
                return s if fs.pid_alive(s.get("pid")) else None
        return ae            # bare active pointer, no full record to pid-check: keep cmux's word (rare
                             # degenerate state, NOT the freeze class -- the freeze RETAINS the full
                             # record, caught above; over-restricting here would break a just-bound seat)
    # freshest non-terminal record whose pid is ALSO alive -- the lifecycle string alone can be a frozen
    # dead-pid ghost (the "ls lies" class); the pid is the authority for "genuinely live" here too.
    live_recs = [s for s in _sessions_on_surface(d, surf)
                 if (s.get("agentLifecycle") or "") not in ("", "-", "ended") and fs.pid_alive(s.get("pid"))]
    if live_recs and surface_loc(surf)[0]:
        return live_recs[0]
    return None


def _poll_live_session(surf, timeout=5):
    """Poll _live_session_for until the surface is live (a just-launched agent may take a beat to bind),
    or timeout. Returns the validated live record, or None."""
    end = time.time() + timeout
    while True:
        rec = _live_session_for(surf)
        if rec or time.time() >= end:
            return rec
        time.sleep(1)


def _discover_surface_for(label, abs_cwd):
    """Discover a LIVE agent's surface UUID from the cmux hook store WITHOUT the registry (it's
    unregistered — the whole point). Considers only NON-ENDED surfaces. An exact AGENT_LABEL match in the
    launchCommand (fleet injects AGENT_LABEL=<label> into every launch) wins outright; else fall back to
    cwd, but ONLY if EXACTLY ONE surface matches. Returns (surf, cwd_candidates): surf is '' when nothing
    matched or the cwd match is ambiguous, and cwd_candidates lists the tied surfaces so the caller can
    show them and ask for --surface (codex P2: the old code returned the FIRST cwd match despite the
    docstring promising ambiguity -> '')."""
    import re
    d = _store()
    needle = re.compile(rf"AGENT_LABEL=['\"]?{re.escape(label)}['\"]?(\s|$)")
    by_cwd = []
    for s in (d.get("sessions") or {}).values():
        surf = s.get("surfaceId") or ""
        if not surf or (s.get("agentLifecycle") or "") in ("-", "ended"):
            continue                                           # skip ended/stale surfaces
        if needle.search(_launchcmd(s)):
            return surf, []                                    # exact label match wins outright
        if abs_cwd and s.get("cwd") and os.path.realpath(s["cwd"]) == os.path.realpath(abs_cwd):
            if surf not in by_cwd:
                by_cwd.append(surf)
    if len(by_cwd) == 1:
        return by_cwd[0], []
    return "", by_cwd                                          # 0 or >1 cwd matches -> ambiguous/none


def _tool_for_surface(surf):
    """Which agent tool (claude/codex/...) owns this surface RIGHT NOW, from cmux's PER-tool hook stores
    (~/.cmuxterm/<tool>-hook-sessions.json). On the rare cross-tool surface reuse we do NOT pick
    alphabetically (the old bug: a stale codex record shadowing a live claude one) — prefer the tool
    whose store lists the surface as ACTIVE, else the tool with the freshest non-ended record. '' if no
    store (live-)knows the surface."""
    import glob
    from .config import HOOKSTORE
    su = (surf or "").upper()
    suffix = "-hook-sessions.json"
    best_tool, best_rank = "", None                            # rank = (is_active, freshest_updatedAt)
    for path in sorted(glob.glob(os.path.join(HOOKSTORE, "*" + suffix))):
        try:
            d = json.load(open(path))
        except Exception:
            continue
        active = any((k or "").upper() == su for k in (d.get("activeSessionsBySurface") or {}))
        ts = None
        for s in (d.get("sessions") or {}).values():
            if (s.get("surfaceId") or "").upper() == su and (s.get("agentLifecycle") or "") != "ended":
                t = s.get("updatedAt") or 0
                ts = t if ts is None else max(ts, t)
        if not active and ts is None:
            continue                                           # this store doesn't live-know the surface
        base = os.path.basename(path)
        tool = base[:-len(suffix)] if base.endswith(suffix) else ""
        rank = (1 if active else 0, ts or 0)
        if best_rank is None or rank > best_rank:
            best_tool, best_rank = tool, rank
    return best_tool


def _role_from_launchcmd(rec):
    """AGENT_ROLE parsed from a session record's launchCommand (str-coerced), or ''. Lets an OFF-ROSTER
    agent's role be rebuilt from the live surface's own binding when no archive/registry entry exists."""
    import re
    m = re.search(r"AGENT_ROLE=['\"]?([\w.\-]+)", _launchcmd(rec))
    return m.group(1) if m else ""


def cmd_register(argv):
    """Manually pull a LIVE-but-UNREGISTERED agent into the registry — belt-and-suspenders recovery for
    the SAME failure Fix 2's gate prevents (a resume that bound a session but skipped register, or an
    agent launched outside fleet). PRINCIPLE: derive, don't ask. Given a label + its live surface, we
    DERIVE tool/session/workspace/cwd from the live surface (cmux hook store) and rebuild the launch spec
    from the roster role (toml-authoritative), falling back to the archive/live entry or the surface's own
    AGENT_ROLE/binding for off-roster agents. --session/--parent are optional OVERRIDES, never required.
    Promotes a parked (archived) label to live; idempotent on the SAME surface; refuses to move a label
    that is already live under a DIFFERENT surface."""
    from . import state as fs
    ap = argparse.ArgumentParser(prog="fleet register")
    ap.add_argument("label")
    ap.add_argument("--surface", default="", help="the agent's live surface UUID (primary input); if "
                    "omitted, discovered from the cmux hook store by AGENT_LABEL/cwd")
    ap.add_argument("--parent", default=os.environ.get("CMUX_SURFACE_ID", ""),
                    help="parent LABEL or surface (default $CMUX_SURFACE_ID)")
    ap.add_argument("--session", default="", help="bound session id override (default: derived from cmux)")
    a = ap.parse_args(argv)
    label = a.label
    arch = fs.archive_get(label)
    live = fs.live_get(label)
    src = arch or live or {}

    # surface: explicit --surface is the primary/robust path; else best-effort discovery. Discovery wants
    # a cwd hint, so compute a preliminary cwd (entry > roster resolve) up front.
    prelim_role = src.get("role") or label
    prelim_cwd = src.get("cwd", "")
    if not prelim_cwd and _is_roster(prelim_role):
        try:
            prelim_cwd = resolve(load_config(), prelim_role, src.get("tool", "claude"), None).get("cwd", "")
        except SystemExit:
            pass
    prelim_abs = (prelim_cwd if os.path.isabs(prelim_cwd)
                  else os.path.join(ROOT, prelim_cwd)) if prelim_cwd else ""
    surf = a.surface
    if not surf:
        surf, candidates = _discover_surface_for(label, prelim_abs)
        if not surf:
            hint = (f" Live candidates in this cwd: {', '.join(candidates)}. Pass one as --surface."
                    if candidates else " Pass --surface <uuid> (copy it from cmux).")
            reason = ("ambiguous — several live surfaces share this cwd" if candidates
                      else "no AGENT_LABEL/cwd match in the hook store")
            sys.exit(f"[fleet] register: could not discover a surface for '{label}' ({reason}).{hint}")

    # validate: don't hijack a label already live on a DIFFERENT surface.
    if live and live.get("surface") and live["surface"].upper() != surf.upper():
        sys.exit(f"[fleet] register: '{label}' is already live under a DIFFERENT surface "
                 f"({live['surface']}); refusing to move it to {surf}. `fleet rm {label}` first if that "
                 f"entry is stale, or re-run with the correct --surface.")

    # LIVE GATE (codex P1): require the surface be CURRENTLY live, not just present in cmux's historical
    # sessions[] — otherwise register would bind + archive_del onto a dead surface. Derive session/tool/
    # workspace/cwd/role from this ONE validated record (codex P2: no more stale cross-record mixing).
    rec = _poll_live_session(surf, timeout=5)
    if not rec:
        sys.exit(f"[fleet] register: surface {surf} is not CURRENTLY live — no active/bound session "
                 f"(it may have ended, or the agent hasn't come up yet). Refusing to register a dead "
                 f"surface. If the agent IS up, wait for its first turn to bind, then re-run.")
    session = a.session or fs.bare_uuid(rec.get("sessionId") or "")
    if not session:
        sys.exit(f"[fleet] register: surface {surf} is live but has no session id yet. "
                 f"Wait for its first turn to bind, or pass --session <id>.")

    # derive tool + workspace + cwd from the SAME validated live record (bindings, not flags).
    tool = _tool_for_surface(surf) or src.get("tool", "claude")
    # workspace from cmux GROUND TRUTH (the live tree), NOT the bound record's workspaceId: after a
    # cross-workspace MOVE the hook-store workspaceId FREEZES at the OLD workspace (root cause #3), so
    # trusting `rec` here re-registered moved children straight back into their old shared workspace
    # (observed 2026-07-07: children re-registered to the shared ws, not their new 43/44/45).
    # current_ws_for_surface reads the tree first; rec.workspaceId is only the last-ditch fallback.
    ws = current_ws_for_surface(surf) or rec.get("workspaceId") or ""
    surf_cwd = rec.get("cwd") or _surface_cwd(surf) or ""

    # rebuild the spec: roster role (toml-authoritative, berg's proven recipe) > archive/live entry >
    # the surface's own binding for a truly off-roster agent.
    role = src.get("role") or _role_from_launchcmd(rec) or label
    if _is_roster(role):
        spec = resolve(load_config(), role, tool, None)
        spec["label"] = label
        source = "roster"
    else:
        spec = {"tool": tool, "role": role, "label": label, "kind": src.get("kind", "child"),
                "place": src.get("place", "tab"), "group": src.get("group", ""),
                "cwd": src.get("cwd", "") or surf_cwd, "plugins": list(src.get("plugins", [])),
                "flags": list(src.get("flags", [])), "settings": src.get("settings", "")}
        source = "archive" if arch else ("live" if live else "surface")
    spec["tool"] = tool
    if not spec.get("cwd"):
        spec["cwd"] = surf_cwd                              # last resort: the live surface's own cwd
    spec["abs_cwd"] = spec["cwd"] if os.path.isabs(spec["cwd"]) else os.path.join(ROOT, spec["cwd"])
    spec.setdefault("worktree_meta", src.get("worktree"))

    register(surf, spec, a.parent, session, ws)
    promoted = bool(arch) and bool(fs.archive_del(label))   # archive->live promotion (else it double-lists)
    fs.log_event("registered", label=label, role=role, surface=surf, session=session, source=source)
    print(f"[fleet] registered {label} = surface {surf} (session {session[:12]}, tool={tool}, "
          f"ws={ws or '-'}, parent={fs.label_for_surface(a.parent) or a.parent or '-'}, source={source})"
          + ("; promoted from archive" if promoted else "")
          + ("; updated in place" if live and not arch else ""))
    return 0


def _self_entry(surface_flag=""):
    """(surface, label, entry) for the CALLING conductor: --surface override, else $CMUX_SURFACE_ID,
    resolved to its registry row. entry is {} when the caller isn't a registered member."""
    from . import state as fs
    surf = surface_flag or os.environ.get("CMUX_SURFACE_ID", "")
    label = fs.label_for_surface(surf) or ""
    entry = (fs.live_get(label) or {}) if label else {}
    return surf, label, entry


def cmd_group(argv):
    """Workspace-group management for the one-conductor-one-group layout (children grouped under their
    conductor, matching berg-sandbox). Membership ops here NEVER move a surface, so agents stay live --
    the SAFE lane (contrast `fleet move`, which relocates a live surface). Subcommands:

      init [--name NAME] [--surface UUID]
          Anchor THIS conductor's OWN existing workspace as a named group and RECORD it in the registry,
          so `fleet launch --place workspace` children inherit + join it. Safe sequence: workspace-group
          create --from <my-ws> -> set-anchor <my-ws> -> close any PROVABLY-EMPTY scaffolding anchor cmux
          spawned (the 2026-07-02 footgun). Idempotent: a re-run on an existing group just re-records it.
          NAME defaults to the conductor's label.

      add <label> [--name NAME] [--surface UUID]
          Retrofit an already-live child's workspace INTO the conductor's group via the surface-preserving
          `workspace-group add` -- the child stays live (no move). Records the group on the child's row.
    """
    from . import state as fs
    ap = argparse.ArgumentParser(prog="fleet group", add_help=True)
    ap.add_argument("sub", choices=["init", "add"], help="init (anchor my workspace as a group) | add <label>")
    ap.add_argument("label", nargs="?", help="child label (for `add`)")
    ap.add_argument("--name", default="", help="group name (default: the conductor's label)")
    ap.add_argument("--surface", default="", help="caller surface UUID (default $CMUX_SURFACE_ID)")
    a = ap.parse_args(argv)

    self_surf, self_label, self_entry = _self_entry(a.surface)
    if not self_label or not self_entry:
        sys.exit("[fleet] group: caller is not a registered member (need $CMUX_SURFACE_ID or --surface "
                 "pointing at a live conductor). Run from the conductor, or pass --surface.")

    if a.sub == "init":
        name = a.name or self_entry.get("group") or self_label
        my_ws = current_ws_for_surface(self_surf) or self_entry.get("workspace") or ""
        if not my_ws:
            sys.exit(f"[fleet] group init: cannot resolve {self_label}'s current workspace from cmux.")
        gref = _group_ref(name)
        if gref:                                            # group already exists -> just (re)record it
            fs.live_put(self_label, {**self_entry, "group": name, "place": "workspace"})
            fs.log_event("group-init", label=self_label, via="existing", group=name)
            print(f"[fleet] group '{name}' ({gref}) already exists; recorded on {self_label}. "
                  f"Children launched with --place workspace (no --group) now join it.")
            return 0
        # bootstrap: snapshot workspaces, create anchored on MY ws, set-anchor, close the empty scaffold.
        before = _all_workspace_uuids(cmuxq("tree", "--all", "--id-format", "both"))
        cmuxq("workspace-group", "create", "--name", name, "--from", my_ws)   # ALWAYS explicit --from
        gref = _group_ref(name)
        if not gref:
            sys.exit(f"[fleet] group init: `workspace-group create` did not register a group named "
                     f"'{name}'. No registry change. Inspect: cmux workspace-group list --json")
        cmuxq("workspace-group", "set-anchor", "--group", gref, "--workspace", my_ws)  # my ws IS the anchor
        after = _all_workspace_uuids(cmuxq("tree", "--all", "--id-format", "both"))
        closed = []
        for ws in sorted(after - before - {my_ws}):
            if not _term_surface_in(ws):                    # PROVABLY empty -> the scaffolding anchor
                cmuxq("close-workspace", "--workspace", ws)
                closed.append(ws)
            else:
                print(f"[fleet] group init: note: new NON-empty workspace {ws} appeared; NOT closing "
                      f"(inspect: cmux tree). It is not part of group '{name}'.")
        fs.live_put(self_label, {**self_entry, "group": name, "place": "workspace"})
        fs.log_event("group-init", label=self_label, via="bootstrap", group=name)
        tail = f"; closed {len(closed)} empty scaffold workspace(s)" if closed else ""
        print(f"[fleet] group '{name}' ({gref}) anchored on {self_label}'s workspace{tail}. "
              f"Children launched with --place workspace (no --group) now join it.")
        return 0

    # --- add <label> ---------------------------------------------------------------------------
    if not a.label:
        sys.exit("[fleet] group add: need a child <label>.")
    child = fs.live_get(a.label)
    if not child:
        sys.exit(f"[fleet] group add: '{a.label}' is not a live registry member.")
    name = a.name or self_entry.get("group")
    if not name:
        sys.exit(f"[fleet] group add: {self_label} has no group yet -- run `fleet group init` first "
                 f"(or pass --name).")
    gref = _group_ref(name)
    if not gref:
        sys.exit(f"[fleet] group add: no cmux group named '{name}' (run `fleet group init`). "
                 f"Inspect: cmux workspace-group list --json")
    child_ws = current_ws_for_surface(child.get("surface", "")) or child.get("workspace") or ""
    if not child_ws:
        sys.exit(f"[fleet] group add: cannot resolve {a.label}'s current workspace from cmux.")
    cmuxq("workspace-group", "add", "--group", gref, "--workspace", child_ws)   # SAFE: no surface move
    fs.live_put(a.label, {**child, "group": name, "place": "workspace", "workspace": child_ws})
    fs.log_event("group-add", label=a.label, via="workspace-group-add", group=name)
    print(f"[fleet] added {a.label} (workspace {child_ws[:8]}) to group '{name}' ({gref}); "
          f"surface preserved -- {a.label} stays live.")
    return 0


def cmd_move(argv):
    """Relocate a LIVE child into another workspace ATOMICALLY -- the one safe verb that replaces the
    manual `cmux move-* ` + `fleet register` recovery dance (the 2026-07-07 incident).

      fleet move <label> (--to-workspace <ws> | --own-workspace) [--name TITLE]

    Order (each step de-risks the next):
      1. TOMBSTONE expected_close on the surface FIRST, so the router never mis-reads the move's
         surface.closed frame as a real close and auto-archives the child (belt; the router's own
         move-vs-close tree check is the suspenders -- defense in depth).
      2. MOVE the surface: `move-surface --workspace <ws>` into an existing workspace, or
         `move-tab-to-new-workspace` for --own-workspace (a fresh workspace; if the conductor has a
         group, the new workspace is ADDED to it via the surface-preserving `workspace-group add`).
      3. RECONCILE the registry `workspace`/`place`/`group` from cmux TREE ground truth (never the frozen
         hook-store record -- root cause #3).
      4. VERIFY the agent is still live and report; the surface/session are unchanged throughout.

    NEVER archives and NEVER recycles -- same surface, same session, same identity, new workspace."""
    from . import state as fs
    ap = argparse.ArgumentParser(prog="fleet move", add_help=True)
    ap.add_argument("label", help="the live child to relocate")
    ap.add_argument("--to-workspace", default="", metavar="WS",
                    help="target workspace UUID or workspace:<n> ref (must already exist)")
    ap.add_argument("--own-workspace", action="store_true",
                    help="move the child into a FRESH workspace (joins the conductor's group if one exists)")
    ap.add_argument("--name", default="", help="title for the new workspace (--own-workspace; default: label)")
    a = ap.parse_args(argv)
    if bool(a.to_workspace) == bool(a.own_workspace):
        sys.exit("[fleet] move: pass exactly one of --to-workspace <ws> | --own-workspace.")

    child = fs.live_get(a.label)
    if not child:
        sys.exit(f"[fleet] move: '{a.label}' is not a live registry member (nothing to move).")
    surf = child.get("surface", "")
    if not surf:
        sys.exit(f"[fleet] move: '{a.label}' has no surface recorded; cannot move.")
    cur_ws = current_ws_for_surface(surf)
    if not cur_ws:
        sys.exit(f"[fleet] move: surface {surf[:8]} for '{a.label}' is not in cmux's tree (already closed?). "
                 f"Use `fleet revive {a.label}` to relaunch, not move.")

    # resolve the target BEFORE tombstoning/moving, so a bad target aborts with nothing touched.
    target_uuid = ""
    if a.to_workspace:
        target_uuid = (a.to_workspace if _looks_like_uuid(a.to_workspace)
                       else _ref_to_uuid("workspace", a.to_workspace))
        if not target_uuid:
            sys.exit(f"[fleet] move: could not resolve --to-workspace '{a.to_workspace}' to a workspace "
                     f"(pass a UUID or a workspace:<n> ref). Inspect: cmux list-workspaces")
        if target_uuid.upper() == cur_ws.upper():
            print(f"[fleet] move: {a.label} is already in workspace {cur_ws[:8]}; nothing to do.")
            return 0

    # 1. suppress the spurious archive (belt) BEFORE any surface op emits surface.closed.
    fs.expected_close_put(surf)

    # 2. move the surface (UUID preserved).
    if a.to_workspace:
        cmuxq("move-surface", "--surface", surf, "--workspace", target_uuid, "--focus", "false")
        new_group = child.get("group", "")               # group membership follows the target workspace
    else:
        cmuxq("move-tab-to-new-workspace", "--surface", surf, "--title", a.name or a.label,
              "--focus", "false")
        new_group = ""
        pe = fs.live_get(child.get("parent") or "") or {}
        gname = pe.get("group") or ""
        gref = _group_ref(gname) if gname else ""
        new_ws_early = current_ws_for_surface(surf)
        if gref and new_ws_early:
            cmuxq("workspace-group", "add", "--group", gref, "--workspace", new_ws_early)  # surface-preserving
            new_group = gname

    # 3. reconcile from TREE ground truth (root cause #3: never trust the frozen hook-store workspaceId).
    new_ws = current_ws_for_surface(surf)
    if not new_ws:
        sys.exit(f"[fleet] move: after the move, surface {surf[:8]} could not be located in any workspace "
                 f"-- NOT touching the registry. Inspect: cmux tree --all. (The expected-close tombstone "
                 f"will lapse; if the surface is truly gone, `fleet register`/`revive` to recover.)")
    fs.live_put(a.label, {**child, "workspace": new_ws, "place": "workspace", "group": new_group})
    fs.log_event("moved", label=a.label, role=child.get("role"), session=child.get("session"),
                 via="fleet-move")

    # 4. verify liveness; a post-move hook-store desync is cosmetic (completions self-recover via the
    # registry session), so warn rather than fail.
    live_note = ""
    if not fs.surface_has_live_agent(surf):
        live_note = (f"\n[fleet]   note: cmux's hook store reads {a.label} as not-live post-move (a known "
                     f"binding desync). Completions still route via the registry session; if `fleet ls` "
                     f"shows STALE, `fleet recycle {a.label}` rebinds it.")
    print(f"[fleet] moved {a.label}: surface {surf[:8]} {cur_ws[:8]} -> {new_ws[:8]}"
          + (f" (group '{new_group}')" if new_group else "") + f"; same session, still live.{live_note}")
    return 0


def _looks_like_uuid(s):
    import re
    return bool(re.fullmatch(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
                             s or ""))


def cmd_unstick(argv):
    """Reap a FROZEN, dead-pid hook-store record for a label's surface -- the ghost a SessionEnd-less
    death leaves (SIGKILL / abrupt kill / the SessionEnd store-write race, root-caused 2026-07-06),
    which freezes agentLifecycle non-terminal ('running'/'idle'/'unknown') with a dead or None pid and
    makes `fleet ls` show a false 'live', recycle refuse ('old session still ALIVE'), and the doctor
    trust a dead 'running'. Clears the ghost from cmux's ~/.cmuxterm/*-hook-sessions.json WITHOUT the
    hand-editing that the incident recovery needed. SAFETY: a record whose pid is ALIVE is NEVER touched --
    if the surface's agent is genuinely live, unstick reaps nothing and says so; a surface holding BOTH
    a live record and a dead ghost keeps the live one. --dry-run previews. With the ghost gone, `fleet
    recycle <label>` / `revive` recover cleanly (a fresh SessionStart also self-cleans) -- unstick is the
    belt to recycle's now-pid-aware suspenders, for when you want the record cleared without relaunching."""
    from . import state as fs
    ap = argparse.ArgumentParser(prog="fleet unstick", add_help=True)
    ap.add_argument("label", nargs="?", help="registry label (default: self via $CMUX_SURFACE_ID)")
    ap.add_argument("--surface", default="", help="target surface UUID directly (overrides label lookup)")
    ap.add_argument("--dry-run", action="store_true", help="preview what would be reaped; touch nothing")
    a = ap.parse_args(argv)
    surf, label = a.surface, a.label
    if not surf:
        if not label:
            label = fs.label_for_surface(os.environ.get("CMUX_SURFACE_ID", "")) or ""
        if label:
            surf = (fs.live_get(label) or {}).get("surface", "")
        if not surf:
            surf = os.environ.get("CMUX_SURFACE_ID", "")
    if not surf:
        sys.exit("[fleet] unstick: need a <label> (live in the registry) or --surface <uuid>.")
    res = fs.reap_dead_surface_records(surf, dry_run=a.dry_run)
    tag = f"{label} = " if label else ""
    for lk in res["live_kept"]:
        print(f"[fleet] unstick: {tag}surface {surf[:8]} has a LIVE record (session "
              f"{(lk['sid'] or '')[:12]}, pid {lk['pid']}, {lk['life']}) -- left untouched.")
    if not res["reaped"]:
        print(f"[fleet] unstick: {tag}surface {surf[:8]} -- no frozen dead-pid records to reap "
              f"(nothing stuck, or the agent is genuinely live).")
        return 0
    verb = "would reap" if a.dry_run else "reaped"
    for r in res["reaped"]:
        print(f"[fleet] unstick: {verb} ghost session {(r['sid'] or '')[:12]} "
              f"(lifecycle {r['life']!r}, pid {r['pid']}, {r['file']}) on surface {surf[:8]}")
    if not a.dry_run:
        fs.log_event("unstick", label=label or "", surface=surf,
                     reaped=[r["sid"] for r in res["reaped"]])
        print(f"[fleet] unstick: {tag}surface {surf[:8]} cleared -- `fleet recycle "
              f"{label or '<label>'}` or `fleet revive` will now recover it (or relaunch to self-clean).")
    return 0


# ---------------------------------------------------------------- sessions (list resumable priors)
def _tool_store(tool):
    """Just ONE tool's hook store (~/.cmuxterm/<tool>-hook-sessions.json), for TOOL-SCOPED surface reads —
    so a reused surface's cross-tool history can't select the wrong session dir or list another tool's
    sessions. Falls back to an empty store on any read error."""
    from .config import HOOKSTORE
    try:
        return json.load(open(os.path.join(HOOKSTORE, f"{tool}-hook-sessions.json")))
    except Exception:
        return {"sessions": {}, "activeSessionsBySurface": {}}


def _project_dir_for_surface(surf, tool="claude"):
    """The ~/.claude/projects/<enc-cwd>/ folder holding EVERY session jsonl for the surface's cwd. cmux
    records each session's transcriptPath; its parent dir is that folder — EXACT (no cwd re-encoding)
    whenever a live/historical record for the surface carries a transcriptPath. TOOL-SCOPED: reads only
    the entry's own tool store, so a surface that hosted two tools can't return the other tool's dir."""
    if not surf:
        return ""
    for s in _sessions_on_surface(_tool_store(tool), surf):
        tp = s.get("transcriptPath") or ""
        if tp:
            return os.path.dirname(tp)
    return ""


def _encode_project_dir(abs_cwd):
    """Claude Code's ~/.claude/projects/<dir> encoding: every non-alphanumeric char of the ABS cwd -> '-'
    (verified live: '/', '.', '_' all collapse; 'cmux-fleet/.worktrees' -> 'cmux-fleet--worktrees',
    'tapestry/_meta' -> 'tapestry--meta'). Fallback for a parked agent with no live surface/transcript."""
    import re
    enc = re.sub(r"[^a-zA-Z0-9]", "-", abs_cwd or "")
    return os.path.join(os.path.expanduser("~/.claude/projects"), enc) if enc else ""


def _projects_dir_for(entry, surf):
    """Resolve an agent's ~/.claude/projects dir: the surface's (tool-scoped) transcriptPath dir (exact)
    else the encoded cwd. '' if neither resolves."""
    tool = (entry or {}).get("tool", "claude")
    pdir = _project_dir_for_surface(surf, tool)
    if pdir:
        return pdir
    cwd = (entry or {}).get("cwd", "")
    abs_cwd = cwd if os.path.isabs(cwd) else os.path.join(ROOT, cwd)
    return _encode_project_dir(abs_cwd)


def _session_snippet(path, cap=64):
    """First user message of a session jsonl (the 'what was this session' hint), one line, capped."""
    try:
        for line in open(path):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") != "user":
                continue
            c = (e.get("message") or {}).get("content")
            t = c if isinstance(c, str) else (
                " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                if isinstance(c, list) else "")
            if t and t.strip():
                return t.strip().replace("\n", " ")[:cap]
    except OSError:
        pass
    return ""


def _list_sessions(entry, surf):
    """[(session_id, mtime, size, jsonl_path)] for an agent's cwd, freshest first. Empty if the projects
    dir doesn't resolve/exist. Pure filesystem read — the shared source for `fleet sessions` AND the
    --session validator."""
    pdir = _projects_dir_for(entry, surf)
    if not pdir or not os.path.isdir(pdir):
        return []
    import glob as _glob
    out = []
    for f in sorted(_glob.glob(os.path.join(pdir, "*.jsonl")), key=os.path.getmtime, reverse=True):
        try:
            st = os.stat(f)
        except OSError:
            continue
        out.append((os.path.basename(f)[:-6], st.st_mtime, st.st_size, f))   # strip .jsonl
    return out


def _human_size(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


def _known_session(entry, surf, sid):
    """True IFF `sid` matches a session jsonl under the agent's TOOL-SCOPED projects dir (bare-uuid
    compare). FAILS CLOSED: when the projects dir can't be resolved/enumerated (sparse archive, moved cwd,
    no transcriptPath) `_list_sessions` is empty and this returns False — we CANNOT confirm the id exists,
    so an explicit `--session` must not silently proceed into `claude --resume <bad-id>` = "No conversation
    found" (the exact footgun the flag exists to kill). Operators who KNOW the id is good bypass the
    check with `--force-session`."""
    from . import state as fs
    want = fs.bare_uuid(sid)
    return any(fs.bare_uuid(s) == want for s, *_ in _list_sessions(entry, surf))


def cmd_sessions(argv):
    """List resumable prior claude sessions for an agent's surface (freshest first) so an operator can
    pick an id for `fleet recycle --session <id>` / `fleet revive --session <id>` without
    hand-hunting under ~/.claude/projects. Marks the CURRENTLY-bound session with '*'. Works for a live
    OR archived label."""
    from . import state as fs
    from . import features as ff                                  # reuse the vitals age formatter
    ap = argparse.ArgumentParser(prog="fleet sessions")
    ap.add_argument("label", help="registry label (live or archived)")
    ap.add_argument("--all", action="store_true", help="list every session (default: 20 most recent)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    entry = fs.live_get(a.label) or fs.archive_get(a.label)
    if not entry:
        sys.exit(f"[fleet] sessions: no live/archived label '{a.label}'")
    surf = entry.get("surface", "")
    rows = _list_sessions(entry, surf)
    if not rows:
        sys.exit(f"[fleet] sessions: no ~/.claude/projects sessions found for '{a.label}' "
                 f"(dir: {_projects_dir_for(entry, surf) or '(unresolved)'})")
    # currently-bound session to mark: cmux checkpoint (ground truth) if present, else registry session
    cur = fs.bare_uuid(_resume_binding(surf).get("checkpoint_id", "")
                       or (entry.get("session") or "").replace("claude-", ""))
    shown = rows if a.all else rows[:20]
    if a.json:
        print(json.dumps([{"session": s, "mtime": mt, "size": sz,
                           "current": fs.bare_uuid(s) == cur, "snippet": _session_snippet(p)}
                          for s, mt, sz, p in shown], indent=2))
        return 0
    print(f"RESUMABLE SESSIONS for {a.label} ({len(rows)} total, showing {len(shown)}):")
    print(f"  dir: {_projects_dir_for(entry, surf)}")
    now = time.time()
    for s, mt, sz, p in shown:
        mark = "*" if fs.bare_uuid(s) == cur else " "
        print(f" {mark} {s:<38}{ff._age(now - mt):>7} ago {_human_size(sz):>8}  {_session_snippet(p)}")
    if any(fs.bare_uuid(s) == cur for s, *_ in shown):
        print(" (* = currently bound)")
    print(f" resume: fleet recycle {a.label} --session <id>   |   fleet revive {a.label} --session <id>")
    return 0


# ---------------------------------------------------------------- recycle (live->live, same surface)
# Restart an agent IN PLACE on its OWN surface via cmux's native `respawn-pane` (the tmux-compat
# kill+restart: cmux tears down the surface's current process and runs a fresh command in the SAME
# surface). Default = RESUME (preserves context — the least-disruptive action, ratified 2026-07-01); --fresh
# sheds context into a brand-new session and auto-primes from the latest handover. Same surfaceId -> the
# registry entry (label, parent/child pointers) stays valid with ZERO churn; only `session` changes. Runs
# DETACHED so it can recycle the CALLER itself.
#
# Why this is NOT the old kill-pid-then-type-the-launch-into-a-prompt dance: respawn-pane does the
# teardown+restart atomically and natively, so 3 of berg-sandbox's 6 lab guards are obsolete (the
# SIGINT escalation ladder, typing into a live prompt, polling the old pid for death). What survives:
#   (1) DETACHED  - can't respawn your own surface from inside your own turn.
#   (2) QUIET-GATE - respawn WILL kill mid-turn; wait for idle + empty draft (--force to override),
#                    re-checked after a settle. Never half-kills: aborts before respawn if not quiet.
#   (3) CONFIRM new session bound, then update the registry + auto-prime.
def _input_draft_nonempty(surf):
    """True if the surface's bottom-most prompt line carries text after the ❯ marker (a human draft)."""
    screen = cmuxq("read-screen", "--surface", surf, "--lines", "40")
    prompts = [ln for ln in screen.splitlines() if "❯" in ln]
    return bool(prompts and prompts[-1].split("❯", 1)[1].strip())


def _quiet_gate(surf, timeout, force):
    """Wait for the surface to go quiet, then clear the respawn. Returns True when clear, False on timeout.

    --force SHORT-CIRCUITS THE ENTIRE GATE: respawn now, no wait, regardless of lifecycle or draft. This
    is the escape hatch — consistent with `rm --force` (which closes a mid-turn 'running' surface anyway)
    and with the caller-side '--force to override' intent. It has to skip the WAIT, not just the draft
    check: a desynced/STALE surface's lifecycle never reads idle/needsInput/unknown, so a force run that
    still ran the lifecycle check could NEVER satisfy the gate and burned the full timeout to an ABORT —
    identical to a non-force run (the exact bug this fixes).

    NON-force (the default) is UNCHANGED: block until a NON-'running' lifecycle AND an empty draft,
    re-checked after a 2s settle to avoid racing a turn start. Never half-kills a live turn.
    'unknown' counts as quiet: cmux's session-start sets agentLifecycle='unknown' on a fresh start OR a
    resume and explicitly does NOT claim 'running', so an agent that resumed-but-was-never-driven (no
    Stop hook yet -> never reaches 'idle') sits at 'unknown' awaiting input. Excluding it made a
    just-resumed agent un-recyclable (the gate would block until the ABORT) -- so back-to-back resume
    recycles deadlocked."""
    from . import state as fs
    if force:
        return True          # --force = respawn now, no wait (consistent with rm --force). See docstring.
    def quiet():
        lc = fs.lifecycle(surf)
        # A 'running' record on a DEAD process is a frozen ghost (SessionEnd-less death), NOT a live
        # turn -- there is nothing to interrupt, so it counts as quiet. Without this a self-bricked
        # agent (the 2026-07-06 dead-agent class: lifecycle frozen 'running', pid dead) could never be
        # recovered by a plain `fleet recycle` -- the gate would block the full 180s and ABORT, forcing
        # --force. The pid, not the string, is the authority (see _confirmed_gone).
        if lc == "running" and not fs.surface_has_live_pid(surf):
            return True
        return lc in ("idle", "needsInput", "unknown") and not _input_draft_nonempty(surf)
    end = time.time() + timeout
    while time.time() < end:
        if quiet():
            time.sleep(2)
            if quiet():
                return True
        time.sleep(1)
    return False


def _latest_handover(abs_cwd):
    """Newest handover/*.md under the agent's cwd (the cmux-handover convention), or '' if none."""
    hd = os.path.join(abs_cwd, "handover")
    try:
        files = [os.path.join(hd, f) for f in os.listdir(hd) if f.endswith(".md")]
    except OSError:
        return ""
    return max(files, key=os.path.getmtime) if files else ""


def _poll_session_back(surf, old_sid, mode, timeout=90, exclude=None):
    """Confirm the recycled agent re-bound a session to `surf`. respawn-pane fully REMOVES the old
    session entry from cmux's hook store (session-end), then the relaunch re-creates it:
      FRESH  -> a brand-new session id (sid != old_sid).
      RESUME -> the SAME session id. `claude --resume <id>` CONTINUES the session (same id, same
                transcript JSONL -- no fork; verified live), re-created with a fresh pid and
                agentLifecycle '' -> 'unknown'. So we CANNOT wait for a different sid (it never
                comes); we wait for the surface to carry a live (non-empty) lifecycle again, which
                only happens once resume's session-start fires. activeSessionsBySurface stays null
                until the first turn, so we rely on poll_session's sessions[] fallback + lifecycle.
    `exclude` is a set of sids that do NOT count as a fresh bind (old_sid plus any stale store entry
    lingering on the surface post-respawn) -- prevents a crashed launch from false-confirming.
    Returns the bound sid, or '' on timeout."""
    from . import state as fs
    exclude = exclude or {old_sid}
    end = time.time() + timeout
    while time.time() < end:
        sid = poll_session(surf, timeout=1)
        # RESUME confirm waits for the surface to carry a live lifecycle again (resume reuses old_sid, so
        # 'a different sid' never comes). Require surface_has_live_agent -- non-terminal AND a LIVE pid --
        # not just the string: a leftover frozen dead-pid 'running' ghost (SessionEnd-less brick,
        # 2026-07-06) would else false-confirm the re-bind before the resumed agent has actually booted.
        if sid and (sid not in exclude if mode == "fresh"
                    else fs.surface_has_live_agent(surf)):
            return sid
        time.sleep(1)
    return ""


def _resume_binding(surf):
    """cmux's ground-truth relaunch binding for a surface (its agent-hook captures the real launch
    cmd -> accurate even when our registry spec is sparse, e.g. hand-bootstrapped conductors). {} if
    none. This is the 'use cmux's own primitives' source of truth for recycle."""
    try:
        d = json.loads(cmuxq("surface", "resume", "get", "--surface", surf, "--json"))
        return d.get("resume_binding") or {}
    except Exception:
        return {}


def _binding_argv(command):
    """Agent argv (FLAGS only) from a resume_binding.command, tool-agnostic. cmux emits two shapes:
      claude: `... && /bin/sh -c '<payload>'` with each token wrapped '\\''TOK'\\'' (the wrapper shim).
      codex:  `... && 'codex' 'resume' '<id>' '<flags>'` -- plain single-quoted tokens, no sh -c.
    Extract the tokens, then drop the leading NON-flag cruft (binary name, a `resume <id>` subcommand,
    any positional) so only flags remain; recycle/revive re-add the resume per-tool on top."""
    import re
    command = command or ""
    argv = re.findall(r"'\\''(.*?)'\\''", command)            # claude: tokens inside the sh -c payload
    if not argv:                                              # codex/plain: tokenize after the last &&
        try:
            argv = shlex.split(command.rsplit("&&", 1)[-1])
        except ValueError:
            argv = []
    while argv and not argv[0].startswith("-"):               # drop binary + subcommand + positional id
        argv.pop(0)
    return argv


def _prepend_resume(args, tool, sid):
    """Prefix a resume directive per tool: claude takes a `--resume <id>` FLAG, codex a `resume <id>`
    SUBCOMMAND. No sid (or an unknown tool) -> no-op = a fresh launch."""
    if not sid:
        return args
    if tool == "claude":
        return ["--resume", sid] + args
    if tool == "codex":
        return ["resume", sid] + args
    return args


def _replay_binding_argv(argv, tool, role, label, cwd, caller_tokens, add_plugins, resume_session, add_use=()):
    """Recompose a launch command from a captured binding's argv — the SHARED core of recycle (reads a
    LIVE surface binding) and revive (reads the binding captured at archive time). Strips the binding's
    own --resume (callers control it), unions add-plugins as --plugin-dir, layers caller flag overrides,
    optionally re-adds `--resume <resume_session>`, and re-injects AGENT_ROLE/AGENT_LABEL (bindings
    capture null env, so the orchestration vars must be put back). Other env (tool-floor env) is NOT
    recoverable from a binding — accepted, same as it's always been for recycle.
    `add_use` (index-aware `--use` names) routes through the index into BOTH channels here too: a `linked`
    name appends a --plugin-dir, an `enabled` name appends an enabledPlugins --settings (the wrapper deep-
    merges multiple --settings, so a fresh one is safe alongside the binding's own)."""
    abs_cwd = cwd if os.path.isabs(cwd) else os.path.join(ROOT, cwd)
    base = _drop_keys(argv, {"--resume"})
    have = {base[i + 1] for i in range(len(base) - 1) if base[i] == "--plugin-dir"}
    for name in (add_plugins or []):
        pd = _plugin_dir(name)
        if not pd or pd in have:
            if not pd:
                print(f"[fleet] warn: plugin '{name}' not resolvable (marketplace unset or not found); skipping")
            continue
        base += ["--plugin-dir", pd]; have.add(pd)
    if add_use:                                                  # index-aware add on the ad-hoc replay path
        use_linked, use_enabled, use_unresolved = _resolve_use(list(add_use), load_plugin_index())
        for name in use_unresolved:
            print(f"[fleet] warn: plugin '{name}' (use) not resolvable (marketplace unset or not found); skipping")
        for pd in use_linked:
            if pd not in have:
                base += ["--plugin-dir", pd]; have.add(pd)
        if use_enabled:                                          # extra --settings; wrapper deep-merges it in
            base += ["--settings", json.dumps({"enabledPlugins": {ref: True for ref in _dedup(use_enabled)}})]
    base = _layer_tokens([base, list(caller_tokens or [])])      # flag overrides
    base = _prepend_resume(base, tool, resume_session)           # claude --resume flag | codex resume subcmd
    # profile-pin a recycled/revived child too (bindings capture null env -> re-inject the build env)
    return render_send_cmd(tool, base, {**_profile_env(), "AGENT_ROLE": role, "AGENT_LABEL": label}, abs_cwd)


def _compose_from_registry(label, entry, caller_tokens, add_plugins, resume_session, add_use=()):
    """Fallback compose from our registry spec (used only when cmux has no binding for the surface).
    `add_use` unions index-aware names onto the spec's `use`, so adapter_compile routes them through the
    index into --plugin-dir / enabledPlugins exactly like the roster path."""
    tool = entry.get("tool", "claude")
    cwd = entry.get("cwd", "")
    abs_cwd = cwd if os.path.isabs(cwd) else os.path.join(ROOT, cwd)
    spec = {"tool": tool, "role": entry.get("role"), "label": label, "kind": entry.get("kind", "child"),
            "place": entry.get("place", "tab"), "group": entry.get("group", ""), "cwd": cwd,
            "abs_cwd": abs_cwd, "plugins": _dedup(list(entry.get("plugins", [])) + list(add_plugins or [])),
            "use": _dedup(list(entry.get("use", [])) + list(add_use or [])),
            "flags": _layer_tokens([list(entry.get("flags", [])), list(caller_tokens or [])]),
            "env": {}, "settings": entry.get("settings", "")}
    bin_name, args, env = adapter_compile(tool, spec, [])
    args = _prepend_resume(args, tool, resume_session)           # claude --resume flag | codex resume subcmd
    return render_send_cmd(bin_name, args, env, abs_cwd)


def _compose_from_roster(role, tool, label, caller_tokens, add_plugins, resume_session, cwd_override="", add_use=()):
    """TOML-AUTHORITATIVE compose for a ROSTER role: re-resolve the CURRENT toml (floor + role config,
    incl. setting_sources / enable_plugins), compile it exactly as `fleet launch` does, then prepend the
    resume per tool. This is the source-of-truth path -- a recycle/revive of a rostered agent PICKS UP
    floor/role changes made since it launched (a frozen binding or a sparse registry can't, and the
    registry never even stored the newer keys). Identity (label/surface/parent/session) stays in the
    registry; only the LOADOUT is re-resolved. One-off caller `--` flags apply this invocation only
    (to persist a change, edit the toml).
    `cwd_override` pins the launch cwd (used on RESUME): a claude session lives in the project dir of the
    cwd it was CREATED in, so a resume must run from THAT cwd — not a re-resolved toml cwd that may have
    moved (or the repo cwd for a worktree agent) -> otherwise `claude --resume` hits 'No conversation
    found'. FRESH passes no override and adopts the current toml cwd."""
    cfg = load_config()
    spec = resolve(cfg, role, tool, None)
    spec["label"] = label                                        # registry label (resolve defaults to role)
    if add_plugins:
        spec["plugins"] = _dedup(spec["plugins"] + list(add_plugins))
    if add_use:
        # UNION `--use` names into the re-resolved spec's `use` BEFORE adapter_compile — index routes each
        # to the right channel, so `--use <enabled>` reaches enabledPlugins on a roster recycle (the gap
        # --add-plugin could never close). Coexists with a role's own `use` (unioned + deduped downstream).
        spec["use"] = _dedup(spec.get("use", []) + list(add_use))
    cwd = cwd_override or spec["cwd"]
    abs_cwd = cwd if os.path.isabs(cwd) else os.path.join(ROOT, cwd)
    bin_name, args, env = adapter_compile(tool, spec, caller_tokens)
    args = _prepend_resume(args, tool, resume_session)
    return render_send_cmd(bin_name, args, env, abs_cwd)


def _is_roster(role):
    """True if `role` is a named roster role in the toml (-> toml-authoritative). Ad-hoc / off-roster
    labels are not, and reproduce from their captured launch instead."""
    try:
        return bool(role) and role in (load_config().get("role") or {})
    except SystemExit:
        return False


def _compose_recycle_cmd(label, entry, caller_tokens, add_plugins, mode, explicit_session="", add_use=()):
    """Recompose the recycle launch. ROSTER agents (role in the toml) are TOML-AUTHORITATIVE: re-resolve
    the current toml so a recycle picks up floor/role changes since launch. AD-HOC / off-roster agents
    have no toml to resolve -> reproduce from cmux's ground-truth binding (registry spec as last resort).
    Identity + session come from the registry; FRESH drops the resume, RESUME re-adds it per tool.
    One-off caller `--` flags apply this invocation only. `add_use` unions index-aware `--use` names into
    whichever compose path runs (roster/binding/registry) — reaching BOTH plugin channels. Returns
    (send_cmd, checkpoint)."""
    tool = entry.get("tool", "claude")
    role = entry.get("role")
    b = _resume_binding(entry.get("surface", ""))
    checkpoint = b.get("checkpoint_id", "")
    # the session to resume: an EXPLICIT --session target wins (resume an arbitrary prior session, no
    # cmux-checkpoint surgery); else cmux's checkpoint if it has one; else the registry's recorded session.
    resume_session = ((explicit_session or checkpoint or (entry.get("session") or "").replace("claude-", ""))
                      if mode == "resume" else None)
    if _is_roster(role):                                          # ROSTER -> re-resolve the toml (truth)
        # RESUME pins the session's original cwd (registry) so a moved-role / worktree agent resumes where
        # its session actually lives; FRESH adopts the current toml cwd (picks up an intentional move).
        cwd_override = entry.get("cwd", "") if mode == "resume" else ""
        return (_compose_from_roster(role, tool, label, caller_tokens, add_plugins, resume_session,
                                     cwd_override, add_use=add_use),
                checkpoint)
    argv = _binding_argv(b.get("command", ""))                    # AD-HOC / off-roster -> reproduce
    if not argv:                                                  # no cmux binding -> registry fallback
        return _compose_from_registry(label, entry, caller_tokens, add_plugins, resume_session,
                                      add_use=add_use), checkpoint
    cwd = b.get("cwd") or entry.get("cwd", "")
    send_cmd = _replay_binding_argv(argv, tool, role, label, cwd, caller_tokens, add_plugins,
                                    resume_session, add_use=add_use)
    return send_cmd, checkpoint


def _sendcmd_session_prefs(send_cmd):
    """GROUND-TRUTH session prefs {'effort','model'} read off a COMPOSED send_cmd's own tokens (str
    value or None per key). This is the one source that sees a caller's one-off --effort/--model
    override: caller tokens are only ever merged into the final command string by adapter_compile's
    token-layering, never written back onto a spec dict, so spec-reading paths (compute_effective)
    are blind to exactly that case. Shared by _session_pref_provenance (the live print) and the
    recycled/revived log_event `effective` fields (the ledger) -- one source of truth, not a fork."""
    try:
        toks = shlex.split(send_cmd or "")
    except ValueError:
        toks = []
    out = {}
    for name in ("--effort", "--model"):
        val = _flag_val(toks, name)
        out[name[2:]] = val if isinstance(val, str) else None
    return out


def _session_pref_provenance(role, tool, send_cmd, effort_override, model_override):
    """(provenance_line, warning) for the SESSION-PREFERENCE flags (effort/model) on a recycle/launch —
    makes 'why did it come back on X' obvious (the cmux-advisor-came-back-on-high surprise). The effective
    value is read from the composed command; its SOURCE is resolved override > role-pin > floor >
    binding(ad-hoc). WARNS when a ROSTER role inherits the [tool.<t>] floor effort with NO role pin: a
    mid-session /effort writes to GLOBAL settings and is overridden by the launch flag, so it won't survive
    the respawn — pin the role (the durable authority) or pass --effort."""
    prefs = _sendcmd_session_prefs(send_cmd)
    roster = _is_roster(role)
    cfg = load_config() if roster else {}
    tdef = (cfg.get("tool", {}) or {}).get(tool, {}) or {}
    rblock = (cfg.get("role", {}) or {}).get(role, {}) or {}
    rtool = (rblock.get(tool) if isinstance(rblock.get(tool), dict) else {}) or {}
    parts, warn = [], ""
    for name, override in (("--effort", effort_override), ("--model", model_override)):
        key = name[2:]
        val = prefs[key]
        if not val:
            continue
        if override:
            src = "override"
        elif not roster:
            src = "binding"
        elif _flag_val(shlex.split(rtool.get("flags", "")), name) not in (None, True):
            src = "role-pin"
        elif _flag_val(shlex.split(tdef.get("flags", "")), name) not in (None, True):
            src = "floor"
            if key == "effort":
                warn = (f"[fleet] note: effort '{val}' comes from the [tool.{tool}] floor — role '{role}' "
                        f"has no --effort pin, so a mid-session /effort won't survive this respawn. Pin it "
                        f"in [role.{role}.{tool}].flags (the durable authority), or pass --effort.")
        else:
            src = "settings/env"
        parts.append(f"{key}={val} ({src})")
    # MODEL-ANALOG of the effort floor-warning above: the loop only warns when a flag IS present (its
    # body runs solely when `val` is truthy), so it structurally CANNOT cover "no --model at all". A
    # roster role with NO --model token anywhere (no role pin, no caller override) silently rides the
    # AMBIENT global default -- the sonnet-instead-of-opus surprise that bit an unpinned role. Placed
    # OUTSIDE the loop; `warn = warn or (...)` so an effort floor-warning already set this call still
    # wins (one caller `if provwarn:` print, no new plumbing).
    if not prefs.get("model") and roster and not model_override:
        warn = warn or (f"[fleet] note: no --model anywhere for role '{role}' — this recycle will ride "
                        f"whatever the AMBIENT global default is right now, not a fixed identity. Pin it "
                        f"in [role.{role}.{tool}].flags, or pass --model.")
    return ("[fleet] session-prefs: " + ", ".join(parts)) if parts else "", warn


def _cwd_of_sendcmd(send_cmd):
    """The abs cwd a composed launch cd's into (`cd <cwd> && ...`), or ''. Used to PERSIST the effective
    cwd after a FRESH recycle/revive: the new session is created under this cwd's project dir, so the
    registry must record it — else the next default RESUME composes `cd <stale cwd> && --resume <new sid>`
    and hits 'No conversation found' (the exact class #4 kills, re-opened by a stale registry cwd)."""
    try:
        toks = shlex.split(send_cmd or "")
    except ValueError:
        return ""
    return toks[1] if len(toks) >= 2 and toks[0] == "cd" else ""


def _recycle_plan(label, entry, caller, add_plugin, mode, session, force, prime_override, no_prime, add_use=()):
    """Compose ONE recycle payload (the dict the detached exec consumes). Shared by single + bulk recycle
    so the mode/session/prime logic lives in exactly one place. FRESH boots clean -> auto-prime from the
    latest handover; RESUME carries its context -> no prime unless asked. `add_use` unions index-aware
    `--use` names into the composed loadout (reaching BOTH plugin channels)."""
    surf = entry.get("surface", "")
    old_sid = (entry.get("session") or "").replace("claude-", "")
    send_cmd, _checkpoint = _compose_recycle_cmd(label, entry, caller, add_plugin, mode, session, add_use=add_use)
    prime = None
    if not no_prime:
        if prime_override:
            prime = prime_override
        elif mode == "fresh":
            abs_cwd = entry.get("cwd", "")
            abs_cwd = abs_cwd if os.path.isabs(abs_cwd) else os.path.join(ROOT, abs_cwd)
            ho = _latest_handover(abs_cwd)
            prime = (f"You were just recycled into a FRESH session (same identity: label '{label}', "
                     f"role '{entry.get('role')}', same surface). Re-orient from your latest handover"
                     + (f" at {ho}" if ho else " under ./handover/")
                     + ", then continue where it left off.")
    return {"label": label, "surface": surf, "send_cmd": send_cmd, "mode": mode,
            "tool": entry.get("tool", "claude"), "force": force, "prime": prime, "old_session": old_sid,
            "cwd": _cwd_of_sendcmd(send_cmd),          # effective launch cwd, persisted after a FRESH bind
            # deterministic plugin set (entry + add_plugin union) for the recycled event's `effective`
            # field -- no token-scan needed for this part, unlike effort/model.
            "plugins": _dedup(list(entry.get("plugins", [])) + list(add_plugin or []))}


def _bulk_targets(target, from_surface, from_label, include_muted):
    """Live agents matching a bulk selector, mirroring `broadcast`'s target vocabulary. ALWAYS excludes
    self + unbound surfaces (external recycle is the safe topology — a conductor can't respawn its own
    surface from its own turn). Muted / human-driven agents (homelab, resume-research) are SKIPPED by
    default; --include-muted keeps them. Returns (selected [(label,entry)], skipped [(label,reason)])."""
    from . import state as fs
    sel, skipped = [], []
    for label, v in fs.live_all().items():
        surf = v.get("surface")
        if not surf or surf == from_surface:                 # self / unbound -> never
            continue
        kind = v.get("kind")
        if target == "conductors" and kind != "conductor":
            continue
        if target == "children" and kind != "child":
            continue
        if target == "my-children" and not (kind == "child" and v.get("parent") == from_label):
            continue
        if v.get("muted") and not include_muted:
            skipped.append((label, "muted/human-driven")); continue
        # STALE/non-live (same signal `fleet ls` shows): NO genuinely-live agent on the surface -> skip in
        # a bulk sweep (respawn-pane would target a gone seat / the quiet-gate would burn its timeout).
        # Routed through surface_has_live_agent so a frozen dead-pid 'running' ghost (SessionEnd-less brick,
        # 2026-07-06) reads STALE here too, CONSISTENT with cmd_ls (both now pid-aware). The skip is
        # REPORTED, so the brick surfaces to the operator for an explicit `fleet recycle <label>` (itself
        # now pid-aware and able to recover it) rather than being silently retried inside the sweep.
        if not fs.surface_has_live_agent(surf) and v.get("session"):
            skipped.append((label, "stale/non-live")); continue
        sel.append((label, v))
    sel.sort()
    return sel, skipped


def cmd_recycle(argv):
    """Restart THIS (or a named) agent in place on the same surface, same identity. A bulk `--scope`
    (mine|all|conductors|children) restarts many, sequentially + gated. Bare (no label, no scope) = self.
    See block comment."""
    from . import state as fs
    caller = []
    if "--" in argv:
        i = argv.index("--"); argv, caller = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="fleet recycle", add_help=True)
    ap.add_argument("label", nargs="?", help="registry label (default: self, via $CMUX_SURFACE_ID)")
    ap.add_argument("--fresh", action="store_true",
                    help="SHED context: recycle into a brand-new session, auto-primed from the latest "
                         "handover. Default is RESUME (preserve context) — --fresh is the explicit opt-in.")
    ap.add_argument("--session", default="", metavar="ID",
                    help="resume an ARBITRARY prior session id directly (list with `fleet sessions "
                         "<label>`) — no cmux-checkpoint surgery; single-target only")
    ap.add_argument("--force-session", action="store_true",
                    help="skip the --session existence check (use when the id is known-good but its "
                         "projects dir can't be enumerated)")
    ap.add_argument("--effort", default="", metavar="LEVEL",
                    help="session-preference override for THIS restart (low|medium|high|xhigh|max); layers "
                         "over the composed loadout. Durable per-agent effort belongs in the role's toml.")
    ap.add_argument("--model", default="", metavar="MODEL",
                    help="session-preference override for THIS restart; layers over the composed loadout")
    ap.add_argument("--force", action="store_true", help="skip the empty-draft guard (intentional go-live)")
    ap.add_argument("--add-plugin", action="append", default=[], metavar="NAME",
                    help="union a marketplace plugin into this identity (repeatable; persisted; legacy: linked-only)")
    ap.add_argument("--use", action="append", default=[], metavar="NAME",
                    help="index-aware plugin to UNION into this identity for the restart (repeatable or "
                         "comma-sep; persisted via the re-captured binding). Routes through plugins.toml so "
                         "a `linked` name adds a --plugin-dir and an `enabled` name adds an enabledPlugins "
                         "entry — reaching BOTH plugin types (unlike --add-plugin). Unindexed names fall "
                         "back to today's behavior")
    ap.add_argument("--prime", help="override the post-fresh-boot priming prompt")
    ap.add_argument("--no-prime", action="store_true", help="don't send any priming prompt")
    ap.add_argument("--dry-run", action="store_true", help="resolve + print, do NOT recycle")
    # bulk / cross-conductor selector: one unified --scope (mirrors every verb); sequential + gated,
    # external-recycle is safe. For an ACT, `mine` = your children (NOT self — a bare recycle is self).
    ap.add_argument("--scope", default="", metavar="SET",
                    help="bulk restart a SET: mine (your children) | all | conductors | children. "
                         "Sequential + gated, skips self + muted. Omit for single-target (bare = self).")
    ap.add_argument("--include-muted", action="store_true",
                    help="bulk: also recycle muted/human-driven agents (skipped by default)")
    a = ap.parse_args(argv)

    # DEFAULT FLIPPED (ratified 2026-07-01): recycle now RESUMES (preserves context) by default; --fresh is
    # the explicit context-shedding opt-in (was the silent default that dropped berg-sandbox's session).
    if a.fresh and a.session:
        sys.exit("[fleet] recycle: --fresh and --session are contradictory (fresh sheds context; --session resumes one)")
    mode = "fresh" if a.fresh else "resume"
    # session-preference overrides funnel into the caller-token layer (highest precedence over the composed
    # floor/role loadout) — applies to the single AND bulk paths.
    if a.effort:
        caller += ["--effort", a.effort]
    if a.model:
        caller += ["--model", a.model]
    # --scope maps onto ONE internal bulk target vocab {all,conductors,children,my-children}.
    # `mine` -> `my-children` (an act's `mine` is your children); the rest map name-for-name.
    SCOPE_TO_TARGET = {"mine": "my-children", "all": "all", "conductors": "conductors", "children": "children"}
    target = None
    if a.scope:
        if a.scope not in SCOPE_TO_TARGET:
            sys.exit(f"[fleet] recycle: --scope must be one of {list(SCOPE_TO_TARGET)}")
        target = SCOPE_TO_TARGET[a.scope]
    if target:
        if a.label or a.session:
            sys.exit("[fleet] recycle: a bulk scope can't combine with a <label> or --session (per-target)")
        return _recycle_bulk(target, mode, caller, a)

    label = a.label or fs.label_for_surface(os.environ.get("CMUX_SURFACE_ID", ""))
    if not label:
        sys.exit("[fleet] recycle: no label and can't resolve self from $CMUX_SURFACE_ID")
    entry = fs.live_get(label)
    if not entry:
        sys.exit(f"[fleet] recycle: no LIVE label '{label}' (recycle is live->live; use `revive` for parked)")
    surf = entry.get("surface", "")
    if not surf:
        sys.exit(f"[fleet] recycle: label '{label}' has no surface on its registry entry")
    if a.session and not a.force_session and not _known_session(entry, surf, a.session):
        sys.exit(f"[fleet] recycle: could not verify session '{a.session}' under {label}'s projects dir "
                 f"(bad id, or the dir couldn't be resolved/enumerated). `fleet sessions {label}` to list "
                 f"resumable ids; add --force-session to skip this check if you're sure the id is valid.")
    payload = _recycle_plan(label, entry, caller, a.add_plugin, mode, a.session, a.force, a.prime, a.no_prime,
                            add_use=_flatten_csv(a.use))
    provline, provwarn = _session_pref_provenance(entry.get("role"), entry.get("tool", "claude"),
                                                   payload["send_cmd"], a.effort, a.model)

    print(f"[fleet] recycle {label} (mode={mode}, tool={entry.get('tool','claude')}, surface={surf})")
    print(f"[fleet] launch: {payload['send_cmd']}")
    if provline:
        print(provline)                                          # effort/model + provenance (source)
    if provwarn:
        print(provwarn)                                          # no-pin warning (floor-inherited effort)
    if a.add_plugin or a.use or caller:
        print("[fleet] overrides applied (persist for free: cmux re-captures this as the new binding)")
    print(f"[fleet] prime: {payload['prime'] if payload['prime'] else '(none)'}")
    if a.dry_run:
        print("[fleet] dry-run (omit --dry-run to recycle)")
        return 0

    # hand to a DETACHED worker (own session) so it outlives this process and can respawn our own surface.
    # UNIQUE payload path (mkstemp): a fixed .recycle-<label>.json would let two concurrent recycles of the
    # same label clobber each other's payload before the detached worker reads it.
    os.makedirs(STATE, exist_ok=True)
    _lbl = "".join(c if (c.isalnum() or c in "_.-") else "_" for c in label)
    fd, pf = tempfile.mkstemp(prefix=f".recycle-{_lbl}-", suffix=".json", dir=STATE)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    log = os.path.join(STATE, "recycle.log")
    subprocess.Popen([sys.executable, "-m", "cmux_fleet", "_recycle-exec", pf],
                     stdout=open(log, "a"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    gate = "idle" if a.force else "idle + empty draft"
    print(f"[fleet] recycle SCHEDULED (detached) for {label} on {surf}; mode={mode}.")
    print(f"[fleet]   waits for the surface to go quiet ({gate}), then respawns in place. log: {log}")
    return 0


def _recycle_bulk(target, mode, caller, a):
    """Restart a SET of agents SEQUENTIALLY (one respawn at a time — never thundering-herd the box), each
    independently quiet-gated. Self is always excluded (external recycle avoids the can't-respawn-own-
    surface footgun); muted/human-driven agents skipped unless --include-muted. --dry-run prints the plan."""
    from . import state as fs
    scope = "mine" if target == "my-children" else target     # display the --scope value the caller typed
    from_surface = os.environ.get("CMUX_SURFACE_ID", "")
    from_label = fs.label_for_surface(from_surface) or (from_surface[:8] if from_surface else "fleet")
    if target == "my-children" and not from_surface:
        sys.exit("[fleet] recycle --scope mine needs $CMUX_SURFACE_ID (run inside a conductor)")
    sel, skipped = _bulk_targets(target, from_surface, from_label, a.include_muted)
    if not sel:
        print(f"[fleet] recycle --scope {scope}: no live targets"
              + (f" ({len(skipped)} skipped: {', '.join(l for l, _ in skipped)})" if skipped else ""))
        return 0
    ov = (f", effort={a.effort}" if a.effort else "") + (f", model={a.model}" if a.model else "")
    print(f"[fleet] recycle --scope {scope} (mode={mode}{ov}) from {from_label}: {len(sel)} target(s), sequential + gated")
    payloads = []
    for label, entry in sel:
        payload = _recycle_plan(label, entry, caller, a.add_plugin, mode, "", a.force, a.prime, a.no_prime,
                                add_use=_flatten_csv(a.use))
        payloads.append(payload)
        # per-agent RESOLVED effort/model (provenance) — mirror the single-target print so an operator
        # watching a bulk recycle sees what each agent is actually coming back on (a bulk recycle is
        # exactly where a silent model/effort drift, like the storm-era Sonnet downgrade, would slip by).
        provline, provwarn = _session_pref_provenance(
            entry.get("role"), entry.get("tool", "claude"), payload["send_cmd"], a.effort, a.model)
        print(f"   {label:<24}{entry.get('kind','-'):<11}{(entry.get('surface') or '')[:8]}  mode={mode}")
        if provline:
            print(f"      {provline}")                          # effort/model + provenance (source)
        if provwarn:
            print(f"      {provwarn}")                          # no-pin warning (floor-inherited effort)
    for label, reason in skipped:
        hint = "; --include-muted to force" if reason.startswith("muted") else "; `fleet revive`/`recycle` it directly"
        print(f"   {label:<24}SKIP ({reason}{hint})")
    if a.dry_run:
        print("[fleet] dry-run (omit --dry-run to recycle these sequentially)")
        return 0
    # UNIQUE payload path (mkstemp): a fixed .recycle-bulk.json lets two concurrent bulk restarts clobber
    # each other's target list before the detached worker reads it (A's --children worker runs B's targets).
    os.makedirs(STATE, exist_ok=True)
    fd, pf = tempfile.mkstemp(prefix=".recycle-bulk-", suffix=".json", dir=STATE)
    with os.fdopen(fd, "w") as fh:
        json.dump(payloads, fh)
    log = os.path.join(STATE, "recycle.log")
    subprocess.Popen([sys.executable, "-m", "cmux_fleet", "_recycle-bulk-exec", pf],
                     stdout=open(log, "a"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    print(f"[fleet] bulk recycle SCHEDULED (detached, sequential) for {len(payloads)} agent(s). log: {log}")
    print(f"[fleet]   each waits for its surface to go quiet, respawns, then the next. Watch: tail -f {log}")
    return 0


def _count_plugin_dirs(send_cmd):
    """Loadout weight proxy: how many `--plugin-dir` plugins the launch carries. Heavier loadouts boot
    slower (RAM pressure + more plugins to load), so the resume-menu watch window scales by this."""
    return (send_cmd or "").count("--plugin-dir")


def _resume_menu_timeout(plugin_count, base=60, per_plugin=8, ceiling=120):
    """Ceiling for the resume-menu watch. The PRIMARY fix is event-driven polling (below), not a bigger
    number; this is only the generous upper bound. Base is already generous (heavy loadouts take 30-40s
    to boot, not 2s); plugin count is a SECONDARY heuristic that stretches the ceiling for the heaviest
    loadouts (e.g. homelab's 6 plugins) without over-waiting light ones."""
    return min(ceiling, base + per_plugin * max(0, plugin_count))


# tri-state outcomes of the resume-menu watch (see _dismiss_resume_summary_prompt)
RESUME_DISMISSED = "dismissed"   # the summary menu rendered and we picked 'full session as-is'
RESUME_READY = "ready"           # resumed straight to a running prompt (no menu; nothing to do)
RESUME_TIMEOUT = "timeout"       # neither appeared within the ceiling -> the caller MUST NOT bind


def _dismiss_resume_summary_prompt(surf, log, timeout=None, plugin_count=0):
    """`claude --resume` on an OLD/LARGE session shows an interactive menu before resuming:
         1. Resume from summary (recommended)   2. Resume full session as-is   3. Don't ask me again
    A respawn/recycle has NO human to choose, so the agent HANGS at the menu (and the resume-confirm
    false-passes on the bound-but-stuck session). Policy: ALWAYS resume FULL, never summarize/compact.
    No claude flag/setting/env var exists to suppress this or force full (GitHub #46751, verified), so a
    keystroke is the only lever: the cursor defaults to option 1, so DOWN -> option 2 ('full as-is'),
    then ENTER.

    This is a PURE TIMING gate, not a detection problem: the menu renders fine, but a heavy loadout can
    take 30-40s to boot, so a fixed window closed before it appeared (WARN + revive left at the shell —
    homelab's symptom). We poll the pane for ONE of three states until a GENEROUS, loadout-scaled ceiling
    (never a single fixed sleep):
      - RESUME_DISMISSED: the menu is up -> pick 'full session as-is'
      - RESUME_READY:     already at a running prompt -> nothing to dismiss
      - RESUME_TIMEOUT:   still booting past the ceiling -> the CALLER MUST treat this as a failed resume
                          and NOT proceed to bind/register (the menu is a GATE that blocks the session
                          bind; binding behind an undismissed menu leaves the agent running UNREGISTERED)."""
    if timeout is None:
        timeout = _resume_menu_timeout(plugin_count)
    end = time.time() + timeout
    resuming = False                    # saw the POST-selection 'Resuming ...' state (menu already gone)
    while time.time() < end:
        pane = cmuxq("capture-pane", "--surface", surf) or ""
        # ONLY the LIVE menu is actionable — it shows BOTH option labels at once
        # ('1. Resume from summary' AND '2. Resume full session as-is'). The old check also fired on
        # "Resuming the full session", but that is the POST-selection / in-progress banner: the menu is
        # already gone, so a down/enter there lands a STRAY keystroke on a no-longer-menu surface. Match
        # only the real menu; treat 'Resuming the full session' as in-progress and keep polling.
        if "Resume from summary" in pane and "Resume full session as-is" in pane:
            log("resume-summary menu detected -> picking 'Resume full session as-is' (full, never compact)")
            cmuxq("send-key", "--surface", surf, "down")
            time.sleep(0.5)
            cmuxq("send-key", "--surface", surf, "enter")
            return RESUME_DISMISSED
        # small session resumed straight to a running prompt -> no menu, nothing to dismiss
        if "Context Remaining" in pane or "bypass permissions" in pane:
            return RESUME_READY
        if "Resuming the full session" in pane:
            resuming = True             # menu was already resolved -> resume is underway; don't touch keys
        time.sleep(1)
    if resuming:
        # the resume got past the menu on its own (a human, or a prior dismiss) and is loading the full
        # session; it just hadn't reached a running prompt before the ceiling. Safe to bind.
        log("resume in progress (summary menu already cleared); proceeding to bind")
        return RESUME_READY
    log(f"WARN: resume launched but neither the summary-menu nor a running prompt appeared within "
        f"{timeout:.0f}s (plugin_count={plugin_count}); NOT binding -- surface is still booting or "
        f"wedged behind the menu. Re-run once it settles.")
    return RESUME_TIMEOUT


def _resume_and_gate(surf, send_cmd, tool, sess, log):
    """Dismiss the resume-summary menu AND report whether the surface is safe to bind. The menu blocks
    the session bind, so poll_session/register can't succeed until it clears; a timed-out dismiss that
    the caller ignored is exactly what left agents running UNREGISTERED (live pane, still shown archived).
    Returns True iff the resume resolved (menu dismissed OR already at a running prompt); False iff it
    timed out -- the caller MUST NOT bind/register. A no-op True for fresh / non-claude launches (no menu)."""
    if not (sess and tool == "claude"):
        return True
    status = _dismiss_resume_summary_prompt(surf, log, plugin_count=_count_plugin_dirs(send_cmd))
    return status != RESUME_TIMEOUT


# Ceiling for _respawn_and_verify's post-respawn poll. respawn-pane's kill is ASYNC and normally lands
# in well under a second, but a transient cmux hang can stretch it (the confirmed field failure: a 15s
# "Command timed out"). 30s leaves a wide margin over that without letting one attempt hang forever.
_RESPAWN_VERIFY_TIMEOUT = 30


def _recycle_exec_one(p):
    """Run ONE recycle: quiet-gate -> respawn-pane (verified) -> confirm new session -> reconcile the
    registry -> auto-prime. Never half-kills: aborts before respawn if the surface won't go quiet, and
    never sends the launch unless the old session is confirmed dead (see _respawn_and_verify) -- a
    respawn-pane timeout that goes unverified leaves the old claude ALIVE, and blindly firing the launch
    types it as an unsubmitted draft into that live TUI (the 9h berg-sandbox silent-recycle failure).
    Shared by the single `_recycle-exec` verb and the sequential `_recycle-bulk-exec` orchestrator.
    Returns 0 when the respawn proceeded (bound or lazy), 1 on a pre-respawn / verify-respawn /
    resume-gate abort."""
    from . import state as fs
    surf, send_cmd, label = p["surface"], p["send_cmd"], p["label"]
    mode, force, prime, old_sid = p["mode"], p["force"], p.get("prime"), p.get("old_session") or ""

    def log(m):
        print(f"[recycle {time.strftime('%H:%M:%S')}] {label}: {m}", flush=True)

    # ledger parity with log_launch: the EFFECTIVE effort/model, read off the composed command itself
    # (_sendcmd_session_prefs -- the only source that sees a one-off --effort/--model on THIS recycle).
    effective = {**_sendcmd_session_prefs(send_cmd), "plugins": p.get("plugins", [])}

    log(f"start mode={mode} surface={surf} force={force}")
    if not _quiet_gate(surf, 180, force):
        log("ABORT: surface never went quiet within 180s; NOT respawning (no half-kill). Re-run when idle or pass --force.")
        return 1
    # respawn-pane natively tears down the old agent + restarts the pane in the SAME seat. We restart
    # it as a fresh INTERACTIVE login shell (not the agent directly): cmux exposes `claude` as a zsh
    # FUNCTION via its shell integration, so the agent must launch from a shell that sourced ~/.zshrc
    # -- a bare `/bin/sh -c claude` fails with 'claude not found'. Then we `send` the launch into it.
    # NOTE: the login shell's PATH is built incrementally during init; the send below PATH-guards the
    # command so a too-early send can't crash on an unready PATH (see `guarded`).
    # PATH-GUARD the launch: the cmux claude-wrapper's find_real_claude walks $PATH for the real binary
    # (~/.local/bin/claude, added by ~/.zshenv). If the send lands before the shell finished building
    # PATH, the wrapper exits 127 'claude not found in PATH'. Prepending the standard dirs makes the
    # binary resolvable regardless of shell-init timing (harmless no-op for codex/other tools).
    guarded = 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"; ' + send_cmd

    def _fire_launch():
        log("launching agent into the fresh shell")
        cmuxq("send", "--surface", surf, guarded)
        cmuxq("send-key", "--surface", surf, "enter")
        # VERIFY the ENTER actually SUBMITTED the paste -- mirrors _send_launch_and_confirm's shape
        # (retry the ENTER, never the paste). The terminating newline can lose the paste-settle race,
        # leaving the launch as an inert DRAFT at the shell; the downstream self-heal then re-sends the
        # WHOLE TEXT on top of it -> the doubled/tripled draft seen in orphan surface AAF4EC13. So we
        # ONLY ever re-kick a bare Enter here -- the launch text is NEVER resent. Either signal means
        # the line submitted: _agent_surfaced (a TUI marker painted) OR _resume_menu_visible (claude's
        # resume-summary menu is up). Bounded to max_kicks (~10s worst case), tiny vs the outer
        # _poll_session_back(90) ceiling that runs AFTER this returns; re-kicks stop the instant a TUI
        # surfaces, so a slow boot is never spammed.
        kicks, max_kicks = 0, 5
        while kicks < max_kicks:
            if _agent_surfaced(surf) or _resume_menu_visible(surf):
                return                                       # submitted -> stop re-kicking
            cmuxq("send-key", "--surface", surf, "enter")    # re-kick the ENTER only, never the paste
            kicks += 1
            time.sleep(2)

    def _direct_kill():
        """cmux-INDEPENDENT teardown of the old claude process: SIGINT x2 (clean TUI exit) straight to
        its pid, the exact mechanism cmd_archive/cmd_rm already use for a reliable kill (Berg's manual
        Ctrl-C-twice). respawn-pane's OWN kill goes through cmux itself, so a wedged/unresponsive cmux
        (the confirmed 05:44 failure: a 15s 'Command timed out') can hang or silently no-op it; a raw
        os.kill on the pid doesn't care whether cmux is responsive. No-ops quietly if the pid is already
        gone or unreadable (best-effort fallback, not the primary path)."""
        import signal
        pid = _pid_for_surface(surf)
        if not pid:
            log("direct-kill fallback: no pid on record for this surface; skipping straight to respawn")
            return
        log(f"direct-kill fallback: SIGINT x2 -> pid {pid} (cmux-independent)")
        try:
            os.kill(pid, signal.SIGINT); time.sleep(0.5); os.kill(pid, signal.SIGINT)
        except (ProcessLookupError, PermissionError):
            pass

    def _confirmed_gone(old_pids):
        """The old agent is gone iff EITHER cmux flipped the lifecycle TERMINAL ('', '-', 'ended' --
        SessionEnd fired and cmux dropped/ended the record, the happy path) OR the PID is conclusive:
        no live process remains on this surface AND none of the pids snapshotted pre-respawn is still
        alive. The pid branch is the SessionEnd-freeze fix (root-caused 2026-07-06): an abrupt death
        (SIGKILL) or a SessionEnd store-write race (the berg-sandbox incident) leaves the record frozen
        NON-terminal ('running'/'idle'/'unknown') with a dead/None pid -- the lifecycle string is then a
        permanent lie, but a dead pid cannot host a TUI, so the agent is provably gone and relaunch is
        safe. Including the pre-respawn snapshot is the SAFETY FLOOR: if the ORIGINAL claude survived the
        respawn (wedged cmux), its pid is STILL ALIVE -> not gone -> we correctly refuse and never type
        into a live TUI (the exact failure the verify shipped to prevent)."""
        if fs.lifecycle(surf) in ("", "-", "ended"):
            return True
        return not fs.surface_has_live_pid(surf) and not any(fs.pid_alive(p) for p in old_pids)

    def _respawn_and_verify(kill_first=False):
        """One respawn-pane attempt, verified by polling for the OLD claude session's death instead of
        a blind sleep. respawn-pane is ASYNC and normally near-instant, but `out=='OK'` means 'command
        accepted', NOT 'old process killed' -- the old claude actually dies a few seconds later (see the
        module docstring above). Confirmation is pid-aware (see _confirmed_gone): a terminal
        agentLifecycle is the happy signal, but the AUTHORITATIVE one is 'the old pid is dead' -- because
        a SessionEnd-less death (SIGKILL) or a cmux write race can freeze the lifecycle non-terminal
        forever (the dead-agent brick class). We snapshot the surface's live pids BEFORE the respawn so
        a claude that SURVIVES the kill (its pid still alive) can never be mistaken for gone. `kill_first`
        runs the cmux-independent SIGINTx2 fallback before respawning -- for when cmux itself was too
        wedged for respawn-pane's own kill to land. Returns True once confirmed dead; False if `out`
        reports an error or the poll exhausts `_RESPAWN_VERIFY_TIMEOUT` while a live pid persists."""
        old_pids = _surface_pids(surf)                    # pre-respawn safety snapshot (alive pids only)
        if kill_first:
            _direct_kill()
        out = cmuxq("respawn-pane", "--surface", surf, "--command", "exec /bin/zsh -il")
        log(f"respawn-pane -> {out.strip()}")
        if "error" in out.lower():
            return False
        end = time.time() + _RESPAWN_VERIFY_TIMEOUT
        while time.time() < end:
            if _confirmed_gone(old_pids):
                return True
            time.sleep(1)
        return _confirmed_gone(old_pids)

    def _graceful_close(timeout=6):
        """Close the OLD session GRACEFULLY before respawn-pane so cmux's lifecycle reaches a clean
        terminal state and EVERY consumer (this verify, `fleet ls`, the fleet-doctor, vitals) sees
        honest state -- not a frozen 'running' ghost. SIGINT x2 straight to the pid is claude's clean
        TUI exit; empirically it fires SessionEnd even mid-turn (~0.5s; the 2026-07-06 sandbox matrix),
        and cmux then drops the record. Best-effort + bounded: skips when the pid is already dead
        (nothing to close -- the pid-aware verify handles the ghost), returns the instant the lifecycle
        goes terminal or the process dies, and ALWAYS falls through to the respawn. This is an HONESTY
        step, NOT the confirming signal: a fired SessionEnd can still be clobbered by a cmux write race
        (the berg-sandbox incident), which is exactly why _confirmed_gone -- not this -- authorizes the
        relaunch."""
        import signal
        pid = _pid_for_surface(surf)
        if not pid or not fs.pid_alive(pid):
            return                                        # already gone -> pid-aware verify confirms
        log(f"graceful close: SIGINT x2 -> pid {pid} (let claude fire SessionEnd before respawn)")
        try:
            os.kill(int(pid), signal.SIGINT); time.sleep(0.5); os.kill(int(pid), signal.SIGINT)
        except (ProcessLookupError, PermissionError, ValueError):
            return
        end = time.time() + timeout
        while time.time() < end:
            if fs.lifecycle(surf) in ("", "-", "ended") or not fs.pid_alive(pid):
                log("graceful close: old session reached a clean terminal lifecycle (SessionEnd fired)")
                return
            time.sleep(0.5)
        log("graceful close: no terminal lifecycle within window; proceeding to respawn (pid-verify decides)")

    log("quiet; respawn-pane -> fresh interactive shell (cmux kills the old agent in place)")
    _graceful_close()
    if not _respawn_and_verify():
        log(f"respawn not confirmed within {_RESPAWN_VERIFY_TIMEOUT}s; falling back to a direct "
            "SIGINTx2 kill (cmux-independent) then re-respawning")
        if not _respawn_and_verify(kill_first=True):
            log(f"ABORT: respawn still not confirmed after the direct-kill fallback; old session is "
                f"(almost certainly) still ALIVE on {surf} -- NOT sending the launch (would type into "
                f"a live TUI). Re-run `fleet recycle` for '{label}' once the surface is confirmed idle.")
            cmuxq("notify", "--surface", surf, "--title", "fleet recycle FAILED",
                  "--body", f"recycle FAILED for {label}: respawn didn't take; "
                            "still on the old session; re-run")
            fs.log_event("recycle_abort", label=label, surface=surf, mode=mode,
                         reason="respawn-not-confirmed")
            return 1
        log("confirmed after direct-kill fallback")
    # SNAPSHOT the surface's store sid right after the verified kill but BEFORE relaunch. cmux's
    # session-end does NOT reliably DROP the old entry from sessions[] (poll_session's fallback still
    # sees it even once its lifecycle reads terminal), so a fresh-mode confirm could match this STALE
    # sid and falsely report success even when the launch crashed -- then prime/wakes get typed into a
    # dead shell (the 'claude not found' incident). Excluding pre_sid makes a crash correctly resolve to
    # '' (no new session) -> WARN + no prime.
    pre_sid = poll_session(surf, timeout=1)
    _fire_launch()

    # CONFIRM is tool-aware. claude binds a session at BOOT -> poll for it (a NEW sid for fresh, the
    # surface live again for resume). codex (and others) bind LAZILY on their first turn AND fire no
    # SessionEnd, so the old store entry lingers after respawn -> there is no reliable pre-turn signal.
    # For lazy tools we don't poll: the session re-binds on the first turn and the router backfills it
    # (fresh -> clear the stale sid so the backfill takes; resume -> the sid is unchanged, keep it).
    lazy = p.get("tool", "claude") != "claude"
    if lazy:
        e = fs.live_get(label) or {}
        e["surface"] = surf
        if mode == "fresh":
            e["session"] = ""                            # a NEW session binds on 1st turn -> router backfills
            if p.get("cwd"):
                e["cwd"] = p["cwd"]                      # PERSIST the fresh cwd so the next RESUME finds the new session
        fs.live_put(label, e)
        fs.log_event("recycled", label=label, role=e.get("role"), surface=surf,
                     session=e.get("session") or "", mode=mode, effective=effective)
        log(f"respawned ({mode}); session re-binds on first turn ({p.get('tool')} registers lazily)")
        sid = old_sid if mode == "resume" else ""        # for the prime gate (prime IS the first turn)
        if prime:
            time.sleep(8)                                # codex boots slower than claude; let the TUI come up
    else:
        if mode == "resume":
            # full-resume the session (dismiss claude's summary-vs-full menu before it hangs the confirm),
            # and GATE the bind on it clearing: the menu blocks the session bind, so a timed-out dismiss
            # would otherwise fall through to a failed poll and skip the registry re-bind -> a live pane
            # that fleet no longer tracks. Abort instead so the caller re-runs when the surface settles.
            if not _resume_and_gate(surf, send_cmd, p.get("tool"), old_sid, log):
                log("ABORT: resume-summary menu never resolved within ceiling; NOT binding/registering "
                    "(surface still booting or wedged at the menu). Re-run `fleet recycle` later.")
                return 1
        # exclude pre_sid (the stale store entry snapshotted post-respawn) so a crashed launch can't
        # false-confirm on it; fresh requires a sid that is neither old_sid nor pre_sid.
        exclude = {old_sid, pre_sid} if mode == "fresh" else {old_sid}
        sid = _poll_session_back(surf, old_sid, mode, 90, exclude=exclude)
        if not sid and mode == "fresh":
            # SELF-HEAL: the launch likely crashed into the bare shell (e.g. PATH not ready -> wrapper
            # 'claude not found'). The shell is fully initialized by now, so re-fire ONCE -- mirrors the
            # manual recovery (re-running the same command succeeds) instead of leaving a dead pane.
            log("no fresh session bound; re-firing launch once (shell now settled)")
            _fire_launch()
            sid = _poll_session_back(surf, old_sid, mode, 60, exclude=exclude)
        if not sid:
            log(f"WARN: no {'resumed' if mode == 'resume' else 'fresh'} session bound; check the surface manually")
            # ESCALATE (mirror the respawn-abort path above): the launch was sent but nothing bound even
            # after the self-heal re-fire -- the SAME silent-failure class that left berg-sandbox down
            # ~9h undetected. Same recycle_abort event type (different reason) so any future consumer
            # (e.g. the conductor-liveness sweep) picks up both failure classes uniformly.
            cmuxq("notify", "--surface", surf, "--title", "fleet recycle FAILED",
                  "--body", f"recycle FAILED for {label}: launch sent but no "
                            f"{'resumed' if mode == 'resume' else 'fresh'} session bound; check the surface manually")
            fs.log_event("recycle_abort", label=label, surface=surf, mode=mode,
                         reason="no-session-after-launch")
        else:
            if mode == "resume":
                # prefer cmux's CHECKPOINT (the id it will `--resume`) over a possibly-bridge poll id, so
                # the registry records the SAME id a later archive/revive resumes — killing the divergence
                # at the source. The router reconciles again on the next turn as a continuous backstop.
                sid = _resume_binding(surf).get("checkpoint_id", "") or sid
            log(f"{'resumed' if mode == 'resume' else 'fresh'} session {sid} bound")
            e = fs.live_get(label) or {}
            e["surface"] = surf
            e["session"] = f"claude-{sid}" if e.get("tool", "claude") == "claude" else sid
            if mode == "fresh" and p.get("cwd"):
                e["cwd"] = p["cwd"]                      # PERSIST the fresh cwd (a role move -> new session lives here)
            fs.live_put(label, e)
            fs.log_event("recycled", label=label, role=e.get("role"), surface=surf, session=sid,
                         mode=mode, effective=effective)
        if prime and sid:
            time.sleep(3)                                # let the fresh TUI settle before sending input

    if prime and (sid or lazy):
        cmuxq("send", "--surface", surf, prime)
        cmuxq("send-key", "--surface", surf, "enter")
        log("primed")
    log("DONE")
    return 0


def cmd_recycle_exec(argv):
    """DETACHED worker (internal verb): one recycle from a payload file, then clean up the file."""
    rc = _recycle_exec_one(json.load(open(argv[0])))
    try:
        os.remove(argv[0])
    except OSError:
        pass
    return rc


def cmd_recycle_bulk_exec(argv):
    """DETACHED orchestrator (internal verb): recycle a LIST of payloads SEQUENTIALLY — one respawn at a
    time so a fleet-wide restart never thundering-herds the box. Each target is independently quiet-gated;
    a single target's abort/error does not stop the sweep. Cleans up the payload file at the end."""
    payloads = json.load(open(argv[0]))
    n = len(payloads)
    print(f"[recycle-bulk {time.strftime('%H:%M:%S')}] start: {n} target(s), sequential", flush=True)
    ok = 0
    for i, p in enumerate(payloads, 1):
        print(f"[recycle-bulk] === {i}/{n}: {p.get('label')} ===", flush=True)
        try:
            if _recycle_exec_one(p) == 0:
                ok += 1
        except Exception as e:                     # one target's failure must not abort the rest
            print(f"[recycle-bulk] {p.get('label')}: ERROR {e}", flush=True)
    print(f"[recycle-bulk {time.strftime('%H:%M:%S')}] DONE: {ok}/{n} proceeded", flush=True)
    try:
        os.remove(argv[0])
    except OSError:
        pass
    return 0


def _mute_bulk(scope, mute, verb, fs):
    """Bulk (un)mute a --scope. Mute governs child→parent completion delivery, so only CHILDREN in the
    scope are touched (non-children skipped); `mine` = your children. Reuses the shared scope predicate."""
    if scope not in fs.SCOPE_SETS:
        sys.exit(f"[fleet] {verb}: --scope must be one of {list(fs.SCOPE_SETS)}")
    caller = ""
    if scope == "mine":
        surface = os.environ.get("CMUX_SURFACE_ID", "")
        if not surface:
            sys.exit(f"[fleet] {verb} --scope mine needs $CMUX_SURFACE_ID (run inside a conductor)")
        caller = fs.label_for_surface(surface) or ""
    targets = [(l, v) for l, v in fs.scope_members(scope, caller, include_self=False)
               if v.get("kind") == "child"]
    if not targets:
        print(f"[fleet] {verb} --scope {scope}: no live children in scope")
        return 0
    for label, e in sorted(targets):
        if mute:
            e["muted"] = True
        else:
            e.pop("muted", None)
        fs.live_put(label, e)
        fs.log_event(verb + "d", label=label, via="scope")
    print(f"[fleet] {verb}d {len(targets)} child(ren) (scope {scope}): " + ", ".join(l for l, _ in sorted(targets)))
    return 0


def cmd_mute(argv, mute=True):
    """Mute/unmute a child's completion delivery. When muted, the router does NOT push the child's
    turn-completions to the parent's inbox (no inbox row, no `cmux notify`, no idle-wake); the parent
    reads that child ON DEMAND (`fleet ls` shows it MUTED with its session → `fleet child-digest`). Use when
    Berg drives a child directly (he is in the loop, so the conductor should not be spammed). The
    inverse of the notify-on-completion default. Mute is per-child runtime state on `fleet.json`.

      fleet mute <label>     fleet unmute <label>     fleet mute --scope mine   (all my children)
    """
    from . import state as fs
    verb = "mute" if mute else "unmute"
    scope, args = fs.pop_scope(argv, default=None)
    if scope is not None:                                     # bulk (un)mute a --scope (children only)
        return _mute_bulk(scope, mute, verb, fs)
    if not args:
        sys.exit(f"usage: fleet {verb} <label>  |  fleet {verb} --scope mine|children")
    label = args[0]
    e = fs.live_get(label)
    if not e:
        sys.exit(f"fleet {verb}: no live label '{label}'")
    if e.get("kind") != "child":
        print(f"[fleet] note: {label} is a {e.get('kind','?')}, not a child; mute only affects "
              f"child→parent completion delivery, so this has no effect on it")
    if mute:
        e["muted"] = True
    else:
        e.pop("muted", None)
    fs.live_put(label, e)
    fs.log_event(verb + "d", label=label)
    if mute:
        print(f"[fleet] {label} MUTED — completions suppressed; read on demand "
              f"(fleet ls → fleet child-digest {(e.get('session') or '').replace('claude-','')[:12]})")
    else:
        print(f"[fleet] {label} unmuted — completions deliver to its parent again")
    return 0


def cmd_broadcast(argv):
    """Notify live agents of an out-of-band change that does NOT auto-reach running agents (a toml/floor
    edit, a plugin bump, an ops heads-up). Delivers over the SAME input-safe path as peer-msg: a
    kind=peer inbox row per target (the awareness hook surfaces it into context, never the input box)
    plus an idle-wake. Informational by default (no reply expected). It NEVER restarts anything — each
    recipient decides what to do (e.g. `fleet recycle` to pick up the new toml). Self is always excluded.

      fleet broadcast "<msg>" --scope mine|all|conductors|children [--no-wake] [--expect-reply] [--dry-run]

    An ACT: `--scope` is REQUIRED (no fan-out default — you say who). `mine` = live children whose parent
    label == mine.
    """
    from . import state as fs; import secrets
    # --scope maps onto the internal target vocab (unchanged selection logic below).
    SCOPE_TO_TARGET = {"mine": "my-children", "all": "all", "conductors": "all-conductors", "children": "all-children"}
    scope = None
    no_wake = expect_reply = dry = False
    pos, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--scope":
            scope = argv[i + 1] if i + 1 < len(argv) else ""; i += 2
        elif a == "--no-wake":
            no_wake = True; i += 1
        elif a == "--expect-reply":
            expect_reply = True; i += 1
        elif a in ("--dry-run", "-n"):
            dry = True; i += 1
        else:
            pos.append(a); i += 1
    if not pos:
        sys.exit('usage: fleet broadcast "<msg>" --scope mine|all|conductors|children'
                 ' [--no-wake] [--expect-reply] [--dry-run]')
    body = " ".join(pos)

    if scope is None:                                          # an ACT: no fan-out default (mirror recycle bulk)
        sys.exit("fleet broadcast: --scope required (mine|all|conductors|children)")
    if scope not in SCOPE_TO_TARGET:
        sys.exit(f"fleet broadcast: --scope must be one of {list(SCOPE_TO_TARGET)}")
    target = SCOPE_TO_TARGET[scope]

    from_surface = os.environ.get("CMUX_SURFACE_ID", "")
    from_label = fs.label_for_surface(from_surface) or (from_surface[:8] if from_surface else "fleet")
    if target == "my-children" and not from_surface:
        sys.exit("fleet broadcast: --scope mine needs $CMUX_SURFACE_ID (run inside a conductor)")

    sel = []
    for label, v in fs.live_all().items():
        surf = v.get("surface")
        if not surf or surf == from_surface:                 # never broadcast to self / unbound
            continue
        kind = v.get("kind")
        if target == "all-conductors" and kind != "conductor":
            continue
        if target == "all-children" and kind != "child":
            continue
        if target == "my-children" and not (kind == "child" and v.get("parent") == from_label):
            continue
        sel.append((label, v))
    sel.sort()

    if not sel:
        print(f"[broadcast] no live targets for --scope {scope}")
        return 0
    if dry:
        print(f"[broadcast] (dry-run) from {from_label}, scope {scope} -> {len(sel)} agent(s):")
        for label, v in sel:
            print(f"  {label:<24}{v.get('kind','-'):<11}{(v.get('surface') or '')[:8]}")
        print(f"  body: {body}")
        return 0

    bid = secrets.token_hex(3)
    woke = []
    for label, v in sel:
        surf = v["surface"]
        fs.inbox_put("peer", surf, {
            "ptype": "broadcast", "to_label": label, "from_surface": from_surface,
            "from_label": from_label, "msg_id": bid, "reply_to": None,
            "reply_expected": expect_reply, "body": body,
        })
        if not no_wake and fs.idlewake_on() and fs.wake_if_idle(surf, "(broadcast-wake) a fleet broadcast is waiting in your context; handle it"):
            woke.append(label)                          # 'passive' mutes the wake fleet-wide; the inbox rows are still written
    fs.log_event("broadcast", **{"from": from_label, "scope": scope, "count": len(sel), "msg_id": bid})
    print(f"[broadcast] {from_label} -> {len(sel)} agent(s) (scope {scope}, msg {bid}, "
          f"reply: {'expected' if expect_reply else 'none'})")
    for label, v in sel:
        print(f"  {label:<24}{v.get('kind','-'):<11}{(v.get('surface') or '')[:8]}{'  (woke)' if label in woke else ''}")
    if no_wake:
        pass
    elif not fs.idlewake_on():
        print(f"  no wake (notify-mode passive); all {len(sel)} see it on their next turn")
    else:
        print(f"  woke {len(woke)} idle agent(s); the rest see it on their next turn")
    return 0


def cmd_profile(argv):
    """Emit a sourceable env block that pins EVERY cmux-fleet entrypoint at THIS build + a named profile,
    so independent builds run side by side with zero shared state. Usage:

        eval "$(/path/to/<build>/bin/fleet profile <name> [--base DIR] [--root DIR] [--init])"

    Pins (CLI, router, hooks, --plugin-dir, AND every child launch all resolve to ONE build):
      PATH                    <build>/bin first
      CMUX_STATE_DIR          <base>/state    (default $XDG_STATE_HOME/cmux-fleet-<name>)
      CMUX_FLEET_TOML         <base>/fleet.toml (default $XDG_CONFIG_HOME/cmux-fleet-<name>/fleet.toml)
      CMUX_FLEET_ROOT         --root or $HOME
      CMUX_FLEET_MARKETPLACE  the build's parent dir (so a roster plugins=["<build-name>"] -> this build)
      CMUX_BIN                the resolved cmux
    --init also creates the state dir and seeds the toml from fleet.toml.example if it's missing.
    The launcher injects these same paths into every child it spawns (see _profile_env), so a conductor
    and all its descendants stay on one build even if a child's shell carries different ambient env."""
    ap = argparse.ArgumentParser(prog="fleet profile")
    ap.add_argument("name", help="profile name; default paths derive cmux-fleet-<name>")
    ap.add_argument("--base", default="", help="one dir holding BOTH state/ and fleet.toml (overrides XDG defaults)")
    ap.add_argument("--root", default="", help="workspace root for relative role cwds (default $HOME)")
    ap.add_argument("--init", action="store_true", help="create the state dir + seed fleet.toml from the example")
    a = ap.parse_args(argv)

    if a.base:
        base = os.path.abspath(os.path.expanduser(a.base))
        state, toml = os.path.join(base, "state"), os.path.join(base, "fleet.toml")
    else:
        xdg_state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        state = os.path.join(xdg_state, f"cmux-fleet-{a.name}")
        toml = os.path.join(xdg_cfg, f"cmux-fleet-{a.name}", "fleet.toml")
    root = os.path.abspath(os.path.expanduser(a.root)) if a.root else os.path.expanduser("~")
    mkt = _marketplace_pin()                      # explicit config, or a real checkout's parent; "" -> omit
    binp = _fleet_bin_dir()                        # THIS build's fleet dir (checkout bin/ or installed script)

    if a.init:
        os.makedirs(state, exist_ok=True)
        os.makedirs(os.path.dirname(toml), exist_ok=True)
        if not os.path.exists(toml):
            seed = _seed_example_text()
            if seed is not None:
                with open(toml, "w") as f:
                    f.write(seed)
                sys.stderr.write(f"[fleet profile] seeded {toml} from fleet.toml.example\n")
            else:
                sys.stderr.write("[fleet profile] warning: no bundled fleet.toml.example found; roster not seeded\n")
        sys.stderr.write(f"[fleet profile] init: state dir {state}\n")

    build_label = PLUGIN_ROOT if _is_plugin_checkout() else (binp or "installed app")
    print(f'# cmux-fleet profile "{a.name}" -> build {build_label}  (eval this to activate)')
    print(f'export CMUX_FLEET_ROOT={shlex.quote(root)}')
    print(f'export CMUX_STATE_DIR={shlex.quote(state)}')
    print(f'export CMUX_FLEET_TOML={shlex.quote(toml)}')
    if mkt:
        print(f'export CMUX_FLEET_MARKETPLACE={shlex.quote(mkt)}')
    else:
        sys.stderr.write("[fleet profile] note: no plugin marketplace pinned (wheel install / no explicit "
                         "$CMUX_FLEET_MARKETPLACE); install the plugin separately and set it if you use plugins=[...]\n")
    print(f'export CMUX_BIN={shlex.quote(CMUX)}')
    if binp:
        print(f'export PATH={shlex.quote(binp)}:"$PATH"')
    else:
        sys.stderr.write("[fleet profile] warning: could not resolve THIS build's fleet bin dir; PATH not pinned "
                         "(set $CMUX_FLEET_BIN to the installed fleet path)\n")
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("usage: fleet <launch|config|ls|plugins|archive|revive|register|recycle|move|group|unstick|sessions|broadcast|mute|unmute|rm|vitals|usage|find|graph|serve|paint|worktree|profile|daemon|drive-child|peer-msg|child-digest|inbox|inbox-ack> ...\n"
              "  launch <role|--adhoc NAME> [--tool t] [--place p] [--parent s] [--effort L] [--model M] [--use NAME] [--provider NAME] [--dry-run] [-- <tool flags>]\n"
              "  config <role|--adhoc NAME|--cwd DIR> [--tool t]   effective config (base settings + fleet adds)\n"
              "  ls [--scope mine|all|conductors|children] [--json] live fleet x hook store; flags STALE + archived (default mine = you + your children; --scope all = the world)\n"
              "  plugins <add|reconcile|ls|show|describe> ...      the plugin INDEX: add-from-URL (safe: never enables) + reconcile + on-demand discovery\n"
              "  archive <label>                                   park a live agent (revivable)\n"
              "  revive <label> [--fresh] [--session id] [--place p] [--parent s] [--add-plugin N] [-- <flags>]\n"
              "                                                    bring a parked agent back (default RESUME last session; --fresh sheds; --session targets an arbitrary prior one)\n"
              "  register <label> [--surface UUID] [--parent s] [--session id]\n"
              "                                                    pull a LIVE-but-unregistered agent into the registry (recovery for a skipped auto-register)\n"
              "  move <label> (--to-workspace WS | --own-workspace) [--name TITLE]\n"
              "                                                    relocate a LIVE child to another workspace atomically (same surface/session; no archive, no recycle); --own-workspace joins the conductor's group\n"
              "  group <init [--name N] | add <label> [--name N]>  make THIS conductor's workspace a named group (init) or retrofit a live child into it (add); membership ops keep agents live (the safe lane)\n"
              "  recycle [label] [--fresh] [--session id] [--effort L] [--model M] [--force] [--add-plugin N] [--use NAME] [--prime T|--no-prime] [-- <flags>]\n"
              "                                                    restart in place, same surface/identity (default self+RESUME; --fresh sheds; --use = index-aware plugin add, reaches linked + enabled)\n"
              "  recycle --scope mine|all|conductors|children [--include-muted] [--dry-run]\n"
              "                                                    BULK restart (sequential + gated, skips self + muted); mine = your children; cross-conductor = the safe topology\n"
              "  unstick [label] [--surface UUID] [--dry-run]      reap a frozen dead-pid hook-store ghost (SessionEnd-less death) so ls/recycle/doctor stop trusting a dead 'running'; never touches a LIVE record\n"
              "  sessions <label> [--all] [--json]                 list resumable prior sessions for the agent's surface (id, age, size, snippet)\n"
              "  broadcast \"<msg>\" --scope mine|all|conductors|children [--no-wake] [--expect-reply] [--dry-run]\n"
              "                                                    input-safe heads-up to live agents (e.g. after a toml/floor change); never restarts them; --scope REQUIRED (an act)\n"
              "  mute <label> | unmute <label> [| --scope mine]    stop/resume pushing a child's completions to its parent (parent reads on demand); --scope mine = all my children\n"
              "  rm <label> [--detach] [--force] [--kill] [--wip-commit] [--with-group]\n"
              "                                                    close + archive a label (revivable; refuses mid-turn, --force overrides); --detach drops the row only; --kill adds worktree teardown; --with-group dissolves its workspace-group\n"
              "  vitals [--scope mine|all|conductors|children] [--json] [--paint] [--watch [--interval N]] cheapest-first triage table + ctx-remaining % (default mine)\n"
              "  usage [--json]                                    per-provider subscription windows (5h + weekly bars, reset countdowns, metered/Fable flags, live attribution) from the daemon poller\n"
              "  find <query> [--turns N] [--json]                 content-aware session lookup (label/role/cwd or transcript)\n"
              "  graph [--scope mine|all|<label>] [--json] [--html] [--out FILE]  fleet parentage tree (text/JSON/HTML); default mine = your subtree; --scope all = full tree\n"
              "  serve [--port N]                                  thin read-only localhost view (graph HTML + vitals.json); no daemon\n"
              "  paint                                             sync fleet state onto the cmux sidebar (status pills + ctx bars)\n"
              "  worktree <ls | clean <label> [--wip-commit]>      manage fleet-owned git worktrees (config-gated, default-off)\n"
              "  profile <name> [--base DIR] [--root DIR] [--init]  emit env that pins ALL entrypoints at THIS build (eval it for multi-build isolation)\n"
              "  daemon <start|stop|status|restart> [--foreground] [--heartbeat [SECS]]  run the router as a detached daemon (survives shell exit + recycle); start --foreground for launchd\n"
              "  drive-child <surface-uuid> <prompt...>            submit a prompt to a child's TUI (beats the paste-settle enter-race)\n"
              "  peer-msg <to-label> \"<body>\" [--no-reply] [--reply-to <id>] [--expect-reply] [--no-wake]\n"
              "                                                    input-safe A2A: message a live PEER conductor (into its context, never its input box)\n"
              "  child-digest <session-frag> [N]                   print a child's last N transcript turns (the reliable content source)\n"
              "  inbox [--scope mine|<label>|all|conductors|children] [--json]  pending inbox on demand (default mine = yours; <label> peeks one; all = triage) — the catch-up read after a recycle\n"
              "  inbox-ack <seq> [--peer|--stale|--doctor] [--surface UUID]  mark shown completions/alerts/peer msgs handled so they stop re-surfacing")
        return 0
    sub, rest = sys.argv[1], sys.argv[2:]
    # Hook verbs are the per-turn hot path (a plugin shim shells into them on every UserPromptSubmit/Stop).
    # Dispatch them FIRST, before the heavier feature/daemon/helper imports, to keep that path lean.
    if sub in ("hook-awareness", "hook-drain"):
        from . import hookverbs as hv
        return (hv.cmd_hook_awareness if sub == "hook-awareness" else hv.cmd_hook_drain)(rest)
    from . import features as ff
    from . import daemon as fd
    from . import helpers as fh
    fns = {"launch": cmd_launch, "config": cmd_config, "ls": cmd_ls, "plugins": cmd_plugins,
           "archive": cmd_archive, "revive": cmd_revive, "register": cmd_register, "recycle": cmd_recycle,
           "move": cmd_move, "group": cmd_group,
           "unstick": cmd_unstick, "sessions": cmd_sessions,
           "_recycle-exec": cmd_recycle_exec, "_recycle-bulk-exec": cmd_recycle_bulk_exec,
           "broadcast": cmd_broadcast,
           "mute": lambda a: cmd_mute(a, mute=True), "unmute": lambda a: cmd_mute(a, mute=False),
           "rm": cmd_rm, "worktree": cmd_worktree, "profile": cmd_profile, "daemon": fd.cmd_daemon,
           "vitals": ff.cmd_vitals, "usage": ff.cmd_usage, "find": ff.cmd_find, "graph": ff.cmd_graph,
           "serve": ff.cmd_serve, "paint": ff.cmd_paint,
           "drive-child": fh.cmd_drive_child, "peer-msg": fh.cmd_peer_msg,
           "child-digest": fh.cmd_child_digest, "inbox": fh.cmd_inbox, "inbox-ack": fh.cmd_inbox_ack}
    if sub in fns:
        return fns[sub](rest)
    sys.exit(f"fleet: unknown subcommand '{sub}'")


if __name__ == "__main__":
    raise SystemExit(main())
