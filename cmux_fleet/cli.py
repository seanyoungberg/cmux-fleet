#!/usr/bin/env python3
# cmux_fleet/cli.py (was scripts/fleet.py) - the native-cmux fleet CLI. ONE tool, tool-agnostic. The `fleet` namespace is the
# umbrella for the rest of the scripts (state/drive/digest/ack).
#
#   fleet launch <role> [launcher flags] [-- <verbatim tool flags>]
#   fleet launch --adhoc <name> --tool claude [-- --model opus]   # alias: the `adhoc` role, label=<name>
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
import argparse, json, os, re, shlex, subprocess, sys, tempfile, time

from .config import ROOT, STATE, CMUX, FLOOR, FLEET_TOML, PLUGIN_INDEX, HOOKSTORE, HOOKSTORE_EXPLICIT, load_plugin_index  # path resolver

# The checkout/build root: the dir that holds bin/, .claude-plugin/, fleet.toml.example next to the
# cmux_fleet package. In a repo/editable install this is the repo root (unchanged from the flat layout,
# where it was dirname(dirname(scripts/fleet.py))). In a WHEEL/venv install it is site-packages — which
# holds NONE of bin/, .claude-plugin/, or a repo-root fleet.toml.example — so `fleet profile` must NOT
# derive its pins from it there (see _fleet_bin_dir / _seed_example_text below, and the codex P1.1 fix).
# PLUGIN_ROOT stays only as the checkout-detection anchor + editable-install seed
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
    e = {"CMUX_STATE_DIR": STATE, "CMUX_FLEET_TOML": FLEET_TOML, "CMUX_FLEET_ROOT": ROOT, "CMUX_BIN": CMUX,
         # pin the plugin INDEX too: a child resolves its marketplaces from plugins.toml, so pinning the
         # index location is what keeps its plugin loadout on THIS build's marketplaces (the marketplace
         # dirs themselves are absolute paths declared IN the index) — the successor to the old
         # $CMUX_FLEET_MARKETPLACE pin now that marketplaces live in the index, not an env var.
         "CMUX_FLEET_PLUGIN_INDEX": PLUGIN_INDEX}
    # Hook-store isolation (BOTH sides, so a nested sub-fleet is isolated too). When an operator pinned a
    # private hookstore, propagate it to every child:
    #   - CMUX_AGENT_HOOK_STATE_DIR (cmux-owned — its hook CLI's WRITE var, NOT ours to rename): the child's
    #     cmux hooks WRITE session records to the private dir instead of prod's ~/.cmuxterm.
    #   - CMUX_HOOKSTORE_DIR (fleet's own READ var): a child that itself runs `fleet` (a sub-conductor) READS
    #     the SAME private dir — without this it would fall back to ~/.cmuxterm and SEE prod's liveness.
    # Same resolved dir for both => read-side and write-side share one knob and cannot drift. Gated on the
    # explicit-pin bit, so a default (prod) launch injects NEITHER and keeps its own default — zero blast radius.
    if HOOKSTORE_EXPLICIT:
        e["CMUX_AGENT_HOOK_STATE_DIR"] = HOOKSTORE
        e["CMUX_HOOKSTORE_DIR"] = HOOKSTORE
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

    # `--adhoc NAME` is an ALIAS for the rostered `adhoc` role with label=NAME (Ship 5d): one shared flat
    # home ([role.adhoc].cwd), the name is the LABEL not a per-name directory. So an ad-hoc agent resolves
    # off the real roster block exactly like any role — same machinery — and only its label differs.
    if adhoc_name:
        if "adhoc" not in roles:
            sys.exit(f"fleet: --adhoc needs a [role.adhoc] block in {FLEET_TOML} (the scratch role's shared home)")
        role = "adhoc"
    if role not in roles:
        sys.exit(f"fleet: role '{role}' not in {FLEET_TOML}")
    rblock = roles[role]
    orch_scalars = {k: v for k, v in rblock.items() if not isinstance(v, dict)}
    label = adhoc_name or role

    tool = tool_override or orch_scalars.get("tool") or defaults.get("tool") or "claude"
    if tool not in rblock and not (tools.get(tool)):
        # role exists but neither it nor a [tool.<t>] floor defines this tool
        sys.exit(f"fleet: role '{role}' has no config for tool '{tool}' (no [role.{role}.{tool}] or [tool.{tool}])")

    tdef = tools.get(tool, {}) or {}                          # [tool.<t>] floor
    rtool = (rblock.get(tool) if isinstance(rblock.get(tool), dict) else {}) or {}  # [role.<name>.<t>]

    # orchestration: [defaults] (drop tool key) <- role scalars
    orch = {k: v for k, v in defaults.items() if k != "tool" and not isinstance(v, dict)}
    orch.update(orch_scalars)
    orch.pop("tool", None)

    # launch channels
    # `plugins` = the mechanism-agnostic plugin list, resolved through the INDEX (plugins.toml) at compile
    # time. Unioned floor ∪ role, deduped. The index says linked (--plugin-dir) vs enabled (enabledPlugins)
    # so a role author never states the mechanism; a name NOT in the index falls back to a linked
    # --plugin-dir (default marketplace / an absolute path). See _resolve_plugins / adapter_compile.
    plugins = _dedup((tdef.get("plugins") or []) + (rtool.get("plugins") or []))
    flags = _layer_tokens([shlex.split(tdef.get("flags", "")),           # tool-floor <- role
                           shlex.split(rtool.get("flags", ""))])
    env = {**(tdef.get("env") or {}), **(rtool.get("env") or {})}
    settings = rtool.get("settings") or tdef.get("settings") or ""
    # setting_sources -> --setting-sources (which settings layers claude loads; excluding 'project' keeps
    # our launches from reading the agent's own .claude/, unlike an ad-hoc launch).
    setting_sources = rtool.get("setting_sources") or tdef.get("setting_sources") or ""

    return {
        "tool": tool, "role": role, "label": label,   # role = behavioral type; label defaults to it (adhoc: role='adhoc', label=NAME)
        "kind": orch.get("kind", "child"),
        "place": orch.get("place", "tab"),
        "group": orch.get("group", ""),
        "cwd": orch.get("cwd", ""),
        "plugins": plugins, "flags": flags, "env": env, "settings": settings,
        "setting_sources": setting_sources,
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
    """Flatten a repeatable + comma-sep flag into a clean name list — `--plugin a,b --plugin c` (append)
    and `--plugin a,b` (comma) both land as ['a','b','c'], so the one `--plugin` flag reads either way."""
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
def _claude_settings_args(spec, enabled_refs=()):
    """`--settings` args for claude: the role's `settings` (a file path or inline JSON) plus an
    enabledPlugins object from `enabled_refs` — the index-resolved `plugins` entries of type=enabled, as
    "<plugin>@<marketplace>" refs. enabledPlugins format is {"<plugin>@<marketplace>": true} (the same
    shape claude writes in settings.json). We emit ONE --settings when we can (role settings is inline
    JSON or absent -> fold them together); only when the role pins a settings FILE *and* also enables
    plugins do we emit two --settings, which is safe because the cmux-claude-wrapper deep-merges multiple
    --settings (and its own hooks) into a single one before claude ever sees them (verified in
    Resources/bin/cmux-claude-wrapper). The JSON must be valid or the wrapper warns + drops it."""
    base = (spec.get("settings") or "").strip()
    ep = {name: True for name in _dedup(list(enabled_refs))}
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


def _linked_dir(name, source, index):
    """Resolve a `type=linked` plugin name to a --plugin-dir path (or None to warn+skip). An absolute/~
    path is used as-is (if it exists); else the plugin is joined under its `[marketplace.<source>].path`.
    A name with no resolvable marketplace (unknown/absent/global source, and not an absolute path) returns
    None — the caller warns + skips. There is NO implicit default marketplace: a linked plugin resolves
    ONLY via its declared marketplace or an absolute path."""
    expanded = os.path.expanduser(name)
    if os.path.isabs(expanded):                              # abs/~ bypasses the marketplace
        return expanded if os.path.exists(expanded) else None
    mk = index["marketplaces"].get(source) if source else None
    if mk and mk.get("path"):
        pd = os.path.join(mk["path"], name)
        return pd if os.path.exists(pd) else None
    return None


def _resolve_plugins(plugin_names, index):
    """Resolve the unioned `plugins` list through the index into the two native channels (design §3).
    Returns (linked_dirs, enabled_refs, unresolved):
      - in index & type=enabled -> "<name>@<source>" accumulated for enabledPlugins (via --settings).
      - in index & type=linked  -> a --plugin-dir path via the entry's source marketplace.
      - NOT in index            -> a linked --plugin-dir IF it's an abs/~ path (used as-is); a bare name
                                   with no index entry has no marketplace to resolve under, so it is
                                   UNRESOLVED (there is no implicit default marketplace).
    A name that should resolve to a dir but doesn't (missing marketplace/dir/abs) lands in `unresolved`
    (caller warns + skips)."""
    linked, enabled, unresolved = [], [], []
    for name in plugin_names:
        entry = index["plugins"].get(name)
        if entry and entry.get("type") == "enabled":
            src = (entry.get("source") or "").strip()
            enabled.append(f"{name}@{src}" if src else name)
            continue
        source = entry.get("source") if entry else ""        # linked (indexed) or unindexed fall-through
        pd = _linked_dir(name, source, index)
        (linked if pd else unresolved).append(pd if pd else name)
    return linked, enabled, unresolved


# claude->codex launch-flag translation (P0-3). The fleet's first-class launch flags (--effort/--model)
# funnel into the caller-token layer in CLAUDE syntax regardless of tool (cmd_launch), and a caller's `--`
# passthrough is often copy-pasted from a claude launch. Forwarding those verbatim to codex can KILL it:
# codex aborts on `--effort` ("unexpected argument '--effort' found", 2026-07-07). The codex adapter is
# the ONE place that owns the mapping (the adapter boundary): TRANSLATE what has a codex equivalent,
# DROP+warn what's claude-only, PASS the rest (incl. --model, a real codex flag) through untouched.
_CODEX_DROP = ("--setting-sources", "--permission-mode", "--plugin-dir")  # claude-only; no codex analog


def _codex_flags(tokens):
    """Map claude-flavored launcher tokens to codex's CLI (P0-3). Value-consuming flags use the repo's
    heuristic (the next token is the value iff it doesn't start with '-'), matching _drop_keys.
      --effort <lvl>                 -> -c model_reasoning_effort=<lvl>   (codex reads effort via -c; the
                                        LEVEL passes through verbatim -- codex's tiers overlap the fleet's,
                                        incl. xhigh (its own TUI shows `gpt-5.5 xhigh`), so we do NOT clamp
                                        and silently downgrade; a value codex rejects now fails LOUD via the
                                        P0-4a launch verify, not silently)
      --dangerously-skip-permissions -> --dangerously-bypass-approvals-and-sandbox
      --setting-sources|--permission-mode|--plugin-dir -> DROP (+warn): codex rejects them
      everything else                -> passthrough (a codex floor's own flags, --model, `--` extras)"""
    out, i, n = [], 0, len(tokens)
    while i < n:
        t = tokens[i]
        key, eq, inline = t.partition("=")
        has_next_val = (not eq) and i + 1 < n and not tokens[i + 1].startswith("-")
        val = inline if eq else (tokens[i + 1] if has_next_val else "")
        if key == "--effort":
            if val:
                out += ["-c", f"model_reasoning_effort={val}"]
            i += 2 if has_next_val else 1
        elif key == "--dangerously-skip-permissions":
            out.append("--dangerously-bypass-approvals-and-sandbox")
            i += 1
        elif key in _CODEX_DROP:
            print(f"[fleet] warn: dropping claude-only flag '{key}"
                  + (f" {val}" if (val and not eq) else "") + "' for codex (no codex equivalent)")
            i += 2 if has_next_val else 1
        else:
            out.append(t)
            i += 1
    return out


def _codex_config_mcp_servers(config_text):
    """The top-level `[mcp_servers.<name>]` server names declared in a codex config.toml. `[^\\].]+` stops
    at the first `.` or `]`, so a subtable like `[mcp_servers.node_repl.env]` still yields just `node_repl`
    (deduped); server names with hyphens (gemini-cli, basic-memory) come through whole."""
    return sorted(set(re.findall(r'^\s*\[mcp_servers\.([^\].]+)', config_text, re.M)))


CODEX_DEFAULT_HOME = "~/.codex"


def _codex_clean_config_flags(home=CODEX_DEFAULT_HOME):
    """Launch flags that keep a codex worker off the DESKTOP app's cruft — enumerated from the home the
    launch will ACTUALLY USE.

    THE BUG THIS SHAPE FIXES (2026-07-12, found only by launching a REAL AGENT): this used to always
    enumerate `~/.codex/config.toml` (Berg's desktop, 6 MCP servers) and emit a
    `-c mcp_servers.<n>.enabled=false` for each — but the flags were then applied to a SEAT's home, whose
    config declares ZERO servers. Setting `enabled=false` on a server that does not exist there CREATES
    `[mcp_servers.<n>]` with no transport, and codex then refuses to load its config at all:
        Error loading config.toml: invalid transport in `mcp_servers.basic-memory`
    The agent never started. `codex exec` never caught it because the fleet agent path is a different one.

    And the deeper point, which makes this a DELETION rather than a patch: **a per-seat home is already
    clean.** The whole cruft problem existed BECAUSE workers shared Berg's desktop `~/.codex`. A fresh seat
    home has no desktop MCP servers and no desktop plugins, so the correct number of `-c mcp_servers.*` flags
    for it is ZERO — and enumerating from the real home yields exactly that, with no special case.

      - `--disable plugins` — one feature flag, harmless in a clean home, and it still strips the desktop's
        13 plugins when a worker does run in `~/.codex`. Leaves `features.hooks` alone (fleet hooks still fire).
      - `-c mcp_servers.<n>.enabled=false` — ONLY for servers this home actually declares. None declared
        (a seat home, or no config at all) -> none emitted."""
    flags = ["--disable", "plugins"]
    try:
        text = open(os.path.join(os.path.expanduser(home), "config.toml")).read()
    except OSError:
        return flags                                  # no config in this home (a fresh seat) -> nothing to strip
    for name in _codex_config_mcp_servers(text):
        flags += ["-c", f"mcp_servers.{name}.enabled=false"]
    return flags


def adapter_compile(tool, spec, caller_tokens, codex_home=None):
    """Compile {plugins, flags, env, settings} + caller passthrough -> (bin, arg_tokens, env_map)
    for the given tool. Adding a tool = adding a branch here + a [tool.<t>] block.

    `codex_home` is the home the codex launch will ACTUALLY run in (from the resolved provider, when the
    optional multi-seat feature is in use). It MUST be threaded in, because the codex cruft-stripping flags
    are enumerated FROM that home's config — computing them against the default home and applying them to a
    seat home is what broke the agent launch (see _codex_clean_config_flags). None = codex's own default
    home, which is exactly right for a user who has configured no providers at all.

    IDENTITY (Ship 5d): the launcher injects ONLY AGENT_ROLE + AGENT_LABEL, the irreducible launch
    identity. Everything else DERIVES from source at use-time (the Ship 5 thesis, applied to identity):
    kind from AGENT_ROLE + the roster (loom:prime resolves it), and the parent conductor from the registry
    `parent` field (routing already reads it; `fleet peer-msg --to-parent` addresses it). No AGENT_CONDUCTOR
    env — a captured binding's env goes stale across a recycle; the registry never does."""
    # spec['flags'] is already a token list (tool-floor<-role); layer the caller passthrough on top.
    merged = _layer_tokens([spec["flags"], list(caller_tokens or [])])
    env = {**_profile_env(), **dict(spec["env"])}            # build/profile pins first; a role's env wins
    env["AGENT_ROLE"] = spec["role"]                          # behavioral type (exposed to the agent)
    env["AGENT_LABEL"] = spec["label"]                        # unique instance -> routing/recycle

    if tool == "claude":
        args = []
        if spec.get("setting_sources"):                      # which settings layers claude loads
            args += ["--setting-sources", spec["setting_sources"]]
        # `plugins` -> the two native channels via the index (design §3): each name resolves to a linked
        # --plugin-dir or an enabled enabledPlugins ref. No plugins -> both lists empty -> a no-plugin launch.
        linked, enabled, unresolved = _resolve_plugins(spec["plugins"], load_plugin_index())
        for name in unresolved:
            print(f"[fleet] warn: plugin '{name}' not resolvable (marketplace unset or not found); skipping")
        seen = set()
        for pd in linked:                                     # linked plugins, deduped by path
            if pd not in seen:
                args += ["--plugin-dir", pd]; seen.add(pd)
        args += merged
        args += _claude_settings_args(spec, enabled)         # role `settings` + enabled-type enabledPlugins
        return "claude", args, env

    if tool == "codex":
        # Stub: codex has its own plugin/settings vocabulary; flags+env passthrough work today. The index
        # can EXPRESS codex plugins (tools=["codex"] + [plugin.<n>.codex] blocks) but this phase does NOT
        # provision codex plugins -> plugins/settings are still no-ops for codex, so warn.
        if spec["plugins"] or spec["settings"]:
            print("[fleet] warn: 'plugins'/'settings' are not yet provisioned for codex; ignored")
        # Cruft-stripping is enumerated from the home this launch will ACTUALLY use — a seat home is already
        # clean and yields ZERO mcp flags. Computing them against the default home and applying them to a
        # seat home CREATES transport-less server tables and codex refuses to start (the agent-path bug).
        return "codex", _codex_clean_config_flags(codex_home or CODEX_DEFAULT_HOME) + _codex_flags(merged), env

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


def _all_workspace_uuids(tree_text):
    """Every workspace UUID present in `cmux tree` TEXT (pure). Used to snapshot the workspace set
    before/after a `workspace-group create` so `group init` can spot the empty scaffolding anchor cmux
    spawns and close it (the 2026-07-02 empty-anchor footgun)."""
    import re
    UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    return {m.group(1) for m in re.finditer(r"workspace\s+workspace:\d+\s+(" + UUID + ")", tree_text or "")}


# --- #6 bare-shell surface reaper: the husk safety gate (pure, testable) ---------------------------
_HUSK_UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
# The FLEET-specific launch fingerprint. A human's shell NEVER contains these env assignments; they are
# emitted only by `fleet launch`/recycle (render_send_cmd). This env prefix is the core discriminator
# that makes closing a matched surface safe — it is why a human's manual shell can never be a candidate.
_FLEET_LAUNCH_SIG = re.compile(r"AGENT_ROLE=|AGENT_LABEL=|CMUX_FLEET_(?:STATE_DIR|TOML|ROOT|MARKETPLACE)")
_HUSK_RESUME = re.compile(r"claude --resume\s+(" + _HUSK_UUID + r")")
_HUSK_LABEL = re.compile(r"AGENT_LABEL=([A-Za-z0-9._-]+)")
# A live claude/codex TUI paints one of these; a bare login-shell husk never does. SECONDARY guard —
# pid/hook liveness (surface_has_live_agent, the union of both tool stores) is the authority.
# `Context \d+% left` is codex's status line: every other alternative here is a claude-ism, so this
# regex matched NOTHING on a live codex pane (measured, codex 0.144.1, 2026-07-10) and the backstop was
# claude-only. The pid check is what kept the live codex surface out of the husk bucket, alone.
_HUSK_LIVE_TUI = re.compile(r"Context Remaining|bypass permissions on \(shift|esc to interrupt\)|"
                            r"⏵⏵ bypass|Auto-accept edits|for shortcuts|\btokens \(\d|"
                            r"Context \d+% left", re.I)
_HUSK_PROMPT = re.compile(r"^.*?@\S+\s+\S.*?[%$]\s?(.*)$|^\s*[❯➜\$%]\s?(.*)$")


def _prompt_typed_text(line):
    """If `line` is a shell prompt, return the text typed AFTER the prompt char ('' for a bare idle
    prompt); None if the line is not a prompt at all. The tail-guard uses this to detect human activity
    (any typed command) after a fleet launch artifact."""
    m = _HUSK_PROMPT.match(line.strip())
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip()


def _pane_shows_live_tui(pane_text):
    """A painted claude/codex TUI (defensive backstop to the pid liveness check)."""
    return bool(_HUSK_LIVE_TUI.search(pane_text or ""))


def _husk_evidence(pane_text):
    """PURE classifier for a NON-live surface's captured pane. Returns
      {'husk': True,  'label': <AGENT_LABEL>, 'resume_id': <claude --resume id>, 'reason': ...}  or
      {'husk': False, 'reason': <why not>}.
    A surface is a husk ONLY IF it carries the FLEET launch signature AND that artifact is the pane's
    TAIL — after the last signature line there is NOTHING but blank lines, claude's 'Resume this session
    with:' exit hint, or a BARE shell prompt. Any human-typed command after the artifact (the 0A3A252A
    case: a manual `cd` after a replayed launch line) FAILS the guard -> not a husk. This is the safety
    gate: it never fingerprints a human's shell (no fleet env prefix) nor a shell a human has since used
    (a typed command after the artifact)."""
    lines = list((pane_text or "").splitlines())
    ne = [(i, l) for i, l in enumerate(lines) if l.strip()]
    if not ne:
        return {"husk": False, "reason": "empty pane"}
    sig_idx = None
    for i, l in ne:
        if _FLEET_LAUNCH_SIG.search(l):
            sig_idx = i                                   # the LAST fleet-signature line
    if sig_idx is None:
        return {"husk": False, "reason": "no fleet launch signature (human shell)"}
    for i, l in ne:
        if i <= sig_idx:
            continue
        s = l.strip()
        if s.lower().startswith("resume this session with") or _HUSK_RESUME.search(s):
            continue                                      # claude's exit hint -> allowed after the artifact
        typed = _prompt_typed_text(s)
        if typed and not _FLEET_LAUNCH_SIG.search(typed) and not _HUSK_RESUME.search(typed):
            return {"husk": False, "reason": f"human activity after launch artifact: {typed[:48]!r}"}
        # a bare prompt (typed == '') or a launch-command continuation line -> allowed
    lab = _HUSK_LABEL.search(pane_text)
    rid = _HUSK_RESUME.search(pane_text)
    return {"husk": True, "label": lab.group(1) if lab else "",
            "resume_id": rid.group(1) if rid else "",
            "reason": "fleet launch artifact is the tail; bare shell; no live agent"}


def _iter_tree_surfaces(tree_text):
    """Yield (surface_uuid, workspace_uuid, kind, title) for EVERY surface in `cmux tree` TEXT (pure),
    whatever its kind — `[terminal]`, `[browser]`, `[markdown]`. Tracks the most-recent workspace line,
    like surface_ws_from_tree. The husk reaper wants terminals only (_iter_terminal_surfaces); a
    workspace close takes every kind with it, so its collateral survey must see them all."""
    ws = ""
    for line in (tree_text or "").splitlines():
        mw = re.search(r"workspace\s+workspace:\d+\s+(" + _HUSK_UUID + ")", line)
        if mw:
            ws = mw.group(1)
            continue
        ms = re.search(r"surface\s+surface:\d+\s+(" + _HUSK_UUID + r")\s+\[(\w+)\]\s+\"([^\"]*)\"", line)
        if ms:
            yield ms.group(1), ws, ms.group(2), ms.group(3)


def _iter_terminal_surfaces(tree_text):
    """Yield (surface_uuid, workspace_uuid, title) for every TERMINAL surface in `cmux tree` TEXT (pure)."""
    for surf, ws, kind, title in _iter_tree_surfaces(tree_text):
        if kind == "terminal":
            yield surf, ws, title


def surface_ws_map_from_tree(tree_text):
    """{SURFACE_UUID_UPPER: workspace_uuid} from `cmux tree` TEXT (pure). The tree-derived topology
    resolve.place_of/workspace_surfaces want, built from a tree text the CALLER already read — so a
    teardown decision reads the tree ONCE and every question it asks is answered off that one snapshot
    (no torn view between 'where does my surface live' and 'who else lives there')."""
    return {surf.upper(): ws for surf, ws, _kind, _title in _iter_tree_surfaces(tree_text) if ws}


def _ref_to_uuid(kind, ref, tree_text=None):
    import re
    txt = cmuxq("tree", "--all", "--id-format", "both") if tree_text is None else tree_text
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


def _group_anchor_workspace(gref):
    """The anchor workspace UUID of a group ref, or '' if unknown. Under Model B (empty-anchor, ratified
    2026-07-10) a group's anchor is an AGENTLESS scaffold workspace that cmux ALSO reports inside
    `member_workspace_refs` -- so the `--with-group` membership cross-check must SUBTRACT it before
    comparing cmux's real membership against the registry (which can never know the scaffold: no agent
    lives on it, so no registry row occupies it). Without this every Model-B group reads as a registry
    mismatch and refuses to dissolve (the anchor-flip regression, live-caught 2026-07-10). `workspace-group
    list --json` reports the anchor as a short ref (`anchor_workspace_ref`: "workspace:88"); resolve it
    through the same _ref_to_uuid `cmux tree` lookup used for members. Returns '' when the group data can't
    be read, the ref isn't listed, or the group has no anchor ref (Model A / contract drift) -- the caller
    then compares against the full real membership unchanged (fail-closed, never masking a real mismatch)."""
    try:
        gd = json.loads(cmuxq("workspace-group", "list", "--json"))
    except Exception:
        return ""
    g = next((x for x in (gd.get("groups") or []) if x.get("ref") == gref), None)
    if g is None:
        return ""
    ref = g.get("anchor_workspace_ref") or ""
    return _ref_to_uuid("workspace", ref) if ref else ""


def _group_of_workspace(ws, tree_text):
    """The workspace-group that CONTAINS workspace `ws`, as (gref, anchor_ws_uuid, {member_ws_uuids}),
    or None when `ws` is ungrouped / the group data can't be read. cmux reports group membership and the
    anchor as short refs (`workspace:11`), so each is resolved through the caller's ONE tree snapshot.

    The anchor matters to teardown: per `cmux workspace-group --help`, "the group header IS the anchor's
    sidebar representation. Closing the anchor dissolves the group while preserving its other members as
    ungrouped workspaces." So closing a conductor's workspace silently scatters its children out of the
    sidebar group — collateral the fleet must re-anchor away from, not discover afterwards."""
    try:
        gd = json.loads(cmuxq("workspace-group", "list", "--json"))
    except Exception:
        return None
    target = (ws or "").upper()
    if not target:
        return None
    for g in gd.get("groups") or []:
        members = {u for u in (_ref_to_uuid("workspace", r, tree_text)
                               for r in (g.get("member_workspace_refs") or [])) if u}
        if target in {m.upper() for m in members}:
            return g.get("ref", ""), _ref_to_uuid("workspace", g.get("anchor_workspace_ref") or "",
                                                  tree_text), members
    return None


def _group_name(gref):
    """The display NAME of a group ref (e.g. 'Conductor - berg-sandbox'), or '' if unknown. Used to
    title a replacement anchor scaffold so the sidebar header keeps reading the same label."""
    try:
        gd = json.loads(cmuxq("workspace-group", "list", "--json"))
    except Exception:
        return ""
    return next((g.get("name", "") for g in (gd.get("groups") or []) if g.get("ref") == gref), "")


def _title_group_anchor_scaffold(gref, member_ws, conductor_label):
    """Model B (empty-anchor, ratified 2026-07-10): a workspace-group's anchor is an EMPTY scaffold
    workspace titled 'Conductor - <label>' with no agent on it, and the conductor runs as an ordinary
    MEMBER in its own '<label>' workspace. cmux's `workspace-group create --name N --from <ref>` ALWAYS
    mints exactly such a scaffold and anchors the group on it, adopting <ref> as a member. THE CONTRACT,
    live-measured on cmux 0.64.17 (2026-07-10, reproduced twice): `workspace-group create --name N --from
    <ref>` does NOT anchor on <ref> -- it builds a NEW workspace named N with a bare login shell, anchors
    the group on THAT, and adopts <ref> as an ordinary member. So bootstrap KEEPS that scaffold as the
    header and just titles it; it does NOT re-anchor onto the conductor and close the scaffold (Model A,
    reversed — an anchored conductor renders as a bare folder shim and has its workspace title forced to
    the group name, which fleet.swift then shows as the label).

    Returns (anchor_uuid, notes). `member_ws` is the conductor's OWN workspace UUID and is never retitled
    here. A defensive branch covers the contract-breaking case where create anchored on the member
    itself (no scaffold minted): the anchor is left untitled rather than mislabel a live member."""
    tree = cmuxq("tree", "--all", "--id-format", "both")
    info = _group_of_workspace(member_ws, tree)
    if not info:
        return "", [f"warn: {conductor_label}'s workspace {(member_ws or '')[:8]} is not in group {gref} "
                    f"after create; anchor NOT titled. Inspect: cmux workspace-group list --json"]
    _, anchor, _ = info
    title = f"Conductor - {conductor_label}"
    if anchor and anchor.upper() != (member_ws or "").upper():
        cmuxq("rename-workspace", "--workspace", anchor, "--", title)
        return anchor, [f"anchored group {gref} on empty scaffold {anchor[:8]} titled '{title}'; "
                        f"{conductor_label} joined as a member"]
    if anchor:                                             # create anchored on the member (contract drift)
        return anchor, [f"warn: group {gref} anchored on {conductor_label}'s OWN workspace ({anchor[:8]}) "
                        f"instead of a fresh scaffold as measured; anchor NOT retitled (that would mislabel "
                        f"a live member). Inspect: cmux workspace-group list --json"]
    return "", [f"warn: group {gref} has no resolvable anchor workspace to title"]


def create_surface(spec, parent_surf, direction):
    """Create the target surface per spec['place']; return (ws_uuid, surf_uuid). Aborts (None) on any
    unresolved UUID -- never send blind."""
    import re
    from . import resolve as rs
    place = spec["place"]
    if place in ("tab", "pane"):
        # WHERE THE PARENT SURFACE LIVES = a question about the TREE, which is the visual ground truth,
        # NOT about the hook store. This used to ask ws_uuid_for_surface (the STORE) for the workspace
        # while asking surface_loc (the TREE) for the pane — a split that made the answer depend on the
        # parent having a live hook-store record at all. It does not always: a bare shell surface has no
        # record ever, and a DARK agent's surface has none either (the app drops its writes). Measured on
        # this box 2026-07-12 — `move-refuse` sat plainly in `cmux tree`, and every `launch --place tab`
        # off it died with "cannot resolve conductor workspace from --parent". One tree read, both answers.
        cws, agents_pane = surface_loc(parent_surf)
        if not cws:
            cws = rs._ws_from_store(parent_surf)       # store fallback, for when the tree can't be read
        if not cws:
            print("[fleet] ABORT: cannot resolve conductor workspace from --parent"); return None, None
        if place == "tab":
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
            # A workspace-placed agent with NO group is legitimate — it is exactly what `move
            # --own-workspace` has always produced for a groupless conductor (plain
            # `move-tab-to-new-workspace`, no group anywhere). This branch used to ABORT, which made a
            # groupless `--place workspace` unreachable from launch AND revive — the exact agents most
            # likely to need it. Mint a standalone workspace; cmux mints it with its own
            # terminal surface, so the agent lands on that surface and no husk is left behind.
            out = cmuxq("new-workspace", "--name", spec["label"], "--cwd", spec["abs_cwd"], "--focus", "false")
            m = re.search(r"(workspace:\d+)", out)
            if not m:
                print(f"[fleet] ABORT: new-workspace gave no workspace ref: {out.strip()}"); return None, None
            ws = _ref_to_uuid("workspace", m.group(1))
            if not ws:
                print(f"[fleet] ABORT: could not resolve {m.group(1)} to a workspace UUID"); return None, None
            return ws, _term_surface_in(ws)
        gref = _group_ref(group)
        if gref:                                              # group EXISTS -> join it
            out = cmuxq("new-workspace", "--group", gref, "--name", spec["label"],
                        "--cwd", spec["abs_cwd"], "--focus", "false")
            m = re.search(r"(workspace:\d+)", out)
            if not m:
                print(f"[fleet] ABORT: new-workspace gave no workspace ref: {out.strip()}"); return None, None
            ws = _ref_to_uuid("workspace", m.group(1))
            return ws, _term_surface_in(ws)
        # group does NOT exist -> BOOTSTRAP it (one conductor = one group). Model B (empty-anchor,
        # ratified 2026-07-10): the group anchor is an EMPTY scaffold titled 'Conductor - <label>' and
        # the conductor runs as an ordinary MEMBER in its own '<label>' workspace. Create the conductor's
        # workspace standalone, THEN `workspace-group create --from <that ref>` with an ALWAYS-EXPLICIT
        # --from (the implicit form adopts the CALLER's workspace, a known footgun). cmux's `create`
        # ALWAYS mints a fresh bare-shell workspace, anchors the group on THAT, and adopts <ref> as a
        # member (measured, cmux 0.64.17) -- which is already Model B's shape. So we KEEP that scaffold as
        # the anchor and just title it; we do NOT re-anchor onto the conductor and close the scaffold
        # (Model A, which rendered the conductor as a bare folder shim with its title forced to the group
        # name). `fleet archive` re-anchors off the scaffold onto a fresh scaffold, never dissolving it.
        out = cmuxq("new-workspace", "--name", spec["label"], "--cwd", spec["abs_cwd"], "--focus", "false")
        m = re.search(r"(workspace:\d+)", out)
        if not m:
            print(f"[fleet] ABORT: new-workspace gave no workspace ref: {out.strip()}"); return None, None
        member_ref = m.group(1)
        cmuxq("workspace-group", "create", "--name", group, "--from", member_ref)
        ws = _ref_to_uuid("workspace", member_ref)
        gref = _group_ref(group)
        if gref:
            for n in _title_group_anchor_scaffold(gref, ws, spec["label"])[1]:
                print(f"[fleet] {n}")
            print(f"[fleet] created group '{group}' ({gref}); {spec['label']} joined as member ({member_ref})")
        else:
            print(f"[fleet] warn: group '{group}' did not register; {spec['label']} workspace is standalone")
        return ws, _term_surface_in(ws)

    print(f"[fleet] ABORT: unknown place '{place}'"); return None, None


def poll_session(surf, timeout=60):
    """Wait for cmux to bind ANY session to `surf`, returning its id ('' on timeout).

    The sessions[] fallback prefers the freshest record with an ALIVE pid, and only then falls back to
    the freshest record of any liveness. The old form returned the FIRST surface-matching record in dict
    order — cmux never drops a surface's dead records, so on a reseated surface that is usually a corpse,
    and the caller binds the registry to a ghost session (the last of the six first-match reads; the
    other five were fixed 2026-07-10).

    The fallback must stay: this is the "has anything bound yet?" probe, and a just-bound session may not
    have written its pid yet — requiring a live pid outright would hang every launch. Prefer-alive gives
    us the ghost fix with no liveness precondition. Callers needing certainty that the bind is a LIVE
    agent use `rs.live_sid` (recycle's confirm), not this."""
    from . import state as fs
    end = time.time() + timeout
    while time.time() < end:
        d = _store()
        e = (d.get("activeSessionsBySurface") or {}).get(surf) or {}
        sid = e.get("sessionId")
        if not sid:
            live_sid, live_ts, any_sid, any_ts = "", -1.0, "", -1.0
            for s in (d.get("sessions") or {}).values():
                if (s.get("surfaceId") or "").upper() != (surf or "").upper():
                    continue
                ts = s.get("updatedAt") or 0
                if ts >= any_ts:
                    any_sid, any_ts = s.get("sessionId", ""), ts
                if fs.pid_alive(s.get("pid")) and ts >= live_ts:
                    live_sid, live_ts = s.get("sessionId", ""), ts
            sid = live_sid or any_sid
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


def register(surf, spec, parent_surface, session, ws, parent_label=None):
    from . import state as fs
    # store parent LABEL (durable); a top-level launch (no parent surface) stores None -- the canonical
    # top-level rep that is_top_level reads (Ship 5d, Berg-ruled: no sentinel), NOT an empty string.
    # `parent_label` explicit override (item 9): revive passes the ARCHIVED parent so the reporting
    # relationship is PRESERVED across the rm->revive round trip (not re-derived from whoever revived it).
    if parent_label is None:
        parent_label = fs.label_for_surface(parent_surface) or parent_surface or None
    # gen (Ship 5): a reseat fence bumped on every launch/revive/recycle, so any consumer can tell a cached
    # derived view is from a PRIOR seat of this label. Monotonic across the label's lifetimes (live/archive).
    _prior = fs.live_get(spec["label"]) or fs.archive_get(spec["label"])
    fs.live_put(spec["label"], {
        "role": spec["role"], "kind": spec["kind"], "tool": spec["tool"], "gen": (fs.e_gen(_prior) + 1) if _prior else 1,
        "cwd": spec["abs_cwd"], "parent": parent_label, "place": spec["place"], "status": "live",
        "surface": surf, "workspace": ws,
        "session": f"claude-{session}" if spec["tool"] == "claude" else session,
        # when this row was written -> the age gate for the fleet-doctor never-bound sweep (P0-4): a LAZY
        # child (codex) registers unbound and BACKFILLS its session on its first turn; a dead-on-arrival
        # one never binds. launchedAt lets the daemon tell "just launched, still booting" from "pending
        # for 10m -> dead/stuck" without a false alarm on a fresh boot. A revive/recycle re-stamps it (it
        # IS a fresh launch); absent on legacy rows -> the sweep treats age as 0 (never false-fires).
        "launchedAt": time.time(),
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
# into a launch that already started (so a slow-booting agent is never spammed with stray keystrokes),
# and to tell a LIVE agent from a DEAD launch (launch_error_line).
_TUI_MARKERS = ("Context Remaining", "bypass permissions", "esc to interrupt",
                "auto-accept edits", "? for shortcuts", "Welcome to Claude")
# codex paints NONE of the above (verified against a live codex 0.144.1 pane, 2026-07-10): its status
# line reads `gpt-5.5 xhigh · ~/cwd · main · Context 100% left`. Every marker above is a claude-ism, so
# `_agent_surfaced` was permanently False for codex — the enter-race loop would re-kick Enter into a
# live codex TUI, and launch_error_line had no way to see that a healthy codex was on screen.
_CODEX_TUI = re.compile(r"Context \d+% left")


def agent_tui_visible(pane_text):
    """True if an agent TUI (claude or codex) is painted on the pane (PURE). A painted TUI means a LIVE
    agent: not a shell awaiting an injected command, and not a launch that died on spawn."""
    pane = pane_text or ""
    return any(m in pane for m in _TUI_MARKERS) or bool(_CODEX_TUI.search(pane))


def _agent_surfaced(surf):
    """True once an agent TUI is visible on the surface (booting or running). While False, the surface
    is still at the shell — an injected command that hasn't started, i.e. the enter-race symptom."""
    return agent_tui_visible(cmuxq("capture-pane", "--surface", surf) or "")


# startup-error signatures on a pane -> a launch that DIED on spawn (a bad flag, a missing binary, an
# early crash), not a healthy agent (P0-4). Curated to catch the CLI-arg-death class the brief names
# (--effort/--model/--permission-mode forwarded to the wrong tool) without matching a live TUI's chrome.
_LAUNCH_ERROR_MARKERS = (
    "unexpected argument", "unrecognized argument", "unrecognized option", "invalid value",
    "error: unexpected", "USAGE:", "Usage: ", "command not found", "No such file or directory",
    "panic:", "thread 'main' panicked", "Traceback (most recent call last)",
)


def launch_error_line(pane_text):
    """First startup-error line printed BY THE LAUNCH in a captured pane (a bad flag / missing binary /
    early crash), or ''. PURE (no shell-out) so the launch-time verify (cli._launch_failure_line) and the
    fleet-doctor never-bound sweep (router._surface_error_line) share ONE scanner and both stay
    unit-testable without a live cmux -- the same shape as surface_ws_from_tree.

    THE SCANNER USED TO CRY WOLF ON EVERY CODEX LAUNCH. A marker match is not a dead launch: the marker
    list ("No such file or directory", "command not found", "Usage: ") matches ordinary chatter from two
    different sources, one per delivery path.

      - PASTE delivery (the command is typed into a running login shell): the pane opens with the
        SHELL's rc chatter. This box's ~/.zshrc:65 sources a file uv never created, so every surface
        begins with `/Users/berg/.zshrc:.:65: no such file or directory: /Users/berg/.local/bin/env`.
      - EXEC delivery (respawn-pane runs the launch AS the pane's process; see design-exec-launch.md):
        no shell, no echoed command -- but a healthy codex prints `⚠ MCP client for \\`terraform\\` failed
        to start: MCP startup failed: No such file or directory (os error 2)` and carries on happily.

    Both were reported as "LAUNCH FAILED ... likely a tool/flag mismatch", with a cleanup recipe, over a
    perfectly healthy agent. A scanner people learn to ignore is worse than no scanner.

    TWO RULES, in this order:

    1. A PAINTED AGENT TUI IS NOT A DEAD LAUNCH -> ''. This is the real discriminator, and it holds on
       both delivery paths: a process that died on spawn cannot paint chrome. It required teaching
       `agent_tui_visible` about codex, which paints none of claude's markers (its status line is
       `Context 100% left`) -- so for codex this gate had never once fired.
    2. Otherwise scan, and when the pane carries the fleet launch line (`_FLEET_LAUNCH_SIG`: the
       AGENT_ROLE=/AGENT_LABEL=/CMUX_FLEET_* env prefix render_send_cmd emits, which a human's shell
       never contains), scan only BELOW THE LAST one -- the shell's rc noise is always above it and the
       tool's own output always below. A pane with no such line (exec delivery) is scanned whole: there
       is no shell whose noise could be mistaken for the tool's.

    Positional, not a denylist of benign strings: it needs no upkeep as rc files and MCP servers change."""
    lines = launch_error_lines(pane_text)
    return lines[0] if lines else ""


def launch_error_lines(pane_text, cap=3):
    """EVERY startup-error-looking line in the pane (up to `cap`), in order. PURE.

    The plural exists because the FIRST match is so often the wrong one to show a human. On this box a
    pane that really did die on a bad flag reads:

        /Users/berg/.zshrc:.:65: no such file or directory: /Users/berg/.local/bin/env   <- rc noise
        error: unexpected argument '--effort' found                                      <- the actual cause

    Printing only the first hands the operator the innocent line and hides the guilty one — which is how a
    diagnosis becomes a red herring. So when the launch does condemn (and by then the PID has already
    settled that it is dead — see launch_verdict), show the whole list and let the human read it."""
    if agent_tui_visible(pane_text):                       # rule 1: a live agent, whatever else it printed
        return []
    lines = (pane_text or "").splitlines()
    sig = [i for i, l in enumerate(lines) if _FLEET_LAUNCH_SIG.search(l)]
    if sig:
        lines = lines[sig[-1] + 1:]                        # rule 2: strictly below the LAST launch line
    out = []
    for line in lines:
        low = line.lower()
        if any(m.lower() in low for m in _LAUNCH_ERROR_MARKERS):
            out.append(line.strip()[:200])
            if len(out) >= cap:
                break
    return out


# codex's interactive "update available" modal. An out-of-date codex paints this INSTEAD of its TUI and
# waits for a keypress: the seat exists, no session ever binds, and `fleet launch` calls it DONE (codex
# binds lazily, so an unbound seat is the healthy path for it). Strings lifted from the codex 0.144.1
# binary. The BACKSTOP for _codex_update_preflight, which stops the modal from ever appearing.
_CODEX_UPDATE_MODAL = ("Update available!", "Skip until next version", "Update now (runs")


def codex_update_modal(pane_text):
    """True if codex's blocking update modal is on the pane (PURE)."""
    return any(m in (pane_text or "") for m in _CODEX_UPDATE_MODAL)


def _launch_failure_line(surf):
    """launch_error_line applied to `surf`'s live pane (captured via cmuxq)."""
    return launch_error_line(cmuxq("capture-pane", "--surface", surf) or "")


def launch_verdict(live_pids, pane_text, swept=True):
    """PURE. What happened to a LAZY launch that bound no session? -> one of:

        'running'      a live seat-agent process is on the surface. The launch worked.
        'running-odd'  a live process AND something ugly on the pane. STILL WORKED — warn, hand it back.
        'failed'       NO live process AND a startup error on the pane. The only verdict that may condemn.
        'unproven'     the process-table sweep FAILED (`swept=False`), so we could not look. Never condemn
                       on a blind eye: an empty sweep is the absence of evidence, not evidence of absence,
                       and the remedy on this path prints `rm --kill`.
        'unbound'      no process yet, nothing wrong on screen. Unknown, not dead: say nothing, let the
                       registry show it PENDING and the daemon's never-bound sweep be the backstop.

    THE RULE, and the reason this is a function rather than three ifs in cmd_launch: **only the pid may
    condemn.** `live_pids` comes from the process table; the pane is text an agent, a shell, an rc file or
    an MCP server can print for any reason. A pane-only verdict convicted a healthy codex of dying on spawn
    and printed `fleet rm --kill` as the cure — the alarm's remedy would have destroyed the patient. Note
    that 'failed' needs BOTH halves: no pid alone is 'unbound' (a cold start), and an error line alone,
    with the process up, is 'running-odd'. Neither half convicts on its own."""
    err = launch_error_line(pane_text)
    wedged = codex_update_modal(pane_text)
    if live_pids:
        return ("running-odd" if (err or wedged) else "running"), err, wedged
    if not swept:
        return "unproven", err, wedged
    if err or wedged:
        return "failed", err, wedged
    return "unbound", err, wedged


_SEAT_ALIVE_TIMEOUT_S = 10        # a cold codex on a loaded box takes a few seconds to exec through zsh


def _seat_agent_alive(surf, tool, timeout=_SEAT_ALIVE_TIMEOUT_S):
    """(verdict, pids) for the seat on `surf` — THE launch verdict's authority, and resolve's tri-state.

    POLLED, not sampled once. The launch reaches here seconds after spawn, and the delivery chain is
    `zsh -ilc '... codex ...'`: until the shell finishes sourcing its rc files and execs, there is no
    agent process yet. A single sweep at t+8s would read an empty table on a slow box and call a perfectly
    healthy launch DEAD — swapping the old false-positive for a new one, from the other side. So poll, and
    return the moment a pid appears; only a full window of nothing counts as nothing.

    It asks `resolve.liveness`, not `pids_ps`, because the third state is exactly as load-bearing here as it
    is for `move`: a FAILED sweep must not be allowed to condemn a launch either. That hole was in this
    function until the two liveness authorities were merged — which is the argument for merging them."""
    from . import resolve as rs
    end = time.time() + timeout
    while True:
        verdict, pids, _ = rs.liveness(surf, tool=tool, store_pids=_surface_pids(surf))
        if verdict == rs.LIVE or time.time() >= end:
            return verdict, pids
        time.sleep(0.5)


_CODEX_UPDATE_TIMEOUT_S = 120     # `codex update` shells `brew upgrade --cask codex`; ~1.5s when current


def codex_update_note(rc, out):
    """PURE: the one line to print for a finished `codex update` (rc, combined output), or '' when the
    preflight found nothing worth saying. Split from the shell-out so the verdict is testable."""
    if rc is None:
        return f"codex update: timed out after {_CODEX_UPDATE_TIMEOUT_S}s; launching anyway"
    if rc != 0:
        return f"codex update: exited {rc} ({(out or '').strip().splitlines()[-1][:100] if (out or '').strip() else 'no output'}); launching anyway"
    if "already installed" in (out or ""):
        return ""                                         # the common case: current. Say nothing.
    return "codex update: updated codex to the latest version before seating the agent"


def _codex_update_preflight():
    """Update codex BEFORE seating a codex agent, so its interactive update modal never appears.
    Returns a note line ('' = nothing to say). Never raises; never blocks the launch.

    THE BUG: an out-of-date codex opens a blocking "Update available! / Update now / Skip until next
    version" modal at startup. It paints none of _TUI_MARKERS, so _send_launch_and_confirm reads the seat
    as "still at the shell" and re-kicks Enter INTO the modal, and no session ever binds -- which for a
    LAZY tool is indistinguishable from health, so `fleet launch` prints DONE over a seat that will sit
    unbound forever. Berg's ruling: ALWAYS ALLOW THE UPDATE.

    WHY A PREFLIGHT AND NOT A PANE SCAN. codex has no suppress-the-prompt flag; `codex update` is the
    non-interactive subcommand. Updating before the surface exists means the modal has nothing to
    interrupt -- the failure is designed out rather than detected. It is cheap and idempotent (it shells
    the install manager -- `brew upgrade --cask codex` here -- and prints "already installed" in ~1.5s
    when current), so paying it per codex launch costs less than one bind-wait timeout. A pane scan can
    only ever tell you afterwards, on a seat already wedged; `codex_update_modal` keeps that scan as the
    BACKSTOP (for an offline box, or a modal this preflight failed to prevent), never as the primary.

    BEST-EFFORT BY DESIGN: a timeout / non-zero rc / missing binary WARNS and launches anyway. An
    offline box must still be able to seat a codex agent, and the backstop catches what slips through."""
    try:
        p = subprocess.run(["codex", "update"], capture_output=True, text=True,
                           timeout=_CODEX_UPDATE_TIMEOUT_S)
        return codex_update_note(p.returncode, (p.stdout or "") + (p.stderr or ""))
    except subprocess.TimeoutExpired:
        return codex_update_note(None, "")
    except Exception as ex:                               # binary missing / not executable / OS error
        return f"codex update: could not run ({ex}); launching anyway"


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


def _exec_send_and_confirm(ws, surf, send_cmd, lazy, timeout):
    """Step-2 delivery for `cmd_launch`: the launch is the PANE PROCESS (adapter.exec_deliver, the
    same mechanism that killed recycle's paste class), then the same bind poll as the paste path
    MINUS the whole re-kick machinery — there is no Enter to lose, so there is nothing to re-kick.
    PATH-guarded like recycle (harmless with -c shells; byte-parity with the recycle path). Returns
    the bound sid, or '' (normal for a lazy tool, or when claude's resume-summary menu is up — the
    caller gates/dismisses that, exactly as with the paste path)."""
    from . import adapter
    _exec_launch(surf, adapter.path_guard(send_cmd), lambda m: print(f"[fleet] {m}"))
    end = time.time() + timeout
    while time.time() < end:
        sid = poll_session(surf, timeout=1)
        if sid:
            return sid                                       # claude bound -> definitively started
        if _resume_menu_visible(surf):
            return ""                # resume-summary menu is up -- caller must gate/dismiss it, not us
        if lazy and _agent_surfaced(surf):
            return ""                                        # lazy tool is up; it binds on its 1st turn
        time.sleep(1)
    return ""


def _deliver_launch(ws, surf, send_cmd):
    """The raw one-shot delivery used by `cmd_revive`: exec (default; the launch is the pane process)
    or paste (CMUX_FLEET_EXEC_LAUNCH=0, the soak fallback — one flag reverts launch, revive, AND
    recycle together). The caller owns all verification (revive's resume gate + bind poll)."""
    from . import adapter
    if _exec_launch_enabled():
        _exec_launch(surf, adapter.path_guard(send_cmd), lambda m: print(f"[fleet] {m}"))
    else:
        cmuxq("send", "--workspace", ws, "--surface", surf, send_cmd + "\n")


def _bind_launched_session(ws, surf, send_cmd, tool, label, abs_cwd, caller, lazy, timeout):
    """The resume-aware bind step for `cmd_launch`. Delivers via exec (default; step 2) or the paste
    path (CMUX_FLEET_EXEC_LAUNCH=0 soak fallback), then, when the caller passthrough carries a claude `--resume <id>`, gates
    the bind on the SAME dismiss sequence `cmd_revive` uses (_resume_and_gate -> picks 'full session
    as-is', never the lossy cursor-default 'resume from summary') instead of trusting a blind re-kick to
    land correctly on the menu. Aborts via sys.exit on a resume-gate timeout, same as cmd_revive: NOT
    binding/registering behind an undismissed menu, surface left alone (nothing torn down -- it may still
    be salvageable). Returns (ws, surf, sid); sid is '' if unresolved (the caller decides whether that's
    fatal, e.g. lazy tools expect it).

    INVARIANT I5 -- THE LAUNCHED SURFACE IS AUTHORITATIVE. `ws`/`surf` are returned exactly as they came
    in. We created that surface and delivered the process onto it; cmux told the process so itself
    (CMUX_SURFACE_ID in its env). Nothing in a hook store may CONTRADICT that. A reconciliation may only
    FILL IN what is missing -- the session id.

    It used to do the opposite. If the post-launch poll came up empty, it asked the hook store "where is
    the agent with this AGENT_LABEL/cwd?" (_discover_surface_for) and, on a hit, OVERWROTE the launched
    surface and re-resolved the workspace to match. Two things made that catastrophic on 2026-07-11:

      1. The label arm cannot fire. Fleet passes AGENT_LABEL as an ENV VAR (render_send_cmd emits
         `cd <cwd> && AGENT_LABEL=x claude ...`), but cmux records `launchCommand` as a structured
         object holding the exec'd binary's ARGV -- and argv never contains the `KEY=val` prefixes, which
         the shell consumes into the environment. So the "exact AGENT_LABEL match wins outright" arm is
         structurally dead on this build, and EVERY discovery silently degrades to the loose arm.
      2. The loose arm matches on CWD, and returns the matched record's `surfaceId` -- a hook-time
         attribution that can be wrong. It was: cmux had filed the freshly-launched agent's session
         (pid 78004, env CMUX_SURFACE_ID=E4CED20C…, the surface fleet launched) under 3F2CDDD4… -- an
         unrelated idle staging shell. poll_session(E4CED20C…) therefore saw nothing (the store knew the
         session only under the wrong surface), the reconcile fired, believed the store, and bound the
         registry row to the staging shell.

    A conductor then drives the registry's surface, so `fleet drive-child doctor-stall` typed an entire
    brief into a bare zsh, which wedged at a `dquote>` continuation prompt. It was luck that the foreign
    surface was an idle orphan and not a live session. `fleet vitals` read the agent `detached`, which
    was CORRECT: there was no agent on the surface the registry believed in.

    Now: the surface never moves. The sid is filled in by polling the launched surface itself; if it
    stays '' cmd_launch handles that safely -- it aborts WITHOUT registering, leaves the surface up, and
    signposts `fleet register <label> --surface <the launched surface>`. An empty sid is a recoverable
    gap; a registry row pointing at someone else's terminal is not. (The old misfile-ADOPTION fallback --
    recovering the sid from the live process's env when cmux filed the session under a phantom surfaceId --
    is retired: cmux 0.64.18+ heals that misfile class natively via the live-identity healing upsert.)"""
    if _exec_launch_enabled():
        sid = _exec_send_and_confirm(ws, surf, send_cmd, lazy, timeout)
    else:
        sid = _send_launch_and_confirm(ws, surf, send_cmd, lazy, timeout)
    resume_flag = _flag_val(caller, "--resume") if tool == "claude" else None
    if not sid and resume_flag not in (None, False):
        resume_sid = resume_flag if isinstance(resume_flag, str) else ""
        if not _resume_and_gate(surf, send_cmd, tool, resume_sid, lambda m: print(f"[fleet] {m}")):
            sys.exit(f"[fleet] ABORT: resume-summary menu never resolved for {label} (surface still "
                     f"booting or wedged at the menu); NOT registering. Re-run the launch once it "
                     f"settles. Inspect: cmux capture-pane --surface {surf}")
        sid = poll_session(surf)
    return ws, surf, sid


def cmd_launch(argv):
    # split launcher args | verbatim tool passthrough on the first standalone `--`
    caller = []
    if "--" in argv:
        i = argv.index("--")
        argv, caller = argv[:i], argv[i + 1:]

    ap = argparse.ArgumentParser(prog="fleet launch", add_help=True)
    ap.add_argument("role", nargs="?", help="roster role name (omit with --adhoc)")
    ap.add_argument("--adhoc", metavar="NAME", help="alias for the rostered `adhoc` role with label=NAME "
                                    "(one shared flat home, no per-name dir)")
    ap.add_argument("--tool", help="override the resolved tool (claude|codex|...)")
    ap.add_argument("--parent", default=os.environ.get("CMUX_SURFACE_ID", ""),
                    help="conductor surfaceId (default $CMUX_SURFACE_ID); 'none' = a top-level agent (no parent)")
    ap.add_argument("--label", help="override the display label / registry label")
    ap.add_argument("--place", help="override placement (tab|pane|workspace)")
    ap.add_argument("--group", help="workspace group for --place workspace (name or workspace_group:<ref>); "
                                    "'none' = a standalone workspace (opt out of the own/parent-group default)")
    ap.add_argument("--direction", default="down", help="split direction for --place pane")
    ap.add_argument("--cwd", help="override the launch cwd (absolute)")
    ap.add_argument("--plugin", action="append", default=[], metavar="NAME",
                    help="plugin to UNION onto the composed loadout (role or floor) for THIS launch "
                         "(repeatable or comma-sep). Routes through the index (plugins.toml), so a `linked` "
                         "name adds a --plugin-dir and an `enabled` name adds an enabledPlugins entry — "
                         "both plugin types. A name not in the index loads as a linked --plugin-dir "
                         "(default marketplace / an absolute path)")
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
    # none-vs-unset on the two placement axes (Ship 5d R-5d-1 / item 7-creation): the launch arg spec must
    # express an EXPLICIT "none" distinct from "unset/default".
    #   --parent none  -> a TOP-LEVEL agent (registry parent=None). Distinct from unset (=$CMUX_SURFACE_ID).
    #   --group  none  -> a STANDALONE workspace (no group). Distinct from unset (=own/parent group, below)
    #                     and from --group NAME (join/bootstrap that group).
    top_level = (a.parent or "").strip().lower() == "none"
    if top_level:
        a.parent = ""
    group_none = (a.group or "").strip().lower() == "none"
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
    if a.group and not group_none:                           # `--group none` is the standalone sentinel, not a name
        spec["group"] = a.group
    if a.label:
        spec["label"] = a.label
    if a.plugin:
        # UNION `--plugin` names onto the resolved spec's `plugins` BEFORE adapter_compile, so they route
        # through the index EXACTLY like a role's own `plugins` — a `linked` name composes an extra
        # --plugin-dir, an `enabled` name an extra enabledPlugins entry. Applies regardless of role/ad-hoc;
        # a role that wants a plugin BY DEFAULT still belongs in the toml.
        spec["plugins"] = _dedup(spec["plugins"] + _flatten_csv(a.plugin))
    # one conductor = one group: a place=workspace conductor with no explicit group anchors its OWN group
    # (named for its label); a place=workspace child with no explicit group joins its parent's group.
    # `--group none` (group_none) opts OUT of this default entirely -> a deliberately standalone workspace.
    if spec["place"] == "workspace" and not spec["group"] and not group_none:
        if spec["kind"] == "conductor":
            spec["group"] = spec["label"]
        elif a.parent:
            from . import state as fs
            pe = fs.entry_for_surface(a.parent)
            if pe and pe.get("group"):
                spec["group"] = pe["group"]
    # a top-level agent has no parent surface to place a tab/pane in -> it must own a workspace.
    if top_level and spec["place"] != "workspace":
        sys.exit(f"[fleet] ABORT: --parent none needs --place workspace (a top-level agent has no parent "
                 f"surface to hold a {spec['place']})")
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

    # --- provider selection (the OPTIONAL providers feature) -----------------------------------
    # Resolved BEFORE adapter_compile, because a codex provider names the HOME the launch runs in, and the
    # codex adapter must enumerate its cruft-stripping flags from THAT home (flags computed against the
    # default home and applied to a seat home break codex's config load — the agent-path bug).
    #
    # STRICTLY OPT-IN: with no [providers.codex] block at all, default_provider() returns "", this block is
    # inert, codex_home stays None, and codex runs in its own ~/.codex with ZERO fleet configuration. A
    # single-account user never has to think about any of this.
    from . import providers as pv
    raw_env, pr, codex_home = {}, None, None
    pname = a.provider or ""
    if pname and ":" in pname:
        ptool, pname = pname.split(":", 1)
        if ptool != spec["tool"]:
            sys.exit(f"[fleet] --provider tool '{ptool}' != this launch's tool '{spec['tool']}'")
    if not pname:
        # A configured DEFAULT is a real SELECTION, not a label — resolve+inject it so a plain launch runs on
        # a deliberate account, never on whatever happens to be ambient. For codex the provider names the HOME
        # (home IS the account); for claude a securestorage default names the keychain namespace. A `keychain:`
        # default resolves to nothing injected, so this stays byte-identical to today for the ambient case;
        # only an explicit securestorage/home default changes the launch. (default_provider("") for a tool with
        # no [providers.<tool>] block is "", so a single-account user still gets zero injection — opt-in holds.)
        # default_provider raises ProviderError on an UNREADABLE toml (fix 2): abort loudly rather than
        # let a broken parse fall through to "" (zero injection = the whole fleet on the ambient account).
        try:
            pname = pv.default_provider(spec["tool"])
        except pv.ProviderError as e:
            sys.exit(f"[fleet] ABORT: {e}")
    if pname:
        try:
            pr = pv.resolve_launch(spec["tool"], pname)
        except pv.ProviderError as e:
            sys.exit(f"[fleet] --provider: {e}")
        codex_home = (pr.get("env") or {}).get("CODEX_HOME")   # the home this codex launch will really use

    bin_name, args, env = adapter_compile(spec["tool"], spec, caller, codex_home=codex_home)

    if pr:
        env.update(pr["env"])
        raw_env.update(pr["raw_env"])
        args = args + list(pr.get("args") or [])         # provider CLI tokens
        spec["provider"] = pr["label"]
        # pre-launch token guard (codex env-token): refresh if near expiry, ABORT loudly on a dead/revoked
        # account so an agent never spawns into a 401 (never a silent wrong-account fallback). Skip on dry-run.
        if pr.get("needs_refresh") and not a.dry_run:
            try:
                pv.codex_ensure_fresh(pr["needs_refresh"])
            except pv.ProviderError as e:
                sys.exit(f"[fleet] ABORT: {e}")
        print(f"[fleet] provider: {pr['label']}" + (f"  ({pr['note']})" if pr.get("note") else ""))
        if pr.get("provisional"):
            print(f"[fleet] WARN: {pr['label']} account selection is PROVISIONAL (codex mechanism "
                  f"verdict pending; not yet final)")
    # no else: pr is None ONLY when no provider is configured at all (default_provider returned "") — a
    # single-account launch with nothing to attribute or inject.
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
    if not a.parent and not top_level:                       # `--parent none` (top_level) is the deliberate opt-out
        sys.exit("[fleet] ABORT: no --parent and no $CMUX_SURFACE_ID (pass --parent none for a top-level agent)")

    # live-label guard (registry/surface 1:1 invariant, same family as the rm flip): register() is a
    # bare live_put overwrite, so launching into a label whose row still points at a live surface would
    # silently orphan that surface with NO trail at all (not even a "removed" event). Refuse unless the
    # old row is clearly STALE (dead lifecycle + a recorded session -- the same predicate `fleet ls`
    # flags); a pending/unverifiable row refuses too (fail closed). --force is the operator override
    # for "I KNOW the old surface is already dead by other means"; same spirit as cmd_register's
    # already-live-under-a-different-surface refusal.
    from . import state as fs
    from . import resolve as rs
    prior = fs.live_get(spec["label"])
    if prior and prior.get("surface"):
        prior_surf = prior["surface"]
        # stale = the prior row's surface holds NO genuinely-live agent (lifecycle terminal, OR frozen
        # non-terminal on a DEAD pid -- the SessionEnd-less brick, 2026-07-06) AND it once bound a session.
        # Via the liveness rule (rs.present) a dead-pid ghost now reads stale -> we overwrite the row (no
        # bogus "already LIVE" refusal); a genuinely-live surface (live pid) still refuses (fail-closed:
        # the orphan-a-live-surface guard this exists for). A pending row (no session) refuses as before.
        stale = not rs.present(prior_surf) and bool(prior.get("session"))
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
    if a.adhoc:                                          # the shared adhoc home has no role plugins/CLAUDE.md ->
        _link_floor_claudemd(spec["abs_cwd"])            # symlink the floor CLAUDE.md so they inherit it
    if spec["tool"] == "codex":                          # design the update modal out (Berg: always
        note = _codex_update_preflight()                 # allow the update); the pane scan below is the
        if note:                                         # backstop, not the primary
            print(f"[fleet] {note}")
        # EVERYTHING this home needs to be a fleet citizen, in ONE pass, on EVERY launch. Sync-on-launch is
        # what makes the home fleet-OWNED rather than hand-maintained: a worker cannot boot with a stale
        # doc or severed hooks, a seat added next month cannot miss them, and nobody has to remember a
        # setup step. Idempotent — the already-correct home is not written to at all.
        _codex_seat_preflight(codex_home or CODEX_DEFAULT_HOME)
    stamp_cursor = rs.stamp_cursor()                     # BEFORE the surface exists: a stamp counted after
    ws, surf = create_surface(spec, a.parent, a.direction)   # this mark can only be OUR agent's
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
        # the surface is LIVE but no session bound in the window — likely still booting (heavy loadout), or
        # the injected command never started. It is NOT torn down, so it is adoptable once it binds
        # (recovery-safety #9): signpost the register-after escape hatch alongside the inspect command.
        sys.exit(f"[fleet] timed out waiting for session binding on surface {surf}; the injected command "
                 f"may still be booting (heavy loadout) or never started. The surface is NOT torn down. "
                 f"If it comes up, adopt it:\n[fleet]     fleet register {spec['label']} --surface {surf}\n"
                 f"[fleet]   (inspect first: cmux capture-pane --surface {surf})")
    # The dark-surface / session-misfile class (cmux filing a freshly-seated agent's session under a
    # surfaceId the fleet/agent env never saw) is HEALED NATIVELY since cmux 0.64.18: the live-identity
    # healing upsert (agent.resolve_delivery_target) promotes the live pid/surface identity over the
    # persisted record and re-stamps the surface. So the fleet's old reseat-if-dark guard is retired —
    # the launched surface is authoritative (invariant I5) and we register it directly.
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
    # launch verification (P0-4a): a LAZY tool that never bound is EITHER healthy (binds on its 1st turn)
    # OR dead-on-arrival -- e.g. codex rejecting a claude-ism like --effort (2026-07-07): it exited on
    # spawn yet launch still printed DONE while the surface sat empty. `not sid` alone can't tell the two
    # apart (a bound claude already sys.exited above on a failed bind, so this only guards the lazy path).
    # POSITIVELY scan the pane for a startup-error signature before reporting DONE: a match = the process
    # died -> FAIL LOUD with the cleanup + retry, never pretend it's a pending backfill. No match = trust
    # the normal lazy path (the row is already registered, so a silent death still shows PENDING in
    # `fleet ls` and the daemon never-bound sweep is the backstop).
    if lazy and not sid:
        # THE VERDICT IS PID-AUTHORITATIVE. A lazy tool binds no session at launch, so "no session" says
        # nothing at all about whether the process is alive — and the pane cannot answer it either. Ask the
        # process table, which is the only thing here that cannot be counterfeited by cosmetics.
        #
        # This block used to convict on pane text alone, and it convicted the innocent: a fleet-launched
        # codex, running perfectly, was reported `!!! LAUNCH FAILED ... the process exited on spawn` with
        # `fleet rm --kill` as the printed cure — because the pane's FIRST line was rc noise from the
        # operator's ~/.zshrc (a `.` of a file uv never created), printed before codex was even exec'd.
        # Both existing guards missed it: `agent_tui_visible` looks for `Context N% left`, which a codex
        # paints only AFTER its first turn (never at t=0), and the positional guard assumed exec delivery
        # has no shell — but exec delivery runs `zsh -ilc`, which sources the rc file like any other login
        # shell. Rather than chase markers, stop asking the pane a question it cannot answer.
        from . import resolve as _rs
        seat, live_pids = _seat_agent_alive(surf, spec["tool"])
        pane = cmuxq("capture-pane", "--surface", surf) or ""
        verdict, errline, wedged = launch_verdict(live_pids, pane, swept=seat != _rs.UNKNOWN)
        if verdict == "unproven":
            print(f"\n[fleet] note: could not read the process table, so I cannot say whether "
                  f"{spec['label']} came up. NOT reporting a failure on a blind eye.")
            print(f"[fleet]   Look: cmux capture-pane --surface {surf}   /   fleet ls")
        elif verdict == "running-odd":
            # ALIVE. Whatever the pane says, this launch did not fail. The heuristic WARNS — it hands the
            # agent back and asks a human to look. It never condemns, and it never gets a kill command.
            what = ("the pane looks like codex's interactive update modal, which binds no session"
                    if wedged else f"the pane shows unexpected text: {errline}")
            print(f"\n[fleet] note: {spec['label']} is RUNNING (pid {sorted(live_pids)[0]}), but {what}.")
            print(f"[fleet]   The process is alive, so this is NOT a failed launch — that text may be "
                  f"harmless startup noise (an rc file, an MCP server).")
            print(f"[fleet]   Look before you touch it: cmux capture-pane --surface {surf}")
        elif verdict == "failed":
            # No live process AND a startup error. NOW the pane line is a diagnosis rather than a guess,
            # and only now may a remedy be destructive — there is nothing alive left to destroy.
            print(f"\n[fleet] !!! LAUNCH FAILED for {spec['label']} (tool {spec['tool']}): no live "
                  f"{spec['tool']} process is on the surface, and the pane shows a startup error.")
            for line in (launch_error_lines(pane) or ["sitting in codex's update modal"]):
                print(f"[fleet]   pane says  : {line}")   # every candidate: the first is often rc noise
            print(f"[fleet]   inspect    : cmux capture-pane --surface {surf}")
            print(f"[fleet]   clean up + retry:")
            print(f"[fleet]     fleet rm {spec['label']} --kill")
            print(f"[fleet]     fleet launch {a.role or ('--adhoc ' + a.adhoc)} ...   # fix the flags first")
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
    # default-worker folded into `adhoc` (5d): --adhoc NAME inspects the scratch role (requires [role.adhoc],
    # like launch). A bare `--cwd` probe, though, is roster-INDEPENDENT — just "what does the tool stack in
    # this dir" — so it must NOT need a roster (nor even a [tool.<t>] floor; a fresh/empty toml is fine).
    # When no [role.adhoc] exists, synthesize one carrying the target tool's (empty) sub-block, so resolve's
    # tool-check is satisfied and we get a floor-only probe spec.
    if not a.role and not a.adhoc and "adhoc" not in (cfg.get("role") or {}):
        tool = a.tool or (cfg.get("defaults", {}) or {}).get("tool") or "claude"
        cfg = {**cfg, "role": {**(cfg.get("role") or {}), "adhoc": {tool: {}}}}
    spec = resolve(cfg, a.role or "adhoc", a.tool, a.adhoc or (None if a.role else "_inspect"))
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
    if spec.get("setting_sources"):
        print(f"  --setting-sources: {spec['setting_sources']}")
    if spec["tool"] == "claude":                           # index-resolved -> the two channels, kept distinct
        p_linked, p_enabled, p_unres = _resolve_plugins(spec["plugins"], load_plugin_index())
        print(f"  plugins (index): {', '.join(spec['plugins']) or '(none)'}")
        if p_linked:
            print(f"      -> --plugin-dir: {', '.join(p_linked)}")
        if p_enabled:
            print(f"      -> enabledPlugins: {', '.join(p_enabled)}")
        if p_unres:
            print(f"      -> unresolved (skipped): {', '.join(p_unres)}")
    else:
        print(f"  plugins: {', '.join(spec['plugins']) or '(none)'}")
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
    load this". Returns [(scope, key)] e.g. ("tool.claude","plugins"), ("role.researcher.claude","plugins")."""
    cfg = load_config()
    hits = []

    def _check(block, scope):
        if not isinstance(block, dict):
            return
        for ref in (block.get("plugins") or []):
            if str(ref) == name:
                hits.append((scope, "plugins"))

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
            print(f"      {scope}  via {key}")
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
    try:                                                     # a malformed index aborts FIRST (before we even
        fp.assert_index_parseable(PLUGIN_INDEX)              # resolve a marketplace — the tolerant read that
    except fp.IndexParseError as e:                          # feeds resolution would silently see an EMPTY
        print(f"[fleet] plugins add: existing index {e.path} is malformed ({e.cause}); refusing to "  # index,
              f"overwrite it — fix or delete it, then re-run. (Nothing cloned.)", file=sys.stderr)     # so the
        return 2                                             # real reason must win over "no LOCAL marketplace")
    target = a.marketplace or "default"
    mk = marketplaces.get(target)
    if not mk or mk.get("kind") == "global" or not mk.get("path"):
        print(f"[fleet] plugins add: no LOCAL marketplace '{target}' to clone into — declare "
              f"[marketplace.{target}] path=... in {PLUGIN_INDEX} (and pass --marketplace {target}).",
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
    # register the plugin in the marketplace's manifest so the reconcile below derives an HONEST origin:
    # a git-url add records a url source (origin=url), a local-path add a path source (origin=path). The
    # manifest is created if the marketplace has none; the write is additive (other entries preserved).
    manifest = fp.register_in_marketplace(mk["path"], target, name, dest, a.ref, kind, fp._plugin_json_desc(dest))
    # reconcile the LINKED channel ONLY (settings_paths=[]): the enabled channel is never scanned or
    # written by an `add linked`, and existing enabled index entries are preserved untouched. (The index
    # was already checked parseable above, before the clone, so this cannot raise IndexParseError.)
    new_text, _diff, existing_text = fp.run_reconcile(PLUGIN_INDEX, marketplaces, [], prune=False)
    if new_text != existing_text:
        os.makedirs(os.path.dirname(os.path.abspath(PLUGIN_INDEX)), exist_ok=True)
        with open(PLUGIN_INDEX, "w", encoding="utf-8") as f:
            f.write(new_text)
    print(f"[fleet] plugins add: wired '{name}' as LINKED into the index ({PLUGIN_INDEX}).")
    print(f"  cloned into:  {dest}")
    print(f"  registered:   {manifest}  (origin={'url' if kind == 'git-url' else 'path'})")
    print(f"  NOT enabled — no role loads it and no hook has run. Enable it for ONE agent when ready:")
    print(f"      fleet recycle <agent> --plugin {name}")
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
    print(f"      fleet recycle <agent> --plugin {name}")
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
    ap.add_argument("--name", help="index name + clone-dir name for the plugin (default: the ref's basename). "
                                   "Pass this to resolve a basename collision with an already-indexed plugin.")
    ap.add_argument("--dry-run", action="store_true",
                    help="infer + print the plan; clone/install/write NOTHING")
    a = ap.parse_args(rest)

    index = load_plugin_index()
    name = a.name or fp.plugin_name_from_ref(a.ref)

    # 1. already indexed? The idempotency check keys on the derived NAME, and two different repos can share
    #    a basename (orgA/tools vs orgB/tools). So distinguish a genuine re-add of the SAME plugin (a safe
    #    no-op) from a COLLISION where a different source would land on a taken name (STOP + tell the human
    #    to pass --name). Signal: an explicit target marketplace that differs from the indexed entry's source.
    if name in index["plugins"]:
        cur = index["plugins"][name]
        target = a.marketplace or (a.ref.partition("@")[2] if "@" in a.ref else "")
        if target and cur.get("source") and target != cur.get("source"):
            print(f"[fleet] plugins add: STOP — the index name '{name}' is already taken by marketplace "
                  f"'{cur.get('source')}' (type={cur['type']}), but this add targets '{target}'. Two "
                  f"different plugins would collide on one index name.\n"
                  f"  Re-run with --name <other> to index it under a different name.", file=sys.stderr)
            return 2
        print(f"plugin '{name}' is already indexed (type={cur['type']}, source={cur.get('source', '') or '?'}); "
              f"nothing to do.")
        print(f"  if you meant a DIFFERENT plugin, its basename collides — re-run with --name <other>.")
        print(f"  enable this one per-agent with:  fleet recycle <agent> --plugin {name}")
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
# (_store — a verbatim second definition of the one at the top of this module — was DELETED
# 2026-07-10. It shadowed nothing (identical body) but see tests/test_no_shadowed_defs.py: the same
# pattern in features.py silently swapped a duration formatter for an epoch one for three days.)

# (_pid_for_surface — the first-record, no-aliveness-check pid lookup — was DELETED 2026-07-10 with
# zero callers left. It fed every kill site the wrong target on multi-record surfaces: the recycle
# wedge AND the rm/archive live-agent leak. Kill targets come from _signal_agent_pids/_surface_pids
# only; do not reintroduce a first-record pid lookup.)
def _surface_pids(surface):
    """The set of pids currently ALIVE on `surface` per the hook store — the pre-respawn safety
    snapshot for the recycle verify AND the kill-path target set: any of these still alive AFTER the
    respawn means the old agent survived the kill, so we must NOT relaunch over it (the 'never type
    into a live TUI' invariant). ALL records are scanned and DEAD pids are excluded — the kill-path
    twin of rs.live_sid's rule (a dead pid cannot be the agent). Contrast _pid_for_surface, which
    returns the FIRST record's pid in dict order with no aliveness check: on a surface with several
    lingering records that is usually a dead ghost (the 2026-07-10 wedge: SIGINTs hit corpses 76035
    and 70208 while the real agent, 76142 on the 4th record, survived orphaned).
    Canonical body: resolve.pids (step 1 of the v2 migration); this name stays as the kill-path
    call-site seam until step 3, and the store still flows through cli._store (the seam the kill-path
    tests inject fixture stores through)."""
    from . import resolve as rs
    return rs.pids(surface, st=_store())


def _live_agent_pids(surf, tool, ps_out=None):
    """The live agent pids on `surf` — DELEGATES to resolve.agent_pids, which is the one place that defines
    what "an agent is running here" means (store live pids UNION process-table seat-agent pids).

    It used to define that union HERE, and resolve.py defined it too, and the two never textually collided —
    which is worse than if they had, because a second liveness authority drifts silently. `resolve.py`'s own
    header already forbade it. The store half is still read through cli's own `_surface_pids` — the accessor
    the never-orphan teardown gate reads through — so the two can never disagree about who is alive; the
    UNION itself is resolve's, defined once."""
    from . import resolve as rs
    return rs.agent_pids(surf, tool=tool or "claude", ps_out=ps_out, store_pids=_surface_pids(surf))


def _identifies_as(cmdline, tool):
    """PURE identity rule for kill targets: the process's argv0 BASENAME equals the tool. The old
    rule was a substring over the whole command line, which passed `python3
    .../claude-marketplace/.../hook.py` (path contains 'claude') — harmless while targets came only
    from the hook store (real agents), but pids_ps now feeds every surface-env process through this
    check, so a substring rule would SIGINT plugin hook scripts (cmux-advisor blocker, 2026-07-10).
    Basename-of-argv0 keeps `~/.local/bin/claude` and the cmux shim `.../cmux-cli-shims/<S>/claude`,
    and excludes the daemon python and any marketplace path. A `claude -p` subprocess (the memsearch
    summarizer) passes THIS check by argv0 — but it is NOT reachable through the live kill/close
    path: pids_ps filters to the seat agent first via the exact CMUX_CLAUDE_PID self-pid rule (the
    summarizer carries its parent's pid), and store pids are real agents by construction. This check
    is the per-pid backstop for store pids and the codex path; do not re-add a summarizer carve-out
    here, there is no residual to fix (cmux-advisor verification, 2026-07-10)."""
    toks = (cmdline or "").split()
    return bool(toks) and os.path.basename(toks[0]) == (tool or "claude")


def _agent_pid_check(pid, tool):
    """True iff `pid` is a live process that identifies as this agent `tool` (_identifies_as over its
    ps command line), applied to KILL targets immediately before signalling. The pid-reuse guard: the
    2026-07-10 direct-kill fired SIGINT x2 at bare pid 70208, which by then belonged to no claude at
    all — had the OS recycled that pid, we'd have signalled an unrelated process. Fails CLOSED
    (unreadable ps -> False): never signal a process we can't identify; the verify then refuses and
    the recycle aborts safely instead."""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=5).stdout or ""
    except Exception:
        return False
    return _identifies_as(out, tool)


def _signal_agent_pids(surf, tool, log, tag):
    """SIGINT x2 EVERY live, identity-checked agent pid on `surf`; returns the pids actually signalled.
    THE one kill-target selector for the recycle tail (graceful close + direct-kill fallback), fixing
    the 2026-07-10 wedge class: targets come from _surface_pids (every ALIVE pid whose record maps to
    this surface — never the first record, never a dead one), and each is re-verified as this tool's
    live process at signal time (_agent_pid_check, the pid-reuse guard). A dead pid is a no-op by
    construction; a live pid that fails the identity check is SKIPPED loudly (better to abort the
    recycle than SIGINT a foreign process). Targets = store pids UNION seat-agent ps pids: the ps
    side exists because SessionEnd reaps the record ~0.3s BEFORE the process exits (cmux-advisor
    finding 2), and it is filtered to the SEAT AGENT at the source (rs.pids_ps applies the
    CMUX_CLAUDE_PID self-referential rule), so a conductor's legitimately-inherited env carriers
    (daemon, router, node servers, the `claude -p` summarizer) are never candidates at all — the
    unfiltered union wedged every conductor close (the 2026-07-10 blocker)."""
    import signal
    signalled = []
    for pid in _live_agent_pids(surf, tool):        # store pids UNION seat-agent ps pids (the ONE authority)
        if not _agent_pid_check(pid, tool):
            log(f"{tag}: pid {pid} is alive but does not identify as a live {tool} process "
                f"(reused/foreign pid?); NOT signalling it")
            continue
        log(f"{tag}: SIGINT x2 -> pid {pid}")
        try:
            os.kill(int(pid), signal.SIGINT); time.sleep(0.5); os.kill(int(pid), signal.SIGINT)
            signalled.append(int(pid))
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    return signalled


def _graceful_close(surf, tool, log, timeout=6):
    """Close the OLD session GRACEFULLY before respawn-pane so cmux's lifecycle reaches a clean
    terminal state and EVERY consumer (the verify, `fleet ls`, the fleet-doctor, vitals) sees honest
    state -- not a frozen 'running' ghost. SIGINT x2 straight to the pid is claude's clean TUI exit;
    empirically it fires SessionEnd even mid-turn (~0.5s; the 2026-07-06 sandbox matrix). Targets EVERY
    live identity-checked agent pid on the surface (_signal_agent_pids) — the old single-pid form drew
    its target from the first hook-store record and SIGINT'd a dead ghost while the real agent (a later
    record) kept running (the 2026-07-10 wedge). Best-effort + bounded: no live target -> skip (the
    pid-aware verify handles ghosts), returns the instant the lifecycle goes terminal or every
    signalled pid dies, and ALWAYS falls through to the respawn. This is an HONESTY step, NOT the
    confirming signal: _confirmed_gone -- not this -- authorizes the relaunch."""
    from . import state as fs
    from . import resolve as rs
    pids = _signal_agent_pids(surf, tool, log, "graceful close")
    if not pids:
        return                                        # nothing live to close -> pid-aware verify confirms
    end = time.time() + timeout
    while time.time() < end:
        if rs.lifecycle(surf) in ("", "-", "ended") or not any(fs.pid_alive(p) for p in pids):
            log("graceful close: old session reached a clean terminal lifecycle (SessionEnd fired)")
            return
        time.sleep(0.5)
    log("graceful close: no terminal lifecycle within window; proceeding to respawn (pid-verify decides)")


_STOP_WAIT_S = 4      # post-SIGINT death-wait ceiling for the rm/archive stop (a clean TUI exit is ~0.5s)


def _stop_agent_for_close(surf, tool, label, verb):
    """Stop the agent(s) on `surf` and answer whether it is SAFE to close the surface. Returns
    (ok_to_close, note). Safe = every live agent pid on the surface was identity-checked, signalled,
    and observed DEAD within a bounded wait — or there was nothing live to stop. NOT safe = a live pid
    was skipped (unidentifiable: pid reuse / ps failure) or survived the SIGINTs.

    THE never-orphan invariant for the deliberate teardown verbs (rm/archive), the same live-pid truth
    as the recycle tail: the 2026-07-10 leak was `fleet rm` SIGINTing a stale first-record pid
    (_pid_for_surface) and then closing the surface anyway — the REAL agent survived close-surface and
    kept running with no pane, no `fleet ls` row, no way for anyone to find it (four live 1M-ctx
    orphans found on the box, two from that day's rms). A surface left OPEN over a live agent stays
    visible and recoverable; a surface CLOSED over one hides it forever — so on any doubt the caller
    must refuse the close (even under --force, which forces past the quiet gate, not past this).

    Death is observed on the SIGNALLED pids themselves plus the per-source block set, never on store
    emptiness: SessionEnd removes the hook-store record ~0.3s before the process exits (measured,
    cmux-advisor finding 2), so `while _surface_pids(surf)` returned empty while the agent still ran —
    for a hung shutdown, forever. `fleet rm ptrprobe` live-corroborated it: 'removed (closed +
    archived)' printed with pid 98942 still alive.

    The block set: a STORE pid blocks if alive (the store claims it is the agent — fail closed); a
    ps-side pid blocks because it IS the seat agent (rs.pids_ps applies the CMUX_CLAUDE_PID
    self-referential rule at the source: the wrapper exports its own pid into the agent's env, so
    only the agent satisfies env pid == own pid; descendants inherit the value with different pids).
    A conductor's surface env is legitimately inherited by never-dying processes (daemon, router,
    `cmux events`, node servers — measured 3-5 per live conductor), so the earlier unfiltered ps-env
    union made rm/archive refuse to close ANY conductor, permanently (the 2026-07-10 blocker). It
    shipped because the suite stubs the ps sweep and the live probe was an ad-hoc seat with no
    daemons; the regression test now injects a mixed ps table through the REAL parse."""
    from . import state as fs
    def log(m):
        print(f"[fleet] {verb} {label}: {m}")
    signalled = _signal_agent_pids(surf, tool, log, f"{verb} stop")
    if signalled:
        end = time.time() + _STOP_WAIT_S                  # SIGINT x2 exits a claude TUI in ~0.5s; 4s is generous
        while time.time() < end and any(fs.pid_alive(p) for p in signalled):
            time.sleep(0.3)
    still = sorted({p for p in signalled if fs.pid_alive(p)} | set(_live_agent_pids(surf, tool)))
    if not still:
        return True, ""
    return False, (f"live agent pid(s) {still} still on the surface "
                   f"({'survived SIGINT x2' if signalled else f'not identifiable as a live {tool} process — reused/foreign pid?'}); "
                   f"NOT closing the surface — closing would strand a live agent with no pane and no "
                   f"registry row (invisible orphan). Check the pid(s), stop the agent, then re-run.")


# --- the seat close: an archived agent leaves NO cmux residue --------------------------------------
# Berg's ruling (2026-07-10): "You retired it ... but now there's just a workspace sitting there open
# with that name on it. Remove the workspace from cmux whenever an agent is getting archived."
#
# The verb is `cmux close-workspace --workspace <uuid>`, which closes the workspace AND its surfaces.
# `close-surface` cannot do it: cmux refuses with `invalid_state: Cannot close the last surface` when
# the surface is its workspace's only one — documented behavior (docs/reap-surfaces.md item 3), and
# precisely why a workspace-placed agent (whose surface IS the only one in its own workspace) needs the
# workspace verb. It is also why the two verbs are not interchangeable in the other direction: a
# tab/pane agent SHARES its parent conductor's workspace, and closing that workspace would take the
# conductor down with it. So the choice of verb is a judgment about placement, and it is made here.
def plan_seat_close(label, surface, workspace, caller_workspace, place, siblings):
    """PURE: which cmux verb retires this agent's seat, and what it takes with it. Returns
        {"verb": "close-workspace"|"close-surface", "workspace": ws, "collateral": [...], "blockers": [...]}
    `siblings` is every OTHER surface the tree places in `workspace`, as
    {"surface","kind","title","label" (registry label or ""), "pids" (live agent pids, any tool)}.

    close-workspace requires ALL of:
      1. the workspace is not the CALLER's own (never close the ground you stand on);
      2. no sibling surface belongs to another REGISTERED agent, and no sibling surface carries a live
         agent pid — the never-orphan floor (resolve.occupants), applied to bystanders because a
         workspace close takes every surface with it, and applied to untracked seats too: a conductor
         whose registry row was already archived is invisible to (1) and (2)-by-label, but its live
         claude pid is not;
      3. the agent's DERIVED placement (resolve.place_of) is 'workspace' — it owns this workspace,
         rather than sitting in its parent's as a tab/pane.
    Any blocker DOWNGRADES to close-surface (which the sibling that caused the blocker guarantees is
    legal — a blocked workspace always has ≥2 surfaces). It never escalates a refusal: the agent still
    retires, only its workspace outlives it, and the reason is printed."""
    if not workspace:
        return {"verb": "close-surface", "workspace": "", "collateral": [],
                "blockers": ["workspace unresolvable from `cmux tree` (surface already closed?)"]}
    blockers = []
    if caller_workspace and caller_workspace.upper() == workspace.upper():
        blockers.append("it is the CALLER's own workspace ($CMUX_SURFACE_ID lives here)")
    if place != "workspace":
        blockers.append(f"derived placement is {place or 'unknown'!r}, not 'workspace' "
                        f"({label} shares this workspace rather than owning it)")
    tenants = sorted(s["label"] for s in siblings if s.get("label"))
    if tenants:
        blockers.append(f"it still hosts registered agent(s): {', '.join(tenants)}")
    busy = sorted((s["surface"], sorted(s["pids"])) for s in siblings if s.get("pids"))
    if busy:
        blockers.append("live agent pid(s) on bystander surface(s): "
                        + "; ".join(f"{s[:8]} -> {p}" for s, p in busy))
    if blockers:
        return {"verb": "close-surface", "workspace": workspace, "collateral": [], "blockers": blockers}
    return {"verb": "close-workspace", "workspace": workspace, "blockers": [], "collateral": list(siblings)}


def _reanchor_group_off(ws, tree_text):
    """Move a group's ANCHOR off workspace `ws` before `ws` is closed. Returns (ok, note).

    Closing an anchor dissolves its group -- cmux's documented behavior, and confirmed live on 0.64.17:
    closing a two-member group's anchor left the survivor as an ungrouped workspace. So closing the
    anchor workspace would silently scatter a group's still-live members out of the sidebar group.

    Under Model B (empty-anchor, ratified 2026-07-10) the anchor is an EMPTY scaffold that no agent lives
    on, and the conductor runs as an ordinary MEMBER -- so archiving a conductor closes a plain member and
    returns early below, never touching the anchor. The re-anchor branch is the DEFENSIVE path for when
    the anchor WORKSPACE itself is removed: a legacy Model A group whose conductor IS the anchor, or a
    deliberate scaffold close. It re-anchors onto a FRESH empty scaffold minted in the group, NEVER onto a
    surviving member -- re-anchoring onto a peopled member conductor is exactly Model A (the bare-folder
    header with the forced title that this flip reverses). `workspace-group set-anchor` takes a group REF,
    never a name, plus a workspace UUID.

    ok=False REFUSES the close: the mint + re-anchor is a cmux mutation we VERIFY (re-read the group
    list), and an unverified anchor move means the next call would dissolve the group. Refuse and report."""
    info = _group_of_workspace(ws, tree_text)
    if not info:
        return True, ""                                   # ungrouped -> nothing to protect
    gref, anchor, members = info
    if not anchor or anchor.upper() != (ws or "").upper():
        return True, ""                                   # a plain member -> closing it just leaves the group
    survivors = sorted(m for m in members if m and m.upper() != ws.upper())
    if not survivors:
        return True, (f"group {gref} has no member besides {ws[:8]}; closing it dissolves an empty group")
    # Model B: re-anchor onto a FRESH empty scaffold, never onto a surviving member. Mint a bare workspace
    # in the group, title it with the group's header name, anchor there.
    title = _group_name(gref) or "Conductor"
    out = cmuxq("new-workspace", "--group", gref, "--name", title, "--focus", "false")
    m = re.search(r"(workspace:\d+)", out)
    fresh_tree = cmuxq("tree", "--all", "--id-format", "both")
    new = _ref_to_uuid("workspace", m.group(1), fresh_tree) if m else ""
    if not new:
        return False, (f"could not mint a replacement scaffold anchor for group {gref}; NOT closing "
                       f"{ws[:8]} (closing an anchor dissolves group {gref} and ungroups its "
                       f"{len(survivors)} surviving member(s)). Re-anchor by hand, then re-run.")
    cmuxq("workspace-group", "set-anchor", "--group", gref, "--workspace", new)
    after = _group_of_workspace(ws, fresh_tree)            # VERIFY: cmuxq gives us no rc worth trusting
    if after and after[1] and after[1].upper() == ws.upper():
        return False, (f"`workspace-group set-anchor --group {gref} --workspace {new[:8]}` did not move "
                       f"the anchor off {ws[:8]}. NOT closing the workspace: closing an anchor dissolves "
                       f"group {gref} and ungroups its {len(survivors)} surviving member(s). Re-anchor "
                       f"by hand, then re-run.")
    return True, (f"re-anchored group {gref} onto fresh empty scaffold {new[:8]} '{title}' "
                  f"(was anchored on {ws[:8]}) -- the conductor stays a member")


def seat_close_plan(label, e):
    """Read the tree ONCE and decide how `label`'s seat retires. Returns the plan bundle, or None when
    there is nothing to close.

    CALL THIS BEFORE STOPPING THE AGENT. Under exec delivery (docs/design-exec-launch.md) the agent IS
    the pane's process, so the instant SIGINT lands cmux closes its surface — and a plan computed
    afterwards can no longer see which workspace the agent lived in. Measured: `fleet archive` of an
    exec-launched codex found the surface already absent from the tree, fell through to the
    surface-unlocatable branch, and would have left a workspace-placed codex's workspace standing
    forever — Berg's exact complaint, surviving the fix, for one tool.

    Reading before the stop is also strictly more correct for claude: every topology question is answered
    off ONE snapshot taken while the seat still exists, so the verb choice cannot straddle a torn view.
    None of the guards depend on post-stop state — they ask about BYSTANDER surfaces, which stopping our
    own agent cannot change."""
    from . import state as fs
    from . import resolve as rs
    surf = (e or {}).get("surface", "")
    if not surf:
        return None
    tree = cmuxq("tree", "--all", "--id-format", "both")
    ws_map = surface_ws_map_from_tree(tree)
    ws = ws_map.get(surf.upper(), "")
    caller_ws = ws_map.get((os.environ.get("CMUX_SURFACE_ID") or "").upper(), "")
    parent_entry = fs.live_get(e.get("parent") or "") or fs.archive_get(e.get("parent") or "")
    place = rs.place_of(e, parent_entry, ws_map=ws_map)
    siblings = []
    if ws:
        kinds = {s.upper(): (k, t) for s, _w, k, t in _iter_tree_surfaces(tree)}
        by_surface = {(v.get("surface") or "").upper(): lbl for lbl, v in fs.live_all().items()
                      if lbl != label}
        ps_out = rs.ps_sweep()                             # ONE sweep, shared across every bystander
        for s in rs.workspace_surfaces(ws, ws_map=ws_map):
            if s == surf.upper():
                continue
            kind, title = kinds.get(s, ("?", ""))
            siblings.append({"surface": s, "kind": kind, "title": title, "label": by_surface.get(s, ""),
                             "pids": sorted(rs.occupants(s, ps_out=ps_out))})
    return {"surface": surf, "workspace": ws, "tree": tree, "ws_map": ws_map, "caller_ws": caller_ws,
            "parent_ws": rs.workspace(fs.e_surface(parent_entry), ws_map=ws_map) if parent_entry else "",
            "plan": plan_seat_close(label, surf, ws, caller_ws, place, siblings)}


def _seat_residue(surf, ws=""):
    """What of a retired seat is STILL in `cmux tree` — ('surface', 'workspace', both, or none). ONE
    fresh tree read. The close is verified against the tree, never inferred from cmux's exit text."""
    tree = cmuxq("tree", "--all", "--id-format", "both")
    left = []
    if surf and surf.upper() in surface_ws_map_from_tree(tree):
        left.append("surface")
    if ws and ws.upper() in {w.upper() for w in _all_workspace_uuids(tree)}:
        left.append("workspace")
    return left


def _close_seat(label, e, verb, planned=None):
    """Retire `label`'s cmux seat, leaving NO residue: close its WORKSPACE when it owns one, else just
    its surface. Returns (ok, notes) -- ok=False means cmux residue SURVIVED the close, and the caller
    must fail loudly. `planned` is a seat_close_plan() bundle read BEFORE the agent was stopped (see its
    docstring); computed here if absent.

    EVERY CLOSE IS VERIFIED AGAINST A FRESH TREE. `cmuxq` returns cmux's stdout+stderr and no exit code,
    so a refusal reads exactly like a success: `fleet rm` on a workspace-placed agent hit the documented
    `invalid_state: Cannot close the last surface`, swallowed it, and printed "removed (closed + archived
    for recovery)" while the surface and its workspace sat untouched in the tree (cmux-advisor,
    reproduced live 2026-07-10 on `placeprobe`). A teardown that reports success while leaving residue is
    worse than one that refuses: the operator stops looking. Now the tree is re-read and disagreement is
    an error, not a note. Verifying the WORKSPACE (not just the surface) matters because an exec-delivered
    agent's surface leaves the tree with its process, so a surface-only check passes vacuously.

    Tombstones every surface it is about to close — a workspace close closes bystander surfaces too, and
    an untombstoned `surface.closed` frame makes the router fire a spurious "revive?" alert for a
    deliberate retirement.

    Callers MUST have written the registry row (archive_put) before calling: registry write precedes the
    cmux mutation it describes (agent-management v2 §2), so a close that half-fails degrades to
    "recorded, maybe-unresumable", never to "vanished"."""
    from . import state as fs
    planned = seat_close_plan(label, e) if planned is None else planned
    if not planned:
        return True, []
    surf, ws, plan = planned["surface"], planned["workspace"], planned["plan"]
    if not ws:
        # No tree (stubbed/unreadable cmux) or the surface was never locatable. Fall back to the bare
        # close-surface that predates this path: harmless on a closed surface, and the only thing we can
        # honestly attempt without knowing where the surface lives. Nothing to verify against.
        fs.expected_close_put(surf)
        cmuxq("close-surface", "--surface", surf)
        return True, []
    notes = []
    if plan["verb"] == "close-surface":
        for b in plan["blockers"]:
            notes.append(f"  workspace {ws[:8]} KEPT: {b}")
        fs.expected_close_put(surf)
        out = cmuxq("close-surface", "--surface", surf, "--workspace", ws)
        left = _seat_residue(surf)
        if left:
            notes.append(f"  !!! surface {surf[:8]} did NOT close -- cmux said: "
                         f"{out.strip()[:140] or '(nothing)'}")
            return False, notes
        return True, notes
    ok, note = _reanchor_group_off(ws, planned["tree"])
    if note:
        notes.append(f"  {note}")
    if not ok:
        fs.expected_close_put(surf)                        # the surface still goes; the workspace does not
        out = cmuxq("close-surface", "--surface", surf, "--workspace", ws)
        if _seat_residue(surf):
            notes.append(f"  !!! surface {surf[:8]} did NOT close either -- cmux said: "
                         f"{out.strip()[:140] or '(nothing)'}")
        return False, notes
    for s in plan["collateral"]:
        fs.expected_close_put(s["surface"])
        notes.append(f"  also closing {s['surface'][:8]} [{s['kind']}] {s['title'][:44]!r} (in this workspace)")
    fs.expected_close_put(surf)
    out = cmuxq("close-workspace", "--workspace", ws)
    left = _seat_residue(surf, ws)
    if left:
        notes.append(f"  !!! workspace {ws[:8]} did NOT close ({' + '.join(left)} still in `cmux tree`) -- "
                     f"cmux said: {out.strip()[:120] or '(nothing)'}")
        return False, notes
    notes.append(f"  closed workspace {ws[:8]} (no cmux residue)")
    fs.log_event("workspace-closed", label=label, workspace=ws, via=verb,
                 collateral=[s["surface"] for s in plan["collateral"]])
    return True, notes


def _report_seat_residue(label, verb, planned, notes):
    """Print the loud, actionable failure a surviving seat deserves. Returns rc 2."""
    surf = (planned or {}).get("surface", "")
    ws = (planned or {}).get("workspace", "")
    for n in notes:
        print(f"[fleet] {n}")
    print(f"\n[fleet] !!! {verb} {label}: the agent is STOPPED and ARCHIVED, but its cmux seat is STILL "
          f"OPEN. This is residue, not success.")
    print(f"[fleet]   surface   : {surf}")
    print(f"[fleet]   workspace : {ws or '(unresolved)'}")
    print(f"[fleet]   inspect + clean up by hand:")
    print(f"[fleet]     cmux tree --all --id-format both")
    if ws:
        print(f"[fleet]     cmux close-workspace --workspace {ws}     # closes the workspace AND its surfaces")
    print(f"[fleet]     cmux close-surface --surface {surf} --workspace {ws or '<ws>'}")
    from . import state as fs
    fs.log_event("seat_close_residue", label=label, surface=surf, workspace=ws, via=verb)
    return 2


def _direct_kill(surf, tool, log):
    """cmux-INDEPENDENT teardown of the old agent process(es): SIGINT x2 straight to every live,
    identity-checked pid on the surface (_signal_agent_pids). respawn-pane's OWN kill goes through cmux
    itself, so a wedged/unresponsive cmux can hang or silently no-op it; a raw os.kill doesn't care
    whether cmux is responsive. With live-only targeting this fallback is ALSO the ORPHAN REAPER: an
    agent respawn-pane abandoned on an old tty (live process, hook-store record still claiming this
    surface — the 2026-07-10 berg-sandbox orphan, pid 76142 on ttys003 after the pane moved to ttys001)
    is selected and cleanly SIGINT'd here, instead of wedging every subsequent recycle until a human
    kills it by hand. A dead/stale pid is never a target (the old form SIGINT'd bare _pid_for_surface
    with no aliveness check — corpse 70208 — while the orphan survived)."""
    if not _signal_agent_pids(surf, tool, log, "direct-kill fallback"):
        log("direct-kill fallback: no live agent pid on this surface; skipping straight to respawn")


def cmd_ls(argv):
    """Reconcile the live registry against cmux's hook store. Flags STALE = registry says live but the
    surface has no live session (a closed tab / crash never fires an archive transition). Scoped like
    every read: defaults `--scope mine` (you + your direct children); `--scope all` opens the whole
    fleet; `conductors`/`children` filter by kind. When `mine` is just you, a one-line hint points at
    `--scope all` so nobody mistakes their corner for the empty fleet. `--json` emits the reconciled
    rows (live + archived, with the computed status/lifecycle) as machine output."""
    from . import state as fs
    from . import features as ff
    from . import resolve as rs
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    scope_arg, _ = fs.pop_scope(argv, default=None)
    scope, caller = fs.read_scope(scope_arg, "ls")
    live, arch = fs.live_all(), fs.archive_all()
    if scope != "all":
        live = {l: v for l, v in live.items() if fs.scope_matches(scope, v, l, caller, include_self=True)}
        arch = {l: v for l, v in arch.items() if fs.scope_matches(scope, v, l, caller, include_self=True)}
    store = fs.read_hook_store()
    open_gates = ff._open_gate_uuids()                        # feed-gated needs-input (once for the listing)
    # reconcile ONCE -> render as JSON or the text table (identical status/lifecycle either way).
    # Reads route through resolve (step 1 of the v2 migration): rs.lifecycle/present/freshest are the
    # one resolver's names for the same canonical predicates (delegation keeps the fs.* test seams live).
    live_rows = []
    for label, v in sorted(live.items()):
        surf = v.get("surface", "")
        life = rs.lifecycle(surf)
        # classified fleet state (coarse - no transcript read here, so no review/done refine): an open Feed
        # gate -> needs-input; needsInput-no-gate -> ready; running -> working; else idle/pending/stale.
        _sid = fs.bare_uuid(rs.freshest(surf, st=store).get("sessionId", ""))
        state = ff._classify(life or "", bool(v.get("session")), "", open_gate=bool(_sid) and _sid in open_gates)
        # I4: a detached agent's lifecycle string is frozen, so every state derived from it is a lie.
        # Same rule as vitals (ff.detached_or), so the two views can never disagree about the axis.
        state = ff.detached_or(state, rs.attachment(surf, st=store)["attached"])
        # STALE if NO genuinely-live agent holds the surface: lifecycle terminal, OR frozen non-terminal
        # on a DEAD pid (the SessionEnd-less brick, root-caused 2026-07-06). Routed through the shared
        # liveness rule (rs.present) so the pid -- not the lifecycle string -- is the authority: a
        # dead 'running' ghost now reads STALE here (the "ls lies" symptom), consistent with bulk-recycle.
        if not rs.present(surf):
            # no live agent on the surface: PENDING = lazily-registered, not bound yet (codex binds
            # on its 1st turn -> drive it); STALE = had a session but the tab/process is gone.
            status = "pending" if not v.get("session") else "STALE"
        else:
            status = v.get("status", "live")
        live_rows.append({"label": label, "role": v.get("role"), "kind": v.get("kind"),
                          "state": state,
                          "status": status, "lifecycle": life or None, "surface": surf,
                          "muted": bool(v.get("muted"))})
    if as_json:
        arch_rows = [{"label": label, "role": v.get("role"), "kind": v.get("kind"),
                      "last_session": v.get("last_session") or None} for label, v in sorted(arch.items())]
        print(json.dumps({"scope": scope, "live": live_rows, "archived": arch_rows}, indent=2))
        return 0
    scope_tag = "" if scope == "all" else f"{scope}: "
    print(f"LIVE FLEET ({scope_tag}{len(live)}):  {'label':<24}{'role':<16}{'kind':<11}{'state':<12}{'status':<8}{'lifecycle':<11}surface")
    for r in live_rows:
        muted = "  MUTED" if r["muted"] else ""
        print(f"  {r['label']:<24}{(r['role'] or '-'):<16}{(r['kind'] or '-'):<11}{r['state']:<12}{r['status']:<8}{(r['lifecycle'] or '-'):<11}{r['surface'][:8]}{muted}")
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
    incident: dissolving a group the target only THOUGHT it belonged to swept 3 unrelated live agents).
    Once membership agrees, a CONFIRM GATE stops a mass-close: if the dissolve would take down LIVE
    collateral (any live agent besides the named target), it PREVIEWS the blast radius and REFUSES until
    --yes (alias --confirm) is passed -- a preview-then-confirm, not an interactive prompt, so it is safe in
    a non-interactive agent turn. A solo/target-only or all-dead group needs no --yes. A
    swept member's worktree dir and branch are left UNMANAGED: their registry rows are gone, so `fleet
    worktree clean` (which discovers from the registry) cannot find them. Reclaim manually with `git
    worktree list` + `git worktree remove <path>` (and `git branch -D fleet/<label>` if you want the
    branch gone). WITHOUT --with-group, only this agent's OWN seat goes (_close_seat: its workspace when
    it owns one, else just its surface) and the group survives -- re-anchored onto a surviving member
    first if this agent's workspace was the anchor."""
    from . import state as fs; import signal
    from . import resolve as rs
    from . import features as ff                              # turn_ended: the codex-aware turn-close signal
    kill = "--kill" in argv
    detach = "--detach" in argv
    force = "--force" in argv
    wipc = "--wip-commit" in argv
    with_group = "--with-group" in argv
    yes = "--yes" in argv or "--confirm" in argv          # confirm gate override for a mass-close dissolve
    args = [a for a in argv if a not in ("--kill", "--detach", "--force", "--wip-commit",
                                         "--with-group", "--yes", "--confirm")]
    if not args:
        sys.exit("usage: fleet rm <label> [--detach] [--force] [--kill] [--wip-commit] [--with-group [--yes]]")
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
    _lifecycle_owner_guard(label, "remove", force)           # item 9: another conductor's child needs --force
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
    # ...and whose transcript does NOT prove the turn already CLOSED. A finished codex agent sticks at
    # lifecycle=running forever (it fires no SessionEnd) though its rollout ends in task_complete — that is
    # done, not mid-turn, so a plain `rm` must work (the gap). turn_ended fails closed, so this only NARROWS
    # the refusal: a genuinely mid-turn agent (no terminal close / a task_started after) still refuses.
    if (closing and not force and rs.lifecycle(surf) == "running" and rs.has_live_pid(surf)
            and not ff.turn_ended((rs.freshest(surf) or {}).get("transcriptPath", ""))):
        sys.exit(f"[fleet] rm: '{label}' is mid-turn (lifecycle=running on surface {surf[:8]}). "
                 f"Use --force to close it anyway, or --detach to drop the registry row and leave "
                 f"the surface running.")
    # --with-group PREFLIGHT (recovery-safety #3): registry-vs-cmux membership cross-check, then a
    # list-what-dies CONFIRM GATE -- BOTH on pure reads, BEFORE any mutation (no tombstone, no close, no
    # sweep yet). A dissolve that would close LIVE collateral (any live agent besides the named target)
    # REFUSES without --yes: it prints the blast radius + the exact re-run and returns, touching nothing.
    # The cross-check (registry group can diverge from cmux's real group -- the 2026-07-02 root cause) still
    # ABORTS on any disagreement. Only after this passes do we tombstone + dissolve (below).
    grp = None
    seat_notes, seat_ok, planned = [], True, None
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
            # Ship 5c: rebuilt on cmux TRUTH (DESIGN-v2 §8). There is no stored `workspace` to trust anymore,
            # so each SEATED member's workspace is derived from the live TREE (rs.surface_ws_map). An
            # archived/parked row has NO surface -> it is not in any cmux workspace, so it does not participate
            # in the membership cross-check (it is swept by LABEL below, not by workspace). A seated member the
            # tree cannot locate is 'unverifiable' -> a mismatch (fail-closed). The set of seated members'
            # ACTUAL workspaces must equal cmux's real group membership (minus the Model-B anchor, below).
            ws_map = rs.surface_ws_map()
            registry_ws = {lbl: (ws_map.get((v.get("surface") or "").upper()) or "")
                           for lbl, v in registry_all.items() if v.get("surface")}
            unverifiable = sorted(lbl for lbl, ws in registry_ws.items() if not ws)
            real_ws = _group_member_workspaces(gref)
            # Model B (empty-anchor, ratified 2026-07-10): a group's anchor is an AGENTLESS scaffold
            # workspace that cmux ALSO lists inside member_workspace_refs. No registry row can occupy it
            # (no agent runs on the scaffold), so subtract it before the registry-vs-cmux membership
            # compare -- else EVERY Model-B group trips this abort (the anchor-flip regression the
            # 2026-07-10 live acceptance caught: registry={X-ws}, cmux={scaffold, X-ws} -> false
            # mismatch). The guard's REAL purpose is preserved: a genuine divergence among AGENT
            # workspaces still aborts, as do unverifiable rows (registry agent with no workspace) and
            # unreadable cmux data. An unresolvable/absent anchor leaves real_agents==real_ws (fail-closed:
            # a Model-A group, or contract drift, still aborts if its full membership disagrees).
            anchor_ws = _group_anchor_workspace(gref)
            real_agents = (real_ws - {anchor_ws}) if (real_ws is not None and anchor_ws) else real_ws
            if real_ws is None or unverifiable or set(registry_ws.values()) != real_agents:
                real_display = (sorted(real_agents) if real_agents is not None
                                else "UNREADABLE (cmux group data unavailable)")
                anchor_note = (f"  (excl. Model-B anchor scaffold {anchor_ws[:8]})"
                               if anchor_ws and real_ws is not None else "")
                sys.exit(
                    f"[fleet] ABORT --with-group: refusing to dissolve '{gname}' ({gref}) -- registry and "
                    f"cmux disagree about membership (this is a registry-integrity bug, not a --force case; "
                    f"see Item 2, 2026-07-02 incident).\n"
                    f"[fleet]   registry believes group '{gname}' = {sorted(registry_ws)}"
                    + (f"  (workspace id unknown for: {', '.join(unverifiable)} -- can't verify, treated as a "
                       f"mismatch)" if unverifiable else "") + "\n"
                    f"[fleet]   cmux reports group '{gref}' agent workspaces = {real_display}{anchor_note}\n"
                    f"[fleet] no dissolve, no sweep happened. Investigate before retrying "
                    f"(`fleet ls`, `cmux workspace-group list --json`).")
            # AGREEMENT confirmed. HARD GUARDS next — ABSOLUTE refusals (rc 1, zero signals, nothing
            # closed, registry untouched), deliberately BEFORE the --yes confirm gate so no preview
            # ever implies these can proceed, and NOT bypassable by --force/--yes (those force the
            # quiet gate and the mass-close preview; never never-orphan, never these). Post-G the stop
            # loop SIGNALS every member, so a group containing the caller or a bystander conductor
            # turned the old leak into a KILL — live shape 2026-07-10: berg-sandbox (conductor) shares
            # a group with homelab + resume-research, so `rm homelab --with-group --yes` would have
            # SIGINT'd the conductor; run FROM berg-sandbox it would have SIGINT'd its OWN pid
            # mid-dissolve (the fleet process is a grandchild — it survives the caller and completes
            # the teardown with no refusal and no clean error). The confirm-gate preview does show
            # [CONDUCTOR, live], but seeing is not stopping, and it never reveals that the CALLER is
            # among the dead. Bulk recycle already skips self; these close the same hole here.
            caller_surf = (os.environ.get("CMUX_SURFACE_ID") or "").upper()
            if caller_surf:
                selfhit = sorted(lbl for lbl, v in registry_all.items()
                                 if (v.get("surface") or "").upper() == caller_surf)
                if selfhit:
                    who = ", ".join("{} (kind={})".format(l, registry_all[l].get("kind") or "?")
                                    for l in selfhit)
                    print(f"[fleet] rm --with-group REFUSED: group '{gname}' contains the CALLER's own "
                          f"surface — {who}. A dissolve never signals or tears down its own caller "
                          f"(zero signals fired, nothing closed, registry untouched). Run this from "
                          f"OUTSIDE the group (a peer conductor or a plain shell).")
                    fs.log_event("rm_refused", label=label, group=gname,
                                 reason="group-contains-caller", blocked=selfhit)
                    return 1
            bystanders = sorted(lbl for lbl, v in registry_all.items()
                                if lbl != label and v.get("kind") == "conductor")
            if bystanders:
                who = ", ".join(f"{l} (kind=conductor)" for l in bystanders)
                print(f"[fleet] rm --with-group REFUSED: group '{gname}' contains a CONDUCTOR that is "
                      f"not the named target — {who}. A child's group dissolve never takes a conductor "
                      f"as collateral (zero signals fired, nothing closed, registry untouched). To "
                      f"retire a conductor's whole group, name the CONDUCTOR as the target: "
                      f"fleet rm {bystanders[0]} --with-group")
                fs.log_event("rm_refused", label=label, group=gname,
                             reason="group-conductor-collateral", blocked=bystanders)
                return 1
            # CONFIRM GATE: a dissolve is a mass-close. Refuse (preview only) whenever
            # it would take down LIVE collateral -- any live agent OTHER than the named target -- unless
            # --yes. A solo/target-only or all-dead group proceeds with no gate (no surprise to confirm).
            def _live(v):
                return rs.present(v.get("surface", ""))
            live_collateral = sorted(lbl for lbl, v in members.items() if _live(v))
            if live_collateral and not yes:
                print(f"[fleet] CONFIRM --with-group: dissolving '{gname}' ({gref}) is a MASS-CLOSE -- it "
                      f"closes {1 + len(members)} surface(s), incl. {len(live_collateral)} LIVE agent(s) "
                      f"besides '{label}'. What dies:")
                for lbl in sorted(registry_all):
                    v = registry_all[lbl]
                    tags = (["CONDUCTOR"] if v.get("kind") == "conductor" else []) \
                        + (["live"] if _live(v) else ["stale"]) \
                        + (["<- named target"] if lbl == label else [])
                    print(f"[fleet]     {lbl:26} {(v.get('role') or '?'):20} [{', '.join(tags)}]")
                rerun = "fleet rm " + " ".join(
                    [label] + [f for f in ("--with-group", "--kill", "--force", "--wip-commit") if f in argv]
                    + ["--yes"])
                print(f"[fleet] NOTHING closed. This op needs explicit confirmation -- re-run with --yes:\n"
                      f"[fleet]     {rerun}")
                return 3
            wt_kept = sorted([lbl for lbl, v in members.items() if v.get("worktree")]
                             + ([label] if e.get("worktree") and not kill else []))
            grp = {"gname": gname, "gref": gref, "members": members,
                   "registry_all": registry_all, "wt_kept": wt_kept}
        else:
            grp = {"gname": gname, "gref": "", "members": {}}
    if closing:
        fs.expected_close_put(surf)                     # tombstone BEFORE any close: mark this a DELIBERATE
                                                        # retirement so the router won't mis-read the
                                                        # surface.closed frame as an accidental external
                                                        # close and fire a spurious stale "revive?" alert
    group_note = ""
    if grp is not None:
        gname, gref = grp["gname"], grp["gref"]
        if gref:
            members, registry_all, wt_kept = grp["members"], grp["registry_all"], grp["wt_kept"]
            # STOP every member's agent BEFORE the dissolve — ALL-OR-NOTHING (never-orphan at group
            # scale): `workspace-group delete` closes EVERY member surface, and close-surface does not
            # kill the pane's agent, so a dissolve without stops leaks every live member at once (the
            # 2026-07-10 single-seat leak, multiplied — and a group dissolve is exactly when several
            # live agents go at once with nobody watching each one). Two phases:
            #   1. PRE-FLIGHT identity check across the WHOLE group: any live pid that doesn't identify
            #      as its member's tool (pid reuse / ps failure) refuses the dissolve with ZERO signals
            #      fired — we never SIGINT half a group and then discover a foreign pid.
            #   2. Stop each member (live-only, identity-checked, death-verified). Any survivor refuses
            #      the WHOLE dissolve: no group delete, no sibling closes, no registry change. A partial
            #      dissolve that strands one agent while tearing down its neighbours is the worst
            #      outcome — the survivor loses its group context AND stays invisible. Members already
            #      stopped by phase 2 are dead processes on OPEN surfaces: visible, revivable, nothing
            #      lost. --force does not bypass any of this (it forces the quiet gate, never
            #      never-orphan).
            unidentifiable = []
            for lbl in sorted(registry_all):
                v = registry_all[lbl]
                ms = v.get("surface", "")
                for mp in sorted(_surface_pids(ms)) if ms else []:
                    if not _agent_pid_check(mp, v.get("tool") or "claude"):
                        unidentifiable.append((lbl, mp))
            if unidentifiable:
                who = ", ".join(f"{lbl} (pid {mp})" for lbl, mp in unidentifiable)
                print(f"[fleet] rm --with-group REFUSED: live pid(s) on '{gname}' member surface(s) do "
                      f"not identify as their agent tool — {who}. NOT dissolving (zero signals fired, "
                      f"no surface closed, registry untouched). Check the pid(s), then re-run.")
                fs.log_event("rm_refused", label=label, group=gname,
                             reason="group-member-pid-unidentifiable",
                             blocked=[lbl for lbl, _ in unidentifiable])
                return 1
            blocked = []
            for lbl in sorted(registry_all):
                v = registry_all[lbl]
                ms = v.get("surface", "")
                if not ms:
                    continue                             # archived row with no seat -> nothing to stop
                ok, note = _stop_agent_for_close(ms, v.get("tool") or "claude", lbl, "rm --with-group")
                if not ok:
                    blocked.append((lbl, note))
            if blocked:
                print(f"[fleet] rm --with-group REFUSED: {len(blocked)} member(s) of '{gname}' still "
                      f"have a live agent after SIGINT x2; NOT dissolving (no group delete, no surface "
                      f"closed, registry untouched — already-stopped members sit dead on OPEN surfaces, "
                      f"revivable). Blocking member(s):")
                for lbl, note in blocked:
                    print(f"[fleet]     {lbl}: {note}")
                fs.log_event("rm_refused", label=label, group=gname,
                             reason="group-member-would-orphan", blocked=[lbl for lbl, _ in blocked])
                return 1
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
        # read the seat's topology BEFORE the stop (exec delivery closes the surface with the process)
        planned = seat_close_plan(label, e_live)
        # STOP the agent(s) FIRST — live-only, identity-checked targets (_stop_agent_for_close), and
        # REFUSE the whole removal if a live agent won't die or can't be identified: close-surface does
        # NOT reliably kill the pane's agent, so closing over a survivor strands it invisibly (the
        # 2026-07-10 leak — the old code SIGINT'd a stale _pid_for_surface pick and closed anyway).
        # Refusing BEFORE any registry mutation keeps the seat live, visible, and re-runnable.
        ok, note = _stop_agent_for_close(surf, e.get("tool") or "claude", label, "rm")
        if not ok:
            print(f"[fleet] rm {label} REFUSED: {note}")
            fs.log_event("rm_refused", label=label, surface=surf, reason="live-agent-would-orphan")
            return 1
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
        # WRITE-ORDER FLIP (Ship 5b, registry-before-close): drop the LIVE row BEFORE _close_seat closes the
        # surface, so the router's fresh surface.closed read finds no live member and skips -- no duplicate
        # archive, no spurious stale alert. archive_put ran first, so the agent is durably parked before its
        # row leaves the live store. (The trailing `fs.live_del` below is then a harmless no-op on this
        # path; it still does the removal on the detach/non-closing paths. The expected-close tombstone is a
        # redundant belt now, deleted once this ordering has soaked -- 5b step 3.)
        fs.live_del(label)
        # close the SEAT, not just the surface: a workspace-placed agent's workspace goes too, so `rm`
        # leaves no named husk workspace in the sidebar (Berg's ruling). Guards + verb choice live in
        # _close_seat/plan_seat_close; a tab/pane agent still only loses its own surface.
        seat_ok, seat_notes = _close_seat(label, e_live, "rm --with-group" if with_group else "rm",
                                          planned=planned)
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
    if not seat_ok:                                     # "removed (closed + archived)" over a surface that
        return _report_seat_residue(label, "rm", planned, seat_notes)   # never closed was THE lie. Not 0.
    for n in seat_notes:
        print(f"[fleet] {n}")
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
        from . import resolve as rs
        live = fs.live_get(a.label)
        # refuse only if a GENUINELY-live agent holds the surface (non-terminal lifecycle AND a live pid).
        # A dead-pid frozen 'running' ghost (the SessionEnd-less brick, 2026-07-06) must NOT block worktree
        # teardown -- there is no live work to protect. surface_has_live_agent is the shared authority.
        if info["where"] == "live" and live and rs.surface_has_live_agent(live.get("surface", "")):
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
                              # `provider` (fix 1): the account recorded at launch, carried so a REVIVE can
                              # compare against the re-resolved account and warn loudly when it moved (else
                              # the archived row has no provider and the move-warn can never fire).
                              "plugins", "flags", "settings", "group", "worktree", "provider") if k in e}
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


def _notify_parent_lifecycle(parent, label, verb, invoker, refused):
    """Best-effort inbox note to a child's parent that ANOTHER conductor touched their child (item 9).
    Needs the parent LIVE to receive the row (inbox is surface-addressed); a parked/offline parent simply
    misses it. Rendered as a `peer` row so it shows in the parent's `fleet inbox` like any peer message."""
    from . import state as fs
    import secrets
    psurf = fs.surface_for_label(parent)
    if not psurf:
        return
    body = (f"[lifecycle] {invoker} "
            + (f"TRIED to {verb}" if refused else f"{verb}d")
            + f" your child '{label}'"
            + (" (REFUSED -- they'd need --force)." if refused else " (forced through --force)."))
    msg_id = secrets.token_hex(3)
    fs.inbox_put("peer", psurf, {
        "ptype": "peer-msg", "to_label": parent,
        "from_surface": os.environ.get("CMUX_SURFACE_ID", ""), "from_label": invoker,
        "msg_id": msg_id, "reply_to": None, "reply_expected": False, "body": body,
    }, event_key=f"lifecycle:{verb}:{label}:{'refused' if refused else 'done'}:{invoker}")


def _lifecycle_owner_guard(label, verb, force):
    """Item 9 cross-conductor guard for archive/rm. A DIFFERENT identified conductor (invoker != the
    target's registry parent) acting on another conductor's child is REFUSED without --force, and the
    parent is notified EITHER WAY (refused attempt or forced-through). No-op when the target is top-level
    (no parent), when the invoker IS the parent, when the invoker IS the target itself (self-removal / a
    call from the target's own surface), or when there's no identified invoker surface at all -- an
    operator driving the CLI directly (no $CMUX_SURFACE_ID, e.g. Berg) keeps full unguarded control.
    `verb` is 'archive' | 'remove' (used in the messages)."""
    from . import state as fs
    parent = fs.parent_of(label)
    invoker = fs.label_for_surface(os.environ.get("CMUX_SURFACE_ID", "")) or ""
    if not parent or not invoker or invoker == parent or invoker == label:
        return
    _notify_parent_lifecycle(parent, label, verb, invoker, refused=not force)
    if not force:
        sys.exit(f"[fleet] {verb} REFUSED: '{label}' is {parent}'s child, not yours. Pass --force to "
                 f"{verb} another conductor's child ({parent} has been notified of the attempt).")


def cmd_archive(argv):
    """Park a live agent: stop its process(es) (SIGINT x2 to every live identity-checked pid = clean
    TUI exit), close its cmux seat, move it to the archive shelf with enough to `claude --resume` it
    later. REFUSES — registry untouched, surface left open — if a live agent on the surface won't die or
    can't be identified: closing over a survivor strands it invisibly (the 2026-07-10 leak class).

    "Close its seat" is _close_seat, not `close-surface`: an agent that OWNS a workspace has its
    workspace closed too, so archiving leaves no husk surface and no empty named workspace in the
    sidebar (Berg's ruling, 2026-07-10). A tab/pane agent only loses its surface — its parent's
    workspace survives."""
    from . import state as fs
    force = "--force" in argv
    args = [a for a in argv if a != "--force"]
    if not args:
        sys.exit("usage: fleet archive <label> [--force]")
    label = args[0]
    e = fs.live_get(label)
    if not e:
        sys.exit(f"fleet archive: no LIVE label '{label}'")
    _lifecycle_owner_guard(label, "archive", force)          # item 9: another conductor's child needs --force
    surf = e.get("surface", "")
    # capture cmux's GROUND-TRUTH launch binding BEFORE we tear the surface down — this is the same
    # source recycle replays, so revive can recompose the EXACT last command (caller passthrough +
    # post-launch overrides included) instead of the lossy registry-spec snapshot. The binding lives
    # on the surface; once close-surface runs it's gone, so read it first.
    b = _resume_binding(surf) if surf else {}
    # decide the seat teardown BEFORE the stop: an exec-delivered agent's surface leaves the tree the
    # instant its process dies, taking with it the only evidence of which workspace it owned.
    planned = seat_close_plan(label, e) if surf else None
    notes = []
    if surf:
        ok, note = _stop_agent_for_close(surf, e.get("tool") or "claude", label, "archive")
        if not ok:
            print(f"[fleet] archive {label} REFUSED: {note}")
            fs.log_event("archive_refused", label=label, surface=surf, reason="live-agent-would-orphan")
            return 1
    # registry BEFORE the cmux mutation it describes (v2 §2): a seat close that half-fails must degrade
    # to "recorded, maybe-unresumable", never to "vanished".
    fs.archive_put(label, _build_archive_entry(e, b))
    # WRITE-ORDER FLIP (Ship 5b, registry-before-close): drop the LIVE row BEFORE the surface closes, so the
    # router's fresh-read surface.closed handler finds no live member for this surface and skips -- no
    # duplicate archive, no spurious `kind='stale'` "revive?" alert. archive_put ran first, so the agent is
    # durably parked before its row leaves the live store (degrades to "recorded", never "vanished"). The
    # expected-close tombstone _close_seat still stamps is a redundant belt now -- deleted once this
    # ordering has soaked on staging (5b step 3).
    fs.live_del(label)
    seat_ok = True
    if surf:
        seat_ok, notes = _close_seat(label, e, "archive", planned=planned)
    fs.log_event("archived", label=label, role=e.get("role"), session=e.get("session"))
    print(f"[fleet] archived {label} (session {e.get('session')}); revive with: fleet revive {label}")
    if not seat_ok:                                     # the row is written and the agent is dead; the
        return _report_seat_residue(label, "archive", planned, notes)   # seat is not. Never call that 0.
    for n in notes:
        print(f"[fleet] {n}")
    return 0


def cmd_revive(argv):
    """Bring a parked agent back into a fresh surface. Default RESUMES its last session (--fresh sheds it
    into a new session, auto-primed from the handover; --session targets an arbitrary prior one). Binding-
    first, like recycle: if archive captured cmux's launch binding, REPLAY it (--resume swapped to the
    parked session, caller `-- <flags>` / --plugin re-layered on top). Falls back to the registry-spec
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
    ap.add_argument("--plugin", action="append", default=[], metavar="NAME",
                    help="union a plugin into this identity (repeatable or comma-sep; routed through the index)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    add_plugins = _flatten_csv(a.plugin)
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
            "plugins": _dedup(list(e.get("plugins", [])) + add_plugins),
            "flags": e.get("flags", []), "env": {}, "settings": e.get("settings", "")}
    spec["abs_cwd"] = spec["cwd"] if os.path.isabs(spec["cwd"]) else os.path.join(ROOT, spec["cwd"])
    if not spec["cwd"]:
        print(f"[fleet] warn: revive {a.label} resolved no cwd (sparse shelf, not a roster role, no "
              f"binding) -> abs_cwd falls back to ROOT root; claude --resume may not find the session")
    # inherit the placement parent's workspace group like `launch --place workspace` (item 9): a
    # place=workspace agent revived with no group of its own joins its parent's group (a conductor anchors
    # its OWN named group). The visual group follows the placement parent (a.parent), same as launch.
    if spec["place"] == "workspace" and not spec["group"]:
        if spec["kind"] == "conductor":
            spec["group"] = spec["label"]
        elif a.parent:
            pe = fs.entry_for_surface(a.parent)
            if pe and pe.get("group"):
                spec["group"] = pe["group"]

    # ACCOUNT re-resolution (fix 1): a revive FOLLOWS CONFIG for the account too, identically to recycle —
    # the archived binding dropped the account env, so re-resolve + inject it here (and record it on the
    # registry row via spec["provider"] so `fleet usage` attributes the revived agent correctly). Aborts on
    # an unresolvable/unreadable account; a moved default prints the loud change warn.
    pr, provider_announce, provider_warn = _resolve_recycle_provider(tool, e.get("role"), e.get("provider", ""))
    if pr:
        spec["provider"] = pr["label"]

    binding_argv = _binding_argv(e.get("binding_cmd", ""))
    if _is_roster(e.get("role")):                                 # ROSTER -> re-resolve the toml (truth)
        # RESUME pins the archived (original) cwd so the session is findable; FRESH adopts the toml cwd.
        send_cmd = _compose_from_roster(e.get("role"), tool, a.label, caller, add_plugins, sess,
                                        cwd_override=(cwd if sess else ""), provider=pr)
        source = "toml"
    elif binding_argv:                                            # AD-HOC: replay the captured binding
        cwd = e.get("binding_cwd") or spec["cwd"]
        send_cmd = _replay_binding_argv(binding_argv, tool, spec["role"], a.label, cwd,
                                        caller, add_plugins, sess, provider=pr)   # _prepend_resume gates per tool
        source = "binding"
    else:                                                         # registry-spec fallback
        codex_home = (pr.get("env") or {}).get("CODEX_HOME") if pr else None
        bin_name, args, env = adapter_compile(tool, spec, caller, codex_home=codex_home)
        args = _prepend_resume(args, tool, sess)                  # claude --resume flag | codex resume subcmd
        if sess and tool not in ("claude", "codex"):
            print(f"[fleet] note: tool '{tool}' has no resume in this flow; fresh launch")
        env, args, raw_env = _apply_provider(env, args, pr)       # fix 1: inject the re-resolved account env
        send_cmd = render_send_cmd(bin_name, args, env, spec["abs_cwd"], raw_env)
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
    if provider_announce:
        print(provider_announce)                                 # fix 1: tool:account (+ note), like launch
    if provider_warn:
        print(provider_warn)                                     # fix 1: LOUD warn when the account moved
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
    # PRE-SPAWN account-token guard (fix 1), mirroring cmd_launch / recycle: refresh the account token
    # before minting a surface so a revived seat never spawns into a dead/revoked token (inert in today's
    # codex-home model — resolve_launch sets no needs_refresh — but wired for parity). Never dry-run here.
    if pr and pr.get("needs_refresh"):
        from . import providers as pv
        try:
            pv.codex_ensure_fresh(pr["needs_refresh"])
        except pv.ProviderError as ex:
            sys.exit(f"[fleet] revive ABORT: account token refresh failed for '{pr['needs_refresh']}' "
                     f"({ex}); re-login the account, then re-run fleet revive.")
    from . import resolve as rs
    _pc = _count_plugin_dirs(send_cmd)

    def _seat(w, s):
        """Deliver + resume onto surface `s`, returning the bound sid ('' if it never bound). The per-verb
        half of the shared dark-surface guard — revive's delivery is a resume through the summary menu."""
        _deliver_launch(w, s, send_cmd)          # exec by default (step 2); paste under the soak flag
        # full-resume: dismiss the summary menu, and GATE the bind on it clearing. The menu blocks the
        # session bind, so binding behind an undismissed menu would register nothing and leave the agent
        # live-but-UNREGISTERED (invisible to `fleet ls`, still shown archived).
        if not _resume_and_gate(s, send_cmd, tool, sess, lambda m: print(f"[fleet] {m}")):
            sys.exit(f"[fleet] ABORT: resume-summary menu never resolved for {a.label} (surface still "
                     f"booting or wedged at the menu); NOT registering. Re-run `fleet revive {a.label}`.")
        # scale the post-menu bind poll by loadout (recovery-safety #9): after the menu clears, a heavy boot
        # (5 plugins + memsearch onnx embedding load — the ~100s+ tail that made revive time out) can run
        # past a flat 60s. Same plugin-count heuristic the menu ceiling uses, with a higher bind ceiling.
        bound = poll_session(s, timeout=_resume_menu_timeout(_pc, base=60, per_plugin=8, ceiling=180))
        # (0.64.18+ heals the session-misfile class natively via the live-identity healing upsert, so the
        # old adopt-misfiled fallback is retired — the seated surface is authoritative.)
        return bound

    ws, surf = create_surface(spec, a.parent, "down")
    if not ws or not surf:
        sys.exit(1)
    sid = _seat(ws, surf)
    if not sid:
        # nothing bound and nothing adoptable: the agent may still be booting. The surface is LIVE and is
        # NEVER torn down, so it is adoptable the instant it binds. The label stays PARKED (archive_del has
        # not run), so the command is re-runnable.
        sys.exit(f"[fleet] timed out waiting for session binding on surface {surf} (plugin_count={_pc}); the "
                 f"agent is likely still booting. It is NOT torn down and '{a.label}' stays parked. Adopt it "
                 f"once it binds:\n[fleet]     fleet register {a.label} --surface {surf}\n"
                 f"[fleet]   (inspect: cmux capture-pane --surface {surf})")
    # (dark-surface heal is native since 0.64.18 — the old reseat-if-dark guard is retired; the seated
    # surface is authoritative.)
    sid = _resume_binding(surf).get("checkpoint_id", "") or sid   # ground-truth session over a bridge poll id
    # PRESERVE the reporting relationship across the round trip (item 9): the revived agent keeps its
    # ARCHIVED parent, not whoever revived it. a.parent still drives PLACEMENT (surface + group), but the
    # registry parent comes from the shelf -- a top-level agent (archived parent None) stays top-level.
    register(surf, spec, a.parent, sid, ws, parent_label=e.get("parent") or None)
    fs.archive_del(a.label)
    fs.log_event("revived", label=a.label, role=spec["role"], surface=surf, session=sid, fresh=a.fresh,
                 # ledger parity with log_launch/recycled: ground-truth effort/model off the composed
                 # command; plugins deterministic from the entry + --plugin union (already in spec).
                 effective={**_sendcmd_session_prefs(send_cmd), "plugins": spec["plugins"]})
    if a.fresh:                                                   # shed -> prime from the handover (like a fresh recycle)
        ho = _latest_handover(spec["abs_cwd"], a.label)
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
    from . import resolve as rs
    d = _store()
    ae = rs.active_entry(surf, st=d)                  # cmux's 'bound right now' pointer entry
    if ae.get("sessionId"):
        s = rs.record_by_session(ae["sessionId"], st=d)
        if s:
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
    from . import resolve as rs
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
    # rs.workspace reads the live tree first; rec.workspaceId is only the last-ditch fallback.
    ws = rs.workspace(surf) or rec.get("workspaceId") or ""
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
    # `register` adopts a surface that ALREADY EXISTS, so it can never CREATE a dark one — but it will
    # happily register one and print DONE, which is how a live agent becomes a row nobody can see. It must
    # NOT re-seat: this is the manual escape hatch, the agent is already working, and a re-seat here would
    # destroy the very context the operator ran `register` to rescue. So: WARN, and name the remedy.
    # (A heuristic may PREVENT a destruction; it may never AUTHORIZE one — and this alarm authorizes nothing.)
    if rs.dark(surf, tool):
        print(f"\n[fleet] WARNING: {label} is alive on {surf[:8]} but cmux is NOT filing it there — the row "
              f"is registered and drivable, and it will NOT appear in `vitals`/`ls`/the sidebar.")
        print(f"[fleet]   Nothing is wrong with the agent; it is invisible, not broken. To restore it: "
              f"`fleet archive {label}` then `fleet revive {label}` (NOT `recycle` — that re-execs onto "
              f"this same dark surface).")
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
    from . import resolve as rs
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
        my_ws = rs.workspace(self_surf) or self_entry.get("workspace") or ""
        if not my_ws:
            sys.exit(f"[fleet] group init: cannot resolve {self_label}'s current workspace from cmux.")
        gref = _group_ref(name)
        if gref:                                            # group already exists -> just (re)record it
            fs.live_put(self_label, {**self_entry, "group": name, "place": "workspace"})
            fs.log_event("group-init", label=self_label, via="existing", group=name)
            print(f"[fleet] group '{name}' ({gref}) already exists; recorded on {self_label}. "
                  f"Children launched with --place workspace (no --group) now join it.")
            return 0
        # bootstrap (Model B, empty-anchor, ratified 2026-07-10): create the group from MY ws, then KEEP
        # the scaffold cmux mints as the EMPTY anchor and title it 'Conductor - <label>'. My workspace
        # stays an ordinary MEMBER. The old model re-anchored onto my workspace and closed the scaffold --
        # that rendered the conductor as a bare folder shim and forced its title to the group name.
        cmuxq("workspace-group", "create", "--name", name, "--from", my_ws)   # ALWAYS explicit --from
        gref = _group_ref(name)
        if not gref:
            sys.exit(f"[fleet] group init: `workspace-group create` did not register a group named "
                     f"'{name}'. No registry change. Inspect: cmux workspace-group list --json")
        for n in _title_group_anchor_scaffold(gref, my_ws, self_label)[1]:
            print(f"[fleet] group init: {n}")
        fs.live_put(self_label, {**self_entry, "group": name, "place": "workspace"})
        fs.log_event("group-init", label=self_label, via="bootstrap", group=name)
        print(f"[fleet] group '{name}' ({gref}) created; {self_label} joined as a member under the empty "
              f"'Conductor - {self_label}' anchor. Children launched with --place workspace (no --group) "
              f"now join it.")
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
    child_ws = rs.workspace(child.get("surface", "")) or child.get("workspace") or ""
    if not child_ws:
        sys.exit(f"[fleet] group add: cannot resolve {a.label}'s current workspace from cmux.")
    cmuxq("workspace-group", "add", "--group", gref, "--workspace", child_ws)   # SAFE: no surface move
    fs.live_put(a.label, {**child, "group": name, "place": "workspace", "workspace": child_ws})
    fs.log_event("group-add", label=a.label, via="workspace-group-add", group=name)
    print(f"[fleet] added {a.label} (workspace {child_ws[:8]}) to group '{name}' ({gref}); "
          f"surface preserved -- {a.label} stays live.")
    return 0


def cmd_move(argv):
    """Relocate a child into another workspace — NATIVELY (cmux 0.64.18+ heals the moved surface).

      fleet move <label> (--to-workspace <ws> | --own-workspace) [--name TITLE]

    Moving a live surface across workspaces USED to permanently darken its agent-status registration
    inside the cmux app, so this verb refused a live agent and offered `--archive-revive` (park + revive
    onto a FRESH surface) as the only safe relocation. cmux 0.64.18 FIXED that natively: the live-identity
    healing upsert (agent.resolve_delivery_target) promotes the live pid/surface identity over the
    persisted record and re-stamps the moved surface, so a LIVE agent survives a relocation with its
    registration intact (e2e-verified — conformance ROW 9 / fleet-0.64.18-verdicts.md, and the live
    relocation test in this build).

    So a move is now a SURFACE MOVE + a REGISTRY UPDATE — never an archive, a revive, a fresh surface, or
    any teardown. The agent keeps its pid, session, context, surface UUID, and — critically — its PARENT
    and GROUP. The old archive-revive path LOST those and split-brained a live rehome (cf-conductor,
    2026-07-15: rehomed agents read parent=None/group=None); the clean repair was `cmux workspace-group
    add`, which is exactly what this native path does. Re-grouping is surface-preserving, not a move.

    An ARCHIVED label has no surface, so there is nothing to relocate — bring it back into the target with
    `fleet revive <label>` instead. Bystanders are safe: a surface move never touches sibling surfaces."""
    from . import state as fs
    from . import resolve as rs
    ap = argparse.ArgumentParser(prog="fleet move", add_help=True)
    ap.add_argument("label", help="the child to relocate")
    ap.add_argument("--to-workspace", default="", metavar="WS",
                    help="target workspace UUID or workspace:<n> ref (must already exist)")
    ap.add_argument("--own-workspace", action="store_true",
                    help="relocate the child into a FRESH workspace (joins the conductor's group if one exists)")
    ap.add_argument("--name", default="", help="title for the new workspace (--own-workspace; default: label)")
    a = ap.parse_args(argv)
    if bool(a.to_workspace) == bool(a.own_workspace):
        sys.exit("[fleet] move: pass exactly one of --to-workspace <ws> | --own-workspace.")

    child = fs.live_get(a.label)
    parked = fs.archive_get(a.label) if not child else None
    if not child and not parked:
        sys.exit(f"[fleet] move: '{a.label}' is neither a live registry member nor on the archive shelf.")

    # An ARCHIVED label has no surface — there is nothing to relocate. Where it lands is decided when it
    # comes back, so bring it back INTO the target with `fleet revive` instead of moving.
    if parked:
        sys.exit(f"[fleet] move: '{a.label}' is ARCHIVED — it has no surface, so there is nothing to "
                 f"move. Bring it back INTO the target instead:\n"
                 f"[fleet]     fleet revive {a.label}"
                 + ("   (--place workspace lands it on its own workspace)" if a.own_workspace else ""))

    surf = child.get("surface", "")
    if not surf:
        sys.exit(f"[fleet] move: '{a.label}' has no surface recorded; cannot move.")
    cur_ws = rs.workspace(surf)                                # pre-move: the 2s memo is fine (nothing has moved yet)
    if not cur_ws:
        sys.exit(f"[fleet] move: surface {surf[:8]} for '{a.label}' is not in cmux's tree (already "
                 f"closed?). Use `fleet revive {a.label}` to relaunch, not move.")

    # RE-PARENT to the caller. Running `fleet move <child>` FROM a conductor is an ownership assertion:
    # the child's registry parent + group follow the mover — this is how a rehome works, as a registry +
    # cmux update, never the old archive-revive teardown (the path that split-brained cf-conductor's rehome
    # to parent=None/group=None). A move with no resolvable caller conductor (a human shell / CI) preserves
    # the child's existing parent + group.
    caller_label = fs.label_for_surface(os.environ.get("CMUX_SURFACE_ID", "")) or ""
    caller = fs.live_get(caller_label) if caller_label else None
    new_parent = (caller_label if (caller and caller.get("kind") == "conductor" and caller_label != a.label)
                  else child.get("parent"))
    parent_entry = fs.live_get(new_parent or "") or {}

    # resolve the target BEFORE the move, so a bad target aborts with nothing touched.
    target_uuid, same_ws = "", False
    if a.to_workspace:
        target_uuid = (a.to_workspace if _looks_like_uuid(a.to_workspace)
                       else _ref_to_uuid("workspace", a.to_workspace))
        if not target_uuid:
            sys.exit(f"[fleet] move: could not resolve --to-workspace '{a.to_workspace}' to a workspace "
                     f"(pass a UUID or a workspace:<n> ref). Inspect: cmux list-workspaces")
        same_ws = target_uuid.upper() == cur_ws.upper()

    # A move is RELOCATE (the surface) + REPARENT (the org chart), DECOUPLED. `--own-workspace` and a
    # cross-ws `--to-workspace` both relocate; a same-ws `--to-workspace` is a PURE REPARENT — the surface
    # stays put, but parent + group still follow the mover. Decoupling is what makes `fleet move <child>
    # --to-workspace <its-own-ws>` from a NEW conductor a valid ownership assertion instead of the old no-op
    # short-circuit (post-thin-registry there is no stored workspace left to "reconcile").
    relocate = a.own_workspace or (a.to_workspace and not same_ws)

    if relocate:
        # NATIVE RELOCATION — safe whether or not an agent is live on the surface. cmux 0.64.18+ heals the
        # moved surface's agent-status registration, so no live-agent refusal, no archive, no revive, no
        # fresh surface. The surface UUID is preserved, so the agent keeps its pid, session and context.
        # Suppress the spurious archive: a cross-workspace move emits surface.closed (root cause #3), which
        # the router would otherwise read as an accidental external close and archive the still-live member.
        fs.expected_close_put(surf)
        if a.to_workspace:
            cmuxq("move-surface", "--surface", surf, "--workspace", target_uuid, "--focus", "false")
        else:
            cmuxq("move-tab-to-new-workspace", "--surface", surf, "--title", a.name or a.label,
                  "--focus", "false")

        # CONFIRM the surface's actual landing from TREE ground truth (never trust the frozen hook-store
        # workspaceId). FRESH read (ttl=0): the default 2s memo would confirm against a STALE pre-move tree
        # (the load-bearing correctness point).
        new_ws = rs.workspace(surf, ws_map=rs.surface_ws_map(ttl=0))
        if not new_ws:
            sys.exit(f"[fleet] move: after the move, surface {surf[:8]} could not be located in any "
                     f"workspace -- NOT touching the registry. Inspect: cmux tree --all. (The expected-close "
                     f"tombstone will lapse; if the surface is truly gone, `fleet register`/`revive`.)")

        # POST-MOVE VERIFY — `cmuxq` DISCARDS the return code, so a failed move-surface /
        # move-tab-to-new-workspace reads as success. Assert the surface actually landed where we asked
        # BEFORE rewriting the registry to claim it did: a false "rehomed" is how a move silently split-brains.
        if a.to_workspace and new_ws.upper() != target_uuid.upper():
            sys.exit(f"[fleet] move: move-surface did NOT take — {a.label} is in {new_ws[:8]}, not the "
                     f"target {target_uuid[:8]}. NOT touching the registry (no false 'rehomed'). "
                     f"Inspect: cmux tree --all.")
        if a.own_workspace and new_ws.upper() == cur_ws.upper():
            sys.exit(f"[fleet] move: --own-workspace did NOT relocate {a.label} — it is still in "
                     f"{cur_ws[:8]} (move-tab-to-new-workspace reused the same workspace, not a fresh one). "
                     f"NOT touching the registry. Inspect: cmux tree --all.")
    else:
        new_ws = cur_ws                                       # same-ws PURE REPARENT: the surface never moved

    # group + place follow the (possibly new) parent. The relocation kind decides placement, and a group the
    # parent owns is (re)joined surface-preserving via `workspace-group add` — the native regroup that repairs
    # the split-brain the old archive-revive path caused (cf-conductor, 2026-07-15).
    gname = parent_entry.get("group") or child.get("group") or ""
    gref = _group_ref(gname) if gname else ""
    if a.own_workspace:
        new_place, new_group = "workspace", ""
        if gref and new_ws:                                  # the FRESH workspace is in no group yet -> join it
            cmuxq("workspace-group", "add", "--group", gref, "--workspace", new_ws)   # surface-preserving
            new_group = gname
    elif relocate:                                           # cross-ws --to-workspace: joined the target as a tab
        # the target workspace carries its own visual group; the fleet group field follows the new parent.
        new_place, new_group = "tab", gname
    else:                                                    # same-ws PURE REPARENT: keep the placement, follow the group
        new_place, new_group = child.get("place", "tab"), child.get("group", "")
        if gref and new_ws and gname != child.get("group"):  # pull the current ws into the new parent's group
            cmuxq("workspace-group", "add", "--group", gref, "--workspace", new_ws)   # surface-preserving
            new_group = gname

    # a registry UPDATE — parent + group + workspace set TOGETHER (the split-brain fix), every other field
    # preserved (via {**child}) — never a teardown.
    rehomed = new_parent != child.get("parent")
    fs.live_put(a.label, {**child, "parent": new_parent, "workspace": new_ws, "place": new_place,
                          "group": new_group})
    fs.log_event("moved", label=a.label, role=child.get("role"), session=child.get("session"),
                 via="fleet-move-native", parent=new_parent)
    if relocate:
        print(f"[fleet] moved {a.label}: surface {surf[:8]} {cur_ws[:8]} -> {new_ws[:8]}"
              + (f" (rehomed under {new_parent})" if rehomed else "")
              + (f" (group '{new_group}')" if new_group else "")
              + "; relocated natively — pid/session/context/agent-status registration intact.")
    else:
        print(f"[fleet] move {a.label}: stayed in workspace {cur_ws[:8]} (pure reparent)"
              + (f"; rehomed under {new_parent}" if rehomed else "; parent unchanged")
              + (f", group '{new_group}'" if new_group else "")
              + ".")
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


def cmd_reap_surfaces(argv):
    """Survey (DRY-RUN) orphaned bare-shell HUSK surfaces: a terminal surface carrying a FLEET launch
    artifact as its tail, with NO live agent and NO registry entry — the inert login shells cmux's
    session-restore replays on reboot, and the shells a fleet agent leaves when its claude exits without
    a fleet archive. Closes NOTHING by default. Every terminal surface is classified into tracked /
    live-agent / human-shell / husk-candidate; only husk-candidate is ever reapable, and only via the
    review-gated --close path (archive-first: harvest the resume id + label, write the archive record,
    re-verify, THEN cmux close-surface). --close refuses until that path is signed off."""
    ap = argparse.ArgumentParser(prog="fleet reap-surfaces", add_help=True,
                                 description="find orphaned bare-shell husk surfaces (dry-run); --close is review-gated")
    ap.add_argument("--all", action="store_true", help="survey EVERY workspace (default: fleet-managed only)")
    ap.add_argument("--json", action="store_true", help="machine output")
    ap.add_argument("--close", action="store_true", help="(review-gated) close husk candidates; refuses for now")
    a = ap.parse_args(argv)
    from . import state as fs
    from . import resolve as rs

    live = fs.live_all()
    member_surf = {(e.get("surface") or "").upper(): lbl for lbl, e in live.items() if e.get("surface")}
    # fleet-managed workspaces = where live OR archived members live (an agent that exited leaves its
    # workspace member-less, so archived members matter for reach). A husk with a fleet AGENT_LABEL is
    # proven fleet-origin regardless of workspace, so it is always in scope (see below).
    # DERIVED (Ship 5): a LIVE member's workspace comes from the tree via the resolver, never a stored field,
    # so a live workspace is protected from reap even after the workspace field leaves the row.
    managed_ws = {w.upper() for e in live.values() if (w := rs.workspace(fs.e_surface(e)))}
    managed_ws |= {(e.get("workspace") or "").upper() for e in fs.archive_all().values() if e.get("workspace")}
    tree = cmuxq("tree", "--all", "--id-format", "both")

    buckets = {"tracked": [], "live-agent": [], "human-shell": [], "husk-candidate": []}
    for surf, ws, title in _iter_terminal_surfaces(tree):
        u = surf.upper()
        in_scope = a.all or (ws or "").upper() in managed_ws
        if u in member_surf:
            buckets["tracked"].append({"surface": surf, "label": member_surf[u], "title": title})
            continue
        if rs.surface_has_live_agent(surf):               # codex-aware: union hook store + live pid is the authority
            buckets["live-agent"].append({"surface": surf, "note": "live agent (pid)", "title": title})
            continue
        pane = cmuxq("capture-pane", "--surface", surf) or ""
        if _pane_shows_live_tui(pane):
            buckets["live-agent"].append({"surface": surf, "note": "TUI painted", "title": title})
            continue
        ev = _husk_evidence(pane)
        if ev["husk"]:
            # a husk carrying a fleet AGENT_LABEL is proven fleet-origin -> always in scope (the env
            # prefix, not the workspace, is the safety gate); a label-less husk falls back to workspace/--all.
            husk_in_scope = in_scope or bool(ev.get("label"))
            buckets["husk-candidate"].append({"surface": surf, "workspace": ws, "title": title,
                                              "label": ev.get("label", ""), "resume_id": ev.get("resume_id", ""),
                                              "reason": ev["reason"], "in_scope": husk_in_scope})
        else:
            buckets["human-shell"].append({"surface": surf, "title": title, "reason": ev["reason"]})

    candidates = [h for h in buckets["husk-candidate"] if h["in_scope"]]
    if a.json:
        print(json.dumps({"scope": "all" if a.all else "fleet-managed",
                          "buckets": buckets, "reapable_in_scope": len(candidates)}, indent=2))
        return 0

    scope = "ALL workspaces" if a.all else "fleet-managed workspaces (use --all for every workspace)"
    total = sum(len(v) for v in buckets.values())
    print(f"[reap-surfaces] DRY-RUN — {total} terminal surfaces in {scope}. Closes NOTHING.\n")
    for k, lbl in (("tracked", "TRACKED (live fleet member)"),
                   ("live-agent", "LIVE AGENT (live pid / painted TUI)"),
                   ("human-shell", "HUMAN SHELL (no fleet launch signature, or human-touched)"),
                   ("husk-candidate", "HUSK CANDIDATE (reapable)")):
        print(f"  {lbl}: {len(buckets[k])}")
    if buckets["husk-candidate"]:
        print("\n  husk candidates:")
        for h in buckets["husk-candidate"]:
            mark = "" if h["in_scope"] else "   [out of default scope — use --all]"
            print(f"    - {h['surface'][:8]}  label={h['label'] or '?'}  "
                  f"resume={(h['resume_id'][:8] + '…') if h['resume_id'] else '(none)'}  "
                  f"\"{h['title'][:34]}\"{mark}")
            arch = (f"label={h['label']}, resume={h['resume_id']}" if h["resume_id"]
                    else "NO resume id harvested — --close would SKIP it (never close a pointer we cannot record)")
            print(f"        archive-on-close would record: {arch}")
    print(f"\n  reapable in scope: {len(candidates)}")
    if a.close:
        print("\n[reap-surfaces] --close is REVIEW-GATED and not yet enabled. Run this dry-run, get sign-off, "
              "then the gated close path ships (archive-first per candidate + re-verify + cmux close-surface).")
        return 2
    print("  (dry-run only) closing ships review-gated: fleet reap-surfaces --close")
    return 0


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
    from . import resolve as rs
    if force:
        return True          # --force = respawn now, no wait (consistent with rm --force). See docstring.
    def quiet():
        lc = rs.lifecycle(surf)
        # A 'running' record on a DEAD process is a frozen ghost (SessionEnd-less death), NOT a live
        # turn -- there is nothing to interrupt, so it counts as quiet. Without this a self-bricked
        # agent (the 2026-07-06 dead-agent class: lifecycle frozen 'running', pid dead) could never be
        # recovered by a plain `fleet recycle` -- the gate would block the full 180s and ABORT, forcing
        # --force. The pid, not the string, is the authority (see _confirmed_gone).
        if lc == "running" and not rs.has_live_pid(surf):
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


def _latest_handover(abs_cwd, label=None):
    """Newest handover under the agent's `handover/` dir, or '' if none. LABEL-KEYED discovery (Ship 5d):
    when `label` is given, prefer this agent's OWN handovers — `handover/<label>-*.md`, newest by mtime —
    and only fall back to the legacy `handover/*.md` (any prefix) when none carry the label. That keeps
    the transition resolving while emitters move onto the label prefix, and stops a co-located agent's
    handover (shared home) from being mistaken for this one's once the prefix is in use."""
    hd = os.path.join(abs_cwd, "handover")
    try:
        files = [os.path.join(hd, f) for f in os.listdir(hd) if f.endswith(".md")]
    except OSError:
        return ""
    if not files:
        return ""
    if label:
        owned = [f for f in files if os.path.basename(f).startswith(f"{label}-")]
        if owned:
            return max(owned, key=os.path.getmtime)
    return max(files, key=os.path.getmtime)          # legacy fallback: newest handover of any prefix


def _poll_session_back(surf, old_sid, mode, timeout=90):
    """Confirm the recycled agent re-bound a session to `surf`. respawn-pane fully REMOVES the old
    session entry from cmux's hook store (session-end), then the relaunch re-creates it:
      FRESH  -> confirm on the LIVE-PID truth (rs.live_sid): the freshest record on the surface
                with an ALIVE pid is the running agent, whatever sid cmux assigned it. This replaced
                the old sid-exclusion-vs-{old_sid, pre_sid} confirm, which rode poll_session's
                arbitrary-first-record fallback and could stare at the dead lingering ghost forever
                while a healthy fresh agent sat on the seat unconfirmed — the destructive misdetect
                that made the self-heal paste the launch into a LIVE TUI (berg-sandbox, 2026-07-09).
                A live bind that equals old_sid does NOT confirm: that seat resumed the OLD session
                (cmux restart-resume interference), which is live but not the fresh context the
                recycle promised — fall through to WARN + escalation, never declare fresh.
      RESUME -> the SAME session id. `claude --resume <id>` CONTINUES the session (same id, same
                transcript JSONL -- no fork; verified live), re-created with a fresh pid and
                agentLifecycle '' -> 'unknown'. So we CANNOT wait for a different sid (it never
                comes); we wait for the surface to carry a live (non-empty) lifecycle again, which
                only happens once resume's session-start fires. activeSessionsBySurface stays null
                until the first turn, so we rely on poll_session's sessions[] fallback + the
                surface_has_live_agent (non-terminal AND live-pid) gate — a frozen dead-pid ghost
                (SessionEnd-less brick, 2026-07-06) must not false-confirm the re-bind.
    Returns the bound sid, or '' on timeout."""
    from . import resolve as rs
    end = time.time() + timeout
    while time.time() < end:
        if mode == "fresh":
            sid = rs.live_sid(surf)
            if sid and sid != old_sid:
                return sid
        else:
            sid = poll_session(surf, timeout=1)
            if sid and rs.present(surf):
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


def _replay_binding_argv(argv, tool, role, label, cwd, caller_tokens, add_plugins, resume_session,
                         provider=None):
    """Recompose a launch command from a captured binding's argv — the SHARED core of recycle (reads a
    LIVE surface binding) and revive (reads the binding captured at archive time). Strips the binding's
    own --resume (callers control it), unions `add_plugins` through the index, layers caller flag overrides,
    optionally re-adds `--resume <resume_session>`, and re-injects AGENT_ROLE/AGENT_LABEL (bindings
    capture null env, so the orchestration vars must be put back). Other env (tool-floor env) is NOT
    recoverable from a binding — accepted, same as it's always been for recycle.
    `add_plugins` (the `--plugin` names) routes through the index into BOTH channels here too: a `linked`
    name appends a --plugin-dir, an `enabled` name appends an enabledPlugins --settings (the wrapper deep-
    merges multiple --settings, so a fresh one is safe alongside the binding's own)."""
    abs_cwd = cwd if os.path.isabs(cwd) else os.path.join(ROOT, cwd)
    base = _drop_keys(argv, {"--resume"})
    have = {base[i + 1] for i in range(len(base) - 1) if base[i] == "--plugin-dir"}
    if add_plugins:
        linked, enabled, unresolved = _resolve_plugins(list(add_plugins), load_plugin_index())
        for name in unresolved:
            print(f"[fleet] warn: plugin '{name}' not resolvable (marketplace unset or not found); skipping")
        for pd in linked:
            if pd not in have:
                base += ["--plugin-dir", pd]; have.add(pd)
        if enabled:                                              # extra --settings; wrapper deep-merges it in
            base += ["--settings", json.dumps({"enabledPlugins": {ref: True for ref in _dedup(enabled)}})]
    base = _layer_tokens([base, list(caller_tokens or [])])      # flag overrides
    base = _prepend_resume(base, tool, resume_session)           # claude --resume flag | codex resume subcmd
    # profile-pin a recycled/revived child too (bindings capture null env -> re-inject the build env)
    env = {**_profile_env(), "AGENT_ROLE": role, "AGENT_LABEL": label}
    # No AGENT_CONDUCTOR (Ship 5d): parentage is derived from the registry at use-time, not carried in env —
    # a captured binding's env goes stale across a recycle, the registry `parent` never does. Routing +
    # `fleet peer-msg --to-parent` read it live.
    # ACCOUNT re-resolution (fix 1): a captured binding drops the account env (its own docstring: env is NOT
    # recoverable), so re-inject the re-resolved account env/args here — this is exactly why an ad-hoc
    # recycle used to revert to the ambient credential. render carries raw_env (spawn-time secret channel).
    env, base, raw_env = _apply_provider(env, base, provider)
    return render_send_cmd(tool, base, env, abs_cwd, raw_env)


def _resolve_account_name(tool, role):
    """The account NAME a recycle/revive resolves for `tool`, FOLLOWING CONFIG — the same precedence
    `fleet launch` uses MINUS the one-off `--provider` flag (one-off flags are one-off by design):
    role account-pin → tool default. Role pins are not parsed yet (that is review #7, out of scope here),
    so today this is the tool default. It resolves THROUGH default_provider — the same seam launch
    resolves through — rather than hardcoding "default", so once #7 lands and layers a
    [role.<role>.<tool>].account pin ABOVE the tool default, both recycle and revive pick it up here for
    free. Raises ProviderError on an unreadable toml (fix 2), same as the launch path."""
    from . import providers as pv
    _ = role                                                    # review #7 seam: role account-pin layers here
    return pv.default_provider(tool)


def _resolve_recycle_provider(tool, role, recorded_provider):
    """Resolve the account env a recycle/revive must inject, FOLLOWING CONFIG — invariant 5: account
    resolution is part of the ONE composed spec every spawn path shares, exactly as `_compose_from_roster`
    already re-resolves the loadout from the current toml. This is the chokepoint the launch path had to
    itself; recycle/revive join it here so a recycled agent no longer silently reverts to the ambient
    credential (the securestorage/CODEX_HOME drop).

    Returns (pr | None, announce_line, change_warn). `pr` is providers.resolve_launch's dict
    (env/raw_env/args + label + any needs_refresh), or None when no [providers.<tool>] is configured
    (single-account opt-in: nothing to inject, byte-identical to before). ABORTS (SystemExit) if a NAMED
    account fails to resolve — NEVER a silent fall back to the ambient account, exactly as cmd_launch does
    (and refuses on an unreadable toml, fix 2)."""
    from . import providers as pv
    try:
        pname = _resolve_account_name(tool, role)
    except pv.ProviderError as e:
        sys.exit(f"[fleet] ABORT: {e}")
    if not pname:
        # No [providers.<tool>] configured now → nothing to inject (single-account opt-in). But if this
        # agent was LAUNCHED under a named account and the operator has since REMOVED the table, "follow
        # config" means it reverts to the AMBIENT credential — a real account move that must not be silent
        # (the same "you see it move" guarantee as a default flip, for the removal case).
        if recorded_provider:
            warn = (f"[fleet] WARN: account DROPPED — the registry recorded '{recorded_provider}', but no "
                    f"[providers.{tool}] is configured now, so this respawn reverts to the AMBIENT "
                    f"credential (following config). Re-add the account, or confirm ambient is intended.")
            return None, "", warn
        return None, "", ""                                    # never had one → truly single-account, silent
    try:
        pr = pv.resolve_launch(tool, pname)
    except pv.ProviderError as e:
        sys.exit(f"[fleet] ABORT: provider resolution failed for {tool}: {e}")
    announce = f"[fleet] provider: {pr['label']}" + (f"  ({pr['note']})" if pr.get("note") else "")
    warn = ""
    if recorded_provider and recorded_provider != pr["label"]:
        warn = (f"[fleet] WARN: account MOVED since launch — the registry recorded '{recorded_provider}', "
                f"re-resolving to '{pr['label']}' (following config). The agent resumes on the NEW account; "
                f"intended if you flipped the default/pin, a surprise otherwise.")
    return pr, announce, warn


def _apply_provider(env, args, provider):
    """Fold a resolved provider (from _resolve_recycle_provider) into a compose's (env, args, raw_env),
    mirroring cmd_launch (cli ~1636-1639): plain env updated, provider CLI tokens appended, raw_env (the
    spawn-time $(cat …) secret channel) carried separately for render_send_cmd. No provider → unchanged
    (empty raw_env), so a single-account recycle composes byte-identically to before."""
    raw_env = {}
    if provider:
        env.update(provider.get("env") or {})
        raw_env.update(provider.get("raw_env") or {})
        args = args + list(provider.get("args") or [])
    return env, args, raw_env


def _compose_from_registry(label, entry, caller_tokens, add_plugins, resume_session, provider=None):
    """Fallback compose from our registry spec (used only when cmux has no binding for the surface).
    `add_plugins` (the `--plugin` names) unions onto the spec's `plugins`, so adapter_compile routes them
    through the index into --plugin-dir / enabledPlugins exactly like the roster path. `provider` (fix 1)
    injects the re-resolved account env/args."""
    tool = entry.get("tool", "claude")
    cwd = entry.get("cwd", "")
    abs_cwd = cwd if os.path.isabs(cwd) else os.path.join(ROOT, cwd)
    spec = {"tool": tool, "role": entry.get("role"), "label": label, "kind": entry.get("kind", "child"),
            "place": entry.get("place", "tab"), "group": entry.get("group", ""), "cwd": cwd,
            "abs_cwd": abs_cwd, "plugins": _dedup(list(entry.get("plugins", [])) + list(add_plugins or [])),
            "flags": _layer_tokens([list(entry.get("flags", [])), list(caller_tokens or [])]),
            "env": {}, "settings": entry.get("settings", "")}
    codex_home = (provider.get("env") or {}).get("CODEX_HOME") if provider else None
    bin_name, args, env = adapter_compile(tool, spec, [], codex_home=codex_home)
    args = _prepend_resume(args, tool, resume_session)           # claude --resume flag | codex resume subcmd
    env, args, raw_env = _apply_provider(env, args, provider)
    return render_send_cmd(bin_name, args, env, abs_cwd, raw_env)


def _compose_from_roster(role, tool, label, caller_tokens, add_plugins, resume_session, cwd_override="",
                         provider=None):
    """TOML-AUTHORITATIVE compose for a ROSTER role: re-resolve the CURRENT toml (floor + role config,
    incl. plugins / setting_sources), compile it exactly as `fleet launch` does, then prepend the
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
        # UNION `--plugin` names into the re-resolved spec's `plugins` BEFORE adapter_compile — the index
        # routes each to the right channel, so `--plugin <enabled>` reaches enabledPlugins on a roster
        # recycle too. Coexists with a role's own `plugins` (unioned + deduped downstream).
        spec["plugins"] = _dedup(spec["plugins"] + list(add_plugins))
    cwd = cwd_override or spec["cwd"]
    abs_cwd = cwd if os.path.isabs(cwd) else os.path.join(ROOT, cwd)
    # thread the re-resolved codex home into adapter_compile so a codex seat's cruft-stripping flags are
    # enumerated from THAT home (same reason cmd_launch passes codex_home; a mismatch breaks codex start).
    codex_home = (provider.get("env") or {}).get("CODEX_HOME") if provider else None
    bin_name, args, env = adapter_compile(tool, spec, caller_tokens, codex_home=codex_home)
    args = _prepend_resume(args, tool, resume_session)
    env, args, raw_env = _apply_provider(env, args, provider)    # fix 1: inject the re-resolved account env
    return render_send_cmd(bin_name, args, env, abs_cwd, raw_env)


def _is_roster(role):
    """True if `role` is a named roster role in the toml (-> toml-authoritative). Ad-hoc / off-roster
    labels are not, and reproduce from their captured launch instead."""
    try:
        return bool(role) and role in (load_config().get("role") or {})
    except SystemExit:
        return False


def _compose_recycle_cmd(label, entry, caller_tokens, add_plugins, mode, explicit_session="", provider=None):
    """Recompose the recycle launch. ROSTER agents (role in the toml) are TOML-AUTHORITATIVE: re-resolve
    the current toml so a recycle picks up floor/role changes since launch. AD-HOC / off-roster agents
    have no toml to resolve -> reproduce from cmux's ground-truth binding (registry spec as last resort).
    Identity + session come from the registry; FRESH drops the resume, RESUME re-adds it per tool.
    One-off caller `--` flags apply this invocation only. `add_plugins` (the `--plugin` names) unions into
    whichever compose path runs (roster/binding/registry) — routed through the index, reaching BOTH plugin
    channels. `provider` (fix 1) is the re-resolved account (env/args) injected into every compose path so
    the account FOLLOWS CONFIG on a recycle, identically to the loadout. Returns (send_cmd, checkpoint)."""
    tool = entry.get("tool", "claude")
    role = entry.get("role")
    b = _resume_binding(entry.get("surface", ""))
    checkpoint = b.get("checkpoint_id", "")
    # the session to resume: an EXPLICIT --session target wins (resume an arbitrary prior session, no
    # cmux-checkpoint surgery); else cmux's checkpoint if it has one; else the registry's recorded session.
    resume_session = ((explicit_session or checkpoint or (entry.get("session") or "").replace("claude-", ""))
                      if mode == "resume" else None)
    # LOUD HEAL-LOG (recovery-safety #11): when we resume from cmux's checkpoint BECAUSE the registry
    # session is stale/absent, say so — the whole point of preferring the checkpoint is to heal a STALE
    # registry binding (the desync that the router's Stop-only reconcile structurally can't reach), and a
    # silent heal is invisible to the operator confirming the fix. cmux's checkpoint is the ground truth
    # (empirically survives the hook-store desync); the post-recycle re-bind writes it back to the registry.
    if mode == "resume" and not explicit_session:
        _reg = (entry.get("session") or "").replace("claude-", "")
        if checkpoint and _reg and checkpoint != _reg:
            print(f"[fleet] heal: registry session {_reg[:12]} != cmux checkpoint {checkpoint[:12]} for "
                  f"'{label}' — resuming from the CHECKPOINT (cmux ground truth); the re-bind refreshes the "
                  f"stale registry binding.")
        elif checkpoint and not _reg:
            print(f"[fleet] note: registry has no session for '{label}'; resuming from cmux checkpoint "
                  f"{checkpoint[:12]} (ground truth).")
    if _is_roster(role):                                          # ROSTER -> re-resolve the toml (truth)
        # RESUME pins the session's original cwd (registry) so a moved-role / worktree agent resumes where
        # its session actually lives; FRESH adopts the current toml cwd (picks up an intentional move).
        cwd_override = entry.get("cwd", "") if mode == "resume" else ""
        return (_compose_from_roster(role, tool, label, caller_tokens, add_plugins, resume_session,
                                     cwd_override, provider),
                checkpoint)
    argv = _binding_argv(b.get("command", ""))                    # AD-HOC / off-roster -> reproduce
    if not argv:                                                  # no cmux binding -> registry fallback
        return _compose_from_registry(label, entry, caller_tokens, add_plugins, resume_session,
                                      provider), checkpoint
    cwd = b.get("cwd") or entry.get("cwd", "")
    send_cmd = _replay_binding_argv(argv, tool, role, label, cwd, caller_tokens, add_plugins,
                                    resume_session, provider)
    return send_cmd, checkpoint


def _codex_cfg_val(tokens, key):
    """Value of a codex `-c <key>=<v>` config override in a composed command's tokens, else None. shlex
    splits `-c model_reasoning_effort=xhigh` into ['-c', 'model_reasoning_effort=xhigh'], so matching the
    `<key>=` token directly is unambiguous (the key prefix is distinctive) and covers every -c spelling."""
    want = key + "="
    for t in tokens:
        if t.startswith(want):
            return t.split("=", 1)[1]
    return None


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
    # CODEX DIALECT (gap-5 #3): the codex adapter translates `--effort <lvl>` into
    # `-c model_reasoning_effort=<lvl>`, so a composed CODEX command carries NO --effort token and this
    # reader saw nothing -- codex effort was invisible to BOTH the provenance print AND the
    # recycled/revived `effective` ledger, and the floor-effort warning could never fire for codex (its
    # loop body only runs on a truthy value). Reading the translated form here repairs print + ledger +
    # warning together, because this is deliberately the ONE shared source. `--model` needs no fallback:
    # codex takes it natively, so it is already found above.
    if out["effort"] is None:
        out["effort"] = _codex_cfg_val(toks, "model_reasoning_effort")
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


def _recycle_plan(label, entry, caller, add_plugin, mode, session, force, prime_override, no_prime,
                  provider=None):
    """Compose ONE recycle payload (the dict the detached exec consumes). Shared by single + bulk recycle
    so the mode/session/prime logic lives in exactly one place. FRESH boots clean -> auto-prime from the
    latest handover; RESUME carries its context -> no prime unless asked. `add_plugin` unions the `--plugin`
    names into the composed loadout, routed through the index (reaching BOTH plugin channels). `provider`
    (fix 1) is the re-resolved account, injected into the composed send_cmd AND recorded on the payload so
    the detached exec re-binds the registry's `provider` field to the account the agent NOW runs under."""
    surf = entry.get("surface", "")
    old_sid = (entry.get("session") or "").replace("claude-", "")
    send_cmd, _checkpoint = _compose_recycle_cmd(label, entry, caller, add_plugin, mode, session, provider)
    prime = None
    if not no_prime:
        if prime_override:
            prime = prime_override
        elif mode == "fresh":
            abs_cwd = entry.get("cwd", "")
            abs_cwd = abs_cwd if os.path.isabs(abs_cwd) else os.path.join(ROOT, abs_cwd)
            ho = _latest_handover(abs_cwd, label)
            prime = (f"You were just recycled into a FRESH session (same identity: label '{label}', "
                     f"role '{entry.get('role')}', same surface). Re-orient from your latest handover"
                     + (f" at {ho}" if ho else " under ./handover/")
                     + ", then continue where it left off.")
    return {"label": label, "surface": surf, "send_cmd": send_cmd, "mode": mode,
            "tool": entry.get("tool", "claude"), "force": force, "prime": prime, "old_session": old_sid,
            "cwd": _cwd_of_sendcmd(send_cmd),          # effective launch cwd, persisted after a FRESH bind
            # deterministic plugin set (entry + add_plugin union) for the recycled event's `effective`
            # field -- no token-scan needed for this part, unlike effort/model.
            "plugins": _dedup(list(entry.get("plugins", [])) + list(add_plugin or [])),
            # fix 1: the re-resolved account (registry `provider` re-bind + `fleet usage` attribution) and
            # the codex pre-launch refresh guard's target. "" when no [providers] (opt-in holds).
            "provider": (provider or {}).get("label", ""),
            "provider_needs_refresh": (provider or {}).get("needs_refresh") or ""}


def _bulk_targets(target, from_surface, from_label, include_muted):
    """Live agents matching a bulk selector, mirroring `broadcast`'s target vocabulary. ALWAYS excludes
    self + unbound surfaces (external recycle is the safe topology — a conductor can't respawn its own
    surface from its own turn). Muted / human-driven agents (homelab, resume-research) are SKIPPED by
    default; --include-muted keeps them. Returns (selected [(label,entry)], skipped [(label,reason)])."""
    from . import state as fs
    from . import resolve as rs
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
        # Routed through the liveness rule (rs.present) so a frozen dead-pid 'running' ghost (SessionEnd-
        # less brick, 2026-07-06) reads STALE here too, CONSISTENT with cmd_ls (both pid-aware). The skip
        # is REPORTED, so the brick surfaces to the operator for an explicit `fleet recycle <label>`
        # (itself pid-aware and able to recover it) rather than being silently retried inside the sweep.
        if not rs.present(surf) and v.get("session"):
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
    ap.add_argument("--plugin", action="append", default=[], metavar="NAME",
                    help="plugin to UNION into this identity for the restart (repeatable or comma-sep; "
                         "persisted via the re-captured binding). Routes through the index (plugins.toml) so "
                         "a `linked` name adds a --plugin-dir and an `enabled` name adds an enabledPlugins "
                         "entry — both plugin types. A name not in the index loads as a linked --plugin-dir")
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
    # FAIL-LOUD on a RESUME with nothing to resume (recovery-safety #11): if cmux holds NO checkpoint for
    # the surface AND the registry has no recorded session, a resume would compose an empty `--resume` and
    # dead-end at runtime ('No conversation found'). Refuse up front with the recovery options instead.
    if mode == "resume" and not a.session:
        _ckpt = _resume_binding(surf).get("checkpoint_id", "")
        _reg = (entry.get("session") or "").replace("claude-", "")
        if not _ckpt and not _reg:
            sys.exit(f"[fleet] recycle: RESUME but '{label}' has NO resumable session — cmux holds no "
                     f"checkpoint for surface {surf[:8]} and the registry has no recorded session. Nothing "
                     f"to resume. Shed into a fresh one: `fleet recycle {label} --fresh`; or pick a prior "
                     f"id: `fleet sessions {label}` then `fleet recycle {label} --session <id>`.")
    # ACCOUNT re-resolution (fix 1): follow config on the recycle, exactly like the loadout — the account
    # joins the composed spec instead of being dropped to ambient. Aborts on an unresolvable/unreadable
    # account (never a silent ambient fall-back). Announced below; a moved default prints a loud warn.
    pr, provider_announce, provider_warn = _resolve_recycle_provider(
        entry.get("tool", "claude"), entry.get("role"), entry.get("provider", ""))
    payload = _recycle_plan(label, entry, caller, _flatten_csv(a.plugin), mode, a.session, a.force,
                            a.prime, a.no_prime, pr)
    provline, provwarn = _session_pref_provenance(entry.get("role"), entry.get("tool", "claude"),
                                                   payload["send_cmd"], a.effort, a.model)

    print(f"[fleet] recycle {label} (mode={mode}, tool={entry.get('tool','claude')}, surface={surf})")
    print(f"[fleet] launch: {payload['send_cmd']}")
    if provider_announce:
        print(provider_announce)                                 # fix 1: tool:account (+ note), like launch
    if provider_warn:
        print(provider_warn)                                     # fix 1: LOUD warn when the account moved
    if provline:
        print(provline)                                          # effort/model + provenance (source)
    if provwarn:
        print(provwarn)                                          # no-pin warning (floor-inherited effort)
    if a.plugin or caller:
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
        # per-target account re-resolution (fix 1): a bulk recycle after a default flip is exactly the
        # "flip the default, recycle the fleet, it MOVES — and you see it move" story, so resolve + announce
        # + warn per agent. Aborts the whole sweep on an unreadable/unresolvable config (a shared toml
        # problem affects every target equally — better to refuse loudly than move some agents wrong).
        pr, provider_announce, provider_warn = _resolve_recycle_provider(
            entry.get("tool", "claude"), entry.get("role"), entry.get("provider", ""))
        payload = _recycle_plan(label, entry, caller, _flatten_csv(a.plugin), mode, "", a.force,
                                a.prime, a.no_prime, pr)
        payloads.append(payload)
        # per-agent RESOLVED effort/model (provenance) — mirror the single-target print so an operator
        # watching a bulk recycle sees what each agent is actually coming back on (a bulk recycle is
        # exactly where a silent model/effort drift, like the storm-era Sonnet downgrade, would slip by).
        provline, provwarn = _session_pref_provenance(
            entry.get("role"), entry.get("tool", "claude"), payload["send_cmd"], a.effort, a.model)
        print(f"   {label:<24}{entry.get('kind','-'):<11}{(entry.get('surface') or '')[:8]}  mode={mode}")
        if provider_announce:
            print(f"      {provider_announce}")                 # fix 1: tool:account (+ note)
        if provider_warn:
            print(f"      {provider_warn}")                     # fix 1: LOUD warn when the account moved
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
    """Loadout-scaled resume-menu ceiling. Canonical body: adapter.resume_menu_timeout (step 2); this
    name stays as the call-site seam until step 3."""
    from . import adapter
    return adapter.resume_menu_timeout(plugin_count, base=base, per_plugin=per_plugin, ceiling=ceiling)


# tri-state outcomes of the resume-menu watch — canonical values live in adapter.py (step 2);
# re-exported here because tests and callers reference the cli names.
from .adapter import RESUME_DISMISSED, RESUME_READY, RESUME_TIMEOUT  # noqa: E402


def _dismiss_resume_summary_prompt(surf, log, timeout=None, plugin_count=0):
    """The resume-summary menu dismisser (ALWAYS 'full session as-is', never the lossy summary).
    Canonical body: adapter.dismiss_resume_menu (step 2 relocated it; the adapter owns the one
    sanctioned screen interaction). This name stays as the call-site/test seam until step 3."""
    from . import adapter
    return adapter.dismiss_resume_menu(surf, log, cmux=cmuxq, timeout=timeout, plugin_count=plugin_count)


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


def _fire_launch(surf, guarded, log):
    """Paste the launch into the fresh post-respawn shell and verify the ENTER submitted it. GUARDED:
    if an agent TUI is ALREADY UP on the surface (or its resume menu is), firing is refused — pasting a
    launch into a live agent is exactly the inert-garbled-draft failure (berg-sandbox 2026-07-09: the
    confirm misdetected a healthy fresh claude, and the self-heal re-fired the launch into its input
    box as a collapsed '[Pasted text #1]' block). Returns True iff the launch was actually sent.

    Submission verify mirrors _send_launch_and_confirm's shape (retry the ENTER, never the paste). The
    terminating newline can lose the paste-settle race, leaving the launch as an inert DRAFT at the
    shell; re-sending the WHOLE TEXT on top of it doubles the draft (orphan surface AAF4EC13), so only
    a bare Enter is ever re-kicked. Either signal means the line submitted: _agent_surfaced (a TUI
    marker painted) OR _resume_menu_visible (claude's resume-summary menu is up). Bounded to max_kicks
    (~10s worst case), tiny vs the outer _poll_session_back(90) ceiling that runs AFTER this returns;
    re-kicks stop the instant a TUI surfaces, so a slow boot is never spammed."""
    if _agent_surfaced(surf) or _resume_menu_visible(surf):
        log("SKIP launch: an agent TUI is already up on this surface — never paste a launch into a live agent")
        return False
    log("launching agent into the fresh shell")
    cmuxq("send", "--surface", surf, guarded)
    cmuxq("send-key", "--surface", surf, "enter")
    kicks, max_kicks = 0, 5
    while kicks < max_kicks:
        if _agent_surfaced(surf) or _resume_menu_visible(surf):
            return True                                  # submitted -> stop re-kicking
        cmuxq("send-key", "--surface", surf, "enter")    # re-kick the ENTER only, never the paste
        kicks += 1
        time.sleep(2)
    return True


def _exec_launch_enabled():
    """C feature flag, default ON for recycle: the launch is delivered as the pane PROCESS
    (_exec_launch), not a paste. Set CMUX_FLEET_EXEC_LAUNCH=0|false|off to fall back to the paste path
    (_fire_launch) — kept as the fallback while OQ3/OQ4/OQ5 (shell-init timing, the resume menu on an
    exec'd pane, codex lazy-bind) accumulate live proof."""
    return os.environ.get("CMUX_FLEET_EXEC_LAUNCH", "1").strip().lower() not in ("0", "false", "off")


def _exec_launch(surf, guarded, log):
    """C: deliver the launch as the PANE PROCESS via a SECOND respawn-pane — no paste, no Enter, no
    settle race, no re-kick, no self-heal. The command travels as ONE argv element end-to-end (cmuxq
    passes argv; cmux hands --command to the pane spawner verbatim; live probe 2026-07-09: a 2898-byte
    element with 2810 bytes of inline JSON executed byte-exact), so there is nothing a TUI can collapse
    — the '[Pasted text #1]' class that ate four berg-sandbox recycles and a drive-child brief in one
    day is structurally gone.

    WHY A SECOND RESPAWN (not the launch on the verify respawn): _respawn_and_verify must confirm the
    OLD agent dead before any launch exists. If the launch rode the first respawn, the NEW agent's live
    pid would poison _confirmed_gone's no-live-pid check — the verify would time out and the direct-kill
    fallback would SIGINT the agent we just launched. The bare-shell respawn stays the verify vehicle;
    this call then replaces only the delivery of the launch into it.

    TRAP (live-reproduced by cmux-advisor): a bare `zsh -ilc '<launch>'` pane DIES WITH ITS COMMAND —
    cmux destroys the whole SURFACE on exit ('not_found: Surface not found'), so a launch that crashes
    at startup would vaporize the seat and its surface UUID. The chained `; exec /bin/zsh -il` is
    NON-NEGOTIABLE: an exiting launch degrades to exactly the old recoverable bare-shell husk
    (reap-surfaces knows it), never a destroyed surface.

    Same TUI-up guard as _fire_launch (B, defense-in-depth): respawn-pane KILLS the pane process, so
    firing it over a live agent that appeared between the verify and this call (a cmux restart-resume)
    would destroy it — refuse instead; the A confirm then recognizes the live seat or the WARN
    escalates. A respawn-pane ERROR falls back to the proven paste path rather than leaving a bare
    shell with no launch at all. Returns True iff a launch was delivered by either mechanism.

    Canonical body: adapter.exec_deliver (step 2 generalized this to launch and revive; this name
    stays as the recycle call-site/test seam, injecting cli's own guards and paste fallback)."""
    from . import adapter
    return adapter.exec_deliver(
        surf, guarded, log, cmux=cmuxq,
        tui_up=lambda: _agent_surfaced(surf) or _resume_menu_visible(surf),
        paste_fallback=lambda: _fire_launch(surf, guarded, log))


def _escalate_recycle_failure(label, surf, mode, reason, detail):
    """Route a recycle failure to an ACTOR, not just the log. The pre-existing 'recycle FAILED' banner
    targets the failed seat's OWN surface — which is a dead shell or an untracked agent at exactly that
    moment, so nobody sees it (how the 9h berg-sandbox outage went unnoticed). A CHILD's failure alerts
    its parent conductor through the SAME doctor inbox+wake rail the fleet-doctor sweep uses; a
    CONDUCTOR's (or an unresolvable-parent's) failure fans out to peer conductors + the desktop exactly
    like conductor-down (router._alert_conductor_peers). The child row's event key is per-attempt
    (timestamped): each deliberate re-run that fails again re-alerts even if the prior failure was
    acked. Best-effort: escalation must never mask the recycle's own exit path."""
    from . import state as fs
    try:
        entry = fs.live_get(label) or {}
        payload = {"failure": reason, "mode": mode, "detail": detail, "via": "recycle"}
        parent = entry.get("parent")
        pe = fs.live_get(parent) if parent else None
        if entry.get("kind") == "child" and pe and pe.get("surface"):
            ps = pe["surface"]
            ekey = f"doctor:recycle-failed:{label}:{surf}:{int(time.time())}"
            seq = fs.inbox_put("doctor", ps, {"reason": "recycle-failed", "label": label,
                                              "child_surface": surf, **payload}, event_key=ekey)
            print(f"[recycle] escalated to parent '{parent}' (doctor seq {seq}): {reason}", flush=True)
            if seq and fs.idlewake_on():
                if fs.wake_if_idle(ps, f"(fleet-doctor) recycle FAILED for child {label} ({reason}); "
                                       f"handle your pending fleet inbox items"):
                    fs.presented_mark(ps, [{"event_key": ekey}], "wake")
        else:
            from . import router                        # lazy: no import cycle (router lazy-imports cli)
            router._alert_conductor_peers("recycle-failed", label, entry, surf, payload)
        fs.log_event("recycle_escalated", label=label, surface=surf, mode=mode, reason=reason)
    except Exception as e:
        print(f"[recycle] escalation error (non-fatal): {e}", flush=True)


def _recycle_exec_one(p):
    """Run ONE recycle: quiet-gate -> respawn-pane (verified) -> confirm new session -> reconcile the
    registry -> auto-prime. Never half-kills: aborts before respawn if the surface won't go quiet, and
    never sends the launch unless the old session is confirmed dead (see _respawn_and_verify) -- a
    respawn-pane timeout that goes unverified leaves the old claude ALIVE, and blindly firing the launch
    types it as an unsubmitted draft into that live TUI (the 9h berg-sandbox silent-recycle failure).
    Every terminal failure ESCALATES to an actor via _escalate_recycle_failure (parent conductor /
    peer-conductor fan-out) — a banner on the failed seat itself reaches nobody.
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
    # PRE-LAUNCH account-token guard (fix 1), mirroring cmd_launch (cli ~1643-1647): if the re-resolved
    # account carries a refresh target, refresh it BEFORE we tear the old agent down (a dead/revoked token
    # aborts loudly instead of spawning a seat into a 401 — and never a silent ambient fall-back). Inert in
    # today's codex-home model (resolve_launch sets no needs_refresh — the home's auth.json IS the cred and
    # codex refreshes it itself); wired for parity so the env-token path, if it returns, stays covered.
    needs_refresh = p.get("provider_needs_refresh") or ""
    if needs_refresh:
        from . import providers as pv
        try:
            pv.codex_ensure_fresh(needs_refresh)
        except pv.ProviderError as e:
            log(f"ABORT: account token refresh failed for '{needs_refresh}' ({e}); NOT respawning (the "
                f"old session is untouched). Re-login the account, then re-run fleet recycle.")
            fs.log_event("recycle_abort", label=label, surface=surf, mode=mode, reason="token-refresh-failed")
            _escalate_recycle_failure(label, surf, mode, "token-refresh-failed",
                                      f"account '{needs_refresh}' token refresh failed at recycle; re-login it")
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
    from . import adapter as _adapter
    guarded = _adapter.path_guard(send_cmd)

    tool_name = p.get("tool", "claude") or "claude"      # kill-target identity check (_agent_pid_check)

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
        from . import resolve as rs
        if rs.lifecycle(surf) in ("", "-", "ended"):
            return True
        return not rs.has_live_pid(surf) and not any(fs.pid_alive(p) for p in old_pids)

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
            _direct_kill(surf, tool_name, log)
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

    log("quiet; respawn-pane -> fresh interactive shell (cmux kills the old agent in place)")
    _graceful_close(surf, tool_name, log)
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
            _escalate_recycle_failure(label, surf, mode, "respawn-not-confirmed",
                                      "respawn didn't take; the OLD session is (almost certainly) "
                                      "still live on the seat; re-run fleet recycle when it is idle")
            return 1
        log("confirmed after direct-kill fallback")
    # (The old pre_sid snapshot + fresh-mode sid-exclusion lived here. Retired: the fresh confirm is now
    # LIVE-PID-resolved (rs.live_sid via _poll_session_back), so cmux's undropped stale sessions[]
    # entry — a DEAD pid by this point, the respawn was just verified — can never false-confirm, and no
    # exclusion set is needed.)
    if _exec_launch_enabled():
        _exec_launch(surf, guarded, log)                 # C: the launch IS the pane process (no paste)
    else:
        _fire_launch(surf, guarded, log)                 # legacy paste path (CMUX_FLEET_EXEC_LAUNCH=0)

    # CONFIRM is tool-aware. claude binds a session at BOOT -> poll for it (a NEW sid for fresh, the
    # surface live again for resume). codex (and others) bind LAZILY on their first turn AND fire no
    # SessionEnd, so the old store entry lingers after respawn -> there is no reliable pre-turn signal.
    # For lazy tools we don't poll: the session re-binds on the first turn and the router backfills it
    # (fresh -> clear the stale sid so the backfill takes; resume -> the sid is unchanged, keep it).
    lazy = p.get("tool", "claude") != "claude"
    if lazy:
        e = fs.live_get(label) or {}
        e["surface"] = surf
        e["gen"] = fs.e_gen(e) + 1                        # reseat fence (Ship 5): a recycle is a new seat
        if mode == "fresh":
            e["session"] = ""                            # a NEW session binds on 1st turn -> router backfills
            if p.get("cwd"):
                e["cwd"] = p["cwd"]                      # PERSIST the fresh cwd so the next RESUME finds the new session
        if "provider" in p:
            e["provider"] = p["provider"]                # fix 1: record the account the agent NOW runs under
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
                _escalate_recycle_failure(label, surf, mode, "resume-menu-wedged",
                                          "resume-summary menu never resolved; the seat is unbound/"
                                          "unregistered; re-run fleet recycle once it settles")
                return 1
        sid = _poll_session_back(surf, old_sid, mode, 90)
        if not sid and mode == "fresh" and not _exec_launch_enabled():
            # SELF-HEAL (PASTE PATH ONLY): the paste can crash into the bare shell (PATH not ready ->
            # wrapper 'claude not found'); the shell is fully initialized by now, so re-fire ONCE.
            # _fire_launch's TUI-up guard makes this NON-DESTRUCTIVE: if an agent is already live on the
            # seat (a confirm miss, an old-sid zombie, a cmux restart-resume), the re-fire is refused
            # instead of pasting the launch into its input box (berg-sandbox 2026-07-09) — we fall
            # through to WARN + escalation and let an actor decide.
            # The EXEC path has no self-heal by design: `-c` runs after shell init completes (the
            # PATH-not-ready class can't happen), and a crashed launch leaves the chained bare shell —
            # a no-bind there is a REAL failure that should escalate, not be papered over by a re-exec.
            log("no fresh session bound; attempting ONE self-heal re-fire (refused if a TUI is up)")
            if _fire_launch(surf, guarded, log):
                sid = _poll_session_back(surf, old_sid, mode, 60)
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
            _escalate_recycle_failure(label, surf, mode, "no-session-after-launch",
                                      "launch fired but nothing bound; the seat may be a bare shell, "
                                      "an old-session zombie, or a flagless default-model agent — "
                                      "check it, then re-run fleet recycle")
        else:
            if mode == "resume":
                # prefer cmux's CHECKPOINT (the id it will `--resume`) over a possibly-bridge poll id, so
                # the registry records the SAME id a later archive/revive resumes — killing the divergence
                # at the source. The router reconciles again on the next turn as a continuous backstop.
                sid = _resume_binding(surf).get("checkpoint_id", "") or sid
            log(f"{'resumed' if mode == 'resume' else 'fresh'} session {sid} bound")
            e = fs.live_get(label) or {}
            e["surface"] = surf
            e["gen"] = fs.e_gen(e) + 1                    # reseat fence (Ship 5)
            e["session"] = f"claude-{sid}" if e.get("tool", "claude") == "claude" else sid
            if mode == "fresh" and p.get("cwd"):
                e["cwd"] = p["cwd"]                      # PERSIST the fresh cwd (a role move -> new session lives here)
            if "provider" in p:
                e["provider"] = p["provider"]            # fix 1: record the account the agent NOW runs under
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
        }, event_key=f"peer:{bid}")                      # one broadcast id; per-surface in the ledger
        if not no_wake and fs.idlewake_on() and fs.wake_if_idle(surf, "(broadcast-wake) a fleet broadcast is waiting in your context; handle it"):
            fs.presented_mark(surf, [{"event_key": f"peer:{bid}"}], "wake")   # cooldown: no heartbeat re-nudge
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
      CMUX_FLEET_PLUGIN_INDEX <base>/plugins.toml (so the profile's plugins resolve from ITS index)
      CMUX_HOOKSTORE_DIR      <base>/state/hookstore (per-profile hook store; write-side follows via _profile_env)
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
    index = os.path.join(os.path.dirname(toml), "plugins.toml")   # the profile's index sits next to its toml
    hookstore = os.path.join(state, "hookstore")   # per-profile hook store: "share nothing" must cover cmux's hooks too
    binp = _fleet_bin_dir()                        # THIS build's fleet dir (checkout bin/ or installed script)

    if a.init:
        os.makedirs(state, exist_ok=True)
        os.makedirs(hookstore, exist_ok=True)      # must exist before any launch or the first hook write fails silently
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
    print(f'export CMUX_FLEET_PLUGIN_INDEX={shlex.quote(index)}')
    if not os.path.exists(index):
        sys.stderr.write(f"[fleet profile] note: no plugin index at {index} yet — declare "
                         f"[marketplace.<name>] there (see plugins.toml.example) if you use plugins=[...]\n")
    # Emit the READ-side hookstore pin; the WRITE-side (cmux's CMUX_AGENT_HOOK_STATE_DIR) then flows to every
    # child through _profile_env(), which fires because this export makes HOOKSTORE_EXPLICIT true. This is what
    # makes a profile's hook liveness private too — without it, side-by-side stacks would still share ~/.cmuxterm
    # and a test stack could SEE prod's agents. `--init` created the dir above.
    print(f'export CMUX_HOOKSTORE_DIR={shlex.quote(hookstore)}')
    print(f'export CMUX_BIN={shlex.quote(CMUX)}')
    if binp:
        print(f'export PATH={shlex.quote(binp)}:"$PATH"')
    else:
        sys.stderr.write("[fleet profile] warning: could not resolve THIS build's fleet bin dir; PATH not pinned "
                         "(set $CMUX_FLEET_BIN to the installed fleet path)\n")
    return 0


def _codex_seats():
    """Every declared codex subscription seat: [(acct, spec)]. The one filter, shared by the codex verbs."""
    from . import providers as pv
    return [(n, s) for tool, n, s, _ in pv.iter_providers()
            if tool == "codex" and s.get("type") == "subscription"]


def _codex_seat_preflight(home):
    """Bring ONE codex home fully up to fleet spec, in one pass, before a worker launches into it.

    A codex home needs TWO things the fleet owns, and they failed for the same reason — nobody was keeping
    them: the CITIZENSHIP doc (a codex worker loads no claude plugins, so without it a worker boots knowing
    nothing about the fleet it is a child of, including that it must report its own completion), and cmux's
    HOOK WIRING (without which its Stop hook fires into a void and no completion ever reaches the router).

    ONE function, called from ONE place in the launch, because two preflights on adjacent lines is exactly
    the drift this is meant to kill. FAIL-OPEN on both halves: an under-equipped worker is still a worker; a
    launch aborted over a doc or a hook is not."""
    from . import providers as pv
    for line in pv.codex_seat_sync(home).report():
        print(f"[fleet] {line}")


def cmd_codex_sync(argv):
    """fleet codex-sync [acct] [--check]   bring every codex seat's HOME up to fleet spec — in one pass.

    Two things live in a codex home and the fleet owns both:

      CITIZENSHIP  `$CODEX_HOME/AGENTS.md` — the one file a codex worker reads whatever its cwd. Codex loads
                   no claude plugins, so this is the ONLY channel the fleet has to it. Written as a FENCED
                   block (your own text outside the fence is left alone), and into `AGENTS.override.md`
                   instead when you have one, because an override REPLACES AGENTS.md rather than merging.
      HOOKS        cmux's hook wiring, INSTALLED AND TRUSTED TOGETHER. An untrusted hook does not run and
                   does not say so, and trust is content-bound — so hooks written without re-trusting are
                   exactly as dead as no hooks at all, while looking installed.

    You should rarely need this: `fleet launch --tool codex` syncs the home it is about to launch into. It
    is here for auditing (`--check`, which writes nothing) and for seeding homes ahead of time."""
    import argparse
    from . import providers as pv
    ap = argparse.ArgumentParser(prog="fleet codex-sync")
    ap.add_argument("acct", nargs="?", help="one codex seat; OMIT for every declared seat")
    ap.add_argument("--check", action="store_true",
                    help="report drift and write NOTHING (exit 1 if any home is out of spec)")
    a = ap.parse_args(argv)

    seats = _codex_seats()
    if a.acct:
        seats = [(n, s) for n, s in seats if n == a.acct]
        if not seats:
            sys.exit(f"[fleet] codex-sync: no [providers.codex.{a.acct}] in fleet.toml")
    homes = []
    for acct, spec in seats:
        try:
            homes.append((acct, pv.codex_seat_home(acct, spec)))
        except pv.ProviderError:
            print(f"[fleet] seat '{acct}': no home declared — skipping (config gap, not a sync failure)")
    if not seats:
        # No seats declared at all = the single-account user, whose codex workers run in codex's own home.
        # That worker needs citizenship and hooks exactly as much as a seated one does.
        homes = [("(default)", os.path.expanduser(CODEX_DEFAULT_HOME))]

    drift = 0
    for acct, home in homes:
        r = pv.codex_seat_status(home) if a.check else pv.codex_seat_sync(home)
        drift += bool(a.check and not r.ok)
        print(f"  {acct:<14} citizenship={r.doc_status:<10} hooks={r.hooks_status:<12} {home}")
        for line in r.notes():
            print(f"    {line}")
    if a.check and drift:
        print(f"[fleet] codex-sync --check: {drift} home(s) out of spec — `fleet codex-sync` to fix")
        return 1
    return 0


def cmd_codex_setup(argv):
    """fleet codex-setup <acct>   SUPERSEDED by `fleet codex-login <acct>`.

    It provisioned the shared-home env-token model (a fleet cred store + a fenced [model_providers.<acct>]
    block in ~/.codex/config.toml). That model is the supersession BUG: it pinned every seat to the single
    ~/.codex device, and the backend keys one active session per device. It refuses rather than hands out
    config the launch resolver now rejects."""
    acct = (argv[0] if argv and not argv[0].startswith("-") else "<acct>")
    sys.exit(
        f"[fleet] codex-setup is SUPERSEDED — use `fleet codex-login {acct}`.\n"
        f"  It set up the shared-home env-token model (one ~/.codex for every seat). The ChatGPT backend\n"
        f"  keys one active session per DEVICE, and the device id lives in the codex HOME — so seats sharing\n"
        f"  a home revoke each other on every login. Each seat now needs its OWN home:\n"
        f"      [providers.codex.{acct}]\n      type = \"subscription\"\n"
        f"      auth = \"codex-home:~/.codex-{acct}\"\n"
        f"  then: fleet codex-login {acct}")


def _codex_login_surface(home, cwd):
    """Open a cmux TERMINAL TAB with the login command TYPED BUT NOT EXECUTED. Returns the surface id, or ''.

    A TAB, never a split: `new-surface --pane <uuid>` SPLITS the pane even when given a uuid, which would
    carve up whatever the operator is looking at. Targeting the WORKSPACE makes a tab.

    The command is sent WITHOUT a trailing \\r on purpose — the operator must sign out of chatgpt.com first,
    and a login fired before that silently reuses the browser session and authenticates the WRONG account."""
    ws = ""
    try:
        ws, _pane = surface_loc(os.environ.get("CMUX_SURFACE_ID", "") or "")
    except Exception:
        ws = ""
    args = ["new-surface", "--type", "terminal", "--working-directory", cwd]
    if ws:
        args += ["--workspace", ws]                  # workspace => a TAB (a --pane would SPLIT)
    surf = (cmuxq(*args) or "").strip().split("\n")[-1].strip()
    if not surf:
        return ""
    cmuxq("send-panel", "--panel", surf, "--", f"CODEX_HOME={home} codex login")   # typed, NOT executed
    return surf


def codex_verify_seat(home):
    """HARD verification of a seat AS IT STANDS. Returns (ok, probe, detail). NEVER logs in — so it is always
    safe to call on a working seat, which matters because a login would supersede it.

    Both halves are required. A backend /me 200 alone is not enough (it does not prove this home can actually
    run), and a model turn is the only thing a 401 cannot counterfeit."""
    from . import providers as pv
    tok = pv.codex_home_token(home)
    if not tok:
        return False, "no-token", "no auth.json in the home (never logged in)"
    probe = pv.codex_probe_backend(tok)
    if probe != "live":
        return False, probe, f"backend says {probe}"
    ok, detail = pv.codex_seat_spoke(home)           # the model must actually SPEAK
    return ok, probe, detail


def _codex_login_seat(acct, home, timeout):
    """Log ONE seat into its own home, then verify hard. Returns (ok, email). Interactive: opens a tab."""
    import time as _t
    from . import providers as pv
    os.makedirs(home, exist_ok=True)                 # codex ERRORS on a CODEX_HOME that does not exist
    _codex_seat_preflight(home)                      # a seat is a full citizen from birth: the verify run
                                                     # below is this home's FIRST codex run, and by then it
                                                     # should already have its doc and its hooks
    print(f"\n[fleet] codex-login '{acct}' -> home {home}")
    print( "  1. SIGN OUT of chatgpt.com in your browser FIRST.")
    print( "     A login that reuses the browser session authenticates the WRONG account, and the fleet")
    print( "     cannot tell you asked for a different one — it can only tell you which one you got.")
    print(f"  2. A terminal tab is opening with the command typed. Press Enter to run it, pick '{acct}'.")

    if not _codex_login_surface(home, os.getcwd()):
        print(f"  (could not open a cmux tab — run it yourself:  CODEX_HOME={home} codex login)")

    authjson = os.path.join(home, "auth.json")
    print(f"[fleet] waiting up to {timeout}s for {authjson} ...")
    end = _t.time() + timeout
    while _t.time() < end and not os.path.exists(authjson):
        _t.sleep(2)
    if not os.path.exists(authjson):
        print(f"[fleet] codex-login: timed out — no auth.json in {home}. Nothing was changed.")
        return False, ""

    got = (pv._codex_identity(home) or {}).get("email") or "(unknown)"
    print(f"[fleet] auth.json appeared. account obtained: {got}")
    ok, probe, detail = codex_verify_seat(home)
    print(f"  backend /me : {probe}")
    print(f"  model turn  : {'YES — ' + detail if ok else 'NO — ' + detail}")
    # installation_id is minted on the FIRST RUN, not at login — so it only exists now, after the verify run.
    print(f"  device id   : {pv.codex_home_installation_id(home)[:8] or '(not yet minted)'}")
    if not ok:
        print(f"[fleet] seat '{acct}' did NOT verify. It is not usable. "
              f"(A backend 200 AND the model speaking are both required.)")
        return False, got
    print(f"[fleet] seat '{acct}' VERIFIED ({got}).")
    return True, got


def cmd_codex_login(argv):
    """fleet codex-login [acct] [--timeout N] [--verify-only]   log codex SEATS into their OWN homes.

    With no acct it CYCLES EVERY codex seat, which is the normal way to bring the fleet up: seats are done one
    at a time (each login needs its own signed-out browser session) and a seat that ALREADY VERIFIES IS
    SKIPPED, never re-logged.

    That skip is the whole safety property, not an optimization. Every `codex login` supersedes that account's
    previous session, so "just re-login everything to be sure" is precisely how you break the seats that were
    working. The only safe cycle is one that proves a seat is fine and then LEAVES IT ALONE.

    Each seat needs its own CODEX_HOME: the backend keys one active session per DEVICE, and the device id
    (installation_id) is a per-home file. Seats sharing a home supersede each other; seats in their own homes
    run concurrently. A seat that declares no home is a CONFIG gap — reported, never guessed at.

    Verification is backend /me 200 AND the model actually SPEAKING (`codex exec -o`, which only an
    authenticated run can write). It reports the email actually obtained, because a browser session that was
    not signed out authenticates the WRONG account — and that must be caught, never celebrated."""
    import argparse
    from . import providers as pv
    ap = argparse.ArgumentParser(prog="fleet codex-login")
    ap.add_argument("acct", nargs="?",
                    help="one codex seat; OMIT to cycle every seat (skipping the ones already verified)")
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait for each login (default 300)")
    ap.add_argument("--verify-only", action="store_true",
                    help="verify the seat(s) as they stand; never open a login (safe: a login supersedes)")
    a = ap.parse_args(argv)

    seats = _codex_seats()
    if a.acct:
        seats = [(n, s) for n, s in seats if n == a.acct]
        if not seats:
            sys.exit(f"[fleet] codex-login: no [providers.codex.{a.acct}] in fleet.toml")
    if not seats:
        sys.exit("[fleet] codex-login: no codex subscription seats in fleet.toml — nothing to log in.")

    results = []                                     # (acct, status, email, home)
    for acct, spec in seats:
        try:
            home = pv.codex_seat_home(acct, spec)    # loud + actionable when undeclared; NEVER guessed
        except pv.ProviderError as e:
            print(f"\n[fleet] seat '{acct}': NO HOME DECLARED — skipping (this is config, not a login).\n{e}")
            results.append((acct, "needs-home", "", ""))
            continue

        # THE INTERLOCK, and it must come BEFORE anything else touches this home. Verification RUNS codex (the
        # model has to speak), and a codex run is what MINTS the home's installation_id. So if this home holds
        # a person who is already logged into another home, merely verifying it would mint the second device
        # for that identity and supersede the seat we were trying to protect: the check would destroy the very
        # thing it was checking. A pure READ of auth.json is the only safe order of operations.
        clash = pv.codex_seat_collision(acct, home)
        if clash:
            ident = pv._codex_identity(home) or {}
            print(f"\n[fleet] seat '{acct}': WRONG ACCOUNT IN THIS HOME — refusing to touch it.")
            print(f"  {home} holds {ident.get('email')}, who is ALREADY seat '{clash}'.")
            print( "  That is one PERSON in two homes = two devices for one identity, and they will supersede")
            print( "  each other. (Sharing a team SUBSCRIPTION is fine and expected — teammates do it. Sharing")
            print( "  a person is not.) It happens when a login reuses a chatgpt.com session that was never")
            print( "  signed out, so codex authenticated whoever the browser was already.")
            print(f"  FIX: sign out of chatgpt.com, remove {os.path.join(home, 'auth.json')}, then re-run")
            print(f"       `fleet codex-login {acct}` and pick the intended account.")
            print( "  Nothing was run in that home, so no second device was minted.")
            results.append((acct, f"WRONG ACCOUNT (= {clash})", ident.get("email") or "", home))
            continue

        # Already good? Then STOP. Re-logging a working seat is not a harmless no-op — it supersedes the very
        # session you were checking. This is what makes cycling ALL seats safe to run at any time.
        ok, probe, detail = codex_verify_seat(home)
        if ok:
            ident = pv._codex_identity(home) or {}
            email = ident.get("email") or "(unknown)"
            print(f"\n[fleet] seat '{acct}' is ALREADY authenticated — not logging in again "
                  f"(a login would supersede it).")
            print(f"  home        : {home}")
            print(f"  account     : {email}")
            print(f"  device id   : {pv.codex_home_installation_id(home)[:8] or '(not yet minted)'}")
            print(f"  backend /me : {probe}")
            print(f"  model turn  : {detail}")
            results.append((acct, "verified (already)", email, home))
            continue

        if a.verify_only:
            print(f"\n[fleet] seat '{acct}' is NOT usable: {detail} (home {home})")
            results.append((acct, f"NOT usable ({probe})", "", home))
            continue

        lok, email = _codex_login_seat(acct, home, a.timeout)
        results.append((acct, "verified" if lok else "FAILED", email, home))

    print("\n[fleet] codex seats:")
    for acct, status, email, home in results:
        print(f"  {acct:<14} {status:<20} {email or '-':<28} {home or '-'}")
    good = [r for r in results if r[1].startswith("verified")]
    bad = [r for r in results if not r[1].startswith("verified")]
    if good:
        print(f"[fleet] launch one: fleet launch <role> --tool codex --provider codex:{good[0][0]}")
    if bad:
        sys.exit(f"[fleet] {len(bad)} seat(s) not usable: {', '.join(r[0] for r in bad)}")
    return 0


# ---------------------------------------------------------------- usage (ONE source of truth)
# VERB_USAGE is BOTH halves of help: `fleet --help` prints the joined values, and `fleet <verb> --help`
# prints the one entry (see main()'s guard). Keep the two leading spaces and the insertion order — the
# joined blob is the top-level help verbatim, and tests/test_help.py pins that it stays that way.
USAGE_HEADER = ("usage: fleet <launch|config|ls|plugins|archive|revive|register|recycle|move|group|unstick|"
                "sessions|broadcast|mute|unmute|rm|vitals|usage|find|graph|serve|paint|worktree|profile|"
                "daemon|drive-child|peer-msg|child-digest|inbox|inbox-ack> ...")
VERB_USAGE = {
    "launch": "  launch <role|--adhoc NAME> [--tool t] [--place p] [--parent s] [--effort L] [--model M] [--plugin NAME] [--provider NAME] [--dry-run] [-- <tool flags>]",
    "config": "  config <role|--adhoc NAME|--cwd DIR> [--tool t]   effective config (base settings + fleet adds)",
    "ls": "  ls [--scope mine|all|conductors|children] [--json] live fleet x hook store; flags STALE + archived (default mine = you + your children; --scope all = the world)",
    "plugins": "  plugins <add|reconcile|ls|show|describe> ...      the plugin INDEX: add-from-URL (safe: never enables) + reconcile + on-demand discovery",
    "archive": "  archive <label>                                   park a live agent (revivable)",
    "revive": "  revive <label> [--fresh] [--session id] [--place p] [--parent s] [--plugin N] [-- <flags>]\n"
              "                                                    bring a parked agent back (default RESUME last session; --fresh sheds; --session targets an arbitrary prior one)",
    "register": "  register <label> [--surface UUID] [--parent s] [--session id]\n"
                "                                                    pull a LIVE-but-unregistered agent into the registry (recovery for a skipped auto-register)",
    "move": "  move <label> (--to-workspace WS | --own-workspace) [--name TITLE]\n"
            "                                                    relocate a LIVE child natively — surface move + registry update, keeping pid/session/context/parent/group (cmux 0.64.18+ heals the moved surface; no archive/revive/fresh-surface). An ARCHIVED label has no surface: `fleet revive` it into the target instead",
    "group": "  group <init [--name N] | add <label> [--name N]>  make THIS conductor's workspace a named group (init) or retrofit a live child into it (add); membership ops keep agents live (the safe lane)",
    "recycle": "  recycle [label] [--fresh] [--session id] [--effort L] [--model M] [--force] [--plugin NAME] [--prime T|--no-prime] [-- <flags>]\n"
               "                                                    restart in place, same surface/identity (default self+RESUME; --fresh sheds; --plugin = index-aware plugin add, reaches linked + enabled)\n"
               "  recycle --scope mine|all|conductors|children [--include-muted] [--dry-run]\n"
               "                                                    BULK restart (sequential + gated, skips self + muted); mine = your children; cross-conductor = the safe topology",
    "unstick": "  unstick [label] [--surface UUID] [--dry-run]      reap a frozen dead-pid hook-store ghost (SessionEnd-less death) so ls/recycle/doctor stop trusting a dead 'running'; never touches a LIVE record",
    "reap-surfaces": "  reap-surfaces [--all] [--json] [--close]          DRY-RUN survey of orphaned bare-shell HUSK surfaces (fleet launch artifact + no live agent + no registry); gated on the fleet env prefix + tail guard; --close is review-gated",
    "reconcile-restore": "  reconcile-restore [--close] [--json]            reconcile the registry against cmux's crash-restore snapshot: survey resume-orphans + husks; --close archives-first + closes the DETERMINISTIC husks (snapshot agent=nil + no live agent + not registered + fleet-origin), never a live agent/human shell",
    "sessions": "  sessions <label> [--all] [--json]                 list resumable prior sessions for the agent's surface (id, age, size, snippet)",
    "broadcast": "  broadcast \"<msg>\" --scope mine|all|conductors|children [--no-wake] [--expect-reply] [--dry-run]\n"
                 "                                                    input-safe heads-up to live agents (e.g. after a toml/floor change); never restarts them; --scope REQUIRED (an act)",
    "mute": "  mute <label> | unmute <label> [| --scope mine]    stop/resume pushing a child's completions to its parent (parent reads on demand); --scope mine = all my children",
    "rm": "  rm <label> [--detach] [--force] [--kill] [--wip-commit] [--with-group]\n"
          "                                                    close + archive a label (revivable; refuses mid-turn, --force overrides); --detach drops the row only; --kill adds worktree teardown; --with-group dissolves its workspace-group",
    "vitals": "  vitals [--scope mine|all|conductors|children] [--json] [--paint] [--no-probe] [--watch [--interval N]] cheapest-first triage table: blocked (waiting on YOU: yes/no/?) + ctx-remaining % (default mine)",
    "usage": "  usage [--json]                                    per-provider subscription windows (5h + weekly bars, reset countdowns, metered/Fable flags, live attribution) from the daemon poller",
    "codex-setup": "  codex-setup <acct>                       SUPERSEDED -> use `codex-login` (it set up the shared-home env-token model, which is the supersession bug)",
    "conformance": "  conformance [--json] [--trials N] [--tool claude|codex|both] [--keep]   does THIS cmux build actually DO what the fleet depends on? Exercises every cmux capability the fleet uses against a LIVE cmux and reports PASS/FAIL/UNKNOWN — each check proving the EFFECT, never the invocation (exit 0 is not a pass, and our own cmuxq() DISCARDS the return code, so cmux's errors arrive as screen content). Run it on stable, run it on nightly, DIFF the two: that diff is the breaking-change report. Safe by construction: its own workspace, its own agents, its own throwaway fleet state, and structurally incapable of touching a fleet member",
    "codex-sync": "  codex-sync [acct] [--check]                      bring each codex seat's HOME up to fleet spec, in one pass: the CITIZENSHIP doc ($CODEX_HOME/AGENTS.md — the only file a codex worker reads whatever its cwd, since it loads no claude plugins; fenced, so your own text survives) AND cmux's HOOK WIRING, installed and TRUSTED together (an untrusted hook does not run and does not say so, so no completion ever reaches the router). `fleet launch --tool codex` syncs the home it launches into, so this is for auditing (--check) and seeding",
    "codex-login": "  codex-login [acct] [--timeout N] [--verify-only]  log codex SEATS into their OWN homes (each seat needs one: the backend keys a session per device, and the device id is per-home). NO acct = cycle every seat, SKIPPING any that already verify (a login supersedes, so re-logging a working seat breaks it). Opens a terminal tab with the login typed, waits, then VERIFIES with a backend 200 + the model actually speaking, and reports the account it really got",
    "find": "  find <query> [--turns N] [--json]                 content-aware session lookup (label/role/cwd or transcript)",
    "graph": "  graph [--scope mine|all|<label>] [--json] [--html] [--out FILE]  fleet parentage tree (text/JSON/HTML); default mine = your subtree; --scope all = full tree",
    "groups": "  groups [--json]                                   the fleet's groups BY LABEL — members per cmux's REAL membership (not the stored `group` field); flags registry-vs-cmux divergence (ghost/unfiled)",
    "serve": "  serve [--port N]                                  thin read-only localhost view (graph HTML + vitals.json); no daemon",
    "paint": "  paint [--sidebar]                                 sync fleet state onto the cmux sidebar (status pills + ctx bars; --sidebar also feeds fleet.swift)",
    "worktree": "  worktree <ls | clean <label> [--wip-commit]>      manage fleet-owned git worktrees (config-gated, default-off)",
    "profile": "  profile <name> [--base DIR] [--root DIR] [--init]  emit env that pins ALL entrypoints at THIS build (eval it for multi-build isolation)",
    "daemon": "  daemon <start|stop|status|restart> [--foreground] [--heartbeat [SECS]]  run the router as a detached daemon (survives shell exit + recycle); start --foreground for launchd",
    "drive-child": "  drive-child <surface-uuid> <prompt...>            submit a prompt to a child's TUI (beats the paste-settle enter-race)",
    "peer-msg": "  peer-msg <to-label>|--to-parent \"<body>\" [--no-reply] [--reply-to <id>] [--expect-reply] [--no-wake]\n"
                "                                                    input-safe A2A: message a live PEER by label (or --to-parent: your registry-resolved conductor), into its context never its input box",
    "child-digest": "  child-digest <session-frag> [N]                   print a child's last N transcript turns (the reliable content source)",
    "inbox": "  inbox [--scope mine|<label>|all|conductors|children] [--json]  pending inbox on demand (default mine = yours; <label> peeks one; all = triage) — the catch-up read after a recycle",
    "inbox-ack": "  inbox-ack <seq> [--peer|--stale|--doctor] [--surface UUID]  mark shown completions/alerts/peer msgs handled so they stop re-surfacing",
}
# `unmute` shares mute's entry (one blob line covers both verbs) — alias it so `fleet unmute --help`
# resolves. Without this it would fall through the guard and (un)mute a label named '--help'.
USAGE_ALIAS = {"unmute": "mute"}
# The two internal `_`-prefixed workers are NOT in VERB_USAGE (they must stay out of `fleet --help` —
# they are not human verbs), but they are in the verb table, so they need a usage entry of their own or
# `--help` would hand '--help' to json.load(open(...)) and traceback.
INTERNAL_USAGE = {
    "_conformance-exec": "  _conformance-exec [flags]                        internal: the ISOLATED child of `fleet conformance` (it refuses to run unless `fleet conformance` set up its throwaway state)",
    "_recycle-exec": "  _recycle-exec <payload.json>                      internal: the detached single-recycle worker (spawned by `fleet recycle`)",
    "_recycle-bulk-exec": "  _recycle-bulk-exec <payloads.json>               internal: the detached sequential bulk-recycle orchestrator (spawned by `fleet recycle --scope`)",
}
# Verbs that render their OWN `--help`: an ArgumentParser (richer, auto-generated, per-flag) or a
# sub-verb dispatcher that already prints its usage (plugins, worktree). main()'s guard skips these and
# lets them handle it. EVERY OTHER verb in the table is hand-rolled and gets its help from the guard.
# Both halves are pinned by tests/test_help.py, which loops the whole table — that is what stops this
# rotting as verbs gain parsers (the 2026-07-11 bug: 16 verbs where `--help` was a positional label or,
# worse, silently ignored — `fleet serve --help` STARTED THE HTTP SERVER and blocked).
SELF_HELP_VERBS = frozenset({
    "launch", "config", "plugins", "revive", "register", "recycle", "move", "group", "unstick", "migrate",
    "reap-surfaces", "reconcile-restore", "sessions", "worktree", "profile", "daemon", "find", "codex-login", "codex-sync",
    # codex-setup is NOT here any more: it lost its ArgumentParser when it became a superseded-stub, so it
    # can no longer render its own --help. The guard now serves it from VERB_USAGE (which is the whole point
    # of the guard: a verb without a parser must never be RUN just to ask it for help).
})


def cmd_migrate(argv):
    """fleet migrate [--dry-run] [--no-backup]   ONE-SHOT registry schema migration to v2 (Ship 5 thin-
    registry). Rewrites every fleet.json + archive.json row into identity + spec + binding, DROPPING the
    fields cmux now owns (workspace, status) so the registry can no longer lie about where an agent is, and
    flips the schema marker to 2. Idempotent (safe to re-run). Backs up the v1 files first
    (<file>.v1.bak-<ts>) unless --no-backup. This is the deliberate, reversible-from-backup switch: the
    fleet reads BOTH shapes, so adopting the build changes NOTHING on disk until you run this."""
    from . import state as fs
    ap = argparse.ArgumentParser(prog="fleet migrate", add_help=True)
    ap.add_argument("--dry-run", action="store_true", help="report what WOULD migrate; touch nothing")
    ap.add_argument("--no-backup", action="store_true", help="skip the v1 backup (NOT recommended)")
    a = ap.parse_args(argv)
    fver, aver = fs.schema_ver("fleet"), fs.schema_ver("archive")
    live_n, arch_n = len(fs.live_all()), len(fs.archive_all())
    if fver >= fs.SCHEMA_CURRENT and aver >= fs.SCHEMA_CURRENT:
        print(f"[fleet] migrate: already at schema v{fs.SCHEMA_CURRENT}. Nothing to do "
              f"(fleet {live_n} rows, archive {arch_n} rows).")
        return 0
    if a.dry_run:
        print(f"[fleet] migrate --dry-run: would migrate fleet.json ({live_n} rows) + archive.json "
              f"({arch_n} rows) from v{fver}/v{aver} to v{fs.SCHEMA_CURRENT} — splits identity/spec/binding, "
              f"drops workspace+status (derived), adds gen. Backup: {'skipped' if a.no_backup else 'yes'}.")
        return 0
    res = fs.migrate_state(backup=not a.no_backup)
    print(f"[fleet] migrated fleet.json ({res['fleet']} rows) + archive.json ({res['archive']} rows) "
          f"to schema v{fs.SCHEMA_CURRENT}.")
    for b in res["backups"]:
        print(f"[fleet]   v1 backup: {b}")
    print("[fleet] the registry now stores identity + spec + binding; workspace/status derive live from cmux.")
    return 0


def usage_for(verb):
    """The `fleet <verb> --help` text, or None if the verb has no hand-rolled usage entry."""
    entry = VERB_USAGE.get(USAGE_ALIAS.get(verb, verb)) or INTERNAL_USAGE.get(verb)
    return None if entry is None else "usage: fleet " + entry.lstrip()


def verb_table():
    """verb -> handler. A function, not a module constant: building it imports features/daemon/helpers,
    which the hook-verb hot path in main() must never pay for. tests/test_help.py enumerates it."""
    from . import features as ff
    from . import daemon as fd
    from . import helpers as fh
    from . import conformance as cf
    from . import reconcile as rc
    return {"launch": cmd_launch, "config": cmd_config, "ls": cmd_ls, "plugins": cmd_plugins,
            "reconcile-restore": rc.cmd_reconcile_restore,
            "archive": cmd_archive, "revive": cmd_revive, "register": cmd_register, "recycle": cmd_recycle,
            "move": cmd_move, "group": cmd_group, "migrate": cmd_migrate,
            "unstick": cmd_unstick, "reap-surfaces": cmd_reap_surfaces, "sessions": cmd_sessions,
            "_recycle-exec": cmd_recycle_exec, "_recycle-bulk-exec": cmd_recycle_bulk_exec,
            "broadcast": cmd_broadcast,
            "mute": lambda a: cmd_mute(a, mute=True), "unmute": lambda a: cmd_mute(a, mute=False),
            "rm": cmd_rm, "worktree": cmd_worktree, "profile": cmd_profile, "daemon": fd.cmd_daemon,
            "vitals": ff.cmd_vitals, "usage": ff.cmd_usage, "find": ff.cmd_find, "graph": ff.cmd_graph,
            "groups": ff.cmd_groups,
            "conformance": cf.cmd_conformance, "_conformance-exec": cf.cmd_conformance_exec,
            "codex-setup": cmd_codex_setup,
            "codex-login": cmd_codex_login,
            "codex-sync": cmd_codex_sync,
            "serve": ff.cmd_serve, "paint": ff.cmd_paint,
            "drive-child": fh.cmd_drive_child, "peer-msg": fh.cmd_peer_msg,
            "child-digest": fh.cmd_child_digest, "inbox": fh.cmd_inbox, "inbox-ack": fh.cmd_inbox_ack}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(USAGE_HEADER + "\n" + "\n".join(VERB_USAGE.values()))
        return 0
    sub, rest = sys.argv[1], sys.argv[2:]
    # Hook verbs are the per-turn hot path (a plugin shim shells into them on every UserPromptSubmit/Stop).
    # Dispatch them FIRST, before the heavier feature/daemon/helper imports, to keep that path lean.
    if sub in ("hook-awareness", "hook-drain"):
        from . import hookverbs as hv
        return (hv.cmd_hook_awareness if sub == "hook-awareness" else hv.cmd_hook_drain)(rest)
    # `fleet <verb> --help` for the hand-rolled verbs, BEFORE they get a chance to run. Fires ONLY when
    # -h/--help is the FIRST token: peer-msg/drive-child/broadcast carry free text, so a message body that
    # mentions --help must still be DELIVERED, never swallowed by help. Never scan the tail.
    if rest[:1] and rest[0] in ("-h", "--help") and sub not in SELF_HELP_VERBS:
        text = usage_for(sub)
        if text:                                              # unknown verbs fall through to the error below
            print(text)
            return 0
    fns = verb_table()
    if sub in fns:
        try:
            return fns[sub](rest)
        except Exception as e:
            # A broken fleet toml raises ProviderError from any provider-reading verb (the codex operator
            # verbs iterate providers unwrapped). Turn it into a clean, named refusal instead of a raw
            # traceback — consistent with the graceful aborts launch/recycle/poll_all already give. (Import
            # locally: providers is a heavy optional import and this is the cold error path.)
            from . import providers as pv
            if isinstance(e, pv.ProviderError):
                sys.exit(f"[fleet] ABORT: {e}")
            raise
    sys.exit(f"fleet: unknown subcommand '{sub}'")


if __name__ == "__main__":
    raise SystemExit(main())
