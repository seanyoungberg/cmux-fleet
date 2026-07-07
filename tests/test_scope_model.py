"""The unified `--scope mine|all|conductors|children` model (ratified 2026-07-07).

One scoping vocabulary on every scope-aware verb; the only thing that varies is the DEFAULT — reads
(ls/vitals/inbox/graph) default `mine`, acts (recycle/broadcast) require an explicit scope. Here we cover
the shared predicate + resolver in `state`, the read default + empty-mine hint (via `ls`), and the act
verbs' scope routing (recycle/broadcast) + that the dropped legacy aliases are gone + the bulk mute.
vitals/graph scope filtering lives in test_features (pure row helpers).
"""
import sys

import pytest

from cmux_fleet import cli as fleet


def _seed(fs):
    """A two-conductor fleet: advisor owns k1/k2; peer owns otherkid. Lets `mine` (advisor) be tested
    against a sibling conductor's child that must NOT leak in."""
    fs.live_put("advisor", {"surface": "SA", "kind": "conductor", "role": "adv"})
    fs.live_put("k1", {"surface": "S1", "kind": "child", "role": "dev", "parent": "advisor", "session": "claude-x"})
    fs.live_put("k2", {"surface": "S2", "kind": "child", "role": "dev", "parent": "advisor", "session": "claude-y"})
    fs.live_put("peer", {"surface": "SP", "kind": "conductor", "role": "c2"})
    fs.live_put("otherkid", {"surface": "SO", "kind": "child", "role": "dev", "parent": "peer", "session": "claude-z"})


def _labels(out):
    """Member-row labels from an `ls` dump: rows indent by two spaces; skip the '(' legend/hint lines."""
    return {ln.split()[0] for ln in out.splitlines()
            if ln.startswith("  ") and ln.split() and not ln.strip().startswith("(")}


# ── the shared predicate: state.scope_matches / scope_members ─────────────────────────────────
def test_scope_matches_mine_children_and_self(fs):
    _seed(fs)
    assert fs.scope_matches("mine", fs.live_get("k1"), "k1", "advisor", include_self=True)
    assert fs.scope_matches("mine", fs.live_get("advisor"), "advisor", "advisor", include_self=True)   # self for READS
    assert not fs.scope_matches("mine", fs.live_get("advisor"), "advisor", "advisor", include_self=False)  # not for ACTS
    assert not fs.scope_matches("mine", fs.live_get("otherkid"), "otherkid", "advisor", include_self=True)  # sibling's kid


def test_scope_members_by_kind_and_mine(fs):
    _seed(fs)
    assert {l for l, _ in fs.scope_members("conductors", "", include_self=False)} == {"advisor", "peer"}
    assert {l for l, _ in fs.scope_members("children", "", include_self=False)} == {"k1", "k2", "otherkid"}
    assert {l for l, _ in fs.scope_members("all", "", include_self=False)} == {"advisor", "k1", "k2", "peer", "otherkid"}
    assert {l for l, _ in fs.scope_members("mine", "advisor", include_self=False)} == {"k1", "k2"}       # act's mine = children


# ── the read resolver: state.read_scope (default + graceful no-surface fallback) ──────────────
def test_read_scope_default_mine_with_surface(fs, monkeypatch):
    fs.live_put("advisor", {"surface": "SA", "kind": "conductor", "role": "adv"})
    monkeypatch.setenv("CMUX_SURFACE_ID", "SA")
    assert fs.read_scope(None, "ls") == ("mine", "advisor")


def test_read_scope_omitted_no_surface_falls_back_to_all(fs, monkeypatch):
    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
    assert fs.read_scope(None, "ls") == ("all", "")            # human at a plain shell -> the world


def test_read_scope_explicit_mine_no_surface_exits(fs, monkeypatch):
    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
    with pytest.raises(SystemExit):
        fs.read_scope("mine", "ls")                            # you named identity-relative scope w/o identity


def test_read_scope_sets_only_rejects_label_but_graph_allows(fs, monkeypatch):
    monkeypatch.setenv("CMUX_SURFACE_ID", "SA")
    with pytest.raises(SystemExit):
        fs.read_scope("somelabel", "ls")                       # ls/vitals: a bare label isn't a listing scope
    assert fs.read_scope("somelabel", "graph", sets_only=False)[0] == "somelabel"   # graph/inbox single-target


# ── read default + scoping via `ls` ───────────────────────────────────────────────────────────
def test_ls_default_mine_shows_self_and_children(fs, monkeypatch, capsys):
    _seed(fs)
    monkeypatch.setenv("CMUX_SURFACE_ID", "SA")
    fleet.cmd_ls([])
    out = capsys.readouterr().out
    assert "mine:" in out                                       # scope tag in the header
    assert _labels(out) == {"advisor", "k1", "k2"}             # self + own children; sibling's fleet excluded


