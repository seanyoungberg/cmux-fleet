# tests/test_close_seat.py — an archived agent leaves NO cmux residue.
#
# Berg's ruling (2026-07-10): "You retired it ... but now there's just a workspace sitting there open
# with that name on it. Remove the workspace from cmux whenever an agent is getting archived."
#
# TWO KINDS OF TEST HERE, and the split is the point:
#
#   1. plan_seat_close — PURE. Which cmux verb retires a seat, and what it takes with it.
#
#   2. _close_seat / cmd_archive / cmd_rm — the REAL SEAM. `cmux` is stubbed by a model of cmux, not by
#      a silent no-op. FakeCmux renders a real-shaped `cmux tree` (the exact nesting, ref+uuid id-format,
#      `[terminal]`/`[markdown]` kinds and quoted titles this box emits), serves a real-shaped
#      `workspace-group list --json` (short `workspace:N` member refs, an `anchor_workspace_ref`), and
#      ENFORCES cmux's two documented refusals:
#        - `close-surface` on a workspace's ONLY surface -> `invalid_state: Cannot close the last surface`
#          (docs/reap-surfaces.md item 3);
#        - `close-surface --surface <uuid>` with no `--workspace` -> `not_found: Surface not found`
#          (docs/reap-surfaces.md item 1).
#      The suite's default cmux stub exits 0 and prints nothing, which would let a `close-surface` on a
#      sole surface pass a test and fail on the box. That is how the teardown wedge shipped green. A stub
#      that cannot refuse cannot test a guard.
import json

import pytest

from cmux_fleet import cli as fleet
from cmux_fleet import resolve as rs

# --- real-shaped ids (a workspace's UUID and its `workspace:N` ref both matter to the group verbs) ---
W_COND, R_COND = "A7FF3E7C-5F54-4EC3-BB8A-2D21C2459FF7", "workspace:2"
W_GRAPH, R_GRAPH = "022AF32C-AF42-4603-B610-E4DC16F40717", "workspace:23"
W_RESEARCH, R_RESEARCH = "B1656D4C-22E7-438F-9797-B62A92B7AF81", "workspace:6"

S_COND = "F1C0AEDB-8CA3-49AB-8BCA-440989FF7C57"          # the conductor's own terminal
S_TAB = "9690F6E8-094A-48EC-84AA-C106804BCD56"           # a tab child, in the conductor's workspace
S_GRAPH = "4E496E4B-A010-482B-BB04-A7DF9929EBCD"         # a workspace child, sole surface
S_RESEARCH = "2694CB4C-706E-45A7-BB64-EA572AA9421C"      # a workspace child ...
S_NOTES = "5CD821D0-5098-4C77-941B-32352BD866FD"         # ... with a markdown view pane beside it
PANE_UUIDS = ["00B68660-784E-4838-BB90-0C37093FB39D", "A2146F3B-4772-42F3-B31B-033EB72CB239",
              "CD82F4D9-4D4C-4177-A985-51950E26B88A"]


def _tree_model():
    """[(ws_ref, ws_uuid, name, [(surf_ref, surf_uuid, kind, title)])] — the live shape of this box."""
    return [
        (R_COND, W_COND, "Conductor - cmux-advisor",
         [("surface:10", S_COND, "terminal", "✳ Chat with Berg about recycling handover"),
          ("surface:11", S_TAB, "terminal", "⠐ Continue loom-dev development session")]),
        (R_GRAPH, W_GRAPH, "graph-view",
         [("surface:83", S_GRAPH, "terminal", "✳ Execute graph-view mission briefing")]),
        (R_RESEARCH, W_RESEARCH, "resume-research",
         [("surface:25", S_RESEARCH, "terminal", "✳ Resume research continuation"),
          ("surface:24", S_NOTES, "markdown", "cloudflare-fde-cover-letter-2026-07-02.md")]),
    ]


def _render_tree(model):
    """`cmux tree --all --id-format both` TEXT, byte-shaped like the real thing."""
    lines = ["window window:1 9FBB70C6-7B17-4DA5-B54D-8FF3641D24E2 [current] ◀ active"]
    for i, (wref, wuuid, name, surfaces) in enumerate(model):
        lines.append(f'├── workspace {wref} {wuuid} "{name}"')
        lines.append(f'│   └── pane pane:{i + 2} {PANE_UUIDS[i % len(PANE_UUIDS)]} [focused]')
        for sref, suuid, kind, title in surfaces:
            sel = " [selected]" if surfaces.index((sref, suuid, kind, title)) == 0 else ""
            lines.append(f'│       ├── surface {sref} {suuid} [{kind}] "{title}"{sel}')
    return "\n".join(lines) + "\n"


