"""Phase 3d — bulk / cross-conductor recycle selectors + the shared per-target plan.

Hermetic: seeds the live registry via the `fs` fixture; no cmux/subprocess. Covers selector filtering
(all/conductors/children/my-children), the ALWAYS-exclude-self rule (external recycle is the safe
topology), muted/human-driven skipping + --include-muted, unbound skipping, and that `_recycle_plan`
threads mode/prime correctly.
"""
import types

from cmux_fleet import cli


def _seed(fs):
    fs.live_put("me",      {"kind": "conductor", "surface": "SELF", "tool": "claude"})
    fs.live_put("peer",    {"kind": "conductor", "surface": "PEER", "tool": "claude"})
    fs.live_put("kidA",    {"kind": "child", "surface": "A", "parent": "me", "tool": "claude"})
    fs.live_put("kidB",    {"kind": "child", "surface": "B", "parent": "peer", "tool": "claude"})
    fs.live_put("muted1",  {"kind": "child", "surface": "M", "parent": "me", "tool": "claude", "muted": True})
    fs.live_put("unbound", {"kind": "child", "surface": "", "parent": "me", "tool": "claude"})


def test_all_excludes_self_and_unbound_and_muted(fs):
    _seed(fs)
    sel, skipped = cli._bulk_targets("all", "SELF", "me", include_muted=False)
    labels = [l for l, _ in sel]
    assert "me" not in labels           # self always excluded
    assert "unbound" not in labels      # no surface -> excluded
    assert "muted1" not in labels       # muted -> skipped by default
    assert set(labels) == {"peer", "kidA", "kidB"}
    assert [l for l, _ in skipped] == ["muted1"]


def test_conductors_selector(fs):
    _seed(fs)
    sel, _ = cli._bulk_targets("conductors", "SELF", "me", include_muted=False)
    assert [l for l, _ in sel] == ["peer"]   # not self, not children


def test_children_selector(fs):
    _seed(fs)
    sel, _ = cli._bulk_targets("children", "SELF", "me", include_muted=False)
    assert set(l for l, _ in sel) == {"kidA", "kidB"}   # both children, muted excluded


def test_my_children_selector(fs):
    _seed(fs)
    sel, _ = cli._bulk_targets("my-children", "SELF", "me", include_muted=False)
    assert [l for l, _ in sel] == ["kidA"]   # kidB's parent is peer, muted1 is muted


def test_include_muted_keeps_them(fs):
    _seed(fs)
    sel, skipped = cli._bulk_targets("my-children", "SELF", "me", include_muted=True)
    assert set(l for l, _ in sel) == {"kidA", "muted1"}
    assert skipped == []


def test_bulk_skips_stale_non_live(fs, monkeypatch):
    # a child whose surface is gone (lifecycle ended) but still recorded a session = STALE -> bulk must
    # skip it (else respawn-pane targets a gone UUID / the quiet-gate burns on a dead surface).
    fs.live_put("kidA", {"kind": "child", "surface": "A", "parent": "me", "tool": "claude", "session": "claude-x"})
    fs.live_put("kidB", {"kind": "child", "surface": "B", "parent": "me", "tool": "claude", "session": "claude-y"})
    monkeypatch.setattr(fs, "lifecycle", lambda surf: "ended" if surf == "A" else "idle")
    monkeypatch.setattr(fs, "surface_has_live_pid", lambda surf: surf == "B")   # B is the genuinely-live one
    sel, skipped = cli._bulk_targets("children", "SELF", "me", include_muted=False)
    assert [l for l, _ in sel] == ["kidB"]                  # only the live one
    assert ("kidA", "stale/non-live") in skipped


