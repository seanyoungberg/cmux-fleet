"""Launch verification: only the PID may condemn, and a dark surface is repaired, not killed.

THE CLASS OF BUG THIS FILE EXISTS TO END. Four times in one week an alarm's printed remedy would have
destroyed the thing the alarm misdiagnosed. `fleet launch` did it in both directions at once:

  - It INVENTED a failure. A fleet-launched codex, running perfectly, was reported `!!! LAUNCH FAILED ...
    the process exited on spawn` with `fleet rm --kill` as the cure — because the first line of its pane was
    rc noise from the operator's ~/.zshrc, printed before codex was even exec'd. The pane cannot answer
    "is it alive"; it was asked anyway, and its answer was allowed to carry a kill command.
  - It MISSED a real one. On 2 of 4 launches cmux filed the session under a surfaceId that was not the one
    it seated the agent on, and kept stamping that phantom. The agent runs, takes work, completes turns —
    and is invisible to vitals/ls/the sidebar, permanently.

The rule, and every test here is a statement of it: **a verdict may rest only on an authoritative signal
(the process table). A heuristic may WARN and may never CONDEMN. The remedy must be proportionate to the
confidence of the alarm.**
"""
import json
import os

import pytest

from cmux_fleet import cli
from cmux_fleet import resolve as rs


# The REAL pane that caused the false conviction (2026-07-12), verbatim: rc noise on line 3, printed by
# `zsh -ilc` before codex is exec'd, and a codex TUI that has not yet painted a `Context N% left` status
# line — so BOTH of the old guards (agent_tui_visible, the below-the-launch-line positional rule) miss it.
PANE_ZSHRC_NOISE = """\
Last login: Sun Jul 12 12:36:45 on ttys026
You have new mail.
/Users/seanyoungberg/.zshrc:.:65: no such file or directory: /Users/seanyoungberg/.local/bin/env
╭─────────────────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.144.1)                              │
│ model:       gpt-5.5 xhigh   /model to change           │
│ directory:   ~/tapestry/_meta/agents/ad-hoc/codex-probe │
│ permissions: YOLO mode                                  │
╰─────────────────────────────────────────────────────────╯
› Explain this codebase
  gpt-5.5 xhigh · ~/tapestry/_meta/agents/ad-hoc/codex-probe
"""

PANE_REAL_DEATH = """\
/Users/seanyoungberg/.zshrc:.:65: no such file or directory: /Users/seanyoungberg/.local/bin/env
error: unexpected argument '--effort' found
Usage: codex [OPTIONS] [PROMPT]
"""

PANE_CLEAN_BOOTING = "Last login: Sun Jul 12 on ttys026\n"


# --- direction 1: the verdict is pid-authoritative ------------------------------------------------
def test_a_LIVE_process_is_never_a_failed_launch_however_ugly_the_pane():
    """THE false conviction, pinned. This exact pane text, with the process ALIVE, was reported as a dead
    launch with a `rm --kill` remedy. An implementation that grades on pane text returns 'failed' here."""
    verdict, err, _ = cli.launch_verdict({4989}, PANE_ZSHRC_NOISE)

    # first prove the trap is ARMED: the scanner really does find that line ugly. Without this the test
    # could pass for the wrong reason (a pane the scanner simply had no opinion about).
    assert err, "the trap is disarmed — the scanner no longer flags the rc line, so this proves nothing"
    assert verdict == "running-odd", "a live process was called dead"
    assert verdict != "failed"


def test_a_live_process_with_a_clean_pane_is_simply_running():
    assert cli.launch_verdict({4989}, PANE_CLEAN_BOOTING)[0] == "running"


def test_only_NO_PROCESS_plus_an_error_may_condemn():
    """The real death: codex rejected a claude-ism and exited. No pid, and the pane says why."""
    assert cli.launch_verdict(set(), PANE_REAL_DEATH)[0] == "failed"


def test_the_condemnation_shows_the_GUILTY_line_not_just_the_first_one():
    """When it does convict, the operator must see the actual cause. The first error-looking line on this
    box is innocent rc noise; the real one is below it. Reporting only the first hands out a red herring —
    which is precisely how the original misdiagnosis got its authority."""
    lines = cli.launch_error_lines(PANE_REAL_DEATH)
    assert any(".zshrc" in l for l in lines), "the innocent line should still be shown, in order"
    assert any("--effort" in l for l in lines), "the GUILTY line was hidden behind the rc noise"


def test_no_process_and_a_quiet_pane_is_UNBOUND_not_dead():
    """A cold start is not a corpse. 'no pid' alone must not condemn — the lazy tools bind on their first
    turn, and a slow box has not exec'd through zsh yet. Silence is not evidence."""
    assert cli.launch_verdict(set(), PANE_CLEAN_BOOTING)[0] == "unbound"


