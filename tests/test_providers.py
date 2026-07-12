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
        [providers.claude.acct2]
        type = "subscription"
        auth = "securestorage:~/.claude-acct2"
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
    assert set(doc["claude"]["providers"]) == {"berg-max", "throwaway", "acct2", "vtx"}
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


# --- securestorage: a second claude account via keychain namespacing, NO config-dir swap ----------
# CLAUDE_SECURESTORAGE_CONFIG_DIR namespaces ONLY the keychain credential (service
# `Claude Code-credentials-<sha256(dir)[:8]>`); CLAUDE_CONFIG_DIR — session logs, hooks, settings — is
# untouched. Proven live 2026-07-12: bogus dir 401s, real dir runs + polls, logs stay in ~/.claude.
def test_securestorage_service_namespace_rule():
    # the EXACT sha256[:8] claude uses, pinned on ABSOLUTE paths (machine-independent literals, hand-verified
    # against the live keychain 2026-07-12) so a hashing regression — trailing slash, wrong slice, wrong algo —
    # fails loudly instead of silently reading an empty namespace and 401-ing every agent.
    assert pv._securestorage_service("/Users/seanyoungberg/.claude-berglabs") == "Claude Code-credentials-00753994"
    assert pv._securestorage_service("/Users/seanyoungberg/.claude") == "Claude Code-credentials-edf52d82"
    # ~ is expanded before hashing (a literal "~/..." would hash differently and read the wrong namespace)
    assert pv._securestorage_service("~/.claude-x") == \
        pv._securestorage_service(os.path.expanduser("~/.claude-x"))
    assert pv._securestorage_service("~/.claude-x") != \
        "Claude Code-credentials-" + pv.hashlib.sha256(b"~/.claude-x").hexdigest()[:8]


