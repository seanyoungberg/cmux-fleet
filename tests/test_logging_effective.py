# tests/test_logging_effective.py — logging parity for recycle/revive (registry-invariant batch,
# 2026-07-03). The `recycled`/`revived` ledger events now carry an `effective` {model, effort, plugins}
# field like `launched` always did, and the effort/model values come from _sendcmd_session_prefs: a
# token-scan of the COMPOSED send_cmd, NOT compute_effective() -- a caller's one-off --effort/--model
# on a single recycle/revive only exists in the final command string (adapter_compile layers caller
# tokens in; nothing writes them back onto a spec dict), so any spec-reading path is blind to exactly
# the override case that matters. _session_pref_provenance shares the same helper (one source of
# truth). Ledger read-back follows test_rm_kill_archive's fs.LOG pattern.
import json

from cmux_fleet import cli as fleet


# --- the extracted helper: raw dict in/out, no print formatting -------------------------------------
def test_sendcmd_session_prefs_token_scan():
    f = fleet._sendcmd_session_prefs
    # caller override / role-pin / floor-inherited all land as the same composed tokens -- the helper
    # reads the command that will actually run, so every source is covered by the same scan.
    assert f("cd /x && AGENT_ROLE=r claude --effort max --model opus") == \
        {"effort": "max", "model": "opus"}
    assert f("cd /x && claude --effort=high") == {"effort": "high", "model": None}   # --key=val form
    assert f("cd /x && claude --model sonnet") == {"effort": None, "model": "sonnet"}
    assert f("cd /x && claude") == {"effort": None, "model": None}                   # none composed
    assert f("") == {"effort": None, "model": None}


def test_provenance_line_uses_the_same_scan():
    # override case end-to-end through the print path: value read from the composed cmd, src=override.
    line, _ = fleet._session_pref_provenance("adhoc-thing", "claude",
                                             "cd /x && claude --effort max", "max", "")
    assert "effort=max (override)" in line


def test_recycle_plan_carries_plugin_union(fs, monkeypatch):
    # the payload hands the deterministic entry+add_plugin union to the detached exec worker, which
    # can't re-derive it (it only ever sees the payload file).
    entry = {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
             "session": "claude-OLD", "kind": "child", "plugins": ["p1"]}
    monkeypatch.setattr(fleet, "_compose_recycle_cmd", lambda *a, **k: ("cd /x && claude", ""))
    p = fleet._recycle_plan("w", entry, [], ["extra"], "resume", "", False, None, True)
    assert p["plugins"] == ["p1", "extra"]


# --- the recycled event (both log sites) -------------------------------------------------------------
def _recycle_stubs(monkeypatch):
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "cmuxq", lambda *a, **k: "")
    monkeypatch.setattr(fleet, "_quiet_gate", lambda *a, **k: True)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=1: "")


def test_recycled_event_carries_effective(fs, monkeypatch):
    fs.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/OLD",
                      "session": "claude-OLD", "kind": "child", "plugins": ["p1"]})
    _recycle_stubs(monkeypatch)
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {})
    payload = {"label": "w", "surface": "S", "mode": "fresh", "tool": "claude", "force": True,
               "prime": None, "old_session": "OLD", "cwd": "/NEW", "plugins": ["p1", "extra"],
               "send_cmd": "cd /NEW && claude --effort max --model opus"}   # one-off overrides composed in
    assert fleet._recycle_exec_one(payload) == 0
    events = [json.loads(l) for l in open(fs.LOG) if l.strip()]
    rec = [e for e in events if e["event"] == "recycled" and e["label"] == "w"][-1]
    assert rec["effective"] == {"effort": "max", "model": "opus", "plugins": ["p1", "extra"]}


def test_recycled_event_carries_effective_lazy_tool(fs, monkeypatch):
    # the OTHER log site: a lazy tool (codex) logs `recycled` before any session binds.
    fs.live_put("wc", {"role": "w", "tool": "codex", "surface": "S2", "cwd": "/x",
                       "session": "old-sid", "kind": "child", "plugins": []})
    _recycle_stubs(monkeypatch)
    payload = {"label": "wc", "surface": "S2", "mode": "resume", "tool": "codex", "force": True,
               "prime": None, "old_session": "old-sid", "cwd": "", "plugins": ["pa"],
               "send_cmd": "cd /x && codex --model gpt-x"}
    assert fleet._recycle_exec_one(payload) == 0
    events = [json.loads(l) for l in open(fs.LOG) if l.strip()]
    rec = [e for e in events if e["event"] == "recycled" and e["label"] == "wc"][-1]
    assert rec["effective"] == {"effort": None, "model": "gpt-x", "plugins": ["pa"]}


# --- the revived event + revive's new provenance print ----------------------------------------------
def _seed_archive(fs, label="w"):
    fs.archive_put(label, {"role": "w", "kind": "child", "tool": "claude", "cwd": "/X",
                           "place": "tab", "group": "", "plugins": ["p1"],
                           "flags": ["--effort", "high"], "settings": "",
                           "last_session": "claude-deadbeef00"})


def test_revived_event_carries_effective(fs, monkeypatch):
    _seed_archive(fs)
    monkeypatch.setattr(fleet, "load_config", lambda: {})        # off-roster -> registry-spec compose
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "cmuxq", lambda *a, **k: "")
    monkeypatch.setattr(fleet, "create_surface", lambda *a, **k: ("WS", "SURF"))
    monkeypatch.setattr(fleet, "_resume_and_gate", lambda *a, **k: True)
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=60: "NEWSID")
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {})
    assert fleet.cmd_revive(["w", "--parent", "FAKE", "--", "--effort", "max"]) == 0
    events = [json.loads(l) for l in open(fs.LOG) if l.strip()]
    rev = [e for e in events if e["event"] == "revived" and e["label"] == "w"][-1]
    # the archived flags pinned high, the caller's one-off --effort max layered over it -- the ledger
    # must record the value the command actually runs with.
    assert rev["effective"] == {"effort": "max", "model": None, "plugins": ["p1"]}


def test_revive_prints_session_prefs_line(fs, monkeypatch, capsys):
    # parity on the LIVE output: revive now prints the same session-prefs provenance line as
    # launch/recycle (dry-run is enough -- the print happens before the spawn).
    _seed_archive(fs)
    monkeypatch.setattr(fleet, "load_config", lambda: {})
    assert fleet.cmd_revive(["w", "--parent", "FAKE", "--dry-run", "--", "--effort", "max"]) == 0
    out = capsys.readouterr().out
    assert "session-prefs: effort=max (override)" in out