def test_bulk_skips_dead_pid_running_ghost(fs, monkeypatch):
    # round-2 gap (2026-07-06): a child FROZEN 'running' on a DEAD pid (SessionEnd-less brick) must read
    # STALE here too -- CONSISTENT with cmd_ls -- so a bulk sweep skips it (reported) rather than burning
    # the quiet-gate on a dead seat. The operator then recovers it with an explicit (pid-aware) recycle.
    fs.live_put("kidA", {"kind": "child", "surface": "A", "parent": "me", "tool": "claude", "session": "claude-x"})
    fs.live_put("kidB", {"kind": "child", "surface": "B", "parent": "me", "tool": "claude", "session": "claude-y"})
    monkeypatch.setattr(fs, "lifecycle", lambda surf: "running")               # BOTH read 'running'...
    monkeypatch.setattr(fs, "surface_has_live_pid", lambda surf: surf == "B")  # ...but A's process is DEAD
    sel, skipped = cli._bulk_targets("children", "SELF", "me", include_muted=False)
    assert [l for l, _ in sel] == ["kidB"]                  # only the genuinely-live one
    assert ("kidA", "stale/non-live") in skipped            # the dead-pid 'running' ghost skipped as stale


# --- the shared per-target plan ------------------------------------------------------------------
def test_recycle_plan_fresh_primes_from_handover(fs, monkeypatch):
    monkeypatch.setattr(cli, "_compose_recycle_cmd", lambda *a, **k: ("claude ...", ""))
    monkeypatch.setattr(cli, "_latest_handover", lambda cwd: "/x/handover/h.md")
    entry = {"kind": "child", "surface": "A", "tool": "claude", "role": "w", "cwd": "/x", "session": "claude-s"}
    p = cli._recycle_plan("kidA", entry, [], [], "fresh", "", False, None, False)
    assert p["mode"] == "fresh" and p["surface"] == "A" and p["old_session"] == "s"
    assert p["prime"] and "FRESH" in p["prime"] and "h.md" in p["prime"]


def test_recycle_plan_resume_has_no_prime(fs, monkeypatch):
    monkeypatch.setattr(cli, "_compose_recycle_cmd", lambda *a, **k: ("claude --resume s ...", ""))
    entry = {"kind": "child", "surface": "A", "tool": "claude", "role": "w", "cwd": "/x", "session": "claude-s"}
    p = cli._recycle_plan("kidA", entry, [], [], "resume", "", False, None, False)
    assert p["mode"] == "resume" and p["prime"] is None


# --- Fix 2: bulk recycle live-print shows each agent's RESOLVED model/effort (provenance). A bulk
#     recycle is exactly where a silent model/effort drift would slip by unseen, so the per-agent line
#     must surface what each agent is coming back on — not just mode=. ---------------------------------
def test_bulk_dryrun_prints_resolved_effort_model_per_agent(fs, monkeypatch, capsys):
    fs.live_put("me",   {"kind": "conductor", "surface": "SELF", "tool": "claude"})
    fs.live_put("kidA", {"kind": "child", "surface": "A", "parent": "me", "tool": "claude", "role": "w"})
    fs.live_put("kidB", {"kind": "child", "surface": "B", "parent": "me", "tool": "claude", "role": "w"})
    monkeypatch.setenv("CMUX_SURFACE_ID", "SELF")
    monkeypatch.setattr(fs, "lifecycle", lambda surf: "idle")            # both live (not stale)
    monkeypatch.setattr(cli, "_is_roster", lambda role: False)           # hermetic: no config read
    # compose emits the effort/model tokens the provenance reads back off the command.
    monkeypatch.setattr(cli, "_compose_recycle_cmd",
                        lambda *a, **k: ("cd /x && claude --effort xhigh --model opus", ""))
    a = types.SimpleNamespace(effort="xhigh", model="opus", dry_run=True, include_muted=False,
                              plugin=[], force=False, prime=None, no_prime=False)
    rc = cli._recycle_bulk("children", "resume", ["--effort", "xhigh", "--model", "opus"], a)
    out = capsys.readouterr().out
    assert rc == 0
    # BOTH targeted agents surface their resolved effort/model (override source), not just mode=
    assert out.count("effort=xhigh (override)") == 2
    assert out.count("model=opus (override)") == 2
    assert "mode=resume" in out
