# tests/test_features.py — unit tests for the view layer (fleet_features). Covers the PURE logic that
# needs no live cmux: status classification (keyword tables, no LLM), context-token reading, the
# parentage tree (including a cycle), and content-aware find scanning. Run: pytest tests/test_features.py
import json
import os
import sys

import pytest



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
    # `resolve` MUST re-import with `state`: it holds a module-level `from . import state as fs`,
    # so resetting state without it leaves resolve delegating to the STALE state object while the
    # tests patch the fresh one — a split-brain that poisoned every later test file (found step 1).
    for sub in ("config", "state", "resolve", "features"):
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
    assert ff._classify("idle", True, "", quiet=True) == "idle"       # long-quiet -> idle
    assert ff._classify("idle", True, "") == "ready"                  # recently active -> ready, not asleep


def test_classify_needs_input_only_on_open_gate():
    ff = _ff()
    # an UNREPLIED Feed gate is the ONLY needs-input signal -> genuinely blocked
    assert ff._classify("needsInput", True, "", open_gate=True) == "needs-input"
    assert ff._classify("idle", True, "", open_gate=True) == "needs-input"
    # a mid-turn agent is NEVER blocked on the human: `running` outranks a (stale) open gate. Regression
    # guard for the live 2026-07-09 FP -- notif-dedup read needs-input while actively executing, because a
    # resume-picker Feed row answered via send-key was never marked terminal.
    assert ff._classify("running", True, "", open_gate=True) == "working"
    # cmux says needsInput but NO open gate = the turn just ENDED -> 'ready', never a false needs-input
    assert ff._classify("needsInput", True, "") == "ready"
    assert ff._classify("needsInput", True, "Standing by for your direction.") == "ready"


def test_classify_pending_vs_stale_when_no_session():
    ff = _ff()
    assert ff._classify("", False, "") == "pending"          # never bound a session
    assert ff._classify("", True, "") == "stale"             # had one, surface gone


def test_classify_keyword_refines_finished_turn_and_idle():
    ff = _ff()
    # a finished turn (needsInput, no gate) refines to error/review/done, else 'ready'
    assert ff._classify("needsInput", True, "Traceback (most recent call last)") == "error"
    assert ff._classify("needsInput", True, "opened pull request #42") == "review"
    assert ff._classify("needsInput", True, "all tests passed ✓ done") == "done"
    assert ff._classify("needsInput", True, "wrapped up, standing by") == "ready"
    # keywords refine the same way whether recent or quiet; the DEFAULT is ready (recent) vs idle (quiet)
    assert ff._classify("idle", True, "opened pull request #42", quiet=True) == "review"
    assert ff._classify("idle", True, "still chugging on the refactor", quiet=True) == "idle"
    # a stale block-phrase in the transcript NO LONGER forces needs-input (Feed gate is authoritative)
    assert ff._classify("idle", True, "Do you want to proceed? [y/n]", quiet=True) == "idle"
    assert ff._classify("needsInput", True, "approve? [y/n]") == "ready"


def test_classify_keywords_only_apply_when_not_working():
    ff = _ff()
    # a running agent that happens to print "error" mid-work stays working (lifecycle wins)
    assert ff._classify("running", True, "error: transient") == "working"


def test_classify_turn_done_clears_the_lagged_running():
    # Fix 1: cmux's lifecycle lags ~60s at 'running' after a turn closes; the transcript's end_turn flips
    # at once. turn_done retires the stale 'working' -> the just-finished agent reads 'ready' immediately.
    ff = _ff()
    assert ff._classify("running", True, "wrapped up", turn_done=True) == "ready"
    assert ff._classify("running", True, "opened pull request #42", turn_done=True) == "review"
    # turn_done also retires a lingering stale Feed gate (a proven end_turn can't be a live gate)
    assert ff._classify("running", True, "", open_gate=True, turn_done=True) == "ready"
    # still mid-turn when the transcript hasn't closed the turn
    assert ff._classify("running", True, "", turn_done=False) == "working"


def test_classify_ready_vs_idle_is_time_based():
    # Fix 2: a finished agent reads 'ready' (present) until QUIET_S of no activity, then 'idle' (asleep) —
    # NOT cmux's needsInput/idle strings, which don't encode "just finished" vs "long dormant".
    ff = _ff()
    assert ff._classify("needsInput", True, "") == "ready"           # recent -> ready
    assert ff._classify("needsInput", True, "", quiet=True) == "idle"  # dormant -> idle
    assert ff._classify("idle", True, "") == "ready"                 # even cmux 'idle', if recently active


def test_open_gate_uuids_only_unreplied_gates(monkeypatch):
    ff = _ff()
    u_open = "2502f0f3-fd17-4370-9709-7f0417d188eb"
    u_done = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    feed = {"items": [
        {"kind": "question", "request_id": f"claude-{u_open}-PermissionRequest"},            # OPEN gate
        {"kind": "question", "request_id": f"claude-{u_done}-x", "resolved_at": "2026-07-08"},# resolved
        {"kind": "permission", "workstream_id": f"claude-{u_done}", "status": "expired"},      # terminal
        {"kind": "toolUse", "workstream_id": "claude-ffffffff-1111-2222-3333-444444444444"},   # not a gate
    ]}
    monkeypatch.setattr(ff, "_cmux", lambda *a: json.dumps(feed))
    gates = ff._open_gate_uuids()
    assert gates == {u_open}                                  # only the unreplied gate's session