class FakeCmux:
    """A cmux that can REFUSE. Mutates its own tree, so `_close_seat`'s post-close re-read of the tree
    is a real verification and not a tautology."""

    def __init__(self, model=None, groups=None):
        self.model = model if model is not None else _tree_model()
        # one group whose anchor IS the conductor's own workspace — the LEGACY Model A shape (or the
        # scaffold-close case). Model B never archives its empty anchor, so this is the fixture for the
        # defensive re-anchor path: closing this anchor must re-anchor onto a FRESH scaffold, never a member.
        self.groups = groups if groups is not None else {"groups": [{
            "ref": "workspace_group:2", "name": "Conductor - cmux-advisor",
            "anchor_workspace_ref": R_COND,
            "member_workspace_refs": [R_COND, R_GRAPH, R_RESEARCH]}]}
        self.calls = []
        self.set_anchor_works = True

    # -- helpers over the model
    def _ws(self, uuid):
        return next((w for w in self.model if w[1].upper() == (uuid or "").upper()), None)

    def _ws_of_surface(self, uuid):
        for w in self.model:
            if any(s[1].upper() == (uuid or "").upper() for s in w[3]):
                return w
        return None

    def _flag(self, args, name):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else ""

    def __call__(self, *args):
        self.calls.append(args)
        if args[:1] == ("tree",):
            return _render_tree(self.model)
        if args[:3] == ("workspace-group", "list", "--json"):
            return json.dumps(self.groups)
        if args[:1] == ("new-workspace",):                 # mint a bare scaffold (Model B re-anchor target)
            n = 900 + len(self.model)
            ref, uuid = f"workspace:{n}", f"F00DF00D-0000-0000-0000-{n:012d}"
            name = self._flag(args, "--name")
            self.model.append((ref, uuid, name,
                               [(f"surface:{n}", f"5CAF01D0-0000-0000-0000-{n:012d}", "terminal", name)]))
            gref = self._flag(args, "--group")
            if gref:
                g = next((g for g in self.groups["groups"] if g["ref"] == gref), None)
                if g:
                    g["member_workspace_refs"].append(ref)
            return f"created {ref}\n"
        if args[:1] == ("rename-workspace",):              # retitle a workspace in the model (Model B anchor)
            w = self._ws(self._flag(args, "--workspace"))
            if w:
                title = args[-1]                           # `rename-workspace --workspace <ws> -- <title>`
                self.model[self.model.index(w)] = (w[0], w[1], title, w[3])
            return ""
        if args[:2] == ("workspace-group", "set-anchor"):
            if self.set_anchor_works:
                ws = self._flag(args, "--workspace")
                w = self._ws(ws)
                if w:
                    self.groups["groups"][0]["anchor_workspace_ref"] = w[0]
            return ""
        if args[:1] == ("close-workspace",):
            w = self._ws(self._flag(args, "--workspace"))
            if not w:
                return "Error: not_found: Workspace not found"
            self.model.remove(w)
            g = self.groups["groups"][0]
            g["member_workspace_refs"] = [r for r in g["member_workspace_refs"] if r != w[0]]
            return ""
        if args[:1] == ("close-surface",):
            surf = self._flag(args, "--surface")
            if not self._flag(args, "--workspace"):        # documented: a bare cross-workspace uuid fails
                return "Error: not_found: Surface not found"
            w = self._ws_of_surface(surf)
            if not w:
                return "Error: not_found: Surface not found"
            if len(w[3]) == 1:                             # documented: cmux refuses the last surface
                return "Error: invalid_state: Cannot close the last surface"
            w[3][:] = [s for s in w[3] if s[1].upper() != surf.upper()]
            return ""
        return ""


def _seed(fs, label, surf, place="tab", parent="cond", ws="", **extra):
    fs.live_put(label, {"role": label, "kind": "child", "tool": "claude", "cwd": "/x", "place": place,
                        "group": "", "surface": surf, "session": f"claude-{label}", "parent": parent,
                        "workspace": ws, "plugins": [], "flags": [], "settings": "", "status": "live",
                        **extra})


