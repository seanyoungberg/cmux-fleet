# tests/test_blocked.py — the `blocked` column: is this agent waiting on ME?
#
# The column exists because cmux's `needsInput` does NOT mean "waiting on a gate": it is stamped ~60s
# after ANY turn ends, so a done-idle agent and a genuinely gated one are indistinguishable at the
# lifecycle level. Every conductor had to learn that individually, and the ones who didn't read
# `needsInput` as "blocked" and were wrong.
#
# Both errors are expensive and they are NOT the same kind of expensive, which is what these tests pin:
#   false YES -> the conductor sends into a BUSY pane; a mid-turn session send WEDGES the agent.
#   false NO  -> the agent sits forever (the old behavior).
# So `yes` and `no` each need positive evidence, and the absence of evidence must read `?` — never a
# guess in either direction.
#
# The pane fixtures are REAL captures off the live fleet (2026-07-12), not hand-typed approximations —
# including pane-claude-gate-question.txt, which was captured from OUTSIDE this repo's own agent while
# it sat on a genuine AskUserQuestion. Hand-written UI fixtures are exactly how the two-column `ps`
# bug shipped (see resolve._line_is_seat_agent); these are the shapes the tool actually renders.
import os

import pytest

from conftest import REPO

from cmux_fleet import features as ff

FIX = os.path.join(REPO, "tests", "fixtures")


def pane(name):
    with open(os.path.join(FIX, f"pane-claude-{name}.txt"), encoding="utf-8") as f:
        return f.read()


# --- pane_gate: the tri-state screen read (real captures) -----------------------------------------
def test_pane_gate_sees_a_real_askuserquestion_dialog():
    """THE positive case, from a real gate: numbered options under a caret + the selection footer."""
    assert ff.pane_gate(pane("gate-question")) is True


@pytest.mark.parametrize("name", ["idle-prompt", "idle-draft", "working"])
def test_pane_gate_reads_ordinary_panes_as_not_gated(name):
    """The FALSE-POSITIVE guard, and the one that matters most: a send into a busy pane wedges it.

    All three are real panes carrying the normal prompt chrome:
      idle-prompt — done-idle at an empty prompt (cmux says `needsInput`; it is gated on NOBODY)
      idle-draft  — done-idle with a half-typed human draft in the input box (berg-sandbox, live)
      working     — mid-turn, spinner running (`✻ Simmering… (18m 5s · ↓ 63.4k tokens)`)
    A dialog REPLACES that chrome, so its presence is proof no dialog is up."""
    assert ff.pane_gate(pane(name)) is False


def test_pane_gate_abstains_on_an_unreadable_pane():
    """No pane, no verdict. An empty capture is not evidence of anything — least of all of safety."""
    assert ff.pane_gate("") is None
    assert ff.pane_gate(None) is None
    assert ff.pane_gate("\n\n   \n") is None


def test_pane_gate_abstains_when_the_evidence_CONTRADICTS_itself():
    """Gate markers AND the prompt chrome in the same tail -> None, never a verdict.

    This is the discipline of the column in one test: the pane did not clearly say, so we do not decide.
    It is also the live FP guard — an agent that merely PRINTED the words "Enter to select" (this repo's
    own session did, writing the matcher) must not read as gated while its prompt is plainly up."""
    contradictory = pane("working") + "\nEnter to select · Esc to cancel\n"
    assert ff.pane_gate(contradictory) is None


def test_pane_gate_ignores_a_dialog_that_has_scrolled_out_of_the_live_zone():
    """Only the BOTTOM of the screen is live. An ANSWERED dialog scrolls up as the agent resumes work;
    once its output has pushed the dialog clear of the live zone, the pane says `working`, not `gated`.

    (Push it only PART of the way out — leaving the dialog's footer still in the zone beside the live
    prompt — and pane_gate abstains instead, which is the contradiction case above. That is the intended
    seam: partially-visible evidence is not evidence.)"""
    resumed_output = "\n".join(f"  ⏺ doing the thing, step {i}" for i in range(6))
    stale = pane("gate-question") + "\n" + resumed_output + "\n" + pane("working")
    assert ff.pane_gate(stale) is False


def test_pane_gate_survives_the_nbsp_cmux_renders_in_the_prompt_box():
    """cmux renders the prompt as `❯\\xa0` (NBSP, not a space). Every space-bearing pattern here would
    silently miss it un-normalized — the class of bug that makes a matcher quietly always-False."""
    assert "\xa0" in pane("working")                      # the fixture really does carry it
    assert ff.pane_gate(pane("working")) is False


# --- blocked_of: the pure rule -------------------------------------------------------------------
def test_a_feed_gate_is_blocked():
    """cmux's Feed gate row. Proven end-to-end on a live gate (2026-07-12): raising an AskUserQuestion
    posted `kind: "question"` with no resolved_at, and answering it set resolved_at + status=expired."""
    b, why = ff.blocked_of(present=True, feed_gate=True, transcript_gate=False, turn_done=False)
    assert b is True and "feed" in why


def test_a_transcript_gate_is_blocked_even_with_no_feed_row():
    """The transcript is cmux-independent, so it still speaks for a DETACHED agent — whose gate never
    reaches the Feed at all (the hook channel is the thing that is dead)."""
    b, why = ff.blocked_of(present=True, feed_gate=False, transcript_gate=True, turn_done=False)
    assert b is True and "transcript" in why


def test_a_closed_turn_is_not_blocked():
    """The done-idle agent cmux stamps `needsInput` on. A gated agent's last message ALWAYS ends on a
    tool_use, never a terminal stop — so a closed turn is positive proof that no gate can be open."""
    b, why = ff.blocked_of(present=True, feed_gate=False, transcript_gate=False, turn_done=True)
    assert b is False and "turn closed" in why


