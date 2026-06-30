# tests/test_profile.py — the multi-build isolation switch. `fleet profile` must emit a sourceable env
# block that pins every entrypoint at THIS build, and the launcher must inject those same paths into a
# child (so a conductor + its descendants stay on one build). Pure: no cmux, no launch.
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import fleet  # noqa: E402  (not popped by other test files)


def test_profile_emits_all_entrypoint_pins(capsys):
    fleet.cmd_profile(["myprof"])
    out = capsys.readouterr().out
    for key in ("CMUX_FLEET_ROOT", "CMUX_STATE_DIR", "CMUX_FLEET_TOML", "CMUX_FLEET_MARKETPLACE", "CMUX_BIN"):
        assert f"export {key}=" in out
    assert "export PATH=" in out
    assert os.path.join(fleet.PLUGIN_ROOT, "bin") in out      # THIS build's bin is pinned onto PATH
    assert "cmux-fleet-myprof" in out                          # name-derived state/config dirs


def test_profile_base_keeps_state_and_toml_together(capsys):
    fleet.cmd_profile(["p", "--base", "/tmp/cf-x"])
    out = capsys.readouterr().out
    assert "/tmp/cf-x/state" in out
    assert "/tmp/cf-x/fleet.toml" in out


def test_profile_marketplace_resolves_this_build(capsys):
    # CMUX_FLEET_MARKETPLACE must be the build's PARENT so plugins=["<build-dirname>"] -> this build.
    fleet.cmd_profile(["p"])
    out = capsys.readouterr().out
    assert f"export CMUX_FLEET_MARKETPLACE={os.path.dirname(fleet.PLUGIN_ROOT)}" in out


def test_profile_env_injection_is_absolute_and_complete():
    e = fleet._profile_env()
    assert {"CMUX_STATE_DIR", "CMUX_FLEET_TOML", "CMUX_FLEET_ROOT", "CMUX_BIN"} <= set(e)
    for k in ("CMUX_STATE_DIR", "CMUX_FLEET_TOML", "CMUX_FLEET_ROOT"):
        assert os.path.isabs(e[k])                             # hermetic: never a relative/ambiguous path