@pytest.fixture
def cmux(monkeypatch, fs):
    """A model-of-cmux wired into every seam `_close_seat` shells through."""
    c = FakeCmux()
    monkeypatch.setattr(fleet, "cmuxq", c)
    monkeypatch.setattr(fs, "read_hook_store", lambda: {"sessions": {}, "activeSessionsBySurface": {}})
    monkeypatch.setattr(rs.fs, "read_hook_store", lambda: {"sessions": {}, "activeSessionsBySurface": {}})
    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
    return c


# ================================ 1. plan_seat_close — PURE ========================================
def _sib(surface, label="", pids=(), kind="terminal", title=""):
    return {"surface": surface.upper(), "kind": kind, "title": title, "label": label, "pids": list(pids)}


def test_plan_closes_the_workspace_of_a_workspace_placed_agent():
    p = fleet.plan_seat_close("graph-view", S_GRAPH, W_GRAPH, caller_workspace=W_COND,
                              place="workspace", siblings=[])
    assert p["verb"] == "close-workspace" and p["workspace"] == W_GRAPH and p["blockers"] == []


def test_plan_takes_the_agents_own_view_panes_as_collateral():
    """An agent's workspace can hold its own markdown/browser view panes. They are the agent's, they
    hold no process, and leaving them behind IS the residue Berg objected to."""
    notes = _sib(S_NOTES, kind="markdown", title="notes.md")
    p = fleet.plan_seat_close("resume-research", S_RESEARCH, W_RESEARCH, W_COND, "workspace", [notes])
    assert p["verb"] == "close-workspace"
    assert [c["surface"] for c in p["collateral"]] == [S_NOTES.upper()]


def test_plan_never_closes_the_workspace_of_a_tab_child():
    """Guard 4. A tab/pane agent SHARES its parent conductor's workspace; closing it takes the conductor."""
    p = fleet.plan_seat_close("tab-child", S_TAB, W_COND, caller_workspace="", place="shared",
                              siblings=[_sib(S_COND, label="cond")])
    assert p["verb"] == "close-surface"
    assert any("not 'workspace'" in b for b in p["blockers"])


def test_plan_never_closes_the_callers_own_workspace():
    """Guard 1. Never close the ground you stand on."""
    p = fleet.plan_seat_close("self", S_GRAPH, W_GRAPH, caller_workspace=W_GRAPH.lower(),
                              place="workspace", siblings=[])
    assert p["verb"] == "close-surface"
    assert any("CALLER" in b for b in p["blockers"])


def test_plan_never_closes_a_workspace_hosting_another_registered_agent():
    """Guard 2a. Membership comes from the tree; the label comes from the registry."""
    p = fleet.plan_seat_close("a", S_GRAPH, W_GRAPH, "", "workspace", [_sib(S_TAB, label="sibling-agent")])
    assert p["verb"] == "close-surface"
    assert any("sibling-agent" in b for b in p["blockers"])


def test_plan_never_closes_a_workspace_with_a_live_pid_on_a_bystander_surface():
    """Guard 2b — the never-orphan floor, applied to UNTRACKED seats. A conductor whose registry row was
    already archived has no label, but its live claude pid is still a live agent. Closing the workspace
    would strand it with no pane: the exact 2026-07-10 orphan class, at workspace scale."""
    p = fleet.plan_seat_close("a", S_GRAPH, W_GRAPH, "", "workspace", [_sib(S_COND, pids=[70208])])
    assert p["verb"] == "close-surface"
    assert any("70208" in b for b in p["blockers"])


def test_plan_falls_back_to_close_surface_when_the_tree_cannot_locate_the_surface():
    p = fleet.plan_seat_close("a", S_GRAPH, "", "", "workspace", [])
    assert p["verb"] == "close-surface" and p["workspace"] == ""


# ============================ 2. _close_seat — the REAL cmux seam ==================================
def test_close_seat_closes_the_workspace_and_leaves_no_residue(fs, cmux):
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "graph-view", S_GRAPH, place="workspace", parent="cond")
    ok, notes = fleet._close_seat("graph-view", fs.live_get("graph-view"), "archive")
    assert ok
    assert ("close-workspace", "--workspace", W_GRAPH) in cmux.calls
    assert not any(c[:1] == ("close-surface",) for c in cmux.calls)   # the verb that would have refused
    assert cmux._ws(W_GRAPH) is None                                  # gone from the tree
    assert any("closed workspace" in n for n in notes)
    assert fs.expected_close_recent(S_GRAPH)                          # tombstoned -> no spurious stale alert


