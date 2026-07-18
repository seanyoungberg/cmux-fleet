"""Fix 1 — account resolution joins the recycle/revive recompose (provider-architecture-review finding #1).

Before this fix, provider resolution lived ONLY in `cmd_launch`; every other spawn path (recycle, revive)
rebuilt the launch command from a captured binding / re-resolved roster loadout WITHOUT the account env, so a
recycled securestorage agent silently reverted to the ambient keychain credential (and a codex seat to
~/.codex). These drive the REAL dry-run compose and assert the account env is now present, announced, and —
when the config default moved out from under a running agent — loudly warned.

The account FOLLOWS CONFIG (Berg's ratified semantic), exactly like the loadout `_compose_from_roster`
already re-resolves. So the provider toml is monkeypatched onto `providers.FLEET_TOML`; the roster
load_config/resolve are stubbed (as the existing recycle tests do) so these stay pure unit tests with no cmux.
"""
import contextlib
import io
import os
import textwrap

import pytest

from cmux_fleet import cli as fleet           # noqa: E402
from cmux_fleet import providers as pv        # noqa: E402
from cmux_fleet import state as fleet_state   # noqa: E402


def _providers_toml(tmp_path, monkeypatch, body):
    p = tmp_path / "fleet.toml"
    p.write_text(textwrap.dedent(body))
    monkeypatch.setattr(pv, "FLEET_TOML", str(p))
    return str(p)


# A claude providers toml whose default is the SECURESTORAGE `berglabs` account (a correct launch MUST carry
# CLAUDE_SECURESTORAGE_CONFIG_DIR); `berg` is the other account, used to prove the moved-default warning.
_CLAUDE_BODY = """
    [providers.claude]
    default = "berglabs"
    [providers.claude.berg]
    type = "subscription"
    auth = "securestorage:~/.claude-seanyoungberg"
    [providers.claude.berglabs]
    type = "subscription"
    auth = "securestorage:~/.claude-berglabs"
"""


