#!/usr/bin/env python3
# cmux_fleet/plugins.py — the reconcile ENGINE for the plugin INDEX (plugins.toml), Phase 2 of the
# plugin-management redesign (design §6). Pure functions with NO CLI/state/config import, so the whole
# derivation is unit-testable in isolation (tests/test_plugin_reconcile.py) by handing it fixture
# marketplaces + a fixture settings file. The CLI wiring (argparse, --dry-run/--prune/--json, and the
# ls/show/describe discovery verbs) lives in cli.cmd_plugins; discovery reads through the canonical
# config.load_plugin_index() so resolution and discovery never disagree on what the index says.
#
# THE OWNERSHIP SPLIT (the whole point of reconcile): the helper OWNS the fields it can DERIVE from
# sources (type/source/tools/origin, and description while un-curated); humans OWN the rest. So:
#   ADD     a derivable entry the index is missing.
#   UPDATE  a machine-owned field that drifted from its source (tools/type/source/origin, description
#           while not curated).
#   PRESERVE hand-authored data sources can't derive: a `curated = true` description, [plugin.<n>.<t>]
#           per-tool blocks, a hand `install`, and any hand-added entry not backed by a source.
#   DRIFT   when a CURATED description no longer matches what the source would derive, REPORT it
#           ("index says X, source says Y") instead of silently clobbering the human's edit.
#   PRUNE   (only under --prune) drop an entry no longer backed by any source.
# The write is a clean deterministic REGENERATE from the merged model (comments are NOT round-tripped —
# see cli.cmd_plugins / the header below); hand-authored FIELDS survive because they are read out of the
# existing file into the model before it is re-rendered. A second reconcile right after the first is a
# no-op: parse(render(model)) == model for our value types, so the file is byte-identical (idempotent).
import json
import os

try:
    import tomllib
except ModuleNotFoundError:                      # py<3.11 — engine degrades to "no existing index to read"
    tomllib = None

# Fields reconcile OWNS: pure functions of the sources, overwritten on every run. `origin` = source-kind
# (a local `path` marketplace entry vs a git `url` one, from marketplace.json). `description` is
# machine-owned too but only while un-curated (handled specially so a `curated=true` desc is preserved).
MACHINE_FIELDS = ("type", "source", "tools", "origin")
# Fixed emit order for a [plugin.<name>] block; any extra hand-authored scalar is appended after, sorted.
FIELD_ORDER = ("type", "source", "tools", "origin", "description", "install", "curated")

HEADER = (
    "# plugins.toml — the plugin INDEX. RECONCILED by `fleet plugins reconcile` (design §6).\n"
    "# Regenerated on reconcile: machine-owned fields (type/source/tools/origin) are DERIVED from the\n"
    "# marketplaces + ~/.claude settings and WILL be overwritten. Hand-authored data is PRESERVED:\n"
    "#   • a curated description survives if you mark the entry `curated = true` (reconcile then keeps it\n"
    "#     and REPORTS drift when the source description moves, instead of clobbering your edit);\n"
    "#   • [plugin.<name>.<tool>] blocks, a hand `install`, and any hand-added entry are preserved\n"
    "#     (an entry no longer backed by a source is removed only under `--prune`).\n"
    "# [marketplace.*] blocks are hand-owned — reconcile scans them but never edits them. Freeform\n"
    "# comments are NOT round-tripped through a reconcile; structured fields above are."
)


# ---------------------------------------------------------------- derivation (scan sources -> entries)
def _manifest_tools(plugin_dir):
    """Derive a plugin's supported tools from WHICH manifests its dir carries — the headline derivation:
    a `.claude-plugin` manifest -> "claude", a `.codex-plugin` manifest -> "codex". A dir with neither is
    not a plugin (returns [] -> the caller skips it)."""
    tools = []
    if os.path.isdir(os.path.join(plugin_dir, ".claude-plugin")) \
            or os.path.exists(os.path.join(plugin_dir, ".claude-plugin", "plugin.json")):
        tools.append("claude")
    if os.path.isdir(os.path.join(plugin_dir, ".codex-plugin")) \
            or os.path.exists(os.path.join(plugin_dir, ".codex-plugin", "plugin.json")):
        tools.append("codex")
    return tools