def test_resolve_securestorage_injects_namespace_var_only(providers_toml):
    r = pv.resolve_launch("claude", "acct2")
    assert r["label"] == "claude:acct2"
    # the account var is injected, config-dir is NOT (that is the whole point: logs/hooks stay default)
    assert r["env"]["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == os.path.expanduser("~/.claude-acct2")
    assert "CLAUDE_CONFIG_DIR" not in r["env"]
    # no secret materialized anywhere — the dir is a keychain key, not a token
    assert r["raw_env"] == {} and not r["provisional"]


def test_read_oauth_token_securestorage_reads_the_namespaced_service(providers_toml, monkeypatch):
    # the poller must query the sha256 service, not the literal path — otherwise it reads the wrong account.
    seen = {}
    class _P:
        stdout = '{"claudeAiOauth":{"accessToken":"sk-ant-oat01-ACCT2"}}'
    def fake_run(argv, **kw):
        seen["service"] = argv[argv.index("-s") + 1]
        return _P()
    monkeypatch.setattr(pv.subprocess, "run", fake_run)
    tok = pv._read_oauth_token("securestorage:~/.claude-acct2")
    assert tok == "sk-ant-oat01-ACCT2"
    assert seen["service"] == "Claude Code-credentials-" + \
        pv.hashlib.sha256(os.path.expanduser("~/.claude-acct2").encode()).hexdigest()[:8]


# --- per-seat codex homes (the model, settled by the 2026-07-11 coexistence test) -----------------
# The ChatGPT backend keys ONE ACTIVE SESSION PER DEVICE, and the device id (installation_id) is a PER-HOME
# file. Seats sharing a home are one device and revoke each other; seats in their own homes coexist. So the
# home IS the credential boundary, it is REQUIRED, and the fleet must NEVER invent one.

def test_resolve_codex_home_sets_CODEX_HOME_even_when_it_is_the_default_home(providers_toml):
    # berg-team's home IS ~/.codex. The old code returned early there ("default home, no injection needed"),
    # which left the seat's identity IMPLICIT — and implicit identity is what let 3 seats share one device.
    r = pv.resolve_launch("codex", "berg-team")
    assert r["env"]["CODEX_HOME"] == os.path.expanduser("~/.codex")
    assert r["raw_env"] == {} and r["args"] == []     # no token injection, no -c model_provider: the home IS the cred


def test_resolve_codex_home_without_login_errors(providers_toml):
    # a declared home with no auth.json -> refuse loudly and name the fix; NEVER silently fall back to ~/.codex
    with pytest.raises(pv.ProviderError, match="fleet codex-login"):
        pv.resolve_launch("codex", "acct2")


def test_resolve_codex_home_with_login_sets_home(providers_toml):
    home = providers_toml["tmp"] / "codex-acct2"
    home.mkdir(exist_ok=True)
    (home / "auth.json").write_text('{"auth_mode":"chatgpt"}')
    r = pv.resolve_launch("codex", "acct2")
    assert r["env"]["CODEX_HOME"] == str(home) and not r["provisional"] and r["args"] == []


def _codex_toml(tmp_path, monkeypatch, auth):
    monkeypatch.setattr(pv, "FLEET_TOML", _toml(tmp_path, f"""
        [providers.codex]
        default = "acctx"
        [providers.codex.acctx]
        type = "subscription"
        auth = "{auth}"
    """))


def test_resolve_codex_token_is_REFUSED_as_the_superseded_shared_home_model(tmp_path, monkeypatch):
    # REGRESSION GUARD on the actual bug. `codex-token:` was the shared-home env-token path: it pinned every
    # seat to the ONE ~/.codex device, so each login superseded the previous seat. It must not merely be
    # discouraged — a launch on it must FAIL, and say what to do instead.
    tok = tmp_path / "codex-acctx.token"
    tok.write_text("ya29-fake\n")
    _codex_toml(tmp_path, monkeypatch, f"codex-token:{tok}")
    with pytest.raises(pv.ProviderError) as ei:
        pv.resolve_launch("codex", "acctx")
    msg = str(ei.value)
    assert "codex-home:~/.codex-acctx" in msg          # names the convention...
    assert "fleet codex-login acctx" in msg            # ...and the command that fixes it
    assert "superseded" in msg


def test_resolve_codex_without_any_home_is_refused_never_invented(tmp_path, monkeypatch):
    # Berg's steer: "require explicit instead of making something up". A GUESSED home would silently aim a
    # seat at another seat's credentials, so an undeclared home must fail, not default.
    _codex_toml(tmp_path, monkeypatch, "codex-home:")
    with pytest.raises(pv.ProviderError, match="declares no per-seat home"):
        pv.resolve_launch("codex", "acctx")


def test_codex_seat_home_never_guesses(tmp_path, monkeypatch):
    with pytest.raises(pv.ProviderError):
        pv.codex_seat_home("ghost", {"auth": ""})                      # no auth at all
    with pytest.raises(pv.ProviderError):
        pv.codex_seat_home("ghost", {"auth": "codex-token:/x.token"})  # the superseded path
    assert pv.codex_seat_home("s", {"auth": "codex-home:~/.codex-s"}) == os.path.expanduser("~/.codex-s")
    assert pv.codex_home_hint("s") == "~/.codex-s"                     # the stated convention


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


def test_poll_codex_api_identifies_as_codex_not_as_the_anthropic_poller(monkeypatch):
    """The usage call was borrowing the ANTHROPIC poller's User-Agent for a Cloudflare-fronted CHATGPT
    endpoint. Cloudflare now blocks it, so the call 403'd, the caller fell back to the rollout scrape, and the
    sidebar rendered a healthy-looking usage bar built from STALE rollout files for a seat the API was refusing
    to talk to. A false-healthy, and one nobody would have questioned -- the bar looked fine."""
    seen = {}
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"rate_limits": {}}).encode()
    def _f(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return _Resp()
    monkeypatch.setattr("urllib.request.urlopen", _f)
    pv.poll_codex_api("tok")
    assert "codex" in seen["ua"].lower()
    assert "cmux" not in seen["ua"].lower() and "anthropic" not in seen["ua"].lower()


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
    # the seat must be LOGGED IN for its rollouts to mean anything: a home with no auth.json is 'unseeded',
    # and scraping stale rollouts for a seat that cannot run would paint a usage bar for a dead seat. This
    # test is about the API->rollout FALLBACK (a live seat whose API call blipped), so give it both halves.
    os.makedirs(home, exist_ok=True)
    open(os.path.join(home, "auth.json"), "w").write(json.dumps({"tokens": {"access_token": "tok-acct2"}}))
    monkeypatch.setattr(pv, "poll_codex_api", lambda *a, **k: {"ok": False})   # API down -> rollout fallback
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
def _seed_home(tmp_path, acct, email=None, iid=None, logged_in=True, user_id=None, subscription=None):
    """A per-seat codex home on disk. `auth.json` IS the seat's credential. `installation_id` (the device id)
    is minted LAZILY on the first RUN — so a freshly logged-in home legitimately has none, and its absence
    must never read as a failure.

    `user_id` = the PERSON (chatgpt_user_id). `subscription` = the TEAM PLAN (chatgpt_account_id), which
    DIFFERENT PEOPLE legitimately share. Defaults keep them distinct per seat; pass them explicitly to model
    a real team (same subscription, different people) or the wrong-account bug (same person, two homes)."""
    home = tmp_path / f".codex-{acct}"
    home.mkdir(parents=True, exist_ok=True)
    if logged_in:
        toks = {"access_token": f"tok-{acct}"}
        if email or user_id or subscription:
            toks["id_token"] = _jwt({"email": email, "https://api.openai.com/auth": {
                "chatgpt_user_id": user_id or f"user-{acct}",
                "chatgpt_account_id": subscription or f"sub-{acct}"}})
        (home / "auth.json").write_text(json.dumps({"tokens": toks}))
    if iid:
        (home / "installation_id").write_text(iid)
    return home


def _codex_health_toml(tmp_path, monkeypatch, seats):
    """seats: {acct: home | None}. None = the seat declares NO home (the config gap, not a revocation)."""
    lines = ["[providers.codex]", f'default = "{list(seats)[0]}"']
    for a, home in seats.items():
        lines += [f"[providers.codex.{a}]", 'type = "subscription"']
        lines += [f'auth = "codex-home:{home}"'] if home else ['auth = "codex-token:/legacy/x.token"']
    monkeypatch.setattr(pv, "FLEET_TOML", _toml(tmp_path, "\n".join(lines)))


def test_codex_health_reads_the_SEATS_OWN_HOME_not_the_stale_cred_store(tmp_path, monkeypatch):
    """THE regression this whole change exists for. Health used to read the fleet cred store (seeded from the
    shared ~/.codex) and to SKIP any seat not on the old codex-token path. Once a seat moved into its own
    home, that stored token was a stale artifact of a device the seat no longer uses — so a seat that was
    demonstrably healthy (backend 200, model speaking) kept being reported REVOKED."""
    live = _seed_home(tmp_path, "live", email="live@x.com")
    dead = _seed_home(tmp_path, "dead", email="dead@x.com")
    _codex_health_toml(tmp_path, monkeypatch, {"live": live, "dead": dead})
    # the probe is keyed on the token, and each token is unique PER HOME -- so reading the wrong home (or a
    # cred store) cannot accidentally produce the right answer.
    verdict = {"tok-live": "live", "tok-dead": "revoked"}
    monkeypatch.setattr(pv, "codex_probe_backend", lambda tok, **k: verdict[tok])
    monkeypatch.setattr(pv, "codex_ensure_fresh",   # if health still reached for the cred store, fail loudly
                        lambda *a, **k: pytest.fail("health must read the SEAT HOME, never the cred store"))
    h = {r["acct"]: r for r in pv.codex_health_check()}
    assert h["live"]["status"] == "healthy" and h["live"]["email"] == "live@x.com"
    assert h["live"]["home"] == str(live)
    assert h["dead"]["status"] == "revoked" and "codex-login dead" in h["dead"]["detail"]


def test_codex_health_undeclared_home_is_needs_home_NOT_revoked(tmp_path, monkeypatch):
    """A seat with no per-seat home is a CONFIG gap, not a revocation. Calling it 'revoked' would send the
    operator off to re-login a seat whose credential is fine — and would fire the offline alert."""
    _codex_health_toml(tmp_path, monkeypatch, {"nohome": None})
    h = {r["acct"]: r for r in pv.codex_health_check()}
    assert h["nohome"]["status"] == "needs-home"
    assert "codex-home:~/.codex-nohome" in h["nohome"]["detail"]      # tells them exactly what to write


def test_codex_health_declared_home_never_logged_in_is_unseeded(tmp_path, monkeypatch):
    empty = _seed_home(tmp_path, "new", logged_in=False)
    _codex_health_toml(tmp_path, monkeypatch, {"new": empty})
    h = {r["acct"]: r for r in pv.codex_health_check()}
    assert h["new"]["status"] == "unseeded" and "fleet codex-login new" in h["new"]["detail"]


def _http_error(code, ctype):
    """An HTTPError carrying a real content-type, because content-type is now load-bearing: it is what tells
    'the API rejected this token' (JSON) from 'a WAF blocked this client' (HTML)."""
    import email.message, urllib.error
    h = email.message.Message()
    h["content-type"] = ctype
    return urllib.error.HTTPError("u", code, "no", h, None)


def test_codex_probe_backend_classifies(monkeypatch):
    import urllib.error
    class _Resp:                                    # minimal context-manager stand-in for urlopen's return
        def __init__(self, status): self.status = status
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def urlopen_status(status):
        def _f(req, timeout=None): return _Resp(status)
        return _f
    def urlopen_raise(exc):
        def _f(req, timeout=None): raise exc
        return _f
    # 200 = the backend accepts the token -> live
    monkeypatch.setattr("urllib.request.urlopen", urlopen_status(200))
    assert pv.codex_probe_backend("tok") == "live"
    # 401/403 WITH A JSON BODY = the API itself REJECTED a token the clock still thinks is valid -> revoked
    monkeypatch.setattr("urllib.request.urlopen", urlopen_raise(_http_error(401, "application/json")))
    assert pv.codex_probe_backend("tok") == "revoked"
    monkeypatch.setattr("urllib.request.urlopen", urlopen_raise(_http_error(403, "application/json")))
    assert pv.codex_probe_backend("tok") == "revoked"
    # 5xx and network errors are TRANSIENT -> unreachable (never cry wolf)
    monkeypatch.setattr("urllib.request.urlopen", urlopen_raise(_http_error(503, "text/html")))
    assert pv.codex_probe_backend("tok") == "unreachable"
    monkeypatch.setattr("urllib.request.urlopen", urlopen_raise(urllib.error.URLError("connection refused")))
    assert pv.codex_probe_backend("tok") == "unreachable"


def test_codex_probe_backend_a_CLOUDFLARE_403_IS_NOT_A_REVOCATION(monkeypatch):
    """CAUGHT LIVE on Berg's fleet, 2026-07-12. chatgpt.com sits behind Cloudflare, which 403s an unrecognized
    client with an HTML challenge page. The probe identified as 'cmux-fleet-health' and mapped every 403 to
    'revoked' -- so a bot block became a REVOCATION VERDICT against three demonstrably healthy seats (all three
    had just spoken; all three returned HTTP 200 the instant a codex User-Agent was used).

    And the verdict is not merely wrong, it is DESTRUCTIVE: the remedy it prints is `fleet codex-login <acct>`,
    and a login SUPERSEDES the seat. The false alarm would have killed the working seat it misdiagnosed.

    Only the API may condemn a token. An HTML body is the network refusing to carry the question, not the
    backend rejecting the credential -- we learned NOTHING about the token, so the honest answer is
    'unreachable' (transient, no alert)."""
    import urllib.error
    for code in (401, 403):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda req, timeout=None, c=code: (_ for _ in ()).throw(_http_error(c, "text/html; charset=UTF-8")))
        assert pv.codex_probe_backend("tok") == "unreachable", f"an HTML {code} must never read as 'revoked'"


