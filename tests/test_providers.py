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


def test_resolve_codex_home_default_noop(providers_toml):
    r = pv.resolve_launch("codex", "berg-team")                       # codex-home:~/.codex = current home
    assert r["env"] == {} and r["raw_env"] == {} and r["args"] == []


def test_resolve_codex_home_without_login_errors(providers_toml):
    # fixture's acct2 = codex-home at a dir with no auth.json -> refuse loudly (never silent-fallback to ~/.codex)
    with pytest.raises(pv.ProviderError, match="codex login"):
        pv.resolve_launch("codex", "acct2")


def test_resolve_codex_home_with_login_sets_home(providers_toml):
    home = providers_toml["tmp"] / "codex-acct2"
    home.mkdir(exist_ok=True)
    (home / "auth.json").write_text('{"auth_mode":"chatgpt"}')
    r = pv.resolve_launch("codex", "acct2")
    assert r["env"]["CODEX_HOME"] == str(home) and not r["provisional"] and r["args"] == []


def test_resolve_codex_token_env_path(tmp_path, monkeypatch):
    tok = tmp_path / "codex-acctx.token"
    tok.write_text("ya29-fake-chatgpt-oauth-token\n")
    toml = _toml(tmp_path, f"""
        [providers.codex]
        default = "acctx"
        [providers.codex.acctx]
        type = "subscription"
        auth = "codex-token:{tok}"
    """)
    monkeypatch.setattr(pv, "FLEET_TOML", toml)
    r = pv.resolve_launch("codex", "acctx")
    # per-launch account selection via -c model_provider, token read at spawn (secret not embedded)
    assert r["args"] == ["-c", "model_provider=acctx"]
    raw = r["raw_env"][pv.CODEX_TOKEN_ENV]
    assert raw.startswith('"$(cat ') and str(tok) in raw and "ya29" not in raw
    assert not r["provisional"]


def test_resolve_codex_token_missing_file_errors(tmp_path, monkeypatch):
    toml = _toml(tmp_path, """
        [providers.codex]
        default = "acctx"
        [providers.codex.acctx]
        type = "subscription"
        auth = "codex-token:/nonexistent/x.token"
    """)
    monkeypatch.setattr(pv, "FLEET_TOML", toml)
    with pytest.raises(pv.ProviderError, match="token file not found"):
        pv.resolve_launch("codex", "acctx")


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


def _write_rollout_raw(home, rate_limits):
    d = os.path.join(home, "sessions", "2026", "07", "10")
    os.makedirs(d, exist_ok=True)
    ev = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": rate_limits}}
    with open(os.path.join(d, "rollout-2026-07-10T09-00-00-zzz.jsonl"), "w") as f:
        f.write(json.dumps(ev) + "\n")


def test_poll_codex_free_plan_single_30day_window(tmp_path):
    """Regression: a Free plan sends primary=43200min (30 DAYS) and secondary=null. Keying windows off the
    primary/secondary SLOT mislabelled that 30-day window as the 5h window (observed live 2026-07-10)."""
    home = str(tmp_path / "free-home")
    now = int(time.time())
    _write_rollout_raw(home, {"limit_id": "codex", "plan_type": None,
                              "primary": {"used_percent": 18.0, "window_minutes": 43200,
                                          "resets_at": now + 20 * 86400},
                              "secondary": None})
    r = pv.poll_codex(home)
    assert r["ok"]
    assert "thirty_day" in r["windows"] and r["windows"]["thirty_day"]["pct"] == 18.0
    assert "five_hour" not in r["windows"]        # the bug: 30d was reported as 5h
    assert "seven_day" not in r["windows"]        # null secondary must not appear


def test_poll_codex_labels_by_window_length_not_slot(tmp_path):
    """Even if the plan swaps the slots, the label follows window_minutes."""
    home = str(tmp_path / "swapped")
    now = int(time.time())
    _write_rollout_raw(home, {"primary": {"used_percent": 5.0, "window_minutes": 10080, "resets_at": now + 1},
                              "secondary": {"used_percent": 9.0, "window_minutes": 300, "resets_at": now + 2}})
    w = pv.poll_codex(home)["windows"]
    assert w["seven_day"]["pct"] == 5.0 and w["five_hour"]["pct"] == 9.0


