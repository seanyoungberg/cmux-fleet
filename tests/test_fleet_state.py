"""Layer 2 — fleet_state transitions (inbox / registry / archive / dial).

In-process against the throwaway STATE dir (wiped per test by the clean_state fixture). Covers the
unified inbox, per-kind acks (the bug fix that a completion-ack must not swallow an unread peer),
seq monotonicity, the live registry, the archive shelf, and the notify-mode dial.
"""
import json
import os
import time


# --- the inbox -----------------------------------------------------------------------------------
def test_seq_is_monotonic(fs):
    a, b, c = fs.inbox_next_seq(), fs.inbox_next_seq(), fs.inbox_next_seq()
    assert (a, b, c) == (1, 2, 3)


def test_put_and_pending_oldest_first(fs):
    s1 = fs.inbox_put("completion", "S", {"label": "w1", "gist": "first"})
    s2 = fs.inbox_put("completion", "S", {"label": "w2", "gist": "second"})
    rows = fs.inbox_pending("S")
    assert [r["seq"] for r in rows] == [s1, s2]
    assert [r["label"] for r in rows] == ["w1", "w2"]


def test_pending_filters_by_kind(fs):
    fs.inbox_put("completion", "S", {"label": "w1"})
    fs.inbox_put("peer", "S", {"from_label": "p1", "body": "hi"})
    assert len(fs.inbox_pending("S", kind="completion")) == 1
    assert len(fs.inbox_pending("S", kind="peer")) == 1
    assert len(fs.inbox_pending("S")) == 2


def test_pending_is_surface_scoped(fs):
    fs.inbox_put("completion", "S", {"label": "mine"})
    fs.inbox_put("completion", "OTHER", {"label": "theirs"})
    assert [r["label"] for r in fs.inbox_pending("S")] == ["mine"]


def test_per_kind_ack_does_not_swallow_other_kind(fs):
    """Acking completions must leave an unread peer pending (the per-kind high-water fix)."""
    cseq = fs.inbox_put("completion", "S", {"label": "w1"})
    fs.inbox_put("peer", "S", {"from_label": "p1", "body": "urgent"})
    fs.inbox_ack("S", "completion", cseq)
    assert fs.inbox_pending("S", kind="completion") == []
    assert len(fs.inbox_pending("S", kind="peer")) == 1


def test_ack_high_water_is_max(fs):
    fs.inbox_ack("S", "completion", 5)
    fs.inbox_ack("S", "completion", 2)  # lower seq must not lower the high-water
    fs.inbox_put("completion", "S", {"seq_hint": "low"})  # seq 1 < 5 -> already acked
    assert fs.inbox_pending("S", kind="completion") == []


def test_max_seq_helper(fs):
    assert fs.max_seq([{"seq": 3}, {"seq": 7}, {"seq": 1}]) == 7
    assert fs.max_seq([]) == 0


# --- ephemeral drain block-guard -----------------------------------------------------------------
def test_block_get_set_roundtrip(fs):
    assert fs.block_get("S", "peer") == 0
    fs.block_set("S", "peer", 9)
    assert fs.block_get("S", "peer") == 9
    assert fs.block_get("S", "completion") == 0  # per-kind, independent


# --- the live registry ---------------------------------------------------------------------------
def test_live_put_get_del(fs):
    fs.live_put("w1", {"role": "worker", "surface": "SURF", "kind": "child"})
    assert fs.live_get("w1")["role"] == "worker"
    assert "w1" in fs.live_all()
    removed = fs.live_del("w1")
    assert removed["surface"] == "SURF"
    assert fs.live_get("w1") is None


def test_surface_label_lookups(fs):
    fs.live_put("w1", {"surface": "SURF1"})
    assert fs.label_for_surface("SURF1") == "w1"
    assert fs.surface_for_label("w1") == "SURF1"
    assert fs.entry_for_surface("SURF1")["surface"] == "SURF1"
    assert fs.label_for_surface("nope") == ""


