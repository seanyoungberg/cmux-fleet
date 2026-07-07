# tests/test_rm_kill_archive.py — `fleet rm` teardown semantics. History: force-archive-on-kill
# (2026-07-02 recovery-safety batch) made --kill write an archive row before tearing the surface down;
# the registry/surface-invariant batch (2026-07-03) then FLIPPED the default -- bare `rm` is now the
# close+archive path itself (a bare rm silently abandoning a still-live surface was the book-keeper
# zombie incident), --detach is the explicit opt-in for the old soft drop-row-only behavior, a
# mid-turn ('running') surface refuses without --force, and --kill remains the alias that also tears
# down a worktree. Pure units against the throwaway $CMUX_STATE_DIR; cmux reads (cmuxq /
# _resume_binding / _pid_for_surface / fs.lifecycle) are stubbed.
import json

import pytest

from cmux_fleet import cli as fleet


def _stub_cmux(monkeypatch, fs, lifecycle="idle", binding=None, calls=None, has_pid=True):
    """The standard cmd_rm stub set: no real cmux, a pinned lifecycle, an optional call recorder.
    `has_pid` models whether a LIVE process backs the surface (default True -- a real live agent always
    does); the mid-turn refuse is now pid-aware ('running' AND a live pid), so pass has_pid=False to
    model the frozen dead-pid 'running' ghost that must NOT block a plain `rm`."""
    monkeypatch.setattr(fleet, "cmuxq",
                        (lambda *a: (calls.append(a) or "")) if calls is not None else (lambda *a: ""))
    monkeypatch.setattr(fleet, "_pid_for_surface", lambda s: None)
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: binding or {})
    monkeypatch.setattr(fs, "lifecycle", lambda s: lifecycle)
    monkeypatch.setattr(fs, "surface_has_live_pid", lambda s: has_pid)


def _seed(fs, label, surf, session="claude-OLD", **extra):
    fs.live_put(label, {"role": "worker", "kind": "child", "tool": "claude", "cwd": "/x",
                        "place": "tab", "group": "", "surface": surf, "session": session,
                        "plugins": [], "flags": [], "settings": "", "status": "live", **extra})


def test_rm_kill_force_archives_with_known_session(fs, monkeypatch):
    _seed(fs, "w1", "S1")
    _stub_cmux(monkeypatch, fs, binding={"checkpoint_id": "NEW-CHECKPOINT"})
    fleet.cmd_rm(["w1", "--kill"])
    assert fs.live_get("w1") is None                       # gone from live
    arch = fs.archive_get("w1")
    assert arch is not None                                 # ...but NOT vanished -- archived for recovery
    assert arch["last_session"] == "NEW-CHECKPOINT"          # prefers cmux's ground-truth checkpoint


def test_rm_kill_archives_with_empty_marker_when_no_session_known(fs, monkeypatch):
    # a wedged agent that never bound a session must still stay KILLABLE (no refuse-if-uncaptured) --
    # it just archives with an empty/pending last_session ("maybe-unresumable", not "vanished").
    _seed(fs, "w2", "S2", session="")
    _stub_cmux(monkeypatch, fs)
    fleet.cmd_rm(["w2", "--kill"])
    assert fs.live_get("w2") is None
    arch = fs.archive_get("w2")
    assert arch is not None
    assert arch["last_session"] == ""


def test_rm_kill_closes_surface_and_logs_via_kill(fs, monkeypatch):
    _seed(fs, "w3", "S3", session="")
    calls = []
    _stub_cmux(monkeypatch, fs, calls=calls)
    fleet.cmd_rm(["w3", "--kill"])
    assert ("close-surface", "--surface", "S3") in calls
    events = [json.loads(l) for l in open(fs.LOG) if l.strip()]
    archived = [e for e in events if e["event"] == "archived" and e["label"] == "w3"]
    assert archived and archived[0].get("via") == "kill"


