#!/usr/bin/env python3
# config.py — the ONE path/setting resolver for cmux-fleet. Every other script imports its constants
# from here; nothing else hardcodes a path. Resolution precedence, per key:
#
#     env var  >  [fleet] block in the fleet toml  >  built-in default (XDG / which / skip)
#
# This is the whole decoupling: with no env, no config file, and cmux on $PATH, the defaults are
# stranger-safe (state under XDG, no vault assumption, marketplace/floor disabled). Tapestry-specific
# behavior is layered back IN by pointing the env vars / [fleet] block at the vault — never the reverse.
import os, shutil, sys

try:
    import tomllib
except ModuleNotFoundError:                      # py<3.11 — engine still works, just can't read a toml
    tomllib = None


def _warn(msg):
    # stderr only: never touches a hook's stdout JSON or its exit code. Side-effect-light.
    sys.stderr.write(f"[cmux-fleet config] {msg}\n")


def _xdg(env, default):
    v = os.environ.get(env, "").strip()
    return v if v else os.path.expanduser(default)


# 1. Locate the toml itself (env > XDG config default). Read BEFORE anything else: its dir anchors
#    relative [fleet] path values, and its [fleet] block feeds every other key.
FLEET_TOML = os.path.expanduser(
    os.environ.get("CMUX_FLEET_TOML", "").strip()
    or os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "cmux-fleet", "fleet.toml"))
TOML_DIR = os.path.dirname(os.path.abspath(FLEET_TOML))


def _load_fleet_block():
    # Distinguish ABSENT (normal for a stranger / hooks / ad-hoc) from MALFORMED (a config typo). A
    # malformed file is WARNED about, not silently swallowed, so the CLI and the router/hooks don't
    # split state (XDG default vs the configured dir) on the same broken file. Still no exit: hooks
    # must fail open. fleet.py's load_config() raises loudly for a malformed ROSTER separately.
    if not tomllib or not os.path.exists(FLEET_TOML):
        return {}
    try:
        with open(FLEET_TOML, "rb") as f:
            return tomllib.load(f).get("fleet") or {}
    except (OSError, ValueError) as e:
        _warn(f"warning: {FLEET_TOML} is unreadable/malformed ({e}); using built-in defaults")
        return {}


_fleet = _load_fleet_block()


def _resolve(env_name, key, default):
    """Plain resolve: env > [fleet].<key> > default. expanduser only (no anchoring). For non-path
    values and for cmux_bin (which may legitimately be a bare PATH command name, not a filesystem path)."""
    v = os.environ.get(env_name)
    if not (v and v.strip()):
        v = _fleet.get(key)
    if v is not None and str(v).strip():
        return os.path.expanduser(str(v))
    return default


def _resolve_path(env_name, key, default):
    """Path-typed resolve. A RELATIVE value from the [fleet] toml anchors to the toml's dir (so
    `root = "."` means the project dir holding fleet.toml, exactly as fleet.toml.example documents);
    a relative value from the ENV is absolutized against the process cwd WITH A WARNING (the caller's
    cwd is not a stable anchor). An absolute or empty value passes through (empty -> default)."""
    raw, src = os.environ.get(env_name), "env"
    if not (raw and raw.strip()):
        raw, src = _fleet.get(key), "toml"
    if raw is None or not str(raw).strip():
        return default
    p = os.path.expanduser(str(raw))
    if os.path.isabs(p):
        return p
    if src == "toml":
        return os.path.normpath(os.path.join(TOML_DIR, p))
    ap = os.path.abspath(p)
    _warn(f"warning: ${env_name} is a relative path ('{raw}'); resolving against cwd -> {ap}")
    return ap


def _default_cmux():
    # Prefer cmux on PATH (Linux / non-standard installs) over the macOS app bundle.
    return shutil.which("cmux") or "/Applications/cmux.app/Contents/Resources/bin/cmux"


# 2. The resolved constants. Import these.
# ROOT is the workspace root that relative role cwds compose against. Default = $HOME — a normal
# ~/.config/cmux-fleet/fleet.toml must NOT silently make role cwds resolve under ~/.config. A
# project-local layout opts in explicitly by setting [fleet].root (e.g. "." -> the toml's dir, or an
# absolute repo path) or $CMUX_FLEET_ROOT.
ROOT        = _resolve_path("CMUX_FLEET_ROOT",        "root",           os.path.expanduser("~"))
STATE       = _resolve_path("CMUX_STATE_DIR",         "state_dir",      os.path.join(_xdg("XDG_STATE_HOME", "~/.local/state"), "cmux-fleet"))
CMUX        = _resolve("CMUX_BIN",                    "cmux_bin",       _default_cmux())                # bare command name OK -> NOT anchored
MARKETPLACE = _resolve_path("CMUX_FLEET_MARKETPLACE", "marketplace",    "")          # "" -> internal --plugin-dir resolution disabled
FLOOR       = _resolve_path("CMUX_FLEET_FLOOR",       "floor_claudemd", "")          # "" -> no ad-hoc CLAUDE.md symlink
HOOKSTORE   = _resolve_path("CMUX_HOOKSTORE_DIR",     "hookstore_dir",  os.path.expanduser("~/.cmuxterm"))   # cmux-owned, $HOME-relative
ADHOC_SUBDIR = _resolve("CMUX_FLEET_ADHOC_SUBDIR",   "adhoc_subdir",   "_meta/agents/ad-hoc")          # relative to ROOT (intentionally not anchored)
# `fleet vitals` context-remaining % denominator. 0 -> fleet_features guesses from the model string
# (a fleet usually runs one window, so one knob is right; the model string can't tell 200k from 1M).
CONTEXT_WINDOW = int(_resolve("CMUX_FLEET_CONTEXT_WINDOW", "context_window", "0") or "0")