def test_window_label_mapping():
    assert pv._window_label(300) == "five_hour"
    assert pv._window_label(10080) == "seven_day"
    assert pv._window_label(43200) == "thirty_day"
    assert pv._window_label(120) == "2hour"
    assert pv._window_label(2880) == "2day"
    assert pv._window_label(None) is None


# --- codex server-side usage API (the productized path) ------------------------------------------
# A real /backend-api/codex/usage 200 body (captured live 2026-07-10). Windows are in SECONDS here.
_CODEX_USAGE_BODY = {
    "user_id": "user-KUwx", "account_id": "77cd2846-abcd", "email": "sean@berglabs.net",
    "plan_type": "team",
    "rate_limit": {
        "allowed": True, "limit_reached": False, "rate_limit_reached_type": None,
        "primary_window": {"used_percent": 2, "limit_window_seconds": 18000,
                           "reset_after_seconds": 15783, "reset_at": 1783748355},
        "secondary_window": {"used_percent": 0, "limit_window_seconds": 604800,
                             "reset_after_seconds": 602583, "reset_at": 1784335155},
    },
    "credits": {"has_credits": False, "unlimited": False, "overage_limit_reached": False, "balance": None},
    "spend_control": {"reached": False, "individual_limit": None},
}


def test_normalize_codex_usage_maps_windows_by_length():
    r = pv._normalize_codex_usage(_CODEX_USAGE_BODY)
    assert r["ok"] and r["error"] is None
    # 18000s -> 300min -> five_hour (Berg's "maybe 4h" is 5h: server-authoritative); 604800s -> 7d
    assert r["windows"]["five_hour"]["pct"] == 2 and r["windows"]["five_hour"]["window_minutes"] == 300
    assert r["windows"]["five_hour"]["resets_at"] == 1783748355
    assert r["windows"]["seven_day"]["window_minutes"] == 10080
    assert r["identity"]["email"] == "sean@berglabs.net" and r["plan"] == "team"
    assert r["account_id"] == "77cd2846-abcd"                     # subscription grouping key
    assert r["limit_reached"] is False and r["credits"]["has_credits"] is False
    assert r["spend_control"]["reached"] is False


def test_normalize_codex_usage_captures_hard_cap_signals():
    body = json.loads(json.dumps(_CODEX_USAGE_BODY))
    body["rate_limit"]["limit_reached"] = True
    body["rate_limit"]["rate_limit_reached_type"] = "WorkspaceOwnerUsageLimitReached"
    body["credits"]["overage_limit_reached"] = True
    body["spend_control"]["reached"] = True
    r = pv._normalize_codex_usage(body)
    assert r["limit_reached"] is True
    assert r["rate_limit_reached_type"] == "WorkspaceOwnerUsageLimitReached"
    assert r["credits"]["overage_limit_reached"] is True and r["spend_control"]["reached"] is True


def test_normalize_codex_usage_single_window_ok():
    body = json.loads(json.dumps(_CODEX_USAGE_BODY))
    body["rate_limit"]["secondary_window"] = None            # a plan may send only one window
    r = pv._normalize_codex_usage(body)
    assert "five_hour" in r["windows"] and "seven_day" not in r["windows"]


class _FakeResp:
    def __init__(self, body): self._b = json.dumps(body).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_poll_codex_api_success(monkeypatch):
    import urllib.request
    captured = {}
    def fake_urlopen(req, timeout=None):
        captured["ua"] = req.headers.get("User-agent")
        captured["auth"] = req.headers.get("Authorization")
        captured["acct"] = req.headers.get("Chatgpt-account-id")
        return _FakeResp(_CODEX_USAGE_BODY)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    r = pv.poll_codex_api("tok-abc", account_id="77cd2846-abcd")
    assert r["ok"] and r["windows"]["five_hour"]["window_minutes"] == 300
    assert captured["auth"] == "Bearer tok-abc"
    assert captured["acct"] == "77cd2846-abcd"
    assert captured["ua"]                               # non-empty UA is required (empty -> 403 at the edge)


