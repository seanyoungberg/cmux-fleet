"""Layer 2 — the THIN PLUGIN SHIM contract (Phase 3 / codex P1.2 + P1.3).

The plugin's hook files (scripts/hooks/{awareness,drain}.py) are now thin fail-open shims: they shell
into the installed app's `fleet hook-<verb>` and pass its stdout through ONLY when it is rc0 + valid
JSON of the expected shape. Everything else fails open: BLANK stdout, exit 0. These tests drive the real
shim scripts against a FAKE `fleet` whose behavior is env-selected, covering every failure mode.

(The uvx fallback was dropped in Phase 3 — the plugin requires the app on PATH — so there are no
uvx-missing/uvx-timeout/offline-cache cases: "app missing" is the single no-app path, tested below.)
"""
import os
import subprocess
import sys

import pytest
from conftest import HOOKS_DIR

AWARENESS = os.path.join(HOOKS_DIR, "awareness.py")
DRAIN = os.path.join(HOOKS_DIR, "drain.py")

FAKE_FLEET = r'''#!/usr/bin/env python3
import os, sys, time, json
mode = os.environ.get("FAKE_FLEET_MODE", "valid")
verb = sys.argv[1] if len(sys.argv) > 1 else ""
data = sys.stdin.buffer.read()
if mode == "timeout":
    time.sleep(5)                       # the shim's inner timeout (set short in the test) fires first
if mode == "nonzero":
    sys.stdout.write(json.dumps({"decision": "block", "reason": "R"})); sys.exit(3)
if mode == "noise":
    sys.stdout.write("this is not json <<garbage>>"); sys.exit(0)
if mode == "wrongshape":
    sys.stdout.write(json.dumps({"foo": 1})); sys.exit(0)
if mode == "echo-stdin":
    open(os.environ["FAKE_FLEET_STDIN_OUT"], "wb").write(data)
    mode = "valid"
if mode == "valid":
    if verb == "hook-awareness":
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": "CTX-OK"}}))
    else:
        sys.stdout.write(json.dumps({"decision": "block", "reason": "REASON-OK"}))
    sys.exit(0)
sys.exit(0)
'''


@pytest.fixture
def fake_fleet(tmp_path):
    p = tmp_path / "fleet"
    p.write_text(FAKE_FLEET)
    p.chmod(0o755)
    return str(p)


def _run_shim(shim, fleet_bin, stdin=b'{"hook_event_name":"x"}', mode="valid", extra=None, timeout="4"):
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"                 # no real `fleet` on PATH -> only CMUX_FLEET_BIN resolves
    env.pop("CMUX_FLEET_BIN", None)
    if fleet_bin is not None:
        env["CMUX_FLEET_BIN"] = fleet_bin
    env["FAKE_FLEET_MODE"] = mode
    env["CMUX_FLEET_HOOK_TIMEOUT"] = timeout
    if extra:
        env.update(extra)
    return subprocess.run([sys.executable, shim], input=stdin, env=env, capture_output=True)


# --- valid passthrough (both verbs) --------------------------------------------------------------
def test_awareness_passthrough_valid(fake_fleet):
    p = _run_shim(AWARENESS, fake_fleet, mode="valid")
    assert p.returncode == 0
    assert b'"additionalContext": "CTX-OK"' in p.stdout


def test_drain_passthrough_valid(fake_fleet):
    p = _run_shim(DRAIN, fake_fleet, mode="valid")
    assert p.returncode == 0
    assert b'"reason": "REASON-OK"' in p.stdout


# --- fail-open failure modes: all -> blank stdout, exit 0 ----------------------------------------
def test_app_missing_fails_open(tmp_path):
    # CMUX_FLEET_BIN points nowhere and no `fleet` on PATH -> silent no-op (the dropped-uvx design).
    p = _run_shim(AWARENESS, str(tmp_path / "nope" / "fleet"))
    assert p.returncode == 0 and p.stdout == b""


def test_app_missing_no_env_fails_open():
    p = _run_shim(AWARENESS, None)                # neither CMUX_FLEET_BIN nor PATH resolves fleet
    assert p.returncode == 0 and p.stdout == b""


def test_nonzero_exit_fails_open(fake_fleet):
    # even with well-formed stdout, a nonzero rc must NOT be forwarded.
    p = _run_shim(DRAIN, fake_fleet, mode="nonzero")
    assert p.returncode == 0 and p.stdout == b""


def test_stdout_noise_fails_open(fake_fleet):
    p = _run_shim(AWARENESS, fake_fleet, mode="noise")
    assert p.returncode == 0 and p.stdout == b""


def test_wrong_shape_fails_open(fake_fleet):
    # valid JSON but not the expected hook shape -> prefer blank over corrupt protocol output.
    p = _run_shim(AWARENESS, fake_fleet, mode="wrongshape")
    assert p.returncode == 0 and p.stdout == b""


def test_timeout_fails_open(fake_fleet):
    # fake fleet sleeps 5s; inner timeout is 1s -> shim must still exit 0 blank (not a timed-out hook).
    p = _run_shim(DRAIN, fake_fleet, mode="timeout", timeout="1")
    assert p.returncode == 0 and p.stdout == b""


# --- stdin is consumed and passed through to the app --------------------------------------------
def test_stdin_passed_through(fake_fleet, tmp_path):
    sentinel = tmp_path / "stdin.bin"
    payload = b'{"hook_event_name":"UserPromptSubmit","session_id":"abc"}'
    p = _run_shim(AWARENESS, fake_fleet, stdin=payload, mode="echo-stdin",
                  extra={"FAKE_FLEET_STDIN_OUT": str(sentinel)})
    assert p.returncode == 0
    assert b'"additionalContext": "CTX-OK"' in p.stdout      # still forwards valid output
    assert sentinel.read_bytes() == payload                  # the app received the exact event payload