def test_close_seat_of_a_tab_child_spares_the_parents_workspace(fs, cmux):
    """The other half of the ruling. A tab child loses its surface; the conductor's workspace survives."""
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "tab-child", S_TAB, place="tab", parent="cond")
    ok, _ = fleet._close_seat("tab-child", fs.live_get("tab-child"), "rm")
    assert ok
    assert ("close-surface", "--surface", S_TAB, "--workspace", W_COND) in cmux.calls
    assert not any(c[:1] == ("close-workspace",) for c in cmux.calls)
    assert cmux._ws(W_COND) is not None                                # conductor's workspace still there
    assert [s[1] for s in cmux._ws(W_COND)[3]] == [S_COND]             # ...holding only the conductor


def test_close_seat_passes_workspace_context_to_close_surface(fs, cmux):
    """docs/reap-surfaces.md item 1: a bare `--surface <uuid>` does not resolve globally. FakeCmux
    enforces it, so a regression to the bare form fails here instead of on the box."""
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "tab-child", S_TAB, place="tab", parent="cond")
    fleet._close_seat("tab-child", fs.live_get("tab-child"), "rm")
    close = next(c for c in cmux.calls if c[:1] == ("close-surface",))
    assert "--workspace" in close


def test_close_seat_takes_the_markdown_view_pane_with_the_workspace(fs, cmux):
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "resume-research", S_RESEARCH, place="workspace", parent="cond")
    _ok, notes = fleet._close_seat("resume-research", fs.live_get("resume-research"), "archive")
    assert ("close-workspace", "--workspace", W_RESEARCH) in cmux.calls
    assert any("[markdown]" in n for n in notes)
    assert fs.expected_close_recent(S_NOTES)          # collateral is tombstoned too, or the router alerts


def test_close_seat_refuses_the_callers_own_workspace(fs, cmux, monkeypatch):
    """Guard 1 end to end. The caller's surface IS the sole surface here, so the downgraded close-surface
    hits cmux's last-surface refusal — and we surface cmux's own words rather than claiming success."""
    monkeypatch.setenv("CMUX_SURFACE_ID", S_GRAPH)
    _seed(fs, "graph-view", S_GRAPH, place="workspace", parent="cond")
    ok, notes = fleet._close_seat("graph-view", fs.live_get("graph-view"), "archive")
    assert ok is False                                    # cmux REFUSED the last-surface close -> not success
    assert not any(c[:1] == ("close-workspace",) for c in cmux.calls)
    assert any("CALLER" in n for n in notes)
    assert any("Cannot close the last surface" in n for n in notes)
    assert any("did NOT close" in n for n in notes)


def test_close_seat_refuses_a_workspace_holding_an_untracked_live_agent(fs, cmux, monkeypatch):
    """Guard 2b through the REAL `ps axeww` parse. The conductor's row has been archived (no label), but
    its claude is alive and self-referential (CMUX_CLAUDE_PID == own pid). The tab child's retirement must
    not close the workspace out from under it.

    The suite's conftest blanks the ps sweep — which is exactly why a teardown wedge shipped green — so
    this test injects a real-shaped process table and lets pids_ps's real parsing run over it."""
    AGENT, DAEMON = 55001, 55002
    ps_table = (f"  PID   TT  STAT      TIME COMMAND\n"
                f"{AGENT} s001  S+     0:05.00 /Users/berg/.local/bin/claude --resume abc "
                f"CMUX_SURFACE_ID={S_COND} CMUX_CLAUDE_PID={AGENT}\n"
                f"{DAEMON}   ??  Ss     1:00.00 /x/.venv/bin/python -m cmux_fleet.daemon "
                f"CMUX_SURFACE_ID={S_COND} CMUX_CLAUDE_PID={AGENT}\n")
    monkeypatch.setattr(rs, "_ps_axeww", lambda: ps_table)
    monkeypatch.setattr(rs.fs, "pid_alive", lambda pid: pid in (AGENT, DAEMON))
    _seed(fs, "tab-child", S_TAB, place="workspace", parent="")     # derived 'workspace': no parent row
    ok, notes = fleet._close_seat("tab-child", fs.live_get("tab-child"), "rm")
    assert ok                                             # the tab child's own surface closed fine
    assert not any(c[:1] == ("close-workspace",) for c in cmux.calls)
    assert any(str(AGENT) in n for n in notes)                       # the agent blocks...
    assert not any(str(DAEMON) in n for n in notes)                  # ...its daemon does not
    assert ("close-surface", "--surface", S_TAB, "--workspace", W_COND) in cmux.calls