def _run(argv):
    """Drive a fleet verb capturing stdout; SystemExit is re-raised (callers that expect an abort catch it)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fleet.verb_table()[argv[0]]([*argv[1:]])
    return rc, buf.getvalue()


def _stub_roster(monkeypatch, role, tool, cwd="/x", flags=None):
    """Make `role` a roster role that resolves to a minimal `tool` spec (no real toml roster read)."""
    monkeypatch.setattr(fleet, "_is_roster", lambda r: r == role)
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {})
    monkeypatch.setattr(fleet, "load_config",
                        lambda: {"role": {role: {}}, "tool": {tool: {}}})
    monkeypatch.setattr(fleet, "resolve", lambda *a: {
        "tool": tool, "role": role, "label": role, "kind": "child", "place": "tab", "group": "",
        "cwd": cwd, "plugins": [], "flags": list(flags or []), "env": {}, "settings": "",
        "setting_sources": ""})


# --- ROSTER claude: securestorage injected + moved-default warning --------------------------------
def test_recycle_roster_claude_injects_securestorage_and_warns_on_move(fs, monkeypatch, tmp_path):
    _providers_toml(tmp_path, monkeypatch, _CLAUDE_BODY)
    # recorded under the OLD account (berg); the toml default is now berglabs -> a correct recycle both
    # injects berglabs' namespace AND announces the move loudly.
    fleet_state.live_put("w", {"role": "secure-child", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": "claude:berg"})
    _stub_roster(monkeypatch, "secure-child", "claude")
    rc, out = _run(["recycle", "w", "--dry-run"])
    assert rc == 0
    launch = next(l for l in out.splitlines() if "[fleet] launch:" in l)
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR=" in launch          # the account env is on the launch line now
    assert os.path.expanduser("~/.claude-berglabs") in launch    # ...and it is the RE-RESOLVED account
    assert "CLAUDE_CONFIG_DIR" not in launch                     # securestorage does NOT swap the config dir
    assert "[fleet] provider: claude:berglabs" in out            # announced, like launch
    assert "WARN" in out and "account MOVED" in out and "claude:berg'" in out   # loud move warning


def test_recycle_roster_claude_no_warn_when_account_unchanged(fs, monkeypatch, tmp_path):
    # recorded account == the resolved default -> inject + announce, but NO change warning.
    _providers_toml(tmp_path, monkeypatch, _CLAUDE_BODY)
    fleet_state.live_put("w", {"role": "secure-child", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": "claude:berglabs"})
    _stub_roster(monkeypatch, "secure-child", "claude")
    rc, out = _run(["recycle", "w", "--dry-run"])
    assert rc == 0 and "[fleet] provider: claude:berglabs" in out
    assert "account MOVED" not in out


# --- AD-HOC / off-roster: follow config = the tool default ----------------------------------------
def test_recycle_adhoc_follows_config_tool_default(fs, monkeypatch, tmp_path):
    # an off-roster label has no role to resolve -> the registry-fallback compose; it must STILL inject the
    # tool default (the captured binding dropped the env, so re-resolving the default IS the fix).
    _providers_toml(tmp_path, monkeypatch, _CLAUDE_BODY)
    fleet_state.live_put("w", {"role": "adhoc-worker", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": ""})
    monkeypatch.setattr(fleet, "_is_roster", lambda r: False)    # off-roster -> reproduce, not toml-resolve
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {})   # no cmux binding -> registry fallback
    monkeypatch.setattr(fleet, "load_config", lambda: {})
    rc, out = _run(["recycle", "w", "--dry-run"])
    assert rc == 0
    launch = next(l for l in out.splitlines() if "[fleet] launch:" in l)
    assert os.path.expanduser("~/.claude-berglabs") in launch    # the tool default, injected
    assert "[fleet] provider: claude:berglabs" in out


# --- AD-HOC binding-replay path (a real cmux binding exists) also injects the account --------------
def test_recycle_adhoc_binding_replay_injects_account(fs, monkeypatch, tmp_path):
    # the OTHER off-roster path: cmux HAS a captured binding, so the recycle replays its flags (never the
    # registry fallback). The binding captured NULL env (its own docstring), so the account must be
    # re-injected here too — this is the exact code path a live ad-hoc worker recycles through.
    _providers_toml(tmp_path, monkeypatch, _CLAUDE_BODY)
    fleet_state.live_put("w", {"role": "adhoc-worker", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": ""})
    monkeypatch.setattr(fleet, "_is_roster", lambda r: False)
    monkeypatch.setattr(fleet, "load_config", lambda: {})
    monkeypatch.setattr(fleet, "_resume_binding",
                        lambda surf: {"command": "cd /x && claude --model claude-opus-4-8", "checkpoint_id": ""})
    rc, out = _run(["recycle", "w", "--dry-run"])
    assert rc == 0
    launch = next(l for l in out.splitlines() if "[fleet] launch:" in l)
    assert os.path.expanduser("~/.claude-berglabs") in launch   # account injected on the binding-replay path
    assert "--model claude-opus-4-8" in launch                  # ...and the binding's own flags preserved
    assert "[fleet] provider: claude:berglabs" in out


# --- ROSTER codex: the per-seat CODEX_HOME is injected --------------------------------------------
def test_recycle_roster_codex_injects_codex_home(fs, monkeypatch, tmp_path):
    home = tmp_path / "codex-berglabs"
    home.mkdir()
    (home / "auth.json").write_text('{"tokens": {}}\n')          # resolve_launch refuses a codex home w/o it
    _providers_toml(tmp_path, monkeypatch, f"""
        [providers.codex]
        default = "berglabs"
        [providers.codex.berglabs]
        type = "subscription"
        auth = "codex-home:{home}"
    """)
    fleet_state.live_put("c", {"role": "codex-child", "tool": "codex", "surface": "S", "cwd": "/x",
                               "session": "OLD", "kind": "child", "provider": "codex:berglabs"})
    _stub_roster(monkeypatch, "codex-child", "codex")
    rc, out = _run(["recycle", "c", "--dry-run"])
    assert rc == 0
    launch = next(l for l in out.splitlines() if "[fleet] launch:" in l)
    assert f"CODEX_HOME={home}" in launch                        # the seat's own home is on the launch line
    assert "[fleet] provider: codex:berglabs" in out


# --- opt-in preserved: no [providers] table -> byte-identical to before (zero injection) ----------
def test_recycle_no_providers_configured_is_byte_identical(fs, monkeypatch, tmp_path):
    _providers_toml(tmp_path, monkeypatch, "[fleet]\n")          # a valid toml with NO [providers] table
    fleet_state.live_put("w", {"role": "secure-child", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": ""})
    _stub_roster(monkeypatch, "secure-child", "claude")
    rc, out = _run(["recycle", "w", "--dry-run"])
    assert rc == 0
    launch = next(l for l in out.splitlines() if "[fleet] launch:" in l)
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in launch       # single-account opt-in: nothing injected
    assert "[fleet] provider:" not in out                        # ...and nothing announced


# --- abort, never fall back to ambient, when a NAMED account can't resolve ------------------------
def test_recycle_aborts_when_named_account_unresolvable(fs, monkeypatch, tmp_path):
    _providers_toml(tmp_path, monkeypatch, """
        [providers.claude]
        default = "ghost"
    """)   # a default naming an account with no [providers.claude.ghost] block
    fleet_state.live_put("w", {"role": "secure-child", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": "claude:berg"})
    _stub_roster(monkeypatch, "secure-child", "claude")
    with pytest.raises(SystemExit) as ei:
        _run(["recycle", "w", "--dry-run"])
    assert "ABORT" in str(ei.value) and "ghost" in str(ei.value)   # refuses; never a silent ambient fall-back


def test_recycle_aborts_loudly_on_malformed_toml(fs, monkeypatch, tmp_path):
    # fix 2 at a recycle call site: a broken toml raises through _resolve_recycle_provider, and recycle
    # sys.exits naming the parse failure (never a silent revert to the ambient credential). This path is
    # NOT covered by load_config's own guard — _is_roster swallows that SystemExit — so the provider-layer
    # refusal is what makes the recycle abort.
    _providers_toml(tmp_path, monkeypatch, '[providers.claude]\ndefault = "berg  # unterminated\n')
    fleet_state.live_put("w", {"role": "secure-child", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": "claude:berg"})
    monkeypatch.setattr(fleet, "_resume_binding", lambda surf: {})
    with pytest.raises(SystemExit) as ei:
        _run(["recycle", "w", "--dry-run"])
    assert "ABORT" in str(ei.value) and "unparseable" in str(ei.value)


# --- the detached exec re-binds the registry's `provider` field to the resolved account -----------
def test_recycle_exec_rebinds_resolved_provider_field(fs, rs, monkeypatch):
    # after a recycle moves the account, the registry must record the account the agent NOW runs under (so
    # `fleet usage` attributes it correctly and the next recycle doesn't re-warn). Drive a clean fresh bind.
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": "claude:berg"})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "cmuxq", lambda *a, **k: "OK" if a and a[0] == "respawn-pane" else "")
    monkeypatch.setattr(fleet, "_quiet_gate", lambda *a, **k: True)
    monkeypatch.setattr(rs, "lifecycle", lambda surf: "ended")
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: False)
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")
    payload = {"label": "w", "surface": "S", "send_cmd": "cd /x && claude", "mode": "fresh",
               "tool": "claude", "force": True, "prime": None, "old_session": "OLD", "cwd": "/x",
               "provider": "claude:berglabs", "provider_needs_refresh": ""}
    assert fleet._recycle_exec_one(payload) == 0
    assert fleet_state.live_get("w")["provider"] == "claude:berglabs"   # re-bound to the resolved account


def test_recycle_exec_leaves_provider_empty_when_none_configured(fs, rs, monkeypatch):
    # opt-in: a no-providers recycle carries provider="" -> the re-bind records "" (unchanged), never errors.
    fleet_state.live_put("w", {"role": "w", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": ""})
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "cmuxq", lambda *a, **k: "OK" if a and a[0] == "respawn-pane" else "")
    monkeypatch.setattr(fleet, "_quiet_gate", lambda *a, **k: True)
    monkeypatch.setattr(rs, "lifecycle", lambda surf: "ended")
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: False)
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    monkeypatch.setattr(fleet, "_poll_session_back", lambda *a, **k: "NEWSID")
    payload = {"label": "w", "surface": "S", "send_cmd": "cd /x && claude", "mode": "fresh",
               "tool": "claude", "force": True, "prime": None, "old_session": "OLD", "cwd": "/x",
               "provider": "", "provider_needs_refresh": ""}
    assert fleet._recycle_exec_one(payload) == 0
    assert fleet_state.live_get("w")["provider"] == ""


# --- account DROPPED: a recorded account with no [providers] now reverts to ambient, LOUDLY -------
def test_recycle_warns_when_account_dropped_from_config(fs, monkeypatch, tmp_path):
    # the removal case of "follow config": the operator deleted [providers], so a recorded agent reverts to
    # the AMBIENT credential. That is a real account move and must not be silent (finding 5).
    _providers_toml(tmp_path, monkeypatch, "[fleet]\n")          # no [providers] table anymore
    fleet_state.live_put("w", {"role": "secure-child", "tool": "claude", "surface": "S", "cwd": "/x",
                               "session": "claude-OLD", "kind": "child", "provider": "claude:berg"})
    _stub_roster(monkeypatch, "secure-child", "claude")
    rc, out = _run(["recycle", "w", "--dry-run"])
    assert rc == 0
    assert "account DROPPED" in out and "claude:berg" in out and "AMBIENT" in out
    launch = next(l for l in out.splitlines() if "[fleet] launch:" in l)
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in launch       # reverts to ambient: nothing injected


# --- REVIVE follows config too (finding #1 names recycle AND revive) ------------------------------
def test_revive_roster_claude_injects_securestorage_and_warns(fs, monkeypatch, tmp_path):
    _providers_toml(tmp_path, monkeypatch, _CLAUDE_BODY)
    _stub_roster(monkeypatch, "secure-child", "claude")
    # archive via the REAL path (_build_archive_entry), NOT a hand-written row: this is what proves the
    # `provider` field survives archival so the move-warn can fire for a real archived agent.
    live = {"role": "secure-child", "tool": "claude", "surface": "S", "cwd": "/x", "session": "claude-OLD",
            "kind": "child", "place": "tab", "provider": "claude:berg"}
    fleet_state.live_put("w", live)
    arch = fleet._build_archive_entry(live, {})
    assert arch.get("provider") == "claude:berg"                 # the field survives archival (finding 1)
    fleet_state.archive_put("w", arch)
    rc, out = _run(["revive", "w", "--dry-run"])
    assert rc == 0
    launch = next(l for l in out.splitlines() if "[fleet] launch:" in l)
    assert os.path.expanduser("~/.claude-berglabs") in launch    # the re-resolved account env, on revive too
    assert "[fleet] provider: claude:berglabs" in out
    assert "account MOVED" in out and "claude:berg'" in out       # the warn now fires through the REAL path