def _marketplace_meta(mkt_path):
    """Map plugin name -> {"description", "origin"} from the marketplace.json that governs a marketplace
    `path` dir, or {} if there is none. Claude's marketplace.json is itself an index ({name, description,
    version, source}); `source` a bare string is a local `path` entry, a {source:"url",...} object is a
    git `url` entry (that's where `origin` comes from). The manifest sits at <root>/.claude-plugin/
    marketplace.json, where <root> is the path itself or its parent (a `path` usually points at
    <root>/plugins)."""
    for root in (mkt_path, os.path.dirname(mkt_path.rstrip(os.sep))):
        mj = os.path.join(root, ".claude-plugin", "marketplace.json")
        if not os.path.exists(mj):
            continue
        try:
            with open(mj, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return {}
        out = {}
        for entry in (doc.get("plugins") or []):
            name = entry.get("name")
            if not name:
                continue
            out[name] = {
                "description": entry.get("description") or "",
                "origin": "url" if isinstance(entry.get("source"), dict) else "path",
            }
        return out
    return {}


def _plugin_json_desc(plugin_dir):
    """Fallback description straight from the plugin's own manifest (plugin.json carries name/version/
    description), for a plugin dir the marketplace.json doesn't list."""
    for manifest in (".claude-plugin", ".codex-plugin"):
        pj = os.path.join(plugin_dir, manifest, "plugin.json")
        if not os.path.exists(pj):
            continue
        try:
            with open(pj, encoding="utf-8") as f:
                return json.load(f).get("description") or ""
        except (OSError, ValueError):
            return ""
    return ""


def derive_entries(marketplaces, settings_paths):
    """Scan sources -> {name: derived_entry}. Two source kinds (design §6):
      (a) every LOCAL [marketplace.<name>] (a resolved {"kind","path"} from config.load_plugin_index) —
          each subdir carrying a manifest becomes a `type=linked` entry; tools from manifest presence;
          description + origin from that marketplace's marketplace.json (else the plugin's own plugin.json).
      (b) each `enabledPlugins` {name@mkt: on/off} in a claude settings JSON — a `type=enabled` entry;
          a globally-DISABLED one records install="global-disabled" (the per-agent-flip candidate).
    A linked dir wins over an enabled ref of the same name (a local checkout is the stronger signal)."""
    derived = {}
    for mkt_name, mk in sorted(marketplaces.items()):
        path = mk.get("path")
        if not path or not os.path.isdir(path):          # kind=global or a missing dir contributes nothing here
            continue
        meta = _marketplace_meta(path)
        for sub in sorted(os.listdir(path)):
            pdir = os.path.join(path, sub)
            if not os.path.isdir(pdir):
                continue
            tools = _manifest_tools(pdir)
            if not tools:                                 # no manifest -> not a plugin
                continue
            m = meta.get(sub, {})
            derived[sub] = {
                "type": "linked",
                "source": mkt_name,
                "tools": tools,
                "origin": m.get("origin", "path"),
                "description": m.get("description") or _plugin_json_desc(pdir),
            }
    for sp in settings_paths:
        if not sp or not os.path.exists(sp):
            continue
        try:
            with open(sp, encoding="utf-8") as f:
                enabled = (json.load(f) or {}).get("enabledPlugins") or {}
        except (OSError, ValueError):
            continue
        for ref, on in enabled.items():
            name, _, mkt = str(ref).partition("@")
            if not name or name in derived:               # linked checkout wins over an enabled ref
                continue
            derived[name] = {
                "type": "enabled",
                "source": mkt,
                "tools": ["claude"],                      # enabledPlugins is a claude-only mechanism
                "origin": "",
                "description": "",                         # not derivable for a global plugin (hand-set allowed)
                "install": "" if on else "global-disabled",
            }
    return derived


# ---------------------------------------------------------------- merge (existing index <- derived)
class Diff:
    """The outcome of a reconcile. `changes` are file-mutating (add/update/prune); `notes` are
    informational (preserve/drift) and may legitimately recur every run — so idempotency is defined as
    `not has_changes` (the rendered file is byte-identical), NOT as "no notes"."""

    def __init__(self):
        self.changes = []                                 # (action, name, detail): add | update | prune
        self.notes = []                                   # (kind,   name, detail): preserve | drift

    @property
    def has_changes(self):
        return bool(self.changes)

    def counts(self):
        c = {"add": 0, "update": 0, "prune": 0, "preserve": 0, "drift": 0}
        for action, _, _ in self.changes:
            c[action] += 1
        for kind, _, _ in self.notes:
            c[kind] += 1
        return c


def _split(entry):
    """Split a raw index entry into (scalar fields, per-tool sub-tables). A [plugin.<n>.<tool>] block
    parses as a nested dict — those are the hand-authored blocks we preserve verbatim."""
    scalars = {k: v for k, v in entry.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in entry.items() if isinstance(v, dict)}
    return scalars, tables


def _norm(v):
    # None (absent field) and "" (empty) compare equal, so a derived origin="" for an enabled entry
    # doesn't read as a spurious change against an entry that simply omits origin.
    return "" if v is None else v


def _merge_entry(cur, der):
    """Merge a derived entry into an existing one under the ownership split. Returns
    (merged_entry, changes, drift) where changes = [(field, old, new)] machine updates and
    drift = [(field, index_value, source_value)] for a curated field the source has moved past."""
    scalars, tables = _split(cur)
    out = dict(scalars)                                   # start from the existing scalars -> hand fields survive
    changes, drift = [], []

    for f in MACHINE_FIELDS:                              # type/source/tools/origin: source is truth
        if f not in der:
            continue
        nv = der[f]
        if _norm(scalars.get(f)) != _norm(nv):
            changes.append((f, scalars.get(f), nv))
        out[f] = nv

    der_desc, cur_desc = der.get("description", ""), scalars.get("description", "")
    if scalars.get("curated"):                            # human owns this description
        out["description"] = cur_desc
        if der_desc and der_desc != cur_desc:
            drift.append(("description", cur_desc, der_desc))
    elif der_desc and der_desc != cur_desc:              # machine owns it -> update to the source
        changes.append(("description", cur_desc, der_desc))
        out["description"] = der_desc
    # (der_desc empty -> leave whatever the entry already had; never wipe a description to "")

    if der.get("type") == "enabled" and "install" in der:  # install is derived for enabled entries
        if _norm(scalars.get("install")) != _norm(der["install"]):
            changes.append(("install", scalars.get("install"), der["install"]))
        out["install"] = der["install"]

    merged = dict(out)
    merged.update(tables)                                 # re-attach preserved per-tool blocks
    return merged, changes, drift


def reconcile_plugins(existing_plugins, derived, *, prune):
    """Merge `derived` (from derive_entries) into `existing_plugins` (the raw doc['plugin'] table, an
    entry being a scalar dict possibly plus nested per-tool tables). Returns (new_plugins, Diff)."""
    diff = Diff()
    new = {}
    for name in sorted(set(existing_plugins) | set(derived)):
        cur = dict(existing_plugins.get(name) or {})
        der = derived.get(name)
        if der is None:                                   # in the index, not backed by any source
            if prune:
                diff.changes.append(("prune", name, "no backing source"))
            else:
                diff.notes.append(("preserve", name, "hand-added; not backed by a source"))
                new[name] = cur
            continue
        if not cur:                                       # brand-new derivable entry
            new[name] = {k: der[k] for k in FIELD_ORDER if k in der}
            diff.changes.append(("add", name,
                                 f"type={der['type']} tools={der.get('tools', [])} source={der.get('source', '')}"))
            continue
        merged, changes, drift = _merge_entry(cur, der)
        new[name] = merged
        for field, old, nw in changes:
            diff.changes.append(("update", name, f"{field}: {old!r} -> {nw!r}"))
        for field, idx_v, src_v in drift:
            diff.notes.append(("drift", name, f"{field}: index={idx_v!r} source={src_v!r}"))
    return new, diff


# ---------------------------------------------------------------- render (merged model -> toml text)
def _toml_str(s):
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{s}"'


def _toml_val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) if isinstance(x, bool) else _toml_str(x) for x in v) + "]"
    return _toml_str(v)


