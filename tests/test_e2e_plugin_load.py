"""Layer 3 — plugin load readiness.

Two checks:
  1. A structural "would-load" validator that always runs: the manifest exists, every hook command
     resolves to a real script, and every skill ships a SKILL.md. This is what makes `claude
     --plugin-dir <repo>` succeed, asserted without launching a model turn.
  2. A real `claude --plugin-dir <repo>` load, run ONLY when a headless-runnable `claude` is present
     (skipped otherwise so the suite stays green on any machine, e.g. a CI box or a cmux-shimmed CLI).
"""
import json
import os
import re
import shutil
import subprocess

import pytest
from conftest import REPO


def test_would_load_structurally():
    manifest = json.load(open(os.path.join(REPO, ".claude-plugin", "plugin.json")))
    assert manifest["name"]
    hooks = json.load(open(os.path.join(REPO, "hooks", "hooks.json")))["hooks"]
    for event_entries in hooks.values():
        for entry in event_entries:
            for hook in entry["hooks"]:
                rel = hook["command"].split("${CLAUDE_PLUGIN_ROOT}/", 1)[1].split()[0]
                assert os.path.exists(os.path.join(REPO, rel))
    skills_dir = os.path.join(REPO, "skills")
    for d in os.listdir(skills_dir):
        sd = os.path.join(skills_dir, d)
        if os.path.isdir(sd):
            assert os.path.exists(os.path.join(sd, "SKILL.md"))


def _claude_runnable():
    """A claude on PATH that answers --help cleanly (not a cmux shim that needs a live surface)."""
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        p = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=20)
        return exe if p.returncode == 0 else None
    except Exception:
        return None


def test_claude_plugin_dir_load():
    exe = _claude_runnable()
    if not exe:
        pytest.skip("no headless-runnable `claude` on PATH (cannot exercise real --plugin-dir load here)")
    p = subprocess.run([exe, "--plugin-dir", REPO, "--help"], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    # A clean load exits 0 and emits no plugin-load complaint. Scan for FAILURE signatures, not the bare
    # words "plugin"/"error": claude's own --help text contains both (`--plugin-dir`, "plugin sync", ...),
    # so a substring check false-positives on any machine where a real claude actually runs.
    combined = (p.stdout + p.stderr).lower()
    bad = re.search(r"(failed to load|could not load|error loading|unable to load|invalid|malformed)"
                    r"[^\n]*plugin|plugin[^\n]*(failed|invalid|not found|malformed|could not)", combined)
    assert not bad, f"plugin load complained:\n{p.stdout}\n{p.stderr}"
