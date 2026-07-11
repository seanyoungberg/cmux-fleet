#!/usr/bin/env python3
# cmux_fleet/providers.py — the OPTIONAL provider-config feature: which inference providers a tool may
# use, how each is authed at launch, and how a subscription's usage windows are tracked. A clean island
# like features.py: imports config + state and NOTHING from cli.py (no circular import). cli.py calls
# resolve_launch() at launch; daemon.py calls poll_all() on its timer; features.cmd_usage() reads the
# state poll_all() writes.
#
# MODEL (Berg-directed): a TOOL (claude, codex, ...) has N named PROVIDERS. Each provider has a TYPE and
# handling differs per type:
#   subscription — track the 5h + 7-day windows (%, reset). Auth = an OAuth token passed PER LAUNCH
#                  (never a config-dir swap → session logs stay in the tool's default dir).
#   api          — metered; windows don't apply. `track = "budget"` is a stub (leave room, don't build).
#   vertex       — just env vars (an env-file); nothing to track.
# Conductors select a provider PER LAUNCH (`fleet launch <role> --provider <name>`); no global swap.
#
# CONFIG lives as a top-level [providers] table in the fleet toml (SAME file config.FLEET_TOML reads for
# [fleet]); parsed here, not in config.py. Example:
#
#   [providers.claude]
#   default = "berg-max"
#   [providers.claude.berg-max]
#   type = "subscription"; auth = "keychain:Claude Code-credentials"; track = "windows"
#   [providers.claude.throwaway]
#   type = "subscription"; auth = "file:providers/throwaway.token"; track = "windows"
#   [providers.codex]
#   default = "berg-team"
#   [providers.codex.berg-team]
#   type = "subscription"; auth = "codex-home:~/.codex"; track = "windows"
#
# AUTH mini-DSL ("<method>:<arg>"):
#   keychain:<service>  read the tool's OAuth blob from the macOS keychain (the CURRENT logged-in acct;
#                       no launch injection needed — the tool uses the keychain natively).
#   file:<path>         a long-lived token minted by `claude setup-token`; relative → under STATE
#                       (~/.local/state/cmux-fleet/, e.g. providers/<name>.token, 0600). Injected at
#                       launch as CLAUDE_CODE_OAUTH_TOKEN via a spawn-time `$(cat <path>)` so the secret
#                       NEVER lands in the rendered/printed launch command. (Prototype stopgap. ROADMAP:
#                       fold these secrets into Berg's SOPS env-var mechanism — document, do not build.)
#   codex-home:<path>   CODEX_HOME for a codex account. NOTE: codex account SELECTION is UNSETTLED (a live
#                       test is deciding CODEX_HOME-per-profile vs an env-token path). This resolver is a
#                       clean STUB: it emits CODEX_HOME but marks the result PROVISIONAL. Swap the body in
#                       _resolve_codex() when the verdict lands; the interface does not change.
#   env-file:<path>     source KEY=VALUE lines (vertex: CLAUDE_CODE_USE_VERTEX, project, region).
#   env:<VAR>           an api key already present in the ambient/role env (api type; pass-through).
import glob
import json
import os
import re
import shlex
import subprocess
import tempfile
import time

try:
    import tomllib
except ModuleNotFoundError:                      # py<3.11 — feature is a no-op without a toml reader
    tomllib = None

from .config import CMUX, FLEET_TOML, STATE

USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
_OAUTH_HEADERS = {"anthropic-beta": "oauth-2025-04-20", "anthropic-version": "2023-06-01",
                  "User-Agent": "cmux-fleet-usage-poller"}
CODEX_STALE_S = 3600            # a codex profile whose newest rollout is older than this = stale snapshot


class ProviderError(Exception):
    """A launch-time provider resolution failure (unknown provider, missing token file, bad type). Also
    the GENUINELY-REVOKED signal for a token refresh (the endpoint rejected the refresh_token)."""


class ProviderTransientError(ProviderError):
    """A TRANSIENT failure — network down/slow/unreachable, or a 5xx. NOT an offline account: the health
    monitor maps this to 'error' and never alerts on it (retries next tick). A subclass of ProviderError
    so the launch guard (which catches ProviderError) still aborts a launch that can't refresh right now."""


# --- config parse -------------------------------------------------------------------------------
def _providers_doc():
    """Parse the top-level [providers] table from the fleet toml. Absent/malformed → {} (the feature is
    optional; a fleet with no [providers] behaves exactly as before). Shape returned:
        {tool: {"default": <name|"">, "providers": {name: {type, auth, track}}}}"""
    if not tomllib or not os.path.exists(FLEET_TOML):
        return {}
    try:
        with open(FLEET_TOML, "rb") as f:
            root = tomllib.load(f).get("providers") or {}
    except (OSError, ValueError):
        return {}
    out = {}
    for tool, block in root.items():
        if not isinstance(block, dict):
            continue
        default = block.get("default") if isinstance(block.get("default"), str) else ""
        provs = {}
        for name, spec in block.items():
            if not isinstance(spec, dict):          # skip the scalar `default` key
                continue
            # carry every key through (pluggability: a new provider kind can read its own keys), then
            # normalize the ones every provider has.
            entry = dict(spec)
            entry["type"] = str(spec.get("type", "subscription"))
            entry["auth"] = str(spec.get("auth", ""))
            entry["track"] = str(spec.get("track", "windows" if spec.get("type") == "subscription" else "none"))
            entry["poller"] = str(spec["poller"]) if spec.get("poller") else ""
            provs[name] = entry
        out[tool] = {"default": default, "providers": provs}
    return out


def default_provider(tool):
    """The configured default provider name for a tool ("" if none/unconfigured)."""
    return (_providers_doc().get(tool) or {}).get("default", "")


