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
import shlex
import subprocess
import time

try:
    import tomllib
except ModuleNotFoundError:                      # py<3.11 — feature is a no-op without a toml reader
    tomllib = None

from .config import FLEET_TOML, STATE

USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
_OAUTH_HEADERS = {"anthropic-beta": "oauth-2025-04-20", "anthropic-version": "2023-06-01",
                  "User-Agent": "cmux-fleet-usage-poller"}
CODEX_STALE_S = 3600            # a codex profile whose newest rollout is older than this = stale snapshot


class ProviderError(Exception):
    """A launch-time provider resolution failure (unknown provider, missing token file, bad type)."""


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
            provs[name] = {
                "type": str(spec.get("type", "subscription")),
                "auth": str(spec.get("auth", "")),
                "track": str(spec.get("track", "windows" if spec.get("type") == "subscription" else "none")),
            }
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
    base = {"label": label, "env": {}, "raw_env": {}, "provisional": False, "note": ""}

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
        return _resolve_codex(base, method, arg)
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


def _resolve_codex(base, method, arg):
    """STUB — codex account selection is UNSETTLED (verdict pending: CODEX_HOME-per-profile vs env-token).
    Keep this the ONLY place that decides the codex mechanism. Today it emits CODEX_HOME and marks the
    result PROVISIONAL so nothing downstream treats it as final. When the verdict lands, swap the body;
    resolve_launch()'s contract does not change."""
    if method != "codex-home":
        raise ProviderError(f"codex provider auth '{method}' not wired (selection verdict pending)")
    home = os.path.expanduser(arg)
    default_home = os.path.expanduser("~/.codex")
    if os.path.realpath(home) == os.path.realpath(default_home):
        return base                                  # default home = current account, no injection needed
    base["env"]["CODEX_HOME"] = home
    base["provisional"] = True
    base["note"] = ("codex account selection is PROVISIONAL (mechanism verdict pending); "
                    f"using CODEX_HOME={home}")
    return base


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


def poll_claude(auth):
    """GET /api/oauth/usage with the account's token → normalized windows/scoped/extra_usage. Returns a
    result dict with ok/error; never raises."""
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
        "windows": {
            "five_hour": {"pct": fh.get("utilization"), "resets_at": _iso_to_epoch(fh.get("resets_at"))},
            "seven_day": {"pct": sd.get("utilization"), "resets_at": _iso_to_epoch(sd.get("resets_at"))},
        },
        "scoped": scoped,
        "extra_usage": {"enabled": bool(xu.get("is_enabled")), "pct": xu.get("utilization")},
        "active_limit": active,
    }


def _newest_rollout(home):
    paths = glob.glob(os.path.join(os.path.expanduser(home), "sessions", "*", "*", "*", "rollout-*.jsonl"))
    return max(paths, key=os.path.getmtime) if paths else None


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


def poll_codex(home):
    """Newest rollout's last rate_limits event → normalized windows (zero-auth, file-only). primary =
    5h window, secondary = weekly (10080 min). Marks `stale` if the newest rollout is old. Never raises."""
    path = _newest_rollout(home)
    if not path:
        return {"ok": False, "error": "no rollout sessions found in this CODEX_HOME"}
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
    p, s = rl.get("primary") or {}, rl.get("secondary") or {}
    stale = (time.time() - os.path.getmtime(path)) > CODEX_STALE_S
    return {
        "ok": True, "error": None, "stale": stale,
        "windows": {
            "five_hour": {"pct": p.get("used_percent"), "resets_at": p.get("resets_at")},
            "seven_day": {"pct": s.get("used_percent"), "resets_at": s.get("resets_at")},
        },
        "scoped": [], "extra_usage": {"enabled": False, "pct": None}, "active_limit": "",
    }


def poll_all():
    """Poll every configured SUBSCRIPTION provider (track != none) and persist the snapshot to
    provider-usage.json. Called by the daemon timer. Returns the written dict. Never raises per-provider
    (one bad token doesn't sink the rest)."""
    from . import state as fs
    out = {}
    for tool, name, spec, is_default in iter_providers():
        if spec["type"] != "subscription" or spec.get("track") == "none":
            continue
        rec = {"tool": tool, "name": name, "type": spec["type"], "is_default": is_default}
        try:
            method, arg = _parse_auth(spec["auth"])
            if tool == "codex":
                rec.update(poll_codex(arg if method == "codex-home" else "~/.codex"))
            else:
                rec.update(poll_claude(spec["auth"]))
        except Exception as e:                       # defensive: a provider must never sink poll_all
            rec.update({"ok": False, "error": f"{type(e).__name__}"})
        rec["checked_at"] = int(time.time())
        out[f"{tool}:{name}"] = rec
    fs.provider_usage_write(out)
    return out
