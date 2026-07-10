# tests/test_move_harden.py — the move-harden pass (2026-07-07): `fleet move` (atomic relocate) and
# `fleet group init|add` (one-conductor-one-group). cmux shell-outs are faked via a capturing cmuxq;
# the tree/group resolvers are monkeypatched so the units never touch a live cmux. Registry side runs
# against the throwaway $CMUX_STATE_DIR. Companion to the router move-vs-close tests in test_router.py.
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from cmux_fleet import cli as fleet  # noqa: E402
from cmux_fleet import state as fs   # noqa: E402


def _seq(*vals):
    """A stand-in for current_ws_for_surface: returns vals[0], vals[1], ... on successive calls."""
    it = iter(vals)
    return lambda *a, **k: next(it)


# =============================== fleet group init / add =========================================

def test_group_init_bootstraps_and_records(fs, monkeypatch):
    # group ABSENT -> create --from MY ws, set-anchor MY ws, close the empty scaffold anchor, record group.
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "surface": "COND-S",
                         "workspace": "WS-COND", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "current_ws_for_surface", lambda surf: "WS-COND")
    grefs = iter(["", "workspace_group:7"])                 # absent on first check, present after create
    monkeypatch.setattr(fleet, "_group_ref", lambda g: next(grefs))
    wsets = iter([{"WS-COND"}, {"WS-COND", "WS-SCAFFOLD"}])  # a NEW empty anchor appears after create
    monkeypatch.setattr(fleet, "_all_workspace_uuids", lambda txt: next(wsets))
    monkeypatch.setattr(fleet, "_term_surface_in", lambda ws, pane=None: "")   # scaffold is EMPTY

    assert fleet.cmd_group(["init", "--name", "AD - Berg Sandbox", "--surface", "COND-S"]) == 0

    create = [c for c in calls if c[:2] == ("workspace-group", "create")][0]
    assert "--from" in create and "WS-COND" in create and "AD - Berg Sandbox" in create
    setanchor = [c for c in calls if c[:2] == ("workspace-group", "set-anchor")][0]
    assert "WS-COND" in setanchor and "workspace_group:7" in setanchor
    assert ("close-workspace", "--workspace", "WS-SCAFFOLD") in calls   # empty scaffold reaped
    assert fs.live_get("cond")["group"] == "AD - Berg Sandbox"          # recorded in the registry
    assert fs.live_get("cond")["place"] == "workspace"


# The scaffold cmux spawns for a new group is NOT "provably empty": `workspace-group create --name N
# --from <ref>` builds a whole new workspace named N with a bare login shell in it, anchors the group
# there, and adopts <ref> as a member (measured on cmux 0.64.17, 2026-07-10). The old emptiness test
# (_term_surface_in) therefore never fired, and every `fleet launch --place workspace --group <new>` left
# a workspace named after the group sitting in the sidebar. _close_group_scaffold tests what actually
# matters -- does any surface in it hold a live agent or a registered seat -- so these two tests feed it a
# REAL `cmux tree` and a REAL `ps axeww` table rather than stubbing the question away.

_WS_COND = "85576ACB-D5B9-4817-9A71-3FEBB54BC9EA"
_WS_NEW = "F008C803-63B1-420C-8FAE-480787E454E1"
_S_COND = "DCCA9A19-4F0C-4C22-9E5B-1C4C1A3F60B1"
_S_NEW = "B941BC13-7380-4596-B58C-E0EB20B463EA"


def _tree_with_scaffold(new_title="Terminal"):
    return (f'window window:1 9FBB70C6-7B17-4DA5-B54D-8FF3641D24E2 [current] ◀ active\n'
            f'├── workspace workspace:7 {_WS_COND} "Conductor - cmux-advisor" [selected]\n'
            f'│   └── pane pane:9 00B68660-784E-4838-BB90-0C37093FB39D [focused]\n'
            f'│       └── surface surface:12 {_S_COND} [terminal] "✳ Claude Code" [selected]\n'
            f'├── workspace workspace:37 {_WS_NEW} "cond"\n'
            f'│   └── pane pane:51 1BE4BFB8-33CD-4131-BA64-FA3508A3AAF1 [focused]\n'
            f'│       └── surface surface:155 {_S_NEW} [terminal] "{new_title}" [selected]\n')