def test_codex_probe_backend_identifies_as_codex(monkeypatch):
    """The other half: do not get blocked in the first place. Our own UA is what Cloudflare was rejecting."""
    seen = {}
    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def _f(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return _Resp()
    monkeypatch.setattr("urllib.request.urlopen", _f)
    pv.codex_probe_backend("tok")
    assert "codex" in seen["ua"].lower()                      # identify as codex...
    assert "cmux" not in seen["ua"].lower()                   # ...never as ourselves (that is what got blocked)


def test_codex_health_check_detects_backend_revocation(tmp_path, monkeypatch):
    """THE regression for the expiry-only gap. A SUPERSEDED token still looks perfect locally — it is present,
    well-formed, and its `exp` is in the future — and the IdP userinfo endpoint calls it fine too. Both those
    layers are FALSE-HEALTHY. Only the ChatGPT backend sees the supersession, so the backend probe is the sole
    thing allowed to return the verdict: local validity must never, on its own, produce 'healthy'.

    All three seats below are locally indistinguishable; only the backend tells them apart."""
    seats = {a: _seed_home(tmp_path, a) for a in ("superseded", "alive", "flaky")}
    _codex_health_toml(tmp_path, monkeypatch, seats)
    # keyed on the token, which _seed_home makes unique PER HOME -- so a verdict cannot land on the right seat
    # by accident (e.g. by iteration order, or by reading some other home's credential).
    verdict = {"tok-superseded": "revoked", "tok-alive": "live", "tok-flaky": "unreachable"}
    monkeypatch.setattr(pv, "codex_probe_backend", lambda tok, **k: verdict[tok])
    monkeypatch.setattr(pv, "codex_ensure_fresh",   # the cred store is the stale layer: touching it is the bug
                        lambda *a, **k: pytest.fail("health must read the SEAT HOME, never the cred store"))
    h = {r["acct"]: r["status"] for r in pv.codex_health_check()}
    assert h["superseded"] == "revoked"              # backend 401 despite a locally-valid token -> the gap, closed
    assert h["alive"] == "healthy"                   # backend 200 -> genuinely healthy
    assert h["flaky"] == "error"                     # unreachable -> we could not VERIFY, so we do not claim we
    #                                                  did: 'error' (transient, no alert), never a false-'healthy'


def test_codex_health_scan_edge_triggers_and_rearms(tmp_path, monkeypatch):
    home = _seed_home(tmp_path, "a")
    _codex_health_toml(tmp_path, monkeypatch, {"a": home})
    st = {"v": "revoked"}
    monkeypatch.setattr(pv, "codex_probe_backend", lambda tok, **k: st["v"] if st["v"] != "healthy" else "live")
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


def test_health_unreachable_backend_is_error_no_alert_revoked_alerts(tmp_path, monkeypatch):
    # an UNREACHABLE backend must never be reported as healthy (we did not verify) nor as revoked (we did not
    # see a rejection). It is 'error': no alert, retry next tick. Only a real rejection alerts.
    home = _seed_home(tmp_path, "a")
    _codex_health_toml(tmp_path, monkeypatch, {"a": home})
    calls = []
    monkeypatch.setattr(pv, "codex_probe_backend", lambda tok, **k: "unreachable")
    h = {r["acct"]: r["status"] for r in pv.codex_health_scan(lambda *a: calls.append(a))}
    assert h["a"] == "error" and calls == []                     # the cry-wolf Berg wanted avoided
    monkeypatch.setattr(pv, "codex_probe_backend", lambda tok, **k: "revoked")
    h = {r["acct"]: r["status"] for r in pv.codex_health_scan(lambda *a: calls.append(a))}
    assert h["a"] == "revoked" and len(calls) == 1


def test_codex_health_scan_unseeded_and_needs_home_never_alert(tmp_path, monkeypatch):
    # both are SETUP states, not offline accounts. Alerting on them would page Berg about config.
    new = _seed_home(tmp_path, "new", logged_in=False)
    _codex_health_toml(tmp_path, monkeypatch, {"new": new, "nohome": None})
    calls = []
    pv.codex_health_scan(lambda *a: calls.append(a))
    assert calls == []
    from cmux_fleet import state as fs
    st = fs.codex_health_read()
    assert st["new"]["status"] == "unseeded" and st["nohome"]["status"] == "needs-home"   # still persisted


def test_codex_health_scan_notify_failure_does_not_sink_scan(tmp_path, monkeypatch):
    home = _seed_home(tmp_path, "a")
    _codex_health_toml(tmp_path, monkeypatch, {"a": home})
    monkeypatch.setattr(pv, "codex_probe_backend", lambda tok, **k: "revoked")
    def boom(*a):
        raise RuntimeError("cmux gone")
    h = pv.codex_health_scan(boom)                               # must not raise
    assert h[0]["status"] == "revoked"
    from cmux_fleet import state as fs
    assert fs.codex_health_read()["a"]["status"] == "revoked"    # state still persisted despite notify blowing up


# --- the false-positive class: what counts as PROOF that a seat spoke ----------------------------
# This section exists to keep ONE specific bug from ever coming back. `codex exec` echoes the PROMPT to
# stdout BEFORE it authenticates. So a run that 401s -- zero assistant output, a totally failed run --
# still contains your nonce in its stdout. Grepping stdout therefore PASSES A TOTAL FAILURE, and it nearly
# shipped three separate times.
#
# The rule these tests enforce: the ONLY admissible proof is an assistant message file (`codex exec -o`),
# because a 401 never produces an assistant message and so structurally CANNOT write one. stdout, the exit
# code, and a task_complete marker are all counterfeit-able by the failure and are therefore never trusted.
def _fake_codex(monkeypatch, *, writes_assistant_message, says=None, exit_code=0):
    """Stand in for `codex exec`, faithful on the one axis that matters: it echoes the prompt (hence the
    nonce) to stdout no matter what, and writes an assistant-message file ONLY when authenticated.

    `writes_assistant_message=False` IS the 401. It is deliberately hostile: it hands back every signal a
    lazy implementation might be tempted to trust -- the nonce in stdout, exit 0, task_complete -- and
    withholds only the assistant message. We do not get to assume codex flags the failure in its exit code."""
    seen = {}
    def run(argv, **kw):
        prompt = argv[-1]
        out = argv[argv.index("-o") + 1]
        seen["nonce"] = prompt.split()[-1]                       # what codex_seat_spoke minted, recovered from argv
        if writes_assistant_message:
            open(out, "w").write(seen["nonce"] if says is None else says)
        # the echo is not embellishment -- it is the actual observed behaviour that created the false positive
        seen["stdout"] = f"codex exec\nprompt: {prompt}\ntask_complete\n"
        return subprocess.CompletedProcess(argv, exit_code, seen["stdout"], "")
    monkeypatch.setattr("subprocess.run", run)
    return seen


def test_codex_seat_spoke_REFUSES_a_401_whose_stdout_CONTAINS_the_nonce(tmp_path, monkeypatch):
    """THE false-positive class, pinned. A failed (401) run whose stdout carries the nonce, exits 0, and says
    task_complete must be judged NOT SPOKEN. Any implementation that grades on stdout, the exit code, or
    task_complete returns True here and fails this test -- which is exactly the point of it existing."""
    seen = _fake_codex(monkeypatch, writes_assistant_message=False)
    ok, detail = pv.codex_seat_spoke(str(tmp_path))
    # first prove the trap was actually ARMED: the counterfeit signal really is sitting in stdout. Without
    # this, a fake that quietly failed to echo the nonce would make the test pass for the wrong reason.
    assert seen["nonce"] in seen["stdout"] and "task_complete" in seen["stdout"]
    assert ok is False                                           # <- the whole point
    assert "did not authenticate" in detail


def test_codex_seat_spoke_passes_only_when_the_model_ACTUALLY_speaks(tmp_path, monkeypatch):
    """The reachable green. Without this, an always-False implementation would satisfy the test above and the
    guard would be vacuous."""
    _fake_codex(monkeypatch, writes_assistant_message=True)
    ok, detail = pv.codex_seat_spoke(str(tmp_path))
    assert ok is True and "the model spoke" in detail


def test_codex_seat_spoke_rejects_an_assistant_message_that_is_not_the_nonce(tmp_path, monkeypatch):
    """An assistant message is necessary but not sufficient: it must be OUR nonce. A stale or unrelated reply
    (a resumed thread, a refusal) is not proof that THIS seat answered THIS prompt."""
    _fake_codex(monkeypatch, writes_assistant_message=True, says="I cannot help with that.")
    ok, detail = pv.codex_seat_spoke(str(tmp_path))
    assert ok is False and "wrote something else" in detail


def test_codex_seat_spoke_passes_the_seat_home_to_codex(tmp_path, monkeypatch):
    """The verification must run in the SEAT's home, or it proves a different seat is healthy."""
    seen = {}
    def run(argv, **kw):
        seen["home"] = kw["env"]["CODEX_HOME"]
        return subprocess.CompletedProcess(argv, 1, "", "")
    monkeypatch.setattr("subprocess.run", run)
    pv.codex_seat_spoke(str(tmp_path / "seat"))
    assert seen["home"] == str(tmp_path / "seat")


# --- the usage poller reads the SEAT's home, and agrees with health ------------------------------
def test_poll_codex_provider_polls_each_seat_from_ITS_OWN_home(tmp_path, monkeypatch):
    """The sidebar's numbers must come from the seat's OWN home. Polling one home for every seat would show
    the same usage bar under three different names -- and, worse, attribute one account's spend to another."""
    seats = {a: _seed_home(tmp_path, a, email=f"{a}@x.com") for a in ("dot", "labs")}
    _codex_health_toml(tmp_path, monkeypatch, seats)
    seen = {}
    def fake_api(tok, acct_id=None):
        seen[tok] = True                              # the token is unique per home -> proves which home was read
        return {"ok": True, "windows": {"five_hour": {"pct": 1.0 if tok == "tok-dot" else 9.0}}}
    monkeypatch.setattr(pv, "poll_codex_api", fake_api)
    out = {n: pv._poll_codex_provider(s, n) for n, s in
           [(n, pv.get_provider("codex", n)) for n in ("dot", "labs")]}
    assert seen == {"tok-dot": True, "tok-labs": True}                  # each seat's OWN token, not a shared one
    assert out["dot"]["windows"]["five_hour"]["pct"] == 1.0             # and distinct numbers per seat
    assert out["labs"]["windows"]["five_hour"]["pct"] == 9.0
    assert out["dot"]["identity"]["email"] == "dot@x.com"               # identity from that home too
    assert out["labs"]["identity"]["email"] == "labs@x.com"


def test_poll_codex_provider_unseeded_seat_says_UNSEEDED_not_no_rollouts(tmp_path, monkeypatch):
    """FOUND LIVE against the real 3-seat config (sean-flat, mid-login). A seat that was never logged in used
    to fall through to the rollout scrape, which reported `no rollout sessions found in this CODEX_HOME`.

    That is the original disease in miniature: an unseeded home has no rollouts EITHER, so the wrong probe
    returns a plausible artifact that reads as "this seat just hasn't run lately" -- sending the operator off
    to look for work when the seat was simply never logged in and the fix is one command. It also made the
    poller and HEALTH disagree about the very same seat, which _poll_codex_provider's shape exists to prevent.
    The poller must speak health's vocabulary: 'unseeded', with the login command."""
    home = _seed_home(tmp_path, "new", logged_in=False)                 # home exists, no auth.json
    _codex_health_toml(tmp_path, monkeypatch, {"new": home})
    monkeypatch.setattr(pv, "poll_codex", lambda *a, **k: pytest.fail(
        "an unseeded seat must never be answered by the rollout scrape"))
    r = pv._poll_codex_provider(pv.get_provider("codex", "new"), "new")
    assert r["ok"] is False and r["error"] == "unseeded"                # the TRUE state...
    assert "fleet codex-login new" in r["detail"]                       # ...and the one command that fixes it
    # and the property that failing this test would have broken: the poller and health agree, seat by seat.
    assert {x["acct"]: x["status"] for x in pv.codex_health_check()}["new"] == "unseeded"


# --- the wrong-account interlock: one PERSON in two homes (never one SUBSCRIPTION) ---------------
# Berg's real topology, and the distinction the first version of this guard got wrong:
#   berglabs   sean@berglabs.net       person KUwx…   subscription 77cd2846  \  ONE TEAM PLAN,
#   sean-flat  seanyoungberg@gmail.com person mzQC…   subscription 77cd2846  /  TWO PEOPLE — legal.
#   sean-dot   sean.youngberg@gmail.com person 6MUK…  subscription 20495a2e
# chatgpt_account_id is the BILL; several people share one, because that is what a team seat IS. A guard keyed
# on it calls berglabs+sean-flat a duplicate and BLOCKS A VALID SETUP -- it cried wolf on a correct login.
# chatgpt_user_id is the PERSON, and the person is what the backend keys a device session on. Same person in
# two homes = two devices for one identity = they supersede each other. Key on the person, never the plan.
_SUB_TEAM = "77cd2846"


def test_collision_allows_TEAMMATES_sharing_one_subscription(tmp_path, monkeypatch):
    """The false positive that a subscription-keyed guard produces. Two DIFFERENT PEOPLE on ONE team plan is
    the normal, intended setup -- it must be allowed, or the guard blocks Berg's correct login."""
    seats = {"berglabs": _seed_home(tmp_path, "berglabs", email="sean@berglabs.net",
                                    user_id="user-KUwx", subscription=_SUB_TEAM),
             "sean-flat": _seed_home(tmp_path, "sean-flat", email="seanyoungberg@gmail.com",
                                     user_id="user-mzQC", subscription=_SUB_TEAM)}
    _codex_health_toml(tmp_path, monkeypatch, seats)
    for acct in seats:
        assert pv.codex_seat_collision(acct, str(seats[acct])) == ""      # same BILL, different PEOPLE -> fine


def test_collision_catches_the_SAME_PERSON_in_two_homes(tmp_path, monkeypatch):
    """The real bug, live on 2026-07-12: a login reused a signed-in chatgpt.com session, so codex authenticated
    sean@berglabs.net INTO the sean-flat home. One person, two homes -- and note it is the SAME subscription
    too, so the person key catches it without the plan key ever being consulted."""
    seats = {"berglabs": _seed_home(tmp_path, "berglabs", email="sean@berglabs.net",
                                    user_id="user-KUwx", subscription=_SUB_TEAM),
             "sean-flat": _seed_home(tmp_path, "sean-flat", email="sean@berglabs.net",   # WRONG account landed
                                     user_id="user-KUwx", subscription=_SUB_TEAM)}
    _codex_health_toml(tmp_path, monkeypatch, seats)
    assert pv.codex_seat_collision("sean-flat", str(seats["sean-flat"])) == "berglabs"   # names the collision
    assert pv.codex_seat_collision("berglabs", str(seats["berglabs"])) == "sean-flat"


def test_collision_is_a_PURE_READ_and_never_runs_codex(tmp_path, monkeypatch):
    """THE lesson of the whole episode. Verification RUNS codex, and a codex run is what MINTS the home's
    installation_id -- so verifying a mis-logged-in home would mint the second device for that identity and
    supersede the seat we were protecting. The check would DESTROY THE THING IT WAS CHECKING. It must be a
    pure read of auth.json, and it must come BEFORE any run."""
    seats = {a: _seed_home(tmp_path, a, user_id="user-same") for a in ("one", "two")}   # same person, 2 homes
    _codex_health_toml(tmp_path, monkeypatch, seats)
    monkeypatch.setattr(pv, "codex_seat_spoke", lambda *a, **k: pytest.fail(
        "the collision check must NEVER run codex — a run mints the second device and trips the bug"))
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("no subprocess at all"))
    assert pv.codex_seat_collision("two", str(seats["two"])) == "one"


