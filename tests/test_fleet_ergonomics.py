# tests/test_fleet_ergonomics.py — the held-dev fleet-ergonomics batch (2026-07-18):
#   FIX 1  self-recycle self-detection: a self-targeted recycle auto-forces so the quiet-gate (which the
#          caller's own running turn can never clear from inside) does not deadlock to the 180s ABORT.
#   FIX 2  `fleet reparent <label> <parent|none>`: surgical registry-parent edit, cross-conductor guarded.
#   FIX 3  recycle writes back the RESOLVED (toml-authoritative) plugin set so the registry stops lying.
import pytest

from cmux_fleet import cli as fleet
from cmux_fleet import helpers as fh


# ============================ FIX 1 — self-recycle self-detection ============================
def _minimal_payload(force):
    return {"label": "self", "surface": "SELFSURF", "send_cmd": "cd /x && claude", "mode": "resume",
            "tool": "claude", "force": force, "prime": None, "old_session": "s", "cwd": "/x",
            "plugins": [], "provider": "", "provider_needs_refresh": ""}


def test_self_recycle_auto_forces(fs, monkeypatch, capsys):
    # the exact deadlock: recycling YOURSELF. The gate can never clear from inside your own turn, so the
    # fix auto-forces. Assert the force flowed into the payload (-> the detached gate short-circuits).
    fs.live_put("self", {"role": "worker", "kind": "child", "tool": "claude", "surface": "SELFSURF",
                         "cwd": "/x", "session": "claude-s", "plugins": []})
    monkeypatch.setenv("CMUX_SURFACE_ID", "SELFSURF")
    monkeypatch.setattr(fleet, "_resolve_recycle_provider", lambda *a: (None, "", ""))
    seen = {}

    def spy(label, entry, caller, add_plugin, mode, session, force, *a, **k):
        seen["force"] = force
        return _minimal_payload(force)

    monkeypatch.setattr(fleet, "_recycle_plan", spy)
    fleet.cmd_recycle(["--dry-run"])
    out = capsys.readouterr().out
    assert seen["force"] is True                              # auto-forced -> the detached gate won't block
    assert "self-recycle: forcing" in out


def test_external_recycle_does_not_force(fs, monkeypatch):
    # CONTROL: recycling ANOTHER agent from a different surface is unchanged — force stays whatever --force
    # said (here: not passed -> False). The auto-force is scoped strictly to the self case.
    fs.live_put("other", {"role": "worker", "kind": "child", "tool": "claude", "surface": "OTHERSURF",
                          "cwd": "/x", "session": "claude-o", "plugins": []})
    monkeypatch.setenv("CMUX_SURFACE_ID", "MYSURF")           # caller != other's surface
    monkeypatch.setattr(fleet, "_resolve_recycle_provider", lambda *a: (None, "", ""))
    seen = {}

    def spy(label, entry, caller, add_plugin, mode, session, force, *a, **k):
        seen["force"] = force
        return _minimal_payload(force)

    monkeypatch.setattr(fleet, "_recycle_plan", spy)
    fleet.cmd_recycle(["other", "--dry-run"])
    assert seen["force"] is False


