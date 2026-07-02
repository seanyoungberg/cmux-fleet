# tests/test_rm_kill_archive.py — Item 4 (2026-07-02 recovery-safety batch): `fleet rm --kill` used to
# stop the process + close the surface WITHOUT writing an archive.json entry, quietly breaking the
# "prune freely, agents are recoverable" doctrine -- a killed agent was the one removal path that left NO
# recovery trace. force-archive-on-kill: always write an archive row (session id if known, else an
# empty/pending marker) BEFORE tearing the surface down, so `--kill` degrades to "recorded but
# maybe-unresumable" instead of "vanished". Pure units against the throwaway $CMUX_STATE_DIR; cmux reads
# (cmuxq / _resume_binding / _pid_for_surface) are stubbed.
from cmux_fleet import cli as fleet


def test_rm_kill_force_archives_with_known_session(fs, monkeypatch):
    fs.live_put("w1", {"role": "worker", "kind": "child", "tool": "claude", "cwd": "/x",
                       "place": "tab", "group": "", "surface": "S1", "session": "claude-OLD",
                       "plugins": [], "flags": [], "settings": "", "status": "live"})
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: "")
    monkeypatch.setattr(fleet, "_pid_for_surface", lambda s: None)
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {"checkpoint_id": "NEW-CHECKPOINT"})
    fleet.cmd_rm(["w1", "--kill"])
    assert fs.live_get("w1") is None                       # gone from live
    arch = fs.archive_get("w1")
    assert arch is not None                                 # ...but NOT vanished -- archived for recovery
    assert arch["last_session"] == "NEW-CHECKPOINT"          # prefers cmux's ground-truth checkpoint


def test_rm_kill_archives_with_empty_marker_when_no_session_known(fs, monkeypatch):
    # a wedged agent that never bound a session must still stay KILLABLE (no refuse-if-uncaptured) --
    # it just archives with an empty/pending last_session ("maybe-unresumable", not "vanished").
    fs.live_put("w2", {"role": "worker", "kind": "child", "tool": "claude", "cwd": "/x",
                       "place": "tab", "group": "", "surface": "S2", "session": "",
                       "plugins": [], "flags": [], "settings": "", "status": "live"})
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: "")
    monkeypatch.setattr(fleet, "_pid_for_surface", lambda s: None)
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {})
    fleet.cmd_rm(["w2", "--kill"])
    assert fs.live_get("w2") is None
    arch = fs.archive_get("w2")
    assert arch is not None
    assert arch["last_session"] == ""


def test_rm_kill_closes_surface_and_logs_via_kill(fs, monkeypatch):
    fs.live_put("w3", {"role": "worker", "kind": "child", "tool": "claude", "cwd": "/x",
                       "place": "tab", "group": "", "surface": "S3", "session": "",
                       "plugins": [], "flags": [], "settings": "", "status": "live"})
    calls = []
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: (calls.append(a) or ""))
    monkeypatch.setattr(fleet, "_pid_for_surface", lambda s: None)
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {})
    fleet.cmd_rm(["w3", "--kill"])
    assert ("close-surface", "--surface", "S3") in calls
    import json
    events = [json.loads(l) for l in open(fs.LOG) if l.strip()]
    archived = [e for e in events if e["event"] == "archived" and e["label"] == "w3"]
    assert archived and archived[0].get("via") == "kill"


def test_rm_without_kill_does_not_archive(fs, monkeypatch):
    # plain `fleet rm` (no --kill) keeps its existing full-removal contract -- force-archive is additive,
    # scoped to --kill only.
    fs.live_put("w4", {"role": "worker", "kind": "child", "tool": "claude", "cwd": "/x",
                       "place": "tab", "group": "", "surface": "S4", "session": "",
                       "plugins": [], "flags": [], "settings": "", "status": "live"})
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: "")
    fleet.cmd_rm(["w4"])
    assert fs.live_get("w4") is None
    assert fs.archive_get("w4") is None
