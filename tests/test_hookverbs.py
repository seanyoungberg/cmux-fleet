"""Layer 2 — the hook VERB stdin->exit-code contract (Phase 3, moved off the old plugin scripts).

The awareness/drain logic now lives in the app as `fleet hook-awareness` / `fleet hook-drain`
(cmux_fleet.hookverbs). These are the same assertions the old tests/test_hooks.py made against the
standalone scripts, now driven through `python -m cmux_fleet hook-*`: read JSON on stdin, self-ID via
$CMUX_SURFACE_ID, read state under $CMUX_STATE_DIR, ALWAYS exit 0, emit structured JSON only when there
is something to surface (awareness -> additionalContext; drain -> {decision:block}).
"""
import json
import os
import subprocess
import sys

from conftest import REPO, _STATE_DIR

SURF = "SURF-TEST"
STDIN = json.dumps({"hook_event_name": "x", "session_id": "s", "cwd": "/tmp"})


def _run(verb, surface=SURF, stdin=STDIN):
    env = dict(os.environ)
    env["CMUX_STATE_DIR"] = _STATE_DIR
    env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
    if surface is None:
        env.pop("CMUX_SURFACE_ID", None)
    else:
        env["CMUX_SURFACE_ID"] = surface
    return subprocess.run([sys.executable, "-m", "cmux_fleet", verb], input=stdin, env=env,
                          capture_output=True, text=True)


def _set_mode(fs, mode):
    with open(fs.MODEFILE, "w") as f:
        f.write(mode)


# --- hook-awareness ------------------------------------------------------------------------------
def test_awareness_empty_inbox_is_silent(fs):
    p = _run("hook-awareness")
    assert p.returncode == 0
    assert p.stdout.strip() == ""


