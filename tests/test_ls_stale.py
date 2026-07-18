# tests/test_ls_stale.py — `fleet ls` STALE detection is pid-aware (round 2, 2026-07-06). A frozen
# 'running' record on a DEAD pid (the SessionEnd-less brick) must render STALE, not a false 'live' --
# the "ls lies" symptom Berg hit at the very start of the incident. Pure unit: the surface's lifecycle
# and pid-liveness are stubbed so nothing touches the host's ~/.cmuxterm store.
import json

from cmux_fleet import cli as fleet


def _seed(fs, label, surf, session="claude-x", **extra):
    fs.live_put(label, {"role": "r", "kind": "child", "tool": "claude", "surface": surf,
                        "session": session, "status": "live", **extra})


def _rows(out):
    # each member row is '  <label> ...'; key by the leading label token
    return {ln.split()[0]: ln for ln in out.splitlines() if ln.startswith("  ") and ln.split()}


def test_ls_flags_dead_pid_running_ghost_stale(fs, rs, monkeypatch, capsys):
    _seed(fs, "livekid", "S-LIVE")             # genuinely live: idle + a live pid
    _seed(fs, "ghostkid", "S-GHOST")           # frozen 'running' on a DEAD pid (the brick)
    _seed(fs, "endedkid", "S-END")             # terminal lifecycle (already handled pre-fix)
    life = {"S-LIVE": "idle", "S-GHOST": "running", "S-END": "ended"}
    pid = {"S-LIVE": True, "S-GHOST": False, "S-END": False}
    monkeypatch.setattr(rs, "lifecycle", lambda s: life.get(s, ""))
    monkeypatch.setattr(rs, "surface_has_live_pid", lambda s: pid.get(s, False))
    fleet.cmd_ls([])
    rows = _rows(capsys.readouterr().out)
    assert "STALE" in rows["ghostkid"]         # THE fix: dead-pid 'running' ghost reads STALE, not 'live'
    assert "running" in rows["ghostkid"]       # ...while still honestly showing cmux's frozen string
    assert "STALE" in rows["endedkid"]         # terminal still STALE
    assert "STALE" not in rows["livekid"]      # genuinely-live agent unaffected (no regression)


def test_ls_pending_row_unbound_shows_pending_not_stale(fs, rs, monkeypatch, capsys):
    # a lazily-registered row with NO session bound yet (codex pre-first-turn) reads 'pending', not STALE,
    # even though it has no live agent -- the session-recorded distinction is preserved by the fix.
    _seed(fs, "pendingkid", "S-PEND", session="")
    monkeypatch.setattr(rs, "lifecycle", lambda s: "")
    monkeypatch.setattr(rs, "surface_has_live_pid", lambda s: False)
    fleet.cmd_ls([])
    rows = _rows(capsys.readouterr().out)
    assert "pending" in rows["pendingkid"] and "STALE" not in rows["pendingkid"]


def test_ls_json_emits_reconciled_rows(fs, rs, monkeypatch, capsys):
    # --json carries the SAME reconciliation as the text table: a dead-pid 'running' ghost reads STALE,
    # a genuinely-live agent reads live, and cmux's raw lifecycle string is preserved honestly.
    _seed(fs, "livekid", "S-LIVE")
    _seed(fs, "ghostkid", "S-GHOST")
    monkeypatch.setattr(rs, "lifecycle", lambda s: {"S-LIVE": "idle", "S-GHOST": "running"}.get(s, ""))
    monkeypatch.setattr(rs, "surface_has_live_pid", lambda s: {"S-LIVE": True, "S-GHOST": False}.get(s, False))
    fleet.cmd_ls(["--json", "--scope", "all"])
    data = json.loads(capsys.readouterr().out)                   # --json is machine output, no text table
    by = {r["label"]: r for r in data["live"]}
    assert by["ghostkid"]["status"] == "STALE"
    assert by["ghostkid"]["lifecycle"] == "running"              # honest cmux string, not masked
    assert by["livekid"]["status"] == "live"
    assert data["scope"] == "all"
