# tests/test_groups_view.py — `fleet groups`: the §2.G read-side org-chart BY LABEL, built from cmux's
# REAL group membership (resolve.group_members), not the registry's stored `group` field. The whole point
# is to render membership as AGENTS (not raw workspace:N refs) and to SURFACE registry-vs-cmux divergence
# — the class that cost a wrong "that's Berg's group" call (2026-07-15). These units stub the three cmux
# reads (_groups_view routes them all through resolve), so nothing touches a live cmux.
#
# NOTE: fetch the modules FRESH inside each test (`_mods`). test_features reloads config/state/resolve/
# features, so a top-level `import ... as` binding goes stale — the patch would land on the old module
# while _groups_view reads the reloaded one (isolation bug caught 2026-07-17).


def _mods():
    import cmux_fleet.features as ff
    import cmux_fleet.resolve as rs
    import cmux_fleet.state as fs
    return ff, rs, fs


def _setup(monkeypatch, live, ws_map, membership):
    """live: {label: row}; ws_map: {SURFACE_UPPER: ws_uuid}; membership: {gname: set(ws_uuid) | None}.
    Returns the (fresh) features module to call _groups_view / _groups_text on."""
    ff, rs, fs = _mods()
    monkeypatch.setattr(fs, "live_all", lambda: live)
    monkeypatch.setattr(rs, "surface_ws_map", lambda *a, **k: ws_map)
    monkeypatch.setattr(rs, "group_members", lambda name: membership.get(name))
    return ff


def test_groups_renders_members_by_label(monkeypatch):
    live = {"cond": {"surface": "S-C", "kind": "conductor", "group": "cond"},
            "kid":  {"surface": "S-K", "kind": "child", "group": "cond"}}
    ff = _setup(monkeypatch, live, {"S-C": "WS1", "S-K": "WS2"}, {"cond": {"WS1", "WS2"}})
    groups = ff._groups_view()
    assert len(groups) == 1
    g = groups[0]
    assert g["name"] == "cond" and g["owner"] == "cond" and g["readable"] is True
    assert g["members"] == ["cond", "kid"]                # cmux membership resolved to labels
    assert g["ghosts"] == [] and g["unfiled"] == []       # registry and cmux agree


def test_groups_flags_ghost_registry_files_but_cmux_does_not(monkeypatch):
    # registry files `kid` under `cond`, but cmux does NOT place kid's workspace in the group.
    live = {"cond": {"surface": "S-C", "kind": "conductor", "group": "cond"},
            "kid":  {"surface": "S-K", "kind": "child", "group": "cond"}}
    ff = _setup(monkeypatch, live, {"S-C": "WS1", "S-K": "WS2"}, {"cond": {"WS1"}})
    g = ff._groups_view()[0]
    assert g["members"] == ["cond"]
    assert g["ghosts"] == ["kid"]                          # filed by registry, not placed by cmux
    assert g["unfiled"] == []


def test_groups_flags_unfiled_cmux_places_but_registry_does_not(monkeypatch):
    # cmux places `kid` in the group's workspace, but the registry files kid under NO group.
    live = {"cond": {"surface": "S-C", "kind": "conductor", "group": "cond"},
            "kid":  {"surface": "S-K", "kind": "child", "group": ""}}
    ff = _setup(monkeypatch, live, {"S-C": "WS1", "S-K": "WS1"}, {"cond": {"WS1"}})
    g = ff._groups_view()[0]
    assert set(g["members"]) == {"cond", "kid"}
    assert g["unfiled"] == ["kid"]                         # cmux places it, registry does not file it
    assert g["ghosts"] == []


def test_groups_unreadable_membership_fails_closed(monkeypatch):
    # cmux reports no membership (dissolved / unreadable) -> readable False, NEVER "zero members".
    ff = _setup(monkeypatch, {"cond": {"surface": "S-C", "kind": "conductor", "group": "cond"}},
                {"S-C": "WS1"}, {"cond": None})
    g = ff._groups_view()[0]
    assert g["readable"] is False
    assert g["members"] == [] and g["filed"] == ["cond"]


def test_groups_empty_when_no_group(monkeypatch):
    ff = _setup(monkeypatch, {"solo": {"surface": "S", "kind": "child", "group": ""}}, {"S": "WS1"}, {})
    assert ff._groups_view() == []
    assert ff._groups_text([], {}) == "(no fleet groups — no live agent carries a group)"


def test_groups_owner_falls_back_to_conductor_among_members(monkeypatch):
    # the real fleet names groups "Conductor - <label>", so the group name matches NO label; owner must
    # resolve to the conductor actually living in the group, not stay blank.
    live = {"gcond": {"surface": "S-C", "kind": "conductor", "group": "Conductor - gcond"},
            "gkid":  {"surface": "S-K", "kind": "child", "group": "Conductor - gcond"}}
    ff = _setup(monkeypatch, live, {"S-C": "WS1", "S-K": "WS1"}, {"Conductor - gcond": {"WS1"}})
    g = ff._groups_view()[0]
    assert g["owner"] == "gcond"                           # found among members, not by name-match


def test_groups_text_marks_conductor_and_ghost(monkeypatch):
    live = {"cond": {"surface": "S-C", "kind": "conductor", "group": "cond"},
            "kid":  {"surface": "S-K", "kind": "child", "group": "cond"},
            "gh":   {"surface": "S-G", "kind": "child", "group": "cond"}}
    # cmux places cond+kid in WS1 (the group); gh sits in WS9 (not in the group) -> gh is a GHOST. kid is
    # both filed (group==cond) AND placed (WS1) -> a plain member.
    ff = _setup(monkeypatch, live, {"S-C": "WS1", "S-K": "WS1", "S-G": "WS9"}, {"cond": {"WS1"}})
    txt = ff._groups_text(ff._groups_view(), live)
    assert "cond  (conductor cond)" in txt                 # group head names its owning conductor
    assert "cond ⟵ conductor" in txt                       # the conductor row is marked
    assert "gh   (GHOST" in txt                            # filed under cond, cmux does not place it
    assert "(unfiled" not in txt                           # kid is filed AND placed -> a plain member
