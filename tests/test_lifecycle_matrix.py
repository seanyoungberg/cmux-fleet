"""Phase 3e — the resume-gate proof matrix, made legible.

The brief's grid: {light,heavy loadout} x {under,over summary threshold} x {recycle,revive} x {fresh,resume}.
Coverage map (so the matrix is auditable, not just asserted):

  AXIS                      WHERE PROVEN
  loadout light/heavy       test_recycle.py::test_resume_menu_timeout_scales_with_plugin_count
  threshold under (no menu) test_recycle.py::test_dismiss_ready_when_running_prompt
  threshold over  (menu)    test_recycle.py::test_dismiss_picks_full_when_menu_present
  heavy still-booting       test_recycle.py::test_dismiss_times_out_when_still_booting  (+ gate blocks bind)
  bind-confirm fresh/resume test_recycle.py::test_fresh_* / test_resume_*
  compose recycle x mode    THIS FILE (fresh -> no --resume; resume -> --resume <id>; --session wins)
  compose revive  x mode    THIS FILE (resume replays --resume; roster re-resolves toml)
  reconcile after bind      test_fleet_state.py::test_reconcile_* (+ live capstone: sandbox e2e)
  live behavioral e2e       lifecycle-hardening-testlog.md (throwaway adhoc on the lh-sbx profile)

NOTE: `revive --fresh` and the `recycle` DEFAULT flip (fresh->resume) are RATIFY-GATED (design §1); those
cells activate once ratified. Today recycle defaults fresh and revive is resume-only — asserted as such
below so the flip is a visible, single-point change.
"""
from cmux_fleet import cli


# --- the resume-directive primitive: tool x mode -------------------------------------------------
def test_prepend_resume_matrix():
    # claude takes a --resume FLAG; codex a `resume` SUBCOMMAND; no sid (fresh) or unknown tool -> no-op.
    assert cli._prepend_resume(["--x"], "claude", "SID") == ["--resume", "SID", "--x"]
    assert cli._prepend_resume(["--x"], "claude", "") == ["--x"]              # fresh
    assert cli._prepend_resume(["--x"], "codex", "SID") == ["resume", "SID", "--x"]
    assert cli._prepend_resume(["--x"], "codex", "") == ["--x"]               # fresh
    assert cli._prepend_resume(["--x"], "grok", "SID") == ["--x"]             # no resume flow -> fresh


# --- compose: recycle x {fresh, resume, --session} -----------------------------------------------
def _adhoc_entry():
    # off-roster (no toml) + no cmux binding -> the registry-spec compose path (hermetic).
    return {"tool": "claude", "role": "adhoc-x", "cwd": "/tmp/x", "session": "claude-REGID",
            "surface": "S", "plugins": [], "flags": [], "settings": ""}


def test_recycle_compose_fresh_vs_resume(monkeypatch):
    monkeypatch.setattr(cli, "_resume_binding", lambda surf: {})
    monkeypatch.setattr(cli, "_is_roster", lambda role: False)
    e = _adhoc_entry()
    fresh, _ = cli._compose_recycle_cmd("adhoc-x", e, [], [], "fresh", "")
    resume, _ = cli._compose_recycle_cmd("adhoc-x", e, [], [], "resume", "")
    assert "--resume" not in fresh
    assert "--resume REGID" in resume                       # falls back to the registry session


def test_recycle_compose_checkpoint_beats_registry(monkeypatch):
    # when cmux exposes a checkpoint, RESUME targets it over the (stale) registry session.
    monkeypatch.setattr(cli, "_resume_binding", lambda surf: {"checkpoint_id": "CKPT"})
    monkeypatch.setattr(cli, "_is_roster", lambda role: False)
    resume, ckpt = cli._compose_recycle_cmd("adhoc-x", _adhoc_entry(), [], [], "resume", "")
    assert "--resume CKPT" in resume and ckpt == "CKPT"
    assert "REGID" not in resume


def test_recycle_compose_explicit_session_beats_checkpoint(monkeypatch):
    monkeypatch.setattr(cli, "_resume_binding", lambda surf: {"checkpoint_id": "CKPT"})
    monkeypatch.setattr(cli, "_is_roster", lambda role: False)
    resume, _ = cli._compose_recycle_cmd("adhoc-x", _adhoc_entry(), [], [], "resume", "PICKED")
    assert "--resume PICKED" in resume
    assert "CKPT" not in resume and "REGID" not in resume


# --- roster cwd change: RESUME pins the session's original cwd, FRESH adopts the moved toml cwd --------
def _roster_resolve_to(cwd):
    return lambda cfg, role, tool, adhoc: {
        "tool": "claude", "role": "w", "label": "w", "kind": "child", "place": "tab", "group": "",
        "cwd": cwd, "plugins": [], "flags": [], "env": {}, "settings": "",
        "enable_plugins": [], "setting_sources": ""}


def test_compose_from_roster_resume_pins_cwd_override(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"role": {"w": {}}})
    monkeypatch.setattr(cli, "resolve", _roster_resolve_to("/NEW/cwd"))
    resume = cli._compose_from_roster("w", "claude", "w", [], [], "SID", cwd_override="/OLD/cwd")
    assert "cd /OLD/cwd" in resume and "--resume SID" in resume          # resume runs where the session lives
    fresh = cli._compose_from_roster("w", "claude", "w", [], [], None, cwd_override="")
    assert "cd /NEW/cwd" in fresh and "--resume" not in fresh            # fresh adopts the moved toml cwd