def test_awareness_surfaces_completion(fs):
    fs.inbox_put("completion", SURF, {"label": "w1", "gist": "finished the audit",
                                      "child_session": "claude-abcd1234ef"})
    p = _run("hook-awareness")
    assert p.returncode == 0
    out = json.loads(p.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "[fleet]" in ctx and "finished the audit" in ctx and "w1" in ctx
    assert "fleet child-digest" in ctx and "fleet inbox-ack" in ctx   # the folded verb hints


def test_awareness_surfaces_peer(fs):
    fs.inbox_put("peer", SURF, {"from_label": "cmux-advisor", "to_label": "me",
                                "body": "status check please", "msg_id": "m1"})
    p = _run("hook-awareness")
    assert p.returncode == 0
    ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "[peer]" in ctx and "status check please" in ctx and "cmux-advisor" in ctx


def test_awareness_surfaces_stale_alert(fs):
    fs.inbox_put("stale", SURF, {"label": "worker", "child_surface": "DEADBEEF-1234",
                                 "via": "surface-closed", "origin": "tab_close"})
    p = _run("hook-awareness")
    assert p.returncode == 0
    ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "auto-archived" in ctx and "worker" in ctx and "tab_close" in ctx
    assert "fleet revive worker" in ctx                               # the recovery hint
    assert "--stale" in ctx                                           # per-kind ack hint


def test_awareness_surfaces_doctor_alert(fs):
    """The fleet-doctor sweep's kind='doctor' health alerts render with a reason-specific line and a
    --doctor ack hint — distinct from the archived 'stale' rows (still LIVE, no revive affordance)."""
    fs.inbox_put("doctor", SURF, {"reason": "stall", "label": "wedged", "child_surface": "AAAA1111",
                                  "stalled_s": 720})
    fs.inbox_put("doctor", SURF, {"reason": "needs-input", "label": "asker", "child_surface": "BBBB2222"})
    p = _run("hook-awareness")
    assert p.returncode == 0
    ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "[fleet-doctor]" in ctx and "wedged" in ctx and "asker" in ctx
    assert "STALLED" in ctx and "NEEDS INPUT" in ctx                  # reason-specific rendering
    assert "revive" not in ctx.split("[fleet-doctor]")[1]             # NOT an archive -> no revive hint
    assert "--doctor" in ctx                                          # per-kind ack hint


def test_awareness_doctor_and_stale_coexist(fs):
    """A doctor health alert and a stale archive alert are DIFFERENT kinds and both surface independently
    (a member can't be both, but the inbox carries both channels)."""
    fs.inbox_put("stale", SURF, {"label": "gone", "child_surface": "DEAD", "via": "surface-closed",
                                 "origin": "tab_close"})
    fs.inbox_put("doctor", SURF, {"reason": "low-ctx", "label": "fullish", "child_surface": "CCCC",
                                  "ctx_pct_remaining": 22})
    ctx = json.loads(_run("hook-awareness").stdout)["hookSpecificOutput"]["additionalContext"]
    assert "auto-archived" in ctx and "gone" in ctx                  # stale channel intact
    assert "LOW CONTEXT" in ctx and "22%" in ctx and "fullish" in ctx  # doctor channel intact


def test_awareness_no_surface_exits_clean(fs):
    fs.inbox_put("completion", SURF, {"label": "w1", "gist": "x"})
    p = _run("hook-awareness", surface=None)
    assert p.returncode == 0
    assert p.stdout.strip() == ""


def test_awareness_fails_open_on_garbage_stdin(fs):
    p = _run("hook-awareness", stdin="not json at all \x00")
    assert p.returncode == 0


# --- hook-drain ----------------------------------------------------------------------------------
def test_drain_peer_always_blocks(fs):
    fs.inbox_put("peer", SURF, {"from_label": "p1", "body": "handle me", "msg_id": "m1"})
    p = _run("hook-drain")
    assert p.returncode == 0
    out = json.loads(p.stdout)
    assert out["decision"] == "block"
    assert "handle me" in out["reason"]


def test_drain_completion_ignored_in_passive(fs):
    _set_mode(fs, "passive")
    fs.inbox_put("completion", SURF, {"label": "w1", "gist": "done"})
    p = _run("hook-drain")
    assert p.returncode == 0
    assert p.stdout.strip() == ""


def test_drain_completion_blocks_in_auto(fs):
    _set_mode(fs, "auto")
    fs.inbox_put("completion", SURF, {"label": "w1", "gist": "all done",
                                      "child_session": "claude-1111aaaa"})
    p = _run("hook-drain")
    assert p.returncode == 0
    out = json.loads(p.stdout)
    assert out["decision"] == "block" and "all done" in out["reason"]


def test_drain_stale_blocks_in_auto_and_mutes_in_passive(fs):
    # same dial as completions: passive is a fleet-wide push mute (awareness still shows it next turn)
    _set_mode(fs, "auto")
    fs.inbox_put("stale", SURF, {"label": "worker", "child_surface": "DEADBEEF-1234",
                                 "via": "surface-closed", "origin": "workspace_teardown"})
    p = _run("hook-drain")
    assert p.returncode == 0
    out = json.loads(p.stdout)
    assert out["decision"] == "block" and "worker" in out["reason"] and "--stale" in out["reason"]

    _set_mode(fs, "passive")
    fs.inbox_put("stale", SURF, {"label": "worker2", "child_surface": "CAFEBABE-5678",
                                 "via": "surface-closed", "origin": "tab_close"})
    p = _run("hook-drain")
    assert p.returncode == 0
    assert p.stdout.strip() == ""                                     # muted like completions


def test_drain_no_surface_exits_clean(fs):
    fs.inbox_put("peer", SURF, {"from_label": "p1", "body": "x", "msg_id": "m1"})
    p = _run("hook-drain", surface=None)
    assert p.returncode == 0
    assert p.stdout.strip() == ""


def test_drain_block_guard_does_not_reblock(fs):
    fs.inbox_put("peer", SURF, {"from_label": "p1", "body": "once", "msg_id": "m1"})
    first = _run("hook-drain")
    assert json.loads(first.stdout)["decision"] == "block"
    second = _run("hook-drain")   # same un-acked peer, block-mark already set
    assert second.returncode == 0
    assert second.stdout.strip() == ""