def _init_with(monkeypatch, calls, tree):
    monkeypatch.setattr(fleet, "cmuxq",
                        lambda *a: (calls.append(a) or (tree if a[:1] == ("tree",) else "")))
    monkeypatch.setattr(fleet, "current_ws_for_surface", lambda surf: _WS_COND)
    monkeypatch.setattr(fleet, "_group_ref", _seq("", "workspace_group:7"))


def test_group_init_reaps_the_bare_shell_scaffold_cmux_spawns(fs, monkeypatch):
    """The scaffold holds a login shell, not nothing. It is still reapable: it did not exist a moment ago
    (the before/after tree diff brackets the `create`), and no surface in it holds a live agent."""
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "surface": _S_COND,
                         "workspace": _WS_COND, "status": "live"})
    calls = []
    _init_with(monkeypatch, calls, _tree_with_scaffold())
    # before-set: only the conductor's ws; after-set: the real tree above (both through the REAL parser)
    monkeypatch.setattr(fleet, "_all_workspace_uuids",
                        _seq({_WS_COND}, {_WS_COND, _WS_NEW}))
    assert fleet.cmd_group(["init", "--surface", _S_COND]) == 0
    assert ("close-workspace", "--workspace", _WS_NEW) in calls        # the scaffold WORKSPACE, not a surface
    assert fs.live_get("cond")["group"] == "cond"                      # name defaulted to the label


def test_group_init_never_closes_a_new_workspace_holding_a_live_agent(fs, monkeypatch):
    """SAFETY, restated against the check that replaced 'provably empty': a brand-new workspace that
    somehow holds a LIVE agent is reported and left alone. Driven through the real `ps axeww` parse --
    the conftest blanks that sweep, which is exactly the blindness that lets a teardown bug ship green."""
    from cmux_fleet import resolve as rs
    AGENT = 71001
    ps_table = (f"  PID   TT  STAT      TIME COMMAND\n"
                f"{AGENT} s001  S+   0:05.00 /Users/berg/.local/bin/claude "
                f"CMUX_SURFACE_ID={_S_NEW} CMUX_CLAUDE_PID={AGENT}\n")
    monkeypatch.setattr(rs, "_ps_axeww", lambda: ps_table)
    monkeypatch.setattr(rs.fs, "pid_alive", lambda pid: pid == AGENT)
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "surface": _S_COND,
                         "workspace": _WS_COND, "status": "live"})
    calls = []
    _init_with(monkeypatch, calls, _tree_with_scaffold(new_title="✳ someone else's agent"))
    monkeypatch.setattr(fleet, "_all_workspace_uuids", _seq({_WS_COND}, {_WS_COND, _WS_NEW}))
    assert fleet.cmd_group(["init", "--surface", _S_COND]) == 0
    assert not [c for c in calls if c[0] == "close-workspace"]          # nothing closed
    assert fs.live_get("cond")["group"] == "cond"


def test_group_init_never_closes_a_new_workspace_holding_a_registered_seat(fs, monkeypatch):
    """The other half of the floor: a registered agent's surface blocks the close even with no live pid
    (a wedged agent still owns its seat)."""
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "surface": _S_COND,
                         "workspace": _WS_COND, "status": "live"})
    fs.live_put("someone", {"role": "w", "kind": "child", "tool": "claude", "surface": _S_NEW,
                            "workspace": _WS_NEW, "status": "live"})
    calls = []
    _init_with(monkeypatch, calls, _tree_with_scaffold())
    monkeypatch.setattr(fleet, "_all_workspace_uuids", _seq({_WS_COND}, {_WS_COND, _WS_NEW}))
    assert fleet.cmd_group(["init", "--surface", _S_COND]) == 0
    assert not [c for c in calls if c[0] == "close-workspace"]


def test_group_init_existing_group_just_records(fs, monkeypatch):
    # group ALREADY exists -> record it on the conductor, no create/set-anchor/close.
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "surface": "COND-S",
                         "workspace": "WS-COND", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "current_ws_for_surface", lambda surf: "WS-COND")
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:3")   # already exists

    assert fleet.cmd_group(["init", "--name", "grp", "--surface", "COND-S"]) == 0
    assert not [c for c in calls if c[:2] == ("workspace-group", "create")]   # did NOT recreate
    assert fs.live_get("cond")["group"] == "grp"