def test_ls_scope_all_shows_world(fs, monkeypatch, capsys):
    _seed(fs)
    monkeypatch.setenv("CMUX_SURFACE_ID", "SA")
    fleet.cmd_ls(["--scope", "all"])
    out = capsys.readouterr().out
    assert _labels(out) == {"advisor", "k1", "k2", "peer", "otherkid"}


def test_ls_scope_conductors_and_children(fs, monkeypatch, capsys):
    _seed(fs)
    monkeypatch.setenv("CMUX_SURFACE_ID", "SA")
    fleet.cmd_ls(["--scope", "conductors"])
    assert _labels(capsys.readouterr().out) == {"advisor", "peer"}
    fleet.cmd_ls(["--scope", "children"])
    assert _labels(capsys.readouterr().out) == {"k1", "k2", "otherkid"}


def test_ls_empty_mine_prints_hint(fs, monkeypatch, capsys):
    fs.live_put("lonely", {"surface": "SL", "kind": "conductor", "role": "c"})
    monkeypatch.setenv("CMUX_SURFACE_ID", "SL")
    fleet.cmd_ls([])
    assert "only you — no children" in capsys.readouterr().out


def test_ls_bad_scope_exits(fs, monkeypatch):
    monkeypatch.setenv("CMUX_SURFACE_ID", "SA")
    with pytest.raises(SystemExit):
        fleet.cmd_ls(["--scope", "bogus"])


# ── act verb: recycle (bare = self; --scope = gated bulk; dropped legacy flags error) ──────────
def test_recycle_scope_maps_to_bulk_target(fs, monkeypatch):
    monkeypatch.setenv("CMUX_SURFACE_ID", "SA")
    calls = []
    monkeypatch.setattr(fleet, "_recycle_bulk", lambda target, mode, caller, a: calls.append(target) or 0)
    for scope, target in [("mine", "my-children"), ("all", "all"),
                          ("conductors", "conductors"), ("children", "children")]:
        fleet.cmd_recycle(["--scope", scope])
        assert calls[-1] == target                            # act's mine -> your children


@pytest.mark.parametrize("flag", ["--all", "--conductors", "--children", "--my-children"])
def test_recycle_legacy_bulk_flags_removed(fs, monkeypatch, flag):
    # the v0.5.0 hidden bulk aliases are GONE (dropped 2026-07-07, no external users): --scope is the
    # only bulk selector, so a bare legacy flag is now an unrecognized argument.
    monkeypatch.setattr(fleet, "_recycle_bulk", lambda *a, **k: 0)
    with pytest.raises(SystemExit):
        fleet.cmd_recycle([flag])


def test_recycle_bad_scope_exits(fs):
    with pytest.raises(SystemExit):
        fleet.cmd_recycle(["--scope", "bogus"])


# ── act verb: broadcast (--scope REQUIRED; dropped --target alias errors) ──────────────────────
def test_broadcast_requires_scope(fs, monkeypatch):
    monkeypatch.setenv("CMUX_SURFACE_ID", "SA")
    with pytest.raises(SystemExit):
        fleet.cmd_broadcast(["hello"])                        # an act: no default fan-out


def test_broadcast_scope_mine_selects_children(fs, monkeypatch, capsys):
    _seed(fs)
    monkeypatch.setenv("CMUX_SURFACE_ID", "SA")
    fleet.cmd_broadcast(["msg", "--scope", "mine", "--dry-run"])
    out = capsys.readouterr().out
    assert "scope mine" in out
    labels = _labels(out)
    assert "k1" in labels and "k2" in labels and "peer" not in labels and "otherkid" not in labels


def test_broadcast_legacy_target_flag_removed(fs, monkeypatch):
    # `--target` (the v0.5.0 hidden alias) is GONE: --scope is the only selector, so --target no longer
    # picks a scope and the act's required-scope guard fires.
    _seed(fs)
    monkeypatch.setenv("CMUX_SURFACE_ID", "SA")
    with pytest.raises(SystemExit):
        fleet.cmd_broadcast(["msg", "--target", "my-children", "--dry-run"])


# ── mute --scope mine (bulk, children only) ───────────────────────────────────────────────────
def test_mute_scope_mine_toggles_only_my_children(fs, monkeypatch):
    _seed(fs)
    monkeypatch.setenv("CMUX_SURFACE_ID", "SA")
    fleet.cmd_mute(["--scope", "mine"], mute=True)
    assert fs.live_get("k1").get("muted") is True and fs.live_get("k2").get("muted") is True
    assert fs.live_get("otherkid").get("muted") is not True   # sibling's child untouched
    fleet.cmd_mute(["--scope", "mine"], mute=False)
    assert "muted" not in fs.live_get("k1") and "muted" not in fs.live_get("k2")