def test_forced_gate_returns_immediately(rs, monkeypatch):
    # RECEIPT that the auto-force actually avoids the deadlock: a forced quiet-gate returns True with NO
    # wait, even on a surface that reads 'running' forever (which a self-recycle's own turn always does).
    slept = []
    monkeypatch.setattr(fleet.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(rs, "lifecycle", lambda surf: "running")
    assert fleet._quiet_gate("SELFSURF", 180, force=True) is True
    assert slept == []                                        # no 180s burn


# ================================ FIX 2 — fleet reparent verb ================================
def _child(fs, label="kid", parent="condA", **extra):
    fs.live_put(label, {"role": "worker", "kind": "child", "tool": "claude", "surface": label.upper() + "S",
                        "cwd": "/x", "place": "tab", "parent": parent, "session": f"claude-{label}",
                        "plugins": ["p1"], **extra})


def _cond(fs, label, surface):
    fs.live_put(label, {"role": "lead", "kind": "conductor", "tool": "claude", "surface": surface,
                        "group": label, "session": f"claude-{label}"})


def test_reparent_to_none_is_top_level(fs):
    _child(fs, "kid", "condA"); _cond(fs, "condA", "SA")
    fleet.cmd_reparent(["kid", "none"])
    e = fs.live_get("kid")
    assert fs.is_top_level(e)                                 # parent None/absent -> top-level
    assert e.get("parent") is None
    assert e["session"] == "claude-kid" and e["plugins"] == ["p1"]   # every other field preserved


def test_reparent_to_a_label(fs):
    _child(fs, "kid", "condA"); _cond(fs, "condA", "SA"); _cond(fs, "condB", "SB")
    fleet.cmd_reparent(["kid", "condB"])
    assert fs.live_get("kid")["parent"] == "condB"


def test_reparent_unknown_parent_refused(fs):
    _child(fs, "kid", "condA"); _cond(fs, "condA", "SA")
    with pytest.raises(SystemExit) as ei:
        fleet.cmd_reparent(["kid", "ghost"])
    assert "no known agent labeled 'ghost'" in str(ei.value)
    assert fs.live_get("kid")["parent"] == "condA"            # untouched


def test_reparent_self_refused(fs):
    _child(fs, "kid", "condA")
    with pytest.raises(SystemExit) as ei:
        fleet.cmd_reparent(["kid", "kid"])
    assert "its own parent" in str(ei.value)


def test_reparent_no_live_label_refused(fs):
    with pytest.raises(SystemExit) as ei:
        fleet.cmd_reparent(["ghost", "none"])
    assert "no LIVE label" in str(ei.value)


def test_reparent_guard_refuses_other_conductors_child(fs, monkeypatch):
    _child(fs, "kid", "condA"); _cond(fs, "condA", "SA"); _cond(fs, "condB", "SB")
    monkeypatch.setenv("CMUX_SURFACE_ID", "SB")              # condB, NOT the parent
    with pytest.raises(SystemExit) as ei:
        fleet.cmd_reparent(["kid", "none"])
    assert "REFUSED" in str(ei.value) and "condA" in str(ei.value)
    assert fs.live_get("kid")["parent"] == "condA"           # not reparented
    pend = fs.inbox_pending("SA", "peer")                     # parent notified either way
    assert pend and "reparent" in pend[-1]["body"] and "kid" in pend[-1]["body"]


def test_reparent_guard_force_allows_and_notifies(fs, monkeypatch):
    _child(fs, "kid", "condA"); _cond(fs, "condA", "SA"); _cond(fs, "condB", "SB")
    monkeypatch.setenv("CMUX_SURFACE_ID", "SB")
    fleet.cmd_reparent(["kid", "none", "--force"])
    assert fs.is_top_level(fs.live_get("kid"))               # forced through
    pend = fs.inbox_pending("SA", "peer")
    assert pend and "reparented your child 'kid'" in pend[-1]["body"]


def test_reparent_own_child_allowed_no_notify(fs, monkeypatch):
    _child(fs, "kid", "condA"); _cond(fs, "condA", "SA")
    monkeypatch.setenv("CMUX_SURFACE_ID", "SA")              # the parent itself
    fleet.cmd_reparent(["kid", "none"])
    assert fs.is_top_level(fs.live_get("kid"))
    assert not fs.inbox_pending("SA", "peer")                # no self-notification


def test_reparent_anonymous_operator_allowed(fs, monkeypatch):
    _child(fs, "kid", "condA"); _cond(fs, "condA", "SA"); _cond(fs, "condB", "SB")
    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)     # CLI operator, no surface
    fleet.cmd_reparent(["kid", "condB"])
    assert fs.live_get("kid")["parent"] == "condB"


# ==================== FIX 3 — recycle writes back the RESOLVED plugins =======================
# a toml where role 'r' resolves to THREE plugins (floor p1,p2 ∪ role p3); the registry row records one.
ROSTER_CFG = {"defaults": {"tool": "claude"},
              "tool": {"claude": {"plugins": ["p1", "p2"]}},
              "role": {"r": {"claude": {"plugins": ["p3"]}}}}


def test_recycle_plan_uses_resolved_roster_plugins(fs, monkeypatch):
    monkeypatch.setattr(fleet, "load_config", lambda: ROSTER_CFG)
    monkeypatch.setattr(fleet, "_compose_recycle_cmd", lambda *a, **k: ("cd /x && claude", ""))
    entry = {"surface": "S", "session": "claude-old", "role": "r", "tool": "claude", "plugins": ["p1"]}
    payload = fleet._recycle_plan("lbl", entry, [], [], "resume", "", False, None, False)
    assert payload["plugins"] == ["p1", "p2", "p3"]           # the RESOLVED set, not the stale recorded [p1]