# --- registry<->real-session reconciliation (kills the "No conversation found" divergence) --------
def test_reconcile_backfills_empty_session(fs):
    fs.live_put("w1", {"tool": "claude", "surface": "S", "session": ""})
    action = fs.reconcile_session("w1", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert action == "backfill"
    assert fs.live_get("w1")["session"] == "claude-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_reconcile_fixes_diverged_bridge_id(fs):
    # the live-proven case: registry holds a stale bridge id, cmux's real live id has moved on
    fs.live_put("w1", {"tool": "claude", "surface": "S", "session": "claude-019f144d-c5f0-7a52-b586-f9e267c469fa"})
    action = fs.reconcile_session("w1", "93666b60-ae1e-4746-bc00-d2c498fac2ff")
    assert action == "reconcile"
    assert fs.live_get("w1")["session"] == "claude-93666b60-ae1e-4746-bc00-d2c498fac2ff"


def test_reconcile_noop_when_already_in_sync(fs):
    fs.live_put("w1", {"tool": "claude", "surface": "S", "session": "claude-abc12345-0000-0000-0000-000000000000"})
    # bare uuid of the stored session == the live id -> no write, no churn
    assert fs.reconcile_session("w1", "abc12345-0000-0000-0000-000000000000") == ""


def test_reconcile_noop_on_empty_sid_or_missing_label(fs):
    fs.live_put("w1", {"tool": "claude", "surface": "S", "session": "claude-x"})
    assert fs.reconcile_session("w1", "") == ""            # empty live id -> never clobber
    assert fs.reconcile_session("ghost", "some-sid") == ""  # unknown label -> no-op


def test_reconcile_codex_stores_bare_uuid(fs):
    fs.live_put("c1", {"tool": "codex", "surface": "S", "session": ""})
    fs.reconcile_session("c1", "77777777-8888-9999-aaaa-bbbbbbbbbbbb")
    assert fs.live_get("c1")["session"] == "77777777-8888-9999-aaaa-bbbbbbbbbbbb"  # no claude- prefix


def test_bus_tool_extracts_prefix(fs):
    assert fs.bus_tool("claude-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") == "claude"
    assert fs.bus_tool("codex-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") == "codex"
    assert fs.bus_tool("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") == ""      # already bare -> no tool
    assert fs.bus_tool("") == ""


def test_reconcile_refuses_cross_tool_write(fs):
    # the berg-sandbox trap: a codex-store id must NOT overwrite a claude agent's session
    fs.live_put("w1", {"tool": "claude", "surface": "S", "session": "claude-93666b60-ae1e-4746-bc00-d2c498fac2ff"})
    action = fs.reconcile_session("w1", "019f144d-c5f0-7a52-b586-f9e267c469fa", "claude", event_tool="codex")
    assert action == "skip-tool"
    assert fs.live_get("w1")["session"] == "claude-93666b60-ae1e-4746-bc00-d2c498fac2ff"  # unchanged
    # a matching-tool event still reconciles
    action = fs.reconcile_session("w1", "ffffffff-0000-0000-0000-000000000000", "claude", event_tool="claude")
    assert action == "reconcile"


# --- the archive shelf + the live->archive->live transition -------------------------------------
def test_archive_put_get_del(fs):
    fs.archive_put("w1", {"role": "worker", "last_session": "abc"})
    assert fs.archive_get("w1")["last_session"] == "abc"
    fs.archive_del("w1")
    assert fs.archive_get("w1") is None


def test_archive_then_revive_transition(fs):
    # simulate archive: live entry leaves the live registry and lands on the shelf
    fs.live_put("w1", {"role": "worker", "surface": "SURF", "session": "claude-xyz"})
    e = fs.live_del("w1")
    fs.archive_put("w1", {"role": e["role"], "last_session": e["session"]})
    assert fs.live_get("w1") is None
    assert fs.archive_get("w1")["last_session"] == "claude-xyz"
    # simulate revive: leaves the shelf, returns live
    fs.archive_del("w1")
    fs.live_put("w1", {"role": "worker", "surface": "SURF2"})
    assert fs.archive_get("w1") is None
    assert fs.live_get("w1")["surface"] == "SURF2"


# --- the notify-mode dial (DEMOTED to a mute switch — design 2.1) --------------------------------
def test_mode_defaults_to_wake_now(fs):
    # no file -> wake-now default (INVERTED from the old 'passive' default).
    assert fs.mode() == "auto"
    assert fs.autodrain_on() is True
    assert fs.idlewake_on() is True


def test_mode_passive_is_the_single_mute(fs):
    with open(fs.MODEFILE, "w") as f:
        f.write("passive\n")
    assert fs.mode() == "passive"
    assert fs.autodrain_on() is False        # 'passive' suppresses drain AND idle-wake fleet-wide
    assert fs.idlewake_on() is False


def test_mode_auto_wakes(fs):
    with open(fs.MODEFILE, "w") as f:
        f.write("auto")
    assert fs.mode() == "auto"
    assert fs.autodrain_on() is True
    assert fs.idlewake_on() is True


def test_legacy_autodrain_folds_into_wake_now(fs):
    # the retired 'autodrain' value normalizes to wake-now (design 2.1: delete autodrain, keep passive
    # as the single override) — it no longer means drain-without-wake.
    with open(fs.MODEFILE, "w") as f:
        f.write("autodrain\n")
    assert fs.mode() == "auto"
    assert fs.autodrain_on() is True
    assert fs.idlewake_on() is True


# --- the event ledger ----------------------------------------------------------------------------
def test_log_event_appends(fs):
    fs.log_event("launched", label="w1", role="worker")
    fs.log_event("removed", label="w1")
    lines = [json.loads(x) for x in open(fs.LOG) if x.strip()]
    assert [r["event"] for r in lines] == ["launched", "removed"]
    assert lines[0]["label"] == "w1" and "ts" in lines[0]


def test_log_event_stamps_agent_invoker(fs, monkeypatch):
    # a fleet AGENT's own identity wins outright (AGENT_LABEL is the routing/recycle key) -- this is
    # the attribution the 2026-07-02 incident forensics needed and didn't have (Item 3): reconstructing
    # WHO ran a destructive command required correlating external logs by hand.
    monkeypatch.setenv("AGENT_LABEL", "berg-sandbox")
    monkeypatch.setenv("AGENT_ROLE", "berg-sandbox")
    fs.log_event("removed", label="staging-conductor")
    rec = json.loads(open(fs.LOG).readlines()[-1])
    assert rec["invoker"] == "agent:berg-sandbox"


def test_log_event_stamps_shell_invoker_without_agent_env(fs, monkeypatch):
    monkeypatch.delenv("AGENT_LABEL", raising=False)
    monkeypatch.delenv("AGENT_ROLE", raising=False)
    fs.log_event("removed", label="staging-conductor")
    rec = json.loads(open(fs.LOG).readlines()[-1])
    assert rec["invoker"].startswith("shell:")   # a bare human shell, not an agent -- diagnostic breadcrumb


def test_atomic_write_creates_dirs(fs, state_dir):
    target = os.path.join(state_dir, "nested", "deep", "f.json")
    fs._atomic_write(target, json.dumps({"ok": True}))
    assert json.load(open(target))["ok"] is True


# --- the wake gate: staleness / liveness / screen-arbiter (design 2.2a, Phase 1) -----------------
# The read-robustness paths the redesign turns on: a STALE/orphaned 'running' record must not silence
# an idle surface (the cmux-advisor stall), the fleet's BOUND session outranks an orphaned
# max-updatedAt record, and the SCREEN is the final arbiter — empty/garbage reads never wake, and a
# wake never fires when no clean prompt is visible.
def _rec(surface, sid, life, age_s=0.0, pid="__live__"):
    """A synthetic cmux hook-store session record (updatedAt is float epoch seconds, aged back). `pid`
    defaults to THIS test process's pid (os.getpid() -- guaranteed alive) so a record models a REAL
    live agent, which always carries a live pid; pass pid=None (or a dead pid) to model the frozen
    SessionEnd-less brick that the pid-aware wake gate must read as not-busy."""
    return {"surfaceId": surface, "sessionId": sid, "agentLifecycle": life,
            "updatedAt": time.time() - age_s, "pid": os.getpid() if pid == "__live__" else pid}


def _store(*records):
    return {"sessions": {r["sessionId"]: r for r in records}, "activeSessionsBySurface": {}}


def _fake_cmux(screen, sink):
    """A _cmux stand-in: returns `screen` for read-screen; records every other verb's argv into sink."""
    def f(*args):
        if args and args[0] == "read-screen":
            return screen
        sink.append(args)
        return ""
    return f


def test_surface_busy_fresh_running_is_busy(fs, monkeypatch):
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store(_rec("S", "u1", "running", age_s=1)))
    assert fs.surface_busy("S") is True