def test_close_seat_survives_an_unreadable_tree(fs, monkeypatch):
    """No tree -> no topology -> the pre-existing bare close-surface, never a blind close-workspace."""
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: calls.append(a) or "")
    _seed(fs, "w1", S_GRAPH, place="workspace")
    assert fleet._close_seat("w1", fs.live_get("w1"), "rm") == (True, [])
    assert ("close-surface", "--surface", S_GRAPH) in calls
    assert not any(c[:1] == ("close-workspace",) for c in calls)


# ================================ 3. the workspace-group anchor ====================================
def test_close_seat_reanchors_onto_a_fresh_scaffold_never_a_member(fs, cmux):
    """Guard 3, Model B (empty-anchor). cmux: "Closing the anchor dissolves the group while preserving its
    other members as ungrouped workspaces." When the anchor WORKSPACE is removed (here a legacy Model A
    group whose conductor IS the anchor), re-anchor onto a FRESH empty scaffold minted in the group --
    NEVER onto a surviving member conductor (that is Model A: the bare-folder header this flip reverses)."""
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "graph-view", S_GRAPH, place="workspace", parent="cond")
    cmux.model[0][3].pop()                       # the conductor is alone in its workspace (tab child gone)
    fs.live_del("graph-view")                    # ...and its children are untracked bystanders elsewhere
    _ok, notes = fleet._close_seat("cond", fs.live_get("cond"), "archive")
    # a fresh scaffold was minted IN the group and titled with the group's header name...
    mint = [c for c in cmux.calls if c[0] == "new-workspace"][0]
    assert "--group" in mint and "workspace_group:2" in mint and "Conductor - cmux-advisor" in mint
    new_ref = cmux.groups["groups"][0]["anchor_workspace_ref"]
    assert new_ref not in (R_COND, R_GRAPH, R_RESEARCH)          # a brand-new anchor, NOT a member
    new_uuid = next(w[1] for w in cmux.model if w[0] == new_ref)
    setanchor = [c for c in cmux.calls if c[:2] == ("workspace-group", "set-anchor")][0]
    assert new_uuid in setanchor
    assert W_GRAPH not in setanchor and W_RESEARCH not in setanchor  # the survivors are NEVER made the anchor
    close = ("close-workspace", "--workspace", W_COND)
    assert close in cmux.calls
    assert cmux.calls.index(setanchor) < cmux.calls.index(close)    # re-anchor BEFORE closing the old anchor
    assert any("re-anchored" in n and "scaffold" in n for n in notes)


def test_close_seat_refuses_the_close_when_the_reanchor_does_not_take(fs, cmux):
    """The mint + re-anchor is a cmux mutation, so it is VERIFIED. An unverified anchor move means the next
    call dissolves the group: refuse the workspace close, retire the surface, and say so."""
    cmux.set_anchor_works = False
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    cmux.model[0][3].pop()
    ok, notes = fleet._close_seat("cond", fs.live_get("cond"), "archive")
    assert ok is False                                    # the workspace survived -> the caller must fail loudly
    assert not any(c[:1] == ("close-workspace",) for c in cmux.calls)
    assert any("did not move the anchor" in n for n in notes)
    assert cmux.groups["groups"][0]["anchor_workspace_ref"] == R_COND      # group intact