# --- the default flip: bare `rm` IS the close+archive path now --------------------------------------
def test_rm_default_closes_and_archives(fs, monkeypatch):
    # INVERTED contract (was test_rm_without_kill_does_not_archive): bare `rm` now closes the surface
    # AND archives a recovery row -- the exact path that used to abandon a still-live surface.
    _seed(fs, "w4", "S4")
    calls = []
    _stub_cmux(monkeypatch, fs, binding={"checkpoint_id": "CKPT"}, calls=calls)
    fleet.cmd_rm(["w4"])
    assert fs.live_get("w4") is None
    arch = fs.archive_get("w4")
    assert arch is not None and arch["last_session"] == "CKPT"   # recoverable: fleet revive w4
    assert ("close-surface", "--surface", "S4") in calls          # surface actually torn down
    events = [json.loads(l) for l in open(fs.LOG) if l.strip()]
    archived = [e for e in events if e["event"] == "archived" and e["label"] == "w4"]
    assert archived and archived[0].get("via") == "rm"


def test_rm_refuses_running_surface_without_force(fs, monkeypatch):
    # the flip's own footgun-guard: a mid-turn surface refuses (synchronously -- no async quiet-gate).
    _seed(fs, "w5", "S5")
    calls = []
    _stub_cmux(monkeypatch, fs, lifecycle="running", calls=calls)
    with pytest.raises(SystemExit) as ei:
        fleet.cmd_rm(["w5"])
    assert "mid-turn" in str(ei.value)
    assert fs.live_get("w5") is not None                          # nothing removed
    assert fs.archive_get("w5") is None                           # nothing archived
    assert ("close-surface", "--surface", "S5") not in calls      # surface untouched


def test_rm_dead_pid_running_ghost_not_refused(fs, monkeypatch):
    # round-2 gap (2026-07-06): a FROZEN 'running' record on a DEAD pid (SessionEnd-less brick) is NOT
    # mid-turn -- there's no live work to interrupt -- so a plain `rm` must proceed (close + archive),
    # NOT refuse. Before this, the dead ghost matched lifecycle=='running' and forced a needless --force.
    _seed(fs, "w5b", "S5B")
    calls = []
    _stub_cmux(monkeypatch, fs, lifecycle="running", calls=calls, has_pid=False)  # frozen string, dead pid
    fleet.cmd_rm(["w5b"])                                          # no --force needed
    assert fs.live_get("w5b") is None                             # removed
    assert fs.archive_get("w5b") is not None                      # archived for recovery
    assert ("close-surface", "--surface", "S5B") in calls         # surface actually closed


def test_rm_force_closes_running_surface(fs, monkeypatch):
    _seed(fs, "w6", "S6")
    calls = []
    _stub_cmux(monkeypatch, fs, lifecycle="running", calls=calls)
    fleet.cmd_rm(["w6", "--force"])
    assert fs.live_get("w6") is None
    assert fs.archive_get("w6") is not None
    assert ("close-surface", "--surface", "S6") in calls


def test_rm_detach_reproduces_old_soft_behavior(fs, monkeypatch):
    # --detach = the OLD bare-rm behavior exactly: drop the row, never touch the surface, no archive
    # write. lifecycle pinned 'running' to prove detach also skips the mid-turn guard (it closes nothing).
    _seed(fs, "w7", "S7")
    calls = []
    _stub_cmux(monkeypatch, fs, lifecycle="running", calls=calls)
    fleet.cmd_rm(["w7", "--detach"])
    assert fs.live_get("w7") is None
    assert fs.archive_get("w7") is None
    assert ("close-surface", "--surface", "S7") not in calls
    events = [json.loads(l) for l in open(fs.LOG) if l.strip()]
    assert not [e for e in events if e["event"] == "archived" and e["label"] == "w7"]
    removed = [e for e in events if e["event"] == "removed" and e["label"] == "w7"]
    assert removed and removed[0].get("detached") is True