def test_the_update_modal_alone_does_not_condemn_a_live_agent():
    """A wedged-looking pane on a LIVE process is still not a kill order — the modal strings could appear
    in any codex output (a transcript, a paste, this very file)."""
    pane = "Update available!\n  Skip until next version\n"
    assert cli.launch_verdict({123}, pane)[0] == "running-odd"
    assert cli.launch_verdict(set(), pane)[0] == "failed"


@pytest.mark.parametrize("pids,pane,expect", [
    ({1}, PANE_ZSHRC_NOISE, "running-odd"),
    ({1}, PANE_REAL_DEATH, "running-odd"),      # even a REAL flag error: if it is alive, it is not dead
    (set(), PANE_ZSHRC_NOISE, "failed"),
    (set(), PANE_CLEAN_BOOTING, "unbound"),
])
def test_the_matrix(pids, pane, expect):
    assert cli.launch_verdict(pids, pane)[0] == expect


def test_condemnation_is_reachable_at_ALL(monkeypatch):
    """The reachable-green guard: an implementation that just never says 'failed' would satisfy every test
    above. It must still convict when the evidence is authoritative."""
    assert cli.launch_verdict(set(), PANE_REAL_DEATH)[0] == "failed"


# --- direction 2: dark = alive but unfiled --------------------------------------------------------
def _surface(monkeypatch, *, ps_alive, store_present):
    monkeypatch.setattr(rs, "pids_ps", lambda s, ps_out=None, tool="claude": {777} if ps_alive else set())
    monkeypatch.setattr(rs, "occupants", lambda s, **kw: {777} if ps_alive else set())
    monkeypatch.setattr(rs, "present", lambda s: store_present)


def test_dark_is_ALIVE_but_NOT_FILED(monkeypatch):
    """The live specimens: a real agent on the surface, and cmux filing nothing there. It reads as death to
    every store-derived check, and the reflex cure for death would land a SECOND agent on its worktree."""
    _surface(monkeypatch, ps_alive=True, store_present=False)
    assert rs.dark("A63131E0", "claude") is True
    assert rs.alive("A63131E0", "claude") is True


def test_a_healthy_agent_is_not_dark(monkeypatch):
    _surface(monkeypatch, ps_alive=True, store_present=True)
    assert rs.dark("DA75E95A", "claude") is False


def test_an_EMPTY_surface_is_not_dark(monkeypatch):
    """Nothing alive is not darkness — it is emptiness. Conflating them would send the re-seat machinery
    after surfaces that hold no agent at all."""
    _surface(monkeypatch, ps_alive=False, store_present=False)
    assert rs.dark("DEADBEEF", "claude") is False


# --- the observability PROOF: is cmux actually stamping this surface? -----------------------------
SURF = "A63131E0-0000-0000-0000-000000000000"
PHANTOM = "7A220F01-0000-0000-0000-000000000000"
WS = "3B45A9C9-0000-0000-0000-000000000000"


def _events(tmp_path, monkeypatch, rows):
    d = tmp_path / "cmuxterm"
    d.mkdir()
    with open(d / "events.jsonl", "w") as f:
        for args in rows:
            f.write(json.dumps({"name": "sidebar.metadata.updated",
                                "payload": {"command": "set_status", "args": args}}) + "\n")
    monkeypatch.setattr(rs, "HOOKSTORE", str(d))
    return str(d)


def test_stamps_are_counted_for_the_PANEL(tmp_path, monkeypatch):
    _events(tmp_path, monkeypatch, [
        f"claude_code Running --icon=bolt.fill --tab={WS} --panel={SURF} --pid=4989",
        f"claude_code Running --icon=bolt.fill --tab={WS} --panel={SURF} --pid=4989",
    ])
    assert rs.stamps_since(SURF, 0) == 2


def test_a_DARK_surface_stamps_the_PHANTOM_and_never_itself(tmp_path, monkeypatch):
    """The live specimen, reproduced: 94 stamps went to a surfaceId that does not exist in the cmux tree,
    and 0 to the one the agent is actually on (and believes it is on, per its own env)."""
    _events(tmp_path, monkeypatch, [f"claude_code Running --tab={WS} --panel={PHANTOM} --pid=4989"] * 94)
    assert rs.stamps_since(PHANTOM, 0) == 94
    assert rs.stamps_since(SURF, 0) == 0, "the dark surface must show NO stamps — that is what makes it dark"


def test_keying_on_TAB_would_invert_the_answer(tmp_path, monkeypatch):
    """--panel is the SURFACE; --tab is the WORKSPACE. Key on --tab and every agent sharing a conductor's
    workspace 'proves' every other agent's surface is stamping — so a dark agent reads healthy and the
    repair never fires. Pinned because the two look alike and sit in the same string."""
    _events(tmp_path, monkeypatch, [f"claude_code Running --tab={WS} --panel={PHANTOM} --pid=4989"])
    assert rs.stamps_since(WS, 0) == 0, "a WORKSPACE id was counted as a surface's stamps"


