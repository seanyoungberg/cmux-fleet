# tests/test_thin_registry.py — Ship 5 (thin-registry) schema v2 core: the to_v2/to_v1 transforms, the
# dual-shape accessors, the flocked writers (R1), and the `fleet migrate` one-shot. The load-bearing test
# is test_post_migrate_*: it proves the fleet's read/write paths keep working once the ON-DISK shape is v2,
# which is what makes the build migrate-ready before Berg runs `fleet migrate`.
import json
import os
import threading

import pytest

from cmux_fleet import state as fs      # noqa: E402


def _v1_row(**over):
    r = {"role": "worker", "kind": "child", "parent": "cond", "tool": "claude",
         "cwd": "/home/x", "place": "tab", "group": "g", "status": "live", "muted": False,
         "workspace": "WS-OLD", "surface": "SURF-1", "session": "claude-abc123"}
    r.update(over)
    return r


def _raw_live():
    """The ON-DISK fleet.json dict (bypasses live_all's flat-view derive)."""
    return fs._read_json(fs.LIVE, {})


# --- transforms ------------------------------------------------------------------------------------
def test_to_v2_nests_and_drops_derived():
    v2 = fs.to_v2(_v1_row())
    assert fs.is_v2(v2)
    assert set(v2["spec"]) >= {"cwd", "place", "group", "muted"}
    assert v2["binding"]["surface"] == "SURF-1"
    assert v2["binding"]["session_hint"] == "claude-abc123"
    assert v2["parent"] == "cond" and v2["role"] == "worker" and v2["gen"] == 1
    # the DRIFT fields are GONE, top-level AND out of spec
    assert "workspace" not in v2 and "status" not in v2
    assert "workspace" not in v2["spec"] and "status" not in v2["spec"]


def test_to_v2_idempotent():
    once = fs.to_v2(_v1_row())
    assert fs.to_v2(once) == once
    assert fs.to_v2(fs.to_v2(once)) == once


def test_to_v2_absorbs_top_level_overrides_on_a_v2_row():
    """The merge-write idiom `{**v2row, "place": x}` must land in spec, not be ignored."""
    v2 = fs.to_v2(_v1_row())
    merged = fs.to_v2({**v2, "place": "workspace", "group": "g2", "surface": "SURF-2", "session": "claude-zzz"})
    assert merged["spec"]["place"] == "workspace"
    assert merged["spec"]["group"] == "g2"
    assert merged["binding"]["surface"] == "SURF-2"
    assert merged["binding"]["session_hint"] == "claude-zzz"


def test_to_v1_roundtrip_preserves_non_derived():
    back = fs.to_v1(fs.to_v2(_v1_row()))
    for k in ("surface", "session", "cwd", "place", "group", "parent", "role", "kind", "tool"):
        assert back[k] == _v1_row()[k], k
    assert "workspace" not in back and "status" not in back    # derived stay dropped


def test_accessors_read_both_shapes():
    v1, v2 = _v1_row(), fs.to_v2(_v1_row())
    assert fs.e_surface(v1) == fs.e_surface(v2) == "SURF-1"
    assert fs.e_session(v1) == fs.e_session(v2) == "claude-abc123"
    assert fs.e_spec(v1, "cwd") == fs.e_spec(v2, "cwd") == "/home/x"
    assert fs.e_spec(v1, "group") == fs.e_spec(v2, "group") == "g"
    assert fs.e_gen(v2) == 1 and fs.e_gen({}) == 1


def test_archive_metadata_stays_top_level():
    arch = {"role": "w", "kind": "child", "tool": "claude", "cwd": "/x", "parent": "p",
            "last_session": "sess-1", "binding_cmd": "claude --resume x", "binding_cwd": "/x", "archived_at": 42}
    v2 = fs.to_v2(arch)
    assert v2["last_session"] == "sess-1" and v2["binding_cmd"] == "claude --resume x" and v2["archived_at"] == 42
    assert fs.e_spec(v2, "cwd") == "/x"


# --- the migrator ----------------------------------------------------------------------------------
def test_migrate_state_transforms_backs_up_and_flips_marker(fs):
    fs.live_put("a", _v1_row(surface="S-A"))
    fs.live_put("b", _v1_row(surface="S-B", parent=None))       # a 'poisoned' parent=None row
    fs.archive_put("z", {"role": "w", "kind": "child", "tool": "claude", "cwd": "/x",
                         "parent": "p", "last_session": "old", "place": "tab"})
    assert fs.schema_ver("fleet") == 1
    res = fs.migrate_state()
    assert res["fleet"] == 2 and res["archive"] == 1 and len(res["backups"]) == 2
    # on-disk is now v2
    raw = _raw_live()
    assert fs.is_v2(raw["a"]) and "workspace" not in raw["a"] and "status" not in raw["a"]
    assert fs.schema_ver("fleet") == 2 and fs.schema_ver("archive") == 2
    # a poisoned parent=None is PRESERVED, never invented
    assert raw["b"]["parent"] is None
    # archive row migrated, metadata intact
    assert fs.to_v2(_raw_archive()["z"])["last_session"] == "old"
    # the backups are readable v1
    for bak in res["backups"]:
        assert os.path.exists(bak)