def get_provider(tool, name):
    """Resolve one provider's config dict, or None. `name` "" → the tool's default."""
    t = _providers_doc().get(tool) or {}
    name = name or t.get("default", "")
    return (t.get("providers") or {}).get(name)


def iter_providers():
    """Yield (tool, name, spec, is_default) for every configured provider (all tools)."""
    for tool, t in _providers_doc().items():
        dflt = t.get("default", "")
        for name, spec in (t.get("providers") or {}).items():
            yield tool, name, spec, (name == dflt)


def _parse_auth(auth):
    """'<method>:<arg>' → (method, arg). No colon → (auth, '')."""
    method, _, arg = auth.partition(":")
    return method.strip(), arg.strip()


def _token_path(arg):
    """Resolve a file: token path — relative anchors under STATE (the fleet's XDG state dir)."""
    p = os.path.expanduser(arg)
    return p if os.path.isabs(p) else os.path.join(STATE, p)


# --- launch-time auth resolution (per tool/type) ------------------------------------------------
def resolve_launch(tool, name):
    """Resolve the env a launch needs to run `tool` under provider `name` (default if name==""). Returns:
        {"label": "tool:name", "env": {plain vars}, "raw_env": {var: RAW shell (unquoted by render)},
         "provisional": bool, "note": str}
    `env` values are shlex-quoted by render_send_cmd; `raw_env` values are emitted VERBATIM (caller
    guarantees shell-safety) so a token can be read at spawn via $(cat ...) without ever appearing in the
    rendered command. Raises ProviderError on an unknown/misconfigured provider."""
    doc = _providers_doc()
    if tool not in doc:
        raise ProviderError(f"no [providers.{tool}] configured; cannot select --provider for tool '{tool}'")
    resolved = name or doc[tool].get("default", "")
    spec = get_provider(tool, resolved)
    if not spec:
        avail = ", ".join((doc[tool].get("providers") or {}).keys()) or "(none)"
        raise ProviderError(f"provider '{resolved}' not found under [providers.{tool}] (have: {avail})")
    label = f"{tool}:{resolved}"
    ptype = spec["type"]
    method, arg = _parse_auth(spec["auth"])
    # `args` = extra tool CLI tokens the launch appends (codex needs `-c model_provider=<acct>` to select
    # a unified-home account per launch). claude/vertex/api leave it empty.
    base = {"label": label, "env": {}, "raw_env": {}, "args": [], "provisional": False, "note": ""}

    if ptype == "vertex":
        base["env"] = _read_env_file(arg) if method == "env-file" else {}
        return base
    if ptype == "api":
        base["note"] = f"api provider '{label}' (metered); no window injection"
        return base                                  # key expected in ambient/role env (leave room)
    if ptype != "subscription":
        raise ProviderError(f"provider '{label}' has unknown type '{ptype}'")

    # subscription:
    if tool == "codex":
        return _resolve_codex(base, resolved, method, arg)
    # claude (and any future OAuth-token tool):
    if method == "keychain":
        return base                                  # current account: tool uses the keychain natively
    if method == "file":
        path = _token_path(arg)
        if not os.path.exists(path):
            raise ProviderError(f"provider '{label}': token file not found: {path} "
                                f"(mint it with `claude setup-token` and save it there)")
        # spawn-time read: the secret never enters the rendered/printed command, only the path does.
        base["raw_env"]["CLAUDE_CODE_OAUTH_TOKEN"] = f'"$(cat {shlex.quote(path)})"'
        base["note"] = f"claude token injected from {path} (spawn-time read; not printed)"
        return base
    raise ProviderError(f"provider '{label}': unsupported auth method '{method}' for a claude subscription")


# The env var a codex env-token launch reads for the account's ChatGPT OAuth token. Deliberately custom
# (not codex's own CODEX_ACCESS_TOKEN/CODEX_API_KEY, which have special metered/agent-identity meaning): a
# per-launch `[model_providers.<acct>]` block in ~/.codex/config.toml declares `env_key = "<this>"`.
CODEX_TOKEN_ENV = "CMUX_FLEET_CODEX_TOKEN"

# Codex/ChatGPT OAuth (discovered read-only from a live token's claims + the codex binary, 2026-07-10):
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REFRESH_MARGIN_S = 1800          # refresh when the access token expires within 30 min
# The ChatGPT backend endpoint a codex token actually authenticates against — the layer that REVOKES a
# superseded token (unified-home one-active-session-per-client). A cheap read-only GET here is the ONLY
# reliable liveness check: verified 2026-07-10 that a superseded seat's token returns 200 from the IdP
# `userinfo` AND has a future JWT `exp`, yet returns 401 here. Expiry/refresh checks alone are false-healthy.
CODEX_BACKEND_PROBE_URL = "https://chatgpt.com/backend-api/me"


def _codex_token_path(acct):
    """The 0600 file a launch reads via $(cat): JUST the access token."""
    return os.path.join(STATE, "providers", f"codex-{acct}.token")


def _codex_cred_path(acct):
    """The 0600 credential store: {access_token, refresh_token, expires_at, account_id}. Holds the
    refresh_token the fleet owns; separate from the .token file so a launch's $(cat) never sees it."""
    return os.path.join(STATE, "providers", f"codex-{acct}.cred.json")