def test_close_seat_of_a_modelb_conductor_member_never_touches_the_empty_anchor(fs, monkeypatch):
    """Model B live-group NO-DISTURB: the anchor is an EMPTY scaffold and the conductor is an ordinary
    MEMBER. Archiving the conductor closes ONLY its member workspace -- no re-anchor, no mint, the empty
    anchor and the group are left exactly as they were. This is the guarantee for the live groups already
    restructured by hand."""
    scaffold_r, scaffold_w = "workspace:61", "0B0B0B0B-0000-0000-0000-000000000061"
    model = _tree_model()
    model.append((scaffold_r, scaffold_w, "Conductor - cmux-advisor",
                  [("surface:200", "0B0B0B0B-0000-0000-0000-0000000000C8", "terminal", "")]))
    groups = {"groups": [{"ref": "workspace_group:2", "name": "Conductor - cmux-advisor",
                          "anchor_workspace_ref": scaffold_r,
                          "member_workspace_refs": [scaffold_r, R_COND, R_GRAPH]}]}
    c = FakeCmux(model=model, groups=groups)
    monkeypatch.setattr(fleet, "cmuxq", c)
    monkeypatch.setattr(fs, "read_hook_store", lambda: {"sessions": {}, "activeSessionsBySurface": {}})
    monkeypatch.setattr(rs.fs, "read_hook_store", lambda: {"sessions": {}, "activeSessionsBySurface": {}})
    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    c.model[0][3].pop()                          # the conductor is alone in its own member workspace
    ok, notes = fleet._close_seat("cond", fs.live_get("cond"), "archive")
    assert ("close-workspace", "--workspace", W_COND) in c.calls        # the MEMBER workspace closed...
    assert not any(x[:2] == ("workspace-group", "set-anchor") for x in c.calls)  # ...anchor untouched
    assert not any(x[:1] == ("new-workspace",) for x in c.calls)        # no scaffold minted
    assert c.groups["groups"][0]["anchor_workspace_ref"] == scaffold_r  # the empty anchor is intact


def test_close_seat_of_a_plain_group_member_leaves_the_group_alone(fs, cmux):
    """A non-anchor member needs no dance: closing it just drops it from the group."""
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "graph-view", S_GRAPH, place="workspace", parent="cond")
    fleet._close_seat("graph-view", fs.live_get("graph-view"), "archive")
    assert not any(c[:2] == ("workspace-group", "set-anchor") for c in cmux.calls)
    assert cmux.groups["groups"][0]["anchor_workspace_ref"] == R_COND
    assert R_GRAPH not in cmux.groups["groups"][0]["member_workspace_refs"]


def test_close_seat_of_a_solo_anchor_dissolves_only_its_own_empty_group(fs, cmux):
    cmux.groups["groups"][0]["member_workspace_refs"] = [R_COND]
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    cmux.model[0][3].pop()
    _ok, notes = fleet._close_seat("cond", fs.live_get("cond"), "archive")
    assert ("close-workspace", "--workspace", W_COND) in cmux.calls
    assert any("empty group" in n for n in notes)


# ============================ 4. archive / rm, end to end =========================================
def test_archive_closes_the_workspace_and_still_writes_a_revivable_row(fs, cmux, monkeypatch, capsys):
    """The whole ruling in one test: `fleet archive` on a workspace-placed agent leaves no surface and no
    workspace behind, and the agent stays revivable."""
    monkeypatch.setattr(fleet, "_stop_agent_for_close", lambda *a: (True, ""))
    monkeypatch.setattr(fleet, "_resume_binding", lambda s: {"checkpoint_id": "CKPT-1"})
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "graph-view", S_GRAPH, place="workspace", parent="cond")
    assert fleet.cmd_archive(["graph-view"]) == 0
    assert ("close-workspace", "--workspace", W_GRAPH) in cmux.calls
    assert fs.live_get("graph-view") is None
    assert fs.archive_get("graph-view")["last_session"] == "CKPT-1"
    assert "closed workspace" in capsys.readouterr().out


def test_rm_closes_the_workspace_of_a_workspace_placed_agent(fs, cmux, monkeypatch):
    monkeypatch.setattr(fleet, "_stop_agent_for_close", lambda *a: (True, ""))
    monkeypatch.setattr(fleet, "_resume_binding", lambda s: {})
    monkeypatch.setattr(fs, "lifecycle", lambda s: "idle")
    monkeypatch.setattr(fs, "surface_has_live_pid", lambda s: False)
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "graph-view", S_GRAPH, place="workspace", parent="cond")
    assert fleet.cmd_rm(["graph-view"]) == 0
    assert ("close-workspace", "--workspace", W_GRAPH) in cmux.calls
    assert fs.archive_get("graph-view") is not None                  # revivable