def test_collision_unseeded_home_collides_with_nothing(tmp_path, monkeypatch):
    seats = {"new": _seed_home(tmp_path, "new", logged_in=False),
             "live": _seed_home(tmp_path, "live", user_id="user-live")}
    _codex_health_toml(tmp_path, monkeypatch, seats)
    assert pv.codex_seat_collision("new", str(seats["new"])) == ""     # nothing to collide with; not an error


def test_codex_login_REFUSES_a_wrong_account_home_BEFORE_running_anything(tmp_path, monkeypatch, capsys):
    """The interlock wired into the cycle: a home holding someone else's identity is refused, and nothing is
    run in it. `logged_in` here is a decoy -- the seat would otherwise verify fine, which is exactly why the
    check must precede verification rather than follow it."""
    seats = {"berglabs": _seed_home(tmp_path, "berglabs", email="sean@berglabs.net", user_id="user-KUwx"),
             "sean-flat": _seed_home(tmp_path, "sean-flat", email="sean@berglabs.net", user_id="user-KUwx")}
    logged_in = _login_harness(tmp_path, monkeypatch, seats, verified=["berglabs"])
    monkeypatch.setattr(cli, "codex_verify_seat", lambda home: pytest.fail(
        "the wrong-account home must be refused BEFORE it is verified — verifying it RUNS codex")
        if "sean-flat" in str(home) else (True, "live", "the model spoke"))
    with pytest.raises(SystemExit):                       # a seat is unusable -> nonzero
        cli.cmd_codex_login([])
    assert logged_in == []                                # nothing logged in, nothing superseded
    out = capsys.readouterr().out
    assert "WRONG ACCOUNT IN THIS HOME" in out
    assert "ALREADY seat 'berglabs'" in out               # names WHO it collided with
    assert "Nothing was run in that home" in out          # and says so, so the operator knows it is defused


