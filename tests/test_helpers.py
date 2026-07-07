"""Layer 2 — the agent-facing helper VERBS (Phase 2 fold, codex P2.1).

`fleet drive-child` is covered by test_launch_enter_race (the enter-race unit); here we cover the other
three folded verbs — peer-msg, inbox-ack, child-digest — through their `cmux_fleet.helpers` entrypoints,
plus the CLI dispatch that routes the hyphenated subcommands to them. Pure/in-process against the
throwaway STATE (no cmux): peer-msg runs with --no-wake so it never shells out.
"""
import json
import os
import sys

import pytest

from cmux_fleet import cli as fleet
from cmux_fleet import helpers as fh


@pytest.fixture
def two_peers(fs, monkeypatch):
    """Register a sender + recipient as live agents so peer-msg can resolve labels<->surfaces."""
    fs.live_put("sender", {"surface": "SND", "kind": "conductor", "role": "c"})
    fs.live_put("recipient", {"surface": "RCP", "kind": "conductor", "role": "c"})
    monkeypatch.setenv("CMUX_SURFACE_ID", "SND")
    return fs


# --- dispatch: the hyphenated verbs actually route to helpers through cli.main -------------------
def test_cli_dispatch_routes_hyphenated_verb(fs, monkeypatch, capsys):
    # Prove the cli.main dispatch table routes a hyphenated verb to its helper (not just that the callable
    # exists): child-digest is side-effect-free with a fragment that matches nothing — it returns 1 and
    # prints its own message, which only cmd_child_digest emits.
    monkeypatch.setattr(sys, "argv", ["fleet", "child-digest", "nomatchfrag"])
    rc = fleet.main()
    assert rc == 1
    assert "child-digest: no transcript" in capsys.readouterr().out


def test_cli_dispatch_routes_inbox_ack(fs, monkeypatch):
    # inbox-ack with no surface exits via its own usage guard -> proves `inbox-ack` reaches cmd_inbox_ack.
    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
    monkeypatch.setattr(sys, "argv", ["fleet", "inbox-ack", "7"])
    with pytest.raises(SystemExit):
        fleet.main()


# --- peer-msg -> inbox-ack roundtrip -------------------------------------------------------------
def test_peer_msg_puts_row_then_inbox_ack_clears(two_peers, capsys):
    fs = two_peers
    rc = fh.cmd_peer_msg(["recipient", "please", "review", "the", "diff", "--no-wake"])
    assert rc == 0
    pend = fs.inbox_pending("RCP", kind="peer")
    assert len(pend) == 1
    row = pend[0]
    assert row["from_label"] == "sender" and row["to_label"] == "recipient"
    assert row["body"] == "please review the diff"
    assert row["reply_expected"] is True                 # fresh (non-reply) message expects a reply

    # the recipient acks through the delivered seq -> it stops being pending
    hi = fs.max_seq(pend)
    fh.cmd_inbox_ack([str(hi), "--peer", "--surface", "RCP"])
    assert fs.inbox_pending("RCP", kind="peer") == []


def test_peer_msg_reply_to_marks_reply_and_no_reply_flag(two_peers):
    fs = two_peers
    fh.cmd_peer_msg(["recipient", "ack on it", "--reply-to", "abc123", "--no-wake"])
    row = fs.inbox_pending("RCP", kind="peer")[0]
    assert row["ptype"] == "peer-reply" and row["reply_to"] == "abc123"
    assert row["reply_expected"] is False                # a reply expects no further reply by default


def test_peer_msg_unknown_label_exits(two_peers):
    with pytest.raises(SystemExit):
        fh.cmd_peer_msg(["nobody", "hi", "--no-wake"])