def test_surface_has_live_agent_predicate(fs, monkeypatch):
    # the shared 'is this seat genuinely live?' authority behind ls STALE, bulk stale-skip, launch's
    # overwrite-guard, worktree-clean's refuse, and recycle's re-bind poll: non-terminal lifecycle AND a
    # live pid. All four sites read a dead-pid ghost as gone BECAUSE they route through this one predicate.
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store(_rec("S", "u1", "running", age_s=1)))
    assert fs.surface_has_live_agent("S") is True             # non-terminal + live pid -> genuinely live
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store(_rec("S", "u1", "running", age_s=1, pid=None)))
    assert fs.surface_has_live_agent("S") is False            # frozen 'running' on a DEAD pid -> gone (brick)
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store(_rec("S", "u1", "idle", age_s=1, pid=None)))
    assert fs.surface_has_live_agent("S") is False            # frozen 'idle' on a dead pid -> gone too
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store(_rec("S", "u1", "ended", age_s=1)))
    assert fs.surface_has_live_agent("S") is False            # terminal string (live pid mid-drop) -> gone
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store())
    assert fs.surface_has_live_agent("S") is False            # nothing on the surface -> gone


def test_surface_busy_dead_pid_running_not_busy(fs, monkeypatch):
    # pid-aware wake gate (round 2, 2026-07-06): a FRESH 'running' record (age 1, well within the
    # staleness window) but on a DEAD pid is the SessionEnd-less brick, NOT a live turn -- must read
    # not-busy so the gate can still wake the (dead/idle) seat instead of being silenced by the frozen
    # string. The staleness guard alone would NOT catch this (the record is fresh); the pid does.
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store(_rec("S", "u1", "running", age_s=1, pid=None)))
    assert fs.surface_busy("S") is False


