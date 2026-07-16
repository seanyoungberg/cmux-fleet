# tests/test_move_harden.py — the move-harden pass (2026-07-07): `fleet move` (atomic relocate) and
# `fleet group init|add` (one-conductor-one-group). cmux shell-outs are faked via a capturing cmuxq;
# the tree/group resolvers are monkeypatched so the units never touch a live cmux. Registry side runs
# against the throwaway $CMUX_STATE_DIR. Companion to the router move-vs-close tests in test_router.py.
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from cmux_fleet import cli as fleet     # noqa: E402
from cmux_fleet import state as fs      # noqa: E402


@pytest.fixture
def rs():
    """The in-process `resolve` module — imported INSIDE the fixture, and never bound at module import.

    WHY THIS IS A FIXTURE AND NOT AN IMPORT: tests/test_features.py pops every `cmux_fleet.*` entry out of
    sys.modules (and off the package object) so it can re-import them under a throwaway env. This file
    sorts AFTER it, so a module-level `from cmux_fleet import resolve as rs` would hold the module object
    from BEFORE that reset — a stale twin of the one `cli` actually imports at call time. Every
    monkeypatch onto it would land on a module nothing under test reads, and the guard would silently run
    against the REAL process table. (That is not a hypothetical: it is what these tests did on their first
    run — green alone, six failures in the full suite.) The conftest `fs` fixture imports inside itself
    for exactly this reason; this is its twin."""
    from cmux_fleet import resolve
    return resolve


def _seq(*vals):
    """A stand-in for current_ws_for_surface: returns vals[0], vals[1], ... on successive calls."""
    it = iter(vals)
    return lambda *a, **k: next(it)


# =============================== fleet group init / add =========================================

def _modelb_group_cmux(calls, gref="workspace_group:7", name="AD - Berg Sandbox",
                       anchor_ref="workspace:88", member_ref="workspace:2"):
    """A cmux where `workspace-group create --from <member>` has already produced a Model B group: the
    group's anchor is the FRESH scaffold cmux minted (anchor_ref, NOT <member>) and <member> is an
    ordinary member. This models the measured cmux 0.64.17 contract the old code compensated for."""
    def fake(*a):
        calls.append(a)
        if a[:3] == ("workspace-group", "list", "--json"):
            return json.dumps({"groups": [{"ref": gref, "name": name, "anchor_workspace_ref": anchor_ref,
                                           "member_workspace_refs": [anchor_ref, member_ref]}]})
        return ""
    return fake


def test_group_init_bootstraps_modelb_and_records(fs, monkeypatch):
    # group ABSENT -> create --from MY ws; Model B: KEEP the scaffold cmux mints as the empty anchor and
    # TITLE it 'Conductor - <label>'. My workspace stays an ordinary MEMBER -- NO set-anchor onto it, NO
    # close. Then record the group on the conductor's row.
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "surface": "COND-S",
                         "workspace": "WS-COND", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", _modelb_group_cmux(calls, member_ref="workspace:2"))
    monkeypatch.setattr(fleet, "current_ws_for_surface", lambda surf: "WS-COND")
    monkeypatch.setattr(fleet, "_group_ref", _seq("", "workspace_group:7"))   # absent, then present
    monkeypatch.setattr(fleet, "_ref_to_uuid",
                        lambda kind, ref, tree=None: {"workspace:88": "WS-SCAFFOLD",
                                                      "workspace:2": "WS-COND"}.get(ref, ""))

    assert fleet.cmd_group(["init", "--name", "AD - Berg Sandbox", "--surface", "COND-S"]) == 0

    create = [c for c in calls if c[:2] == ("workspace-group", "create")][0]
    assert "--from" in create and "WS-COND" in create and "AD - Berg Sandbox" in create
    # Model B: the scaffold is TITLED with the CONDUCTOR's label (not the group name) and KEPT...
    rename = [c for c in calls if c[0] == "rename-workspace"][0]
    assert "WS-SCAFFOLD" in rename and "Conductor - cond" in rename
    assert "WS-COND" not in rename                                      # the conductor's own ws is never retitled
    # ...and the conductor is NEVER re-anchored onto, and no scaffold is reaped.
    assert not [c for c in calls if c[:2] == ("workspace-group", "set-anchor")]
    assert not [c for c in calls if c[0] == "close-workspace"]
    assert fs.live_get("cond")["group"] == "AD - Berg Sandbox"         # recorded in the registry
    assert fs.live_get("cond")["place"] == "workspace"