def test_a_closed_turn_RETIRES_a_stale_feed_row():
    """Precedence, and it is load-bearing: a picker answered by a key-send never marks its Feed row
    terminal. Believing that row forever is a permanent false positive — the expensive error."""
    b, why = ff.blocked_of(present=True, feed_gate=True, transcript_gate=False, turn_done=True)
    assert b is False and "turn closed" in why


def test_mid_turn_with_no_evidence_says_it_CANNOT_TELL():
    """THE point of the column. Mid-turn, a long tool call and a silent dialog are identical to every
    cheap signal — store, lifecycle and transcript all freeze the same way. Guessing `no` strands the
    agent; guessing `yes` wedges it. So: `?`."""
    b, why = ff.blocked_of(present=True, feed_gate=False, transcript_gate=False, turn_done=False)
    assert b is None and "capture-pane" in why


def test_a_dead_seat_is_not_waiting_on_anyone():
    b, why = ff.blocked_of(present=False, feed_gate=False, transcript_gate=False, turn_done=False)
    assert b is False and "no live agent" in why


def test_the_pane_settles_what_the_cheap_signals_could_not():
    yes, _ = ff.blocked_of(present=True, feed_gate=False, transcript_gate=False, turn_done=False, pane=True)
    no, _ = ff.blocked_of(present=True, feed_gate=False, transcript_gate=False, turn_done=False, pane=False)
    unknown, _ = ff.blocked_of(present=True, feed_gate=False, transcript_gate=False, turn_done=False, pane=None)
    assert (yes, no, unknown) == (True, False, None)


# --- the unregistered seat: the resume-picker false negative --------------------------------------
def test_an_unregistered_seat_does_NOT_trust_a_closed_turn():
    """A live seat PROCESS with no hook-store record: SessionStart never fired, so any transcript on this
    surface belongs to a PRIOR session and its closed turn proves NOTHING about what is on screen now.

    This is the `claude --resume` picker: the agent hangs at a dialog, having never taken a turn. Without
    this rule the stale closed turn reads `no` and the agent hangs unseen — the exact false negative the
    column exists to kill. It must fall through to the pane instead."""
    b, why = ff.blocked_of(present=False, feed_gate=False, transcript_gate=False, turn_done=True,
                           unregistered=True)
    assert b is None and "pane" in why


def test_an_unregistered_seat_with_a_dialog_on_its_pane_is_blocked():
    b, why = ff.blocked_of(present=False, feed_gate=False, transcript_gate=False, turn_done=True,
                           unregistered=True, pane=True)
    assert b is True and "unregistered" in why


def test_an_unregistered_seat_at_a_clean_prompt_is_merely_booting():
    b, why = ff.blocked_of(present=False, feed_gate=False, transcript_gate=False, turn_done=False,
                           unregistered=True, pane=False)
    assert b is False and "booting" in why


# --- probe_blocked: the escalation ----------------------------------------------------------------
def _rows(*blocked):
    return [{"label": f"a{i}", "surface": f"S{i}", "blocked": b, "blocked_why": "", "unregistered": False}
            for i, b in enumerate(blocked)]


def test_probe_reads_ONLY_the_panes_it_needs():
    """The affordability claim, pinned: a settled row is never probed. A fleet that is mostly decided
    costs a handful of capture-panes, not one per agent — which is what makes probe-by-default viable."""
    seen = []

    def cap(*args):
        seen.append(args[-1])
        return pane("idle-prompt")

    rows = _rows(True, False, None, False, None)
    assert ff.probe_blocked(rows, cap=cap) == 2
    assert seen == ["S2", "S4"]                       # only the two unsettled rows
    assert [r["blocked"] for r in rows] == [True, False, False, False, False]


def test_probe_leaves_a_row_UNKNOWN_when_the_pane_does_not_say():
    """An unreadable screen is not evidence. The row stays `?` rather than being quietly decided."""
    rows = _rows(None)
    assert ff.probe_blocked(rows, cap=lambda *a: "") == 1
    assert rows[0]["blocked"] is None


def test_probe_promotes_a_real_gate_to_blocked():
    rows = _rows(None)
    ff.probe_blocked(rows, cap=lambda *a: pane("gate-question"))
    assert rows[0]["blocked"] is True


# --- the tri-state contract ------------------------------------------------------------------------
def test_unknown_is_None_so_a_naive_consumer_fails_SAFE():
    """`blocked` is True/False/None and never the strings, precisely because the obvious consumer writes
    `if row["blocked"]: send_answer(...)`. With None, unknown collapses to the SAFE side (no send, no
    wedge). A truthy "unknown"/"?" string would collapse to the expensive one — a send into a busy pane."""
    unknown, _ = ff.blocked_of(present=True, feed_gate=False, transcript_gate=False, turn_done=False)
    assert unknown is None
    assert not unknown                                # the naive truthiness check does NOT fire


def test_the_table_renders_the_tri_state_as_yes_no_question():
    assert ff._blk({"blocked": True}) == "yes"
    assert ff._blk({"blocked": False}) == "no"
    assert ff._blk({"blocked": None}) == "?"


def test_a_newly_blocked_agent_forces_a_watch_repaint():
    """`blocked` is in the change-fingerprint: hitting a gate is the most repaint-worthy event on the
    board, and it can flip with `state` completely unmoved (the feed row and the transcript gate are both
    invisible to the lifecycle string — which is the whole reason this column exists)."""
    base = [{"label": "a", "state": "working", "blocked": False, "ctx_pct_remaining": 50, "last_text": "x"}]
    gated = [dict(base[0], blocked=True)]
    assert ff._vitals_fp(base) != ff._vitals_fp(gated)