def test_recycle_plan_offroster_keeps_recorded_plugins(fs, monkeypatch):
    monkeypatch.setattr(fleet, "load_config", lambda: ROSTER_CFG)     # 'adhoc-x' is NOT a roster role here
    monkeypatch.setattr(fleet, "_compose_recycle_cmd", lambda *a, **k: ("cd /x && claude", ""))
    entry = {"surface": "S", "session": "claude-old", "role": "adhoc-x", "tool": "claude", "plugins": ["p1"]}
    payload = fleet._recycle_plan("lbl", entry, [], ["extra"], "resume", "", False, None, False)
    assert payload["plugins"] == ["p1", "extra"]             # recorded ∪ --plugin add (reproduced from binding)


def test_recycle_exec_writes_resolved_plugins_to_row(fs, rs, monkeypatch):
    # the ACCEPTANCE test: a recycle whose resolved set differs from the recorded set UPDATES the row.
    # Lazy (codex) path = the shortest route to the writeback; the cmux/respawn seams are stubbed.
    fs.live_put("w", {"role": "r", "kind": "child", "tool": "codex", "surface": "S",
                      "session": "codex-old", "plugins": ["p1"], "cwd": "/x"})
    p = {"label": "w", "surface": "S", "send_cmd": "cd /x && codex", "mode": "resume", "tool": "codex",
         "force": True, "prime": None, "old_session": "old", "cwd": "/x",
         "plugins": ["p1", "p2", "p3"], "provider": "", "provider_needs_refresh": ""}
    monkeypatch.setattr(fleet.time, "sleep", lambda *a: None)
    monkeypatch.setattr(fleet, "cmuxq", lambda *a, **k: "OK")
    monkeypatch.setattr(fleet, "_surface_pids", lambda s: [])
    monkeypatch.setattr(rs, "lifecycle", lambda s: "ended")   # -> _confirmed_gone True (old agent gone)
    monkeypatch.setattr(fleet, "_graceful_close", lambda *a, **k: None)
    monkeypatch.setattr(fleet, "_exec_launch_enabled", lambda: True)
    monkeypatch.setattr(fleet, "_exec_launch", lambda *a, **k: None)
    assert fleet._recycle_exec_one(p) == 0
    assert fs.live_get("w")["plugins"] == ["p1", "p2", "p3"]  # the row now matches what launched


# ==================== FIX 4 (bonus) — surface-prefix handle resolution =======================
FULL_A = "0866393D-1111-2222-3333-444444444444"
FULL_B = "0866393D-9999-8888-7777-666666666666"       # shares the 8-char ls prefix with A
FULL_C = "FEEDFACE-0000-0000-0000-000000000000"


def _live_surface(fs, label, surface):
    fs.live_put(label, {"role": "worker", "kind": "child", "tool": "claude", "surface": surface,
                        "session": f"claude-{label}"})


def test_short_ls_prefix_resolves_to_full_uuid(fs):
    # THE reported case: `fleet ls` shows the 8-char short surface; it must resolve to the full UUID so it
    # is copy-pasteable into drive-child (no more fishing the full id out of fleet.json).
    _live_surface(fs, "kid", FULL_A)
    assert fh._resolve_surface_handle("0866393D") == FULL_A
    assert fh._resolve_surface_handle("0866393d") == FULL_A          # case-insensitive


def test_ambiguous_prefix_errors_clearly(fs):
    _live_surface(fs, "kidA", FULL_A); _live_surface(fs, "kidB", FULL_B)
    with pytest.raises(SystemExit) as ei:
        fh._resolve_surface_handle("0866393D")                       # prefixes BOTH A and B
    assert "AMBIGUOUS" in str(ei.value)
    # one more character disambiguates
    assert fh._resolve_surface_handle("0866393D-1") == FULL_A


def test_full_uuid_passes_through(fs):
    _live_surface(fs, "kid", FULL_A)
    assert fh._resolve_surface_handle(FULL_A) == FULL_A              # exact (unique) match -> itself


def test_unknown_surface_passes_through_untouched(fs):
    # a full UUID cmux knows but the registry doesn't (a bare surface) -> passthrough; cmux errors if bad.
    _live_surface(fs, "kid", FULL_A)
    assert fh._resolve_surface_handle(FULL_C) == FULL_C