def test_infer_state_open_gate_forces_needs_input(monkeypatch):
    ff = _ff()
    import time as _t
    u = "2502f0f3-fd17-4370-9709-7f0417d188eb"
    monkeypatch.setattr(ff.fs, "lifecycle", lambda surf: "idle")
    monkeypatch.setattr(ff.fs, "last_agent_text", lambda p, cap=400: "wrapped up cleanly")
    entry = {"surface": "S1", "session": True}
    sess = {"sessionId": u, "transcriptPath": "", "updatedAt": _t.time()}   # fresh activity
    assert ff._infer_state(entry, sess, {u}) == "needs-input"      # open gate -> genuinely blocked
    assert ff._infer_state(entry, sess, frozenset()) == "ready"    # no gate, recent -> ready (present)
    sess_old = {"sessionId": u, "transcriptPath": "", "updatedAt": _t.time() - ff.QUIET_S - 60}
    assert ff._infer_state(entry, sess_old, frozenset()) == "idle" # no gate, long-quiet -> idle (asleep)


def test_turn_ended_reads_the_transcript_close(tmp_path):
    # Fix 1's real-time signal: a terminal stop_reason with nothing after it = the turn CLOSED.
    ff = _ff()
    def w(rows):
        p = tmp_path / "t.jsonl"; p.write_text("\n".join(json.dumps(x) for x in rows)); return str(p)
    ended = w([{"type": "user", "message": {}},
               {"type": "assistant", "message": {"stop_reason": "end_turn", "content": []}}])
    assert ff.turn_ended(ended) is True
    working = w([{"type": "assistant", "message": {"stop_reason": "tool_use",
                  "content": [{"type": "tool_use", "name": "Bash"}]}}])
    assert ff.turn_ended(working) is False                        # mid-turn tool call -> not ended
    answered = w([{"type": "assistant", "message": {"stop_reason": "end_turn", "content": []}},
                  {"type": "user", "message": {}}])               # a new user turn follows
    assert ff.turn_ended(answered) is False
    assert ff.turn_ended("") is False                             # codex / no transcript -> fail closed


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


def test_context_used_none_for_zero_token_usage(tmp_path):
    # Fix 3: a usage block that sums to 0 (an errored/empty turn) is NOT a real context reading — used
    # stays None so vitals shows '—', never the garbage '0k 100%' (a 0 total made 1 - 0/window == 100%).
    ff = _ff()
    p = tmp_path / "errored.jsonl"
    p.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-8",
        "usage": {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}))
    assert ff._context_used(str(p))[0] is None               # 0-token usage -> None, not 0


def test_context_used_last_positive_survives_trailing_zero(tmp_path):
    # a REAL turn followed by a 0-token errored turn keeps the last real reading (doesn't reset to None).
    ff = _ff()
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in [
        {"type": "assistant", "message": {"model": "claude-opus-4-8",
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 0}}},
        {"type": "assistant", "message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 0}}},
    ]))
    assert ff._context_used(str(p))[0] == 5100


def test_unparseable_transcript_renders_dash_not_full(tmp_path):
    # Fix 3 at the format boundary: a garbage/truncated transcript -> used None -> pct None -> '—'.
    ff = _ff()
    p = tmp_path / "garbage.jsonl"
    p.write_text("not json at all\n{broken")
    used, _ = ff._context_used(str(p))
    assert used is None
    pct = None if used is None else max(0, round(100 * (1 - used / 200000)))
    assert ff._ctx(_row("x", ctx_used=used, ctx_pct_remaining=pct)) == "—"   # NOT "0k 100%"


# ── per-agent context WINDOW (Fix 1: real per-agent, flavor-aware; override demoted) ──────────
def test_context_window_flavor_is_the_truth():
    # Fix 1: an explicit [Nk]/[Nm] flavor wins — same keyword, DIFFERENT flavor -> DIFFERENT window.
    ff = _ff()
    assert ff._context_window("claude-opus-4-8[1m]") == 1_000_000
    assert ff._context_window("claude-opus-4-8") == 200_000        # bare opus -> default 200k
    assert ff._context_window("claude-opus-4-8[200k]") == 200_000
    assert ff._context_window("some-model[500k]") == 500_000


def test_context_window_keyword_map():
    ff = _ff()
    assert ff._context_window("claude-sonnet-4-6") == 200000
    assert ff._context_window("gpt-5-codex") == 272000
    assert ff._context_window("gemini-2-5-pro") == 1000000
    assert ff._context_window("totally-unknown") == 200000         # safe default (no override set)


def test_context_window_override_beats_keyword_below_flavor(monkeypatch):
    # Corrected precedence: an explicit [flavor] wins; else the fleet's DECLARED window (the override) —
    # it sits ABOVE the keyword guess because a bare model string can't disambiguate opus-4-8's 200k vs
    # 1M tier, so a keyword guess of 200k FALSE-alarms agents actually on 1M (cmux-advisor at 395k on a
    # bare `--model claude-opus-4-8`, 2026-07-04). The keyword only catches a model with NO declared window.
    monkeypatch.setenv("CMUX_FLEET_CONTEXT_WINDOW", "777000")
    ff = _ff()
    assert ff._context_window("claude-opus-4-8[1m]") == 1_000_000  # explicit flavor beats the declared window
    assert ff._context_window("claude-opus-4-8") == 777_000        # declared window beats the bare-model keyword guess
    assert ff._context_window("totally-unknown") == 777_000        # and catches unknown models too


