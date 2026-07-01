# tests/test_register.py — `fleet register`: the manual escape hatch that pulls a LIVE-but-UNREGISTERED
# agent into the registry (recovery for a skipped auto-register / an agent launched outside fleet).
# Pure units: the cmux reads (_store / poll_session / ws_uuid_for_surface) are monkeypatched; the
# registry/archive side runs against the throwaway $CMUX_STATE_DIR. No toml in the test env, so
# _is_roster is False and no roster resolve runs.
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import fleet  # noqa: E402


def _patch_cmux(monkeypatch, session="SESS", ws="WS-1", store=None, tool="claude", surf_cwd="",
                roster=False, live=True):
    # derive tool/session/workspace/cwd from the "live surface" — all cmux reads are stubbed so the
    # unit tests never touch the host's real ~/.cmuxterm hook store. `roster` pins _is_roster (the test
    # env otherwise falls back to the host's real ~/.config/cmux-fleet/fleet.toml, breaking hermeticity).
    # `_live_session_for` is THE register live gate (codex P1): a truthy record means the surface is
    # CURRENTLY live; None means dead/stale -> register must refuse. Stub it so tests don't poll the host.
    rec = {"sessionId": session, "workspaceId": ws, "cwd": surf_cwd} if live else None
    monkeypatch.setattr(fleet, "_live_session_for", lambda surf: rec)
    monkeypatch.setattr(fleet, "ws_uuid_for_surface", lambda surf: ws)
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
    # the gate itself: a sessions[] record on the surface but agentLifecycle 'ended' -> not live.
    store = {"activeSessionsBySurface": {},
             "sessions": {"s1": {"surfaceId": "SURF-E", "sessionId": "S1",
                                 "agentLifecycle": "ended", "updatedAt": 5}}}
    monkeypatch.setattr(fleet, "_store", lambda: store)
    assert fleet._live_session_for("SURF-E") is None
    # active-index entry -> live (cmux says bound right now), resolved to the full record
    store["activeSessionsBySurface"] = {"SURF-E": {"sessionId": "S1"}}
    got = fleet._live_session_for("SURF-E")
    assert got and got.get("sessionId") == "S1"


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


def test_register_typeerror_repro_dict_launchcommand(fs, monkeypatch):
    # THE crash repro: a fully UNREGISTERED agent (no archive/live entry) whose cmux launchCommand is a
    # dict. The old code did re.search(pattern, dict) while deriving the role from the binding ->
    # 'expected string or bytes-like object, got dict'. _launchcmd coerces it now -> no crash, role
    # falls back to the label. Exercises the real recovery path (src is empty, so the binding IS parsed).
    store = {"activeSessionsBySurface": {"SURF-KG": {"sessionId": "SESS-KG"}},
             "sessions": {"s": {"surfaceId": "SURF-KG", "sessionId": "SESS-KG",
                                "agentLifecycle": "idle", "cwd": "/x", "workspaceId": "WS-KG",
                                "launchCommand": {"argv": ["claude"], "env": {"AGENT_ROLE": "kg"}}}}}
    monkeypatch.setattr(fleet, "_store", lambda: store)
    monkeypatch.setattr(fleet, "_tool_for_surface", lambda surf: "claude")
    monkeypatch.setattr(fleet, "ws_uuid_for_surface", lambda surf: "WS-KG")
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
