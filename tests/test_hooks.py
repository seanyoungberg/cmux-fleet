"""Layer 2 — the hook stdin -> exit-code contract.

awareness.py (UserPromptSubmit) and drain.py (Stop) are subprocess entrypoints. The contract
(P1-plugin-standards.md §5): read JSON on stdin, self-ID via $CMUX_SURFACE_ID, read state under
$CMUX_STATE_DIR, and ALWAYS exit 0 (fail open) — emitting structured JSON on stdout only when there
is something to surface. awareness adds context; drain returns {decision:"block"} to auto-continue.

State is seeded in-process via `fs` (shared throwaway STATE), then the hook runs in a child process
pointed at the same STATE with a test surface id.
"""
import json
import subprocess
import sys

from conftest import HOOKS_DIR, _STATE_DIR
import os

AWARENESS = os.path.join(HOOKS_DIR, "awareness.py")
DRAIN = os.path.join(HOOKS_DIR, "drain.py")
SURF = "SURF-TEST"
STDIN = json.dumps({"hook_event_name": "x", "session_id": "s", "cwd": "/tmp"})


def _run(hook, surface=SURF, stdin=STDIN):
    env = dict(os.environ)
    env["CMUX_STATE_DIR"] = _STATE_DIR
    if surface is None:
        env.pop("CMUX_SURFACE_ID", None)
    else:
        env["CMUX_SURFACE_ID"] = surface
    return subprocess.run([sys.executable, hook], input=stdin, env=env,
                          capture_output=True, text=True)


def _set_mode(fs, mode):
    with open(fs.MODEFILE, "w") as f:
        f.write(mode)


# --- awareness -----------------------------------------------------------------------------------
def test_awareness_empty_inbox_is_silent(fs):
    p = _run(AWARENESS)
    assert p.returncode == 0
    assert p.stdout.strip() == ""


def test_awareness_surfaces_completion(fs):
    fs.inbox_put("completion", SURF, {"label": "w1", "gist": "finished the audit",
                                      "child_session": "claude-abcd1234ef"})
    p = _run(AWARENESS)
    assert p.returncode == 0
    out = json.loads(p.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "[fleet]" in ctx and "finished the audit" in ctx and "w1" in ctx


def test_awareness_surfaces_peer(fs):
    fs.inbox_put("peer", SURF, {"from_label": "cmux-advisor", "to_label": "me",
                                "body": "status check please", "msg_id": "m1"})
    p = _run(AWARENESS)
    assert p.returncode == 0
    ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "[peer]" in ctx and "status check please" in ctx and "cmux-advisor" in ctx


def test_awareness_no_surface_exits_clean(fs):
    fs.inbox_put("completion", SURF, {"label": "w1", "gist": "x"})
    p = _run(AWARENESS, surface=None)
    assert p.returncode == 0
    assert p.stdout.strip() == ""


def test_awareness_fails_open_on_garbage_stdin(fs):
    p = _run(AWARENESS, stdin="not json at all \x00\xff")
    assert p.returncode == 0


# --- drain ---------------------------------------------------------------------------------------
def test_drain_peer_always_blocks(fs):
    fs.inbox_put("peer", SURF, {"from_label": "p1", "body": "handle me", "msg_id": "m1"})
    p = _run(DRAIN)
    assert p.returncode == 0
    out = json.loads(p.stdout)
    assert out["decision"] == "block"
    assert "handle me" in out["reason"]


def test_drain_completion_ignored_in_passive(fs):
    _set_mode(fs, "passive")
    fs.inbox_put("completion", SURF, {"label": "w1", "gist": "done"})
    p = _run(DRAIN)
    assert p.returncode == 0
    assert p.stdout.strip() == ""  # completions are dial-gated; passive does not chase them


def test_drain_completion_blocks_in_auto(fs):
    _set_mode(fs, "auto")
    fs.inbox_put("completion", SURF, {"label": "w1", "gist": "all done",
                                      "child_session": "claude-1111aaaa"})
    p = _run(DRAIN)
    assert p.returncode == 0
    out = json.loads(p.stdout)
    assert out["decision"] == "block" and "all done" in out["reason"]


def test_drain_no_surface_exits_clean(fs):
    fs.inbox_put("peer", SURF, {"from_label": "p1", "body": "x", "msg_id": "m1"})
    p = _run(DRAIN, surface=None)
    assert p.returncode == 0
    assert p.stdout.strip() == ""


def test_drain_block_guard_does_not_reblock(fs):
    """Once drain has blocked for a seq, a second Stop must not re-block the same un-acked item
    (else the turn loops forever); it falls back to the awareness hook instead."""
    fs.inbox_put("peer", SURF, {"from_label": "p1", "body": "once", "msg_id": "m1"})
    first = _run(DRAIN)
    assert json.loads(first.stdout)["decision"] == "block"
    second = _run(DRAIN)  # same un-acked peer, block-mark already set
    assert second.returncode == 0
    assert second.stdout.strip() == ""
