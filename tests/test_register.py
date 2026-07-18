# tests/test_register.py — `fleet register`: the manual escape hatch that pulls a LIVE-but-UNREGISTERED
# agent into the registry (recovery for a skipped auto-register / an agent launched outside fleet).
# Pure units: the cmux reads (_store / poll_session / rs.workspace) are monkeypatched; the
# registry/archive side runs against the throwaway $CMUX_STATE_DIR. No toml in the test env, so
# _is_roster is False and no roster resolve runs.
import os
import sys

import pytest


from cmux_fleet import cli as fleet  # noqa: E402


@pytest.fixture
def rs():
    """The in-process `resolve` module — imported INSIDE the fixture so a test_features sys.modules reset
    (this file sorts after it) can't leave us holding a stale twin. Same rationale as test_move_harden's."""
    from cmux_fleet import resolve
    return resolve


def _patch_cmux(monkeypatch, session="SESS", ws="WS-1", store=None, tool="claude", surf_cwd="",
                roster=False, live=True):
    # derive tool/session/workspace/cwd from the "live surface" — all cmux reads are stubbed so the
    # unit tests never touch the host's real ~/.cmuxterm hook store. `roster` pins _is_roster (the test
    # env otherwise falls back to the host's real ~/.config/cmux-fleet/fleet.toml, breaking hermeticity).
    # `_live_session_for` is THE register live gate (codex P1): a truthy record means the surface is
    # CURRENTLY live; None means dead/stale -> register must refuse. Stub it so tests don't poll the host.
    from cmux_fleet import resolve as rs
    rec = {"sessionId": session, "workspaceId": ws, "cwd": surf_cwd} if live else None
    monkeypatch.setattr(fleet, "_live_session_for", lambda surf: rec)
    # register derives the workspace from cmux TREE ground-truth (rs.workspace), not the frozen bind
    # record -- stub it so unit tests never shell out to `cmux tree`. Default: agrees with `ws`.
    monkeypatch.setattr(rs, "workspace", lambda *a, **k: ws)
    monkeypatch.setattr(fleet, "_tool_for_surface", lambda surf: tool)
    monkeypatch.setattr(fleet, "_surface_cwd", lambda surf: surf_cwd)
    monkeypatch.setattr(fleet, "_is_roster", lambda role: roster)
    monkeypatch.setattr(fleet, "_store",
                        lambda: store or {"sessions": {}, "activeSessionsBySurface": {}})


def test_register_promotes_archived_with_explicit_surface(fs, monkeypatch):
    fs.archive_put("homelab", {"role": "homelab", "tool": "claude", "kind": "conductor",
                               "cwd": "/x/homelab", "place": "tab", "plugins": ["a"], "flags": ["--f"]})
    _patch_cmux(monkeypatch, session="SESS-UUID", ws="WS-1")
    rc = fleet.cmd_register(["homelab", "--surface", "SURF-1", "--parent", "P"])
    assert rc == 0
    e = fs.live_get("homelab")
    assert e and e["surface"] == "SURF-1"
    assert e["session"] == "claude-SESS-UUID"           # claude tool -> prefixed
    assert e["role"] == "homelab" and e["kind"] == "conductor" and e["workspace"] == "WS-1"
    assert e["plugins"] == ["a"] and e["flags"] == ["--f"]   # spec rebuilt from the archive entry
    assert fs.archive_get("homelab") is None            # archive->live promotion: shelf entry removed


def test_register_uses_tree_workspace_not_frozen_bind_record(fs, rs, monkeypatch):
    # root cause #3 (2026-07-07): after a cross-workspace MOVE the bind record's workspaceId FREEZES at
    # the OLD workspace, so register must record where the surface lives NOW (cmux tree ground-truth),
    # not the frozen value -- else a moved child re-registers back into its old shared workspace.
    fs.archive_put("moved", {"role": "moved", "tool": "claude", "kind": "child", "cwd": "/x/m",
                             "place": "workspace", "group": "g"})
    _patch_cmux(monkeypatch, session="S", ws="WS-OLD")   # frozen bind record + hook store both say OLD
    monkeypatch.setattr(rs, "workspace", lambda *a, **k: "WS-NEW")   # the tree = ground truth
    assert fleet.cmd_register(["moved", "--surface", "SURF-M", "--parent", "P"]) == 0
    assert fs.live_get("moved")["workspace"] == "WS-NEW"   # tree wins over the frozen workspaceId