def test_peer_msg_muted_by_passive(two_peers, monkeypatch, capsys):
    # 'passive' is the fleet-wide wake mute (codex BLOCKER): peer-msg must NOT wake, but STILL queues.
    fs = two_peers
    with open(fs.MODEFILE, "w") as f:
        f.write("passive")
    called = []
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: called.append(surf) or True)
    rc = fh.cmd_peer_msg(["recipient", "hello while muted"])    # NO --no-wake
    assert rc == 0
    assert called == []                                         # muted -> wake_if_idle never invoked
    assert len(fs.inbox_pending("RCP", kind="peer")) == 1       # but the row IS queued
    assert "passive" in capsys.readouterr().out


def test_broadcast_muted_by_passive(fs, monkeypatch, capsys):
    # 'passive' also mutes broadcast wakes (codex BLOCKER): no wake, rows still queued.
    fs.live_put("sender", {"surface": "SND", "kind": "conductor", "role": "c"})
    fs.live_put("c1", {"surface": "C1", "kind": "conductor", "role": "c"})
    monkeypatch.setenv("CMUX_SURFACE_ID", "SND")
    with open(fs.MODEFILE, "w") as f:
        f.write("passive")
    called = []
    monkeypatch.setattr(fs, "wake_if_idle", lambda surf, msg: called.append(surf) or True)
    rc = fleet.cmd_broadcast(["heads up", "--target", "all-conductors"])
    assert rc == 0
    assert called == []                                         # muted -> no wake attempted
    assert len(fs.inbox_pending("C1", kind="peer")) == 1        # the row IS queued
    assert "passive" in capsys.readouterr().out


# --- inbox-ack guards ----------------------------------------------------------------------------
def test_inbox_ack_requires_surface(monkeypatch):
    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
    with pytest.raises(SystemExit):
        fh.cmd_inbox_ack(["5"])


def test_inbox_ack_completion_default_kind(fs, monkeypatch):
    monkeypatch.setenv("CMUX_SURFACE_ID", "S")
    fs.inbox_put("completion", "S", {"label": "w1", "gist": "done"})
    hi = fs.max_seq(fs.inbox_pending("S", kind="completion"))
    fh.cmd_inbox_ack([str(hi)])                           # no --peer -> acks the completion stream
    assert fs.inbox_pending("S", kind="completion") == []


def test_inbox_ack_doctor_clears_only_doctor_stream(fs, monkeypatch):
    """--doctor acks the fleet-doctor health-alert stream per-kind, leaving a co-pending completion
    untouched (the alert channels are independent high-waters)."""
    monkeypatch.setenv("CMUX_SURFACE_ID", "S")
    fs.inbox_put("completion", "S", {"label": "w1", "gist": "done"})
    fs.inbox_put("doctor", "S", {"reason": "stall", "label": "wedged", "child_surface": "X"})
    hi = fs.max_seq(fs.inbox_pending("S", kind="doctor"))
    fh.cmd_inbox_ack([str(hi), "--doctor"])
    assert fs.inbox_pending("S", kind="doctor") == []            # doctor stream cleared...
    assert len(fs.inbox_pending("S", kind="completion")) == 1    # ...completion untouched


# --- inbox (the on-demand catch-up read) ---------------------------------------------------------
def _seed_mixed_inbox(fs, surf="S"):
    """One pending row of each kind addressed to `surf`, in seq order (completion, peer, doctor)."""
    fs.inbox_put("completion", surf, {"label": "w1", "gist": "shipped the fix"})
    fs.inbox_put("peer", surf, {"ptype": "peer-msg", "from_label": "peerc", "to_label": "me",
                                "msg_id": "abc123", "reply_expected": True, "body": "look at X"})
    fs.inbox_put("doctor", surf, {"reason": "stall", "label": "wedged", "child_surface": "GHOST"})


