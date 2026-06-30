#!/usr/bin/env python3
# config.py — the ONE path/setting resolver for cmux-fleet. Every other script imports its constants
# from here; nothing else hardcodes a path. Resolution precedence, per key:
#
#     env var  >  [fleet] block in the fleet toml  >  built-in default (XDG / which / skip)
#
# This is the whole decoupling: with no env, no config file, and cmux on $PATH, the defaults are
# stranger-safe (state under XDG, no vault assumption, marketplace/floor disabled). Tapestry-specific
# behavior is layered back IN by pointing the env vars / [fleet] block at the vault — never the reverse.
import os, shutil

try:
    import tomllib
except ModuleNotFoundError:                      # py<3.11 — engine still works, just can't read a toml
    tomllib = None


def _xdg(env, default):
    v = os.environ.get(env, "").strip()
    return v if v else os.path.expanduser(default)


# 1. Locate the toml itself (env > XDG config default). Read BEFORE anything else: its dir is the
#    default ROOT, and its [fleet] block feeds every other key.
FLEET_TOML = os.path.expanduser(
    os.environ.get("CMUX_FLEET_TOML", "").strip()
    or os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "cmux-fleet", "fleet.toml"))


def _load_fleet_block():
    if not tomllib:
        return {}
    try:
        with open(FLEET_TOML, "rb") as f:
            return tomllib.load(f).get("fleet") or {}
    except (OSError, ValueError):                 # absent or malformed -> defaults only
        return {}


_fleet = _load_fleet_block()


def _resolve(env_name, key, default):
    """env > [fleet].<key> > default. Strings get expanduser; an empty value falls through."""
    v = os.environ.get(env_name)
    if not (v and v.strip()):
        v = _fleet.get(key)
    if v is not None and str(v).strip():
        return os.path.expanduser(str(v))
    return default


def _default_cmux():
    # Prefer cmux on PATH (Linux / non-standard installs) over the macOS app bundle.
    return shutil.which("cmux") or "/Applications/cmux.app/Contents/Resources/bin/cmux"


# 2. The resolved constants. Import these.
# ROOT is the workspace root that relative role cwds compose against. Default = $HOME — a normal
# ~/.config/cmux-fleet/fleet.toml must NOT silently make role cwds resolve under ~/.config. A
# project-local layout opts in explicitly by setting [fleet].root (e.g. "." or an absolute repo path)
# or $CMUX_FLEET_ROOT.
ROOT        = _resolve("CMUX_FLEET_ROOT",        "root",           os.path.expanduser("~"))
STATE       = _resolve("CMUX_STATE_DIR",         "state_dir",      os.path.join(_xdg("XDG_STATE_HOME", "~/.local/state"), "cmux-fleet"))
CMUX        = _resolve("CMUX_BIN",               "cmux_bin",       _default_cmux())
MARKETPLACE = _resolve("CMUX_FLEET_MARKETPLACE", "marketplace",    "")          # "" -> internal --plugin-dir resolution disabled
FLOOR       = _resolve("CMUX_FLEET_FLOOR",       "floor_claudemd", "")          # "" -> no ad-hoc CLAUDE.md symlink
HOOKSTORE   = _resolve("CMUX_HOOKSTORE_DIR",     "hookstore_dir",  os.path.expanduser("~/.cmuxterm"))   # cmux-owned, $HOME-relative
ADHOC_SUBDIR = _resolve("CMUX_FLEET_ADHOC_SUBDIR", "adhoc_subdir", "agents/ad-hoc")                     # relative to ROOT