def test_poll_codex_api_401_is_soft_error(monkeypatch):
    import urllib.request, urllib.error
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(pv.CODEX_USAGE_ENDPOINT, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    r = pv.poll_codex_api("stale-tok", account_id="x")
    assert r["ok"] is False and r["error"] == "HTTP 401"     # caller falls back to the rollout scrape


def test_poll_codex_api_no_token():
    assert pv.poll_codex_api("")["ok"] is False


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


# --- the paint accessor: the stable contract with sidebar-build ----------------------------------
def test_usage_for_paint_shape_and_ordering():
    now = int(time.time())
    fs.provider_usage_write({
        "claude:berg-max": {
            "tool": "claude", "name": "berg-max", "type": "subscription", "plan": "max",
            "is_default": True, "ok": True, "error": None, "stale": False, "checked_at": now - 30,
            "active_limit": "session",
            "windows": {
                "seven_day": {"pct": 14.0, "resets_at": now + 86400, "window_minutes": 10080},
                "five_hour": {"pct": 19.0, "resets_at": now + 3000, "window_minutes": 300},
            },
            "scoped": [{"label": "Fable", "pct": 8.0, "resets_at": now + 86400}],
        },
        "codex:free": {
            "tool": "codex", "name": "free", "type": "subscription", "plan": None,
            "is_default": False, "ok": True, "stale": True, "checked_at": now,
            "windows": {"thirty_day": {"pct": 17.0, "resets_at": now + 20 * 86400, "window_minutes": 43200}},
            "scoped": [], "active_limit": "",
        },
    })
    view = pv.usage_for_paint()
    assert view["schema"] == pv.PAINT_SCHEMA and "generated_at" in view
    by_id = {p["id"]: p for p in view["providers"]}

    c = by_id["claude:berg-max"]
    assert c["kind"] == "subscription" and c["plan"] == "max" and c["is_default"] is True
    assert c["age_s"] >= 30
    # windows ordered shortest-first, then scoped last
    assert [w["label"] for w in c["windows"]] == ["5h", "7d", "Fable"]
    assert c["windows"][0]["binding"] is True                 # 5h is the active/binding limit
    assert c["windows"][-1]["scoped"] is True
    assert c["headline"]["label"] == "5h" and c["headline"]["pct"] == 19.0   # most-constrained
    assert all(w["resets_in_s"] >= 0 for w in c["windows"])

    z = by_id["codex:free"]
    assert [w["label"] for w in z["windows"]] == ["30d"]       # single-window plan renders one row
    assert z["stale"] is True and z["headline"]["label"] == "30d"


def test_usage_for_paint_surfaces_real_identity():
    now = int(time.time())
    fs.provider_usage_write({
        "claude:berg-max": {"tool": "claude", "name": "berg-max", "type": "subscription", "ok": True,
                            "checked_at": now, "windows": {}, "scoped": [],
                            "identity": {"email": "seanyoungberg@gmail.com", "display": "Berg"}},
        "codex:acct2": {"tool": "codex", "name": "acct2", "type": "subscription", "ok": True,
                        "checked_at": now, "windows": {}, "scoped": [],
                        "identity": {"email": "other@example.com", "display": None}},
        "claude:noident": {"tool": "claude", "name": "noident", "type": "subscription", "ok": True,
                           "checked_at": now, "windows": {}, "scoped": []},
    })
    p = {x["id"]: x for x in pv.usage_for_paint()["providers"]}
    # `account` stays the config id (stable key); `label`/`identity` carry the REAL account for display
    assert p["claude:berg-max"]["account"] == "berg-max"
    assert p["claude:berg-max"]["label"] == "Berg"                          # display preferred
    assert p["claude:berg-max"]["identity"]["email"] == "seanyoungberg@gmail.com"
    assert p["codex:acct2"]["label"] == "other@example.com"                 # email when no display
    assert p["claude:noident"]["label"] == "noident"                        # falls back to config id


def test_usage_for_paint_badge_and_subscription_grouping():
    now = int(time.time())
    # two codex SEATS on ONE subscription (shared account_id) + one on another + a claude account
    fs.provider_usage_write({
        "codex:sean-flat": {"tool": "codex", "name": "sean-flat", "type": "subscription", "ok": True,
                            "checked_at": now, "windows": {}, "scoped": [], "account_id": "acct-77"},
        "codex:berglabs": {"tool": "codex", "name": "berglabs", "type": "subscription", "ok": True,
                           "checked_at": now, "windows": {}, "scoped": [], "account_id": "acct-77"},
        "codex:sean-dot": {"tool": "codex", "name": "sean-dot", "type": "subscription", "ok": True,
                           "checked_at": now, "windows": {}, "scoped": [], "account_id": "acct-20"},
        "claude:berg-max": {"tool": "claude", "name": "berg-max", "type": "subscription", "ok": True,
                            "checked_at": now, "windows": {}, "scoped": []},
    })
    p = {x["id"]: x for x in pv.usage_for_paint()["providers"]}
    # badge per tool
    assert p["codex:sean-flat"]["badge"] == "Codex" and p["claude:berg-max"]["badge"] == "Claude Code"
    # two seats sharing account_id group under one subscription; a third seat is its own group
    assert p["codex:sean-flat"]["subscription"] == p["codex:berglabs"]["subscription"] == "acct-77"
    assert p["codex:sean-dot"]["subscription"] == "acct-20"
    # claude (no account_id) falls back to its own tool:account singleton group
    assert p["claude:berg-max"]["subscription"] == "claude:berg-max"
    # the short seat handle stays on `account` (disambiguates the dotted/flat gmails)
    assert p["codex:sean-flat"]["account"] == "sean-flat"


def test_usage_for_paint_surfaces_limit_reached():
    now = int(time.time())
    fs.provider_usage_write({
        "codex:capped": {"tool": "codex", "name": "capped", "type": "subscription", "ok": True,
                         "checked_at": now, "windows": {}, "scoped": [], "limit_reached": True},
        "codex:ok": {"tool": "codex", "name": "ok", "type": "subscription", "ok": True,
                     "checked_at": now, "windows": {}, "scoped": []},
    })
    p = {x["id"]: x for x in pv.usage_for_paint()["providers"]}
    assert p["codex:capped"]["limit_reached"] is True and p["codex:ok"]["limit_reached"] is False


def test_codex_identity_decodes_id_token(tmp_path):
    import base64
    def jwt(claims):
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"aaa.{body}.bbb"
    home = tmp_path / "h"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": jwt({
        "email": "sean.youngberg@gmail.com",
        "https://api.openai.com/auth": {"chatgpt_plan_type": "team"}})}}))
    idn = pv._codex_identity(str(home))
    assert idn["email"] == "sean.youngberg@gmail.com" and idn["plan"] == "team"
    assert pv._codex_identity(str(tmp_path / "missing")) == {}              # no auth.json -> {}