def _atomic_write_secret(path, data):
    """Write-temp-then-rename at 0600 (refinement 1: a concurrent spawn-time $(cat) can never read a
    half-written token). mkstemp is 0600; os.replace is atomic within a filesystem."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _jwt_exp(token):
    """The `exp` (unix seconds) from a JWT access token, or None."""
    import base64
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("exp")
    except Exception:
        return None


def codex_seed_from_authjson(acct, authjson_path):
    """Seed the fleet cred store for `acct` from a `codex login` auth.json (the ONE interactive step per
    account). Extracts {access_token, refresh_token, account_id, expires_at} and writes both the cred store
    and the launch .token file (atomically, 0600). Returns the cred dict. Raises ProviderError on a bad file."""
    try:
        d = json.load(open(os.path.expanduser(authjson_path)))
        t = d.get("tokens") or {}
        at, rt = t.get("access_token"), t.get("refresh_token")
        if not at or not rt:
            raise ValueError("auth.json has no access_token/refresh_token")
    except Exception as e:
        raise ProviderError(f"codex seed for '{acct}': cannot read {authjson_path} ({e})")
    # capture identity from the id_token now (env-token accounts are NOT ~/.codex/auth.json, so the poller
    # can't read it later). Best-effort.
    ident = {}
    try:
        import base64
        p = (t.get("id_token") or "").split(".")[1]
        p += "=" * (-len(p) % 4)
        c = json.loads(base64.urlsafe_b64decode(p))
        ident = {"email": c.get("email"), "display": c.get("email"),
                 "plan": (c.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")}
    except Exception:
        pass
    cred = {"access_token": at, "refresh_token": rt, "account_id": t.get("account_id"),
            "expires_at": _jwt_exp(at), "identity": ident}
    _atomic_write_secret(_codex_cred_path(acct), json.dumps(cred))
    _atomic_write_secret(_codex_token_path(acct), at)
    return cred


def _codex_token_identity(acct):
    """Identity for an env-token codex account, from the fleet cred store (seeded at login). {} if absent."""
    try:
        return json.load(open(_codex_cred_path(acct))).get("identity") or {}
    except Exception:
        return {}


# --- account health monitor (built on the refresh loop) ------------------------------------------
# The distinction Berg cares about, do NOT conflate:
#   "usage stale"  = no recent CLI activity. The account is FINE. NEVER an alert (it's a poller/rollout
#                    concept, tracked separately by poll_codex's `stale`).
#   "token dead"   = the refresh_token is revoked/expired, so the account cannot be refreshed and will go
#                    OFFLINE. Needs a human re-login. THIS is the only "account offline" signal to notify.
def codex_probe_backend(access_token, timeout=15):
    """READ-ONLY liveness validation of a codex access token against the ChatGPT backend — the layer codex
    actually calls, and the layer that REVOKES a superseded token. Returns one of:
      'live'        — the backend accepted the token (HTTP < 400).
      'revoked'     — the backend REJECTED it (HTTP 401/403). This is the signal the clock cannot see: a
                      token superseded by a later `codex login` on the shared codex OAuth client keeps a
                      FUTURE JWT `exp` and still passes the IdP `userinfo`, yet the backend 401s it
                      (unified-home one-active-session-per-client; verified 2026-07-10).
      'unreachable' — network error / timeout / 5xx: TRANSIENT, never cry wolf.
    No refresh, no mint, no token spend — a pure GET. (Kept separate from _oauth_refresh, which MUTATES.)"""
    import urllib.request, urllib.error, socket
    req = urllib.request.Request(CODEX_BACKEND_PROBE_URL,
                                 headers={"Authorization": f"Bearer {access_token}",
                                          "User-Agent": "cmux-fleet-health"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "live" if r.status < 400 else "unreachable"
    except urllib.error.HTTPError as e:
        return "revoked" if e.code in (401, 403) else "unreachable"   # 5xx/other = transient, not offline
    except (urllib.error.URLError, socket.timeout, OSError):
        return "unreachable"


def codex_health_check():
    """Check each configured codex-token account's TOKEN health (not its usage). Two layers, because the
    clock alone is FALSE-HEALTHY for a superseded token (future `exp`, but the backend has revoked it):
      1. codex_ensure_fresh — silent refresh only if near expiry (a not-near-expiry token costs no network);
         a dead refresh_token surfaces here as 'revoked'.
      2. codex_probe_backend — a read-only GET against the ChatGPT backend that catches a token the clock
         and refresh_token both still consider valid but the backend has revoked (the one-active-session
         supersession; see CODEX_BACKEND_PROBE_URL). This is the gap the expiry-only check missed.
    Classifies: 'healthy' | 'revoked' (needs re-login) | 'unseeded' (never set up — not an alert) | 'error'
    (transient, no alert). Returns [{acct, email, status, detail, checked_at}]. Never raises."""
    out = []
    for tool, name, spec, _ in iter_providers():
        method, _m = _parse_auth(spec.get("auth", ""))
        if tool != "codex" or method != "codex-token":
            continue
        rec = {"acct": name, "email": (_codex_token_identity(name) or {}).get("email"),
               "checked_at": int(time.time())}
        try:
            tok = codex_ensure_fresh(name)
            probe = codex_probe_backend(tok)         # layer 2: server truth, catches supersession
            if probe == "revoked":
                rec["status"] = "revoked"
                rec["detail"] = ("token rejected by the ChatGPT backend (superseded / server-revoked "
                                 "despite a future expiry — needs a re-login)")
            else:                                    # 'live' or 'unreachable' — a transient probe never cries wolf
                rec["status"] = "healthy"
                rec["detail"] = "" if probe == "live" else "backend probe unreachable (expiry OK; unverified)"
        except ProviderTransientError as e:          # network blip / 5xx — NOT offline; retry next tick, no alert
            rec["status"], rec["detail"] = "error", str(e)
        except ProviderError as e:                   # a genuine rejection (revoked) or an unseeded account
            msg = str(e)
            rec["status"] = "unseeded" if "not seeded" in msg else "revoked"
            rec["detail"] = msg
        except Exception as e:                        # unknown — never cry wolf
            rec["status"], rec["detail"] = "error", f"{type(e).__name__}"
        out.append(rec)
    return out


def codex_health_scan(notify=None):
    """Run codex_health_check and EDGE-TRIGGER `notify(acct, email, message)` only when an account NEWLY
    transitions into 'revoked' (was not revoked last scan). Recovery re-arms it; a still-revoked account is
    not re-alerted every hour. 'unseeded'/'healthy'/'error' never alert. Persists per-account status.
    Returns the health list. Never raises."""
    from . import state as fs
    prev = fs.codex_health_read() or {}
    cur = codex_health_check()
    new_state = {}
    for r in cur:
        acct = r["acct"]
        was = (prev.get(acct) or {}).get("status")
        new_state[acct] = {"status": r["status"], "email": r.get("email"), "checked_at": r["checked_at"]}
        if r["status"] == "revoked" and was != "revoked" and notify:
            em = r.get("email") or "unknown account"
            try:
                notify(acct, r.get("email"),
                       f"codex account '{acct}' ({em}) needs a re-login — run `codex login` for it, "
                       f"then `fleet codex-setup {acct}`.")
            except Exception:
                pass                                 # a notify failure must not sink the scan/persist
    fs.codex_health_write(new_state)
    return cur


def _codex_notify(acct, email, message):
    """Surfaceless desktop banner (reaches Berg regardless of focus) for a revoked codex account."""
    try:
        subprocess.run([CMUX, "notify", "--title", f"codex account '{acct}' needs re-login",
                        "--body", message], capture_output=True, timeout=10)
    except Exception:
        pass


# --- config.toml provisioning (fleet-managed, FENCED, idempotent — refinement 4) -----------------
CODEX_FENCE_BEGIN = "# >>> cmux-fleet managed (codex providers) — do not edit inside"
CODEX_FENCE_END = "# <<< cmux-fleet managed"


def _codex_provider_block(acct):
    return (f"[model_providers.{acct}]\n"
            f'name = "OpenAI"\n'
            f'base_url = "https://chatgpt.com/backend-api/codex"\n'
            f'env_key = "{CODEX_TOKEN_ENV}"\n'
            f'wire_api = "responses"\n'
            f"requires_openai_auth = false\n")


def codex_provision_config(acct, config_path="~/.codex/config.toml"):
    """Idempotently add a `[model_providers.<acct>]` block to ~/.codex/config.toml, INSIDE a fleet-owned
    fence so Berg's hand-written config is never clobbered (refinement 4). Re-running is a no-op-ish merge
    (the fence is regenerated from the union of managed accounts). Refuses if the same block already exists
    OUTSIDE the fence (a manual definition) rather than create a duplicate. Returns the config path."""
    path = os.path.expanduser(config_path)
    text = open(path).read() if os.path.exists(path) else ""
    b, e = text.find(CODEX_FENCE_BEGIN), text.find(CODEX_FENCE_END)
    managed = set()
    outside = text
    if b != -1 and e != -1 and e > b:
        managed = set(re.findall(r'\[model_providers\.([^\]]+)\]', text[b:e]))
        outside = (text[:b] + text[e + len(CODEX_FENCE_END):])
    if acct in set(re.findall(r'\[model_providers\.([^\]]+)\]', outside)):
        raise ProviderError(f"[model_providers.{acct}] already exists OUTSIDE the fleet fence in {path}; "
                            f"remove it or rename the provider (the fleet will not clobber your config).")
    managed.add(acct)
    fence = CODEX_FENCE_BEGIN + "\n" + "\n".join(_codex_provider_block(a) for a in sorted(managed)) + CODEX_FENCE_END + "\n"
    body = outside.strip()
    new = (body + "\n\n" + fence) if body else fence
    _atomic_write_secret(path, new)                  # ~/.codex/config.toml is 0600 already
    return path


def _oauth_refresh(refresh_token):
    """POST the OAuth refresh. Returns {access_token, refresh_token, expires_at}. CRITICAL for the health
    monitor's no-cry-wolf guarantee: distinguish a genuine REJECTION from a TRANSIENT failure.
      - HTTP 400/401/403 (the endpoint rejected the refresh_token = invalid_grant) -> ProviderError (REVOKED).
      - any other HTTP (5xx), a URLError/timeout/socket error, or a malformed/empty response -> a network
        blip or server issue, NOT an offline account -> ProviderTransientError (health maps to 'error').
    The response commonly ROTATES the refresh_token — the caller MUST persist the returned one (refinement 2)."""
    import urllib.request, urllib.error, urllib.parse, socket
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token", "client_id": CODEX_OAUTH_CLIENT_ID,
        "refresh_token": refresh_token, "scope": "openid profile email offline_access"}).encode()
    req = urllib.request.Request(CODEX_OAUTH_TOKEN_URL, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):                # the endpoint REJECTED the token -> genuinely revoked
            raise ProviderError(f"oauth refresh rejected (HTTP {e.code}); refresh_token revoked/invalid")
        raise ProviderTransientError(f"oauth refresh server error (HTTP {e.code})")   # 5xx etc. -> transient
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise ProviderTransientError(f"oauth refresh unreachable ({type(e).__name__})")   # network -> transient
    except (ValueError, json.JSONDecodeError) as e:
        raise ProviderTransientError(f"oauth refresh bad response ({type(e).__name__})")  # garbage -> transient
    at = d.get("access_token")
    if not at:
        raise ProviderTransientError("oauth refresh: response had no access_token")   # not a clear revocation
    return {"access_token": at,
            "refresh_token": d.get("refresh_token") or refresh_token,   # keep old only if not rotated
            "expires_at": _jwt_exp(at) or (int(time.time()) + int(d.get("expires_in") or 0))}


def codex_ensure_fresh(acct, force=False):
    """Pre-launch guard (refinement 3): ensure `acct`'s access token is valid, refreshing if it expires
    within the margin. On success the .token file holds a fresh token (atomically written). Raises
    ProviderError if the account is not seeded or the refresh fails (dead/revoked) — so a launch surfaces
    the break loudly and NEVER spawns into a broken account. Returns the fresh access token."""
    cpath = _codex_cred_path(acct)
    try:
        cred = json.load(open(cpath))
    except Exception:
        raise ProviderError(f"codex account '{acct}' is not seeded ({cpath} missing); run the one-time "
                            f"`codex login` for it, then seed the fleet cred store.")
    exp = cred.get("expires_at") or 0
    if not force and exp and exp - time.time() > CODEX_REFRESH_MARGIN_S:
        # still valid; make sure the .token file matches the cred (idempotent, cheap)
        _atomic_write_secret(_codex_token_path(acct), cred["access_token"])
        return cred["access_token"]
    try:
        new = _oauth_refresh(cred["refresh_token"])
    except ProviderError:
        raise                                        # ProviderError (revoked) / ProviderTransientError both propagate typed
    except Exception as e:
        raise ProviderTransientError(f"codex account '{acct}' refresh failed unexpectedly ({type(e).__name__})")
    cred.update(new)                                 # persist the ROTATED refresh_token + new expiry
    _atomic_write_secret(cpath, json.dumps(cred))
    _atomic_write_secret(_codex_token_path(acct), cred["access_token"])
    return cred["access_token"]


def _resolve_codex(base, acct, method, arg):
    """Codex per-launch account selection. Two methods (verdict settled 2026-07-07; env-token validated
    live 2026-07-10, so it is now the PREFERRED path, not a stub):

      codex-token:<file>  (PREFERRED, unified home, no session/config split) — inject the account's ChatGPT
          OAuth token into `CODEX_TOKEN_ENV` at spawn (spawn-time $(cat), secret-safe) and select the account
          with `-c model_provider=<acct>`. Requires a one-time `[model_providers.<acct>]` block in
          ~/.codex/config.toml (name="OpenAI", base_url="https://chatgpt.com/backend-api/codex",
          env_key="CMUX_FLEET_CODEX_TOKEN", wire_api="responses", requires_openai_auth=false). The token file
          is fleet-owned and kept fresh by the refresh loop (SEPARATE, not yet wired — this resolver only
          READS the file; a stale token fails the launch loudly at codex, it does not silently fall back).

      codex-home:<path>   (FALLBACK/opt-in, self-refreshing, splits sessions per home) — select the account
          by pointing CODEX_HOME at its own config dir + auth.json.
    """
    if method == "codex-token":
        path = _token_path(arg)
        if not os.path.exists(path):
            raise ProviderError(f"codex provider '{acct}': token file not found: {path} "
                                f"(the fleet-owned token; provision + refresh it there)")
        base["raw_env"][CODEX_TOKEN_ENV] = f'"$(cat {shlex.quote(path)})"'
        base["args"] = ["-c", f"model_provider={acct}"]
        base["needs_refresh"] = acct                 # cmd_launch calls codex_ensure_fresh(acct) before spawn
        base["note"] = (f"codex account '{acct}' via env-token (unified home; needs "
                        f"[model_providers.{acct}] in ~/.codex/config.toml with env_key={CODEX_TOKEN_ENV})")
        return base
    if method == "codex-home":
        home = os.path.expanduser(arg)
        if os.path.realpath(home) == os.path.realpath(os.path.expanduser("~/.codex")):
            return base                              # default home = current account, no injection needed
        if not os.path.exists(os.path.join(home, "auth.json")):
            raise ProviderError(f"codex provider '{acct}' home has no auth.json: {home} "
                                f"(log it in once with `CODEX_HOME={home} codex login`)")
        base["env"]["CODEX_HOME"] = home
        base["note"] = f"codex account '{acct}' via CODEX_HOME={home} (fallback; splits sessions)"
        return base
    raise ProviderError(f"codex provider '{acct}' auth method '{method}' unsupported; use "
                        f"codex-token:<file> (preferred) or codex-home:<path> (fallback)")


def _read_env_file(path):
    """Parse KEY=VALUE lines from an env-file (vertex). Comments/blank lines skipped. Non-secret by design."""
    env = {}
    try:
        for line in open(os.path.expanduser(path)):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


# --- pollers (write no secrets; never raise — the daemon loop must survive any provider) ---------
def _read_oauth_token(auth):
    """Read a claude OAuth access token for POLLING. keychain:<svc> → the current account's accessToken
    (non-interactive `security` read, validated); file:<path> → the long-lived setup-token. None on failure."""
    method, arg = _parse_auth(auth)
    if method == "keychain":
        try:
            out = subprocess.run(["security", "find-generic-password", "-s", arg, "-w"],
                                 capture_output=True, text=True, timeout=10)
            blob = json.loads(out.stdout.strip())
            oa = blob.get("claudeAiOauth") or blob
            return oa.get("accessToken") or oa.get("access_token")
        except Exception:
            return None
    if method == "file":
        try:
            return open(_token_path(arg)).read().strip() or None
        except OSError:
            return None
    return None


def _iso_to_epoch(s):
    try:
        import datetime
        return int(datetime.datetime.fromisoformat(str(s)).timestamp())
    except Exception:
        return None


def _claude_identity(tok):
    """The REAL account behind a token (email + display name) via /api/oauth/account — token-authed, so it
    works for the keychain default AND an injected file token. Best-effort; {} on any failure."""
    import urllib.request
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            "https://api.anthropic.com/api/oauth/account",
            headers={"Authorization": f"Bearer {tok}", **_OAUTH_HEADERS}), timeout=15)
        d = json.loads(r.read().decode())
        return {"email": d.get("email_address"), "display": d.get("display_name") or d.get("full_name")}
    except Exception:
        return {}


def poll_claude(auth):
    """GET /api/oauth/usage with the account's token → normalized windows/scoped/extra_usage + the real
    account identity. Returns a result dict with ok/error; never raises."""
    import urllib.request, urllib.error
    tok = _read_oauth_token(auth)
    if not tok:
        return {"ok": False, "error": "no token (keychain locked or token file missing)"}
    req = urllib.request.Request(USAGE_ENDPOINT, headers={"Authorization": f"Bearer {tok}", **_OAUTH_HEADERS})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}"}
    fh, sd = data.get("five_hour") or {}, data.get("seven_day") or {}
    scoped = []
    active = ""
    for lim in (data.get("limits") or []):
        if lim.get("is_active"):
            active = lim.get("kind", "")
        if lim.get("kind") == "weekly_scoped":
            model = ((lim.get("scope") or {}).get("model") or {}).get("display_name") or "scoped"
            scoped.append({"label": model, "pct": lim.get("percent"), "resets_at": _iso_to_epoch(lim.get("resets_at"))})
    xu = data.get("extra_usage") or {}
    return {
        "ok": True, "error": None,
        "identity": _claude_identity(tok),
        "windows": {
            "five_hour": {"pct": fh.get("utilization"), "resets_at": _iso_to_epoch(fh.get("resets_at")),
                          "window_minutes": 300},
            "seven_day": {"pct": sd.get("utilization"), "resets_at": _iso_to_epoch(sd.get("resets_at")),
                          "window_minutes": 10080},
        },
        "plan": data.get("plan") or "",
        "scoped": scoped,
        "extra_usage": {"enabled": bool(xu.get("is_enabled")), "pct": xu.get("utilization")},
        "active_limit": active,
    }


def _codex_identity(home):
    """The REAL codex account (email + plan) from the home's auth.json id_token. Best-effort; {} on failure."""
    import base64
    try:
        d = json.load(open(os.path.join(os.path.expanduser(home), "auth.json")))
        idt = (d.get("tokens") or {}).get("id_token", "")
        p = idt.split(".")[1]
        p += "=" * (-len(p) % 4)
        c = json.loads(base64.urlsafe_b64decode(p))
        return {"email": c.get("email"), "display": c.get("email"),
                "plan": (c.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")}
    except Exception:
        return {}


def _newest_rollout(home, model_provider=None):
    """Newest rollout file in `home`. With `model_provider` set (unified-home attribution), the newest
    rollout TAGGED with that provider — the home interleaves every account's rollouts, so the globally
    newest may belong to a different account. Scans newest-first and returns the first match."""
    paths = glob.glob(os.path.join(os.path.expanduser(home), "sessions", "*", "*", "*", "rollout-*.jsonl"))
    if not paths:
        return None
    if not model_provider:
        return max(paths, key=os.path.getmtime)
    for p in sorted(paths, key=os.path.getmtime, reverse=True):
        try:
            txt = open(p, encoding="utf-8").read()
        except OSError:
            continue
        if f'"model_provider": "{model_provider}"' in txt or f'"model_provider":"{model_provider}"' in txt:
            return p
    return None


def _find_rate_limits(obj):
    """Depth-first find the {primary, secondary} rate-limit payload inside a codex rollout event."""
    if isinstance(obj, dict):
        if "primary" in obj and "secondary" in obj:
            return obj
        for v in obj.values():
            r = _find_rate_limits(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_rate_limits(v)
            if r:
                return r
    return None


_WINDOW_LABELS = {300: "five_hour", 10080: "seven_day", 43200: "thirty_day"}


def _window_label(minutes):
    """Canonical window name from its LENGTH, never from its slot. Codex's `primary`/`secondary` slots are
    plan-dependent: a Team plan sends primary=300min (5h) + secondary=10080min (7d), but a Free plan sends
    primary=43200min (30d) and secondary=null. Keying off the slot mislabels a 30-day window as "5h"."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return None
    if m in _WINDOW_LABELS:
        return _WINDOW_LABELS[m]
    if m % 1440 == 0:
        return f"{m // 1440}day"
    if m % 60 == 0:
        return f"{m // 60}hour"
    return f"{m}min"


def poll_codex(home, model_provider=None):
    """Newest rollout's last rate_limits event → normalized windows (zero-auth, file-only). Windows are
    labelled by `window_minutes` (5h / 7day / 30day / …), NOT by their primary/secondary slot, because the
    slot meaning varies by plan. `model_provider` filters to one account's rollouts in a unified home.
    Marks `stale` if the newest rollout is old. Never raises."""
    path = _newest_rollout(home, model_provider)
    if not path:
        which = f" for model_provider={model_provider}" if model_provider else ""
        return {"ok": False, "error": f"no rollout sessions found{which} in this CODEX_HOME"}
    rl = None
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
        for ln in reversed(lines):
            if "rate_limit" not in ln:
                continue
            try:
                rl = _find_rate_limits(json.loads(ln))
            except Exception:
                rl = None
            if rl:
                break
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}"}
    if not rl:
        return {"ok": False, "error": "no rate_limits event in newest rollout"}
    windows = {}
    for slot in ("primary", "secondary"):
        w = rl.get(slot)
        if not isinstance(w, dict):              # a plan may send only one window (Free: secondary=null)
            continue
        label = _window_label(w.get("window_minutes")) or slot
        windows[label] = {"pct": w.get("used_percent"), "resets_at": w.get("resets_at"),
                          "window_minutes": w.get("window_minutes")}
    stale = (time.time() - os.path.getmtime(path)) > CODEX_STALE_S
    ident = _codex_identity(home)
    return {
        "ok": True, "error": None, "stale": stale, "windows": windows,
        "identity": ident,
        "plan": rl.get("plan_type") or ident.get("plan") or "",
        "scoped": [], "extra_usage": {"enabled": False, "pct": None}, "active_limit": "",
    }


# --- server-side codex usage (the productized path; replaces the rollout scrape) ----------------
# The ChatGPT backend exposes a usage endpoint the seat's OAuth access token can hit directly, server-side
# (the codex analog to Claude's /api/oauth/usage). Verified 2026-07-10 against a live seat token. Strictly
# richer than the rollout scrape: a LIVE reset countdown (no resets_at math, never a stale snapshot),
# email + plan in the same call, and the metered/hard-cap signals (credits, spend_control, limit_reached)
# the "4 limit types" taxonomy needs beyond the % bars. Terminal-independent: no agent need have RUN.
CODEX_USAGE_ENDPOINT = "https://chatgpt.com/backend-api/codex/usage"


def _normalize_codex_usage(data):
    """Map the /codex/usage JSON into the shared poll record (windows keyed by LENGTH via _window_label, so
    the accessor renders codex identically to claude). Captures the metered/spend signals too."""
    rl = data.get("rate_limit") or {}
    windows = {}
    for slot in ("primary_window", "secondary_window"):
        w = rl.get(slot)
        if not isinstance(w, dict):
            continue
        secs = w.get("limit_window_seconds")
        mins = int(secs // 60) if isinstance(secs, (int, float)) else None
        # prefer the server's live countdown; fall back to reset_at if absent
        resets_at = w.get("reset_at")
        label = _window_label(mins) or slot.replace("_window", "")
        windows[label] = {"pct": w.get("used_percent"), "resets_at": resets_at, "window_minutes": mins}
    email = data.get("email")
    ident = {"email": email, "display": email, "plan": data.get("plan_type")}
    credits = data.get("credits") or {}
    spend = data.get("spend_control") or {}
    return {
        "ok": True, "error": None, "stale": False, "windows": windows, "identity": ident,
        "plan": data.get("plan_type") or "", "account_id": data.get("account_id"),
        "scoped": [], "extra_usage": {"enabled": False, "pct": None}, "active_limit": "",
        # metered / hard-cap signals (distinct from the % bars; the $-spend + quota caps in the taxonomy)
        "limit_reached": bool(rl.get("limit_reached")),
        "rate_limit_reached_type": rl.get("rate_limit_reached_type"),
        "credits": {"has_credits": bool(credits.get("has_credits")),
                    "overage_limit_reached": bool(credits.get("overage_limit_reached")),
                    "balance": credits.get("balance")},
        "spend_control": {"reached": bool(spend.get("reached")),
                          "individual_limit": spend.get("individual_limit")},
    }


def poll_codex_api(token, account_id=None):
    """Server-side codex usage via GET /backend-api/codex/usage with `Authorization: Bearer <access token>`
    (+ the `chatgpt-account-id` header when known; the token scopes the account either way). Returns the
    normalized poll record; never raises. A non-empty User-Agent is REQUIRED — the backend edge 403s an
    empty UA. A 401 means the token is invalidated/rotated (the unified-home clobber case); the caller falls
    back to the rollout scrape (keep-stale)."""
    import urllib.request, urllib.error
    if not token:
        return {"ok": False, "error": "no token"}
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json",
            "User-Agent": _OAUTH_HEADERS["User-Agent"]}
    if account_id:
        hdrs["chatgpt-account-id"] = account_id
    try:
        with urllib.request.urlopen(urllib.request.Request(CODEX_USAGE_ENDPOINT, headers=hdrs), timeout=20) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}"}
    return _normalize_codex_usage(data)


# --- poller registry (the pluggability seam) ----------------------------------------------------
# Adding a provider family (Vertex-metered, Gemini, a direct inference API with a usage endpoint) is:
#   1. write a poller  fn(spec, name) -> {ok, error?, windows{...}, plan?, extra_usage?, scoped?, budget?}
#   2. register_poller("<kind>", fn)
#   3. config: [providers.<tool>.<acct>] poller = "<kind>"   (defaults to the tool name)
# No change to poll_all, the state shape, or the paint accessor. `windows` is length-labelled
# (see _window_label), so any provider's window cadence renders through the same contract.
def _poll_claude_provider(spec, name):
    return poll_claude(spec["auth"])


def _poll_codex_provider(spec, name):
    method, arg = _parse_auth(spec["auth"])
    if method == "codex-token":
        # unified ~/.codex home: filter rollouts by this account's model_provider (= the provider name),
        # and take identity from the fleet cred store (env-token accounts are not ~/.codex/auth.json).
        r = poll_codex("~/.codex", model_provider=name)
        r["identity"] = _codex_token_identity(name)
        return r
    return poll_codex(arg or "~/.codex")             # codex-home: its own home, no filter


_POLLERS = {"claude": _poll_claude_provider, "codex": _poll_codex_provider}


def register_poller(kind, fn):
    """Register a usage poller for a provider `kind` (extension point for Vertex/Gemini/API/…)."""
    _POLLERS[kind] = fn


def poll_all():
    """Poll every configured TRACKED provider and persist the snapshot to provider-usage.json. Called by
    the daemon timer. Returns the written dict. Never raises per-provider (one bad token doesn't sink the
    rest). The poller is chosen from the registry by `spec.poller` (default = the tool name); a provider
    with `track = none` (e.g. vertex) or no registered poller is skipped."""
    from . import state as fs
    out = {}
    for tool, name, spec, is_default in iter_providers():
        if spec.get("track") == "none":
            continue
        poller = _POLLERS.get(spec.get("poller") or tool)
        if poller is None:
            continue                                 # trackless / no poller for this kind — nothing to record
        rec = {"tool": tool, "name": name, "type": spec["type"], "is_default": is_default}
        try:
            rec.update(poller(spec, name))
        except Exception as e:                       # defensive: a provider must never sink poll_all
            rec.update({"ok": False, "error": f"{type(e).__name__}"})
        rec["checked_at"] = int(time.time())
        out[f"{tool}:{name}"] = rec
    fs.provider_usage_write(out)
    return out


# --- the paint accessor: the STABLE render contract (usage-ops -> fleet paint -> sidebar) --------
PAINT_SCHEMA = 1

_WIN_PRETTY = {"five_hour": "5h", "seven_day": "7d", "thirty_day": "30d"}


def _paint_windows(rec):
    """Normalize one poll record's windows into an ORDERED, render-ready list (shortest window first),
    provider-agnostic: a provider with one 30-day window, two (5h+weekly), or none all produce the same
    shape. `binding` marks the currently-limiting window (from active_limit). Scoped limits (e.g. Fable)
    are appended, flagged scoped=true."""
    active = str(rec.get("active_limit", ""))
    out = []
    for key, w in (rec.get("windows") or {}).items():
        mins = w.get("window_minutes")
        binding = (key == "five_hour" and active == "session") or (key == "seven_day" and active.startswith("weekly"))
        out.append({"key": key, "label": _WIN_PRETTY.get(key, key), "pct": w.get("pct"),
                    "resets_at": w.get("resets_at"), "resets_in_s": _resets_in(w.get("resets_at")),
                    "window_minutes": mins, "binding": bool(binding), "scoped": False})
    out.sort(key=lambda d: d["window_minutes"] or 0)
    for sc in (rec.get("scoped") or []):
        out.append({"key": "scoped", "label": sc.get("label", "scoped"), "pct": sc.get("pct"),
                    "resets_at": sc.get("resets_at"), "resets_in_s": _resets_in(sc.get("resets_at")),
                    "window_minutes": None, "binding": False, "scoped": True})
    return out


def _resets_in(epoch):
    if not epoch:
        return None
    return max(0, int(epoch) - int(time.time()))


def _headline(windows):
    """The single most-constrained window (highest %) — the one-glance figure for a compact panel."""
    ranked = [w for w in windows if isinstance(w.get("pct"), (int, float))]
    if not ranked:
        return None
    w = max(ranked, key=lambda d: d["pct"])
    return {"key": w["key"], "label": w["label"], "pct": w["pct"], "resets_in_s": w["resets_in_s"]}


# The provider BADGE (a small source chip in the sidebar) and the SUBSCRIPTION grouping key (§3). A badge
# is per-tool; the subscription key groups SEATS that share one bill. Codex seats on the same ChatGPT
# subscription share `account_id` (verified: sean-flat + berglabs both = 77cd2846); claude has one account
# per config entry, so it groups by its own id. api-key providers (Gemini) have no seats — each is its own
# group. Kept provider-agnostic: no subscription-only assumption hardcoded.
_TOOL_BADGE = {"claude": "Claude Code", "codex": "Codex", "gemini": "Gemini"}


def _provider_badge(tool):
    return _TOOL_BADGE.get(tool, tool.title() if tool else "")


def _subscription_key(rec):
    """The grouping key: `account_id` when the provider reports one (codex seats share it per subscription),
    else the provider's own `tool:account` (claude, api-key) so it forms a singleton group."""
    return rec.get("account_id") or f"{rec.get('tool', '')}:{rec.get('name', '')}"


def usage_for_paint():
    """STABLE, versioned, render-ready view of the last usage poll — THE contract that `fleet paint` (and
    the sidebar behind it) consume. Decoupled from the raw poll record so the poller can evolve without
    breaking the sidebar. Provider-agnostic by construction: `kind` says how to read a row (subscription
    has `windows`; api has `budget`; vertex has neither), and `windows` is an ordered list, not fixed keys.

      { "schema": 1, "generated_at": <epoch>,
        "providers": [ {
          "id": "claude:berg-max", "tool": "claude", "account": "berg-max",
          "kind": "subscription", "plan": "max", "is_default": true,
          "ok": true, "error": null, "stale": false, "checked_at": <epoch>, "age_s": 36,
          "windows": [ {"key":"five_hour","label":"5h","pct":19.0,"resets_at":<epoch>,
                        "resets_in_s":3000,"window_minutes":300,"binding":true,"scoped":false}, … ],
          "headline": {"key":"five_hour","label":"5h","pct":19.0,"resets_in_s":3000},
          "budget": null } ] }

    Never raises: an empty/absent snapshot returns {schema, generated_at, providers: []}."""
    from . import state as fs
    snap = fs.provider_usage_read() or {}
    provs = []
    for pid in sorted(snap):
        r = snap[pid]
        windows = _paint_windows(r) if r.get("ok") else []
        ca = r.get("checked_at")
        ident = r.get("identity") or {}
        # `account` = config id (stable key, e.g. "berg-max"); `identity`/`label` = the REAL account for
        # display (email + name), so the sidebar shows "Berg (seanyoungberg@gmail.com)" not "berg-max".
        label = ident.get("display") or ident.get("email") or r.get("name", "")
        provs.append({
            "id": pid, "tool": r.get("tool", ""), "account": r.get("name", ""),
            "badge": _provider_badge(r.get("tool", "")),        # §3: source chip (Claude Code / Codex / …)
            "subscription": _subscription_key(r),               # §3: group seats sharing one bill
            "identity": {"email": ident.get("email"), "display": ident.get("display")},
            "label": label,
            "kind": r.get("type", ""), "plan": r.get("plan", ""),
            "is_default": bool(r.get("is_default")),
            "ok": bool(r.get("ok")), "error": r.get("error"), "stale": bool(r.get("stale")),
            "checked_at": ca, "age_s": (int(time.time()) - int(ca)) if ca else None,
            "windows": windows, "headline": _headline(windows), "budget": r.get("budget"),
            # §2 keep-stale-grayed: hard-cap signal + the metered-spend cap distinct from the % bars
            "limit_reached": bool(r.get("limit_reached")),
        })
    return {"schema": PAINT_SCHEMA, "generated_at": int(time.time()), "providers": provs}