def test_group_add_retrofits_child_without_moving_surface(fs, monkeypatch):
    # `group add`: the SAFE lane -- workspace-group add (no surface move), child stays live, group recorded.
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "surface": "COND-S",
                         "workspace": "WS-COND", "group": "grp", "status": "live"})
    fs.live_put("kid", {"role": "w", "kind": "child", "tool": "claude", "parent": "cond",
                        "surface": "KID-S", "workspace": "WS-KID", "place": "tab", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:4")
    monkeypatch.setattr(fleet, "current_ws_for_surface", lambda surf: "WS-KID")

    assert fleet.cmd_group(["add", "kid", "--surface", "COND-S"]) == 0
    add = [c for c in calls if c[:2] == ("workspace-group", "add")][0]
    assert "workspace_group:4" in add and "WS-KID" in add
    assert not [c for c in calls if c[0] in ("move-surface", "move-tab-to-new-workspace")]  # NO move
    kid = fs.live_get("kid")
    assert kid["group"] == "grp" and kid["place"] == "workspace"       # child row now claims the group


def test_group_add_refuses_without_conductor_group(fs, monkeypatch):
    fs.live_put("cond", {"role": "c", "kind": "conductor", "surface": "COND-S", "status": "live"})  # no group
    fs.live_put("kid", {"role": "w", "kind": "child", "parent": "cond", "surface": "KID-S",
                        "status": "live"})
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: "")
    with pytest.raises(SystemExit):
        fleet.cmd_group(["add", "kid", "--surface", "COND-S"])         # run `group init` first


# =============================== fleet move ====================================================

def test_move_to_workspace_reconciles_registry(fs, monkeypatch):
    fs.live_put("kid", {"role": "w", "kind": "child", "tool": "claude", "parent": "cond",
                        "surface": "KID-S", "workspace": "WS-OLD", "place": "tab",
                        "session": "claude-k", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "current_ws_for_surface",
                        _seq("WS-OLD", "22222222-2222-2222-2222-222222222222"))  # cur, then new
    monkeypatch.setattr(fs, "surface_has_live_agent", lambda s: True)

    rc = fleet.cmd_move(["kid", "--to-workspace", "22222222-2222-2222-2222-222222222222"])
    assert rc == 0
    mv = [c for c in calls if c[0] == "move-surface"][0]
    assert "KID-S" in mv and "22222222-2222-2222-2222-222222222222" in mv
    kid = fs.live_get("kid")
    assert kid["workspace"] == "22222222-2222-2222-2222-222222222222"  # reconciled from tree ground truth
    assert kid["place"] == "workspace"
    assert kid["surface"] == "KID-S" and kid["session"] == "claude-k"  # surface + session UNCHANGED
    # the expected-close tombstone was stamped BEFORE the move (router archive-suppression belt)
    assert fs.expected_close_recent("KID-S")


def test_move_own_workspace_joins_conductor_group(fs, monkeypatch):
    fs.live_put("cond", {"role": "c", "kind": "conductor", "surface": "COND-S", "group": "G",
                         "status": "live"})
    fs.live_put("kid", {"role": "w", "kind": "child", "tool": "claude", "parent": "cond",
                        "surface": "KID-S", "workspace": "WS-OLD", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "current_ws_for_surface", _seq("WS-OLD", "WS-NEW", "WS-NEW"))
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:5")
    monkeypatch.setattr(fs, "surface_has_live_agent", lambda s: True)

    assert fleet.cmd_move(["kid", "--own-workspace"]) == 0
    assert [c for c in calls if c[0] == "move-tab-to-new-workspace"]   # fresh workspace
    add = [c for c in calls if c[:2] == ("workspace-group", "add")][0]
    assert "workspace_group:5" in add and "WS-NEW" in add             # joined the conductor's group
    kid = fs.live_get("kid")
    assert kid["workspace"] == "WS-NEW" and kid["group"] == "G" and kid["place"] == "workspace"


def test_move_refuses_when_surface_not_in_tree(fs, monkeypatch):
    fs.live_put("kid", {"role": "w", "kind": "child", "surface": "KID-S", "workspace": "WS-OLD",
                        "status": "live"})
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: "")
    monkeypatch.setattr(fleet, "current_ws_for_surface", lambda s: "")   # surface GONE from the tree
    with pytest.raises(SystemExit):
        fleet.cmd_move(["kid", "--own-workspace"])                       # -> revive, not move


def test_move_requires_exactly_one_target(fs, monkeypatch):
    fs.live_put("kid", {"role": "w", "kind": "child", "surface": "KID-S", "status": "live"})
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: "")
    with pytest.raises(SystemExit):
        fleet.cmd_move(["kid"])                                          # neither flag
    with pytest.raises(SystemExit):
        fleet.cmd_move(["kid", "--own-workspace", "--to-workspace", "WS"])  # both


def test_move_noop_when_already_in_target(fs, monkeypatch):
    fs.live_put("kid", {"role": "w", "kind": "child", "surface": "KID-S",
                        "workspace": "11111111-1111-1111-1111-111111111111", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "current_ws_for_surface",
                        lambda s: "11111111-1111-1111-1111-111111111111")
    assert fleet.cmd_move(["kid", "--to-workspace", "11111111-1111-1111-1111-111111111111"]) == 0
    assert not [c for c in calls if c[0] == "move-surface"]              # nothing moved
    assert not fs.expected_close_recent("KID-S")                        # and no tombstone stamped


# ================= P0-3: tool-aware launch flags (claude-isms must not kill codex) ==================
# The 2026-07-07 incident: `--effort` (a claude flag) was forwarded VERBATIM to codex, which aborted on
# "unexpected argument '--effort' found". _codex_flags is the ONE place that owns claude->codex flag
# translation at the adapter boundary. These are pure units on the token mapping + adapter wiring.

def test_codex_flags_translates_effort_to_config_override():
    assert fleet._codex_flags(["--effort", "high"]) == ["-c", "model_reasoning_effort=high"]
    assert fleet._codex_flags(["--effort", "low"]) == ["-c", "model_reasoning_effort=low"]


def test_codex_flags_passes_the_effort_LEVEL_through_verbatim():
    # The LEVEL is passed through, not clamped: codex's tiers overlap the fleet's (its TUI shows xhigh),
    # so clamping would SILENTLY DOWNGRADE reasoning. A value codex rejects fails LOUD via the P0-4a
    # launch verify instead. Inline (--effort=X) and separate (--effort X) forms both work.
    assert fleet._codex_flags(["--effort", "xhigh"]) == ["-c", "model_reasoning_effort=xhigh"]
    assert fleet._codex_flags(["--effort=max"]) == ["-c", "model_reasoning_effort=max"]


def test_codex_flags_translates_dangerous_bypass():
    assert fleet._codex_flags(["--dangerously-skip-permissions"]) \
        == ["--dangerously-bypass-approvals-and-sandbox"]


def test_codex_flags_drops_claude_only_flags_with_values():
    # --setting-sources / --permission-mode / --plugin-dir have no codex analog -> drop (consume value).
    assert fleet._codex_flags(["--setting-sources", "user,project", "--model", "gpt-5-codex"]) \
        == ["--model", "gpt-5-codex"]                                    # setting-sources gone, model kept
    assert fleet._codex_flags(["--permission-mode", "plan"]) == []
    assert fleet._codex_flags(["--plugin-dir", "/x/p"]) == []


def test_codex_flags_passes_codex_native_flags_through():
    # a codex floor's own flags (and any -- passthrough that's already codex-shaped) are untouched.
    assert fleet._codex_flags(["--effort", "high", "-c", "sandbox=danger", "--search"]) \
        == ["-c", "model_reasoning_effort=high", "-c", "sandbox=danger", "--search"]


def test_codex_flags_effort_as_last_bare_token_is_dropped_not_crashed():
    assert fleet._codex_flags(["--effort"]) == []                       # no value -> emit nothing, no IndexError


def test_adapter_compile_codex_translates_effort_and_claude_stays_verbatim():
    spec = {"tool": "codex", "role": "w", "label": "w", "flags": [], "env": {},
            "plugins": [], "settings": "", "setting_sources": ""}
    binn, args, _ = fleet.adapter_compile("codex", spec, ["--effort", "high"])
    assert binn == "codex"
    assert "--effort" not in args and "-c" in args and "model_reasoning_effort=high" in args
    # the SAME caller tokens on a claude spec are forwarded AS-IS (claude owns --effort natively).
    cspec = dict(spec, tool="claude")
    _, cargs, _ = fleet.adapter_compile("claude", cspec, ["--effort", "high"])
    assert "--effort" in cargs and "high" in cargs                      # claude path unchanged (regression guard)


# ================= P0-4a: launch verification (a dead-on-arrival lazy child != DONE) ================
# launch_error_line is the PURE scanner (shared with the router never-bound sweep) that tells a launch
# that DIED on spawn (a bad flag / missing binary / crash) from a healthy agent TUI. _launch_failure_line
# wires it to a live pane via cmuxq. cmd_launch uses these to FAIL LOUD instead of a false "DONE".
#
# FIXTURE SHAPE MATTERS (the same lesson as the two-column `ps` fixture that hid the codex argv0 bug).
# The original fixtures here were bare error strings with no launch line. That is not what a fleet pane
# looks like, and the difference is the whole bug: a real pane opens with the LOGIN SHELL's rc chatter,
# and this box's rc chatter contains "no such file or directory" -- so the scanner cried LAUNCH FAILED on
# every healthy codex launch. Every fixture below is a real pane: shell noise, the prompt, the echoed
# `render_send_cmd` line with its AGENT_ROLE=/AGENT_LABEL= env prefix, then the tool's own output.
# `_ZSHRC_NOISE` is verbatim from this box; `_BAD_FLAG` is verbatim from `codex --effort high` (0.144.1).

_ZSHRC_NOISE = "/Users/berg/.zshrc:.:65: no such file or directory: /Users/berg/.local/bin/env"
_PROMPT = "berg@Seans-MacBook-Pro cmux-fleet % "
_LAUNCH_LINE = ("cd /Users/berg/tapestry/_meta/agents/probe && AGENT_ROLE=probe AGENT_LABEL=probe "
                "CMUX_FLEET_STATE_DIR=/Users/berg/.local/state/cmux-fleet codex --effort high")
_BAD_FLAG = ("error: unexpected argument '--effort' found\n"
             "\n  tip: to pass '--effort' as a value, use '-- --effort'\n"
             "\nUsage: codex [OPTIONS] [PROMPT]\n"
             "       codex [OPTIONS] <COMMAND> [ARGS]\n"
             "\nFor more information, try '--help'.")


def _pane(*after_launch):
    """A real fleet pane: rc noise ABOVE the launch line, the tool's output BELOW it."""
    return "\n".join([_ZSHRC_NOISE, _PROMPT + _LAUNCH_LINE, *after_launch])


def _live_codex_pane():
    """The REAL pane of a healthy codex 0.144.1 launched by exec delivery, captured from this box on
    2026-07-10 (`cmux capture-pane`). It contains three `⚠ MCP client ... failed to start` blocks -- one
    of which reads `No such file or directory (os error 2)` -- above a perfectly live TUI. It carries NO
    fleet launch line, because respawn-pane runs the launch AS the pane's process and nothing is echoed."""
    with open(os.path.join(HERE, "fixtures", "pane-codex-live-exec.txt")) as f:
        return f.read()


def test_launch_error_line_catches_the_bad_flag_death():
    assert "unexpected argument" in fleet.launch_error_line(_pane(_BAD_FLAG, _PROMPT))


def test_launch_error_line_catches_missing_binary():
    assert "command not found" in fleet.launch_error_line(_pane("zsh: command not found: codex"))


def test_launch_error_line_ignores_shell_rc_noise_above_the_launch_line():
    """Cry-wolf, PASTE delivery. ~/.zshrc:65 sources a file uv never created, so every surface on this box
    opens with a line matching the "No such file or directory" marker. It is the login shell's, printed
    before the launch command was ever injected. A DEAD launch below the line must still report the tool's
    OWN error, not the shell's."""
    dead = _pane(_BAD_FLAG)
    assert "unexpected argument" in fleet.launch_error_line(dead)
    assert "zshrc" not in fleet.launch_error_line(dead)
    # ...and with no launch line and no TUI, the noise alone is all there is to report (exec delivery has
    # no shell, so a bare-noise pane can only come from a shell that never ran the launch).
    assert "no such file or directory" in fleet.launch_error_line(_ZSHRC_NOISE).lower()


def test_launch_error_line_is_quiet_on_a_REAL_live_codex_pane():
    """THE cry-wolf bug, exec delivery, against the captured pane. A healthy codex whose MCP servers
    failed to start printed "No such file or directory (os error 2)"; `fleet launch` called that a dead
    process and printed a cleanup recipe. The agent was fine, and it is fine here."""
    pane = _live_codex_pane()
    assert "No such file or directory" in pane                      # the trap is really in the fixture
    assert not fleet._FLEET_LAUNCH_SIG.search(pane)                 # ...and exec delivery echoes no launch line
    assert fleet.agent_tui_visible(pane) is True                    # the TUI is painted -> a LIVE agent
    assert fleet.launch_error_line(pane) == ""


def test_agent_tui_visible_recognises_codex(monkeypatch):
    """codex paints none of claude's markers, so `_agent_surfaced` was permanently False for it: the
    enter-race loop would re-kick Enter into a live codex TUI, and the husk reaper's TUI backstop was
    blind to codex entirely."""
    pane = _live_codex_pane()
    assert not any(m in pane for m in fleet._TUI_MARKERS)           # zero claude-isms on a live codex
    assert fleet.agent_tui_visible(pane) is True
    assert fleet._pane_shows_live_tui(pane) is True                 # the husk reaper's backstop, too
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: pane)
    assert fleet._agent_surfaced("SURF") is True                    # -> stop kicking Enter into it
    assert fleet.agent_tui_visible(_pane("zsh: command not found: codex")) is False


def test_launch_error_line_still_catches_a_dead_exec_launch():
    """The other half: exec delivery, no launch line, no TUI -> scan the whole pane. Removing the
    launch-line requirement is what keeps the P0-4a protection alive on the exec path."""
    dead = "error: unexpected argument '--effort' found\n\nUsage: codex [OPTIONS] [PROMPT]\n"
    assert not fleet._FLEET_LAUNCH_SIG.search(dead)
    assert "unexpected argument" in fleet.launch_error_line(dead)


def test_launch_error_line_scans_below_the_LAST_launch_line():
    """A re-kicked Enter echoes the launch line twice (the paste-settle race). The error that matters is
    the one below the attempt that actually ran."""
    pane = _pane("zsh: command not found: codex", _PROMPT + _LAUNCH_LINE, _BAD_FLAG)
    assert "unexpected argument" in fleet.launch_error_line(pane)


def test_launch_error_line_is_quiet_on_a_healthy_tui():
    # a booted agent shows its chrome, NOT a CLI error -> no false failure verdict.
    healthy = _pane("  Context Remaining: 100%   ? for shortcuts   esc to interrupt", "> ")
    assert fleet.launch_error_line(healthy) == ""
    assert fleet.launch_error_line("") == ""


def test_launch_failure_line_reads_the_pane_via_cmuxq(monkeypatch):
    pane = _pane(_BAD_FLAG)
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: pane if a[:1] == ("capture-pane",) else "")
    assert "unexpected argument" in fleet._launch_failure_line("SURF")
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: _live_codex_pane())
    assert fleet._launch_failure_line("SURF") == ""