def test_group_init_defaults_the_group_name_to_the_label(fs, monkeypatch):
    # no --name -> the group (and the 'Conductor - <label>' anchor title) default to the conductor's label.
    fs.live_put("cond", {"role": "c", "kind": "conductor", "tool": "claude", "surface": "COND-S",
                         "workspace": "WS-COND", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", _modelb_group_cmux(calls, name="cond"))
    monkeypatch.setattr(fleet, "current_ws_for_surface", lambda surf: "WS-COND")
    monkeypatch.setattr(fleet, "_group_ref", _seq("", "workspace_group:7"))
    monkeypatch.setattr(fleet, "_ref_to_uuid",
                        lambda kind, ref, tree=None: {"workspace:88": "WS-SCAFFOLD",
                                                      "workspace:2": "WS-COND"}.get(ref, ""))
    assert fleet.cmd_group(["init", "--surface", "COND-S"]) == 0
    create = [c for c in calls if c[:2] == ("workspace-group", "create")][0]
    assert "cond" in create                                            # group name defaulted to the label
    rename = [c for c in calls if c[0] == "rename-workspace"][0]
    assert "Conductor - cond" in rename
    assert fs.live_get("cond")["group"] == "cond"


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
# THE CUTOVER (redesign build #1, 2026-07-16): `fleet move` relocates a LIVE agent NATIVELY. cmux 0.64.18+
# heals the moved surface's agent-status registration (the live-identity healing upsert), so the old
# "moving a live surface permanently darkens it" hazard is GONE — and with it the live-agent REFUSAL and the
# `--archive-revive` (park + revive onto a fresh surface) workaround. A move is now a SURFACE MOVE + a
# REGISTRY UPDATE that PRESERVES the agent's pid / session / context / surface-UUID and — the split-brain
# the old archive-revive path caused (cf-conductor, 2026-07-15) — its PARENT and GROUP.
TARGET_WS = "22222222-2222-2222-2222-222222222222"
SIB_WS = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def movable(fs, rs, monkeypatch):
    """A live tab child 'kid' under conductor 'cond', an empty hook store, and a cmux that records calls."""
    fs.live_put("cond", {"role": "c", "kind": "conductor", "surface": "COND-S", "workspace": "WS-OLD",
                         "status": "live"})
    fs.live_put("kid", {"role": "w", "kind": "child", "tool": "claude", "parent": "cond", "cwd": "/x",
                        "surface": "KID-S", "workspace": "WS-OLD", "place": "tab", "session": "claude-k",
                        "plugins": [], "flags": [], "settings": "", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "_store", lambda: {"sessions": {}})
    monkeypatch.setattr(fleet, "current_ws_for_surface", lambda s: "WS-OLD")
    monkeypatch.setattr(rs, "workspace_surfaces", lambda ws, ws_map=None: ["SIB-S"])
    return calls


def _moves(calls, verb):
    return [c for c in calls if c and c[0] == verb]


# --- the native relocation: a LIVE agent moves (no refusal, no archive-revive) ----------------------

def test_move_to_workspace_relocates_a_LIVE_agent_natively(fs, movable, monkeypatch):
    """A live agent is NO LONGER refused — the surface is moved natively and the registry updated. cmux
    0.64.18+ heals the moved surface, so no archive, no revive, no fresh surface, no registry churn."""
    monkeypatch.setattr(fleet, "current_ws_for_surface", _seq("WS-OLD", TARGET_WS))
    assert fleet.cmd_move(["kid", "--to-workspace", TARGET_WS]) == 0
    mv = _moves(movable, "move-surface")
    assert mv and "KID-S" in mv[0] and TARGET_WS in mv[0]        # the surface itself moved (UUID preserved)
    assert not _moves(movable, "move-tab-to-new-workspace")
    kid = fs.live_get("kid")
    assert kid["workspace"] == TARGET_WS and kid["surface"] == "KID-S"   # same surface, new workspace
    assert kid["parent"] == "cond"                              # PARENT PRESERVED (the split-brain fix)
    assert kid["place"] == "tab"
    assert kid["session"] == "claude-k"                         # session/context untouched
    assert fs.expected_close_recent("KID-S")                    # router archive-suppression belt


def test_move_own_workspace_regroups_into_the_conductors_group_surface_preserving(fs, movable, monkeypatch):
    """`--own-workspace`: the surface moves to a FRESH workspace, which is then regrouped into the
    conductor's group via `workspace-group add` (surface-preserving — NOT a surface move). This is the
    exact native regroup that repairs the split-brain the old archive-revive path caused."""
    fs.live_put("cond", {**fs.live_get("cond"), "group": "G"})
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:7" if g == "G" else "")
    monkeypatch.setattr(fleet, "current_ws_for_surface", _seq("WS-OLD", "NEW-WS", "NEW-WS"))
    assert fleet.cmd_move(["kid", "--own-workspace"]) == 0
    assert _moves(movable, "move-tab-to-new-workspace")
    ga = [c for c in movable if c[:2] == ("workspace-group", "add")]
    assert ga and "workspace_group:7" in ga[0] and "NEW-WS" in ga[0]     # regrouped, surface-preserving
    kid = fs.live_get("kid")
    assert kid["parent"] == "cond" and kid["group"] == "G"      # PARENT + GROUP both preserved
    assert kid["place"] == "workspace"


def test_move_does_NOT_archive_or_revive_a_live_agent(fs, movable, monkeypatch):
    """The whole point of the cutover: a live relocation never parks the agent or spins a fresh surface."""
    monkeypatch.setattr(fleet, "cmd_archive", lambda argv: pytest.fail("move must NOT archive a live agent"))
    monkeypatch.setattr(fleet, "cmd_revive", lambda argv: pytest.fail("move must NOT revive a live agent"))
    monkeypatch.setattr(fleet, "current_ws_for_surface", _seq("WS-OLD", TARGET_WS))
    assert fleet.cmd_move(["kid", "--to-workspace", TARGET_WS]) == 0
    assert fs.archive_get("kid") is None and fs.live_get("kid") is not None   # never parked


def test_move_REHOMES_under_the_caller_conductor(fs, movable, monkeypatch):
    """Running `fleet move <child>` FROM a conductor re-parents the child to that conductor — the rehome
    the cf-conductor incident needed. The registry parent + group follow the mover, set TOGETHER (no
    archive, no revive, no split-brain to parent=None/group=None)."""
    fs.live_put("cf", {"role": "c", "kind": "conductor", "surface": "CF-S", "workspace": "CF-WS",
                       "group": "CFG", "status": "live"})
    monkeypatch.setenv("CMUX_SURFACE_ID", "CF-S")               # the caller running move IS conductor cf
    monkeypatch.setattr(fleet, "_group_ref", lambda g: "workspace_group:9" if g == "CFG" else "")
    monkeypatch.setattr(fleet, "current_ws_for_surface", _seq("WS-OLD", "NEW-WS", "NEW-WS"))
    assert fleet.cmd_move(["kid", "--own-workspace"]) == 0
    kid = fs.live_get("kid")
    assert kid["parent"] == "cf"                                # RE-PARENTED to the mover (was 'cond')
    assert kid["group"] == "CFG"                                # regrouped into cf's group
    ga = [c for c in movable if c[:2] == ("workspace-group", "add")]
    assert ga and "workspace_group:9" in ga[0] and "NEW-WS" in ga[0]   # cf's group, surface-preserving


# --- the ARCHIVED agent: no surface, so revive it into the target (never move) -----------------------

def test_move_of_an_ARCHIVED_label_refuses_and_signposts_revive(fs, monkeypatch):
    """An archived agent has NO surface — nothing to relocate. Refuse, and hand over the revive command
    that expresses what they actually want (bring it back INTO the target)."""
    fs.archive_put("kid", {"role": "w", "tool": "claude", "cwd": "/x", "last_session": "S1"})
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: "")
    with pytest.raises(SystemExit) as ex:
        fleet.cmd_move(["kid", "--own-workspace"])
    assert "ARCHIVED" in str(ex.value) and "no surface" in str(ex.value)
    assert "fleet revive kid" in str(ex.value)
    assert "--archive-revive" not in str(ex.value)              # the dead flag is never suggested


# --- D. argument handling (unchanged contracts) -------------------------------------------------------

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


# --- E. the liveness authority itself ------------------------------------------------------------------

def _ps_table(*rows):
    """A real-shaped `ps axeww` sweep. Columns are PID TT STAT TIME COMMAND...env. Each row is
    (pid, surface, claude_pid); `claude_pid == pid` marks THE seat agent (the cmux wrapper exports
    CMUX_CLAUDE_PID=$$ then execs claude, so only the agent's own pid matches), and any other value is a
    mere env-carrier (daemon, router, the `claude -p` summarizer) that must NOT read as an agent."""
    out = ["  PID   TT  STAT      TIME COMMAND"]
    for pid, surf, cpid in rows:
        out.append(f"{pid} s001  S+     0:05.00 /Users/berg/.local/bin/claude --resume abc "
                   f"CMUX_SURFACE_ID={surf} CMUX_WORKSPACE_ID=WS-OLD CMUX_CLAUDE_PID={cpid}")
    return "\n".join(out) + "\n"


def test_live_agent_pids_unions_the_store_and_the_process_table(fs, rs, monkeypatch):
    """Neither source is sufficient alone. The STORE misses an agent whose record SessionEnd already
    reaped (~0.3s before the process exits) or never wrote; the PROCESS TABLE needs the seat-agent rule to
    tell the agent from its env-inheriting children. The union is the authority both the teardown gate and
    the move refusal spend, so they can never disagree about who is alive."""
    monkeypatch.setattr(fleet, "_store",
                        lambda: {"sessions": {"s1": {"surfaceId": "KID-S", "pid": 111,
                                                     "agentLifecycle": "running"}}})
    monkeypatch.setattr(rs, "_ps_axeww", lambda: _ps_table((222, "KID-S", 222),      # the agent, per ps
                                                           (333, "KID-S", 222)))     # its summarizer -> NOT
    monkeypatch.setattr(rs.fs, "pid_alive", lambda p: True)
    assert fleet._live_agent_pids("KID-S", "claude") == [111, 222]       # store ∪ ps-seat-agent; 333 excluded


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


def test_adapter_compile_codex_translates_effort_and_claude_stays_verbatim(monkeypatch):
    # pin the clean-config prefix so the assertion is hermetic (not coupled to the real ~/.codex/config.toml)
    monkeypatch.setattr(fleet, "_codex_clean_config_flags", lambda *a, **k: ["--disable", "plugins"])
    spec = {"tool": "codex", "role": "w", "label": "w", "flags": [], "env": {},
            "plugins": [], "settings": "", "setting_sources": ""}
    binn, args, _ = fleet.adapter_compile("codex", spec, ["--effort", "high"])
    assert binn == "codex"
    assert "--effort" not in args and "-c" in args and "model_reasoning_effort=high" in args
    assert args[:2] == ["--disable", "plugins"]                         # clean-config prefix leads the argv
    # the SAME caller tokens on a claude spec are forwarded AS-IS (claude owns --effort natively).
    cspec = dict(spec, tool="claude")
    _, cargs, _ = fleet.adapter_compile("claude", cspec, ["--effort", "high"])
    assert "--effort" in cargs and "high" in cargs                      # claude path unchanged (regression guard)
    assert "--disable" not in cargs                                     # clean-config is codex-only


def test_codex_config_mcp_servers_parses_top_level_names(tmp_path):
    cfg = ("model = \"gpt-5.5\"\n"
           "[mcp_servers.context7]\ncommand = \"npx\"\n"
           "[mcp_servers.gemini-cli]\ncommand = \"npx\"\n"
           "[mcp_servers.node_repl]\ncommand = \"x\"\n"
           "[mcp_servers.node_repl.env]\nFOO = \"bar\"\n"          # a SUBTABLE -> still just node_repl
           "[mcp_servers.basic-memory]\nurl = \"http://127.0.0.1:8000/mcp\"\n"
           "[model_providers.berglabs]\nname = \"OpenAI\"\n")     # NOT an mcp_server -> excluded
    assert fleet._codex_config_mcp_servers(cfg) == ["basic-memory", "context7", "gemini-cli", "node_repl"]


def _codex_home(tmp_path, name, servers=()):
    """A codex HOME (the dir), not a config path — that distinction IS the bug below."""
    h = tmp_path / name
    h.mkdir()
    if servers:
        h.joinpath("config.toml").write_text(
            "".join(f"[mcp_servers.{s}]\ncommand=\"npx\"\n" for s in servers))
    return h


def test_codex_clean_config_flags_disables_plugins_and_each_server_IN_THAT_HOME(tmp_path):
    # the arg is the HOME the launch will actually run in; the servers are read from ITS config.toml.
    home = _codex_home(tmp_path, "dirty", ["context7", "terraform"])
    flags = fleet._codex_clean_config_flags(str(home))
    assert flags == ["--disable", "plugins",
                     "-c", "mcp_servers.context7.enabled=false",
                     "-c", "mcp_servers.terraform.enabled=false"]


def test_codex_clean_config_flags_missing_config_is_plugins_only(tmp_path):
    # a home that exists but has never been configured (a fresh seat) -> nothing to strip
    assert fleet._codex_clean_config_flags(str(_codex_home(tmp_path, "fresh"))) == ["--disable", "plugins"]


def test_codex_clean_config_flags_enumerate_the_SEAT_home_NOT_the_desktop(tmp_path):
    """THE agent-launch bug (2026-07-12), pinned. The flags used to be enumerated from Berg's DESKTOP
    ~/.codex (6 MCP servers) and then applied to a SEAT's home, which declares none. `enabled=false` on a
    server the seat home never declared CREATES a transport-less `[mcp_servers.<n>]`, and codex then refuses
    to load its config at all -- `Error loading config.toml: invalid transport in mcp_servers.basic-memory`.
    The agent never started. Only a REAL agent launch caught it; `codex exec` takes a different path.

    A per-seat home is ALREADY clean, so the correct mcp-flag count for it is ZERO."""
    desktop = _codex_home(tmp_path, "desktop", ["basic-memory", "context7", "gemini-cli"])
    seat = _codex_home(tmp_path, "seat")                      # a real seat home: logged in, no desktop cruft
    spec = {"tool": "codex", "role": "w", "label": "w", "flags": [], "env": {},
            "plugins": [], "settings": "", "setting_sources": ""}

    _, seat_args, _ = fleet.adapter_compile("codex", spec, [], codex_home=str(seat))
    assert [a for a in seat_args if a.startswith("mcp_servers.")] == []      # ZERO -- nothing to disable
    assert seat_args[:2] == ["--disable", "plugins"]                          # still strips desktop plugins

    # the control that makes the assertion above MEAN something: the same compile against the dirty desktop
    # home DOES emit all three. So the zero is the seat home being read -- not the flags silently vanishing.
    _, desk_args, _ = fleet.adapter_compile("codex", spec, [], codex_home=str(desktop))
    assert [a for a in desk_args if a.startswith("mcp_servers.")] == [
        "mcp_servers.basic-memory.enabled=false",
        "mcp_servers.context7.enabled=false",
        "mcp_servers.gemini-cli.enabled=false"]


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
