# tests/test_launch_guard.py — cmd_launch's live-label guard (registry/surface-invariant batch,
# 2026-07-03). register() is a bare live_put overwrite, so `fleet launch --label X` while X was still
# live silently orphaned the OLD surface with no trail at all (no "removed" event -- a worse trace than
# even the pre-flip rm bug). The guard refuses unless the prior row is clearly STALE (dead lifecycle +
# a recorded session, the same predicate `fleet ls` flags); --force is the operator override. Pure
# in-process units: config, lifecycle, and create_surface are stubbed -- create_surface doubles as the
# "got past the guard" tripwire.
import pytest

from cmux_fleet import cli as fleet


def _seed_live(fs, label, surf="S-OLD", session="claude-OLD"):
    fs.live_put(label, {"role": "worker", "kind": "child", "tool": "claude", "cwd": "/x",
                        "place": "tab", "group": "", "surface": surf, "session": session,
                        "plugins": [], "flags": [], "settings": "", "status": "live"})


def _launch(tmp_path, *extra):
    # ad-hoc (no roster needed), explicit --cwd so nothing touches the real ROOT
    return fleet.cmd_launch(["--adhoc", "dup-x", "--label", "dup-x", "--parent", "FAKE",
                             "--cwd", str(tmp_path), *extra])


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    # keep the host's real toml out, but carry a [role.adhoc] block so `--adhoc` resolves (5d: --adhoc
    # is an alias for the rostered adhoc role; --cwd below still overrides the home).
    monkeypatch.setattr(fleet, "load_config",
                        lambda: {"role": {"adhoc": {"cwd": "agents/ad-hoc", "claude": {}}}})


def test_launch_refuses_live_label(fs, rs, monkeypatch, tmp_path):
    _seed_live(fs, "dup-x")
    monkeypatch.setattr(rs, "lifecycle", lambda s: "idle")      # old surface genuinely live:
    monkeypatch.setattr(rs, "surface_has_live_pid", lambda s: True)  # ...non-terminal AND a live pid
    spawned = []
    monkeypatch.setattr(fleet, "create_surface", lambda *a: (spawned.append(a) or (None, None)))
    with pytest.raises(SystemExit) as ei:
        _launch(tmp_path)
    assert "already LIVE" in str(ei.value)
    assert not spawned                                          # refused BEFORE any surface was spawned
    assert fs.live_get("dup-x")["surface"] == "S-OLD"           # prior row untouched


def test_launch_refuses_pending_label_fail_closed(fs, rs, monkeypatch, tmp_path):
    # a pending row (surface present, no session bound yet, lifecycle empty) is NOT provably stale --
    # fail closed: refuse rather than orphan a surface that may be mid-boot.
    _seed_live(fs, "dup-x", session="")
    monkeypatch.setattr(rs, "lifecycle", lambda s: "")
    monkeypatch.setattr(fleet, "create_surface", lambda *a: (None, None))
    with pytest.raises(SystemExit) as ei:
        _launch(tmp_path)
    assert "already LIVE" in str(ei.value)


def test_launch_force_overrides_live_label(fs, rs, monkeypatch, tmp_path):
    _seed_live(fs, "dup-x")
    monkeypatch.setattr(rs, "lifecycle", lambda s: "idle")
    monkeypatch.setattr(rs, "surface_has_live_pid", lambda s: True)  # genuinely live -> --force is the override
    spawned = []
    monkeypatch.setattr(fleet, "create_surface", lambda *a: (spawned.append(a) or (None, None)))
    with pytest.raises(SystemExit) as ei:
        _launch(tmp_path, "--force")
    assert ei.value.code == 1                                   # exit came from the stubbed spawn...
    assert spawned                                              # ...i.e. the guard let --force through


def test_launch_proceeds_over_stale_label(fs, rs, monkeypatch, tmp_path):
    # dead lifecycle + a recorded session = the `fleet ls` STALE predicate -> relaunching is the normal
    # recovery move, no --force needed.
    _seed_live(fs, "dup-x")
    monkeypatch.setattr(rs, "lifecycle", lambda s: "ended")
    spawned = []
    monkeypatch.setattr(fleet, "create_surface", lambda *a: (spawned.append(a) or (None, None)))
    with pytest.raises(SystemExit) as ei:
        _launch(tmp_path)
    assert ei.value.code == 1                                   # past the guard, died at the stubbed spawn
    assert spawned


def test_launch_proceeds_over_dead_pid_running_ghost(fs, rs, monkeypatch, tmp_path):
    # round-2 gap (2026-07-06): a prior row FROZEN 'running' on a DEAD pid (SessionEnd-less brick) must
    # read STALE here too -- via the shared surface_has_live_agent, a dead pid means no live surface to
    # orphan, so relaunch overwrites the ghost row with NO --force (before this it hit a bogus
    # "already LIVE" and forced a manual `fleet rm` first).
    _seed_live(fs, "dup-x")
    monkeypatch.setattr(rs, "lifecycle", lambda s: "running")   # frozen non-terminal string...
    monkeypatch.setattr(rs, "surface_has_live_pid", lambda s: False)  # ...but the process is DEAD
    spawned = []
    monkeypatch.setattr(fleet, "create_surface", lambda *a: (spawned.append(a) or (None, None)))
    with pytest.raises(SystemExit) as ei:
        _launch(tmp_path)
    assert ei.value.code == 1                                   # past the guard (read stale), died at stub
    assert spawned
