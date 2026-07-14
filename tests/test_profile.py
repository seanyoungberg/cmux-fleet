# tests/test_profile.py — the multi-build isolation switch. `fleet profile` must emit a sourceable env
# block that pins every entrypoint at THIS build, and the launcher must inject those same paths into a
# child (so a conductor + its descendants stay on one build). Pure: no cmux, no launch.
import os

from cmux_fleet import cli as fleet


def test_profile_emits_all_entrypoint_pins(capsys):
    fleet.cmd_profile(["myprof"])
    out = capsys.readouterr().out
    for key in ("CMUX_FLEET_ROOT", "CMUX_STATE_DIR", "CMUX_FLEET_TOML", "CMUX_FLEET_PLUGIN_INDEX", "CMUX_BIN"):
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


def test_profile_pins_the_plugin_index_next_to_the_toml(capsys):
    # Marketplaces live in the index now, so the profile pins CMUX_FLEET_PLUGIN_INDEX (next to its toml) —
    # THAT is what keeps the profile's plugin loadout on its own declared [marketplace.*] blocks.
    fleet.cmd_profile(["p", "--base", "/tmp/cf-prof-x"])
    out = capsys.readouterr().out
    assert "export CMUX_FLEET_PLUGIN_INDEX=/tmp/cf-prof-x/plugins.toml" in out


def test_profile_env_injection_is_absolute_and_complete():
    e = fleet._profile_env()
    assert {"CMUX_STATE_DIR", "CMUX_FLEET_TOML", "CMUX_FLEET_ROOT", "CMUX_BIN", "CMUX_FLEET_PLUGIN_INDEX"} <= set(e)
    for k in ("CMUX_STATE_DIR", "CMUX_FLEET_TOML", "CMUX_FLEET_ROOT", "CMUX_FLEET_PLUGIN_INDEX"):
        assert os.path.isabs(e[k])                             # hermetic: never a relative/ambiguous path


# --- hook-state WRITE isolation: the launcher pins cmux's write-side var, but ONLY on an explicit pin ----
def test_profile_env_omits_hook_state_at_default(monkeypatch):
    # At the ~/.cmuxterm default (no pin) the launcher must NOT set cmux's write-side var — prod's launch
    # env stays byte-identical and cmux keeps its own default hook dir. Zero blast radius.
    monkeypatch.setattr(fleet, "HOOKSTORE_EXPLICIT", False)
    assert "CMUX_AGENT_HOOK_STATE_DIR" not in fleet._profile_env()


def test_profile_env_pins_hook_state_when_hookstore_explicit(monkeypatch):
    # With a private hookstore pinned, cmux's WRITE var is injected at the SAME dir fleet READS from,
    # so read-side and write-side share one knob and cannot drift onto different dirs.
    monkeypatch.setattr(fleet, "HOOKSTORE_EXPLICIT", True)
    monkeypatch.setattr(fleet, "HOOKSTORE", "/tmp/private-hookstore")
    assert fleet._profile_env()["CMUX_AGENT_HOOK_STATE_DIR"] == "/tmp/private-hookstore"


def test_profile_emits_per_profile_hookstore(capsys):
    # "share nothing" must cover cmux's hooks too, or side-by-side stacks still share ~/.cmuxterm and a
    # test stack could SEE prod's agents. The write-side then follows via _profile_env in the activated shell.
    fleet.cmd_profile(["p", "--base", "/tmp/cf-hs"])
    out = capsys.readouterr().out
    assert "export CMUX_HOOKSTORE_DIR=/tmp/cf-hs/state/hookstore" in out
