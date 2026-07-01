"""Layer 4 — the INSTALLED-WHEEL smoke suite (codex P1.1 / P2.2 / P3.2).

Everything else in the suite runs the package from the checkout via PYTHONPATH; that is exactly what
FAILED to catch the wheel-installed `fleet profile` regression (it derived a checkout-style PLUGIN_ROOT
from `__file__` and, in a wheel, emitted a nonexistent site-packages `bin/`, pointed the marketplace at
the Python lib dir, and silently skipped the seed). So this module builds a REAL wheel, installs it into
a throwaway venv, and drives the installed `fleet` console script in a CLEAN env (no CMUX_*, no
PYTHONPATH, no memsearch/plugin leakage, a temp $HOME) — the only layer that proves packaged-data access
and entrypoint resolution the way a `uv tool install` user actually gets them.

Skipped when `uv` is not on PATH (the whole build/install path needs it). Wheel build + venv install run
ONCE per session (module-scoped fixture); the individual tests are cheap subprocess calls.
"""
import os
import shutil
import subprocess
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS_DIR)

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="needs `uv` to build+install the wheel")


def _clean_env(home):
    """A stranger-clean env: only PATH + a temp HOME. Everything that could leak the host build —
    CMUX_* (state/toml/bin/marketplace), PYTHONPATH (the checkout), and memsearch/plugin vars — is
    absent, so anything the installed app resolves it resolves from the WHEEL + XDG-under-temp-HOME."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home}


def _run(fleet_exe, home, *args, expect=0):
    p = subprocess.run([fleet_exe, *args], env=_clean_env(home), capture_output=True, text=True)
    if expect is not None:
        assert p.returncode == expect, f"`fleet {' '.join(args)}` rc={p.returncode}\n{p.stdout}\n{p.stderr}"
    return p


def _uv_env():
    """Env for the fixture's `uv build/venv/pip` calls. The test suite may run under `uv run`, whose
    active $VIRTUAL_ENV / $UV_* / $PYTHONPATH make `uv pip install` do a non-standard (editable/cache)
    install where the package lives in the build source tree, not site-packages — which would defeat the
    whole point of this suite. Scrub those so we get a clean, copied wheel install into OUR venv."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("VIRTUAL_ENV", "PYTHONPATH", "CONDA_PREFIX") and not k.startswith("UV_")}
    return env


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    """Build the wheel from the checkout, install it into a throwaway venv, yield (fleet_exe, venv_bin)."""
    env = _uv_env()
    root = tmp_path_factory.mktemp("wheel-smoke")
    dist = root / "dist"
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(dist), REPO],
                   check=True, capture_output=True, text=True, env=env)
    wheels = list(dist.glob("*.whl"))
    assert wheels, f"no wheel built into {dist}"
    venv = root / "venv"
    subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True, text=True, env=env)
    vpy = venv / "bin" / "python"
    subprocess.run(["uv", "pip", "install", "--python", str(vpy), str(wheels[0])],
                   check=True, capture_output=True, text=True, env=env)
    fleet_exe = venv / "bin" / "fleet"
    assert fleet_exe.exists(), f"console script not installed at {fleet_exe}"
    # Guard: prove it's a real copied install (package under the venv), not an editable/source-tree link
    # — otherwise this suite would silently retest the checkout instead of the wheel.
    probe = subprocess.run([str(vpy), "-c", "import cmux_fleet.cli as c; print(c.__file__)"],
                           cwd=str(root), env={"PATH": os.environ.get("PATH", ""), "HOME": str(root)},
                           capture_output=True, text=True)
    assert str(venv) in probe.stdout, f"wheel not installed into venv site-packages: {probe.stdout!r}"
    return {"fleet": str(fleet_exe), "venv_bin": str(venv / "bin"), "root": str(root)}


