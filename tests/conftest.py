"""Shared fixtures for the cmux-fleet test suite.

The whole suite is stdlib + pytest only (near-zero deps). State isolation hinges on ONE fact:
`config.py` resolves `STATE` from `$CMUX_STATE_DIR` AT IMPORT TIME. So this conftest points
`$CMUX_STATE_DIR` at a throwaway dir BEFORE any test module imports `config`/`fleet_state`, and an
autouse fixture wipes that dir between tests for per-test isolation.

Subprocess-based tests (hooks, the CLI E2E) inherit the SAME `$CMUX_STATE_DIR` so in-process seeding
via `fleet_state` is visible to the child process. The config-resolution tests deliberately spawn
fresh interpreters with their own env/toml (import-time resolution can't be re-done in-process).
"""
import os
import shutil
import sys
import tempfile

# --- repo geometry -------------------------------------------------------------------------------
# The app is the `cmux_fleet` package (import from REPO); the plugin's hook + agent-helper scripts still
# live under scripts/ (folded into `fleet` subcommands / hook verbs in Phases 2-3).
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS_DIR)
SCRIPTS = os.path.join(REPO, "scripts")
HOOKS_DIR = os.path.join(SCRIPTS, "hooks")

# --- pin STATE to a throwaway dir BEFORE config/fleet_state import ------------------------------
# A session-lived temp dir; the clean_state fixture empties it each test. It must be set in the
# process env (config reads it at import) AND be importable by the subprocess children we spawn.
_STATE_DIR = tempfile.mkdtemp(prefix="cmux-fleet-test-state-")
os.environ["CMUX_STATE_DIR"] = _STATE_DIR
# Keep config's other knobs from leaking the host machine's real config into a test run.
os.environ.pop("CMUX_FLEET_TOML", None)
os.environ.pop("CMUX_FLEET_ROOT", None)
os.environ.pop("CMUX_FLEET_MARKETPLACE", None)
os.environ.pop("CMUX_FLEET_FLOOR", None)
# Also drop the AMBIENT caller surface: the scope-aware read verbs (ls/vitals/inbox/graph) default to
# `--scope mine` and self-ID from $CMUX_SURFACE_ID. If the dev runs the suite from inside a live cmux
# agent, that surface would leak in and silently scope every bare `fleet ls` to the dev's own (empty, in
# the throwaway registry) fleet. Popping it once makes the default the no-surface path (graceful -> all);
# tests that exercise a specific caller set CMUX_SURFACE_ID themselves via monkeypatch/env.
os.environ.pop("CMUX_SURFACE_ID", None)

sys.path.insert(0, REPO)          # so `import cmux_fleet...` resolves in-process

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    """Empty the shared STATE dir before each test so registry/inbox/archive start clean."""
    for name in os.listdir(_STATE_DIR):
        p = os.path.join(_STATE_DIR, name)
        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
    yield


@pytest.fixture
def state_dir():
    return _STATE_DIR


@pytest.fixture
def fs():
    """The in-process state module (STATE already points at the throwaway dir)."""
    from cmux_fleet import state
    return state


@pytest.fixture(scope="session")
def cmux_stub(tmp_path_factory):
    """A no-op `cmux` executable. The CLI shells out to it for close-surface/read-screen/etc.; the
    stub exits 0 and prints nothing, so state-transition verbs (archive/rm) complete with no real cmux."""
    d = tmp_path_factory.mktemp("cmux-stub")
    stub = d / "cmux"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return str(stub)


@pytest.fixture
def cli_env(cmux_stub, tmp_path):
    """Base env for a `fleet` CLI subprocess: shared throwaway STATE, stub cmux, an isolated ROOT,
    and REPO on PYTHONPATH so `python -m cmux_fleet` (and the plugin helper scripts) import the package."""
    env = dict(os.environ)
    env["CMUX_STATE_DIR"] = _STATE_DIR
    env["CMUX_BIN"] = cmux_stub
    env["CMUX_FLEET_ROOT"] = str(tmp_path)
    hookstore = tmp_path / "hookstore"
    hookstore.mkdir(exist_ok=True)
    env["CMUX_HOOKSTORE_DIR"] = str(hookstore)  # keep _pid_for_surface off the host's real ~/.cmuxterm
    env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
    return env


@pytest.fixture(autouse=True)
def _hermetic_ps_sweep(monkeypatch):
    """resolve.pids_ps (the never-orphan union source) shells one `ps axeww` per call. A REAL sweep
    inside the suite is slow, box-dependent, and — when the suite runs inside a live fleet agent —
    can see the dev's own agents' environments. Hermetic default: a sweep that WORKED and found no agent,
    patched at the raw text seam so pids_ps's PARSING stays real code under test. Tests that exercise the
    union inject `ps_out` explicitly or re-patch resolve.pids_ps (tests/test_resolve.py).

    IT MUST NOT BE `""`. An empty sweep is a FAILED sweep — a box always has processes — and
    `resolve.liveness` reads it as UNKNOWN and refuses, which is the whole point of that state. Returning
    "" here would make every test in the suite silently simulate a broken `ps`, quietly converting every
    "nothing is running there" into "I could not look", and the one test that means to exercise a genuinely
    dead agent would stop being able to. So: a real-shaped table, with real processes, and no agent on the
    surface under test."""
    import cmux_fleet.resolve as _resolve
    # PID TT STAT TIME COMMAND... — the columns pids_ps actually parses. No CMUX_SURFACE_ID, so no seat agent.
    no_agents = ("  501   ??  Ss     0:01.23 /sbin/launchd\n"
                 "  777   ??  S      0:00.10 /usr/sbin/cfprefsd agent\n")
    monkeypatch.setattr(_resolve, "_ps_axeww", lambda: no_agents)
