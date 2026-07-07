"""Layer 2 — the providers feature: config parse, per-launch auth resolution (incl. secret-safe token
injection), the codex read poller, poll_all, the raw_env render, and the `fleet usage` view.

resolve/parse read `providers.FLEET_TOML` (imported from config at module load); tests monkeypatch that
module attribute to a throwaway toml. The codex poller is fully file-based (no network), so it and
poll_all are exercised end to end; the claude HTTP poller is only smoke-tested for its no-token branch.
"""
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from conftest import REPO

sys.path.insert(0, REPO)
from cmux_fleet import providers as pv  # noqa: E402
from cmux_fleet import features as ff  # noqa: E402
from cmux_fleet import state as fs  # noqa: E402
from cmux_fleet import cli  # noqa: E402


def _toml(tmp_path, body):
    p = tmp_path / "fleet.toml"
    p.write_text(textwrap.dedent(body))
    return str(p)


@pytest.fixture
def providers_toml(tmp_path, monkeypatch):
    tokfile = tmp_path / "throwaway.token"
    tokfile.write_text("sk-ant-oat01-SENTINELTOKEN-do-not-print\n")
    body = f"""
        [providers.claude]
        default = "berg-max"
        [providers.claude.berg-max]
        type = "subscription"
        auth = "keychain:Claude Code-credentials"
        track = "windows"
        [providers.claude.throwaway]
        type = "subscription"
        auth = "file:{tokfile}"
        track = "windows"
        [providers.claude.vtx]
        type = "vertex"
        auth = "env-file:{tmp_path / 'vtx.env'}"
        track = "none"

        [providers.codex]
        default = "berg-team"
        [providers.codex.berg-team]
        type = "subscription"
        auth = "codex-home:{os.path.expanduser('~/.codex')}"
        track = "windows"
        [providers.codex.acct2]
        type = "subscription"
        auth = "codex-home:{tmp_path / 'codex-acct2'}"
        track = "windows"
    """
    (tmp_path / "vtx.env").write_text("CLAUDE_CODE_USE_VERTEX=1\nANTHROPIC_VERTEX_PROJECT_ID=proj-x\n# c\n")
    path = _toml(tmp_path, body)
    monkeypatch.setattr(pv, "FLEET_TOML", path)
    return {"path": path, "token": str(tokfile), "tmp": tmp_path}


# --- config parse ---------------------------------------------------------------------------------
def test_providers_doc_parse(providers_toml):
    doc = pv._providers_doc()
    assert set(doc) == {"claude", "codex"}
    assert doc["claude"]["default"] == "berg-max"
    assert set(doc["claude"]["providers"]) == {"berg-max", "throwaway", "vtx"}
    assert doc["claude"]["providers"]["throwaway"]["type"] == "subscription"
    assert pv.default_provider("codex") == "berg-team"


def test_absent_providers_block_is_inert(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "FLEET_TOML", str(tmp_path / "nope.toml"))
    assert pv._providers_doc() == {}
    assert pv.default_provider("claude") == ""


# --- launch-time auth resolution ------------------------------------------------------------------
def test_resolve_default_claude_keychain_is_noop(providers_toml):
    r = pv.resolve_launch("claude", "berg-max")
    assert r["label"] == "claude:berg-max"
    assert r["env"] == {} and r["raw_env"] == {} and not r["provisional"]


def test_resolve_file_token_injects_via_spawn_read(providers_toml):
    r = pv.resolve_launch("claude", "throwaway")
    raw = r["raw_env"]["CLAUDE_CODE_OAUTH_TOKEN"]
    # the value is a $(cat 'path') so the secret is read at spawn, never materialized in the command
    assert raw.startswith('"$(cat ') and providers_toml["token"] in raw
    assert "SENTINELTOKEN" not in raw            # the token VALUE is not embedded


def test_resolve_missing_token_file_errors(providers_toml, monkeypatch):
    os.remove(providers_toml["token"])
    with pytest.raises(pv.ProviderError):
        pv.resolve_launch("claude", "throwaway")


def test_resolve_codex_default_noop_nondefault_provisional(providers_toml):
    assert pv.resolve_launch("codex", "berg-team")["env"] == {}       # ~/.codex = current home
    r = pv.resolve_launch("codex", "acct2")
    assert r["provisional"] is True and "CODEX_HOME" in r["env"]


def test_resolve_vertex_reads_env_file(providers_toml):
    r = pv.resolve_launch("claude", "vtx")
    assert r["env"]["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert r["env"]["ANTHROPIC_VERTEX_PROJECT_ID"] == "proj-x"


def test_resolve_unknown_provider_errors(providers_toml):
    with pytest.raises(pv.ProviderError):
        pv.resolve_launch("claude", "ghost")


# --- codex read poller (file-based, no network) ---------------------------------------------------
def _write_rollout(home, resets_p, resets_s):
    d = os.path.join(home, "sessions", "2026", "07", "07")
    os.makedirs(d, exist_ok=True)
    ev = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "limit_id": "codex",
        "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": resets_p},
        "secondary": {"used_percent": 44.0, "window_minutes": 10080, "resets_at": resets_s},
        "plan_type": "team"}}}
    with open(os.path.join(d, "rollout-2026-07-07T20-00-00-abc.jsonl"), "w") as f:
        f.write(json.dumps({"type": "session_meta"}) + "\n")
        f.write(json.dumps(ev) + "\n")


