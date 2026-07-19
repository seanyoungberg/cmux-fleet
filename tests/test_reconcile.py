# tests/test_reconcile.py — restore reconciliation (Ship 2). The reconcile-restore CLOSE path is the
# second fleet verb that can close a live cmux surface, so — like reap-surfaces — the classifier carries
# the safety burden: it must CLOSE only a surface it can DETERMINISTICALLY prove is a fleet husk (snapshot
# agent=nil + no live agent + not registered + fleet-origin) and NEVER a live agent or a human's shell.
import json

import pytest

from cmux_fleet import reconcile as rc, cli


# --- valid `cmux tree` UUIDs (must match _HUSK_UUID: 8-4-4-4-12 hex) ---
TRACKED = "aaaaaaaa-0000-0000-0000-000000000001"
LIVE_ORPHAN = "bbbbbbbb-0000-0000-0000-000000000002"
HUSK = "cccccccc-0000-0000-0000-000000000003"
HUMAN = "dddddddd-0000-0000-0000-000000000004"
DEAD_AGENT = "eeeeeeee-0000-0000-0000-000000000005"
WS_A = "f0000000-0000-0000-0000-0000000000aa"
WS_H = "f0000000-0000-0000-0000-0000000000bb"


def _tree(*surfaces):
    """Build `cmux tree` text (workspace + one terminal surface each) the pure parser accepts."""
    lines = ["window window:1 99999999-0000-0000-0000-000000000000 [current]"]
    for i, (surf, ws, title) in enumerate(surfaces, 1):
        lines.append(f"  workspace workspace:{i} {ws} \"ws{i}\"")
        lines.append(f"    surface surface:{i} {surf} [terminal] \"{title}\"")
    return "\n".join(lines)


def _snap(**per_surface):
    """{SURFACE_UPPER: rec} in the shape parse_snapshot emits (so _classify can consume it directly)."""
    out = {}
    for surf, r in per_surface.items():
        out[surf.upper()] = {"has_agent": r.get("has_agent", False), "was_running": r.get("was_running"),
                             "session": r.get("session", ""), "fleet_origin": r.get("fleet_origin", False),
                             "label": r.get("label", ""), "resume_id": r.get("resume_id", ""),
                             "directory": r.get("directory", ""), "title": r.get("title", "")}
    return out


@pytest.fixture
def seams(monkeypatch, fs):
    """Control the liveness seams _classify reads. Registry + archive are SEEDED into the real (isolated)
    STATE via fs.live_put/fs.archive_put — NOT monkeypatched — so archive_put/archive_get round-trips in
    the close path stay real. Only the store-backed predicates (surface_has_live_agent, _hook_fleet_origin)
    are patched, off two mutable sets the test fills."""
    state = {"live_surfaces": set(), "hook_origin": set()}
    from cmux_fleet import resolve as rs   # ASSESS re-home: surface_has_live_agent moved state->resolve (Ship 5)
    monkeypatch.setattr(rs, "surface_has_live_agent", lambda s: s.upper() in {x.upper() for x in state["live_surfaces"]})
    monkeypatch.setattr(rc, "_hook_fleet_origin", lambda s: s.upper() in {x.upper() for x in state["hook_origin"]})
    state["fs"] = fs
    return state


# ── parse_snapshot ────────────────────────────────────────────────────────────────────────────
def test_parse_snapshot_keys_on_panel_id_not_stable_id(tmp_path):
    doc = {"windows": [{"tabManager": {"workspaces": [{"panels": [
        {"id": "11111111-0000-0000-0000-000000000001",
         "stableSurfaceId": "99999999-9999-9999-9999-999999999999",
         "directory": "/x", "title": "t",
         "terminal": {"wasAgentRunning": None, "scrollback": "boot\nAGENT_LABEL=foo claude --resume abc",
                      "agent": None}}]}]}}]}
    p = tmp_path / "session.json"
    p.write_text(json.dumps(doc))
    out = rc.parse_snapshot(str(p))
    # keyed on the PANEL id (== the live surface id), never the stableSurfaceId
    assert "11111111-0000-0000-0000-000000000001".upper() in out
    assert "99999999-9999-9999-9999-999999999999".upper() not in out
    rec = out["11111111-0000-0000-0000-000000000001".upper()]
    assert rec["has_agent"] is False and rec["fleet_origin"] is True and rec["label"] == "foo"


def test_parse_snapshot_reads_agent_and_was_running(tmp_path):
    doc = {"windows": [{"tabManager": {"workspaces": [{"panels": [
        {"id": "22222222-0000-0000-0000-000000000002", "terminal": {
            "wasAgentRunning": True, "scrollback": "",
            "agent": {"sessionId": "abcdef01-2345-6789-abcd-ef0123456789", "kind": "claude"}}}]}]}}]}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(doc))
    rec = rc.parse_snapshot(str(p))["22222222-0000-0000-0000-000000000002".upper()]
    assert rec["has_agent"] is True and rec["was_running"] is True
    assert rec["session"] == "abcdef01-2345-6789-abcd-ef0123456789"


