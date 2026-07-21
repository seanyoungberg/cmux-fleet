# tests/test_boot_dispatch.py — the T6 boot contract (cmux-advisor, Berg 2026-07-20).
#
# Three load-bearing pieces, all on this branch:
#   1. LAUNCH sends a machine-composed turn-one boot prompt, CONVERGED onto recycle --fresh's
#      prime-prompt path — ONE source (_boot_prime_prompt), never two.
#   2. `--brief` queues a work brief to the CHILD's inbox at launch (input-safe, label-addressed).
#   3. The brief surfaces via idle-wake the moment the child first goes idle POST-PRIME — an unprimed
#      agent can NEVER receive a raw brief. The structural guarantee: nothing wakes the child until its
#      first post-prime Stop, at which point the router self-wakes it on a pending brief.
import pytest

from cmux_fleet import cli as fleet
from cmux_fleet import helpers as fh
from cmux_fleet import router
from cmux_fleet import state as fs


# ============================ 1. the ONE turn-one boot prompt (converged source) ============================
def test_boot_prime_prompt_carries_the_four_placeholder_elements(tmp_path):
    """Identity + run /loom:prime --role <role> + handover discovery + report-when-primed (T6 point 7)."""
    p = fleet._boot_prime_prompt("kidA", "cmux-dev", str(tmp_path), event="launched")
    assert "kidA" in p and "cmux-dev" in p                       # identity line
    assert "/loom:prime --role cmux-dev" in p                    # the load-bearing prime directive
    assert "Report when primed" in p                             # report-when-primed
    assert "PLACEHOLDER" in p                                    # marked pending prime-architect review


def test_boot_prime_prompt_does_handover_discovery(tmp_path):
    """When a handover exists it is named (prime picks it up); when none, the clean-boot clause fires."""
    hd = tmp_path / "handover"
    hd.mkdir()
    (hd / "kidA-2026-07-20.md").write_text("state")
    got = fleet._boot_prime_prompt("kidA", "cmux-dev", str(tmp_path), event="launched")
    assert "kidA-2026-07-20.md" in got                           # handover discovery surfaced the file
    # a fresh home with no handover -> the clean-boot clause, never a dangling "at ." path
    clean = fleet._boot_prime_prompt("kidA", "cmux-dev", str(tmp_path / "empty"), event="launched")
    assert "No handover yet" in clean


@pytest.mark.parametrize("event,marker", [("launched", "LAUNCHED"), ("recycled", "FRESH"), ("revived", "REVIVED")])
def test_boot_prime_prompt_event_only_varies_the_opening(tmp_path, event, marker):
    """The event varies ONLY the opening clause; the /loom:prime body is identical across all three so
    there is exactly ONE boot-prompt source (T6 converge mandate)."""
    p = fleet._boot_prime_prompt("kidA", "cmux-dev", str(tmp_path), event=event)
    assert marker in p
    assert "/loom:prime --role cmux-dev" in p and "Report when primed" in p


def test_recycle_fresh_converges_onto_the_boot_prompt(fs, monkeypatch):
    """recycle --fresh now sends the SAME boot prompt (identity + /loom:prime + handover + report), not
    the old inline 're-orient from your handover' string that never told the agent to prime."""
    monkeypatch.setattr(fleet, "_compose_recycle_cmd", lambda *a, **k: ("claude ...", ""))
    monkeypatch.setattr(fleet, "_latest_handover", lambda cwd, label=None: "/x/handover/h.md")
    entry = {"kind": "child", "surface": "A", "tool": "claude", "role": "cmux-dev", "cwd": "/x",
             "session": "claude-s"}
    p = fleet._recycle_plan("kidA", entry, [], [], "fresh", "", False, None, False)
    assert "/loom:prime --role cmux-dev" in p["prime"]           # CONVERGED: recycle now primes explicitly
    assert "h.md" in p["prime"] and "FRESH" in p["prime"]        # ...still does handover discovery