def test_surface_busy_stale_running_not_busy(fs, monkeypatch):
    # a 'running' record frozen well past the staleness window is an orphan, not a live turn.
    monkeypatch.setattr(fs, "read_hook_store",
                        lambda: _store(_rec("S", "u1", "running", age_s=fs.LIFECYCLE_STALE_S + 120)))
    assert fs.surface_busy("S") is False


# real-UUID session ids: the fleet stores the tool-prefixed form ("claude-<uuid>"), the hook store
# keys on the bare uuid; surface_busy must reconcile them via bare_uuid (as the router does).
_BOUND = "11111111-1111-1111-1111-111111111111"
_ORPHAN = "22222222-2222-2222-2222-222222222222"


def test_surface_busy_prefers_bound_session_over_max_updatedat(fs, monkeypatch):
    # the cmux-advisor shape: a FRESH orphaned 'running' record (wrong session) alongside the fleet's
    # actual bound session sitting 'idle'. max-updatedAt would pick the orphan -> 'busy' (the stall);
    # the bound-session cross-check picks the real session -> not busy.
    fs.live_put("advisor", {"surface": "S", "session": f"claude-{_BOUND}", "kind": "conductor"})
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store(
        _rec("S", _ORPHAN, "running", age_s=1),         # freshest, but NOT the bound session
        _rec("S", _BOUND, "idle", age_s=30)))           # the fleet's bound session, idle
    assert fs.surface_busy("S") is False