# --- the Phase 0 acceptance: wheel-installed `fleet profile --init` -------------------------------
def test_wheel_profile_init_pins_installed_bin_and_seeds(installed, tmp_path):
    home = str(tmp_path / "home"); os.makedirs(home)
    base = str(tmp_path / "base")
    p = _run(installed["fleet"], home, "profile", "smoke", "--base", base, "--init")
    out = p.stdout

    # 1. PATH pins the INSTALLED console-script dir (venv/bin), not a nonexistent site-packages/bin.
    assert f'export PATH={installed["venv_bin"]}:' in out, out

    # 2. The seed roster was actually written (importlib.resources read the force-included data).
    assert os.path.exists(os.path.join(base, "fleet.toml")), "profile --init did not seed fleet.toml"

    # 3. NOTHING emitted points a bare plugin name at a Python lib dir. In a wheel install with no
    #    explicit $CMUX_FLEET_MARKETPLACE the marketplace pin is correctly OMITTED (not site-packages).
    assert "CMUX_FLEET_MARKETPLACE" not in out, "wheel install must not emit an inferred marketplace pin"
    assert "site-packages" not in out and "/lib/python" not in out, out


def test_wheel_profile_bin_override_env(installed, tmp_path):
    # $CMUX_FLEET_BIN wins: an operator can point PATH at any fleet they choose (repointable app path).
    home = str(tmp_path / "home"); os.makedirs(home)
    override = str(tmp_path / "custom" / "bin")
    os.makedirs(override)
    env = _clean_env(home); env["CMUX_FLEET_BIN"] = override
    p = subprocess.run([installed["fleet"], "profile", "p"], env=env, capture_output=True, text=True)
    assert f"export PATH={override}:" in p.stdout, p.stdout


# --- P2.2/P3.2: the installed console script runs at all in a clean env ---------------------------
def test_wheel_help(installed, tmp_path):
    home = str(tmp_path / "home"); os.makedirs(home)
    p = _run(installed["fleet"], home, "--help")
    assert "launch" in p.stdout and "profile" in p.stdout and "daemon" in p.stdout


def test_wheel_python_m_entrypoint(installed, tmp_path):
    # `python -m cmux_fleet` == the console script; must work from the wheel with no REPO on PYTHONPATH.
    home = str(tmp_path / "home"); os.makedirs(home)
    vpy = os.path.join(installed["venv_bin"], "python")
    p = subprocess.run([vpy, "-m", "cmux_fleet", "--help"], env=_clean_env(home), capture_output=True, text=True)
    assert p.returncode == 0 and "launch" in p.stdout, p.stderr


def test_wheel_ls_empty(installed, tmp_path):
    home = str(tmp_path / "home"); os.makedirs(home)
    p = _run(installed["fleet"], home, "ls")
    assert "LIVE FLEET" in p.stdout


def test_wheel_daemon_status(installed, tmp_path):
    # No daemon running under this temp HOME -> status prints, any exit code is fine (not a crash/import err).
    home = str(tmp_path / "home"); os.makedirs(home)
    p = _run(installed["fleet"], home, "daemon", "status", expect=None)
    combined = p.stdout + p.stderr
    assert "Traceback" not in combined, combined


# --- Phase 3 end-to-end: the REAL plugin shim shells into the REAL installed `fleet` --------------
def test_hook_shim_invokes_installed_app(installed, tmp_path):
    """The unit tests drive the shim against a FAKE fleet; this proves the shim resolves and runs the
    genuinely-installed console script end to end. Seed one completion via the installed package, then
    run the awareness shim with CMUX_FLEET_BIN pointed at the installed fleet -> it must forward the
    verb's additionalContext JSON."""
    state = str(tmp_path / "state")
    vpy = os.path.join(installed["venv_bin"], "python")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CMUX_STATE_DIR": state}
    seed = subprocess.run(
        [vpy, "-c", "from cmux_fleet import state as fs; "
                    "fs.inbox_put('completion','SFC',{'label':'w1','gist':'shim e2e ok'})"],
        env=env, capture_output=True, text=True)
    assert seed.returncode == 0, seed.stderr

    shim = os.path.join(REPO, "scripts", "hooks", "awareness.py")
    env2 = dict(env, CMUX_FLEET_BIN=installed["fleet"], CMUX_SURFACE_ID="SFC")
    p = subprocess.run([sys.executable, shim], input=b'{"hook_event_name":"UserPromptSubmit"}',
                       env=env2, capture_output=True)
    assert p.returncode == 0
    assert b"shim e2e ok" in p.stdout and b"additionalContext" in p.stdout, p.stdout
