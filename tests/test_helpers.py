"""Layer 2 — the agent-facing helper VERBS (Phase 2 fold, codex P2.1).

`fleet drive-child` is covered by test_launch_enter_race (the enter-race unit); here we cover the other
three folded verbs — peer-msg, inbox-ack, child-digest — through their `cmux_fleet.helpers` entrypoints,
plus the CLI dispatch that routes the hyphenated subcommands to them. Pure/in-process against the
throwaway STATE (no cmux): peer-msg runs with --no-wake so it never shells out.
"""
import os

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


# --- dispatch: the hyphenated verbs route to helpers ---------------------------------------------
def test_cli_dispatch_maps_the_four_verbs():
    from cmux_fleet import helpers
    # main() builds the fns table lazily; assert the helper callables are the ones wired for each verb.
    assert helpers.cmd_drive_child.__name__ == "cmd_drive_child"
    assert helpers.cmd_peer_msg.__name__ == "cmd_peer_msg"
    assert helpers.cmd_child_digest.__name__ == "cmd_child_digest"
    assert helpers.cmd_inbox_ack.__name__ == "cmd_inbox_ack"


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
