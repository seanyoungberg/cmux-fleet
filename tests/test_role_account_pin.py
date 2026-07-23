"""Role account-pin (review #7 seam) — a DURABLE per-seat account.

`[role.<role>.<tool>].account = "<name>"` layers ABOVE `[providers.<tool>].default`, resolved through the
ONE chokepoint (`_resolve_account_name`) that launch, recycle, and revive all share. This is the mechanism
that keeps a seat on its account across a recycle instead of silently reverting to the tool default (the
incident that motivated it: cmux-advisor's seat drifting off `berg-max` on respawn).

Precedence proven end-to-end: `--provider` flag (one-off) > role account-pin > `[providers.<tool>].default`.

Two layers of test:
  * subprocess `launch`/`config --dry-run` (the REAL cmd_launch path, fresh interpreter, CMUX_FLEET_TOML) —
    mirrors tests/test_providers.py's e2e launch tests.
  * in-process `recycle` (the shared-chokepoint re-resolution) — mirrors tests/test_provider_recycle.py.
Plus direct units on `providers.role_account`.
"""
import contextlib
import io
import os
import subprocess
import sys
import textwrap

import pytest

from cmux_fleet import cli as fleet           # noqa: E402
from cmux_fleet import providers as pv        # noqa: E402
from cmux_fleet import state as fleet_state   # noqa: E402


def _toml(tmp_path, body):
    p = tmp_path / "fleet.toml"
    p.write_text(textwrap.dedent(body))
    return str(p)


# A claude roster whose worker is PINNED to `berg-max`, while the tool default is a DIFFERENT account
# (`berglabs`). Distinct securestorage dirs make "which account won" unambiguous on the launch line.
def _pinned_body(pin_line='account = "berg-max"'):
    return f"""
        [tool.claude]
        flags = "--effort high"
        [role.worker]
        kind = "child"
        place = "tab"
        cwd = "worker"
        [role.worker.claude]
        {pin_line}
        [providers.claude]
        default = "berglabs"
        [providers.claude.berglabs]
        type = "subscription"
        auth = "securestorage:~/.claude-berglabs"
        [providers.claude.berg-max]
        type = "subscription"
        auth = "securestorage:~/.claude-berg-max"
    """


_PIN_DIR = os.path.expanduser("~/.claude-berg-max")       # the PINNED account's namespace
_DEFAULT_DIR = os.path.expanduser("~/.claude-berglabs")   # the tool default's namespace


def _launch(cli_env, tmp_path, body, *extra):
    env = dict(cli_env, CMUX_FLEET_TOML=_toml(tmp_path, body))
    return subprocess.run([sys.executable, "-m", "cmux_fleet", "launch", "worker", *extra, "--dry-run"],
                          env=env, capture_output=True, text=True)


# ================================ acceptance test 1 — pin composes at launch ======================
def test_launch_pin_composes_pinned_account(cli_env, tmp_path):
    p = _launch(cli_env, tmp_path, _pinned_body())
    assert p.returncode == 0, p.stderr
    launch = next(l for l in p.stdout.splitlines() if "[fleet] launch:" in l)
    assert "provider: claude:berg-max" in p.stdout                  # the PIN was resolved, not the default
    assert f"CLAUDE_SECURESTORAGE_CONFIG_DIR={_PIN_DIR}" in launch  # ...and its namespace is on the launch line
    assert _DEFAULT_DIR not in launch                               # the default did NOT win
    assert "CLAUDE_CONFIG_DIR=" not in launch                       # securestorage: keychain-only, no dir swap


# ================================ acceptance test 2 — pin removed -> tool default =================
def test_launch_pin_removed_falls_to_default(cli_env, tmp_path):
    # the healthy case stays reachable: strip the [role.worker.claude] account line, launch resolves the
    # [providers.claude].default exactly as before the seam existed.
    body = _pinned_body().replace('account = "berg-max"', "")
    p = _launch(cli_env, tmp_path, body)
    assert p.returncode == 0, p.stderr
    launch = next(l for l in p.stdout.splitlines() if "[fleet] launch:" in l)
    assert "provider: claude:berglabs" in p.stdout                  # the tool default
    assert f"CLAUDE_SECURESTORAGE_CONFIG_DIR={_DEFAULT_DIR}" in launch
    assert _PIN_DIR not in launch


# ================================ acceptance test 3 — flag overrides pin ==========================
def test_launch_flag_overrides_pin(cli_env, tmp_path):
    # the one-off --provider flag beats the durable pin (one-off by design), so an operator can send a
    # single launch to another account without editing the toml.
    p = _launch(cli_env, tmp_path, _pinned_body(), "--provider", "berglabs")
    assert p.returncode == 0, p.stderr
    launch = next(l for l in p.stdout.splitlines() if "[fleet] launch:" in l)
    assert "provider: claude:berglabs" in p.stdout                  # flag wins over the pin
    assert f"CLAUDE_SECURESTORAGE_CONFIG_DIR={_DEFAULT_DIR}" in launch
    assert _PIN_DIR not in launch