# --- the codex update modal: the backstop to _codex_update_preflight -------------------------------
# Strings lifted from the codex 0.144.1 binary. An out-of-date codex paints this INSTEAD of its TUI and
# waits; the seat never binds, and for a LAZY tool an unbound seat is the HEALTHY path -> `fleet launch`
# printed DONE over an agent wedged forever.

def test_codex_update_modal_is_seen_on_a_wedged_seat():
    pane = _pane("", "  Update available!", "  Update now (runs `codex update`)",
                 "  Skip until next version", "  Release notes: https://github.com/openai/codex/releases/latest")
    assert fleet.codex_update_modal(pane) is True
    assert fleet.launch_error_line(pane) == ""            # the modal is NOT a startup error -> distinct verdicts


def test_codex_update_modal_absent_on_a_healthy_codex_seat():
    assert fleet.codex_update_modal(_pane("  Codex  v0.144.1", "  ? for shortcuts", "> ")) is False
    assert fleet.codex_update_modal("") is False


def test_codex_update_note_is_quiet_when_already_current():
    # verbatim `codex update` output on this box when current (rc 0) -> nothing worth printing
    out = ("Updating Codex via `brew upgrade --cask codex`...\n"
           "Warning: Not upgrading codex, the latest version is already installed\n"
           "\n🎉 Update ran successfully! Please restart Codex.\n")
    assert fleet.codex_update_note(0, out) == ""
    assert "updated codex" in fleet.codex_update_note(0, "Updating Codex...\n🎉 Update ran successfully!")


def test_codex_update_note_never_blocks_the_launch():
    # a timeout, a non-zero rc, and a missing binary all WARN and launch anyway (offline box).
    assert "timed out" in fleet.codex_update_note(None, "")
    assert "launching anyway" in fleet.codex_update_note(None, "")
    assert "launching anyway" in fleet.codex_update_note(1, "Error: network unreachable")


def test_codex_update_preflight_survives_a_missing_binary(monkeypatch):
    monkeypatch.setattr(fleet.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("codex")))
    assert "could not run" in fleet._codex_update_preflight()
