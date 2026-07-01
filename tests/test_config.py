"""Layer 2 — config.py resolution precedence.

config.py resolves every path/setting AT IMPORT TIME, so each scenario runs in a FRESH interpreter
(can't re-resolve in-process). The helper writes an optional fleet.toml, sets env, imports config in
a subprocess, and dumps the resolved constants + captures stderr (where the malformed/relative warns
land). Precedence under test: env > [fleet] toml > built-in default (XDG / $HOME / which / "").
"""
import json
import os
import subprocess
import sys
import textwrap

from conftest import SCRIPTS

_DUMP = textwrap.dedent("""
    import json, config
    keys = ["ROOT","STATE","CMUX","MARKETPLACE","FLOOR","HOOKSTORE","ADHOC_SUBDIR","FLEET_TOML","TOML_DIR"]
    print(json.dumps({k: getattr(config, k) for k in keys}))
""")


def _resolve(env=None, toml_text=None, toml_dir=None, cwd=None):
    """Import config in a clean subprocess. Returns (constants_dict, stderr_text)."""
    e = {k: v for k, v in os.environ.items() if not k.startswith("CMUX_") and k != "XDG_CONFIG_HOME"
         and k != "XDG_STATE_HOME"}
    e["PYTHONPATH"] = SCRIPTS
    if toml_text is not None:
        td = toml_dir or cwd or os.getcwd()
        path = os.path.join(td, "fleet.toml")
        with open(path, "w") as f:
            f.write(toml_text)
        e["CMUX_FLEET_TOML"] = path
    else:
        # Hermeticity: strip of CMUX_* + XDG_CONFIG_HOME would otherwise let config fall back to the
        # HOST's real ~/.config/cmux-fleet/fleet.toml. Point at an absent path so config resolves pure
        # built-in defaults regardless of any machine config (e.g. a cutover-created fleet.toml).
        e["CMUX_FLEET_TOML"] = os.path.join(toml_dir or cwd or os.getcwd(), "__no_such_fleet__.toml")
    if env:
        e.update(env)
    p = subprocess.run([sys.executable, "-c", _DUMP], env=e, cwd=cwd, capture_output=True, text=True)
    assert p.returncode == 0, f"config import failed: {p.stderr}"
    return json.loads(p.stdout), p.stderr


# --- precedence ----------------------------------------------------------------------------------
def test_env_beats_toml(tmp_path):
    env_root = str(tmp_path / "from_env")
    c, _ = _resolve(env={"CMUX_FLEET_ROOT": env_root}, toml_text='[fleet]\nroot = "/from/toml"\n',
                    toml_dir=str(tmp_path))
    assert c["ROOT"] == env_root


def test_toml_used_when_no_env(tmp_path):
    c, _ = _resolve(toml_text='[fleet]\nroot = "/from/toml"\n', toml_dir=str(tmp_path))
    assert c["ROOT"] == "/from/toml"


def test_xdg_state_default(tmp_path):
    xdg = str(tmp_path / "xdgstate")
    c, _ = _resolve(env={"XDG_STATE_HOME": xdg})
    assert c["STATE"] == os.path.join(xdg, "cmux-fleet")


def test_root_default_is_home(tmp_path):
    c, _ = _resolve(env={"HOME": str(tmp_path)})
    assert c["ROOT"] == str(tmp_path)


def test_marketplace_and_floor_default_empty():
    c, _ = _resolve()
    assert c["MARKETPLACE"] == ""
    assert c["FLOOR"] == ""


# --- the dirname anchor (relative toml path -> toml's dir) --------------------------------------
def test_toml_relative_dot_anchors_to_toml_dir(tmp_path):
    c, _ = _resolve(toml_text='[fleet]\nroot = "."\n', toml_dir=str(tmp_path))
    assert c["ROOT"] == str(tmp_path)


def test_toml_relative_subdir_anchors(tmp_path):
    c, _ = _resolve(toml_text='[fleet]\nroot = "sub/repo"\n', toml_dir=str(tmp_path))
    assert c["ROOT"] == os.path.join(str(tmp_path), "sub", "repo")


def test_env_relative_path_anchors_to_cwd_with_warning(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    c, err = _resolve(env={"CMUX_FLEET_ROOT": "rel_root"}, cwd=str(cwd))
    assert c["ROOT"] == os.path.join(str(cwd), "rel_root")
    assert "relative path" in err


# --- malformed vs absent toml -------------------------------------------------------------------
def test_malformed_toml_warns_and_falls_back(tmp_path):
    xdg = str(tmp_path / "xs")
    c, err = _resolve(env={"XDG_STATE_HOME": xdg}, toml_text="this is : not = valid toml [[[",
                      toml_dir=str(tmp_path))
    assert "malformed" in err or "unreadable" in err
    # falls back to the XDG default rather than splitting state on a broken file
    assert c["STATE"] == os.path.join(xdg, "cmux-fleet")


def test_absent_toml_is_silent(tmp_path):
    # point CMUX_FLEET_TOML at a non-existent path -> no warning, defaults apply
    c, err = _resolve(env={"CMUX_FLEET_TOML": str(tmp_path / "nope.toml"),
                           "XDG_STATE_HOME": str(tmp_path / "xs")})
    assert "malformed" not in err and "unreadable" not in err
    assert c["STATE"].endswith(os.path.join("xs", "cmux-fleet"))


# --- cmux_bin: a bare command name must NOT be path-anchored ------------------------------------
def test_cmux_bin_bare_name_not_anchored(tmp_path):
    c, _ = _resolve(env={"CMUX_BIN": "mycmux"}, toml_text="[fleet]\n", toml_dir=str(tmp_path))
    assert c["CMUX"] == "mycmux"  # _resolve (not _resolve_path): no TOML_DIR join