def test_inbox_lists_pending_across_kinds(fs, monkeypatch, capsys):
    monkeypatch.setenv("CMUX_SURFACE_ID", "S")
    fs.live_put("me", {"surface": "S", "kind": "conductor", "role": "c"})
    _seed_mixed_inbox(fs)
    rc = fh.cmd_inbox([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "3 pending" in out and "oldest first" in out
    assert "[completion]" in out and "w1" in out and "shipped the fix" in out
    assert "[peer]" in out and "peerc" in out and "look at X" in out and "REPLY EXPECTED" in out
    assert "[doctor]" in out and "wedged" in out and "stall" in out
    # per-kind ack hint: completion (bare), peer, doctor all present; no stale flag (none pending)
    assert "fleet inbox-ack" in out and "--peer" in out and "--doctor" in out
    assert "--stale" not in out


def test_inbox_empty_prints_zero_pending_not_error(fs, monkeypatch, capsys):
    monkeypatch.setenv("CMUX_SURFACE_ID", "S")
    rc = fh.cmd_inbox([])
    out = capsys.readouterr().out
    assert rc == 0                                           # empty is a clean read, not an error
    assert "0 pending" in out


def test_inbox_json_emits_raw_records(fs, monkeypatch, capsys):
    monkeypatch.setenv("CMUX_SURFACE_ID", "S")
    _seed_mixed_inbox(fs)
    rc = fh.cmd_inbox(["--json"])
    assert rc == 0
    recs = json.loads(capsys.readouterr().out)
    assert [r["kind"] for r in recs] == ["completion", "peer", "doctor"]   # oldest→newest, raw rows
    assert recs[0]["gist"] == "shipped the fix" and recs[1]["from_label"] == "peerc"


def test_inbox_acked_rows_drop_out(fs, monkeypatch, capsys):
    # inbox is a view over inbox_pending -> an acked completion no longer shows (the catch-up stays current)
    monkeypatch.setenv("CMUX_SURFACE_ID", "S")
    fs.inbox_put("completion", "S", {"label": "w1", "gist": "done"})
    hi = fs.max_seq(fs.inbox_pending("S", kind="completion"))
    fs.inbox_ack("S", "completion", hi)
    rc = fh.cmd_inbox([])
    assert rc == 0 and "0 pending" in capsys.readouterr().out


def test_inbox_no_surface_exits(fs, monkeypatch):
    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
    with pytest.raises(SystemExit):
        fh.cmd_inbox([])


def test_inbox_surface_override_reads_another_agent(fs, monkeypatch, capsys):
    # --surface debugs another agent's inbox regardless of $CMUX_SURFACE_ID
    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
    fs.inbox_put("completion", "OTHER", {"label": "wx", "gist": "peek"})
    rc = fh.cmd_inbox(["--surface", "OTHER"])
    out = capsys.readouterr().out
    assert rc == 0 and "1 pending" in out and "peek" in out


def test_cli_dispatch_routes_inbox(fs, monkeypatch, capsys):
    # prove `inbox` reaches cmd_inbox through cli.main (distinct from inbox-ack)
    monkeypatch.setenv("CMUX_SURFACE_ID", "S")
    monkeypatch.setattr(sys, "argv", ["fleet", "inbox"])
    rc = fleet.main()
    assert rc == 0 and "0 pending" in capsys.readouterr().out


# --- child-digest --------------------------------------------------------------------------------
def test_child_digest_no_transcript_returns_1(fs, capsys):
    rc = fh.cmd_child_digest(["nomatchfragment"])
    assert rc == 1
    assert "no transcript found" in capsys.readouterr().out


def test_child_digest_reads_claude_transcript(fs, tmp_path, monkeypatch, capsys):
    # a minimal claude-dialect JSONL under a fake ~/.claude projects dir; child-digest globs for it.
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "p"
    proj.mkdir(parents=True)
    sid = "deadbeefcafe"
    tx = proj / f"session-{sid}.jsonl"
    tx.write_text(
        '{"type":"user","message":{"content":"do the thing"}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"did the thing"}]}}\n')
    monkeypatch.setenv("HOME", str(home))
    rc = fh.cmd_child_digest([sid, "3"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "do the thing" in out and "did the thing" in out
    assert "[USER]" in out and "[ASSISTANT]" in out
