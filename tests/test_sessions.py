"""Phase 3a/3b — `fleet sessions` lister + `--session` arbitrary-resume plumbing.

Hermetic: the projects-dir resolution and cmux binding read are monkeypatched, so there is no host
~/.claude or ~/.cmuxterm dependency. Covers the ~/.claude/projects encoding, first-user snippet
extraction, mtime-desc listing, --session validation (match / reject / fail-open), and that an explicit
--session composes `claude --resume <id>` (the arbitrary-prior-session recovery, F2).
"""
import json
import os

from cmux_fleet import cli


def _write_session(d, sid, first_user, mtime=None):
    p = os.path.join(d, sid + ".jsonl")
    with open(p, "w") as f:
        f.write(json.dumps({"type": "user", "message": {"content": first_user}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"content": "ok"}}) + "\n")
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


# --- pure helpers --------------------------------------------------------------------------------
def test_encode_project_dir_matches_claude_rule():
    # verified live: '/', '.', '_' all collapse to '-'
    assert cli._encode_project_dir("/Users/x/cmux-fleet/.worktrees/lh").endswith(
        "-Users-x-cmux-fleet--worktrees-lh")
    assert cli._encode_project_dir("/a/tapestry/_meta/b").endswith("-a-tapestry--meta-b")


def test_session_snippet_str_and_list(tmp_path):
    p1 = _write_session(str(tmp_path), "s1", "hello world")
    assert cli._session_snippet(p1) == "hello world"
    p2 = os.path.join(str(tmp_path), "s2.jsonl")
    with open(p2, "w") as f:
        f.write(json.dumps({"type": "user",
                            "message": {"content": [{"type": "text", "text": "block msg"}]}}) + "\n")
    assert cli._session_snippet(p2) == "block msg"


def test_human_size():
    assert cli._human_size(512) == "512B"
    assert cli._human_size(2048) == "2.0K"
    assert cli._human_size(3 * 1024 * 1024) == "3.0M"


# --- listing + validation ------------------------------------------------------------------------
def test_list_sessions_orders_by_mtime_desc(tmp_path, monkeypatch):
    d = str(tmp_path)
    _write_session(d, "old", "old one", mtime=1000)
    _write_session(d, "new", "new one", mtime=2000)
    monkeypatch.setattr(cli, "_projects_dir_for", lambda entry, surf: d)
    rows = cli._list_sessions({}, "S")
    assert [r[0] for r in rows] == ["new", "old"]        # freshest first
    assert all(r[2] > 0 for r in rows)                    # size populated


def test_list_sessions_empty_when_no_dir(monkeypatch):
    monkeypatch.setattr(cli, "_projects_dir_for", lambda entry, surf: "")
    assert cli._list_sessions({}, "S") == []


def test_known_session_matches_and_rejects(tmp_path, monkeypatch):
    d = str(tmp_path)
    _write_session(d, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "x")
    monkeypatch.setattr(cli, "_projects_dir_for", lambda entry, surf: d)
    assert cli._known_session({}, "S", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") is True
    assert cli._known_session({}, "S", "claude-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") is True  # bare compare
    assert cli._known_session({}, "S", "deadbeef-0000-0000-0000-000000000000") is False


def test_project_dir_is_tool_scoped(monkeypatch):
    # a reused surface's cross-tool history must not leak: only the entry's OWN tool store is read.
    monkeypatch.setattr(cli, "_tool_store", lambda tool: {
        "sessions": {"s1": {"surfaceId": "S", "transcriptPath": "/p/enc/abc.jsonl", "updatedAt": 1}},
        "activeSessionsBySurface": {}} if tool == "claude" else {"sessions": {}, "activeSessionsBySurface": {}})
    assert cli._project_dir_for_surface("S", "claude") == "/p/enc"
    assert cli._project_dir_for_surface("S", "codex") == ""     # codex store has nothing -> no wrong-tool dir


def test_known_session_fails_closed_when_no_dir(monkeypatch):
    # FAIL CLOSED: when the projects dir can't be resolved/enumerated we CANNOT confirm the id exists, so
    # an explicit --session must be refused (not silently proceed into `claude --resume <bad-id>`).
    monkeypatch.setattr(cli, "_projects_dir_for", lambda entry, surf: "")
    assert cli._known_session({}, "S", "whatever") is False   # can't verify -> refuse (--force-session to override)


# --- --session composes an arbitrary --resume ----------------------------------------------------
def test_compose_recycle_explicit_session_wins(monkeypatch):
    # off-roster + no cmux binding -> registry-spec compose; the explicit --session must win over the
    # registry session and appear as `claude --resume <id>`.
    monkeypatch.setattr(cli, "_resume_binding", lambda surf: {})
    monkeypatch.setattr(cli, "_is_roster", lambda role: False)
    entry = {"tool": "claude", "role": "adhoc-x", "cwd": "/tmp/x", "session": "claude-REGISTRYID",
             "surface": "S", "plugins": [], "flags": [], "settings": ""}
    send_cmd, _cp = cli._compose_recycle_cmd("adhoc-x", entry, [], [], "resume", "PICKEDID")
    assert "--resume PICKEDID" in send_cmd
    assert "REGISTRYID" not in send_cmd


def test_compose_recycle_fresh_has_no_resume(monkeypatch):
    monkeypatch.setattr(cli, "_resume_binding", lambda surf: {})
    monkeypatch.setattr(cli, "_is_roster", lambda role: False)
    entry = {"tool": "claude", "role": "adhoc-x", "cwd": "/tmp/x", "session": "claude-REGISTRYID",
             "surface": "S", "plugins": [], "flags": [], "settings": ""}
    send_cmd, _cp = cli._compose_recycle_cmd("adhoc-x", entry, [], [], "fresh", "")
    assert "--resume" not in send_cmd
