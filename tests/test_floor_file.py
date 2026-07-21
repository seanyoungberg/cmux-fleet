# tests/test_floor_file.py — F1: the declarative floor FILE fleet places into an agent's cwd|home.
#
# Two seams:
#   resolve()          — floor_file composes per-tool (`[tool.<t>.floor_file]`) with a per-role override
#                        (`[role.<n>.<t>.floor_file]`), whole-table replace, string shorthand.
#   _place_floor_file  — the four modes (write|append|overwrite|symlink), clobber-safe + idempotent +
#                        fail-open, with the target defaulting per-tool (claude->cwd, codex->home).
import os

import pytest

from cmux_fleet import cli as fleet


# --- resolve(): the compose merge --------------------------------------------------------------------
def _cfg(tool_floor=None, role_floor=None, tool="claude"):
    tblock = {}
    if tool_floor is not None:
        tblock["floor_file"] = tool_floor
    rtool = {}
    if role_floor is not None:
        rtool["floor_file"] = role_floor
    return {
        "defaults": {"tool": tool},
        "tool": {tool: tblock},
        "role": {"r": {tool: rtool}},
    }


def test_resolve_floor_from_tool_layer():
    spec = fleet.resolve(_cfg(tool_floor={"source": "/f", "mode": "write"}), "r", None, None)
    assert spec["floor_file"] == {"source": "/f", "mode": "write"}


def test_resolve_role_overrides_tool_wholetable():
    # role floor REPLACES the tool floor (same semantics as settings/setting_sources), not a deep merge.
    cfg = _cfg(tool_floor={"source": "/tool", "mode": "append"}, role_floor={"source": "/role"})
    spec = fleet.resolve(cfg, "r", None, None)
    assert spec["floor_file"] == {"source": "/role"}


def test_resolve_string_shorthand_becomes_source():
    spec = fleet.resolve(_cfg(tool_floor="/just/a/path"), "r", None, None)
    assert spec["floor_file"] == {"source": "/just/a/path"}


def test_resolve_absent_floor_is_empty_dict():
    spec = fleet.resolve(_cfg(), "r", None, None)
    assert spec["floor_file"] == {}


def test_resolve_bad_type_floor_is_ignored():
    spec = fleet.resolve(_cfg(tool_floor=["not", "a", "table"]), "r", None, None)
    assert spec["floor_file"] == {}


# --- _place_floor_file(): target resolution ----------------------------------------------------------
def _spec(tmp_path, tool="claude", **floor):
    return {"tool": tool, "abs_cwd": str(tmp_path), "floor_file": floor}


def test_target_claude_defaults_to_cwd_claudemd():
    p = fleet._floor_target_path({"tool": "claude", "abs_cwd": "/cwd"}, {}, None)
    assert p == "/cwd/CLAUDE.md"