# --- codex-login cycles ALL seats, and SKIPS the ones already working ----------------------------
# The skip is a SAFETY property, not an optimization: every `codex login` supersedes that account's previous
# session, so "re-login everything to be sure" is exactly how you break the seats that were working. A cycle
# is only safe if it proves a seat is fine and then LEAVES IT ALONE.
def _login_harness(tmp_path, monkeypatch, seats, verified):
    """seats: {acct: home|None}. verified: the accts whose seat currently verifies. Returns the list of accts
    that codex-login actually ATTEMPTED TO LOG IN (which, for a working seat, must stay empty)."""
    _codex_health_toml(tmp_path, monkeypatch, seats)
    # identity is READ FROM DISK, never stubbed: the collision interlock keys on the real chatgpt_user_id in
    # each home's id_token, and a stub that flattened every seat to one fake identity would hide it.
    monkeypatch.setattr(pv, "codex_home_installation_id", lambda h: "dev12345")
    monkeypatch.setattr(cli, "codex_verify_seat",
                        lambda home: (any(f".codex-{a}" == os.path.basename(home) for a in verified),
                                      "live", "the model spoke"))
    logged_in = []
    def fake_login(acct, home, timeout):
        logged_in.append(acct)
        return True, "new@y.com"
    monkeypatch.setattr(cli, "_codex_login_seat", fake_login)
    return logged_in


