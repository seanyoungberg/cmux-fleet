#!/usr/bin/env python3
# fleet.py - the native-cmux fleet CLI. ONE tool, tool-agnostic. The `fleet` namespace is the
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
import argparse, json, os, shlex, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import ROOT, STATE, CMUX, MARKETPLACE, FLOOR, FLEET_TOML, ADHOC_SUBDIR  # path resolver

REGISTRY = os.path.join(STATE, "fleet-registry.json")

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
        "enable_plugins": enable_plugins, "setting_sources": setting_sources,
    }


def _dedup(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
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
def _claude_settings_args(spec):
    """`--settings` args for claude: the role's `settings` (a file path or inline JSON) plus an
    enabledPlugins object synthesized from `enable_plugins` (EXTERNAL marketplace plugins to flip on
    for this agent). enabledPlugins format is {"<plugin>@<marketplace>": true} (the same shape claude
    writes in settings.json). We emit ONE --settings when we can (role settings is inline JSON or
    absent -> fold them together); only when the role pins a settings FILE *and* also enables plugins
    do we emit two --settings, which is safe because the cmux-claude-wrapper deep-merges multiple
    --settings (and its own hooks) into a single one before claude ever sees them (verified in
    Resources/bin/cmux-claude-wrapper). The JSON must be valid or the wrapper warns + drops it."""
    base = (spec.get("settings") or "").strip()
    ep = {name: True for name in (spec.get("enable_plugins") or [])}
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


def adapter_compile(tool, spec, caller_tokens):
    """Compile {plugins, flags, env, settings} + caller passthrough -> (bin, arg_tokens, env_map)
    for the given tool. Adding a tool = adding a branch here + a [tool.<t>] block."""
    # spec['flags'] is already a token list (tool-floor<-role); layer the caller passthrough on top.
    merged = _layer_tokens([spec["flags"], list(caller_tokens or [])])
    env = dict(spec["env"])
    env["AGENT_ROLE"] = spec["role"]                          # behavioral type (exposed to the agent)
    env["AGENT_LABEL"] = spec["label"]                        # unique instance -> routing/recycle

    if tool == "claude":
        args = []
        if spec.get("setting_sources"):                      # which settings layers claude loads
            args += ["--setting-sources", spec["setting_sources"]]
        for name in spec["plugins"]:                          # INTERNAL plugins: load + auto-enable
            pd = _plugin_dir(name)
            if pd:
                args += ["--plugin-dir", pd]
            else:
                print(f"[fleet] warn: plugin '{name}' not resolvable (marketplace unset or not found); skipping")
        args += merged
        args += _claude_settings_args(spec)                  # role `settings` + EXTERNAL enabledPlugins
        return "claude", args, env

    if tool == "codex":
        # Stub: codex has its own plugin/settings vocabulary; flags+env passthrough work today.
        # plugins/settings are claude concepts -> warn if a codex role tries to use them.
        if spec["plugins"] or spec["settings"]:
            print("[fleet] warn: 'plugins'/'settings' are claude-only; ignored for codex")
        return "codex", merged, env

    sys.exit(f"fleet: unknown tool '{tool}' (no adapter)")


# ---------------------------------------------------------------- cmux placement (ported, proven)
def _store():
    import fleet_state as fs                                  # union of all per-agent hook stores
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
        gref = group
        if not group.startswith("workspace_group:"):
            try:
                gd = json.loads(cmuxq("workspace-group", "list", "--json"))
                gref = next((g["ref"] for g in gd.get("groups", []) if g.get("name") == group), "")
            except Exception:
                gref = ""
        if not gref:
            print(f"[fleet] ABORT: cannot resolve group '{group}' to a ref"); return None, None
        out = cmuxq("new-workspace", "--group", gref, "--name", spec["label"],
                    "--cwd", spec["abs_cwd"], "--focus", "false")
        m = re.search(r"(workspace:\d+)", out)
        if not m:
            print(f"[fleet] ABORT: new-workspace gave no workspace ref: {out.strip()}"); return None, None
        ws = _ref_to_uuid("workspace", m.group(1))
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


def register(surf, spec, parent_surface, session, ws):
    import fleet_state as fs
    parent_label = fs.label_for_surface(parent_surface) or parent_surface   # store parent LABEL (durable)
    fs.live_put(spec["label"], {
        "role": spec["role"], "kind": spec["kind"], "tool": spec["tool"],
        "cwd": spec["abs_cwd"], "parent": parent_label, "place": spec["place"], "status": "live",
        "surface": surf, "workspace": ws,
        "session": f"claude-{session}" if spec["tool"] == "claude" else session,
        # carried so archive->revive can rebuild the launch without re-resolving the roster
        "plugins": spec["plugins"], "flags": spec["flags"], "settings": spec["settings"],
        "group": spec["group"]})


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


def render_send_cmd(bin_name, args, env, abs_cwd):
    parts = [f"cd {shlex.quote(abs_cwd)} &&"]
    for k, v in env.items():
        parts.append(f"{k}={shlex.quote(str(v))}")
    parts.append(bin_name)
    # shlex.quote every arg: it's a no-op for safe tokens (flags, paths) but is REQUIRED for inline
    # JSON values like --settings '{"enabledPlugins":...}' — compact JSON has no spaces yet is full of
    # shell metacharacters ({ } " ), and the old space-only guard let the shell mangle it (brace
    # expansion / quote stripping) -> claude got malformed args and never bound a session.
    parts += [shlex.quote(a) for a in args]
    return " ".join(parts)


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
    ap.add_argument("--plugins", help="ad-hoc: comma-separated plugin names (adds to floor)")
    ap.add_argument("--dry-run", action="store_true", help="resolve + print, do NOT spawn")
    a = ap.parse_args(argv)
    if not a.role and not a.adhoc:
        ap.error("need a <role> or --adhoc <name>")

    cfg = load_config()
    spec = resolve(cfg, a.role, a.tool, a.adhoc)
    if a.place:
        spec["place"] = a.place
    if a.group:
        spec["group"] = a.group
    if a.label:
        spec["label"] = a.label
    if a.adhoc and a.plugins:
        spec["plugins"] = _dedup(spec["plugins"] + [p.strip() for p in a.plugins.split(",") if p.strip()])
    spec["abs_cwd"] = a.cwd or (spec["cwd"] if os.path.isabs(spec["cwd"]) else os.path.join(ROOT, spec["cwd"]))

    bin_name, args, env = adapter_compile(spec["tool"], spec, caller)
    send_cmd = render_send_cmd(bin_name, args, env, spec["abs_cwd"])

    print(f"[fleet] tool={spec['tool']} role/label={spec['label']} kind={spec['kind']} place={spec['place']}")
    print(f"[fleet] cwd={spec['abs_cwd']}")
    print(f"[fleet] launch: {send_cmd}")
    if a.dry_run:
        print("[fleet] dry-run (omit --dry-run to spawn)")
        return 0
    if not a.parent:
        sys.exit("[fleet] ABORT: no --parent and no $CMUX_SURFACE_ID")

    os.makedirs(spec["abs_cwd"], exist_ok=True)
    if a.adhoc:                                          # ad-hoc cwds are created fresh at launch ->
        _link_floor_claudemd(spec["abs_cwd"])            # symlink the floor CLAUDE.md so they inherit it
    ws, surf = create_surface(spec, a.parent, a.direction)
    if not ws or not surf:
        sys.exit(1)
    print(f"[fleet] target ws={ws} surface={surf}")
    cmuxq("send", "--workspace", ws, "--surface", surf, send_cmd + "\n")
    # claude binds a session at BOOT; codex (and the other cmux agents) register LAZILY on their first
    # turn. So poll briefly but DON'T fail if there's no session yet -> register the surface now and let
    # the session BACKFILL on the child's first turn (the router does this when it sees the first Stop).
    lazy = spec["tool"] != "claude"
    print(f"[fleet] waiting for cmux to bind a session to {surf} ...")
    sid = poll_session(surf, timeout=8 if lazy else 60)
    if not sid and not lazy:
        sys.exit("[fleet] timed out waiting for session binding")
    register(surf, spec, a.parent, sid or "", ws)
    log_launch(spec, a.parent, surf, sid or "", send_cmd)
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
    import fleet_state as fs
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


# ---------------------------------------------------------------- lifecycle verbs (the conductor's job)
def _store():
    import fleet_state as fs                                  # union of all per-agent hook stores
    return fs.read_hook_store()


def _pid_for_surface(surface):
    for s in (_store().get("sessions") or {}).values():
        if s.get("surfaceId") == surface:
            return s.get("pid")
    return None


def cmd_ls(argv):
    """Reconcile the live registry against cmux's hook store. Flags STALE = registry says live but the
    surface has no live session (a closed tab / crash never fires an archive transition)."""
    import fleet_state as fs
    live, arch = fs.live_all(), fs.archive_all()
    print(f"LIVE FLEET ({len(live)}):  {'label':<24}{'role':<16}{'kind':<11}{'status':<8}{'lifecycle':<11}surface")
    for label, v in sorted(live.items()):
        surf = v.get("surface", "")
        life = fs.lifecycle(surf) or "-"
        if life in ("", "-", "ended"):
            # no live session on the surface: PENDING = lazily-registered, not bound yet (codex binds
            # on its 1st turn -> drive it); STALE = had a session but the tab/process is gone.
            status = "pending" if not v.get("session") else "STALE"
        else:
            status = v.get("status", "live")
        muted = "  MUTED" if v.get("muted") else ""
        print(f"  {label:<24}{v.get('role','-'):<16}{v.get('kind','-'):<11}{status:<8}{life:<11}{surf[:8]}{muted}")
    if arch:
        print(f"\nARCHIVED ({len(arch)}, revivable):")
        for label, v in sorted(arch.items()):
            print(f"  {label:<24}{v.get('role','-'):<16}{v.get('kind','-'):<11}last_session={(v.get('last_session') or '')[:14]}")
    print("\n(STALE = surface gone, `fleet rm`/`revive`.  pending = launched, awaiting first turn to bind.)")
    return 0


def cmd_rm(argv):
    """Drop a label from live/archive. --kill also stops the process + closes its tab (throwaway)."""
    import fleet_state as fs, signal
    kill = "--kill" in argv
    args = [a for a in argv if a != "--kill"]
    if not args:
        sys.exit("usage: fleet rm <label> [--kill]")
    label = args[0]
    e = fs.live_get(label) or fs.archive_get(label)
    if not e:
        sys.exit(f"fleet rm: no such label '{label}'")
    if kill and e.get("surface"):
        pid = _pid_for_surface(e["surface"])
        if pid:
            try:
                os.kill(pid, signal.SIGINT); time.sleep(0.4); os.kill(pid, signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                pass
        cmuxq("close-surface", "--surface", e["surface"])
    fs.live_del(label); fs.archive_del(label)
    fs.log_event("removed", label=label, role=e.get("role"), killed=kill)
    print(f"[fleet] removed {label}{' (killed + closed)' if kill else ''}")
    return 0


def cmd_archive(argv):
    """Park a live agent: stop its process (SIGINT x2 = clean TUI exit), close the tab, move it to the
    archive shelf with enough to `claude --resume` it later."""
    import fleet_state as fs, signal
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
    pid = _pid_for_surface(surf)
    if pid:
        try:
            os.kill(pid, signal.SIGINT); time.sleep(0.5); os.kill(pid, signal.SIGINT)
        except (ProcessLookupError, PermissionError):
            pass
    cmuxq("close-surface", "--surface", surf)
    arch = {k: e[k] for k in ("role", "kind", "tool", "cwd", "parent", "place",
                              "plugins", "flags", "settings", "group") if k in e}
    arch["last_session"] = e.get("session")
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
    fs.archive_put(label, arch)
    fs.live_del(label)
    fs.log_event("archived", label=label, role=e.get("role"), session=e.get("session"))
    print(f"[fleet] archived {label} (session {e.get('session')}); revive with: fleet revive {label}")
    return 0


def cmd_revive(argv):
    """Bring a parked agent back into a fresh surface, resuming its last session. Binding-first, just
    like recycle: if archive captured cmux's launch binding, REPLAY it (--resume swapped to the parked
    session, caller `-- <flags>` / --add-plugin re-layered on top). Falls back to the registry-spec
    compose for entries archived before binding-capture existed (or with no binding)."""
    import fleet_state as fs
    caller = []
    if "--" in argv:
        i = argv.index("--"); argv, caller = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="fleet revive")
    ap.add_argument("label")
    ap.add_argument("--parent", default=os.environ.get("CMUX_SURFACE_ID", ""))
    ap.add_argument("--place")
    ap.add_argument("--add-plugin", action="append", default=[], metavar="NAME",
                    help="union a marketplace plugin into this identity (repeatable)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    e = fs.archive_get(a.label)
    if not e:
        sys.exit(f"fleet revive: no archived label '{a.label}'")
    tool = e.get("tool", "claude")
    sess = (e.get("last_session") or "").replace("claude-", "")   # --resume wants the bare uuid
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
        send_cmd = _compose_from_roster(e.get("role"), tool, a.label, caller, a.add_plugin, sess)
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
    print(f"[fleet] revive {a.label} (tool={tool}, resume {sess[:12] or '-'}, source={source})\n[fleet] launch: {send_cmd}")
    if a.dry_run:
        print("[fleet] dry-run"); return 0
    if not a.parent:
        sys.exit("[fleet] ABORT: no --parent and no $CMUX_SURFACE_ID")
    ws, surf = create_surface(spec, a.parent, "down")
    if not ws or not surf:
        sys.exit(1)
    cmuxq("send", "--workspace", ws, "--surface", surf, send_cmd + "\n")
    sid = poll_session(surf)
    if not sid:
        sys.exit("[fleet] timed out waiting for session binding")
    register(surf, spec, a.parent, sid, ws)
    fs.archive_del(a.label)
    fs.log_event("revived", label=a.label, role=spec["role"], surface=surf, session=sid)
    print(f"[fleet] DONE: revived {a.label} = surface {surf} (session {sid})")
    return 0


# ---------------------------------------------------------------- recycle (live->live, same surface)
# Restart an agent IN PLACE on its OWN surface via cmux's native `respawn-pane` (the tmux-compat
# kill+restart: cmux tears down the surface's current process and runs a fresh command in the SAME
# seat). Default = FRESH session (sheds context, auto-primes from the latest handover); --resume
# continues the session. Same surfaceId -> the registry entry (label, parent/child pointers) stays
# valid with ZERO churn; only `session` changes. Runs DETACHED so it can recycle the CALLER itself.
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
    """Block until the surface is at a quiet prompt (a non-running lifecycle AND empty draft), re-checked
    after a 2s settle to avoid racing a turn start. force = skip the draft guard (still requires the
    agent be not-'running'). Returns True when quiet, False on timeout.
    'unknown' counts as quiet: cmux's session-start sets agentLifecycle='unknown' on a fresh start OR a
    resume and explicitly does NOT claim 'running', so an agent that resumed-but-was-never-driven (no
    Stop hook yet -> never reaches 'idle') sits at 'unknown' awaiting input. Excluding it made a
    just-resumed agent un-recyclable (the quiet-gate would block until 180s ABORT, and --force only
    skips the DRAFT check, not the lifecycle check) -- so back-to-back resume recycles deadlocked."""
    import fleet_state as fs
    def quiet():
        lc = fs.lifecycle(surf)
        return lc in ("idle", "needsInput", "unknown") and (force or not _input_draft_nonempty(surf))
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


def _poll_session_back(surf, old_sid, mode, timeout=90):
    """Confirm the recycled agent re-bound a session to `surf`. respawn-pane fully REMOVES the old
    session entry from cmux's hook store (session-end), then the relaunch re-creates it:
      FRESH  -> a brand-new session id (sid != old_sid).
      RESUME -> the SAME session id. `claude --resume <id>` CONTINUES the session (same id, same
                transcript JSONL -- no fork; verified live), re-created with a fresh pid and
                agentLifecycle '' -> 'unknown'. So we CANNOT wait for a different sid (it never
                comes); we wait for the surface to carry a live (non-empty) lifecycle again, which
                only happens once resume's session-start fires. activeSessionsBySurface stays null
                until the first turn, so we rely on poll_session's sessions[] fallback + lifecycle.
    Returns the bound sid, or '' on timeout."""
    import fleet_state as fs
    end = time.time() + timeout
    while time.time() < end:
        sid = poll_session(surf, timeout=1)
        if sid and (sid != old_sid if mode == "fresh"
                    else fs.lifecycle(surf) not in ("", "-", "ended")):
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


def _replay_binding_argv(argv, tool, role, label, cwd, caller_tokens, add_plugins, resume_session):
    """Recompose a launch command from a captured binding's argv — the SHARED core of recycle (reads a
    LIVE surface binding) and revive (reads the binding captured at archive time). Strips the binding's
    own --resume (callers control it), unions add-plugins as --plugin-dir, layers caller flag overrides,
    optionally re-adds `--resume <resume_session>`, and re-injects AGENT_ROLE/AGENT_LABEL (bindings
    capture null env, so the orchestration vars must be put back). Other env (tool-floor env) is NOT
    recoverable from a binding — accepted, same as it's always been for recycle."""
    abs_cwd = cwd if os.path.isabs(cwd) else os.path.join(ROOT, cwd)
    base = _drop_keys(argv, {"--resume"})
    have = {base[i + 1] for i in range(len(base) - 1) if base[i] == "--plugin-dir"}
    for name in (add_plugins or []):
        pd = _plugin_dir(name)
        if not pd or pd in have:
            if not pd:
                print(f"[fleet] warn: plugin '{name}' not resolvable (marketplace unset or not found); skipping")
            continue
        base += ["--plugin-dir", pd]
    base = _layer_tokens([base, list(caller_tokens or [])])      # flag overrides
    base = _prepend_resume(base, tool, resume_session)           # claude --resume flag | codex resume subcmd
    return render_send_cmd(tool, base, {"AGENT_ROLE": role, "AGENT_LABEL": label}, abs_cwd)


def _compose_from_registry(label, entry, caller_tokens, add_plugins, resume_session):
    """Fallback compose from our registry spec (used only when cmux has no binding for the surface)."""
    tool = entry.get("tool", "claude")
    cwd = entry.get("cwd", "")
    abs_cwd = cwd if os.path.isabs(cwd) else os.path.join(ROOT, cwd)
    spec = {"tool": tool, "role": entry.get("role"), "label": label, "kind": entry.get("kind", "child"),
            "place": entry.get("place", "tab"), "group": entry.get("group", ""), "cwd": cwd,
            "abs_cwd": abs_cwd, "plugins": _dedup(list(entry.get("plugins", [])) + list(add_plugins or [])),
            "flags": _layer_tokens([list(entry.get("flags", [])), list(caller_tokens or [])]),
            "env": {}, "settings": entry.get("settings", "")}
    bin_name, args, env = adapter_compile(tool, spec, [])
    args = _prepend_resume(args, tool, resume_session)           # claude --resume flag | codex resume subcmd
    return render_send_cmd(bin_name, args, env, abs_cwd)


def _compose_from_roster(role, tool, label, caller_tokens, add_plugins, resume_session):
    """TOML-AUTHORITATIVE compose for a ROSTER role: re-resolve the CURRENT toml (floor + role config,
    incl. setting_sources / enable_plugins), compile it exactly as `fleet launch` does, then prepend the
    resume per tool. This is the source-of-truth path -- a recycle/revive of a rostered agent PICKS UP
    floor/role changes made since it launched (a frozen binding or a sparse registry can't, and the
    registry never even stored the newer keys). Identity (label/surface/parent/session) stays in the
    registry; only the LOADOUT is re-resolved. One-off caller `--` flags apply this invocation only
    (to persist a change, edit the toml)."""
    cfg = load_config()
    spec = resolve(cfg, role, tool, None)
    spec["label"] = label                                        # registry label (resolve defaults to role)
    if add_plugins:
        spec["plugins"] = _dedup(spec["plugins"] + list(add_plugins))
    abs_cwd = spec["cwd"] if os.path.isabs(spec["cwd"]) else os.path.join(ROOT, spec["cwd"])
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


def _compose_recycle_cmd(label, entry, caller_tokens, add_plugins, mode):
    """Recompose the recycle launch. ROSTER agents (role in the toml) are TOML-AUTHORITATIVE: re-resolve
    the current toml so a recycle picks up floor/role changes since launch. AD-HOC / off-roster agents
    have no toml to resolve -> reproduce from cmux's ground-truth binding (registry spec as last resort).
    Identity + session come from the registry; FRESH drops the resume, RESUME re-adds it per tool.
    One-off caller `--` flags apply this invocation only. Returns (send_cmd, checkpoint)."""
    tool = entry.get("tool", "claude")
    role = entry.get("role")
    b = _resume_binding(entry.get("surface", ""))
    checkpoint = b.get("checkpoint_id", "")
    # the session to resume: cmux's checkpoint if it has one, else the registry's recorded session
    resume_session = (checkpoint or (entry.get("session") or "").replace("claude-", "")) if mode == "resume" else None
    if _is_roster(role):                                          # ROSTER -> re-resolve the toml (truth)
        return _compose_from_roster(role, tool, label, caller_tokens, add_plugins, resume_session), checkpoint
    argv = _binding_argv(b.get("command", ""))                    # AD-HOC / off-roster -> reproduce
    if not argv:                                                  # no cmux binding -> registry fallback
        return _compose_from_registry(label, entry, caller_tokens, add_plugins, resume_session), checkpoint
    cwd = b.get("cwd") or entry.get("cwd", "")
    send_cmd = _replay_binding_argv(argv, tool, role, label, cwd, caller_tokens, add_plugins, resume_session)
    return send_cmd, checkpoint


def cmd_recycle(argv):
    """Restart THIS (or a named) agent in place on the same surface, same identity. See block comment."""
    import fleet_state as fs
    caller = []
    if "--" in argv:
        i = argv.index("--"); argv, caller = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="fleet recycle", add_help=True)
    ap.add_argument("label", nargs="?", help="registry label (default: self, via $CMUX_SURFACE_ID)")
    ap.add_argument("--resume", action="store_true", help="continue the session (default: fresh)")
    ap.add_argument("--force", action="store_true", help="skip the empty-draft guard (intentional go-live)")
    ap.add_argument("--add-plugin", action="append", default=[], metavar="NAME",
                    help="union a marketplace plugin into this identity (repeatable; persisted)")
    ap.add_argument("--prime", help="override the post-fresh-boot priming prompt")
    ap.add_argument("--no-prime", action="store_true", help="don't send any priming prompt")
    ap.add_argument("--dry-run", action="store_true", help="resolve + print, do NOT recycle")
    a = ap.parse_args(argv)

    label = a.label or fs.label_for_surface(os.environ.get("CMUX_SURFACE_ID", ""))
    if not label:
        sys.exit("[fleet] recycle: no label and can't resolve self from $CMUX_SURFACE_ID")
    entry = fs.live_get(label)
    if not entry:
        sys.exit(f"[fleet] recycle: no LIVE label '{label}' (recycle is live->live; use `revive` for parked)")
    surf = entry.get("surface", "")
    if not surf:
        sys.exit(f"[fleet] recycle: label '{label}' has no surface on its registry entry")
    tool = entry.get("tool", "claude")
    mode = "resume" if a.resume else "fresh"
    old_sid = (entry.get("session") or "").replace("claude-", "")
    send_cmd, checkpoint = _compose_recycle_cmd(label, entry, caller, a.add_plugin, mode)

    # fresh boots clean -> prime from the handover; resume carries context -> no prime unless asked
    prime = None
    if not a.no_prime:
        if a.prime:
            prime = a.prime
        elif mode == "fresh":
            abs_cwd = entry.get("cwd", "")
            abs_cwd = abs_cwd if os.path.isabs(abs_cwd) else os.path.join(ROOT, abs_cwd)
            ho = _latest_handover(abs_cwd)
            prime = (f"You were just recycled into a FRESH session (same identity: label '{label}', "
                     f"role '{entry.get('role')}', same surface). Re-orient from your latest handover"
                     + (f" at {ho}" if ho else " under ./handover/")
                     + ", then continue where it left off.")

    print(f"[fleet] recycle {label} (mode={mode}, tool={tool}, surface={surf})")
    print(f"[fleet] launch: {send_cmd}")
    if a.add_plugin or caller:
        print("[fleet] overrides applied (persist for free: cmux re-captures this as the new binding)")
    print(f"[fleet] prime: {prime if prime else '(none)'}")
    if a.dry_run:
        print("[fleet] dry-run (omit --dry-run to recycle)")
        return 0

    # hand to a DETACHED worker (own session) so it outlives this process and can respawn our own surface
    payload = {"label": label, "surface": surf, "send_cmd": send_cmd, "mode": mode, "tool": tool,
               "force": a.force, "prime": prime, "old_session": old_sid}
    os.makedirs(STATE, exist_ok=True)
    pf = os.path.join(STATE, f".recycle-{label}.json")
    with open(pf, "w") as fh:
        json.dump(payload, fh)
    log = os.path.join(STATE, "recycle.log")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "_recycle-exec", pf],
                     stdout=open(log, "a"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    gate = "idle" if a.force else "idle + empty draft"
    print(f"[fleet] recycle SCHEDULED (detached) for {label} on {surf}; mode={mode}.")
    print(f"[fleet]   waits for the surface to go quiet ({gate}), then respawns in place. log: {log}")
    return 0


def cmd_recycle_exec(argv):
    """DETACHED worker (internal verb): quiet-gate -> respawn-pane -> confirm new session -> update
    registry -> auto-prime. Never half-kills: aborts before respawn if the surface won't go quiet."""
    import fleet_state as fs
    p = json.load(open(argv[0]))
    surf, send_cmd, label = p["surface"], p["send_cmd"], p["label"]
    mode, force, prime, old_sid = p["mode"], p["force"], p.get("prime"), p.get("old_session") or ""

    def log(m):
        print(f"[recycle {time.strftime('%H:%M:%S')}] {label}: {m}", flush=True)

    log(f"start mode={mode} surface={surf} force={force}")
    if not _quiet_gate(surf, 180, force):
        log("ABORT: surface never went quiet within 180s; NOT respawning (no half-kill). Re-run when idle or pass --force.")
        return 1
    # respawn-pane natively tears down the old agent + restarts the pane in the SAME seat. We restart
    # it as a fresh INTERACTIVE login shell (not the agent directly): cmux exposes `claude` as a zsh
    # FUNCTION via its shell integration, so the agent must launch from a shell that sourced ~/.zshrc
    # -- a bare `/bin/sh -c claude` fails with 'claude not found'. Then we `send` the launch into it.
    log("quiet; respawn-pane -> fresh interactive shell (cmux kills the old agent in place)")
    out = cmuxq("respawn-pane", "--surface", surf, "--command", "exec /bin/zsh -il")
    log(f"respawn-pane -> {out.strip()}")
    time.sleep(2)                                        # let the login shell source its integration
    log("launching agent into the fresh shell")
    cmuxq("send", "--surface", surf, send_cmd)
    cmuxq("send-key", "--surface", surf, "enter")

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
        fs.live_put(label, e)
        fs.log_event("recycled", label=label, role=e.get("role"), surface=surf,
                     session=e.get("session") or "", mode=mode)
        log(f"respawned ({mode}); session re-binds on first turn ({p.get('tool')} registers lazily)")
        sid = old_sid if mode == "resume" else ""        # for the prime gate (prime IS the first turn)
        if prime:
            time.sleep(8)                                # codex boots slower than claude; let the TUI come up
    else:
        sid = _poll_session_back(surf, old_sid, mode, 90)
        if not sid:
            log(f"WARN: no {'resumed' if mode == 'resume' else 'fresh'} session bound within 90s; check the surface manually")
        else:
            log(f"{'resumed' if mode == 'resume' else 'fresh'} session {sid} bound")
            e = fs.live_get(label) or {}
            e["surface"] = surf
            e["session"] = f"claude-{sid}" if e.get("tool", "claude") == "claude" else sid
            fs.live_put(label, e)
            fs.log_event("recycled", label=label, role=e.get("role"), surface=surf, session=sid, mode=mode)
        if prime and sid:
            time.sleep(3)                                # let the fresh TUI settle before sending input

    if prime and (sid or lazy):
        cmuxq("send", "--surface", surf, prime)
        cmuxq("send-key", "--surface", surf, "enter")
        log("primed")
    try:
        os.remove(argv[0])
    except OSError:
        pass
    log("DONE")
    return 0


def cmd_mute(argv, mute=True):
    """Mute/unmute a child's completion delivery. When muted, the router does NOT push the child's
    turn-completions to the parent's inbox (no inbox row, no `cmux notify`, no idle-wake); the parent
    reads that child ON DEMAND (`fleet ls` shows it MUTED with its session → `child-digest`). Use when
    Berg drives a child directly (he is in the loop, so the conductor should not be spammed). The
    inverse of the notify-on-completion default. Mute is per-child runtime state on `fleet.json`.

      fleet mute <label>     fleet unmute <label>
    """
    import fleet_state as fs
    verb = "mute" if mute else "unmute"
    if not argv:
        sys.exit(f"usage: fleet {verb} <label>")
    label = argv[0]
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
              f"(fleet ls → child-digest {(e.get('session') or '').replace('claude-','')[:12]})")
    else:
        print(f"[fleet] {label} unmuted — completions deliver to its parent again")
    return 0