def test_window_flavor_parse():
    ff = _ff()
    assert ff._window_flavor("claude-opus-4-8[1m]") == 1_000_000
    assert ff._window_flavor("x[1M]") == 1_000_000                 # case-insensitive
    assert ff._window_flavor("x[200k]") == 200_000
    assert ff._window_flavor("x[500000]") == 500_000              # bare integer
    assert ff._window_flavor("claude-opus-4-8") is None            # no flavor


# ── launched-model resolution (Fix 1 linchpin: effective model = launch flag > global default) ─
def test_launched_prefs_flag_overrides_global_default(monkeypatch):
    ff = _ff()
    monkeypatch.setattr(ff, "_user_prefs", lambda: ("claude-opus-4-8[1m]", "medium"))
    sess = {"launchCommand": {"launcher": "claude",
            "arguments": ["claude", "--model", "claude-opus-4-8", "--effort", "high"]}}
    assert ff._launched_prefs(sess, "claude") == ("claude-opus-4-8", "high")


def test_launched_prefs_inherits_global_default(monkeypatch):
    # the [1m] flavor lives in the global default; an agent launched with NO --model inherits it.
    ff = _ff()
    monkeypatch.setattr(ff, "_user_prefs", lambda: ("claude-opus-4-8[1m]", "medium"))
    sess = {"launchCommand": {"launcher": "claude",
            "arguments": ["claude", "--dangerously-skip-permissions"]}}
    assert ff._launched_prefs(sess, "claude") == ("claude-opus-4-8[1m]", "medium")


def test_launched_prefs_codex_ignores_claude_default(monkeypatch):
    ff = _ff()
    monkeypatch.setattr(ff, "_user_prefs", lambda: ("claude-opus-4-8[1m]", "medium"))
    sess = {"launchCommand": {"launcher": "codex", "arguments": ["codex"]}}
    assert ff._launched_prefs(sess, "codex") == ("", "")           # no claude model bleed onto codex


def test_launch_args_dict_and_string_forms():
    ff = _ff()
    assert ff._launch_args({"launchCommand": {"arguments": ["claude", "--model", "opus"]}}) \
        == ["claude", "--model", "opus"]
    assert ff._launch_args({"launchCommand": "claude --model opus"}) == ["claude", "--model", "opus"]
    assert ff._launch_args({}) == []


def test_snapshot_surfaces_effort_cwd_and_real_window(monkeypatch):
    # Fix 1 + Fix 2 end-to-end: two claude agents, same used tokens, DIFFERENT launched flavor -> DIFFERENT
    # real window -> DIFFERENT remaining %; effort + cwd land on the row.
    ff = _ff()
    from cmux_fleet import state as fs
    fs.live_put("big", {"role": "r", "kind": "child", "tool": "claude", "cwd": "/work/big",
                        "surface": "S-BIG", "parent": "", "status": "live"})
    fs.live_put("small", {"role": "r", "kind": "child", "tool": "claude", "cwd": "/work/small",
                          "surface": "S-SMALL", "parent": "", "status": "live"})
    store = {"sessions": {
        "u-big": {"surfaceId": "S-BIG", "updatedAt": 100, "transcriptPath": "big.jsonl",
                  "agentLifecycle": "running", "workspaceId": "W1", "sessionId": "u-big",
                  "launchCommand": {"launcher": "claude",
                      "arguments": ["claude", "--model", "claude-opus-4-8[1m]", "--effort", "high"]}},
        "u-small": {"surfaceId": "S-SMALL", "updatedAt": 100, "transcriptPath": "small.jsonl",
                    "agentLifecycle": "running", "workspaceId": "W2", "sessionId": "u-small",
                    "launchCommand": {"launcher": "claude",
                        "arguments": ["claude", "--model", "claude-opus-4-8", "--effort", "low"]}},
    }, "activeSessionsBySurface": {}}
    monkeypatch.setattr(fs, "read_hook_store", lambda: store)
    monkeypatch.setattr(ff, "_context_used", lambda path: (400_000, "claude-opus-4-8"))
    monkeypatch.setattr(fs, "last_agent_text", lambda path, cap=160: "")
    rows = {r["label"]: r for r in ff.snapshot()}
    assert rows["big"]["window"] == 1_000_000 and rows["small"]["window"] == 200_000
    assert rows["big"]["model"] == "claude-opus-4-8[1m]"           # launched model carries the flavor
    assert rows["big"]["effort"] == "high" and rows["small"]["effort"] == "low"
    assert rows["big"]["cwd"] == "/work/big" and rows["small"]["cwd"] == "/work/small"
    # same 400k used, different REAL window -> different remaining %
    assert rows["big"]["ctx_pct_remaining"] == 60                  # 400k/1M -> 60% left
    assert rows["small"]["ctx_pct_remaining"] == 0                 # 400k/200k -> over-full -> 0%


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