def test_codex_login_no_acct_CYCLES_every_seat(tmp_path, monkeypatch):
    seats = {a: _seed_home(tmp_path, a) for a in ("one", "two", "three")}
    logged_in = _login_harness(tmp_path, monkeypatch, seats, verified=[])   # none work yet
    cli.cmd_codex_login([])                                                 # no acct -> the whole fleet
    assert logged_in == ["one", "two", "three"]                             # every seat, one at a time


def test_codex_login_SKIPS_a_seat_that_already_verifies(tmp_path, monkeypatch):
    """THE safety property. A login supersedes, so a working seat must be left strictly alone -- proving it is
    healthy is the ONLY thing we may do to it."""
    seats = {a: _seed_home(tmp_path, a) for a in ("working", "broken")}
    logged_in = _login_harness(tmp_path, monkeypatch, seats, verified=["working"])
    cli.cmd_codex_login([])
    assert logged_in == ["broken"]                    # the broken one is fixed...
    assert "working" not in logged_in                 # ...and the working one is NEVER re-logged


def test_codex_login_needs_home_seat_is_reported_and_does_not_abort_the_cycle(tmp_path, monkeypatch, capsys):
    """A seat with no declared home is a CONFIG gap. It must not be guessed at, and it must not stop the other
    seats from being logged in -- one unconfigured seat should never hold the fleet hostage."""
    seats = {"nohome": None, "real": _seed_home(tmp_path, "real")}
    logged_in = _login_harness(tmp_path, monkeypatch, seats, verified=[])
    with pytest.raises(SystemExit):                   # exits nonzero: a seat IS unusable
        cli.cmd_codex_login([])
    assert logged_in == ["real"]                      # the cycle carried on past the config gap
    out = capsys.readouterr().out
    assert "NO HOME DECLARED" in out and "codex-home:~/.codex-nohome" in out    # says exactly what to write