# ============================ 2 + 3. brief queue + post-prime idle-wake delivery ============================
def _happy_launch(monkeypatch, tmp_path, *extra):
    """Drive cmd_launch through a stubbed happy path (surface bound, session bound), capturing every
    cmuxq() send. Returns (rc, sent) where `sent` is the list of cmuxq argument tuples."""
    ROLES = {"role": {"adhoc": {"cwd": "agents/ad-hoc", "claude": {}}}, "defaults": {"tool": "claude"}}
    monkeypatch.setattr(fleet, "load_config", lambda: ROLES)
    monkeypatch.setattr(fleet, "create_surface", lambda spec, parent, direction: ("WS", "SURF"))
    monkeypatch.setattr(fleet, "_bind_launched_session",
                        lambda ws, surf, *a, **k: (ws, surf, "newsid"))
    monkeypatch.setattr(fleet, "log_launch", lambda *a, **k: None)     # skip host settings reads
    monkeypatch.setattr(fleet, "_link_floor_claudemd", lambda *a, **k: None)
    monkeypatch.setattr(fleet.time, "sleep", lambda s: None)
    sent = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: sent.append(a) or "")
    rc = fleet.cmd_launch(["--adhoc", "probe", "--place", "workspace", "--parent", "none",
                           "--cwd", str(tmp_path), *extra])
    return rc, sent


def test_launch_sends_boot_prompt_as_turn_one(fs, monkeypatch, tmp_path):
    """The launcher composes + sends the turn-one boot prompt itself (the dispatcher never types it)."""
    rc, sent = _happy_launch(monkeypatch, tmp_path)
    assert rc == 0
    sends = [a for a in sent if a and a[0] == "send"]
    assert sends, "launch must send a turn-one boot prompt"
    boot = sends[0][3]                                            # ("send","--surface","SURF",<boot text>)
    assert "/loom:prime --role adhoc" in boot and "Report when primed" in boot
    assert ("send-key", "--surface", "SURF", "enter") in sent    # ...and SUBMIT it


def test_launch_brief_queues_to_child_inbox_not_the_input_box(fs, monkeypatch, tmp_path):
    """--brief lands in the CHILD's inbox (input-safe), never typed into its input box."""
    rc, sent = _happy_launch(monkeypatch, tmp_path, "--brief", "ship the T6 boot contract")
    assert rc == 0
    pending = fs.inbox_pending("SURF", kind="brief")
    assert len(pending) == 1
    assert pending[0]["body"] == "ship the T6 boot contract"
    assert pending[0]["from_label"] == "operator"                # top-level launch -> operator
    # the brief text is NEVER one of the typed sends (input-safe): only the boot prompt + enter are typed
    assert not any("ship the T6 boot contract" in str(a) for a in sent)


def test_launch_no_prime_suppresses_boot_prompt(fs, monkeypatch, tmp_path):
    rc, sent = _happy_launch(monkeypatch, tmp_path, "--no-prime")
    assert rc == 0
    assert not [a for a in sent if a and a[0] == "send"]          # opt-out: no boot prompt typed


def test_launch_brief_without_prime_is_refused(fs, monkeypatch, tmp_path):
    """--brief rides the POST-prime idle-wake; --no-prime would strand it on an unprimed agent. Refuse."""
    with pytest.raises(SystemExit):
        _happy_launch(monkeypatch, tmp_path, "--brief", "x", "--no-prime")


def test_alert_pending_wakes_on_a_brief(fs):
    """A brief is wake-worthy (unlike a peer msg, whose own send path wakes it)."""
    fs.inbox_put("brief", "SURF", {"label": "kidA", "from_label": "op", "body": "do the thing"})
    assert router._alert_pending("SURF")                         # brief -> wake-worthy
    fs2 = "SURF2"
    fs.inbox_put("peer", fs2, {"body": "hi", "msg_id": "m1"}, event_key="peer:m1")
    assert not router._alert_pending(fs2)                        # peer -> NOT (peer-msg self-wakes)