# ── --scope filtering on the view rows (vitals _apply_scope, graph _scope_subtree) ────────────
def _scope_rows():
    # a two-conductor fleet: adv owns k1 (which owns grandkid gk); peer owns ok
    return [
        {"label": "adv", "kind": "conductor", "parent": ""},
        {"label": "k1", "kind": "child", "parent": "adv"},
        {"label": "gk", "kind": "child", "parent": "k1"},
        {"label": "peer", "kind": "conductor", "parent": ""},
        {"label": "ok", "kind": "child", "parent": "peer"},
    ]


def test_vitals_apply_scope_mine_kinds_and_all():
    ff = _ff()
    rows = _scope_rows()
    # reads' mine = self + DIRECT children (not grandkids); include_self=True is baked into _apply_scope
    assert {r["label"] for r in ff._apply_scope(rows, "mine", "adv")} == {"adv", "k1"}
    assert {r["label"] for r in ff._apply_scope(rows, "conductors", "")} == {"adv", "peer"}
    assert {r["label"] for r in ff._apply_scope(rows, "children", "")} == {"k1", "gk", "ok"}
    assert {r["label"] for r in ff._apply_scope(rows, "all", "")} == {"adv", "k1", "gk", "peer", "ok"}


def test_graph_scope_subtree_roots_at_caller_and_label():
    ff = _ff()
    rows = _scope_rows()
    # graph mine = your whole SUBTREE (transitive), rooted at the caller
    assert {r["label"] for r in ff._scope_subtree(rows, "mine", "adv")} == {"adv", "k1", "gk"}
    assert {r["label"] for r in ff._scope_subtree(rows, "k1", "")} == {"k1", "gk"}      # a bare <label> root
    assert {r["label"] for r in ff._scope_subtree(rows, "all", "")} == {"adv", "k1", "gk", "peer", "ok"}


def test_graph_scope_unknown_label_exits():
    ff = _ff()
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        ff._scope_subtree(_scope_rows(), "nosuch", "")


def test_graph_json_emits_scoped_node_rows(monkeypatch, capsys):
    ff = _ff()
    monkeypatch.setattr(ff, "snapshot", lambda: _scope_rows())
    ff.cmd_graph(["--json", "--scope", "all"])
    data = json.loads(capsys.readouterr().out)                   # --json is machine output, no text table
    assert {r["label"] for r in data} == {"adv", "k1", "gk", "peer", "ok"}
    assert {r["label"]: r["parent"] for r in data}["k1"] == "adv"  # parent pointers preserved in JSON


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


def test_ctx_shows_real_window():
    # Fix 1: the denominator is now the REAL per-agent window, shown as used/window.
    ff = _ff()
    big = dict(_row("x", ctx_used=456000, ctx_pct_remaining=54)); big["window"] = 1_000_000
    assert ff._ctx(big) == "456k/1M 54%"
    small = dict(_row("y", ctx_used=150000, ctx_pct_remaining=25))   # _row window is 200000
    assert ff._ctx(small) == "150k/200k 25%!"


def test_winlabel():
    ff = _ff()
    assert ff._winlabel(1_000_000) == "1M" and ff._winlabel(200_000) == "200k"
    assert ff._winlabel(272_000) == "272k"


def test_short_model_and_cwd():
    ff = _ff()
    assert ff._short_model("claude-opus-4-8[1m]") == "opus-4-8[1m]"
    assert ff._short_model("gpt-5-codex") == "gpt-5-codex"
    assert ff._short_model("") == "-"
    assert ff._short_cwd("/Users/x/repo/.worktrees/feat") == ".worktrees/feat"
    assert ff._short_cwd("") == "-"


def test_fit_truncates():
    ff = _ff()
    assert ff._fit("short", 10) == "short"
    assert ff._fit("a-very-long-label-here", 8) == "a-very-…"


# ── vitals watch/dock render + change-fingerprint ─────────────────────────────────────────────
def test_render_vitals_carries_the_board():
    ff = _ff()
    _c = dict(effort="high", cwd="/Users/x/repo")            # snapshot fields the _row helper omits
    rows = [dict(_row("alpha", state="working", ctx_used=100000, ctx_pct_remaining=50, last_text="doing X"), **_c),
            dict(_row("bravo", state="needs-input", ctx_used=180000, ctx_pct_remaining=10, last_text="blocked"), **_c)]
    out = ff._render_vitals(rows)
    assert "FLEET VITALS (2)" in out and "alpha" in out and "bravo" in out
    assert "doing X" in out and "blocked" in out
    assert "1 near-full" in out and "bravo" in out.split("near-full")[1]   # <=30% flagged in footer


def test_vitals_fp_excludes_age_but_tracks_meaning():
    ff = _ff()
    base = _row("a", state="working", ctx_pct_remaining=50, last_text="hi")
    # idle/last-age tick every second — must NOT move the fingerprint (else the dock loop churns)
    older = dict(base, last_age_s=999)
    assert ff._vitals_fp([base]) == ff._vitals_fp([older])
    # a real change (state / ctx / last-text) MUST move it
    assert ff._vitals_fp([base]) != ff._vitals_fp([dict(base, state="needs-input")])
    assert ff._vitals_fp([base]) != ff._vitals_fp([dict(base, ctx_pct_remaining=49)])
    assert ff._vitals_fp([base]) != ff._vitals_fp([dict(base, last_text="bye")])


