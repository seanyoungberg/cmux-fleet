# tests/test_features.py — unit tests for the view layer (fleet_features). Covers the PURE logic that
# needs no live cmux: status classification (keyword tables, no LLM), context-token reading, the
# parentage tree (including a cycle), and content-aware find scanning. Run: pytest tests/test_features.py
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)


@pytest.fixture(autouse=True)
def _throwaway_state(tmp_path, monkeypatch):
    # isolate config so importing fleet_features never touches a real state dir. These modules read
    # their paths from the env AT IMPORT, so they're popped before this test re-imports them under the
    # throwaway env — AND popped again on teardown, so the next test file re-imports cleanly under the
    # restored (session) env instead of inheriting this test's now-deleted tmp_path state dir.
    monkeypatch.setenv("CMUX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CMUX_FLEET_TOML", str(tmp_path / "none.toml"))
    _reset_pkg_modules()
    yield
    _reset_pkg_modules()


def _reset_pkg_modules():
    """Force a fresh re-import of the config-reading modules under the current env. Popping from
    sys.modules is NOT enough for a package: `from cmux_fleet import X` reuses the stale attribute
    still bound on the parent package object, so we clear those attributes too."""
    import cmux_fleet
    for sub in ("config", "state", "features"):
        sys.modules.pop(f"cmux_fleet.{sub}", None)
        if hasattr(cmux_fleet, sub):
            delattr(cmux_fleet, sub)


def _ff():
    from cmux_fleet import features as ff
    return ff


# ── status classification (no LLM) ────────────────────────────────────────────────────────────
def test_classify_lifecycle_authoritative():
    ff = _ff()
    assert ff._classify("running", True, "anything") == "working"
    assert ff._classify("needsInput", True, "") == "needs-input"
    assert ff._classify("idle", True, "") == "idle"


def test_classify_pending_vs_stale_when_no_session():
    ff = _ff()
    assert ff._classify("", False, "") == "pending"          # never bound a session
    assert ff._classify("", True, "") == "stale"             # had one, surface gone


def test_classify_keyword_refines_idle():
    ff = _ff()
    assert ff._classify("idle", True, "Traceback (most recent call last)") == "error"
    assert ff._classify("idle", True, "Do you want to proceed? [y/n]") == "needs-input"
    assert ff._classify("idle", True, "opened pull request #42") == "review"
    assert ff._classify("idle", True, "all tests passed ✓ done") == "done"
    assert ff._classify("idle", True, "still chugging along on the refactor") == "idle"


def test_classify_keywords_only_apply_to_idle():
    ff = _ff()
    # a running agent that happens to print "error" mid-work stays working (lifecycle wins)
    assert ff._classify("running", True, "error: transient") == "working"


# ── context-token reading ─────────────────────────────────────────────────────────────────────
def test_context_used_sums_claude_usage(tmp_path):
    ff = _ff()
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in [
        {"type": "assistant", "message": {"model": "claude-opus-4-8",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100}}},
        {"type": "assistant", "message": {"model": "claude-opus-4-8",
            "usage": {"input_tokens": 5, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 50}}},
    ]))
    used, model = ff._context_used(str(p))
    assert used == 2055                                       # LAST turn's usage, not a sum across turns
    assert model == "claude-opus-4-8"


def test_context_used_none_for_missing_or_codex(tmp_path):
    ff = _ff()
    assert ff._context_used("")[0] is None
    assert ff._context_used(str(tmp_path / "nope.jsonl"))[0] is None
    p = tmp_path / "codex.jsonl"
    p.write_text(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "hi"}}))
    assert ff._context_used(str(p))[0] is None               # codex counter is cumulative -> we don't guess


def test_context_window_configurable(monkeypatch):
    monkeypatch.setenv("CMUX_FLEET_CONTEXT_WINDOW", "1000000")
    ff = _ff()
    assert ff._context_window("claude-opus-4-8") == 1000000   # config override wins over the guess map


def test_context_window_guess_map():
    ff = _ff()
    assert ff._context_window("claude-sonnet-4-6") == 200000
    assert ff._context_window("gpt-5-codex") in (272000,)
    assert ff._context_window("totally-unknown") == 200000    # safe default


# ── parentage tree (label-keyed, cycle-safe) ──────────────────────────────────────────────────
def _row(label, parent="", state="idle", surface="", role="r", tool="claude",
         ctx_used=None, ctx_pct_remaining=None, last_text=""):
    return {"label": label, "parent": parent, "state": state, "surface": surface or label,
            "role": role, "tool": tool, "ctx_used": ctx_used, "ctx_pct_remaining": ctx_pct_remaining,
            "window": 200000, "last_text": last_text, "rank": 5, "last_age_s": 0, "ws": "", "model": "",
            "muted": False, "kind": "child"}