def test_register_falls_back_to_bind_record_when_tree_unreadable(fs, rs, monkeypatch):
    # if the tree can't be read (rs.workspace -> ''), fall back to the bind record's workspaceId rather
    # than registering an empty workspace.
    fs.archive_put("w", {"role": "w", "tool": "claude", "kind": "child", "cwd": "/x/w", "place": "tab"})
    _patch_cmux(monkeypatch, session="S", ws="WS-REC")
    monkeypatch.setattr(rs, "workspace", lambda *a, **k: "")   # tree unreadable
    assert fleet.cmd_register(["w", "--surface", "SURF-W", "--parent", "P"]) == 0
    assert fs.live_get("w")["workspace"] == "WS-REC"   # fell back to rec.workspaceId


def test_register_discovers_surface_by_agent_label(fs, monkeypatch):
    # no --surface: match AGENT_LABEL=<label> in the recorded launchCommand from the hook store.
    fs.archive_put("worker7", {"role": "worker7", "tool": "claude", "cwd": "/x/w7"})
    store = {"sessions": {"s1": {"surfaceId": "SURF-D", "cwd": "/unrelated",
                                 "launchCommand": "cd /x && AGENT_LABEL=worker7 claude --foo"}},
             "activeSessionsBySurface": {}}
    _patch_cmux(monkeypatch, session="S7", store=store)
    assert fleet.cmd_register(["worker7", "--parent", "P"]) == 0
    assert fs.live_get("worker7")["surface"] == "SURF-D"


def test_register_discovers_surface_by_cwd(fs, monkeypatch):
    # no AGENT_LABEL match, but a session's cwd matches the spec cwd -> that surface.
    fs.archive_put("w8", {"role": "w8", "tool": "claude", "cwd": "/abs/w8"})
    store = {"sessions": {"s1": {"surfaceId": "SURF-C", "cwd": "/abs/w8", "launchCommand": "claude"}},
             "activeSessionsBySurface": {}}
    _patch_cmux(monkeypatch, session="S8", store=store)
    assert fleet.cmd_register(["w8", "--parent", "P"]) == 0
    assert fs.live_get("w8")["surface"] == "SURF-C"


def test_register_errors_without_discoverable_surface(fs, monkeypatch):
    fs.archive_put("ghost", {"role": "ghost", "cwd": "/no/match"})
    _patch_cmux(monkeypatch, store={"sessions": {}, "activeSessionsBySurface": {}})
    with pytest.raises(SystemExit):
        fleet.cmd_register(["ghost", "--parent", "P"])     # no surface -> abort asking for --surface


def test_register_errors_without_session(fs, monkeypatch):
    # surface is live but hasn't bound a session id yet -> refuse (wait for the first turn / --session).
    fs.archive_put("nosess", {"role": "nosess", "cwd": "/x"})
    _patch_cmux(monkeypatch, session="", ws="WS")             # live=True but empty sessionId
    with pytest.raises(SystemExit):
        fleet.cmd_register(["nosess", "--surface", "SURF-X", "--parent", "P"])
    assert fs.live_get("nosess") is None                      # nothing registered
    assert fs.archive_get("nosess") is not None               # archive NOT deleted (no bind)