# ── paint: native built-in-sidebar pills (per-agent stack) + worst-case ctx bar ───────────────
def _capture_cmux(ff, monkeypatch):
    calls = []
    monkeypatch.setattr(ff, "_cmux", lambda *a: calls.append(a) or "")
    return calls


def test_paint_stacks_one_pill_per_child_not_a_single_fleet_pill(monkeypatch):
    ff = _ff()
    calls = _capture_cmux(ff, monkeypatch)
    # two children SHARING one conductor workspace — the old code keyed both 'fleet' -> they clobbered.
    rows = [dict(_row("book-keeper", state="needs-input", ctx_pct_remaining=49), ws="workspace:1"),
            dict(_row("loom-dev", state="working", ctx_pct_remaining=38), ws="workspace:1")]
    ff._paint(rows)
    pills = [c for c in calls if c[0] == "set-status"]
    keys = {c[1] for c in pills}
    assert keys == {"book-keeper", "loom-dev"}                # keyed per AGENT, never the constant 'fleet'
    assert "fleet" not in keys
    vals = {c[1]: c[2] for c in pills}                        # the VALUE renders — must identify the agent
    assert vals["book-keeper"] == "book-keeper · 49%"         # label + its OWN ctx% (not the shared bar)
    assert vals["loom-dev"] == "loom-dev · 38%"


def test_paint_solo_workspace_pill_is_state_not_label(monkeypatch):
    ff = _ff()
    calls = _capture_cmux(ff, monkeypatch)
    # ONE agent alone on its own workspace (workspace-per-agent) -> pill VALUE is the state word, because
    # the workspace TITLE already shows the label and the per-agent bar carries the ctx%.
    rows = [dict(_row("usage-ops", state="working", ctx_pct_remaining=68), ws="workspace:7", kind="child")]
    ff._paint(rows)
    pills = [c for c in calls if c[0] == "set-status"]
    assert len(pills) == 1
    assert pills[0][1] == "usage-ops"                        # key stays the label (clears cleanly)
    assert pills[0][2] == "working"                          # value is the STATE word, not "usage-ops · 68%"


def test_paint_progress_bar_is_the_worst_agent_on_the_workspace(monkeypatch):
    ff = _ff()
    calls = _capture_cmux(ff, monkeypatch)
    rows = [dict(_row("a", state="working", ctx_pct_remaining=50), ws="workspace:1"),
            dict(_row("b", state="working", ctx_pct_remaining=12), ws="workspace:1")]
    ff._paint(rows)
    progs = [c for c in calls if c[0] == "set-progress"]
    assert len(progs) == 1                                    # ONE bar per workspace, not one per agent
    assert progs[0][1] == "0.88"                              # (100-12)/100 -> the WORST agent's usage
    assert "b · 12% left" in progs[0]                         # labelled with who's tightest (shared ws)


def test_paint_progress_label_carries_model_and_effort(monkeypatch):
    # model·effort are NOT native cmux fields -> they ride the bar's LABEL (the second free-text channel),
    # so the built-in sidebar's ctx-bar caption shows them without lengthening the subtitle.
    ff = _ff()
    calls = _capture_cmux(ff, monkeypatch)
    rows = [dict(_row("solo", state="working", ctx_pct_remaining=63),
                 ws="workspace:9", model="claude-opus-4-8[1m]", effort="xhigh")]
    ff._paint(rows)
    prog = [c for c in calls if c[0] == "set-progress"][0]
    assert "opus-4-8[1m] · xhigh · 63% left" in prog          # model·effort in the caption
    assert "solo" not in prog[prog.index("--label") + 1]      # solo ws: no name prefix (title has it)


def test_paint_on_change_only_and_retires_vanished_pills(monkeypatch):
    ff = _ff()
    calls = _capture_cmux(ff, monkeypatch)
    # keep 'a' and 'b' on SEPARATE workspaces so removing 'b' doesn't flip 'a' shared->solo (which would
    # legitimately change 'a's pill value) — this isolates the on-change/vanish behavior.
    rows = [dict(_row("a", state="working", ctx_pct_remaining=50), ws="workspace:1"),
            dict(_row("b", state="idle", ctx_pct_remaining=80), ws="workspace:2")]
    ff._paint(rows)                                           # first paint: both pills land
    calls.clear()
    # 'a' unchanged, 'b' gone -> no repaint of 'a', a clear-status for 'b'
    ff._paint([dict(_row("a", state="working", ctx_pct_remaining=50), ws="workspace:1")])
    assert not [c for c in calls if c[0] == "set-status" and c[1] == "a"]   # unchanged -> not repainted
    clears = [c for c in calls if c[0] == "clear-status"]
    assert ("clear-status", "b", "--workspace", "workspace:2") in clears