# ================================ acceptance test 5 — unknown pin -> loud ABORT ===================
def test_launch_pin_unknown_account_aborts(cli_env, tmp_path):
    # a pin naming an account with no [providers.claude.<name>] block must ABORT loudly, never fall back to
    # the ambient credential (same invariant as an unknown default).
    p = _launch(cli_env, tmp_path, _pinned_body('account = "ghost"'))
    assert p.returncode != 0                                        # refused
    out = p.stdout + p.stderr
    assert "ABORT" in out and "ghost" in out                        # names the offending account
    assert "[fleet] launch:" not in out                             # ...and never composed a spawn


# ================================ fleet config honesty (resolved account + source) ================
def test_config_shows_account_source_role_pin(cli_env, tmp_path):
    env = dict(cli_env, CMUX_FLEET_TOML=_toml(tmp_path, _pinned_body()))
    p = subprocess.run([sys.executable, "-m", "cmux_fleet", "config", "worker"],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "account: claude:berg-max" in p.stdout
    assert "source: role pin [role.worker.claude].account" in p.stdout


def test_config_shows_account_source_default(cli_env, tmp_path):
    body = _pinned_body().replace('account = "berg-max"', "")
    env = dict(cli_env, CMUX_FLEET_TOML=_toml(tmp_path, body))
    p = subprocess.run([sys.executable, "-m", "cmux_fleet", "config", "worker"],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "account: claude:berglabs" in p.stdout
    assert "source: default [providers.claude].default" in p.stdout


# ================================ acceptance test 4 — recycle re-resolution honors the pin ========
def _run(argv):
    """Drive a fleet verb capturing stdout; SystemExit re-raised for the abort assertions."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fleet.verb_table()[argv[0]]([*argv[1:]])
    return rc, buf.getvalue()


def _providers_toml(tmp_path, monkeypatch, body):
    p = tmp_path / "fleet.toml"
    p.write_text(textwrap.dedent(body))
    monkeypatch.setattr(pv, "FLEET_TOML", str(p))
    return str(p)


def _stub_roster(monkeypatch, role, tool, cwd="/x"):
    """Make `role` resolve to a minimal spec (no real toml roster read). The account pin is read SEPARATELY
    through pv.role_account (pv.FLEET_TOML), so the stubbed loadout does not hide it."""
    monkeypatch.setattr(fleet, "_is_roster", lambda r: r == role)
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {})
    monkeypatch.setattr(fleet, "load_config", lambda: {"role": {role: {}}, "tool": {tool: {}}})
    monkeypatch.setattr(fleet, "resolve", lambda *a: {
        "tool": tool, "role": role, "label": role, "kind": "child", "place": "tab", "group": "",
        "cwd": cwd, "plugins": [], "flags": [], "env": {}, "settings": "", "setting_sources": ""})


# the pin lives on the ROLE the registry recorded (`secure-child`); the tool default is a different account.
_RECYCLE_BODY = """
    [role.secure-child.claude]
    account = "berg-max"
    [providers.claude]
    default = "berglabs"
    [providers.claude.berglabs]
    type = "subscription"
    auth = "securestorage:~/.claude-berglabs"
    [providers.claude.berg-max]
    type = "subscription"
    auth = "securestorage:~/.claude-berg-max"
"""


def test_recycle_honors_pin_over_recorded_and_warns(fs, monkeypatch, tmp_path):
    # THE drift this feature kills: the agent was recorded under `berglabs` (the old tool default), but the
    # role is now PINNED to `berg-max`. A recycle used to revert to the default; it must now re-resolve the
    # PIN, inject its namespace, announce it, and warn LOUDLY that the account moved.
    _providers_toml(tmp_path, monkeypatch, _RECYCLE_BODY)
    fleet_state.live_put("w", {"role": "secure-child", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": "claude:berglabs"})
    _stub_roster(monkeypatch, "secure-child", "claude")
    rc, out = _run(["recycle", "w", "--dry-run"])
    assert rc == 0
    launch = next(l for l in out.splitlines() if "[fleet] launch:" in l)
    assert f"CLAUDE_SECURESTORAGE_CONFIG_DIR={_PIN_DIR}" in launch   # the PIN, re-resolved on recycle
    assert _DEFAULT_DIR not in launch                               # not the recorded/default account
    assert "[fleet] provider: claude:berg-max" in out              # announced
    assert "account MOVED" in out and "claude:berglabs'" in out    # loud move warning (recorded -> pin)


def test_recycle_pin_matches_recorded_no_warn(fs, monkeypatch, tmp_path):
    # once the recycle has settled the seat onto the pin, a subsequent recycle re-resolves the SAME account
    # -> inject + announce, but NO spurious move warning.
    _providers_toml(tmp_path, monkeypatch, _RECYCLE_BODY)
    fleet_state.live_put("w", {"role": "secure-child", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": "claude:berg-max"})
    _stub_roster(monkeypatch, "secure-child", "claude")
    rc, out = _run(["recycle", "w", "--dry-run"])
    assert rc == 0 and "[fleet] provider: claude:berg-max" in out
    assert "account MOVED" not in out


def test_recycle_unknown_pin_aborts(fs, monkeypatch, tmp_path):
    # a pin naming an unknown account aborts the recycle too (never a silent revert to ambient).
    _providers_toml(tmp_path, monkeypatch, """
        [role.secure-child.claude]
        account = "ghost"
        [providers.claude]
        default = "berglabs"
        [providers.claude.berglabs]
        type = "subscription"
        auth = "securestorage:~/.claude-berglabs"
    """)
    fleet_state.live_put("w", {"role": "secure-child", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": "claude:berglabs"})
    _stub_roster(monkeypatch, "secure-child", "claude")
    with pytest.raises(SystemExit) as ei:
        _run(["recycle", "w", "--dry-run"])
    assert "ABORT" in str(ei.value) and "ghost" in str(ei.value)


# ================================ units: providers.role_account ===================================
def test_role_account_reads_pin(monkeypatch, tmp_path):
    _providers_toml(tmp_path, monkeypatch, _RECYCLE_BODY)
    assert pv.role_account("claude", "secure-child") == "berg-max"


def test_role_account_absent_when_no_pin(monkeypatch, tmp_path):
    _providers_toml(tmp_path, monkeypatch, """
        [role.secure-child.claude]
        flags = "--effort high"
        [providers.claude]
        default = "berglabs"
    """)
    assert pv.role_account("claude", "secure-child") == ""        # a role block with no `account` key
    assert pv.role_account("claude", "other-role") == ""          # no such role block
    assert pv.role_account("codex", "secure-child") == ""         # no such tool sub-block


def test_role_account_ignores_provider_key(monkeypatch, tmp_path):
    # `provider` is deliberately NOT accepted as a pin — one name, one meaning (the registry's own
    # `provider` field records the RESOLVED choice, not the pin).
    _providers_toml(tmp_path, monkeypatch, """
        [role.secure-child.claude]
        provider = "berg-max"
        [providers.claude]
        default = "berglabs"
    """)
    assert pv.role_account("claude", "secure-child") == ""


def test_role_account_raises_on_unreadable_toml(monkeypatch, tmp_path):
    # unknown-is-not-absence: a broken toml must abort, never read as "no pin" and revert the seat.
    _providers_toml(tmp_path, monkeypatch, '[role.secure-child.claude]\naccount = "berg  # unterminated\n')
    with pytest.raises(pv.ProviderError) as ei:
        pv.role_account("claude", "secure-child")
    assert "unparseable" in str(ei.value)


def test_role_account_empty_when_no_toml(monkeypatch, tmp_path):
    monkeypatch.setattr(pv, "FLEET_TOML", str(tmp_path / "nope.toml"))
    assert pv.role_account("claude", "secure-child") == ""        # absent file -> no pin (opt-out), not an error


# ================================ scope-add 1 — securestorage seeded-guard ========================
# The silent-wrong-account hazard: an UNSEEDED securestorage namespace has no keychain item, so claude
# falls back to the ambient credential — a launch that BILLS the wrong account under a pin. The guard warns
# LOUDLY (never ABORT — an abort would block the /login-in-a-pane seeding bootstrap) on every spawn path.

class _Proc:
    def __init__(self, rc, stderr=""):
        self.returncode = rc
        self.stdout = ""
        self.stderr = stderr


def test_securestorage_seeded_probe_parses_existence(monkeypatch):
    # the REAL detector's parse: `security find-generic-password -s <svc>` exits 0 when the item exists
    # (seeded) and 44/errSecItemNotFound when it never was. Bypass the hermetic env seam; stub subprocess.
    monkeypatch.delenv("CMUX_FLEET_SECURESTORAGE_SEEDED", raising=False)
    monkeypatch.setattr(pv.subprocess, "run", lambda *a, **k: _Proc(0))
    assert pv.securestorage_seeded("~/.claude-seeded") is True     # item present -> seeded
    monkeypatch.setattr(pv.subprocess, "run",
                        lambda *a, **k: _Proc(44, "security: SecKeychainSearchCopyNext: ... could not be found"))
    assert pv.securestorage_seeded("~/.claude-never") is False     # errSecItemNotFound -> never seeded


def test_securestorage_seeded_fails_open(monkeypatch):
    # a broken/absent `security` (Linux/headless) must NEVER fabricate 'unseeded' and cry wolf on a real acct.
    monkeypatch.delenv("CMUX_FLEET_SECURESTORAGE_SEEDED", raising=False)
    monkeypatch.setattr(pv.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert pv.securestorage_seeded("~/.claude-x") is True          # OSError -> fail-open (seeded)
    monkeypatch.setattr(pv.subprocess, "run", lambda *a, **k: _Proc(1, "some other error"))
    assert pv.securestorage_seeded("~/.claude-x") is True          # unknown nonzero rc -> fail-open


def test_securestorage_seeded_env_seam(monkeypatch):
    monkeypatch.setenv("CMUX_FLEET_SECURESTORAGE_SEEDED", "assume")
    assert pv.securestorage_seeded("~/.claude-anything") is True   # forced seeded, no probe
    monkeypatch.setenv("CMUX_FLEET_SECURESTORAGE_SEEDED", "none")
    assert pv.securestorage_seeded("~/.claude-anything") is False  # forced unseeded, no probe


# --- launch guard: unseeded namespace WARNs loudly but still LAUNCHES (WARN, not ABORT) -----------
def _guard_body():
    return """
        [tool.claude]
        flags = "--effort high"
        [role.worker]
        kind = "child"
        place = "tab"
        cwd = "worker"
        [providers.claude]
        default = "acct"
        [providers.claude.acct]
        type = "subscription"
        auth = "securestorage:~/.claude-guard-e2e"
    """


def test_launch_unseeded_securestorage_warns_but_launches(cli_env, tmp_path):
    env = dict(cli_env, CMUX_FLEET_TOML=_toml(tmp_path, _guard_body()),
               CMUX_FLEET_SECURESTORAGE_SEEDED="none")            # force unseeded (hermetic; no real keychain)
    p = subprocess.run([sys.executable, "-m", "cmux_fleet", "launch", "worker", "--dry-run"],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr                            # WARN, NOT abort — the seeding bootstrap must launch
    assert "WARN" in p.stdout and "SILENTLY FALLS BACK" in p.stdout
    assert "/login" in p.stdout                                   # names the fix
    launch = next(l for l in p.stdout.splitlines() if "[fleet] launch:" in l)
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR=" in launch           # ...and the namespace is still on the line


def test_launch_seeded_securestorage_no_warn(cli_env, tmp_path):
    env = dict(cli_env, CMUX_FLEET_TOML=_toml(tmp_path, _guard_body()),
               CMUX_FLEET_SECURESTORAGE_SEEDED="assume")          # seeded -> clean, no guard noise
    p = subprocess.run([sys.executable, "-m", "cmux_fleet", "launch", "worker", "--dry-run"],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "SILENTLY FALLS BACK" not in p.stdout                  # seeded namespace: no guard warning


# --- recycle carries the guard too (every spawn path, never silent) -------------------------------
def test_recycle_unseeded_securestorage_warns(fs, monkeypatch, tmp_path):
    _providers_toml(tmp_path, monkeypatch, """
        [providers.claude]
        default = "acct"
        [providers.claude.acct]
        type = "subscription"
        auth = "securestorage:~/.claude-guard-recycle"
    """)
    monkeypatch.setattr(pv, "securestorage_seeded", lambda d: False)   # force unseeded (in-process)
    fleet_state.live_put("w", {"role": "secure-child", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": "claude:acct"})
    _stub_roster(monkeypatch, "secure-child", "claude")
    rc, out = _run(["recycle", "w", "--dry-run"])
    assert rc == 0
    assert "WARN" in out and "SILENTLY FALLS BACK" in out         # the guard fires on recycle too
    launch = next(l for l in out.splitlines() if "[fleet] launch:" in l)
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR=" in launch           # still injected (WARN, not abort)


def test_recycle_seeded_securestorage_no_warn(fs, monkeypatch, tmp_path):
    _providers_toml(tmp_path, monkeypatch, """
        [providers.claude]
        default = "acct"
        [providers.claude.acct]
        type = "subscription"
        auth = "securestorage:~/.claude-guard-recycle"
    """)
    monkeypatch.setattr(pv, "securestorage_seeded", lambda d: True)    # seeded
    fleet_state.live_put("w", {"role": "secure-child", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": "claude:acct"})
    _stub_roster(monkeypatch, "secure-child", "claude")
    rc, out = _run(["recycle", "w", "--dry-run"])
    assert rc == 0 and "SILENTLY FALLS BACK" not in out