def test_usage_for_paint_empty_and_errored():
    assert pv.usage_for_paint()["providers"] == []            # no snapshot -> empty, no raise
    now = int(time.time())
    fs.provider_usage_write({"codex:x": {"tool": "codex", "name": "x", "type": "subscription",
                                         "ok": False, "error": "no rollout", "checked_at": now}})
    p = pv.usage_for_paint()["providers"][0]
    assert p["ok"] is False and p["error"] == "no rollout" and p["windows"] == [] and p["headline"] is None


def test_poller_registry_is_pluggable(providers_toml, monkeypatch):
    # register a NEW provider kind without touching poll_all; config selects it via `poller`.
    toml = _toml(providers_toml["tmp"], """
        [providers.gemini]
        default = "g1"
        [providers.gemini.g1]
        type = "subscription"
        auth = "env:GEMINI_KEY"
        poller = "gemini-fake"
        track = "windows"
    """)
    monkeypatch.setattr(pv, "FLEET_TOML", toml)
    monkeypatch.setattr(pv, "_read_oauth_token", lambda a: None)
    pv.register_poller("gemini-fake", lambda spec, name: {
        "ok": True, "windows": {"one_day": {"pct": 42.0, "resets_at": None, "window_minutes": 1440}}})
    try:
        snap = pv.poll_all()
        assert snap["gemini:g1"]["ok"] and snap["gemini:g1"]["windows"]["one_day"]["pct"] == 42.0
        view = {p["id"]: p for p in pv.usage_for_paint()["providers"]}
        assert view["gemini:g1"]["windows"][0]["label"] == "one_day"
    finally:
        pv._POLLERS.pop("gemini-fake", None)