def test_fleet_blobs_are_per_workspace_and_keyed_by_surface():
    # NOT one marker workspace: a blob per workspace, each record keyed by the agent's stable surface uuid.
    # That's what makes the render survive a placement change (tabs -> per-agent workspaces).
    ff = _ff()
    rows = [dict(_row("lead", state="working", ctx_pct_remaining=41), kind="conductor", surface="s1", ws="ws-A"),
            dict(_row("w~one;x", state="idle", ctx_pct_remaining=None), kind="child", parent="lead",
                 surface="s2", ws="ws-B", last_text="did a ~thing; ok")]
    blobs = ff._fleet_blobs(rows)
    assert set(blobs) == {"ws-A", "ws-B"}
    assert blobs["ws-A"].startswith("FLEET4;")
    lead = blobs["ws-A"].split(";")[1].split("~")
    assert len(lead) == ff.BLOB_FIELDS
    assert lead[0] == "s1"                                    # surface uuid FIRST — the identity key
    assert lead[1] == "lead" and lead[2] == "working" and lead[3] == "41"
    assert lead[5] == "conductor" and lead[10] == "0"         # conductor, not collapsed
    child = blobs["ws-B"].split(";")[1].split("~")
    assert child[0] == "s2" and child[1] == "w-one,x"         # ~ and ; stripped from the label
    assert child[3] == "-"                                    # no ctx -> '-'
    assert child[4] == "lead" and child[5] == "child"
    assert child[11] == "did a -thing, ok"                    # last-text delimiters neutralized


def test_blob_never_emits_an_empty_field():
    # Swift's split(separator:) DROPS empty components, which would shift every later field's index in the
    # sidebar and silently mis-render. Every empty must serialize as '-'.
    ff = _ff()
    rows = [dict(_row("solo", state="idle", ctx_pct_remaining=None), kind="child", parent="",
                 surface="s1", ws="ws-A", last_text="")]
    f = ff._fleet_blobs(rows)["ws-A"].split(";")[1].split("~")
    assert len(f) == ff.BLOB_FIELDS                           # nothing dropped
    assert "" not in f                                        # every empty became the '-' sentinel


def test_collapse_bit_round_trips_and_survives_a_repaint():
    ff = _ff()
    rows = [dict(_row("lead", state="working", ctx_pct_remaining=41), kind="conductor", surface="s1", ws="ws-A")]
    tapped = ff._fleet_blobs(rows, collapsed={"s1": "1"})["ws-A"]   # a sidebar tap flipped the bit
    assert tapped.split(";")[1].split("~")[10] == "1"
    carried = ff._collapsed_map({"ws-A": tapped})                   # paint reads it back out of the live desc
    assert carried == {"s1": "1"}
    assert ff._fleet_blobs(rows, carried)["ws-A"] == tapped         # repaint preserves the user's choice
    # only conductors carry a collapse bit — a child's is always '0'
    kid = [dict(_row("w", state="idle"), kind="child", parent="lead", surface="s2", ws="ws-A")]
    assert ff._fleet_blobs(kid, {"s2": "1"})["ws-A"].split(";")[1].split("~")[10] == "0"


def test_collapsed_map_tolerates_malformed_records():
    ff = _ff()
    assert ff._collapsed_map({"w": "not a blob"}) == {}
    assert ff._collapsed_map({"w": "FLEET4;too~few~fields"}) == {}
    assert ff._collapsed_map({}) == {}


def test_surface_ws_map_parses_the_tree_and_snapshot_prefers_it_over_stale_caches(fs, monkeypatch):
    # 2026-07-10 (found by sidebar-build): snapshot read the hook store's workspaceId, which is never
    # updated when a surface MOVES — so two agents moved into their own workspaces both reported the
    # conductor's workspace, collapsed onto one id. The live tree is the only never-stale source.
    from cmux_fleet import features as ff
    WS_A = "AAAAAAAA-1111-2222-3333-444444444444"
    WS_B = "BBBBBBBB-1111-2222-3333-444444444444"
    S_A  = "11111111-1111-2222-3333-444444444444"
    S_B  = "22222222-1111-2222-3333-444444444444"
    tree = (f'workspace workspace:1 {WS_A} "a"\n'
            f'  pane pane:1 99999999-1111-2222-3333-444444444444\n'
            f'    surface surface:1 {S_A} [terminal]\n'
            f'workspace workspace:2 {WS_B} "b"\n'
            f'    surface surface:2 {S_B} [terminal]\n')
    ff._WS_MAP.update({"at": 0.0, "map": {}})                  # defeat the memo
    monkeypatch.setattr(ff, "_cmux", lambda *a: tree)
    m = ff._surface_ws_map(ttl=0)
    assert m[S_A.upper()] == WS_A and m[S_B.upper()] == WS_B   # surfaces bind to their OWN workspace

    # an unreadable tree must not regress a working read: caller falls back to the cached fields
    ff._WS_MAP.update({"at": 0.0, "map": {}})
    monkeypatch.setattr(ff, "_cmux", lambda *a: "")
    assert ff._surface_ws_map(ttl=0) == {}


def c_index(call):
    return call.index("--description") + 1


def _boss_rows():
    return [dict(_row("boss", state="working", ctx_pct_remaining=40), ws="ws-A", kind="conductor", surface="s1")]


# ── legacy descriptor recognizer: recognizes/cleans old native-first prose, never a user's note ────
def test_is_descriptor_only_claims_subtitles_we_wrote():
    ff = _ff()
    assert ff._is_descriptor("working · ↳berg-sandbox")
    assert ff._is_descriptor("ready · ▸berg-sandbox")
    assert not ff._is_descriptor("a ▾ b")                                 # bare glyph, wrong separator
    assert not ff._is_descriptor("my own notes about this workspace")     # never clobber the user's text
    assert not ff._is_descriptor("")


