"""Layer 1 — ONE version, four surfaces (codex P1.5).

The app version is single-sourced at `cmux_fleet/__init__.py::__version__` (pyproject reads it via
`[tool.hatch.version]`, so the wheel/sdist can't drift). The two plugin manifests are independent JSON
that Claude Code reads directly — they can't import the package — so this test is the gate that keeps
them in lockstep with the app. If the plugin manifest the harness loads disagrees with the app version,
a "thin, version-behind plugin is safe as long as the fleet on PATH speaks the verb" claim breaks.

(There is deliberately NO hook-fallback pin to keep in sync: the uvx fallback was dropped in Phase 3, so
the plugin hooks carry no baked app spec — one fewer version surface.)
"""
import json
import os

from conftest import REPO

import cmux_fleet

PLUGIN_JSON = os.path.join(REPO, ".claude-plugin", "plugin.json")
MARKETPLACE_JSON = os.path.join(REPO, ".claude-plugin", "marketplace.json")
PYPROJECT = os.path.join(REPO, "pyproject.toml")


def _json(path):
    with open(path) as f:
        return json.load(f)


def test_all_version_surfaces_agree():
    app = cmux_fleet.__version__
    plugin = _json(PLUGIN_JSON)["version"]
    market = _json(MARKETPLACE_JSON)["plugins"][0]["version"]
    assert app == plugin == market, (
        f"version drift: __version__={app!r}, plugin.json={plugin!r}, marketplace.json={market!r} "
        f"— bump all together (the app version is authoritative)")


def test_pyproject_version_is_single_sourced():
    # pyproject must NOT carry a hardcoded version literal (that would be a second source that can drift);
    # it declares the version dynamic and points hatch at the package __version__.
    text = open(PYPROJECT).read()
    assert 'dynamic = ["version"]' in text
    assert 'path = "cmux_fleet/__init__.py"' in text
    assert "\nversion = " not in text, "pyproject must not hardcode `version =` (single-source via hatch)"