def test_surface_busy_bound_session_genuinely_running(fs, monkeypatch):
    # regression: when the BOUND session is itself freshly running, still busy (never interrupt).
    fs.live_put("advisor", {"surface": "S", "session": f"claude-{_BOUND}", "kind": "conductor"})
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store(
        _rec("S", _ORPHAN, "idle", age_s=1),
        _rec("S", _BOUND, "running", age_s=2)))
    assert fs.surface_busy("S") is True


def test_surface_busy_no_records_not_busy(fs, monkeypatch):
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store())
    assert fs.surface_busy("S") is False


def test_surface_busy_idle_not_busy(fs, monkeypatch):
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store(_rec("S", "u1", "idle", age_s=1)))
    assert fs.surface_busy("S") is False


def test_wake_if_idle_wakes_on_clean_prompt(fs, monkeypatch):
    monkeypatch.setattr(fs, "surface_busy", lambda s: False)
    sink = []
    monkeypatch.setattr(fs, "_cmux", _fake_cmux("some output\n❯ ", sink))
    assert fs.wake_if_idle("S", "wake up") is True
    verbs = [a[0] for a in sink]
    assert "send" in verbs and "send-key" in verbs           # injected + submitted


def test_wake_if_idle_skips_when_busy(fs, monkeypatch):
    monkeypatch.setattr(fs, "surface_busy", lambda s: True)
    sink = []
    monkeypatch.setattr(fs, "_cmux", _fake_cmux("❯ ", sink))
    assert fs.wake_if_idle("S", "wake up") is False
    assert sink == []                                        # tier 1: never even reads/injects


def test_wake_if_idle_preserves_human_draft(fs, monkeypatch):
    monkeypatch.setattr(fs, "surface_busy", lambda s: False)
    sink = []
    monkeypatch.setattr(fs, "_cmux", _fake_cmux("❯ a half-typed thought", sink))
    assert fs.wake_if_idle("S", "wake up") is False
    assert [a for a in sink if a[0] in ("send", "send-key")] == []   # draft not clobbered


def test_wake_if_idle_empty_read_skips(fs, monkeypatch):
    # empty-read path: a bad/stale screen read must NOT wake (retry+heartbeat catch it) and must NOT
    # inject blindly into a pane we cannot see.
    monkeypatch.setattr(fs, "surface_busy", lambda s: False)
    sink = []
    monkeypatch.setattr(fs, "_cmux", _fake_cmux("", sink))
    assert fs.wake_if_idle("S", "wake up") is False
    assert [a for a in sink if a[0] in ("send", "send-key")] == []


def test_wake_if_idle_garbage_read_skips(fs, monkeypatch):
    # garbage read (no compose prompt at all) -> no wake, no injection.
    monkeypatch.setattr(fs, "surface_busy", lambda s: False)
    sink = []
    monkeypatch.setattr(fs, "_cmux", _fake_cmux("\x1b[2Jgarbled tool output 42%|", sink))
    assert fs.wake_if_idle("S", "wake up") is False
    assert [a for a in sink if a[0] in ("send", "send-key")] == []


def test_stale_running_no_longer_silences_idle_surface(fs, monkeypatch):
    # THE cmux-advisor acceptance (real surface_busy, not stubbed): an orphaned 5h-stale 'running'
    # record, but the agent is actually at a clean idle prompt. Pre-fix, lifecycle()=='running'
    # short-circuited and the wake was skipped forever; now staleness + the screen arbitrate -> wake.
    monkeypatch.setattr(fs, "read_hook_store",
                        lambda: _store(_rec("S", "u1", "running", age_s=5 * 3600)))
    sink = []
    monkeypatch.setattr(fs, "_cmux", _fake_cmux("idle at prompt\n❯ ", sink))
    assert fs.wake_if_idle("S", "you have a completion") is True
    assert [a[0] for a in sink].count("send") == 1