def test_rm_of_a_tab_child_never_closes_the_conductors_workspace(fs, cmux, monkeypatch):
    monkeypatch.setattr(fleet, "_stop_agent_for_close", lambda *a: (True, ""))
    monkeypatch.setattr(fleet, "_resume_binding", lambda s: {})
    monkeypatch.setattr(fs, "lifecycle", lambda s: "idle")
    monkeypatch.setattr(fs, "surface_has_live_pid", lambda s: False)
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "tab-child", S_TAB, place="tab", parent="cond")
    assert fleet.cmd_rm(["tab-child"]) == 0
    assert not any(c[:1] == ("close-workspace",) for c in cmux.calls)
    assert fs.live_get("cond") is not None and cmux._ws(W_COND) is not None


def test_archive_of_an_exec_delivered_agent_still_closes_its_workspace(fs, cmux, monkeypatch):
    """Under exec delivery (docs/design-exec-launch.md) the agent IS the pane's process, so cmux closes
    its surface the instant SIGINT lands. Measured live on a codex seat, 2026-07-10: `_close_seat` read
    the tree AFTER the stop, found no surface, and fell through to the surface-unlocatable branch — which
    would leave a workspace-placed codex's workspace standing forever. The plan must be read BEFORE the
    stop. Here the stop deletes the whole workspace's surface from the model, exactly as cmux does."""
    def stop_and_vanish(surf, tool, label, verb):
        w = cmux._ws_of_surface(surf)
        w[3][:] = [s for s in w[3] if s[1].upper() != surf.upper()]   # the pane process died -> surface gone
        return True, ""
    monkeypatch.setattr(fleet, "_stop_agent_for_close", stop_and_vanish)
    monkeypatch.setattr(fleet, "_resume_binding", lambda s: {})
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "codex-child", S_GRAPH, place="workspace", parent="cond", tool="codex")
    notes = fleet.cmd_archive(["codex-child"]) or []
    assert ("close-workspace", "--workspace", W_GRAPH) in cmux.calls
    assert cmux._ws(W_GRAPH) is None
    assert fs.archive_get("codex-child") is not None


def test_close_seat_verifies_the_workspace_not_the_surface(fs, cmux, monkeypatch):
    """The post-close verify must check the WORKSPACE is gone, not the surface. Worst case, and the one
    that motivates it: an exec-delivered agent whose surface ALREADY left the tree with its process, and
    a `close-workspace` that cmux then refuses. A surface-absence check passes vacuously there and
    reports "no cmux residue" over a workspace still sitting in the sidebar."""
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "graph-view", S_GRAPH, place="workspace", parent="cond")
    planned = fleet.seat_close_plan("graph-view", fs.live_get("graph-view"))   # read while the seat lives
    w = cmux._ws(W_GRAPH)
    w[3][:] = []                                          # exec delivery: the pane process died, surface gone
    real = FakeCmux.__call__
    def refuse_close(self, *args):                        # ...and cmux refuses to close the workspace
        if args[:1] == ("close-workspace",):
            self.calls.append(args)
            return "Error: invalid_state: workspace busy"
        return real(self, *args)
    monkeypatch.setattr(FakeCmux, "__call__", refuse_close)
    ok, notes = fleet._close_seat("graph-view", fs.live_get("graph-view"), "archive", planned=planned)
    assert ok is False
    assert cmux._ws(W_GRAPH) is not None                  # the workspace really is still there...
    assert S_GRAPH.upper() not in fleet.surface_ws_map_from_tree(cmux("tree"))   # ...and the surface is not
    assert any("did NOT close" in n for n in notes)       # so we must NOT claim success
    assert not any("no cmux residue" in n for n in notes)


def test_archive_writes_the_registry_row_before_the_cmux_close(fs, cmux, monkeypatch):
    """agent-management v2 §2: the registry write precedes the cmux mutation it describes, so a close
    that half-fails degrades to "recorded, maybe-unresumable" rather than "vanished"."""
    monkeypatch.setattr(fleet, "_stop_agent_for_close", lambda *a: (True, ""))
    monkeypatch.setattr(fleet, "_resume_binding", lambda s: {})
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "graph-view", S_GRAPH, place="workspace", parent="cond")
    seen = {}
    real = fleet.cmuxq
    def spy(*args):
        if args[:1] == ("close-workspace",):
            seen["archived_first"] = fs.archive_get("graph-view") is not None
        return real(*args)
    monkeypatch.setattr(fleet, "cmuxq", spy)
    fleet.cmd_archive(["graph-view"])
    assert seen.get("archived_first") is True