def test_codex_login_verify_only_NEVER_logs_anyone_in(tmp_path, monkeypatch):
    """--verify-only is the safe read of the whole fleet: it must not open a single login, even for a seat that
    is definitely broken."""
    seats = {a: _seed_home(tmp_path, a) for a in ("working", "broken")}
    logged_in = _login_harness(tmp_path, monkeypatch, seats, verified=["working"])
    with pytest.raises(SystemExit):                   # 'broken' is not usable -> nonzero
        cli.cmd_codex_login(["--verify-only"])
    assert logged_in == []                            # nothing was superseded


def test_codex_login_single_acct_still_targets_just_that_seat(tmp_path, monkeypatch):
    seats = {a: _seed_home(tmp_path, a) for a in ("one", "two")}
    logged_in = _login_harness(tmp_path, monkeypatch, seats, verified=[])
    cli.cmd_codex_login(["two"])
    assert logged_in == ["two"]                       # naming a seat still means ONLY that seat


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


def test_codex_setup_cli_REFUSES_and_points_at_codex_login(cli_env, tmp_path):
    """codex-setup provisioned the shared-home env-token model, which IS the supersession bug: it pinned every
    seat to the one ~/.codex device. It must REFUSE rather than hand out config the resolver now rejects — a
    command that still 'worked' would keep rebuilding the exact bug we just spent a week finding."""
    aj = tmp_path / "auth.json"
    aj.write_text(json.dumps({"tokens": {"access_token": "t", "refresh_token": "r1"}}))
    p = subprocess.run([sys.executable, "-m", "cmux_fleet", "codex-setup", "acctx",
                        "--auth-json", str(aj), "--no-provision"],
                       env=dict(cli_env), capture_output=True, text=True)
    assert p.returncode != 0                                                    # refuses; never a silent no-op
    out = p.stdout + p.stderr
    assert "SUPERSEDED" in out and "fleet codex-login acctx" in out             # names the replacement
    assert 'auth = "codex-home:~/.codex-acctx"' in out                          # and the config to write
    assert "codex-token:" not in out                                            # never re-offers the broken model


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


