"""Layer 2 — fleet_state transitions (inbox / registry / archive / dial).

In-process against the throwaway STATE dir (wiped per test by the clean_state fixture). Covers the
unified inbox, per-kind acks (the bug fix that a completion-ack must not swallow an unread peer),
seq monotonicity, the live registry, the archive shelf, and the notify-mode dial.
"""
import json
import os


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


# --- the notify-mode dial ------------------------------------------------------------------------
def test_mode_defaults_passive(fs):
    assert fs.mode() == "passive"
    assert fs.autodrain_on() is False
    assert fs.idlewake_on() is False


def test_mode_autodrain(fs):
    with open(fs.MODEFILE, "w") as f:
        f.write("autodrain\n")
    assert fs.mode() == "autodrain"
    assert fs.autodrain_on() is True
    assert fs.idlewake_on() is False


def test_mode_auto(fs):
    with open(fs.MODEFILE, "w") as f:
        f.write("auto")
    assert fs.autodrain_on() is True
    assert fs.idlewake_on() is True


# --- the event ledger ----------------------------------------------------------------------------
def test_log_event_appends(fs):
    fs.log_event("launched", label="w1", role="worker")
    fs.log_event("removed", label="w1")
    lines = [json.loads(x) for x in open(fs.LOG) if x.strip()]
    assert [r["event"] for r in lines] == ["launched", "removed"]
    assert lines[0]["label"] == "w1" and "ts" in lines[0]


def test_atomic_write_creates_dirs(fs, state_dir):
    target = os.path.join(state_dir, "nested", "deep", "f.json")
    fs._atomic_write(target, json.dumps({"ok": True}))
    assert json.load(open(target))["ok"] is True
