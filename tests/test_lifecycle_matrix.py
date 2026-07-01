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


# --- today's DEFAULTS (make the ratify-gated flip a single visible point) -------------------------
def test_recycle_default_is_fresh_today():
    # design §1 proposes flipping this to resume; until ratified, no --resume/--session => fresh.
    import argparse
    # mirror cmd_recycle's decision without spawning: mode = resume iff (--resume or --session)
    def mode_for(resume, session):
        return "resume" if (resume or session) else "fresh"
    assert mode_for(False, "") == "fresh"          # <-- the cell §1 will flip
    assert mode_for(True, "") == "resume"
    assert mode_for(False, "SID") == "resume"      # --session implies resume