def test_parse_snapshot_missing_file_is_empty():
    assert rc.parse_snapshot("/no/such/session.json") == {}


def test_resume_and_husk_sets():
    snap = _snap(
        a={"has_agent": True, "was_running": None},     # unknown => WOULD resume
        b={"has_agent": True, "was_running": True},      # running => WOULD resume
        c={"has_agent": True, "was_running": False},     # idle => NOT resumed
        d={"has_agent": False})                          # bare shell => husk-set
    assert set(rc.resume_set(snap)) == {"A", "B"}
    assert set(rc.husk_set(snap)) == {"D"}


# ── _classify: the safety-critical bucketing ────────────────────────────────────────────────────
def test_tracked_surface_is_kept(seams):
    seams["fs"].live_put("boss", {"surface": TRACKED, "workspace": WS_A})
    tree = _tree((TRACKED, WS_A, "boss"))
    b = rc._classify(tree, _snap())
    assert [r["surface"] for r in b["tracked"]] == [TRACKED]
    assert b["husk"] == [] and b["human_shell"] == []


def test_live_agent_not_in_registry_is_resume_orphan_never_closed(seams):
    seams["live_surfaces"] = {LIVE_ORPHAN}
    tree = _tree((LIVE_ORPHAN, WS_A, "resumed"))
    b = rc._classify(tree, _snap(**{LIVE_ORPHAN: {"has_agent": True, "was_running": None}}))
    assert [r["surface"] for r in b["resume_orphan"]] == [LIVE_ORPHAN]
    assert b["husk"] == []                                # a LIVE agent is NEVER a husk


def test_deterministic_husk_is_flagged_when_fleet_origin(seams):
    tree = _tree((HUSK, WS_A, "…/ad-hoc/gone"))
    snap = _snap(**{HUSK: {"has_agent": False, "fleet_origin": True, "label": "gone", "resume_id": "s1"}})
    b = rc._classify(tree, snap)
    assert [r["surface"] for r in b["husk"]] == [HUSK]
    assert b["human_shell"] == []


def test_human_shell_is_never_a_husk_without_fleet_origin(seams):
    # snapshot agent=nil + no live + not registered, but NO fleet-origin proof: the SAFETY FLOOR.
    tree = _tree((HUMAN, WS_H, "~/tapestry"))
    b = rc._classify(tree, _snap(**{HUMAN: {"has_agent": False, "fleet_origin": False}}))
    assert b["husk"] == []                                # <-- the whole safety point
    assert [r["surface"] for r in b["human_shell"]] == [HUMAN]


def test_hook_launchcommand_origin_promotes_a_husk(seams):
    # scrollback scrolled past the boot line (fleet_origin False), but a hook record's launchCommand
    # proves fleet-origin -> still a deterministic husk (the SessionEnd-less death class).
    seams["hook_origin"] = {HUSK}
    tree = _tree((HUSK, WS_H, "…/ad-hoc/gone"))
    b = rc._classify(tree, _snap(**{HUSK: {"has_agent": False, "fleet_origin": False}}))
    assert [r["surface"] for r in b["husk"]] == [HUSK]


def test_managed_workspace_promotes_a_husk(seams):
    # an archived member still pins its workspace as fleet-managed -> a bare-shell surface there is a husk.
    seams["fs"].archive_put("gone", {"workspace": WS_A, "last_session": "s9"})
    tree = _tree((HUSK, WS_A, "…/ad-hoc/gone"))
    b = rc._classify(tree, _snap(**{HUSK: {"has_agent": False, "fleet_origin": False}}))
    assert [r["surface"] for r in b["husk"]] == [HUSK]


def test_dead_agent_record_is_resume_orphan_not_husk(seams):
    # snapshot HAS an agent record but no live pid: cmux may still resume it -> flag, do not sweep.
    tree = _tree((DEAD_AGENT, WS_A, "dead"))
    b = rc._classify(tree, _snap(**{DEAD_AGENT: {"has_agent": True, "was_running": None, "session": "s5"}}))
    assert [r["surface"] for r in b["resume_orphan"]] == [DEAD_AGENT]
    assert b["husk"] == []


def test_resume_orphan_carries_adopt_hint(seams):
    seams["fs"].archive_put("parked", {"last_session": "abcdef01-2345-6789-abcd-ef0123456789"})
    seams["live_surfaces"].add(LIVE_ORPHAN)
    tree = _tree((LIVE_ORPHAN, WS_A, "resumed"))
    snap = _snap(**{LIVE_ORPHAN: {"has_agent": True, "session": "abcdef01-2345-6789-abcd-ef0123456789"}})
    b = rc._classify(tree, snap)
    assert b["resume_orphan"][0]["adopt_label"] == "parked"