def test_genuine_midturn_still_not_woken(fs, monkeypatch):
    # regression companion: a genuinely mid-turn surface (fresh 'running') is never interrupted.
    monkeypatch.setattr(fs, "read_hook_store",
                        lambda: _store(_rec("S", "u1", "running", age_s=2)))
    sink = []
    monkeypatch.setattr(fs, "_cmux", _fake_cmux("❯ ", sink))
    assert fs.wake_if_idle("S", "x") is False
    assert sink == []


def test_surface_busy_survives_summarizer_stomp_shape(fs, monkeypatch):
    # The memsearch summarizer stomp (and any nested-claude Agent subagent) is the SAME class as the
    # reboot-orphan: a NON-live nested session leaves a FRESH 'running' record on the parent's surface,
    # outranking the parent's real (idle) bound session by max-updatedAt. The upstream fix is
    # `env -u CMUX_SURFACE_ID` in memsearch's stop.sh (out of THIS repo, still active per the design);
    # this asserts the wake gate is ROBUST to the stomp even if that isolation ever regresses — the
    # bound-session cross-check keeps the foreign fresh-'running' record from reading as busy.
    fs.live_put("advisor", {"surface": "S", "session": f"claude-{_BOUND}", "kind": "conductor"})
    monkeypatch.setattr(fs, "read_hook_store", lambda: _store(
        _rec("S", _ORPHAN, "running", age_s=1),         # the summarizer's fresh nested 'running' stomp
        _rec("S", _BOUND, "idle", age_s=20)))           # the conductor's real bound session, idle
    assert fs.surface_busy("S") is False                # not fooled -> the wake still fires


# --- draft-through: tier 3, opt-in clobber-with-log (design 2.3, Phase 4) -------------------------
def test_draft_through_defaults_stale(fs):
    assert fs.draft_through() == "stale"                 # default = the stale-draft gate (not preserve)


def test_draft_through_clobber_and_preserve_are_opt_in(fs):
    with open(fs.DRAFTMODE, "w") as f:
        f.write("clobber")
    assert fs.draft_through() == "clobber"
    with open(fs.DRAFTMODE, "w") as f:
        f.write("preserve")
    assert fs.draft_through() == "preserve"


def test_wake_preserves_fresh_draft_by_default(fs, monkeypatch):
    # default 'stale' gate: a FRESH draft (just appeared) is preserved -> protects active typing.
    monkeypatch.setattr(fs, "surface_busy", lambda s: False)
    sink = []
    monkeypatch.setattr(fs, "_cmux", _fake_cmux("❯ my half-typed draft", sink))
    assert fs.wake_if_idle("S", "wake") is False
    assert [a for a in sink if a[0] in ("send", "send-key")] == []   # not clobbered while fresh


def test_wake_clobbers_stale_draft_by_default(fs, monkeypatch):
    # default 'stale' gate: a WALKED-AWAY draft (unchanged >= DRAFT_STALE_S) is clobbered + woken so it
    # can never silence the surface indefinitely (the codex should-fix acceptance).
    draft = "my half-typed draft"
    with open(fs.DRAFTMARKS, "w") as f:                  # seed the mark: draft has sat unchanged, abandoned
        json.dump({"S": {"text": draft, "since": time.time() - (fs.DRAFT_STALE_S + 30)}}, f)
    monkeypatch.setattr(fs, "surface_busy", lambda s: False)
    sink = []
    monkeypatch.setattr(fs, "_cmux", _fake_cmux(f"❯ {draft}", sink))
    assert fs.wake_if_idle("S", "wake") is True
    assert ("send-key", "--surface", "S", "ctrl+u") in sink          # best-effort clear
    assert any(a[0] == "send" for a in sink)                         # then woke
    events = [json.loads(l) for l in open(fs.LOG) if l.strip()]
    assert any(e["event"] == "draft_clobbered" for e in events)      # ledger audit