# ================= 5. the lie: reporting a close that never happened ===============================
# cmux-advisor, reproduced live on `placeprobe` 2026-07-10: `fleet rm` on a workspace-placed agent printed
# "removed (closed + archived for recovery)" and returned 0. close-surface had hit the documented
# `invalid_state: Cannot close the last surface`; rm swallowed it. The surface and its workspace were both
# still in the tree. `cmuxq` returns stdout+stderr and NO exit code, so a refusal reads exactly like a
# success -- the close must be verified against a fresh TREE, and disagreement must be an error.
# With [defaults] place = "workspace" now the norm, this was every agent, every teardown.

def test_rm_never_reports_a_close_it_did_not_perform(fs, cmux, monkeypatch, capsys):
    """The placeprobe repro. A guard downgrades the workspace-placed agent to close-surface; its surface
    is the workspace's only one, so cmux refuses. rm must NOT say "removed ... closed"."""
    monkeypatch.setenv("CMUX_SURFACE_ID", S_GRAPH)        # caller-workspace guard -> downgrade
    monkeypatch.setattr(fleet, "_stop_agent_for_close", lambda *a: (True, ""))
    monkeypatch.setattr(fleet, "_resume_binding", lambda s: {})
    monkeypatch.setattr(fs, "lifecycle", lambda s: "idle")
    monkeypatch.setattr(fs, "surface_has_live_pid", lambda s: False)
    _seed(fs, "graph-view", S_GRAPH, place="workspace", parent="cond")
    rc = fleet.cmd_rm(["graph-view"])
    out = capsys.readouterr().out
    assert rc == 2                                        # NOT 0. The seat is still open.
    assert "STILL OPEN" in out and "residue, not success" in out
    assert "Cannot close the last surface" in out         # cmux's own words, surfaced
    assert "cmux close-workspace --workspace" in out      # ...and the fix, spelled out
    assert cmux._ws(W_GRAPH) is not None                  # the residue really is there
    assert fs.archive_get("graph-view") is not None       # the agent is still recorded + revivable
    events = [json.loads(l) for l in open(fs.LOG) if l.strip()]
    assert any(e["event"] == "seat_close_residue" and e["label"] == "graph-view" for e in events)


def test_archive_fails_loudly_when_the_workspace_survives(fs, cmux, monkeypatch, capsys):
    """Same contract on the close-workspace path: cmux accepts the call, the workspace stays."""
    monkeypatch.setattr(fleet, "_stop_agent_for_close", lambda *a: (True, ""))
    monkeypatch.setattr(fleet, "_resume_binding", lambda s: {})
    real = FakeCmux.__call__
    def refuse(self, *args):
        if args[:1] == ("close-workspace",):
            self.calls.append(args)
            return "Error: invalid_state: workspace busy"
        return real(self, *args)
    monkeypatch.setattr(FakeCmux, "__call__", refuse)
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "graph-view", S_GRAPH, place="workspace", parent="cond")
    rc = fleet.cmd_archive(["graph-view"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "STILL OPEN" in out
    assert "workspace busy" in out
    assert fs.archive_get("graph-view") is not None


def test_a_clean_close_still_returns_zero(fs, cmux, monkeypatch, capsys):
    """The guard must not turn every teardown into a failure: a real close reports success and rc 0."""
    monkeypatch.setattr(fleet, "_stop_agent_for_close", lambda *a: (True, ""))
    monkeypatch.setattr(fleet, "_resume_binding", lambda s: {})
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "graph-view", S_GRAPH, place="workspace", parent="cond")
    assert fleet.cmd_archive(["graph-view"]) == 0
    out = capsys.readouterr().out
    assert "no cmux residue" in out and "STILL OPEN" not in out


def test_tab_child_close_is_verified_too(fs, cmux, monkeypatch, capsys):
    """A tab child's close-surface is verified against the tree as well -- and here it genuinely works,
    because its workspace holds the conductor too (so it is never the last surface)."""
    monkeypatch.setattr(fleet, "_stop_agent_for_close", lambda *a: (True, ""))
    monkeypatch.setattr(fleet, "_resume_binding", lambda s: {})
    _seed(fs, "cond", S_COND, place="workspace", parent="")
    _seed(fs, "tab-child", S_TAB, place="tab", parent="cond")
    assert fleet.cmd_archive(["tab-child"]) == 0
    assert "STILL OPEN" not in capsys.readouterr().out
    assert [s[1] for s in cmux._ws(W_COND)[3]] == [S_COND]