def _nonempty(v):
    return v is not None and v != "" and v != [] and v is not False


def render_index(marketplaces, plugins, header=HEADER):
    """Deterministically serialize the merged model back to plugins.toml text. Marketplaces re-emitted
    verbatim (hand-owned, sorted); plugins in FIELD_ORDER then any extra hand scalar (sorted); per-tool
    blocks last (sorted). Empty/false scalars are omitted so the file stays clean and round-trips."""
    lines = [header.rstrip("\n"), ""]
    for name in sorted(marketplaces):
        mk = marketplaces[name]
        lines.append(f"[marketplace.{name}]")
        for k in sorted(mk):
            if _nonempty(mk[k]) or isinstance(mk[k], bool):
                lines.append(f"{k} = {_toml_val(mk[k])}")
        lines.append("")
    for name in sorted(plugins):
        scalars, tables = _split(plugins[name])
        lines.append(f"[plugin.{name}]")
        emitted = set()
        for f in FIELD_ORDER:
            if f in scalars and _nonempty(scalars[f]):
                lines.append(f"{f} = {_toml_val(scalars[f])}")
                emitted.add(f)
        for k in sorted(scalars):
            if k not in emitted and _nonempty(scalars[k]):
                lines.append(f"{k} = {_toml_val(scalars[k])}")
        for tk in sorted(tables):
            lines.append(f"[plugin.{name}.{tk}]")
            for kk in sorted(tables[tk]):
                lines.append(f"{kk} = {_toml_val(tables[tk][kk])}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------- top-level pass (read -> merge -> render)
def run_reconcile(index_path, marketplaces, settings_paths, *, prune):
    """One reconcile pass. Reads the raw existing index at `index_path` (absent/malformed -> empty),
    derives from the sources, merges, and renders — but WRITES NOTHING (the caller decides, so --dry-run
    is just "don't write"). Returns (new_text, diff, existing_text). `marketplaces` is the resolved map
    from config.load_plugin_index()['marketplaces']; `settings_paths` are the claude settings JSONs."""
    existing_text, doc = "", {}
    if tomllib and os.path.exists(index_path):
        try:
            with open(index_path, encoding="utf-8") as f:
                existing_text = f.read()
            doc = tomllib.loads(existing_text)
        except (OSError, ValueError):
            doc = {}
    raw_marketplaces = doc.get("marketplace") or {}       # re-emitted verbatim (relative paths preserved)
    existing_plugins = doc.get("plugin") or {}
    derived = derive_entries(marketplaces, settings_paths)
    new_plugins, diff = reconcile_plugins(existing_plugins, derived, prune=prune)
    new_text = render_index(raw_marketplaces, new_plugins)
    return new_text, diff, existing_text