def test_draft_age_resets_when_draft_changes(fs):
    # active typing (changed text) restarts the clock -> a person who keeps typing is never clobbered.
    now = time.time()
    assert fs._draft_age("S", "abc", now) == 0.0                     # first sight -> 0
    assert fs._draft_age("S", "abc", now + 100) == 100               # unchanged -> accrues
    assert fs._draft_age("S", "abcd", now + 101) == 0.0             # changed -> reset to 0


def test_wake_clobbers_any_draft_when_clobber_opt_in(fs, monkeypatch):
    # 'clobber' opt-in -> immediate clobber even for a FRESH draft (no stale wait).
    with open(fs.DRAFTMODE, "w") as f:
        f.write("clobber")
    monkeypatch.setattr(fs, "surface_busy", lambda s: False)
    sink = []
    monkeypatch.setattr(fs, "_cmux", _fake_cmux("❯ fresh draft", sink))
    assert fs.wake_if_idle("S", "wake") is True
    assert ("send-key", "--surface", "S", "ctrl+u") in sink


def test_wake_preserve_opt_in_never_clobbers(fs, monkeypatch):
    # 'preserve' opt-in -> never clobber, even a long-stale draft (the fully conservative choice).
    draft = "left this here"
    with open(fs.DRAFTMODE, "w") as f:
        f.write("preserve")
    with open(fs.DRAFTMARKS, "w") as f:
        json.dump({"S": {"text": draft, "since": time.time() - (fs.DRAFT_STALE_S + 999)}}, f)
    monkeypatch.setattr(fs, "surface_busy", lambda s: False)
    sink = []
    monkeypatch.setattr(fs, "_cmux", _fake_cmux(f"❯ {draft}", sink))
    assert fs.wake_if_idle("S", "wake") is False
    assert [a for a in sink if a[0] in ("send", "send-key")] == []


# --- expected-close tombstones (CLI <-> router registry-hygiene handshake, fleet-doctor #5) ------------
def test_expected_close_fresh_is_recent(fs):
    fs.expected_close_put("S1", now=1000.0)
    assert fs.expected_close_recent("S1", now=1000.0) is True
    assert fs.expected_close_recent("S1", now=1000.0 + fs.EXPECTED_CLOSE_S - 1) is True


def test_expected_close_expires(fs):
    fs.expected_close_put("S1", now=1000.0)
    assert fs.expected_close_recent("S1", now=1000.0 + fs.EXPECTED_CLOSE_S + 1) is False   # window passed


def test_expected_close_missing_file_is_not_recent(fs):
    # no tombstone file at all -> a GENUINE external close is never suppressed (fail-open).
    assert fs.expected_close_recent("NEVER-CLOSED") is False


def test_expected_close_only_matches_its_own_surface(fs):
    fs.expected_close_put("S1", now=1000.0)
    assert fs.expected_close_recent("S2", now=1000.0) is False


def test_expected_close_prunes_expired_on_write(fs):
    fs.expected_close_put("OLD", now=1000.0)
    fs.expected_close_put("NEW", now=1000.0 + fs.EXPECTED_CLOSE_S + 5)   # write prunes the expired OLD row
    import json
    rows = json.load(open(fs.EXPECTED_CLOSE))
    ids = [r["surface_id"] for r in rows]
    assert ids == ["NEW"]                                          # OLD pruned, file can't grow unboundedly


def test_expected_close_empty_surface_is_noop(fs):
    fs.expected_close_put("", now=1000.0)                          # empty id -> no row written, no crash
    assert fs.expected_close_recent("", now=1000.0) is False