def test_register_refuses_stale_ended_surface(fs, monkeypatch):
    # codex P1: register must NOT bind + archive_del onto a dead/ended surface. _live_session_for
    # returns None for a stale surface -> abort, archive shelf left intact.
    fs.archive_put("dead", {"role": "dead", "tool": "claude", "cwd": "/x", "place": "tab"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)  # don't sit through the 5s live-poll
    _patch_cmux(monkeypatch, live=False)                       # surface not currently live
    with pytest.raises(SystemExit):
        fleet.cmd_register(["dead", "--surface", "SURF-DEAD", "--parent", "P"])
    assert fs.live_get("dead") is None                        # not promoted to live
    assert fs.archive_get("dead") is not None                 # archive_del did NOT run


def test_live_session_for_refuses_ended_record(fs, monkeypatch):
    # the gate itself: a sessions[] record on the surface but agentLifecycle 'ended' -> not live (terminal
    # string, no active pointer -> the fallback filters it out regardless of pid).
    store = {"activeSessionsBySurface": {},
             "sessions": {"s1": {"surfaceId": "SURF-E", "sessionId": "S1",
                                 "agentLifecycle": "ended", "updatedAt": 5, "pid": os.getpid()}}}
    monkeypatch.setattr(fleet, "_store", lambda: store)
    assert fleet._live_session_for("SURF-E") is None
    # active-index entry -> resolved to the full record, but ONLY when a LIVE pid backs it (pid-aware gate,
    # round 2, 2026-07-06): cmux's 'bound right now' pointer can still reference a frozen dead-pid ghost.
    store["activeSessionsBySurface"] = {"SURF-E": {"sessionId": "S1"}}
    got = fleet._live_session_for("SURF-E")
    assert got and got.get("sessionId") == "S1"                # live pid -> returned
    store["sessions"]["s1"]["pid"] = None                      # same pointer, but the process is DEAD
    assert fleet._live_session_for("SURF-E") is None           # -> refuse (never bind onto a ghost)


def test_register_ambiguous_cwd_asks_for_surface(fs, monkeypatch):
    # codex P2: two live surfaces share the cwd and no AGENT_LABEL match -> discovery is ambiguous,
    # returns '' + candidates, and register aborts asking for --surface (never picks the first).
    fs.archive_put("amb", {"role": "amb", "cwd": "/shared"})
    store = {"activeSessionsBySurface": {},
             "sessions": {"a": {"surfaceId": "SURF-A", "cwd": "/shared", "launchCommand": "claude"},
                          "b": {"surfaceId": "SURF-B", "cwd": "/shared", "launchCommand": "claude"}}}
    monkeypatch.setattr(fleet, "_store", lambda: store)
    monkeypatch.setattr(fleet, "_is_roster", lambda role: False)
    surf, cands = fleet._discover_surface_for("amb", "/shared")
    assert surf == "" and set(cands) == {"SURF-A", "SURF-B"}   # ambiguous -> no pick
    with pytest.raises(SystemExit):
        fleet.cmd_register(["amb", "--parent", "P"])           # no --surface -> abort
    assert fs.live_get("amb") is None


def test_register_typeerror_repro_dict_launchcommand(fs, rs, monkeypatch):
    # THE crash repro: a fully UNREGISTERED agent (no archive/live entry) whose cmux launchCommand is a
    # dict. The old code did re.search(pattern, dict) while deriving the role from the binding ->
    # 'expected string or bytes-like object, got dict'. _launchcmd coerces it now -> no crash, role
    # falls back to the label. Exercises the real recovery path (src is empty, so the binding IS parsed).
    store = {"activeSessionsBySurface": {"SURF-KG": {"sessionId": "SESS-KG"}},
             "sessions": {"s": {"surfaceId": "SURF-KG", "sessionId": "SESS-KG",
                                "agentLifecycle": "idle", "cwd": "/x", "workspaceId": "WS-KG",
                                "pid": os.getpid(),   # a genuinely-live agent (the register live gate is pid-aware)
                                "launchCommand": {"argv": ["claude"], "env": {"AGENT_ROLE": "kg"}}}}}
    monkeypatch.setattr(fleet, "_store", lambda: store)
    monkeypatch.setattr(fleet, "_tool_for_surface", lambda surf: "claude")
    monkeypatch.setattr(rs, "workspace", lambda *a, **k: "WS-KG")
    monkeypatch.setattr(fleet, "_surface_cwd", lambda surf: "/x")
    monkeypatch.setattr(fleet, "_is_roster", lambda role: False)
    # must NOT raise TypeError; deriving role from the dict binding coerces it and falls back to the label
    rc = fleet.cmd_register(["kg-practices", "--surface", "SURF-KG", "--parent", "P"])
    assert rc == 0
    e = fs.live_get("kg-practices")
    assert e and e["surface"] == "SURF-KG" and e["session"] == "claude-SESS-KG"
    assert e["role"] == "kg-practices"                        # dict binding not parseable -> label fallback


def test_register_idempotent_updates_same_surface_in_place(fs, monkeypatch):
    # re-register on the SAME surface -> update in place (session/ws refreshed), no duplicate.
    fs.live_put("dup", {"role": "dup", "tool": "claude", "kind": "child", "cwd": "/x",
                        "surface": "SURF-1", "session": "claude-old"})
    _patch_cmux(monkeypatch, session="NEW", ws="WS")
    fleet.cmd_register(["dup", "--surface", "SURF-1", "--parent", "P"])
    live = fs.live_all()
    assert len(live) == 1                                # no duplicate label
    assert live["dup"]["surface"] == "SURF-1" and live["dup"]["session"] == "claude-NEW"


def test_register_refuses_move_to_different_surface(fs, monkeypatch):
    # already live under SURF-1 -> refuse to hijack the label onto SURF-2 (validation guard).
    fs.live_put("busy", {"role": "busy", "tool": "claude", "cwd": "/x", "surface": "SURF-1",
                         "session": "claude-old"})
    _patch_cmux(monkeypatch, session="NEW")
    with pytest.raises(SystemExit):
        fleet.cmd_register(["busy", "--surface", "SURF-2", "--parent", "P"])
    assert fs.live_get("busy")["surface"] == "SURF-1"    # unchanged


def test_register_session_override(fs, monkeypatch):
    fs.archive_put("ov", {"role": "ov", "tool": "claude", "cwd": "/x"})
    # poll would return this, but --session overrides it
    _patch_cmux(monkeypatch, session="FROM-POLL", ws="WS")
    fleet.cmd_register(["ov", "--surface", "S", "--parent", "P", "--session", "PINNED"])
    assert fs.live_get("ov")["session"] == "claude-PINNED"


def test_register_roster_role_is_toml_authoritative(fs, monkeypatch):
    # a roster role rebuilds its spec from resolve() (berg's proven recipe), NOT the archive entry.
    fs.archive_put("hl", {"role": "hl", "tool": "claude", "cwd": "/stale", "kind": "child"})
    _patch_cmux(monkeypatch, session="S", ws="WS", roster=True)
    monkeypatch.setattr(fleet, "resolve", lambda cfg, role, tool, adhoc: {
        "tool": "claude", "role": role, "label": role, "kind": "conductor", "place": "tab",
        "group": "", "cwd": "roles/hl", "plugins": ["p"], "flags": [], "settings": ""})
    monkeypatch.setattr(fleet, "load_config", lambda: {"role": {"hl": {}}})
    assert fleet.cmd_register(["hl", "--surface", "SURF-R", "--parent", "P"]) == 0
    e = fs.live_get("hl")
    assert e["kind"] == "conductor"                     # from resolve(), not the archived 'child'
    assert e["plugins"] == ["p"] and e["cwd"].endswith("roles/hl")   # register stores abs_cwd
    assert fs.archive_get("hl") is None                 # promoted from archive


def test_ws_from_store_prefers_the_live_record_over_stale_ghosts(fs, rs):
    # THE 2026-07-10 berg-sandbox incident, replayed against rs._ws_from_store (the resolver that absorbed
    # cli.ws_uuid_for_surface in 5c). cmux never drops a surface's old records, and a reboot/resume re-homes
    # the surface into a NEW workspace — so the lingering DEAD records keep naming the OLD, now-closed
    # workspace. The old first-in-dict-order pick handed `fleet launch --place tab` that dead workspace and
    # every launch aborted 'Workspace not found'. Dict order puts the ghosts first here on purpose: a dead
    # pid cannot be the agent, so it cannot name the agent's workspace. (The store snapshot is threaded via
    # st= so the unit never reads the host hook store.)
    DEAD_WS, LIVE_WS = "WS-DEAD", "WS-LIVE"
    store = {"sessions": {
        "ghost1": {"surfaceId": "S", "sessionId": "g1", "updatedAt": 30, "pid": 999999, "workspaceId": DEAD_WS},
        "ghost2": {"surfaceId": "S", "sessionId": "g2", "updatedAt": 20, "pid": None,   "workspaceId": DEAD_WS},
        "live":   {"surfaceId": "S", "sessionId": "l1", "updatedAt": 10, "pid": os.getpid(), "workspaceId": LIVE_WS},
        "other":  {"surfaceId": "X", "sessionId": "o1", "updatedAt": 99, "pid": os.getpid(), "workspaceId": "WS-OTHER"},
    }}
    # the live record wins even though BOTH ghosts are newer by updatedAt and come first in dict order
    assert rs._ws_from_store("S", st=store) == LIVE_WS
    assert rs._ws_from_store("s", st=store) == LIVE_WS       # surface match is case-insensitive
    # no live record on the surface -> fall back to the freshest record of any liveness (best effort)
    del store["sessions"]["live"]
    assert rs._ws_from_store("S", st=store) == DEAD_WS
    # surface absent entirely -> ''
    assert rs._ws_from_store("NOPE", st=store) == ""


def test_poll_session_prefers_live_record_but_still_binds_a_pidless_fresh_one(fs, monkeypatch):
    # the LAST of the six first-match reads (2026-07-10). Dict order puts the corpse first on purpose.
    store = {"activeSessionsBySurface": {},
             "sessions": {
                 "ghost": {"surfaceId": "S", "sessionId": "GHOST", "updatedAt": 99, "pid": 999999},
                 "live":  {"surfaceId": "S", "sessionId": "LIVE",  "updatedAt": 10, "pid": os.getpid()},
             }}
    monkeypatch.setattr(fleet, "_store", lambda: store)
    assert fleet.poll_session("S", timeout=1) == "LIVE"        # alive beats a NEWER dead ghost

    # the fallback MUST survive: a just-bound session has no pid yet. Requiring liveness would hang launch.
    store["sessions"] = {"fresh": {"surfaceId": "S2", "sessionId": "FRESH", "updatedAt": 1, "pid": None}}
    assert fleet.poll_session("S2", timeout=1) == "FRESH"

    # the active-index pointer still short-circuits everything (unchanged contract)
    store["activeSessionsBySurface"] = {"S2": {"sessionId": "PINNED"}}
    assert fleet.poll_session("S2", timeout=1) == "PINNED"
