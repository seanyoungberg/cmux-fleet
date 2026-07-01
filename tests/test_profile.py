# tests/test_profile.py — the multi-build isolation switch. `fleet profile` must emit a sourceable env
# block that pins every entrypoint at THIS build, and the launcher must inject those same paths into a
# child (so a conductor + its descendants stay on one build). Pure: no cmux, no launch.
import os

from cmux_fleet import cli as fleet


def test_profile_emits_all_entrypoint_pins(capsys):
    fleet.cmd_profile(["myprof"])
    out = capsys.readouterr().out
    for key in ("CMUX_FLEET_ROOT", "CMUX_STATE_DIR", "CMUX_FLEET_TOML", "CMUX_FLEET_MARKETPLACE", "CMUX_BIN"):
        assert f"export {key}=" in out
    assert "export PATH=" in out
    # PATH pins THIS build's fleet dir — resolved via _fleet_bin_dir(), NOT blindly PLUGIN_ROOT/bin
    # (in a wheel install PLUGIN_ROOT is site-packages and has no bin/; the codex P1.1 fix).
    assert fleet._fleet_bin_dir() in out
    assert "cmux-fleet-myprof" in out                          # name-derived state/config dirs


def test_profile_base_keeps_state_and_toml_together(capsys):
    fleet.cmd_profile(["p", "--base", "/tmp/cf-x"])
    out = capsys.readouterr().out
    assert "/tmp/cf-x/state" in out
    assert "/tmp/cf-x/fleet.toml" in out


def test_profile_marketplace_resolves_this_build(capsys):
    # CMUX_FLEET_MARKETPLACE must be the pin from _marketplace_pin(): explicit config, or a REAL
    # checkout's parent (so plugins=["<build-dirname>"] -> this build) — never a wheel's site-packages.
    fleet.cmd_profile(["p"])
    out = capsys.readouterr().out
    assert f"export CMUX_FLEET_MARKETPLACE={fleet._marketplace_pin()}" in out


def test_profile_env_injection_is_absolute_and_complete():
    e = fleet._profile_env()
    assert {"CMUX_STATE_DIR", "CMUX_FLEET_TOML", "CMUX_FLEET_ROOT", "CMUX_BIN"} <= set(e)
    for k in ("CMUX_STATE_DIR", "CMUX_FLEET_TOML", "CMUX_FLEET_ROOT"):
        assert os.path.isabs(e[k])                             # hermetic: never a relative/ambiguous path