# ── the SIDEBAR surface: a full CLI-derived FLEET4 record per workspace (model/effort/tool restored) ──
def test_paint_writes_a_full_fleet_record_per_agent_workspace(monkeypatch):
    # the custom sidebar gets the SAME snapshot `fleet vitals` reads (model/effort/tool/state/ctx/last),
    # NOT the native-first prose that dropped model/effort/tool.
    ff = _ff()
    monkeypatch.setenv("FLEET_SIDEBAR_BLOB", "1")             # opt-in (off by default)
    monkeypatch.setattr(ff, "_ws_descriptions", lambda: {})
    calls = _capture_cmux(ff, monkeypatch)
    rows = [dict(_row("worker", state="working", ctx_pct_remaining=50, tool="codex"), ws="ws-B",
                 kind="child", parent="boss", surface="s2", model="claude-opus-4-8[1m]", effort="high"),
            dict(_row("boss", state="ready", ctx_pct_remaining=40), ws="ws-A", kind="conductor", surface="s1")]
    ff._paint(rows)
    desc = {c[-1]: c[c_index(c)] for c in calls if c[0] == "workspace-action" and "set-description" in c}
    assert desc["ws-A"] == "FLEET4;s1~boss~ready~40~-~conductor~claude~-~-~-~0~-"
    worker = desc["ws-B"].split(";")[1].split("~")
    assert worker[6] == "codex" and worker[7] == "opus-4-8[1m]" and worker[8] == "high"  # tool/model/effort back
    assert all(v.startswith("FLEET4;") for v in desc.values())


def test_paint_puts_every_agent_sharing_a_workspace_in_one_blob(monkeypatch):
    # transitional: while several agents still resolve to one workspace, all their records ride in that
    # workspace's single blob (conductor first) so the sidebar can still render each — no data dropped.
    ff = _ff()
    monkeypatch.setenv("FLEET_SIDEBAR_BLOB", "1")
    monkeypatch.setattr(ff, "_ws_descriptions", lambda: {})
    calls = _capture_cmux(ff, monkeypatch)
    rows = [dict(_row("kid", state="idle"), ws="ws-A", kind="child", parent="boss", surface="s2"),
            dict(_row("boss", state="ready"), ws="ws-A", kind="conductor", surface="s1")]
    ff._paint(rows)
    desc = [c[c_index(c)] for c in calls if c[0] == "workspace-action" and "set-description" in c]
    assert len(desc) == 1
    recs = desc[0].split(";")
    assert recs[0] == "FLEET4"
    assert recs[1].split("~")[1] == "boss" and recs[1].split("~")[5] == "conductor"   # conductor first
    assert recs[2].split("~")[1] == "kid" and recs[2].split("~")[4] == "boss"         # then its child


def test_paint_carries_the_collapse_bit_forward(monkeypatch):
    ff = _ff()
    monkeypatch.setenv("FLEET_SIDEBAR_BLOB", "1")
    collapsed = "FLEET4;s1~boss~ready~40~-~conductor~claude~-~-~-~1~-"   # user tapped it collapsed (col=1)
    monkeypatch.setattr(ff, "_ws_descriptions", lambda: {"ws-A": collapsed})
    calls = _capture_cmux(ff, monkeypatch)
    ff._paint([dict(_row("boss", state="ready", ctx_pct_remaining=40), ws="ws-A", kind="conductor", surface="s1")])
    # read back the bit, regenerate the SAME blob -> no write, so the user's collapse choice survives
    assert not [c for c in calls if c[0] == "workspace-action" and "set-description" in c]


def test_paint_never_clobbers_a_description_it_did_not_write(monkeypatch):
    ff = _ff()
    monkeypatch.delenv("FLEET_SIDEBAR_BLOB", raising=False)
    monkeypatch.setattr(ff, "_ws_descriptions", lambda: {"ws-A": "berg's own note"})
    os.makedirs(os.path.dirname(ff.PAINT_STATE), exist_ok=True)
    json.dump({f"desc{ff._SEP}ws-A": "working · ↳boss"}, open(ff.PAINT_STATE, "w"))
    calls = _capture_cmux(ff, monkeypatch)
    ff._paint([])
    assert not [c for c in calls if "clear-description" in c]  # hands off


def test_paint_self_heals_a_corrupted_record(monkeypatch):
    # a stale PAINT_STATE fingerprint must NOT stop us repairing a blob someone else clobbered.
    ff = _ff()
    monkeypatch.setenv("FLEET_SIDEBAR_BLOB", "1")
    monkeypatch.setattr(ff, "_ws_descriptions", lambda: {"ws-A": "FLEET4;CORRUPTED"})
    os.makedirs(os.path.dirname(ff.PAINT_STATE), exist_ok=True)
    json.dump({f"desc{ff._SEP}ws-A": "FLEET4;s1~boss~working~40~-~conductor~claude~-~-~-~0~-"},
              open(ff.PAINT_STATE, "w"))
    calls = _capture_cmux(ff, monkeypatch)
    ff._paint(_boss_rows())
    desc = [c[c_index(c)] for c in calls if c[0] == "workspace-action" and "set-description" in c]
    assert desc == ["FLEET4;s1~boss~working~40~-~conductor~claude~-~-~-~0~-"]   # repaired despite the fingerprint