def cmd_broadcast(argv):
    """Notify live agents of an out-of-band change that does NOT auto-reach running agents (a toml/floor
    edit, a plugin bump, an ops heads-up). Delivers over the SAME input-safe path as peer-msg: a
    kind=peer inbox row per target (the awareness hook surfaces it into context, never the input box)
    plus an idle-wake. Informational by default (no reply expected). It NEVER restarts anything — each
    recipient decides what to do (e.g. `fleet recycle` to pick up the new toml). Self is always excluded.

      fleet broadcast "<msg>" [--target all|all-conductors|all-children|my-children]
                              [--no-wake] [--expect-reply] [--dry-run]

    Default target: all-conductors (config-change broadcasts are a conductor concern — they refresh
    their own fleets). `my-children` = live children whose parent label == mine.
    """
    import fleet_state as fs, secrets
    target = "all-conductors"
    no_wake = expect_reply = dry = False
    pos, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--target":
            target = argv[i + 1] if i + 1 < len(argv) else target; i += 2
        elif a == "--no-wake":
            no_wake = True; i += 1
        elif a == "--expect-reply":
            expect_reply = True; i += 1
        elif a in ("--dry-run", "-n"):
            dry = True; i += 1
        else:
            pos.append(a); i += 1
    if not pos:
        sys.exit('usage: fleet broadcast "<msg>" [--target all|all-conductors|all-children|my-children]'
                 ' [--no-wake] [--expect-reply] [--dry-run]')
    body = " ".join(pos)
    valid = {"all", "all-conductors", "all-children", "my-children"}
    if target not in valid:
        sys.exit(f"fleet broadcast: --target must be one of {sorted(valid)}")

    from_surface = os.environ.get("CMUX_SURFACE_ID", "")
    from_label = fs.label_for_surface(from_surface) or (from_surface[:8] if from_surface else "fleet")
    if target == "my-children" and not from_surface:
        sys.exit("fleet broadcast: --target my-children needs $CMUX_SURFACE_ID (run inside a conductor)")

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
        print(f"[broadcast] no live targets for --target {target}")
        return 0
    if dry:
        print(f"[broadcast] (dry-run) from {from_label}, target {target} -> {len(sel)} agent(s):")
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
        if not no_wake and fs.wake_if_idle(surf, "(broadcast-wake) a fleet broadcast is waiting in your context; handle it"):
            woke.append(label)
    fs.log_event("broadcast", **{"from": from_label, "target": target, "count": len(sel), "msg_id": bid})
    print(f"[broadcast] {from_label} -> {len(sel)} agent(s) (target {target}, msg {bid}, "
          f"reply: {'expected' if expect_reply else 'none'})")
    for label, v in sel:
        print(f"  {label:<24}{v.get('kind','-'):<11}{(v.get('surface') or '')[:8]}{'  (woke)' if label in woke else ''}")
    if not no_wake:
        print(f"  woke {len(woke)} idle agent(s); the rest see it on their next turn")
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("usage: fleet <launch|config|ls|archive|revive|recycle|rm|vitals|find|graph|serve|paint> ...\n"
              "  launch <role|--adhoc NAME> [--tool t] [--place p] [--parent s] [--dry-run] [-- <tool flags>]\n"
              "  config <role|--adhoc NAME|--cwd DIR> [--tool t]   effective config (base settings + fleet adds)\n"
              "  ls                                                live fleet x hook store; flags STALE + archived\n"
              "  archive <label>                                   park a live agent (revivable)\n"
              "  revive <label> [--place p] [--parent s] [--add-plugin N] [-- <flags>]\n"
              "                                                    bring a parked agent back (replays the captured binding, claude --resume)\n"
              "  recycle [label] [--resume] [--force] [--add-plugin N] [--prime T|--no-prime] [-- <flags>]\n"
              "                                                    restart in place, same surface/identity (default self+fresh)\n"
              "  broadcast \"<msg>\" [--target all|all-conductors|all-children|my-children] [--no-wake] [--expect-reply] [--dry-run]\n"
              "                                                    input-safe heads-up to live agents (e.g. after a toml/floor change); never restarts them\n"
              "  mute <label> | unmute <label>                     stop/resume pushing a child's completions to its parent (parent reads on demand)\n"
              "  rm <label>                                        drop a label from live/archive\n"
              "  vitals [--json] [--paint]                         cheapest-first triage table + each agent's context-remaining %\n"
              "  find <query> [--turns N] [--json]                 content-aware session lookup (label/role/cwd or transcript)\n"
              "  graph [--html] [--out FILE]                       fleet parentage tree (text, or self-contained HTML)\n"
              "  serve [--port N]                                  thin read-only localhost view (graph HTML + vitals.json); no daemon\n"
              "  paint                                             sync fleet state onto the cmux sidebar (status pills + ctx bars)")
        return 0
    sub, rest = sys.argv[1], sys.argv[2:]
    import fleet_features as ff
    fns = {"launch": cmd_launch, "config": cmd_config, "ls": cmd_ls,
           "archive": cmd_archive, "revive": cmd_revive, "recycle": cmd_recycle,
           "_recycle-exec": cmd_recycle_exec, "broadcast": cmd_broadcast,
           "mute": lambda a: cmd_mute(a, mute=True), "unmute": lambda a: cmd_mute(a, mute=False),
           "rm": cmd_rm,
           "vitals": ff.cmd_vitals, "find": ff.cmd_find, "graph": ff.cmd_graph,
           "serve": ff.cmd_serve, "paint": ff.cmd_paint}
    if sub in fns:
        return fns[sub](rest)
    sys.exit(f"fleet: unknown subcommand '{sub}'")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