def _raw_archive():
    return fs._read_json(fs.ARCHIVE, {})


def test_migrate_state_idempotent(fs):
    fs.live_put("boss", _v1_row(kind="conductor", parent=None))   # a valid known parent for "a"
    fs.live_put("a", _v1_row(parent="boss"))
    first = fs.migrate_state()
    disk1 = _raw_live()
    second = fs.migrate_state()                                 # re-run: already v2
    assert _raw_live() == disk1                                 # no further change
    assert second["fleet"] == 2


def test_migrate_normalizes_broken_parents_to_top_level_none(fs):
    """5d reconcile (Berg-ruled: normalize AT migrate). Collapse empty / self / dangling parents to the
    durable top-level None — the cf-conductor dead-surface-uuid case, the berg-sandbox empty case, a
    self-parent — while carrying a RESOLVABLE parent forward faithfully (the migrator fixes provably-broken
    edges only, it does not guess intent)."""
    fs.live_put("boss", _v1_row(surface="S-BOSS", kind="conductor", parent=None))    # top-level conductor
    fs.live_put("kid", _v1_row(surface="S-KID", parent="boss"))                       # a REAL edge -> keep
    fs.live_put("berg", _v1_row(surface="S-BERG", kind="conductor", parent=""))       # empty -> None
    fs.live_put("selfp", _v1_row(surface="S-SELF", kind="conductor", parent="selfp")) # self -> None
    fs.live_put("cf", _v1_row(surface="S-CF", kind="conductor",
                              parent="29A7AC52-0E69-43FC-B3D4-B6F5319BF5ED"))          # dead surface-uuid -> None
    fs.migrate_state()
    assert fs.live_get("kid").get("parent") == "boss"        # resolvable parent preserved faithfully
    assert fs.live_get("berg").get("parent") is None         # empty collapsed
    assert fs.live_get("selfp").get("parent") is None        # self collapsed
    assert fs.live_get("cf").get("parent") is None           # dangling surface-uuid collapsed
    # the derived predicate agrees with the normalized rep
    assert fs.is_top_level(fs.live_get("berg")) and fs.is_top_level(fs.live_get("cf"))
    assert not fs.is_top_level(fs.live_get("kid"))


# --- flocked writers (R1: the concurrent-reparent lost-update) -------------------------------------
def test_live_put_preserves_other_rows(fs):
    fs.live_put("a", _v1_row())
    fs.live_put("b", _v1_row())
    fs.live_del("a")
    assert fs.live_get("a") is None and fs.live_get("b") is not None


def test_flock_no_lost_update_under_concurrency(fs):
    """R1: many threads each hammering a DISTINCT label must all survive — the unlocked read-modify-write
    would drop rows (writer A reads {..}, B reads {..}, A writes, B writes -> A's row lost)."""
    labels = [f"L{i}" for i in range(24)]

    def worker(lbl):
        for _ in range(15):
            fs.live_put(lbl, _v1_row(surface=f"S-{lbl}"))

    threads = [threading.Thread(target=worker, args=(l,)) for l in labels]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    survivors = set(fs.live_all())
    assert survivors == set(labels), f"lost rows: {set(labels) - survivors}"


# --- migrate-READINESS: the fleet's read/write paths work on a v2 on-disk store --------------------
def test_post_migrate_readers_and_writers_work(fs):
    fs.live_put("kid", _v1_row(surface="S-KID"))
    fs.live_put("cond", _v1_row(kind="conductor", parent=None, surface="S-COND"))
    fs.migrate_state()
    assert fs.is_v2(_raw_live()["kid"])                         # on disk = v2

    # flat working view: existing `.get(...)` readers keep working
    kid = fs.live_get("kid")
    assert kid["surface"] == "S-KID" and kid["cwd"] == "/home/x" and kid["parent"] == "cond"
    assert kid["place"] == "tab" and kid["group"] == "g" and "workspace" not in kid

    # the surface indexes resolve on v2 rows
    assert fs.label_for_surface("S-KID") == "kid"
    assert fs.surface_for_label("kid") == "S-KID"
    assert fs.e_surface(fs.entry_for_surface("S-COND")) == "S-COND"

    # a merge-write persists as v2 (spec updated, derived not resurrected)
    fs.live_put("kid", {**kid, "place": "workspace", "group": "g2"})
    raw = _raw_live()["kid"]
    assert fs.is_v2(raw) and raw["spec"]["place"] == "workspace" and raw["spec"]["group"] == "g2"
    assert "workspace" not in raw and "status" not in raw

    # session reconcile writes to session_hint on a v2 row
    fs.reconcile_session("kid", "def456")
    assert fs.e_session(fs.live_get("kid")) == "claude-def456"
    assert _raw_live()["kid"]["binding"]["session_hint"] == "claude-def456"


def test_pre_migrate_writes_stay_v1_reversible(fs):
    """Before `fleet migrate`, live_put must persist FLAT v1 (no spec/binding on disk) so a rollback to
    pre-Ship-5 code still reads the store."""
    fs.live_put("kid", _v1_row())
    raw = _raw_live()["kid"]
    assert "spec" not in raw and "binding" not in raw and raw["surface"] == "SURF-1"
