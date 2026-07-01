"""Layer 1 — STATIC validators.

The plugin's JSON surface (plugin.json, marketplace.json, hooks.json) and skill files must be
well-formed and schema-correct, since Claude Code refuses to load a malformed plugin. These tests
need no state; they read the repo as shipped. The rules mirror P1-plugin-standards.md §2/§5.
"""
import glob
import json
import os
import re

import pytest
from conftest import REPO, SCRIPTS

PLUGIN_JSON = os.path.join(REPO, ".claude-plugin", "plugin.json")
MARKETPLACE_JSON = os.path.join(REPO, ".claude-plugin", "marketplace.json")
HOOKS_JSON = os.path.join(REPO, "hooks", "hooks.json")
SKILLS_DIR = os.path.join(REPO, "skills")

NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([-+].+)?$")


def _load(path):
    with open(path) as f:
        return json.load(f)


# --- plugin.json ---------------------------------------------------------------------------------
def test_plugin_json_parses():
    _load(PLUGIN_JSON)


def test_plugin_name_is_kebab():
    assert NAME_RE.match(_load(PLUGIN_JSON)["name"])


def test_plugin_version_is_semver():
    assert SEMVER_RE.match(_load(PLUGIN_JSON)["version"])


def test_plugin_description_present_and_bounded():
    desc = _load(PLUGIN_JSON)["description"]
    assert desc and len(desc) <= 300  # standards target <200; allow headroom, fail only on a wall


def test_plugin_has_license_and_repo():
    m = _load(PLUGIN_JSON)
    assert m.get("license")
    assert m.get("repository")


def test_plugin_hooks_path_resolves():
    hooks_field = _load(PLUGIN_JSON)["hooks"]
    assert hooks_field == "./hooks/hooks.json"
    assert os.path.exists(os.path.join(REPO, hooks_field))


# --- marketplace.json ----------------------------------------------------------------------------
def test_marketplace_parses_and_shape():
    m = _load(MARKETPLACE_JSON)
    assert NAME_RE.match(m["name"])
    assert m["owner"]["name"]
    assert isinstance(m["plugins"], list) and m["plugins"]


def test_marketplace_plugin_entry_required_fields():
    p = _load(MARKETPLACE_JSON)["plugins"][0]
    assert NAME_RE.match(p["name"])
    assert p["source"]
    assert isinstance(p["strict"], bool)
    # source "./" must resolve to the plugin (== repo root)
    assert os.path.exists(os.path.join(REPO, p["source"], ".claude-plugin", "plugin.json"))


def test_manifest_and_marketplace_versions_agree():
    assert _load(PLUGIN_JSON)["version"] == _load(MARKETPLACE_JSON)["plugins"][0]["version"]


# --- hooks.json ----------------------------------------------------------------------------------
def test_hooks_json_parses_and_wrapper():
    h = _load(HOOKS_JSON)
    assert "hooks" in h, "plugin hooks.json must use the {hooks:{...}} wrapper, not the bare settings form"


@pytest.mark.parametrize("event", ["UserPromptSubmit", "Stop"])
def test_hook_event_command_paths_resolve(event):
    events = _load(HOOKS_JSON)["hooks"]
    assert event in events
    for entry in events[event]:
        assert "matcher" in entry
        for hook in entry["hooks"]:
            assert hook["type"] == "command"
            cmd = hook["command"]
            assert "${CLAUDE_PLUGIN_ROOT}" in cmd, "hook commands must use ${CLAUDE_PLUGIN_ROOT}"
            # substitute the plugin root and confirm the referenced script exists
            rel = cmd.split("${CLAUDE_PLUGIN_ROOT}/", 1)[1].split()[0]
            assert os.path.exists(os.path.join(REPO, rel)), f"missing hook script: {rel}"


# --- skills --------------------------------------------------------------------------------------
def _skill_dirs():
    return [
        os.path.join(SKILLS_DIR, d)
        for d in os.listdir(SKILLS_DIR)
        if os.path.isdir(os.path.join(SKILLS_DIR, d))
    ]


def test_skills_exist():
    assert _skill_dirs()


@pytest.mark.parametrize("skill_dir", _skill_dirs())
def test_each_skill_has_valid_frontmatter(skill_dir):
    md = os.path.join(skill_dir, "SKILL.md")
    assert os.path.exists(md), f"skill {skill_dir} missing SKILL.md"
    text = open(md).read()
    assert text.startswith("---"), "SKILL.md must open with YAML frontmatter"
    fm = text.split("---", 2)[1]
    assert re.search(r"^name:\s*\S+", fm, re.M), "frontmatter needs a name"
    assert re.search(r"^description:\s*\S+", fm, re.M), "frontmatter needs a description"


# --- no stale helper-script / router-script recipes (Phase 2 / codex P2.1) -----------------------
# The four agent helpers folded from standalone plugin scripts into `fleet <verb>` subcommands, and the
# router moved to the package (`python -m cmux_fleet.router`). No operator-followed surface — docs,
# skills, profiles, README, tests docs, or the plugin hooks — may still tell an agent or human to run a
# now-deleted `scripts/<helper>.py` (bare, `python3 scripts/...`, or `${CLAUDE_PLUGIN_ROOT}/scripts/...`)
# or `scripts/router.py`. A line explicitly marked historical/deprecated is exempt (e.g. a CHANGELOG-style
# ledger line documenting a past release). This is the release gate that keeps the package correct-in-code
# while conductors keep emitting broken commands.
_HELPER_PY_RE = re.compile(r"(child-digest|drive-child|inbox-ack|peer-msg)\.py")
_ROUTER_SCRIPT_RE = re.compile(r"scripts/router\.py")
_REF_EXEMPT = ("historical", "deprecated", "fleet-refs-ok")


def _operator_surfaces():
    pats = ["docs/**/*.md", "skills/**/*.md", "profiles/*.toml", "profiles/*.example",
            "README.md", "tests/README.md", "scripts/hooks/*.py", "hooks/*.json"]
    files = []
    for p in pats:
        files += glob.glob(os.path.join(REPO, p), recursive=True)
    return sorted(set(files))


def test_no_stale_helper_or_router_script_refs():
    offenders = []
    for f in _operator_surfaces():
        with open(f, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if any(m in line.lower() for m in _REF_EXEMPT):
                    continue
                if _HELPER_PY_RE.search(line) or _ROUTER_SCRIPT_RE.search(line):
                    offenders.append(f"{os.path.relpath(f, REPO)}:{i}: {line.strip()}")
    assert not offenders, (
        "stale helper/router SCRIPT refs — fold into `fleet <verb>` / `python -m cmux_fleet.router` "
        "(or mark the line historical/deprecated):\n  " + "\n  ".join(offenders))