def test_poll_all_skips_track_none(providers_toml, monkeypatch):
    toml = _toml(providers_toml["tmp"], """
        [providers.claude]
        default = "vtx"
        [providers.claude.vtx]
        type = "vertex"
        auth = "env-file:/nonexistent"
        track = "none"
    """)
    monkeypatch.setattr(pv, "FLEET_TOML", toml)
    assert pv.poll_all() == {}                                # vertex (track none) is not polled


# --- codex env-token: fleet-owned refresh (mocked HTTP; never a live refresh) --------------------
import base64


def _jwt(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"aaa.{body}.bbb"


def _seed_authjson(tmp_path, access_exp, rt="refresh-1", email="a@b.com", plan="team"):
    aj = tmp_path / "auth.json"
    aj.write_text(json.dumps({"tokens": {
        "access_token": _jwt({"exp": access_exp}),
        "id_token": _jwt({"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": plan}}),
        "refresh_token": rt, "account_id": "acc-1"}}))
    return str(aj)


def test_codex_seed_from_authjson_writes_cred_token_identity(tmp_path):
    now = int(time.time())
    cred = pv.codex_seed_from_authjson("acctx", _seed_authjson(tmp_path, now + 999999))
    assert cred["refresh_token"] == "refresh-1" and cred["identity"]["email"] == "a@b.com"
    # both files exist, 0600, and the .token holds JUST the access token
    import os as _os, stat
    tokp, credp = pv._codex_token_path("acctx"), pv._codex_cred_path("acctx")
    assert open(tokp).read() == cred["access_token"]
    assert stat.S_IMODE(_os.stat(tokp).st_mode) == 0o600 and stat.S_IMODE(_os.stat(credp).st_mode) == 0o600
    assert pv._codex_token_identity("acctx")["plan"] == "team"


def test_codex_ensure_fresh_skips_refresh_when_valid(tmp_path, monkeypatch):
    now = int(time.time())
    pv.codex_seed_from_authjson("acctx", _seed_authjson(tmp_path, now + 999999))   # far-future exp
    monkeypatch.setattr(pv, "_oauth_refresh", lambda rt: (_ for _ in ()).throw(AssertionError("must not refresh")))
    assert pv.codex_ensure_fresh("acctx")                                          # returns without refreshing


def test_codex_ensure_fresh_refreshes_and_persists_rotated_refresh_token(tmp_path, monkeypatch):
    now = int(time.time())
    pv.codex_seed_from_authjson("acctx", _seed_authjson(tmp_path, now + 60, rt="refresh-OLD"))  # near expiry
    new_at = _jwt({"exp": now + 999999})
    calls = {}
    def fake_refresh(rt):
        calls["sent"] = rt
        return {"access_token": new_at, "refresh_token": "refresh-NEW-rotated", "expires_at": now + 999999}
    monkeypatch.setattr(pv, "_oauth_refresh", fake_refresh)
    got = pv.codex_ensure_fresh("acctx")
    assert got == new_at and calls["sent"] == "refresh-OLD"
    cred = json.load(open(pv._codex_cred_path("acctx")))
    assert cred["refresh_token"] == "refresh-NEW-rotated"        # refinement 2: rotated token persisted
    assert cred["access_token"] == new_at
    assert open(pv._codex_token_path("acctx")).read() == new_at  # .token file updated for the launch $(cat)


