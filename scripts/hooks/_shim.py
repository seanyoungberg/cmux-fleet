#!/usr/bin/env python3
# scripts/hooks/_shim.py — the THIN, FAIL-OPEN plugin hook shim (Phase 3 / codex P1.2 + P1.3). The plugin
# ships NO hook logic; each hook file (awareness.py, drain.py) just calls run() here, which shells into the
# installed app's `fleet hook-<name>` verb and passes its stdout through ONLY when it is rc0 + valid JSON
# of the expected shape. Every other path fails open: BLANK stdout, exit 0. Stdlib only — it must NOT
# import cmux_fleet (the whole point is that the plugin does not need the app's checkout on sys.path).
#
# Why python and not a shell `exec fleet ...`: a shell shim cannot GUARANTEE exit 0 on app failure, cannot
# BLANK invalid/corrupt output, cannot BOUND runtime, and can mix stderr into the stdout protocol channel.
#
# uvx fallback: DROPPED (codex P1.2). The plugin REQUIRES the app on PATH. A network `uvx` fallback in the
# per-turn hot path risked first-run/offline cost, private-repo auth latency inside hook execution, and —
# worst — the harness's 10s hook timeout killing the shim mid-resolve BEFORE it could reach its own exit 0
# (a timed-out hook, not a graceful fail-open). When `fleet` is absent this shim silently no-ops; fleet
# features simply don't activate and the rest of Claude Code is unaffected. Install the app to turn them on.
import json, os, shutil, subprocess, sys

# Inner timeout, safely under the harness's 10s hook timeout (headroom to still exit 0). Overridable via
# env for tests / an operator on a slow box, but HARD-CLAMPED to TIMEOUT_MAX: a value at/above the
# harness timeout would recreate the P1.2 failure (harness kills the shim mid-run before it can exit 0),
# so anything >= TIMEOUT_MAX (or unparseable) is capped, never honored. Keep the 8s default.
TIMEOUT_MAX = 9.0                     # must stay < the 10s harness hook timeout
try:
    TIMEOUT = float(os.environ.get("CMUX_FLEET_HOOK_TIMEOUT", "") or "8")
except ValueError:
    TIMEOUT = 8.0
if TIMEOUT != TIMEOUT or TIMEOUT <= 0:  # NaN or non-positive -> the safe default
    TIMEOUT = 8.0
TIMEOUT = min(TIMEOUT, TIMEOUT_MAX)     # a bigger override is capped, never honored past the ceiling


def _find_fleet():
    """The installed `fleet` app: $CMUX_FLEET_BIN (an executable OR a bin dir) else `which fleet`. None
    if the app is not installed -> caller fails open.

    $CMUX_FLEET_BIN is AUTHORITATIVE: if it is set but does not resolve to an executable `fleet`, return
    None (fail open blank) — do NOT fall through to `which`. During a strategy-A cutover the operator
    pins CMUX_FLEET_BIN as the atomically-repointable app path; silently running whatever stale `fleet`
    happens to be first on a live agent's baked PATH would defeat that (codex should-fix #1)."""
    env = os.environ.get("CMUX_FLEET_BIN", "").strip()
    if env:
        env = os.path.expanduser(env)
        if os.path.isfile(env) and os.access(env, os.X_OK):
            return env
        cand = os.path.join(env, "fleet")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
        return None                     # explicit-but-invalid -> fail open; never ambient which()
    return shutil.which("fleet")


def _valid(verb, text):
    """The exact output contract per verb — the shim never forwards anything else to the harness. Strict:
    the payload fields must be the right TYPES (a non-string additionalContext/reason, or an awareness
    object missing hookEventName, is corrupt protocol output -> blank fail-open, codex should-fix #4)."""
    try:
        obj = json.loads(text)
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    if verb == "hook-awareness":
        hso = obj.get("hookSpecificOutput")
        return (isinstance(hso, dict)
                and hso.get("hookEventName") == "UserPromptSubmit"
                and isinstance(hso.get("additionalContext"), str))
    if verb == "hook-drain":
        return obj.get("decision") == "block" and isinstance(obj.get("reason"), str)
    return False


def _app_output(verb, data):
    """`fleet <verb>` stdout to pass through (str), or None to fail open. Any failure mode -> None:
    fleet missing, timeout, nonzero exit, empty (a valid no-op), or nonempty-but-wrong-shape output."""
    try:
        fleet = _find_fleet()
        if not fleet:
            return None
        p = subprocess.run([fleet, verb], input=data, capture_output=True, timeout=TIMEOUT)
        if p.returncode != 0:
            return None
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        if out and _valid(verb, out):
            return out
        return None                     # empty = no-op; wrong-shape = prefer blank over corrupt protocol
    except Exception:
        return None


def run(verb):
    """Consume stdin, shell into the app verb, forward valid output else blank. ALWAYS exit 0."""
    try:
        data = sys.stdin.buffer.read()
    except Exception:
        data = b""
    out = _app_output(verb, data)
    if out:
        try:
            sys.stdout.write(out)
            sys.stdout.flush()
        except Exception:
            pass
    sys.exit(0)
