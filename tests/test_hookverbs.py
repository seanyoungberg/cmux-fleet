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


def test_awareness_renders_conductor_liveness_alerts(fs):
    """The two conductor-liveness rows (condition #5, routed to a PEER conductor) render a distinct DOWN
    line and — unlike the still-live health rows — DO carry a `fleet revive` affordance (the seat is gone)."""
    fs.inbox_put("doctor", SURF, {"reason": "conductor-down", "label": "berg-sandbox",
                                  "child_surface": "F1C0AEDB", "down_s": 900})
    fs.inbox_put("doctor", SURF, {"reason": "conductor-closed", "label": "cmux-advisor",
                                  "child_surface": "DCCA9A19"})
    ctx = json.loads(_run("hook-awareness").stdout)["hookSpecificOutput"]["additionalContext"]
    assert "CONDUCTOR DOWN" in ctx and "berg-sandbox" in ctx
    assert "CONDUCTOR SURFACE CLOSED" in ctx and "cmux-advisor" in ctx
    assert "fleet revive berg-sandbox" in ctx                         # the seat is gone -> revive affordance


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


# --- hook-stopfailure / hook-notification (F2: hooks-first structured halt truth) ----------------
def _park_halt():
    return {"api_error": True, "error": "rate_limit", "api_status": 429, "stop_reason": "", "detail": "resets 12:10am"}


def test_stopfailure_rate_limit_records_a_limit_parked_park(fs):
    from cmux_fleet import features as ff
    stdin = json.dumps({"error_type": "rate_limit", "session_id": "sid-x",
                        "error_message": "You've hit your session limit · resets 12:10am (America/New_York)"})
    p = _run("hook-stopfailure", surface="SURF-SF", stdin=stdin)
    assert p.returncode == 0 and p.stdout.strip() == ""          # a recorder: injects nothing into context
    h = fs.halt_get("SURF-SF")
    assert h and h["api_error"] is True and h["error"] == "rate_limit" and h["api_status"] == 429
    assert h["detail"] == "resets 12:10am"                        # resets-HH:MM extracted from error_message
    assert ff._classify("needsInput", True, h) == "limit-parked"  # structure -> park, never red/error


def test_stopfailure_other_error_type_records_errored(fs):
    from cmux_fleet import features as ff
    p = _run("hook-stopfailure", surface="SURF-ER",
             stdin=json.dumps({"error_type": "overloaded", "session_id": "s", "error_message": "busy"}))
    assert p.returncode == 0
    h = fs.halt_get("SURF-ER")
    assert h["api_error"] is True and h["api_status"] is None and h["detail"] == "overloaded"
    assert ff._classify("idle", True, h) == "errored"


def test_stopfailure_unknown_error_type_is_recorded_not_dropped_and_logged_loudly(fs):
    from cmux_fleet import features as ff
    p = _run("hook-stopfailure", surface="SURF-UN",
             stdin=json.dumps({"error_type": "brand_new_type", "session_id": "s", "error_message": "?"}))
    assert p.returncode == 0
    h = fs.halt_get("SURF-UN")
    assert h and ff._classify("idle", True, h) == "errored"       # recorded (never dropped)
    assert os.path.exists(fs.HOOK_ANOMALY_LOG)                    # ...and logged LOUDLY for triage
    assert "brand_new_type" in open(fs.HOOK_ANOMALY_LOG).read()


def test_stopfailure_no_surface_is_a_safe_noop(fs):
    p = _run("hook-stopfailure", surface=None,
             stdin=json.dumps({"error_type": "rate_limit", "session_id": "s"}))
    assert p.returncode == 0 and p.stdout.strip() == ""


def test_notification_completed_clears_the_park_but_needs_input_leaves_it(fs):
    # agent_completed = a turn finished (a parked agent never completes) -> clear the park (ready corroboration);
    # a needs-input notification must NOT touch it (the Feed gate stays authoritative).
    fs.halt_set("SURF-N", "s", _park_halt())
    p = _run("hook-notification", surface="SURF-N",
             stdin=json.dumps({"notification_type": "agent_completed", "session_id": "s"}))
    assert p.returncode == 0 and fs.halt_get("SURF-N") is None
    fs.halt_set("SURF-N2", "s", _park_halt())
    _run("hook-notification", surface="SURF-N2", stdin=json.dumps({"notification_type": "agent_needs_input"}))
    assert fs.halt_get("SURF-N2") is not None                     # gate-authoritative type leaves the park


def test_notification_idle_prompt_does_NOT_clear_a_fresh_park(fs):
    # THE trap (live specimen: a Notification fired ~60s after the halt while the agent sat idle waiting for
    # the reset). A rate-limit park reads AS idle-at-the-prompt, so idle_prompt must NEVER clear the park it
    # would otherwise wipe the state F2 just recorded, seconds after recording it.
    fs.halt_set("SURF-IDLE", "s", _park_halt())
    p = _run("hook-notification", surface="SURF-IDLE",
             stdin=json.dumps({"notification_type": "idle_prompt", "session_id": "s"}))
    assert p.returncode == 0 and fs.halt_get("SURF-IDLE") is not None   # park SURVIVES an idle notification


def test_awareness_and_drain_clear_a_recorded_park(fs):
    # forward progress — a new prompt (awareness) or a clean Stop (drain) — means the agent moved PAST the halt.
    fs.halt_set(SURF, "s", _park_halt())
    _run("hook-awareness")
    assert fs.halt_get(SURF) is None
    fs.halt_set(SURF, "s", _park_halt())
    _run("hook-drain")
    assert fs.halt_get(SURF) is None