def test_router_self_wakes_the_child_on_a_pending_brief_post_prime(fs, monkeypatch):
    """The decisive wiring: when a child with a queued brief first goes idle (its first Stop = post-prime),
    the router self-wakes it on its OWN surface so the brief surfaces. A child with NO brief is untouched."""
    uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fs.live_put("parent", {"surface": "PARENT", "kind": "conductor", "role": "c", "session": "claude-parent"})
    fs.live_put("child", {"surface": "CHILD", "kind": "child", "role": "w", "parent": "parent",
                          "session": f"claude-{uuid}"})
    fs.inbox_put("brief", "CHILD", {"label": "child", "from_label": "parent", "body": "your assignment"})

    monkeypatch.setattr(router, "LIVE", True)
    monkeypatch.setattr(router, "_last", {})                      # fresh per-surface debounce (module global)
    monkeypatch.setattr(router, "store",
                        lambda: {"sessions": {uuid: {"sessionId": uuid, "surfaceId": "CHILD"}},
                                 "activeSessionsBySurface": {"CHILD": {"sessionId": uuid}}})
    monkeypatch.setattr(router, "cmux", lambda *a, **k: "")
    monkeypatch.setattr(router.time, "sleep", lambda s: None)
    waked = []
    monkeypatch.setattr(router, "maybe_idle_wake", lambda surface, label: waked.append(surface))

    router.handle({"name": "agent.hook.Stop", "occurred_at": "2026-07-01T12:00:00Z",
                   "payload": {"phase": "completed", "session_id": f"claude-{uuid}"}})
    assert "CHILD" in waked                                       # self-wake fired on the child's own surface


def test_router_does_not_self_wake_a_child_without_a_brief(fs, monkeypatch):
    """CONTROL: a normal child (no brief in its own inbox) is never self-woken — only its parent is
    notified. Adding the brief rail must not perturb ordinary child completions."""
    uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    fs.live_put("parent", {"surface": "PARENT", "kind": "conductor", "role": "c", "session": "claude-parent"})
    fs.live_put("child", {"surface": "CHILD", "kind": "child", "role": "w", "parent": "parent",
                          "session": f"claude-{uuid}"})
    monkeypatch.setattr(router, "LIVE", True)
    monkeypatch.setattr(router, "_last", {})                      # fresh per-surface debounce (module global)
    monkeypatch.setattr(router, "store",
                        lambda: {"sessions": {uuid: {"sessionId": uuid, "surfaceId": "CHILD"}},
                                 "activeSessionsBySurface": {"CHILD": {"sessionId": uuid}}})
    monkeypatch.setattr(router, "cmux", lambda *a, **k: "")
    monkeypatch.setattr(router.time, "sleep", lambda s: None)
    waked = []
    monkeypatch.setattr(router, "maybe_idle_wake", lambda surface, label: waked.append(surface))

    router.handle({"name": "agent.hook.Stop", "occurred_at": "2026-07-01T12:00:00Z",
                   "payload": {"phase": "completed", "session_id": f"claude-{uuid}"}})
    assert "CHILD" not in waked                                   # child self-wake NEVER fires without a brief
    assert waked == ["PARENT"]                                    # only the parent-delivery wake


# ============================ brief rendering + ack ============================
def test_inbox_renders_and_acks_a_brief(fs):
    seq = fs.inbox_put("brief", "SURF", {"label": "kidA", "from_label": "cmux-advisor", "body": "the task"})
    line = fh._inbox_line(fs.inbox_pending("SURF", kind="brief")[0])
    assert "[brief]" in line and "cmux-advisor" in line and "the task" in line
    # a bare seq ack resolves the row's kind from the row itself (event-key ack) — no --brief needed
    fh.cmd_inbox_ack([str(seq), "--surface", "SURF"])
    assert fs.inbox_pending("SURF", kind="brief") == []          # cleared on every path