def test_poll_codex_extracts_windows(tmp_path):
    home = str(tmp_path / "codex-home")
    now = int(time.time())
    _write_rollout(home, now + 3600, now + 7 * 86400)
    r = pv.poll_codex(home)
    assert r["ok"] and r["windows"]["five_hour"]["pct"] == 12.0
    assert r["windows"]["seven_day"]["pct"] == 44.0
    assert r["windows"]["five_hour"]["resets_at"] == now + 3600
    assert r["stale"] is False


def test_poll_codex_no_sessions(tmp_path):
    r = pv.poll_codex(str(tmp_path / "empty"))
    assert not r["ok"] and "no rollout" in r["error"]


def test_poll_all_writes_snapshot(providers_toml, monkeypatch):
    # point codex acct2's home at a temp home with a rollout; claude poll will fail (no token) -> ok:false,
    # but the record must still be present. poll_all writes provider-usage.json in the throwaway STATE.
    home = str(providers_toml["tmp"] / "codex-acct2")
    now = int(time.time())
    _write_rollout(home, now + 100, now + 100)
    # make the claude keychain read deterministically fail (no real security in the test env is fine, but
    # force it) so the test doesn't depend on the host keychain.
    monkeypatch.setattr(pv, "_read_oauth_token", lambda auth: None)
    snap = pv.poll_all()
    assert "codex:acct2" in snap and snap["codex:acct2"]["ok"]
    assert snap["codex:acct2"]["windows"]["five_hour"]["pct"] == 12.0
    assert "claude:berg-max" in snap and snap["claude:berg-max"]["ok"] is False
    # persisted + readable via state
    assert fs.provider_usage_read()["codex:acct2"]["name"] == "acct2"
    for rec in snap.values():
        assert "checked_at" in rec


# --- render_send_cmd raw_env (secret stays out of the command string) -----------------------------
def test_render_send_cmd_raw_env_unquoted():
    cmd = cli.render_send_cmd("claude", ["--foo"], {"A": "b c"}, "/tmp/x",
                              raw_env={"TOK": '"$(cat \'/p/t\')"'})
    assert "A='b c'" in cmd                                   # normal env is shlex-quoted
    assert 'TOK="$(cat \'/p/t\')"' in cmd                     # raw env is verbatim (evaluates at spawn)


def test_render_send_cmd_no_raw_env_is_unchanged():
    a = cli.render_send_cmd("claude", ["--m", "x"], {"K": "v"}, "/tmp/x")
    b = cli.render_send_cmd("claude", ["--m", "x"], {"K": "v"}, "/tmp/x", raw_env={})
    assert a == b


# --- the `fleet usage` view -----------------------------------------------------------------------
def test_cmd_usage_smoke(capsys):
    now = int(time.time())
    fs.provider_usage_write({
        "claude:berg-max": {"tool": "claude", "name": "berg-max", "type": "subscription",
                            "is_default": True, "ok": True, "checked_at": now, "active_limit": "weekly_all",
                            "windows": {"five_hour": {"pct": 2.0, "resets_at": now + 3600},
                                        "seven_day": {"pct": 10.0, "resets_at": now + 86400}},
                            "scoped": [{"label": "Fable", "pct": 6, "resets_at": now + 86400}],
                            "extra_usage": {"enabled": False, "pct": None}},
        "codex:berg-team": {"tool": "codex", "name": "berg-team", "type": "subscription", "ok": False,
                            "error": "no rollout sessions found", "checked_at": now},
    })
    assert ff.cmd_usage([]) == 0
    out = capsys.readouterr().out
    assert "claude:berg-max" in out and "*default" in out
    assert "5h" in out and "7day" in out and "Fable" in out
    assert "not readable" in out                             # the errored codex row
    # secrets never appear in the view (it only ever reads %/resets)
    assert "sk-ant" not in out


def test_cmd_usage_json(capsys):
    fs.provider_usage_write({"claude:berg-max": {"tool": "claude", "ok": True, "windows": {}}})
    assert ff.cmd_usage(["--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "providers" in doc and "attribution" in doc


def test_cmd_usage_empty(capsys):
    assert ff.cmd_usage([]) == 0
    assert "no usage snapshot" in capsys.readouterr().out


# --- end-to-end: launch --provider injects the token WITHOUT printing it --------------------------
def test_launch_provider_dry_run_hides_token(cli_env, tmp_path, providers_toml):
    # a roster with a worker role + the providers block; dry-run resolves + prints the send-cmd, then stops.
    tokfile = tmp_path / "e2e.token"
    tokfile.write_text("sk-ant-oat01-E2ESECRET\n")
    toml = _toml(tmp_path, f"""
        [tool.claude]
        flags = "--effort high"
        [role.worker]
        kind = "child"
        place = "tab"
        cwd = "worker"
        [providers.claude]
        default = "berg-max"
        [providers.claude.berg-max]
        type = "subscription"
        auth = "keychain:Claude Code-credentials"
        [providers.claude.throwaway]
        type = "subscription"
        auth = "file:{tokfile}"
    """)
    env = dict(cli_env)
    env["CMUX_FLEET_TOML"] = toml
    p = subprocess.run([sys.executable, "-m", "cmux_fleet", "launch", "worker",
                        "--provider", "throwaway", "--dry-run"],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "provider: claude:throwaway" in p.stdout
    assert str(tokfile) in p.stdout                          # the PATH is shown
    assert "E2ESECRET" not in p.stdout                       # the token VALUE is not
    assert "$(cat " in p.stdout                              # injected as a spawn-time read