def test_codex_ensure_fresh_unseeded_revoked_and_transient(tmp_path, monkeypatch):
    with pytest.raises(pv.ProviderError, match="not seeded"):
        pv.codex_ensure_fresh("ghost")
    now = int(time.time())
    pv.codex_seed_from_authjson("dead", _seed_authjson(tmp_path, now + 60))         # near expiry -> refreshes
    # a genuine rejection propagates as ProviderError (revoked), NOT transient
    monkeypatch.setattr(pv, "_oauth_refresh", lambda rt: (_ for _ in ()).throw(pv.ProviderError("revoked")))
    with pytest.raises(pv.ProviderError) as ei:
        pv.codex_ensure_fresh("dead")
    assert not isinstance(ei.value, pv.ProviderTransientError)
    # an UNEXPECTED error is treated as TRANSIENT (never cry wolf), not revoked
    monkeypatch.setattr(pv, "_oauth_refresh", lambda rt: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(pv.ProviderTransientError):
        pv.codex_ensure_fresh("dead")


def test_poll_codex_filters_by_model_provider(tmp_path):
    home = str(tmp_path / "unified")
    now = int(time.time())
    d = os.path.join(home, "sessions", "2026", "07", "10")
    os.makedirs(d)
    def rollout(fn, mp, pct):
        ev = {"payload": {"model_provider": mp, "rate_limits": {
            "primary": {"used_percent": pct, "window_minutes": 300, "resets_at": now + 60},
            "secondary": None}}}
        p = os.path.join(d, fn)
        open(p, "w").write(json.dumps({"model_provider": mp}) + "\n" + json.dumps(ev) + "\n")
        return p
    older = rollout("rollout-2026-07-10T09-00-00-a.jsonl", "acctx", 11.0)
    newer = rollout("rollout-2026-07-10T10-00-00-b.jsonl", "other", 99.0)
    os.utime(newer, (now, now))              # newer mtime
    os.utime(older, (now - 100, now - 100))
    r = pv.poll_codex(home, model_provider="acctx")
    assert r["ok"] and r["windows"]["five_hour"]["pct"] == 11.0   # picked acctx's older rollout, not other's
    assert pv.poll_codex(home, model_provider="missing")["ok"] is False


# --- codex account health monitor (built on the refresh loop) ------------------------------------
def _codex_health_toml(tmp_path, monkeypatch, accts, home_accts=()):
    lines = ["[providers.codex]", f'default = "{accts[0]}"']
    for a in accts:
        lines += [f"[providers.codex.{a}]", 'type = "subscription"', f'auth = "codex-token:{tmp_path}/{a}.token"']
    for a in home_accts:                              # a codex-HOME (fallback) provider: must NOT be monitored
        lines += [f"[providers.codex.{a}]", 'type = "subscription"', f'auth = "codex-home:{tmp_path}/{a}"']
    monkeypatch.setattr(pv, "FLEET_TOML", _toml(tmp_path, "\n".join(lines)))


def test_codex_health_check_classifies(tmp_path, monkeypatch):
    _codex_health_toml(tmp_path, monkeypatch, ["good", "dead", "new"], home_accts=["home1"])
    def fake_ensure(acct, force=False):
        if acct == "good":
            return "tok"
        if acct == "new":
            raise pv.ProviderError("codex account 'new' is not seeded (...missing)")
        raise pv.ProviderError("codex account 'dead' token refresh failed; the account may be revoked")
    monkeypatch.setattr(pv, "codex_ensure_fresh", fake_ensure)
    h = {r["acct"]: r["status"] for r in pv.codex_health_check()}
    assert h == {"good": "healthy", "dead": "revoked", "new": "unseeded"}   # home1 (codex-home) is NOT monitored


def test_codex_health_scan_edge_triggers_and_rearms(tmp_path, monkeypatch):
    _codex_health_toml(tmp_path, monkeypatch, ["a"])
    st = {"v": "revoked"}
    def fake_ensure(acct, force=False):
        if st["v"] == "healthy":
            return "t"
        raise pv.ProviderError("refresh failed; may be revoked")
    monkeypatch.setattr(pv, "codex_ensure_fresh", fake_ensure)
    calls = []
    notify = lambda acct, email, msg: calls.append(acct)
    pv.codex_health_scan(notify); assert calls == ["a"]          # first revocation -> alert
    pv.codex_health_scan(notify); assert calls == ["a"]          # still revoked -> deduped (no storm)
    st["v"] = "healthy"; pv.codex_health_scan(notify); assert calls == ["a"]   # recovered -> re-arm, no alert
    st["v"] = "revoked"; pv.codex_health_scan(notify); assert calls == ["a", "a"]  # revoked again -> alert again


def test_oauth_refresh_rejection_is_revoked_network_is_transient(monkeypatch):
    import urllib.error
    def raiser(exc):
        def _f(req, timeout=None):
            raise exc
        return _f
    # HTTP 401 invalid_grant = the endpoint REJECTED the token -> REVOKED (ProviderError, not Transient)
    monkeypatch.setattr("urllib.request.urlopen", raiser(urllib.error.HTTPError("u", 401, "no", None, None)))
    with pytest.raises(pv.ProviderError) as ei:
        pv._oauth_refresh("rt")
    assert not isinstance(ei.value, pv.ProviderTransientError)
    # HTTP 503 (server) and a network error = TRANSIENT (ProviderTransientError)
    monkeypatch.setattr("urllib.request.urlopen", raiser(urllib.error.HTTPError("u", 503, "busy", None, None)))
    with pytest.raises(pv.ProviderTransientError):
        pv._oauth_refresh("rt")
    monkeypatch.setattr("urllib.request.urlopen", raiser(urllib.error.URLError("connection refused")))
    with pytest.raises(pv.ProviderTransientError):
        pv._oauth_refresh("rt")


def test_health_transient_refresh_is_error_no_alert_revoked_alerts(tmp_path, monkeypatch):
    # a near-expiry token so ensure_fresh actually attempts a refresh; the two refresh outcomes must diverge.
    _codex_health_toml(tmp_path, monkeypatch, ["a"])
    pv.codex_seed_from_authjson("a", _seed_authjson(tmp_path, int(time.time()) + 60))   # near expiry -> refreshes
    calls = []
    # network blip -> transient -> status 'error', NO alert (the cry-wolf Berg wanted avoided)
    monkeypatch.setattr(pv, "_oauth_refresh", lambda rt: (_ for _ in ()).throw(pv.ProviderTransientError("net")))
    h = {r["acct"]: r["status"] for r in pv.codex_health_scan(lambda *a: calls.append(a))}
    assert h["a"] == "error" and calls == []
    # genuine rejection -> revoked -> alert
    monkeypatch.setattr(pv, "_oauth_refresh", lambda rt: (_ for _ in ()).throw(pv.ProviderError("rejected; revoked")))
    h = {r["acct"]: r["status"] for r in pv.codex_health_scan(lambda *a: calls.append(a))}
    assert h["a"] == "revoked" and len(calls) == 1


def test_codex_health_scan_unseeded_never_alerts(tmp_path, monkeypatch):
    _codex_health_toml(tmp_path, monkeypatch, ["a"])
    monkeypatch.setattr(pv, "codex_ensure_fresh",
                        lambda acct, force=False: (_ for _ in ()).throw(pv.ProviderError("not seeded")))
    calls = []
    pv.codex_health_scan(lambda *a: calls.append(a))
    assert calls == []                                           # unseeded is a setup state, not an offline alert
    from cmux_fleet import state as fs
    assert fs.codex_health_read()["a"]["status"] == "unseeded"   # but the state is still persisted


def test_codex_health_scan_notify_failure_does_not_sink_scan(tmp_path, monkeypatch):
    _codex_health_toml(tmp_path, monkeypatch, ["a"])
    monkeypatch.setattr(pv, "codex_ensure_fresh",
                        lambda acct, force=False: (_ for _ in ()).throw(pv.ProviderError("revoked")))
    def boom(*a):
        raise RuntimeError("cmux gone")
    h = pv.codex_health_scan(boom)                               # must not raise
    assert h[0]["status"] == "revoked"
    from cmux_fleet import state as fs
    assert fs.codex_health_read()["a"]["status"] == "revoked"    # state still persisted despite notify blowing up


# --- config.toml fenced provisioning (never clobber Berg's manual config) ------------------------
def test_codex_provision_config_fenced_and_idempotent(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "gpt-5.5"\n\n[mcp_servers.x]\nurl = "http://y"\n')   # Berg's manual config
    pv.codex_provision_config("acctx", str(cfg))
    t = cfg.read_text()
    assert 'model = "gpt-5.5"' in t and "[mcp_servers.x]" in t                 # manual config preserved
    assert pv.CODEX_FENCE_BEGIN in t and "[model_providers.acctx]" in t
    assert 'env_key = "CMUX_FLEET_CODEX_TOKEN"' in t and "requires_openai_auth = false" in t
    # idempotent + multi-account: re-run with a 2nd acct, both present, fence appears ONCE
    pv.codex_provision_config("accty", str(cfg))
    t2 = cfg.read_text()
    assert t2.count(pv.CODEX_FENCE_BEGIN) == 1
    assert "[model_providers.acctx]" in t2 and "[model_providers.accty]" in t2
    # re-running the same acct doesn't duplicate it
    pv.codex_provision_config("acctx", str(cfg))
    assert cfg.read_text().count("[model_providers.acctx]") == 1


def test_codex_provision_refuses_manual_duplicate(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[model_providers.acctx]\nname = "OpenAI"\n')                 # Berg defined it by hand
    with pytest.raises(pv.ProviderError, match="OUTSIDE the fleet fence"):
        pv.codex_provision_config("acctx", str(cfg))


def test_codex_setup_cli_end_to_end(cli_env, tmp_path):
    aj = tmp_path / "auth.json"
    aj.write_text(json.dumps({"tokens": {
        "access_token": _jwt({"exp": int(time.time()) + 999999}),
        "id_token": _jwt({"email": "acct@x.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "team"}}),
        "refresh_token": "r1", "account_id": "a1"}}))
    # --no-provision keeps the test off the host's real ~/.codex/config.toml; seed goes to the throwaway STATE.
    p = subprocess.run([sys.executable, "-m", "cmux_fleet", "codex-setup", "acctx",
                        "--auth-json", str(aj), "--no-provision"],
                       env=dict(cli_env), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "seeded codex account 'acctx'" in p.stdout and "acct@x.com" in p.stdout
    assert "codex-token:providers/codex-acctx.token" in p.stdout               # the fleet.toml hint


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


def test_launch_codex_provider_env_token_threads_args(cli_env, tmp_path):
    # codex env-token path: -c model_provider=<acct> threaded into the codex command, token via $(cat ...).
    tokfile = tmp_path / "cx.token"
    tokfile.write_text("CXSECRET-oauth-token\n")
    toml = _toml(tmp_path, f"""
        [tool.codex]
        flags = "-a never"
        [role.cxworker]
        kind = "child"
        place = "tab"
        cwd = "cxworker"
        tool = "codex"
        [providers.codex]
        default = "acctx"
        [providers.codex.acctx]
        type = "subscription"
        auth = "codex-token:{tokfile}"
    """)
    env = dict(cli_env)
    env["CMUX_FLEET_TOML"] = toml
    p = subprocess.run([sys.executable, "-m", "cmux_fleet", "launch", "cxworker",
                        "--provider", "codex:acctx", "--dry-run"],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "provider: codex:acctx" in p.stdout
    assert "-c model_provider=acctx" in p.stdout             # per-launch account selection
    assert f'CMUX_FLEET_CODEX_TOKEN="$(cat {tokfile})"' in p.stdout   # spawn-time token read
    assert "CXSECRET" not in p.stdout                        # the token VALUE is never printed