def test_no_snapshot_never_closes(seams):
    # with no snapshot (agent=nil evidence absent) a non-live non-registered surface is 'unknown', never husk.
    tree = _tree((HUSK, WS_A, "x"))
    b = rc._classify(tree, {})
    assert b["husk"] == [] and [r["surface"] for r in b["unknown"]] == [HUSK]


# ── reconcile_restore: dry-run vs close ─────────────────────────────────────────────────────────
def test_reconcile_dryrun_closes_nothing(seams, monkeypatch):
    tree = _tree((HUSK, WS_A, "…/ad-hoc/gone"))
    monkeypatch.setattr(cli, "cmuxq", lambda *a, **k: tree)
    monkeypatch.setattr(rc, "parse_snapshot", lambda p: _snap(**{HUSK: {"has_agent": False, "fleet_origin": True, "label": "gone"}}))
    monkeypatch.setattr(rc, "session_snapshot_path", lambda *a, **k: "/x")
    rep = rc.reconcile_restore(close=False, force=True)
    assert rep["husks"] == [HUSK] and rep["closed"] == []      # flagged, not closed


def test_reconcile_close_archives_first_then_closes(seams, monkeypatch, fs):
    calls = []
    # a fake cmuxq: the first tree read shows the husk; after close-workspace it is GONE (verified).
    state = {"closed": False}

    def fake_cmuxq(*a, **k):
        if a[0] == "tree":
            return "" if state["closed"] else _tree((HUSK, WS_A, "…/ad-hoc/gone"))
        if a[0] in ("close-workspace", "close-surface"):
            calls.append(a[0]); state["closed"] = True
            return ""
        return ""
    monkeypatch.setattr(cli, "cmuxq", fake_cmuxq)
    monkeypatch.setattr(rc, "parse_snapshot",
                        lambda p: _snap(**{HUSK: {"has_agent": False, "fleet_origin": True,
                                                  "label": "gone", "resume_id": "s7"}}))
    monkeypatch.setattr(rc, "session_snapshot_path", lambda *a, **k: "/x")
    rep = rc.reconcile_restore(close=True, force=True)
    assert rep["closed"] == [HUSK] and rep["residue"] == []
    assert "close-workspace" in calls                         # sole-in-ws husk -> close the workspace
    assert fs.archive_get("gone") is not None                 # ARCHIVE-FIRST: revive-able
    assert fs.archive_get("gone")["via"] == "reconcile-restore"


def test_reconcile_close_reports_residue_when_cmux_refuses(seams, monkeypatch):
    # cmuxq 'closes' but the tree still shows the surface (a refusal cmuxq swallowed) -> residue, not closed.
    tree = _tree((HUSK, WS_A, "…/ad-hoc/gone"))
    monkeypatch.setattr(cli, "cmuxq", lambda *a, **k: tree)
    monkeypatch.setattr(rc, "parse_snapshot", lambda p: _snap(**{HUSK: {"has_agent": False, "fleet_origin": True, "label": "gone"}}))
    monkeypatch.setattr(rc, "session_snapshot_path", lambda *a, **k: "/x")
    rep = rc.reconcile_restore(close=True, force=True)
    assert rep["closed"] == [] and rep["residue"] == [HUSK]


def test_reconcile_debounces(monkeypatch):
    monkeypatch.setattr(cli, "cmuxq", lambda *a, **k: "")
    monkeypatch.setattr(rc, "parse_snapshot", lambda p: {})
    monkeypatch.setattr(rc, "session_snapshot_path", lambda *a, **k: "")
    rc._last_run["ts"] = 0.0
    first = rc.reconcile_restore(close=False, force=True)
    assert "skipped" not in first
    second = rc.reconcile_restore(close=False)                 # within DEBOUNCE_S, not forced
    assert second.get("skipped") == "debounced"


def test_hook_fleet_origin_matches_the_REAL_launchcommand_shape(monkeypatch):
    """Regression: a real fleet launchCommand carries NO AGENT_LABEL= (that's an env var, not argv) — its
    fleet marker is the `--plugin-dir …/cmux-fleet` argument. A synthetic launchCommand hid this; the
    test-env crash-test caught it. Route through resolve.records (the one store reader)."""
    from cmux_fleet import resolve as rs
    real = {"arguments": ["/Users/x/.local/bin/claude", "--setting-sources", "user,local",
                          "--plugin-dir", "/Users/x/marketplace/plugins/cmux-fleet"],
            "launcher": "claude", "source": "environment", "workingDirectory": "/x"}
    monkeypatch.setattr(rs, "records", lambda s, st=None: [{"launchCommand": real}])
    assert rc._hook_fleet_origin("anysurf") is True
    # a NON-fleet claude (bare, no fleet plugin-dir) is NOT fleet-origin -> never swept
    monkeypatch.setattr(rs, "records", lambda s, st=None: [{"launchCommand": {"arguments": ["/bin/claude", "--resume", "x"]}}])
    assert rc._hook_fleet_origin("anysurf") is False
    monkeypatch.setattr(rs, "records", lambda s, st=None: [])
    assert rc._hook_fleet_origin("anysurf") is False