def test_the_cursor_excludes_a_previous_tenants_stamps(tmp_path, monkeypatch):
    """The cursor is taken BEFORE the surface is created. Without it, a re-seat onto a recycled surface id
    could 'prove' observability with stamps that belonged to whoever sat there before."""
    d = _events(tmp_path, monkeypatch, [f"claude_code Running --tab={WS} --panel={SURF} --pid=1"])
    cursor = os.path.getsize(os.path.join(d, "events.jsonl"))
    assert rs.stamps_since(SURF, cursor) == 0            # nothing NEW since the mark
    with open(os.path.join(d, "events.jsonl"), "a") as f:
        f.write(json.dumps({"name": "sidebar.metadata.updated",
                            "payload": {"args": f"claude_code Running --panel={SURF} --pid=2"}}) + "\n")
    assert rs.stamps_since(SURF, cursor) == 1


def test_a_missing_event_log_is_zero_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "HOOKSTORE", str(tmp_path / "nope"))
    assert rs.stamps_since(SURF, 0) == 0
    assert rs.stamp_cursor() == 0


def test_alive_never_consults_the_store(monkeypatch):
    """`alive` must be process-table-only. If it ever falls back to the store it inherits the store's
    blindness, and a dark agent becomes condemnable again — which is the whole bug."""
    monkeypatch.setattr(rs, "pids_ps", lambda s, ps_out=None, tool="claude": {777})
    monkeypatch.setattr(rs, "present", lambda s: (_ for _ in ()).throw(
        AssertionError("alive() read the hook store")))
    assert rs.alive("S", "claude") is True


# --- the tri-state: "I could not look" is not "nothing is there" ----------------------------------
# The two branches that got merged here each had a liveness authority, and move-refuse's was the stronger
# one: it knew that an empty `ps` sweep is a FAILED sweep (a box always has processes), and refused. Mine
# did not — `alive()` collapsed it to False, so a broken sweep could have driven a `LAUNCH FAILED` whose
# printed cure is `fleet rm --kill`. One authority now, and the strength flows both ways.
def test_a_FAILED_sweep_can_never_condemn_a_launch():
    """The hole the merge closed. Sweep failed -> we did not look -> we do not get to convict. Absence of
    evidence is not evidence of absence, least of all when the remedy prints `rm --kill`."""
    verdict, err, _ = cli.launch_verdict(set(), PANE_REAL_DEATH, swept=False)
    assert verdict == "unproven"
    assert verdict != "failed", "a blind sweep condemned a launch"
    assert err, "the pane error is still reported — we warn, we just do not convict"


def test_a_WORKING_sweep_that_finds_nothing_still_condemns():
    """Reachable-green: the refusal must not swallow the real failure it was built around. A sweep that ran
    and found no process, with a startup error on the pane, is still a dead launch."""
    assert cli.launch_verdict(set(), PANE_REAL_DEATH, swept=True)[0] == "failed"


def test_liveness_is_a_TRI_state_and_UNKNOWN_is_not_GONE(monkeypatch):
    monkeypatch.setattr(rs, "ps_sweep", lambda: "")                       # the sweep FAILED
    monkeypatch.setattr(rs, "pids", lambda s, st=None: set())             # and the store knows nothing
    verdict, pids, why = rs.liveness("S", tool="claude")
    assert verdict == rs.UNKNOWN and verdict != rs.GONE
    assert "could not read the process table" in why
    assert rs.alive("S", "claude") is False        # the BOOLEAN loses the state — which is why it must
                                                   # never gate a destructive act. Documented, and pinned.


def test_a_live_STORE_pid_proves_life_without_the_process_table(monkeypatch):
    """Order is load-bearing. Proving a thing ALIVE needs one witness; only the NEGATIVE conclusion needs a
    working sweep. Checking the sweep first would answer UNKNOWN about an agent the store already proved was
    running — and it did, until this was fixed (a live conductor got the DOWN script)."""
    monkeypatch.setattr(rs, "ps_sweep", lambda: "")                       # blind...
    monkeypatch.setattr(rs, "pids", lambda s, st=None: {4989})            # ...but the store SAW it
    verdict, pids, _ = rs.liveness("S", tool="claude")
    assert verdict == rs.LIVE and pids == [4989]


def test_the_store_can_only_ever_push_toward_LIVE(monkeypatch):
    """move-refuse's non-negotiable: the store must never be able to AUTHORIZE a destructive act. It can add
    life (refuse), never subtract it (allow) — the process table alone decides GONE."""
    monkeypatch.setattr(rs, "pids", lambda s, st=None: set())             # store: nothing
    monkeypatch.setattr(rs, "ps_sweep", lambda: "  1 ?? Ss 0:01 /sbin/launchd\n")
    monkeypatch.setattr(rs, "pids_ps", lambda s, ps_out=None, tool=None: {4989})   # ps: ALIVE
    assert rs.liveness("S", tool="claude")[0] == rs.LIVE, "an empty store overrode a live process"