def test_paint_clears_the_blob_when_the_sidebar_is_disabled(monkeypatch):
    # sidebar OFF -> retire a blob we own (even one a prior process left) so the built-in view goes clean.
    ff = _ff()
    monkeypatch.delenv("FLEET_SIDEBAR_BLOB", raising=False)
    monkeypatch.setattr(ff, "_ws_descriptions", lambda: {"ws-A": "FLEET4;s1~boss~ready~40~-~conductor~claude~-~-~-~0~-"})
    os.makedirs(os.path.dirname(ff.PAINT_STATE), exist_ok=True)
    json.dump({f"blob{ff._SEP}ws-A": "whatever"}, open(ff.PAINT_STATE, "w"))
    calls = _capture_cmux(ff, monkeypatch)
    ff._paint([])
    assert ("workspace-action", "--action", "clear-description", "--workspace", "ws-A") in calls


# ── subscription-usage panel (per subscription, NOT per agent) — from the stable usage_for_paint() ──
def test_usage_lines_from_the_accessor(monkeypatch):
    # ONE line per subscription: the REAL account (label), each rolling window's %, and the soonest reset.
    ff = _ff()
    from cmux_fleet import providers as pv
    monkeypatch.setattr(pv, "usage_for_paint", lambda: {"schema": 1, "providers": [
        {"kind": "subscription", "account": "berg-max", "label": "Berg", "ok": True, "stale": False,
         "windows": [{"label": "5h", "pct": 44.0, "resets_in_s": 7200},
                     {"label": "7d", "pct": 34.0, "resets_in_s": 500000},
                     {"label": "Fable", "pct": 90, "scoped": True}]},           # scoped -> skipped
        {"kind": "subscription", "account": "berg-team", "label": "sean@x.com", "ok": True, "stale": True,
         "windows": [{"label": "5h", "pct": 1.0, "resets_in_s": 900}]},         # stale -> one clean line
        {"kind": "api", "account": "vertex-x", "ok": True, "stale": False, "windows": []},   # not a sub -> skip
    ]})
    assert ff._usage_lines() == [
        "Berg~0~5h~44~7d~34~2h",           # label from `label`, 5h+7d %, reset of the shortest window
        "sean@x.com~1~-~-~-~-~-",          # stale -> stale flag, no numbers (renders one clean line)
    ]


def test_usage_lines_use_label_not_config_id_and_one_window(monkeypatch):
    # display the REAL account (label), never the config-id `account`; a single-window provider is fine.
    ff = _ff()
    from cmux_fleet import providers as pv
    monkeypatch.setattr(pv, "usage_for_paint", lambda: {"schema": 1, "providers": [
        {"kind": "subscription", "account": "berg-team", "label": "sean.youngberg@gmail.com",
         "ok": True, "stale": False, "windows": [{"label": "30d", "pct": 12.0, "resets_in_s": 200000}]},
    ]})
    assert ff._usage_lines() == ["sean.youngberg@gmail.com~0~30d~12~-~-~2d"]   # label shown; 2nd window '-'


def test_usage_lines_gate_on_schema(monkeypatch):
    ff = _ff()
    from cmux_fleet import providers as pv
    monkeypatch.setattr(pv, "usage_for_paint", lambda: {"schema": 2, "providers": [
        {"kind": "subscription", "account": "x", "label": "X", "ok": True, "stale": False,
         "windows": [{"label": "5h", "pct": 5, "resets_in_s": 100}]}]})
    assert ff._usage_lines() == []                                       # unknown schema -> render nothing, never mis-parse


def test_paint_rides_usage_on_conductor_blobs_only(monkeypatch):
    # fleet-global usage has no per-workspace home, so it rides every CONDUCTOR's blob (the sidebar reads it
    # off the first). ⧗ separates it from the record and is stripped from record text, so it never collides.
    ff = _ff()
    monkeypatch.setenv("FLEET_SIDEBAR_BLOB", "1")
    monkeypatch.setattr(ff, "_ws_descriptions", lambda: {})
    monkeypatch.setattr(ff, "_usage_lines", lambda: ["Berg~0~5h~44~7d~34~2h", "sean@x.com~1~-~-~-~-~-"])
    calls = _capture_cmux(ff, monkeypatch)
    rows = [dict(_row("boss", state="ready", ctx_pct_remaining=40), ws="ws-A", kind="conductor", surface="s1"),
            dict(_row("kid", state="working", ctx_pct_remaining=50), ws="ws-B", kind="child", parent="boss", surface="s2")]
    ff._paint(rows)
    desc = {c[-1]: c[c_index(c)] for c in calls if c[0] == "workspace-action" and "set-description" in c}
    assert desc["ws-A"].endswith("⧗Berg~0~5h~44~7d~34~2h⧗sean@x.com~1~-~-~-~-~-")    # conductor carries the panel
    assert desc["ws-A"].split("⧗")[0] == "FLEET4;s1~boss~ready~40~-~conductor~claude~-~-~-~0~-"  # record intact
    assert "⧗" not in desc["ws-B"]                                       # a child never carries it