def test_rm_detach_and_kill_are_contradictory(fs, monkeypatch):
    _seed(fs, "w8", "S8")
    _stub_cmux(monkeypatch, fs)
    with pytest.raises(SystemExit):
        fleet.cmd_rm(["w8", "--detach", "--kill"])
    assert fs.live_get("w8") is not None                          # refused before touching anything


# --- worktree teardown stays behind --kill (the one thing distinguishing the alias now) --------------
def test_rm_kill_still_tears_down_worktree(fs, monkeypatch):
    from cmux_fleet import worktree as wt
    _seed(fs, "w9", "S9", worktree={"repo": "/r", "path": "/r/.worktrees/w9", "branch": "fleet/w9"})
    _stub_cmux(monkeypatch, fs)
    torn = []
    monkeypatch.setattr(wt, "teardown",
                        lambda repo, path, label, wip_commit_flag=False, force=False:
                        (torn.append((repo, path, label)) or (True, "removed")))
    fleet.cmd_rm(["w9", "--kill"])
    assert torn == [("/r", "/r/.worktrees/w9", "w9")]


def test_rm_default_does_not_tear_down_worktree(fs, monkeypatch):
    # bare rm closes+archives but leaves the tree alone: `fleet worktree clean` is the dedicated,
    # dirty-guarded verb (the incident was never about worktrees leaking, only surfaces).
    from cmux_fleet import worktree as wt
    _seed(fs, "w10", "S10", worktree={"repo": "/r", "path": "/r/.worktrees/w10", "branch": "fleet/w10"})
    _stub_cmux(monkeypatch, fs)
    torn = []
    monkeypatch.setattr(wt, "teardown",
                        lambda *a, **k: (torn.append(a) or (True, "removed")))
    fleet.cmd_rm(["w10"])
    assert fs.live_get("w10") is None and fs.archive_get("w10") is not None
    assert torn == []                                             # tree untouched without --kill


# --- expected-close tombstone (fleet-doctor #5): a DELIBERATE close must not read as accidental --------
# rm/archive/--with-group write a short-lived tombstone BEFORE tearing the surface down, so the router's
# surface.closed handler can tell an intentional retirement from an accidental external close (and skip
# the spurious `kind='stale'` "revive?" alert). Here: prove the CLI WRITES the tombstone.
def test_rm_default_writes_expected_close_tombstone(fs, monkeypatch):
    _seed(fs, "wt1", "SURF-WT1")
    _stub_cmux(monkeypatch, fs)
    fleet.cmd_rm(["wt1"])                                         # default close+archive
    assert fs.expected_close_recent("SURF-WT1") is True          # tombstoned before the close


def test_rm_kill_writes_expected_close_tombstone(fs, monkeypatch):
    _seed(fs, "wt2", "SURF-WT2")
    _stub_cmux(monkeypatch, fs)
    fleet.cmd_rm(["wt2", "--kill"])
    assert fs.expected_close_recent("SURF-WT2") is True


def test_rm_detach_does_not_tombstone(fs, monkeypatch):
    # --detach leaves the surface RUNNING (no close, no surface.closed frame) -> nothing to shield.
    _seed(fs, "wt3", "SURF-WT3")
    _stub_cmux(monkeypatch, fs)
    fleet.cmd_rm(["wt3", "--detach"])
    assert fs.expected_close_recent("SURF-WT3") is False


def test_archive_writes_expected_close_tombstone(fs, monkeypatch):
    _seed(fs, "wt4", "SURF-WT4")
    _stub_cmux(monkeypatch, fs)
    monkeypatch.setattr(fleet, "_pid_for_surface", lambda s: None)
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {})
    fleet.cmd_archive(["wt4"])
    assert fs.live_get("wt4") is None                            # archived...
    assert fs.expected_close_recent("SURF-WT4") is True          # ...and tombstoned first