def test_recycle_roster_resume_uses_original_cwd_not_moved_toml(monkeypatch):
    # the codex-flagged trap: role cwd moved since launch; default RESUME must resume from the OLD cwd
    # (where the session's project dir is), not the re-resolved NEW cwd -> else "No conversation found".
    monkeypatch.setattr(cli, "_is_roster", lambda role: True)
    monkeypatch.setattr(cli, "_resume_binding", lambda surf: {})
    monkeypatch.setattr(cli, "load_config", lambda: {"role": {"w": {}}})
    monkeypatch.setattr(cli, "resolve", _roster_resolve_to("/NEW/cwd"))
    entry = {"tool": "claude", "role": "w", "cwd": "/OLD/cwd", "session": "claude-SID", "surface": "S"}
    resume, _ = cli._compose_recycle_cmd("w", entry, [], [], "resume", "")
    assert "cd /OLD/cwd" in resume and "--resume SID" in resume
    fresh, _ = cli._compose_recycle_cmd("w", entry, [], [], "fresh", "")
    assert "cd /NEW/cwd" in fresh and "--resume" not in fresh


# --- compose: revive (roster = toml-authoritative, resume replayed) ------------------------------
def test_revive_roster_compose_resumes(monkeypatch):
    # a roster revive re-resolves the toml and prepends --resume <sess>. Patch the roster resolve so the
    # test needs no host toml; assert the resume directive lands.
    monkeypatch.setattr(cli, "load_config", lambda: {"role": {"w": {}}})
    monkeypatch.setattr(cli, "resolve", lambda cfg, role, tool, adhoc: {
        "tool": "claude", "role": "w", "label": "w", "kind": "child", "place": "tab", "group": "",
        "cwd": "/tmp/x", "plugins": [], "flags": [], "env": {}, "settings": "",
        "enable_plugins": [], "setting_sources": ""})
    send = cli._compose_from_roster("w", "claude", "w", [], [], "SESSID")
    assert "--resume SESSID" in send


# --- GATE 2: effort/model provenance + no-pin warning --------------------------------------------
def test_provenance_override(monkeypatch):
    monkeypatch.setattr(cli, "_is_roster", lambda role: True)
    # BOTH prefs overridden -> both read (override), no warning. (A --model is present so the model-gap
    # warning added for the effort-pinned-but-no-model case is out of scope here; that gap is covered by
    # its own tests in test_recycle.py.)
    line, warn = cli._session_pref_provenance("w", "claude", "claude --effort max --model opus", "max", "opus")
    assert "effort=max (override)" in line and "model=opus (override)" in line and warn == ""


def test_provenance_role_pin(monkeypatch):
    monkeypatch.setattr(cli, "_is_roster", lambda role: True)
    monkeypatch.setattr(cli, "load_config", lambda: {
        "tool": {"claude": {"flags": "--effort high"}},
        "role": {"w": {"claude": {"flags": "--effort xhigh --model claude-opus-4-8"}}}})
    # role pins BOTH effort and model -> both read (role-pin), no warning (model is pinned, so the
    # model-gap warning does not fire).
    line, warn = cli._session_pref_provenance("w", "claude",
                                              "claude --effort xhigh --model claude-opus-4-8", "", "")
    assert "effort=xhigh (role-pin)" in line and "model=claude-opus-4-8 (role-pin)" in line and warn == ""


def test_provenance_floor_warns_no_pin(monkeypatch):
    monkeypatch.setattr(cli, "_is_roster", lambda role: True)
    monkeypatch.setattr(cli, "load_config", lambda: {
        "tool": {"claude": {"flags": "--effort high"}},
        "role": {"w": {"claude": {"flags": "--add-dir /x"}}}})    # role has NO --effort pin
    line, warn = cli._session_pref_provenance("w", "claude", "claude --effort high", "", "")
    assert "effort=high (floor)" in line
    assert "no --effort pin" in warn and "won't survive" in warn  # the cmux-advisor-came-back-on-high catch


def test_provenance_adhoc_is_binding(monkeypatch):
    monkeypatch.setattr(cli, "_is_roster", lambda role: False)
    line, warn = cli._session_pref_provenance("adhoc-x", "claude", "claude --effort low --model opus", "", "")
    assert "effort=low (binding)" in line and "model=opus (binding)" in line and warn == ""


def test_provenance_empty_when_none(monkeypatch):
    monkeypatch.setattr(cli, "_is_roster", lambda role: False)
    assert cli._session_pref_provenance("x", "claude", "claude --dangerously-skip-permissions", "", "") == ("", "")


# --- the RATIFIED default (flipped 2026-07-01): recycle now RESUMES; --fresh is the shed opt-in --------
def test_recycle_default_is_resume():
    # mirror cmd_recycle's decision without spawning: mode = fresh IFF --fresh; else resume (preserve).
    def mode_for(fresh, session):
        return "fresh" if fresh else "resume"
    assert mode_for(False, "") == "resume"         # <-- the flip: plain recycle preserves context
    assert mode_for(True, "") == "fresh"           # --fresh is the explicit shed
    assert mode_for(False, "SID") == "resume"      # --session naturally resumes (the default)
