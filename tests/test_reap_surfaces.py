# tests/test_reap_surfaces.py — the #6 bare-shell husk reaper SAFETY GATE. `fleet reap-surfaces` is the
# only fleet verb that closes a live cmux surface, so the pure classifier (_husk_evidence) carries the
# whole safety burden: it must fingerprint ONLY fleet-origin husks and NEVER a human's shell or a shell a
# human has since used. Fixtures are distilled from real panes captured on the live fleet 2026-07-08.
from cmux_fleet import cli


# an exited fleet agent: its claude ran, printed the resume hint, exited; a bare shell remains (the
# research-agent / recovery-safety / usage-ops husks today). Launch line carries the fleet env prefix.
EXITED_HUSK = """You have new mail.
seanyoungberg@Mac-Studio research-agent % cd /Users/seanyoungberg/tapestry/_meta/agents/research-agent && CMUX_STATE_DIR=/Users/seanyoungberg/.local/state/cmux-fleet CMUX_FLEET_ROOT=/Users/seanyoungberg/tapestry AGENT_ROLE=research-agent AGENT_LABEL=prior-art-intel claude --setting-sources user,local --model 'claude-opus-4-8[1m]' --dangerously-skip-permissions
Resume this session with:
claude --resume b1cae473-af75-4049-87f3-ec894748c518
seanyoungberg@Mac-Studio research-agent % """

# the reboot-restore replay: the fleet launch command sits UNSUBMITTED at the prompt, nothing after.
DRAFT_HUSK = """seanyoungberg@Mac-Studio usage-ops % cd /x && CMUX_FLEET_ROOT=/y AGENT_ROLE=usage-ops AGENT_LABEL=usage-ops claude --resume 2a94e2aa-2c63-45e2-887f-a818842acbaa --dangerously-skip-permissions"""

# a fleet launch artifact FOLLOWED by a human `cd` (the 0A3A252A live false-positive). Must NOT reap.
HUMAN_TOUCHED = """seanyoungberg@Mac-Studio tapestry % cd /x && AGENT_ROLE=r AGENT_LABEL=lbl claude --resume efae7d95-0aaa-45b0-832b-5e98ab44995d --dangerously-skip-permissions
Resume this session with:
claude --resume efae7d95-0aaa-45b0-832b-5e98ab44995d
seanyoungberg@Mac-Studio tapestry %
seanyoungberg@Mac-Studio tapestry % cd ~/.cmuxterm
seanyoungberg@Mac-Studio .cmuxterm % """

# a plain human shell: no fleet env prefix anywhere. Must NOT reap.
HUMAN_SHELL = """seanyoungberg@Mac-Studio tapestry % git status
On branch main
nothing to commit, working tree clean
seanyoungberg@Mac-Studio tapestry % claude --resume abc
seanyoungberg@Mac-Studio tapestry % """

# a live claude TUI (defensive backstop when a pid read misses).
LIVE_TUI = """  🧠 Context Remaining: 47%
  ⏵⏵ bypass permissions on (shift+tab to cycle)
❯ """


def test_exited_agent_husk_is_reaped_with_harvested_identity():
    ev = cli._husk_evidence(EXITED_HUSK)
    assert ev["husk"] is True
    assert ev["label"] == "prior-art-intel"
    assert ev["resume_id"] == "b1cae473-af75-4049-87f3-ec894748c518"


def test_unsubmitted_draft_husk_is_reaped():
    ev = cli._husk_evidence(DRAFT_HUSK)
    assert ev["husk"] is True
    assert ev["label"] == "usage-ops"
    assert ev["resume_id"] == "2a94e2aa-2c63-45e2-887f-a818842acbaa"


def test_human_touched_after_launch_artifact_is_NOT_reaped():
    """The tail guard: a human command (cd ~/.cmuxterm) after the launch artifact means a human has used
    the shell -> never reap, even though the fleet signature is present. This is the live 0A3A252A case."""
    ev = cli._husk_evidence(HUMAN_TOUCHED)
    assert ev["husk"] is False
    assert "human activity" in ev["reason"]


def test_plain_human_shell_is_NOT_reaped():
    """No fleet env prefix -> not a candidate, even with an incidental `claude --resume abc` a human typed."""
    ev = cli._husk_evidence(HUMAN_SHELL)
    assert ev["husk"] is False
    assert "no fleet launch signature" in ev["reason"]


def test_empty_pane_is_not_a_husk():
    assert cli._husk_evidence("")["husk"] is False
    assert cli._husk_evidence("\n\n  \n")["husk"] is False


def test_prompt_typed_text():
    assert cli._prompt_typed_text("seanyoungberg@Mac-Studio tapestry %") == ""      # bare idle prompt
    assert cli._prompt_typed_text("seanyoungberg@Mac-Studio tapestry % cd ~/x") == "cd ~/x"
    assert cli._prompt_typed_text("❯ ") == ""
    assert cli._prompt_typed_text("Resume this session with:") is None              # not a prompt line


def test_live_tui_backstop():
    assert cli._pane_shows_live_tui(LIVE_TUI) is True
    assert cli._pane_shows_live_tui(HUMAN_SHELL) is False
    assert cli._pane_shows_live_tui(EXITED_HUSK) is False


TREE = """window window:1 9FBB70C6-7B17-4DA5-B54D-8FF3641D24E2 [current]
├── workspace workspace:11 995A1E62-5AFA-4A80-B724-F28640A64A63 "AD - Berg Sandbox"
│   └── pane pane:15 1202D373-1293-49AF-AC1D-0CD084A13C0F
│       └── surface surface:61 6A3321C5-29E4-4B2D-8DB0-6A4B1AD7D557 [terminal] "…/conductors" tty=ttys016
├── workspace workspace:42 51F24BC4-C6B1-4833-A7F4-EE984AC4EB9C "prior-art-intel"
│   └── pane pane:56 51DC6608-B4BD-4361-B092-FE23EDDA9CD6
│       ├── surface surface:160 0DE03CC9-0D6D-49FC-90FF-19CCEB132A8C [browser] "some page"
│       └── surface surface:222 72C89319-2D50-4A6E-BD8C-0FAD80F69F6F [terminal] "…/research-agent" tty=ttys013"""


def test_iter_terminal_surfaces_parses_tree():
    got = list(cli._iter_terminal_surfaces(TREE))
    assert ("6A3321C5-29E4-4B2D-8DB0-6A4B1AD7D557", "995A1E62-5AFA-4A80-B724-F28640A64A63", "…/conductors") in got
    # the browser surface is excluded; the terminal under prior-art-intel is attributed to that workspace
    assert ("72C89319-2D50-4A6E-BD8C-0FAD80F69F6F", "51F24BC4-C6B1-4833-A7F4-EE984AC4EB9C", "…/research-agent") in got
    assert all("[browser]" not in t for _, _, t in got) and len(got) == 2