def test_tree_nests_by_label():
    ff = _ff()
    rows = [_row("boss"), _row("w1", parent="boss"), _row("w2", parent="boss")]
    order, children, byl = ff._emit_order(rows)
    assert order[0] == ("boss", 0)
    assert set(children["boss"]) == {"w1", "w2"}
    assert ("w1", 1) in order and ("w2", 1) in order


def test_tree_cycle_terminates_and_keeps_all():
    ff = _ff()
    # a ↔ b cycle with c under a — must terminate and emit every node exactly once
    rows = [_row("a", parent="b"), _row("b", parent="a"), _row("c", parent="a")]
    order, children, byl = ff._emit_order(rows)
    labels = [lbl for lbl, _ in order]
    assert sorted(labels) == ["a", "b", "c"]                 # no node dropped, no infinite loop
    assert len(labels) == 3                                  # each exactly once


def test_tree_orphan_promotes_ancestor_first():
    ff = _ff()
    # no true root (a<->b cycle). 'a' has the most descendants -> it should be the pseudo-root, not 'c'.
    rows = [_row("a", parent="b"), _row("b", parent="a"), _row("c", parent="a")]
    order, _, _ = ff._emit_order(rows)
    assert order[0][1] == 0                                   # first emitted is a depth-0 pseudo-root
    assert order[0][0] in ("a", "b")                         # an ancestor, never the leaf 'c'


def test_graph_html_is_balanced():
    ff = _ff()
    from html.parser import HTMLParser

    class V(HTMLParser):
        def __init__(self):
            super().__init__(); self.stack = []; self.ok = True
        def handle_starttag(self, t, a):
            if t in ("ul", "li"): self.stack.append(t)
        def handle_endtag(self, t):
            if t in ("ul", "li"):
                if not self.stack or self.stack.pop() != t: self.ok = False

    rows = [_row("a", parent="b"), _row("b", parent="a"), _row("c", parent="a", last_text="<script>x")]
    v = V(); v.feed(ff._graph_html(rows))
    assert v.ok and not v.stack                               # well-formed even with a cycle + HTML in text


# ── content-aware find scanning ───────────────────────────────────────────────────────────────
def test_scan_transcript_finds_recent_match(tmp_path):
    ff = _ff()
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"content": "please fix the auth bug"}},
        {"type": "assistant", "message": {"content": "done, the OAuth flow now refreshes tokens"}},
    ]))
    assert "oauth" in ff._scan_transcript(str(p), "oauth", 6).lower()
    assert ff._scan_transcript(str(p), "kubernetes", 6) == ""  # absent -> empty


def test_scan_transcript_respects_turn_window(tmp_path):
    ff = _ff()
    msgs = [{"type": "assistant", "message": {"content": "needle here"}}]
    msgs += [{"type": "assistant", "message": {"content": f"filler {i}"}} for i in range(10)]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in msgs))
    assert ff._scan_transcript(str(p), "needle", 3) == ""     # outside the last 3 turns -> not found


# ── find arg parsing: the --turns value must not leak into the query ───────────────────────────
def test_find_turns_value_not_folded_into_query(capsys):
    ff = _ff()
    from cmux_fleet import state as fs
    fs.live_put("alpha", {"role": "r", "kind": "child", "tool": "claude", "cwd": "",
                          "surface": "", "parent": "", "status": "live"})
    # the OLD bug collected every non-dash token, so the query became "alpha 3" and matched nothing.
    rc = ff.cmd_find(["alpha", "--turns", "3"])
    out = capsys.readouterr().out
    assert rc == 0 and "alpha" in out           # query parsed as "alpha" -> matches the label


def test_find_turns_value_reaches_scanner(monkeypatch):
    ff = _ff()
    from cmux_fleet import state as fs
    fs.live_put("w1", {"role": "r", "kind": "child", "tool": "claude", "cwd": "",
                       "surface": "S1", "parent": "", "status": "live"})
    seen = {}
    monkeypatch.setattr(ff, "_freshest_session", lambda store, surf: {"transcriptPath": "x.jsonl"})
    monkeypatch.setattr(ff, "_scan_transcript", lambda path, q, turns: seen.update(q=q, turns=turns) or "")
    # query "zzz" matches no label/role/cwd -> falls through to the transcript scan
    ff.cmd_find(["zzz", "--turns", "9"])
    assert seen == {"q": "zzz", "turns": 9}      # N is the turn count, not part of the query


# ── small formatters ──────────────────────────────────────────────────────────────────────────
def test_ctx_flags_near_full():
    ff = _ff()
    assert ff._ctx(_row("x", ctx_used=180000, ctx_pct_remaining=10)).endswith("%!")
    assert not ff._ctx(_row("x", ctx_used=20000, ctx_pct_remaining=90)).endswith("!")
    assert ff._ctx(_row("x")) == "—"


def test_fit_truncates():
    ff = _ff()
    assert ff._fit("short", 10) == "short"
    assert ff._fit("a-very-long-label-here", 8) == "a-very-…"