def test_target_codex_defaults_to_home_agentsmd(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    p = fleet._floor_target_path({"tool": "codex", "abs_cwd": "/cwd"}, {}, str(home))
    assert p == str(home / "AGENTS.md")


def test_target_filename_override(tmp_path):
    p = fleet._floor_target_path({"tool": "claude", "abs_cwd": str(tmp_path)},
                                 {"filename": "FLOOR.md"}, None)
    assert p == str(tmp_path / "FLOOR.md")


# --- _place_floor_file(): the modes ------------------------------------------------------------------
def test_unconfigured_is_noop(tmp_path):
    fleet._place_floor_file(_spec(tmp_path))               # no source
    assert list(tmp_path.iterdir()) == []


def test_write_places_into_empty_slot(tmp_path):
    fleet._place_floor_file(_spec(tmp_path, source="hello floor", mode="write"))
    assert (tmp_path / "CLAUDE.md").read_text() == "hello floor\n"


def test_write_never_clobbers_existing(tmp_path, capsys):
    dst = tmp_path / "CLAUDE.md"
    dst.write_text("USER OWNED\n")
    fleet._place_floor_file(_spec(tmp_path, source="new", mode="write"))
    assert dst.read_text() == "USER OWNED\n"               # untouched
    assert "not overwriting" in capsys.readouterr().out


def test_append_creates_fenced_block(tmp_path):
    fleet._place_floor_file(_spec(tmp_path, source="floor body", mode="append"))
    text = (tmp_path / "CLAUDE.md").read_text()
    assert fleet.FLOOR_FENCE_BEGIN in text and fleet.FLOOR_FENCE_END in text
    assert "floor body" in text


def test_append_is_the_default_mode(tmp_path):
    fleet._place_floor_file(_spec(tmp_path, source="floor body"))   # no mode -> append
    assert fleet.FLOOR_FENCE_BEGIN in (tmp_path / "CLAUDE.md").read_text()


def test_append_idempotent_no_duplicate_on_relaunch(tmp_path):
    s = _spec(tmp_path, source="floor body", mode="append")
    fleet._place_floor_file(s)
    once = (tmp_path / "CLAUDE.md").read_text()
    fleet._place_floor_file(s)                              # re-launch
    twice = (tmp_path / "CLAUDE.md").read_text()
    assert once == twice
    assert twice.count(fleet.FLOOR_FENCE_BEGIN) == 1        # exactly one fence, never duplicated


def test_append_preserves_user_text_outside_fence(tmp_path):
    dst = tmp_path / "CLAUDE.md"
    dst.write_text("# my own notes\nkeep me\n")
    fleet._place_floor_file(_spec(tmp_path, source="floor body", mode="append"))
    text = dst.read_text()
    assert "keep me" in text and "floor body" in text


def test_append_coexists_with_codex_citizenship_fence(tmp_path):
    # A codex AGENTS.md may already carry the citizenship fence; the floor fence is DISTINCT and both
    # must survive each other's re-write.
    from cmux_fleet import providers as pv
    dst = tmp_path / "AGENTS.md"
    dst.write_text(f"{pv.CITIZEN_FENCE_BEGIN}\ncitizen text\n{pv.CITIZEN_FENCE_END}\n")
    fleet._place_floor_file(_spec(tmp_path, tool="codex", source="floor body", mode="append",
                                  target="cwd"))
    text = dst.read_text()
    assert pv.CITIZEN_FENCE_BEGIN in text                  # citizenship survived
    assert fleet.FLOOR_FENCE_BEGIN in text                 # floor added
    # re-place the floor: citizenship still there, floor still single
    fleet._place_floor_file(_spec(tmp_path, tool="codex", source="floor body", mode="append",
                                  target="cwd"))
    text = dst.read_text()
    assert pv.CITIZEN_FENCE_BEGIN in text
    assert text.count(fleet.FLOOR_FENCE_BEGIN) == 1


def test_overwrite_replaces_whole_file(tmp_path):
    dst = tmp_path / "CLAUDE.md"
    dst.write_text("OLD everything\n")
    fleet._place_floor_file(_spec(tmp_path, source="brand new", mode="overwrite"))
    assert dst.read_text() == "brand new\n"


def test_symlink_places_relative_link(tmp_path):
    src = tmp_path / "src.md"
    src.write_text("linked floor\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    fleet._place_floor_file(_spec(cwd, source=str(src), mode="symlink"))
    link = cwd / "CLAUDE.md"
    assert link.is_symlink()
    assert not os.path.isabs(os.readlink(link))            # relative, matches the role-cwd convention
    assert link.read_text() == "linked floor\n"


def test_symlink_skips_existing_file(tmp_path):
    src = tmp_path / "src.md"
    src.write_text("linked\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "CLAUDE.md").write_text("REAL FILE\n")
    fleet._place_floor_file(_spec(cwd, source=str(src), mode="symlink"))
    assert (cwd / "CLAUDE.md").read_text() == "REAL FILE\n"   # not clobbered, not a link
    assert not (cwd / "CLAUDE.md").is_symlink()


def test_symlink_warns_on_inline_source(tmp_path, capsys):
    fleet._place_floor_file(_spec(tmp_path, source="inline not a file", mode="symlink"))
    assert not (tmp_path / "CLAUDE.md").exists()
    assert "needs source to be a readable FILE" in capsys.readouterr().out


def test_source_reads_a_file_path(tmp_path):
    src = tmp_path / "floor.md"
    src.write_text("from a file\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    fleet._place_floor_file(_spec(cwd, source=str(src), mode="write"))
    assert (cwd / "CLAUDE.md").read_text() == "from a file\n"


def test_codex_home_target(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    spec = {"tool": "codex", "abs_cwd": str(cwd), "floor_file": {"source": "codex floor", "mode": "write"}}
    fleet._place_floor_file(spec, codex_home=str(home))
    assert (home / "AGENTS.md").read_text() == "codex floor\n"   # home, not cwd
    assert not (cwd / "AGENTS.md").exists()


# --- fail-open ---------------------------------------------------------------------------------------
def test_unknown_mode_warns_and_skips(tmp_path, capsys):
    fleet._place_floor_file(_spec(tmp_path, source="x", mode="bogus"))
    assert list(tmp_path.iterdir()) == []
    assert "unknown" in capsys.readouterr().out


def test_unreadable_source_warns_and_skips(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(fleet, "_floor_source_text", lambda s: None)   # simulate an unreadable file path
    fleet._place_floor_file(_spec(tmp_path, source="/some/file", mode="write"))
    assert not (tmp_path / "CLAUDE.md").exists()
    assert "unreadable" in capsys.readouterr().out