def _codex_launch_toml(tmp_path, auth):
    return _toml(tmp_path, f"""
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
        auth = "{auth}"
    """)


def test_launch_codex_env_token_is_REFUSED_as_the_superseded_shared_home_model(cli_env, tmp_path):
    """A launch on the old `codex-token:` path must FAIL, not launch. That path runs every seat out of the one
    ~/.codex, which is one DEVICE to the backend — so seats revoke each other. Launching anyway would hand
    back an agent that silently kills another seat's session."""
    tokfile = tmp_path / "cx.token"
    tokfile.write_text("CXSECRET-oauth-token\n")
    env = dict(cli_env, CMUX_FLEET_TOML=_codex_launch_toml(tmp_path, f"codex-token:{tokfile}"))
    p = subprocess.run([sys.executable, "-m", "cmux_fleet", "launch", "cxworker",
                        "--provider", "codex:acctx", "--dry-run"],
                       env=env, capture_output=True, text=True)
    assert p.returncode != 0                                            # refused, never launched
    out = p.stdout + p.stderr
    assert "codex-home:~/.codex-acctx" in out                           # says exactly what to write instead
    assert "CXSECRET" not in out                                        # the token VALUE is still never printed


def test_launch_codex_home_threads_CODEX_HOME_and_composes_ZERO_mcp_flags(cli_env, tmp_path):
    """The end-to-end launch on the per-seat model, and the shape cmux-advisor accepted BUG 1 on: a seat home
    is already clean, so the launch composes ZERO `-c mcp_servers.*` flags (main composed 3, which is what
    made codex refuse to load its config and the agent never start)."""
    home = tmp_path / ".codex-acctx"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "t"}}))   # the seat is logged in
    env = dict(cli_env, CMUX_FLEET_TOML=_codex_launch_toml(tmp_path, f"codex-home:{home}"))
    p = subprocess.run([sys.executable, "-m", "cmux_fleet", "launch", "cxworker",
                        "--provider", "codex:acctx", "--dry-run"],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "provider: codex:acctx" in p.stdout
    assert f"CODEX_HOME={home}" in p.stdout                  # the seat's home is VISIBLE on the launch line
    assert "mcp_servers." not in p.stdout                    # ZERO -- a seat home has no desktop cruft to strip
