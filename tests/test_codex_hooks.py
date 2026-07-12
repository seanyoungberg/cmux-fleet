"""cmux's hook wiring, per codex SEAT HOME — the completion push the seat migration severed.

THE REGRESSION. cmux wires a codex worker's Stop hook by writing a `hooks.json` into the codex home, and it
only ever wrote one into `~/.codex`. When the fleet moved every seat into its OWN home (per-seat
CODEX_HOME), it moved every codex worker out of the one home that had hooks. Since then a seat worker has
fired Stop into a home with no hooks: no bus event, no router, no completion, and a conductor waiting
forever on an agent that finished minutes ago. Codex was never the problem — it HAS a Stop hook, and it
fires (SessionStart and Stop fire; SessionEnd does not).

TRUST IS THE HALF THAT FAILS SILENTLY, and it is why these tests insist on both halves. Codex will not run
an untrusted hook, and under `exec` it does not prompt and does not complain — it just skips it. Trust is a
content-bound `trusted_hash` in the home's own config.toml, so a hooks.json installed without a matching
trust entry is EXACTLY as dead as no hooks.json at all, while looking installed. (Verified against
codex-cli 0.144.1 in a throwaway home: hooks.json alone -> hook never fired; + trusted_hash -> fired, with
no feature flag and no bypass flag; stale hash after a command change -> silently stopped firing again.)
"""
import os

from cmux_fleet import providers as pv


REAL_KEY = '[hooks.state."{path}:stop:0:0"]\ntrusted_hash = "sha256:abc123"\n'


def _home(tmp_path, *, hooks=True, trusted=True):
    home = tmp_path / "codex-home"
    home.mkdir()
    if hooks:
        (home / "hooks.json").write_text('{"hooks":{"Stop":[]}}')
    cfg = ""
    if trusted:
        cfg = REAL_KEY.format(path=os.path.realpath(str(home / "hooks.json")))
    (home / "config.toml").write_text(cfg)
    return str(home)


def test_hooks_plus_trust_is_the_only_ok_state(tmp_path):
    assert pv.codex_hooks_ok(_home(tmp_path)) is True


def test_NO_hooks_file_is_not_ok(tmp_path):
    """The seat homes as they stand today: no hooks.json at all."""
    assert pv.codex_hooks_ok(_home(tmp_path, hooks=False)) is False


def test_UNTRUSTED_hooks_are_NOT_ok_even_though_the_file_is_right_there(tmp_path):
    """THE silent failure. hooks.json present and perfect, no trust entry -> codex skips every hook without
    a word. An implementation that checks only for the FILE reports this home healthy and ships a worker
    whose completions go nowhere — the exact bug, reintroduced while looking fixed."""
    assert pv.codex_hooks_ok(_home(tmp_path, trusted=False)) is False


def test_trust_for_a_DIFFERENT_hooks_path_does_not_count(tmp_path):
    """Trust is keyed on the realpath of the hooks.json it trusts. A trust entry pointing at ~/.codex's
    hooks.json says nothing about THIS seat's — and every seat home would otherwise inherit a false pass
    from the one home that was set up correctly."""
    home = _home(tmp_path, trusted=False)
    with open(os.path.join(home, "config.toml"), "w") as f:
        f.write(REAL_KEY.format(path="/Users/somebody/.codex/hooks.json"))
    assert pv.codex_hooks_ok(home) is False


def test_install_reports_failure_when_cmux_is_missing(tmp_path, monkeypatch):
    """FAIL-OPEN, but never fail-SILENT: if the wiring cannot be installed the launch must still happen,
    and the operator must be told the worker's completions will not arrive."""
    monkeypatch.setattr(pv, "CMUX", "/nonexistent/cmux")
    ok, detail = pv.codex_install_hooks(str(tmp_path / "fresh"))
    assert ok is False
    assert "cmux" in detail.lower()


def test_install_shells_out_with_CODEX_HOME_pointed_at_THIS_seat(tmp_path, monkeypatch):
    """The whole point is the per-seat home. `cmux hooks codex install` picks its target from $CODEX_HOME,
    so passing the wrong one would wire hooks into somebody else's seat and leave this one dark."""
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["home"] = (kw.get("env") or {}).get("CODEX_HOME")
        import subprocess as sp
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pv.subprocess, "run", fake_run)
    monkeypatch.setattr(pv, "codex_hooks_ok", lambda h: True)
    home = str(tmp_path / "seat-a")
    ok, _ = pv.codex_install_hooks(home)

    assert ok is True
    assert seen["home"] == home, "installed hooks into the wrong codex home"
    assert seen["argv"][1:] == ["hooks", "codex", "install", "--yes"]


def test_install_does_NOT_pass_a_bypass_flag(tmp_path, monkeypatch):
    """`--dangerously-bypass-hook-trust` runs UNTRUSTED hooks. Normalising that in the launch path would be
    a real security regression, and it is not needed: granting trust is what cmux's own installer does."""
    seen = {}
    monkeypatch.setattr(pv.subprocess, "run", lambda argv, **kw: seen.setdefault("argv", argv) or
                        __import__("subprocess").CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(pv, "codex_hooks_ok", lambda h: True)
    pv.codex_install_hooks(str(tmp_path / "seat"))
    assert not any("bypass" in a for a in seen["argv"])
