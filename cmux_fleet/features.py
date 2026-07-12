#!/usr/bin/env python3
# cmux_fleet/features.py (was fleet_features.py) — the read-only VIEW layer over the live fleet: vitals / find / graph / serve,
# plus native sidebar telemetry (paint). Kept OUT of fleet.py (the lifecycle CLI) so the view code
# is a clean, dependency-light island: it imports fleet_state + config and NOTHING from fleet.py
# (no circular import). fleet.py just routes `vitals|find|graph|serve|paint` here.
#
# DESIGN: everything derives from live state every call — fleet_state's registry + cmux's per-agent
# hook stores + the agents' transcripts. No daemon, no stored status, no analytics. Status is inferred
# WITHOUT an LLM: cmux's agentLifecycle is authoritative, refined by cheap keyword tables (the
# agentmaster move). Context-remaining % is read straight from the transcript's token usage (Berg's
# fleet-management ask: see who is near-full and needs recycling).
import argparse
import html as _html
import json
import os
import re
import shlex
import subprocess
import sys
import time

from . import state as fs
from .config import CMUX, STATE

try:
    from .config import CONTEXT_WINDOW as _CFG_WINDOW         # optional override (env/[fleet])
except Exception:
    _CFG_WINDOW = 0

PAINT_STATE = os.path.join(STATE, "sidebar-paint.json")       # on-change fingerprint (avoid churn)


def _cmux(*args):
    """Run a cmux subcommand, return stdout. Quiet, never raises."""
    try:
        p = subprocess.run([CMUX, *args], capture_output=True, text=True,
                           env=dict(os.environ, CMUX_QUIET="1"))
        return p.stdout or ""
    except Exception:
        return ""


_UUID_RE = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_WS_MAP = {"at": 0.0, "map": {}}


def _surface_ws_map(ttl=2.0):
    """`{SURFACE_UUID: workspace_uuid}` derived from the LIVE cmux tree — the only never-stale source.

    Neither cached copy can be trusted here, and they go stale in opposite directions:
      - the hook store's `workspaceId` is written when a session starts and is NEVER updated when the
        surface MOVES, so `fleet move` silently leaves it pointing at the old workspace;
      - the registry's `workspace` is written by fleet's own verbs and goes stale when CMUX re-homes a
        surface (a reboot/restore), which is how three agents ended up naming dead workspaces.
    Symptom that forced this (2026-07-10, found by sidebar-build): `snapshot()` read the hook store, so
    two agents moved into their own workspaces both reported the conductor's workspace — collapsed onto
    one id. cmux's own sidebar shows the same blind spot for those two, being downstream of the same field.

    ONE `cmux tree` call per snapshot (not per agent), memoized for `ttl` seconds. Callers fall back to
    the cached fields when the tree can't be read, so this can never regress a working read."""
    now = time.time()
    if _WS_MAP["map"] and (now - _WS_MAP["at"]) < ttl:
        return _WS_MAP["map"]
    out = _cmux("tree", "--all", "--id-format", "both")
    mapping, ws = {}, ""
    for line in out.splitlines():
        mw = re.search(r"workspace\s+workspace:\d+\s+(" + _UUID_RE + ")", line)
        if mw:
            ws = mw.group(1)
            continue
        ms = re.search(r"surface\s+surface:\d+\s+(" + _UUID_RE + ")", line)
        if ms and ws:
            mapping[ms.group(1).upper()] = ws
    if mapping:
        _WS_MAP.update({"at": now, "map": mapping})
    return mapping or _WS_MAP["map"]


# ─── status inference (keyword tables, NO LLM) ────────────────────────────────────────────────
# cmux's agentLifecycle (idle|running|needsInput) is the authoritative base. The keyword tables only
# REFINE an idle agent — they tell apart "idle: finished cleanly", "idle: hit an error", "idle:
# wants review" — from the agent's own last transcript line. Substring match, lowercased, cheap.
ERROR_HINTS  = ("error:", "traceback", "exception", "fatal:", "panic:", "✗", "failed", "rate limit",
                "rate-limit", "usage limit", "context low", "compact")
BLOCK_HINTS  = ("[y/n]", "(y/n)", "approve?", "permission", "press enter", "waiting for", "shall i",
                "do you want", "should i", "confirm")
REVIEW_HINTS = ("diff --git", "opened pull request", "ready for review", "please review", "pr #")
DONE_HINTS   = ("✓ done", "all tests passed", "0 errors", "complete", "finished", "done.", "✅")

# state -> (sidebar pill color, SF-Symbol icon, urgency rank). Lower rank = more urgent = sorts first
# (the "NEED YOU floats to top" triage from agentmaster). cheapest-first = act on the cheap signal.
STATE_STYLE = {
    "error":       ("#E5484D", "exclamationmark.triangle.fill", 0),
    "needs-input": ("#F5A623", "hand.raised.fill",              1),
    "review":      ("#3E63DD", "eye.fill",                      2),
    # alive and working, but its cmux hook channel is dead — every TIME-based signal about it is a lie.
    # A WARNING, not an error: distinct from 'stale' (gone) and from 'working' (we can still hear it).
    # Violet, not amber, so it can never be misread as 'needs-input'. Remedy is a recycle (re-exports env).
    "detached":    ("#A45CDB", "antenna.radiowaves.left.and.right.slash", 3),
    "working":     ("#30A46C", "gearshape.fill",                4),
    "done":        ("#46A758", "checkmark.circle.fill",         5),
    "ready":       ("#3DB9A0", "circle.fill",                   6),   # teal presence dot — finished, available
    "idle":        ("#8B8D98", "moon.zzz.fill",                 7),   # asleep — only after QUIET_S of no activity
    "pending":     ("#8B8D98", "hourglass",                     8),
    "stale":       ("#6F6E77", "questionmark.circle",           9),
    "gone":        ("#6F6E77", "xmark.circle",                  9),
}

# A finished agent stays 'ready' (available, present) until it has been quiet this long, then reads 'idle'
# (dormant). The distinction is TIME-since-last-activity, not cmux's needsInput/idle strings (which don't
# encode "recently finished" vs "long dormant"). Keeps a just-finished agent from looking asleep.
QUIET_S = 900   # 15 min


def _freshest_session(store, surf):
    """The newest hook-store session record on a surface (transcriptPath/pid/model/sessionId/updatedAt/
    workspaceId). A surface can carry more than one record across a recycle; newest updatedAt wins."""
    best, best_ts = {}, -1.0
    for s in (store.get("sessions") or {}).values():
        if (s.get("surfaceId") or "").upper() == (surf or "").upper():
            ts = s.get("updatedAt") or 0
            if ts >= best_ts:
                best, best_ts = s, ts
    return best


# ─── per-agent context window (Fix 1: REAL per-agent, not a static global) ─────────────────────
# The window is knowable PER AGENT from the model it launched with. opus-4-8 ships in both a 200k and a
# 1M ([1m]) flavor, so the flavor — not a fleet-wide constant — is the truth. Precedence INVERTS the old
# one: a real per-agent value (flavor, then keyword) wins; the CMUX_FLEET_CONTEXT_WINDOW /
# [fleet].context_window override is DEMOTED to a manual last resort (only an unknown model reaches it).
def _flag_val(tokens, name):
    """Value of `--name V` (or `--name=V`) in a token list; True if a bare flag; else None. Local copy —
    features.py is a dependency-light island and must not import cli.py (no circular import)."""
    for i, t in enumerate(tokens):
        if t == name:
            return tokens[i + 1] if i + 1 < len(tokens) and not tokens[i + 1].startswith("-") else True
        if t.startswith(name + "="):
            return t.split("=", 1)[1]
    return None


def _launch_args(sess):
    """Launch argv tokens from a hook-store session record's launchCommand. cmux stores it either as a
    dict {'arguments':[...], 'launcher':...} (current builds) or a bare command string (older) — normalize
    to a flat token list for --flag scanning. Mirrors cli._launchcmd's dict/str tolerance."""
    lc = sess.get("launchCommand") if isinstance(sess, dict) else sess
    if isinstance(lc, dict):
        args = lc.get("arguments")
        if isinstance(args, list):
            return [str(a) for a in args]
        lc = lc.get("command") or ""
    if isinstance(lc, str) and lc:
        try:
            return shlex.split(lc)
        except ValueError:
            return lc.split()
    return []


def _launcher(sess):
    """The launching tool ('claude'/'codex'/...) from a session record's launchCommand dict, or ''."""
    lc = sess.get("launchCommand") if isinstance(sess, dict) else None
    return (lc.get("launcher") or "").lower() if isinstance(lc, dict) else ""


def _user_prefs():
    """The GLOBAL default (model, effort) from ~/.claude/settings.json / env — the values a claude agent
    launched WITHOUT a --model/--effort override inherits. This is where the window FLAVOR (e.g.
    'claude-opus-4-8[1m]') lives for the common no-override case: the launchCommand rarely carries it, so
    this default is ESSENTIAL to per-agent window resolution, not a nicety. Read fresh per call (cheap;
    vitals is already a live-derive). '' when unknown. (Mirrors cli.compute_effective's model/effort
    precedence: launch flag > settings > env.)"""
    model = effort = ""
    try:
        d = json.load(open(os.path.expanduser("~/.claude/settings.json")))
        model = d.get("model") or ""
        effort = d.get("effortLevel") or ""
    except Exception:
        pass
    return (model or os.environ.get("ANTHROPIC_MODEL", ""),
            effort or os.environ.get("CLAUDE_CODE_EFFORT_LEVEL", ""))


def _launched_prefs(sess, tool=""):
    """(model, effort) an agent EFFECTIVELY launched with, per Claude Code's own precedence: a
    --model/--effort launch flag (per-agent override) wins; else the global user default (settings/env).
    The model string is returned WITH any [Nk]/[Nm] window flavor — that's the whole point, the window is
    derived from it. The global default is applied only for CLAUDE agents (a codex agent must not inherit
    the claude settings model); codex ctx is '—' anyway (used=None), so its window is cosmetic."""
    args = _launch_args(sess)
    fmodel, feffort = _flag_val(args, "--model"), _flag_val(args, "--effort")
    is_claude = (tool or _launcher(sess) or "claude").lower() == "claude"
    umodel, ueffort = _user_prefs() if is_claude else ("", "")
    return (fmodel if isinstance(fmodel, str) else umodel,
            feffort if isinstance(feffort, str) else ueffort)


def _window_flavor(model):
    """The window a [Nk]/[Nm] suffix flavor on a model string encodes (case-insensitive): '[1m]'->1_000_000,
    '[200k]'->200_000, '[500000]'->500_000. None when the string carries no flavor. This is the
    launch-encoded TRUTH about the window (opus-4-8 ships in both 200k and 1M flavors)."""
    m = re.search(r"\[(\d+)\s*([km]?)\]", (model or "").lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n * 1_000_000 if unit == "m" else n * 1000 if unit == "k" else n


def _context_window(model):
    """Tokens of context for a model STRING. Precedence:
      1. an explicit [Nk]/[Nm] window flavor on the string (the launch-encoded truth: [1m]->1M) — the ONLY
         per-agent signal that reliably disambiguates opus-4-8's 200k vs 1M tier when it's present;
      2. else the CMUX_FLEET_CONTEXT_WINDOW / [fleet].context_window override — the fleet's DECLARED
         window. It sits ABOVE the keyword guess deliberately: a bare model string CANNOT disambiguate
         200k vs 1M (the [1m] flavor is usually absent from the launch — stripped, or an explicit bare
         `--model opus`/`claude-opus-4-8` that still runs 1M on this fleet), so a keyword guess of 200k
         produces FALSE "over-full, recycle-now" alarms for agents actually on 1M (confirmed live
         2026-07-04: cmux-advisor at 395k on a bare `--model claude-opus-4-8`, auto-compact off — a real
         200k window is impossible). The declared window is the least-wrong denominator absent a flavor;
      3. else a model-keyword map (opus/sonnet/haiku->200k, gpt-5/codex->272k, gemini->1M) — only for a
         model the operator never declared a window for;
      4. else 200k.
    NOTE: TRUE per-agent windows on a genuinely mixed fleet need the launched [1m] flavor preserved (or a
    real window signal) — see the vitals backlog. Absent that, flavor-or-declared-window is the honest floor."""
    flav = _window_flavor(model)
    if flav:
        return flav
    if _CFG_WINDOW:                                       # the fleet's DECLARED window — beats the keyword
        return int(_CFG_WINDOW)                           # guess (a bare model can't disambiguate 200k vs 1M)
    m = (model or "").lower()
    # ORDER IS LOAD-BEARING: first match wins, and every gpt-5.6 slug CONTAINS "gpt-5". Listed after it,
    # `gpt-5.6-sol` resolves to 272k — understating a 372k window by 100k and manufacturing false
    # "near-full, recycle now" alarms on the very model we moved to FOR its extra room. Windows are
    # codex's own, read out of the model registry embedded in the codex-cli 0.144.1 binary: every
    # gpt-5.6-* variant (sol/terra/luna) is 372_000; gpt-5.5 and the 5.4/5.2 line are 272_000.
    # NOTE this map is only the LAST-RESORT floor: a codex agent's REAL window comes from its rollout
    # (`info.model_context_window`, live-measured 353_400 for gpt-5.6-sol — the server's effective window,
    # which is smaller than the registry's nominal 372_000), and on a fleet that declares
    # [fleet].context_window the declared value outranks this map entirely.
    for key, win in (("haiku", 200000), ("sonnet", 200000), ("opus", 200000),
                     ("gpt-5.6", 372000), ("gpt-5", 272000), ("o3", 200000),
                     ("codex", 272000), ("gemini", 1000000)):
        if key in m:
            return win
    return 200000


def _context_used(path):
    """Approx context tokens occupied at the agent's last turn, from its transcript. claude records
    a per-turn usage block: input + cache_read + cache_creation = the whole prompt that turn = the live
    context size. codex's transcript only carries a CUMULATIVE session counter (not the live window), so
    we don't guess it — codex returns None and vitals shows '—'. Returns (tokens|None, model).

    Returns None (not 0) when NO REAL usage record is found — an errored/empty/truncated transcript, or a
    turn whose usage summed to 0 (Fix 3). A live agent's prompt is never 0 tokens, so a 0 total is "no
    parseable usage" not "genuinely 0"; requiring a POSITIVE total keeps used=None -> pct_remaining None
    -> vitals shows '—' instead of the garbage '0k 100%' (a 0 total made 1 - 0/window resolve to 100%)."""
    used, model = None, ""
    if not path or not os.path.exists(path):
        return None, ""
    try:
        for line in open(path):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") == "assistant":                         # claude only (see docstring)
                msg = e.get("message") or {}
                model = msg.get("model") or model
                u = msg.get("usage") or {}
                if u:
                    tot = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                           + u.get("cache_creation_input_tokens", 0))
                    if tot > 0:                                      # a REAL record; 0 == errored/empty turn
                        used = tot
    except Exception:
        return None, model
    return used, model


def _codex_rollout_stats(path):
    """Codex vitals from its rollout JSONL (the fleet records a codex agent's rollout as its transcriptPath,
    same as last_agent_text reads). ONE newest-wins pass yields what claude gets from its transcript but
    codex records elsewhere:
      • context — the last `event_msg/token_count`: info.last_token_usage.input_tokens is the live prompt
        occupancy (the full re-sent context that turn, cached tokens included — they still fill the window),
        over info.model_context_window (the model's REAL window, e.g. gpt-5.5 = 258400, more precise than a
        keyword guess). Mirrors claude's prompt-size approach; requires a POSITIVE count (a probe/empty
        rollout has no token_count -> used stays None -> vitals shows '—', exactly like claude).
      • model / effort — the last `turn_context`: the EFFECTIVE values codex ran with (effort defaults land
        here even when no --effort flag was passed; the field is `effort`, not `reasoning_effort`).
    Returns {used, window, model, effort} (None/'' when absent). Never raises."""
    out = {"used": None, "window": None, "model": "", "effort": ""}
    if not path or not os.path.exists(path):
        return out
    try:
        for line in open(path):
            try:
                e = json.loads(line)
            except Exception:
                continue
            t, pl = e.get("type"), (e.get("payload") or {})
            if t == "event_msg" and pl.get("type") == "token_count":
                info = pl.get("info") or {}
                inp = (info.get("last_token_usage") or {}).get("input_tokens")
                if isinstance(inp, int) and inp > 0:                 # 0/None == no real reading -> keep prior
                    out["used"] = inp
                win = info.get("model_context_window")
                if isinstance(win, int) and win > 0:
                    out["window"] = win
            elif t == "turn_context":
                out["model"] = pl.get("model") or out["model"]
                out["effort"] = pl.get("effort") or out["effort"]
    except Exception:
        return out
    return out


# The interactive tool calls that genuinely BLOCK a turn on the human. A trailing, unanswered one of
# these is the ONLY needsInput state the fleet-doctor should alert on (fleet-doctor #iii). Everything
# else at needsInput — a completed turn idling >~60s at the prompt (which cmux also stamps needsInput via
# Claude's idle Notification hook), the feedback survey, a max-tokens stop — is a done-idle NON-gate.
_INPUT_GATE_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})
_GATE_TAIL_BYTES = 262144   # read only the transcript tail: a turn boundary is always the last thing written
_TERMINAL_STOP = frozenset({"end_turn", "stop", "stop_sequence", "max_tokens"})   # the turn CLOSED here


def _last_assistant_turn(transcript_path):
    """(last_assistant_message, has_user_after) parsed from the transcript TAIL, or None on any ambiguity
    (absent/unreadable transcript, codex with no transcript, no assistant row). Shared by the gate and
    turn-end predicates so they read/parse the tail once each with identical, tested logic."""
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _GATE_TAIL_BYTES), 0)
            chunk = f.read().decode("utf-8", "ignore")
    except Exception:
        return None
    rows = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))          # a leading partial line just fails to parse -> skipped
        except Exception:
            continue
    last = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].get("type") == "assistant"), None)
    if last is None:
        return None
    has_user_after = any(r.get("type") == "user" for r in rows[last + 1:])   # a tool_result / new turn follows
    return (rows[last].get("message") or {}), has_user_after


def pending_interactive_gate(transcript_path):
    """True iff the transcript's LAST assistant turn ends on an UNANSWERED interactive gate
    (AskUserQuestion / ExitPlanMode) with nothing after it — the one 'agent is blocked on the human'
    state a needsInput lifecycle can mean. cmux stamps needsInput for BOTH a real gate AND an ordinary
    done-idle turn, so the lifecycle string can't tell them apart, but the transcript can — a done-idle
    turn ends with stop_reason=end_turn, a gate ends on the tool_use. FAILS CLOSED to False on any
    ambiguity, so the predicate SUPPRESSES rather than alerts when it can't prove a gate."""
    parsed = _last_assistant_turn(transcript_path)
    if not parsed:
        return False
    msg, has_user_after = parsed
    if has_user_after:                                         # answered (tool_result / new user turn) -> not pending
        return False
    if msg.get("stop_reason") not in ("tool_use", None):       # end_turn / max_tokens / stop -> done-idle
        return False
    return any(isinstance(c, dict) and c.get("type") == "tool_use"
               and c.get("name") in _INPUT_GATE_TOOLS
               for c in (msg.get("content") or []))


def _codex_turn_ended(path):
    """True iff a CODEX rollout's latest turn CLOSED. Codex fires no SessionEnd, so a finished codex agent
    otherwise reads 'working' forever (and a plain `fleet rm` refuses it); its rollout's `task_complete`
    event is the done signal. A turn is task_started -> ... -> task_complete, so the turn is ended iff the
    LAST turn-boundary event is task_complete with no task_started/user_message after it. FAILS CLOSED to
    False (absent/unreadable/no boundary/mid-turn), matching turn_ended's only-ever-CLEAR contract."""
    if not path or not os.path.exists(path):
        return False
    ended = None
    try:
        for line in open(path):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") != "event_msg":
                continue
            pt = (e.get("payload") or {}).get("type")
            if pt == "task_complete":
                ended = True
            elif pt in ("task_started", "user_message"):       # a new turn opened after the last complete
                ended = False
    except Exception:
        return False
    return ended is True


def turn_ended(transcript_path):
    """True iff the transcript's LAST turn CLOSED — a REAL-TIME turn-completion signal. cmux's agentLifecycle
    keeps reading 'running' for a while after a turn ends (its idle timer lags, and codex can stick there
    since it fires no SessionEnd), so a just-finished agent shows 'working' far too long; the transcript
    flips the instant the turn closes. Two dialects: claude = a terminal stop_reason with nothing after it;
    codex = a trailing task_complete (see _codex_turn_ended). FAILS CLOSED to False (absent/unreadable/
    mid-turn/gate) so an unprovable case keeps whatever cmux's lifecycle says — this only ever CLEARS a
    lagged 'working'."""
    parsed = _last_assistant_turn(transcript_path)
    if not parsed:                                             # no claude assistant rows -> try the codex dialect
        return _codex_turn_ended(transcript_path)
    msg, has_user_after = parsed
    if has_user_after:                                         # a tool_result / new turn follows -> not ended
        return False
    return msg.get("stop_reason") in _TERMINAL_STOP


def _refine(last_text, default):
    """Keyword-refine a not-actively-working agent from its last transcript line. Returns error / review /
    done, else `default`. NOTE: no longer emits 'needs-input' — a real block is now signalled ONLY by an
    open Feed gate (see _classify), so stale block-phrases in the transcript can't raise a false alarm."""
    text = (last_text or "").lower()
    if any(h in text for h in ERROR_HINTS):
        return "error"
    if any(h in text for h in REVIEW_HINTS):
        return "review"
    if any(h in text for h in DONE_HINTS):
        return "done"
    return default


def _classify(life, has_session, last_text, open_gate=False, turn_done=False, quiet=False):
    """PURE state classifier, NO LLM (unit-testable). An open Feed GATE (unreplied AskUserQuestion /
    permission / ExitPlan) is the authoritative 'needs-input' signal — the agent truly cannot proceed.
    `turn_done` is the transcript's real-time end-of-turn signal: cmux's agentLifecycle lags ~60s at
    'running' after a turn closes, so when the transcript proves the turn ended we treat it as finished at
    once instead of showing a stale 'working'. `quiet` = last activity older than QUIET_S: a just-finished
    agent reads 'ready' (present, available); only a long-dormant one reads 'idle' (asleep). Stateless."""
    if life == "running" and not turn_done:
        return "working"                                      # genuinely mid-turn (cmux running, turn open),
        # so `running` OUTRANKS an open gate. cmux stamps needsInput for a REAL gate, never running, so a
        # gate seen alongside a still-open `running` turn is a stale non-terminal Feed row (e.g. a resume
        # picker answered by a key-send, which never marks the row terminal) -- honoring it resurrects the FP.
    if open_gate and not turn_done:
        return "needs-input"                                  # unreplied Feed gate -> genuinely blocked
        # (a proven end_turn can't be a live gate, so `turn_done` also retires a lingering stale gate row).
    if life in ("", "ended", "unknown") and not turn_done:
        return "pending" if not has_session else "stale"
    # turn ended (cmux needsInput/idle, OR cmux lags at 'running' but the transcript closed the turn):
    # refine from last words; default 'ready' (recently active, available) unless quiet a while -> 'idle'.
    return _refine(last_text, "idle" if quiet else "ready")


_GATE_KINDS = ("question", "permission", "exitplan", "exit_plan", "askuser", "askuserquestion")
_TERMINAL_GATE = ("expired", "resolved", "replied", "answered", "completed", "dismissed", "cancelled")


def _open_gate_uuids():
    """Session uuids that currently have an UNREPLIED actionable Feed gate (AskUserQuestion / permission /
    ExitPlan) — cmux's authoritative 'this agent is truly blocked' signal. A gate is OPEN when it has no
    `resolved_at` and a non-terminal status. Maps a gate to its agent via the session uuid embedded in the
    item's request_id / workstream_id / session_id. Fails OPEN (empty set) on any error, so a Feed hiccup
    degrades to 'no false needs-input' rather than a crash."""
    out = set()
    try:
        data = json.loads(_cmux("rpc", "feed.list", "{}") or "{}")
    except Exception:
        return out
    for it in data.get("items", []):
        if (it.get("kind") or "").lower() not in _GATE_KINDS:
            continue
        if it.get("resolved_at") or (it.get("status") or "").lower() in _TERMINAL_GATE:
            continue                                          # already handled -> not an open gate
        for field in ("request_id", "workstream_id", "session_id", "session"):
            u = fs.bare_uuid(it.get(field) or "")
            if u:
                out.add(u)
    return out


# ─── blocked: the DECISION column (invariant: never guess) ────────────────────────────────────────
# `blocked` answers the one question a conductor actually has — IS THIS AGENT WAITING ON ME? — and it
# is emphatically NOT cmux's `needsInput`. cmux stamps needsInput ~60s after ANY turn ends, so it reads
# identically on a real gate and on an ordinary done-idle agent (live-confirmed 2026-07-12: berg-sandbox
# sat at `needsInput` with a half-typed draft in its input box, gated on nobody). Every conductor has had
# to learn that trivia individually; this column exists so nobody has to.
#
# Both errors are expensive, and they are NOT the same kind of expensive:
#   false YES -> the conductor sends text into a BUSY pane. A session send mid-turn WEDGES the agent:
#                the cure damages a healthy agent.
#   false NO  -> the agent sits forever waiting on a human who was never told. (Today's behavior.)
# Therefore NEITHER answer is guessed. `yes` and `no` each require positive evidence; when the evidence
# is missing or self-contradictory the column SAYS SO (`?`) instead of picking a side. Three honest
# states beat two confident ones.
#
# The tri-state is carried as True / False / None (never the strings) precisely because a naive consumer
# writes `if row["blocked"]: send_answer(...)` — with None, unknown collapses to the SAFE side (no send,
# no wedge). A truthy "unknown" string would collapse to the expensive one.
#
# EVIDENCE (each proof is independent; provenance for every marker is a live capture, not a guess):
#   YES  feed       — cmux's Feed carries an unreplied gate row for this session. Proven end-to-end on a
#                     live gate (2026-07-12): raising an AskUserQuestion posted `kind: "question"` with no
#                     resolved_at, and ANSWERING it set `resolved_at` + `status: "expired"`, so
#                     _open_gate_uuids() correctly stops reporting it. The Feed both fires AND retires.
#   YES  transcript — the last assistant turn ends on an unanswered AskUserQuestion/ExitPlanMode tool_use
#                     (pending_interactive_gate). Independent of cmux entirely, so it still holds for a
#                     DETACHED agent, whose gate never reaches the Feed at all.
#   YES  pane       — a selection dialog / permission prompt is on screen (pane_gate).
#   NO   transcript — the turn provably CLOSED (turn_ended): a terminal stop_reason with nothing after it.
#                     A gated agent's last message ALWAYS ends on a tool_use (the gate tool, or the tool
#                     whose permission is pending), never on a terminal stop, so a closed turn is positive
#                     proof that no gate can be open. This is also what retires a STALE feed row (a picker
#                     answered by a key-send never marks its row terminal), which is why it outranks it.
#   NO   pane       — the normal prompt UI owns the bottom of the screen and no dialog is over it.
_PANE_TAIL_LINES = 12          # the live UI zone: a dialog and the prompt box both render at the BOTTOM

# Every marker below is verbatim from a REAL capture (tests/fixtures/pane-{claude,codex}-*.txt, taken off
# the live fleet 2026-07-12) — never from memory of what the TUI "looks like". One claude capture was taken
# from OUTSIDE this very agent while it sat on a genuine AskUserQuestion; the codex set came from a codex
# seat driven into a real approval prompt. Hand-written UI fixtures are how the two-column `ps` bug shipped.
#
# THE STRUCTURAL FACT both tools share, and the one this whole read depends on: a dialog REPLACES the
# normal chrome, it does not render above it. Measured, not assumed — across 16 consecutive codex frames,
# the 7 pre-gate frames carry the chrome and no gate markers, and the 9 gated frames carry the gate and NO
# chrome. Claude's four captures show the same. That disjointness is what makes "chrome present" a sound
# proof that no dialog is up.
_PANE_GATE_MARKERS = (
    # claude
    "enter to select",                      # the selection-dialog footer: AskUserQuestion / ExitPlanMode
    "esc to cancel",                        #   (both lines observed on the real gate capture)
    "do you want to proceed?",              # the permission prompt
    "would you like to proceed?",           # ExitPlanMode
    "no, and tell claude what to do differently",
    "resume from summary",                  # the --resume picker (see adapter.dismiss_resume_menu)
    # codex — its approval prompt, captured live
    "would you like to run the following command?",
    "press enter to confirm",               # the codex dialog footer ("...or esc to cancel")
    "no, and tell codex what to do differently",
)
# The normal prompt chrome. Present on idle AND working panes alike — it means "the agent's ordinary UI is
# up", NOT "the agent is free": `blocked` asks gate/no-gate only, and working-vs-ready is `state`'s job.
_PANE_PROMPT_MARKERS = (
    "context remaining", "bypass permissions", "shift+tab to cycle",   # claude
    "esc to interrupt",                                                # codex (and some claude builds)
)
# codex's status footer — `gpt-5.5 xhigh fast · ~/path/to/cwd` — as a SHAPE, not a model name: matching
# "gpt-5.5" would silently stop working the day the floor's model pin changes (and that pin has moved
# before). `· <path>` is the durable part. Anchored to the last lines, where the footer lives.
_PANE_CODEX_FOOTER_RE = re.compile(r"·\s*[~/]\S")
# The caret of a SELECTED option. codex draws it `›` (U+203A) and claude `❯` (U+276F) — different glyphs,
# identical meaning. Both tools also use their caret as the INPUT prompt char, so a caret alone proves
# nothing; it is the caret ON a numbered option that means "a selection list is up".
_PANE_CARET_OPTION_RE = re.compile(r"^\s*[❯›>]\s*\d+\.\s+\S")
_PANE_OPTION_RE = re.compile(r"^\s*[❯›>]?\s*\d+\.\s+\S")          # any numbered option


def pane_gate(pane):
    """Tri-state read of ONE captured pane. True = a dialog the agent cannot get past without a human is
    on screen; False = the agent's normal prompt UI is up with no dialog over it; None = unrecognized.

    Reads only the bottom _PANE_TAIL_LINES: on a live pane the dialog (or the prompt box + status footer)
    always owns the bottom of the screen, while the agent's own OUTPUT scrolls above it. That anchoring is
    what stops an agent that merely PRINTED the words "Enter to select" (this session did, writing this
    very matcher) from reading as gated.

    Contradictory evidence — gate markers AND the prompt chrome in the same tail — returns None, not a
    verdict. That is the whole discipline of this column: when the pane does not clearly say, we do not
    decide. Normalizes NBSP, which cmux renders inside the prompt box (`❯\\xa0`) and which would otherwise
    defeat every space-bearing pattern here."""
    if not pane or not pane.strip():
        return None
    lines = [l for l in pane.replace("\xa0", " ").splitlines() if l.strip()][-_PANE_TAIL_LINES:]
    tail = "\n".join(lines).lower()
    has_gate = any(m in tail for m in _PANE_GATE_MARKERS)
    if not has_gate:                        # a caret'd option beside >=2 numbered ones IS a selection list
        opts = [l for l in lines if _PANE_OPTION_RE.match(l)]
        has_gate = len(opts) >= 2 and any(_PANE_CARET_OPTION_RE.match(l) for l in opts)
    has_prompt = (any(m in tail for m in _PANE_PROMPT_MARKERS)
                  or any(_PANE_CODEX_FOOTER_RE.search(l) for l in lines[-3:]))
    if has_gate and not has_prompt:
        return True
    if has_prompt and not has_gate:
        return False
    return None                             # both (or neither) -> the pane does not settle it


def blocked_of(present, feed_gate, transcript_gate, turn_done, unregistered=False, pane=None):
    """THE tri-state, PURE (no I/O — the whole rule is unit-testable). Returns (blocked, why) where
    blocked is True / False / None(=cannot tell) and `why` names the evidence that decided it.

    Order is precedence, and it is deliberate:
      1. UNREGISTERED (a live seat PROCESS with no hook-store record) -> the store and the transcript are
                                     both mute here, so nothing below may run: SessionStart has not fired,
                                     so any transcript on this surface belongs to a PRIOR session and its
                                     closed turn proves nothing about what is on screen NOW. Only the pane
                                     can speak. This is the `claude --resume` picker / startup-stall class:
                                     the agent hangs at a dialog it cannot pass, having never taken a turn —
                                     codex seats have sat at `pending` FOREVER on exactly this state.
                                     It is NOT excused by "adapter.dismiss_resume_menu cleans it up": a
                                     detector whose blind spot is only survivable because some OTHER code
                                     happens to run is a detector that LIES the day that code does not run.
      2. no live agent            -> not blocked (nothing is waiting on you; `state` already says stale)
      3. turn CLOSED **and BOUND** -> not blocked. BOUND is the whole guard, and it is not redundant: an
                                     UNBOUND closed turn is a resume-picker/startup-stall and MUST fall
                                     through to the probe (rule 1). Someone will eventually read
                                     `not unregistered` here, think it cannot matter because rule 1 already
                                     returned, and delete it — leaving nothing but rule 1 between a hung
                                     agent and a cheerful `no`. Leave both.
                                     This also OUTRANKS the feed: it is what retires a stale gate row that a
                                     key-send never marked terminal (mirrors _classify's turn_done rule).
                                     turn_done and transcript_gate are mutually exclusive by construction
                                     (a gate leaves stop_reason=tool_use); the `not transcript_gate` guard
                                     is belt-and-braces so the precedence does not depend on proving that.
      4. any positive gate proof  -> blocked
      5. the pane, when probed    -> the only ground truth for a dialog the transcript cannot see
      6. otherwise                -> None. Mid-turn with no gate evidence is genuinely UNKNOWABLE from the
                                     cheap signals: a long tool call and a silent dialog look identical.
                                     Say so."""
    if unregistered:
        if pane is True:
            return True, "pane: dialog on an unregistered seat (never took a turn — e.g. the resume picker)"
        if pane is False:
            return False, "pane: normal prompt on a seat that has not registered yet (still booting)"
        return None, "live process, no session record — booting or hung at a pre-session dialog; look at the pane"
    if not present:
        return False, "no live agent on the surface"
    if turn_done and not unregistered and not transcript_gate:      # BOUND is the guard — see rule 3
        return False, "transcript: turn closed on a bound session (a gate would have left it open)"
    if feed_gate:
        return True, "feed: unreplied gate row for this session"
    if transcript_gate:
        return True, "transcript: unanswered AskUserQuestion/ExitPlanMode"
    if pane is True:
        return True, "pane: selection dialog on screen"
    if pane is False:
        return False, "pane: normal prompt, no dialog"
    return None, "mid-turn, no gate evidence either way — capture-pane to settle"


# I4: states that must NEVER be masked by `detached`. needs-input / review come from the live cmux Feed
# and are actionable NOW; error and pending describe a seat, not a hook channel. Everything else
# (working / ready / idle / done / stale) is a TIME-based reading of a frozen record — for a detached
# agent those readings are lies, and saying "detached" is the only honest answer.
_ATTACH_PRESERVE = ("needs-input", "review", "error", "pending")


def detached_or(state, attached):
    """`detached` when the hook channel is dead, else `state`. The ONE place the I4 axis meets the
    status vocabulary, shared by `fleet vitals` and `fleet ls` so they can never disagree.

    STATE_STYLE has carried a violet 'detached' glyph and rank since the status taxonomy landed, and
    resolve.attachment() has computed the axis since v2 step 1 — but nothing ever ASSIGNED the state,
    so a detached agent rendered as `ready` in both views. Live proof (2026-07-10): a moved agent read
    `ready` with attached=False and a correct env-mismatch reason sitting unused one field away, and
    berg-sandbox read `stale` while six hours detached. The design's whole point was to NAME this
    state; it was visible only in `--json` and the doctor alert.
    """
    if attached is False and state not in _ATTACH_PRESERVE:
        return "detached"
    return state


def _infer_state(entry, session, open_gates=frozenset(), now=None, turn_done=None):
    """state for one agent: read live signals, then classify (the impure edge over _classify). `open_gates`
    is the set of session uuids with an unreplied Feed gate (computed once per snapshot). Lifecycle reads
    route through resolve (the one resolver; step 1 of the v2 migration). Also reads the transcript's
    end-of-turn signal (beats cmux's ~60s lifecycle lag) and how long the agent has been quiet (ready vs
    idle). `turn_done` may be passed in when the caller already read the transcript tail (snapshot does,
    for `blocked`) — it is the same signal, so re-reading the file per row would be pure waste."""
    from . import resolve as rs
    sid = fs.bare_uuid(session.get("sessionId", ""))
    tpath = session.get("transcriptPath", "")
    updated = session.get("updatedAt") or 0
    quiet = bool(updated) and ((now or time.time()) - updated) > QUIET_S
    return _classify(rs.lifecycle(entry.get("surface", "")), bool(entry.get("session")),
                     fs.last_agent_text(tpath, cap=400),
                     open_gate=bool(sid) and sid in open_gates,
                     turn_done=turn_ended(tpath) if turn_done is None else turn_done, quiet=quiet)


def snapshot():
    """The whole live fleet as a list of view-rows, cheapest signals first. One row per live agent:
        label role kind tool parent surface ws state rank ctx_used ctx_pct_remaining window
        model effort cwd last_text last_age_s
    Pure derive: registry + hook store + transcripts. No cmux screen reads (keeps it cheap).
    Record selection and the attachment fields route through resolve (step 1 of the v2 migration);
    `attached` False = invariant I4's present-but-detached (hooks dead while the process works), the
    state the sidebar renders distinctly and the doctor alerts on."""
    from . import resolve as rs
    store = fs.read_hook_store()
    open_gates = _open_gate_uuids()                           # one Feed query per snapshot (not per agent)
    ws_map = _surface_ws_map()                                # one cmux tree per snapshot (not per agent)
    now = time.time()
    rows = []

    _sweep = []                                              # memo cell for the at-most-one `ps axeww`

    def _ps():
        """The process-table sweep, taken AT MOST ONCE per snapshot and only when a row is unregistered
        (see blocked_of rule 1). A healthy fleet has none, so it costs nothing there."""
        if not _sweep:
            _sweep.append(rs._ps_axeww())
        return _sweep[0]

    for label, e in fs.live_all().items():
        surf = e.get("surface", "")
        sess = rs.freshest(surf, st=store)
        tpath = sess.get("transcriptPath", "")
        # the two transcript reads `blocked` needs, taken ONCE and shared with the state classifier
        tdone, tgate = turn_ended(tpath), pending_interactive_gate(tpath)
        sid = fs.bare_uuid(sess.get("sessionId", ""))
        state = _infer_state(e, sess, open_gates, now=now, turn_done=tdone)
        att = rs.attachment(surf, st=store, ws_map=ws_map, now=now)
        state = detached_or(state, att["attached"])
        # `blocked` reads the SEAT (a live-pid record), never the lifecycle string — see blocked_of.
        # This is the CHEAP tier: no pane read. Rows it cannot settle come back None and are probed by
        # probe_blocked() (one capture-pane each, only for those rows).
        present = bool(rs.freshest_live(surf, st=store))
        # A registered member with NO live record may still be a live PROCESS (booting, or hung at a
        # pre-session dialog the store cannot see). The ps sweep is the only witness; it is taken once
        # per snapshot and only if some row actually needs it — a steady fleet never pays for it.
        unreg = (not present) and bool(surf) and bool(
            rs.pids_ps(surf, ps_out=_ps(), tool=e.get("tool", "claude")))
        blocked, why = blocked_of(present=present, feed_gate=bool(sid) and sid in open_gates,
                                  transcript_gate=tgate, turn_done=tdone, unregistered=unreg)
        used, tmodel = _context_used(sess.get("transcriptPath", ""))
        # Fix 1: the LAUNCHED model carries the window flavor ([1m]); the transcript model doesn't.
        # Prefer it, fall back to the transcript's, then the tool keyword — window is derived from it.
        lmodel, effort = _launched_prefs(sess, e.get("tool", ""))
        model = lmodel or tmodel
        window = _context_window(model or e.get("tool", ""))
        # codex records context/model/effort in its rollout, not the (claude-shaped) transcript that
        # _context_used/_launched_prefs read — so for codex, prefer the rollout's ground truth (real
        # per-turn occupancy + the model's real window + the effective model/effort). '—' still shows
        # when the rollout carries no token_count yet (used stays None), same as claude.
        if (e.get("tool", "") or "").lower() == "codex":
            cx = _codex_rollout_stats(sess.get("transcriptPath", ""))
            if cx["used"] is not None:
                used = cx["used"]
            if cx["window"]:
                window = cx["window"]
            model = cx["model"] or model                     # rollout = what codex ACTUALLY ran
            effort = cx["effort"] or effort
        pct_remaining = None if used is None else max(0, round(100 * (1 - used / window)))
        updated = sess.get("updatedAt") or 0
        rows.append({
            "label": label, "role": e.get("role", "-"), "kind": e.get("kind", "-"),
            "tool": e.get("tool", "-"), "parent": e.get("parent", ""), "surface": surf,
            # DERIVED workspace: live tree first, then the two caches as degraded fallbacks (registry
            # before hook store — fleet's verbs at least update it on move). Never read the hook store
            # alone: it collapses moved agents onto their launch workspace.
            "ws": ws_map.get(surf.upper()) or e.get("workspace") or sess.get("workspaceId", ""),
            "state": state,
            "rank": STATE_STYLE.get(state, ("", "", 9))[2],
            "ctx_used": used, "ctx_pct_remaining": pct_remaining, "window": window,
            "model": model, "effort": effort or "",                     # Fix 2: effort + cwd surfaced
            "cwd": e.get("cwd", "") or sess.get("cwd", ""), "muted": bool(e.get("muted")),
            "last_text": fs.last_agent_text(tpath, cap=120),
            "last_age_s": (now - updated) if updated else None,
            # THE decision column: True = waiting on you, False = not, None = cannot tell (never guessed).
            # None here means "the cheap signals are exhausted"; probe_blocked() settles it off the pane.
            # `unregistered` = a live seat PROCESS with no hook-store record (booting, or hung at a
            # pre-session dialog): the one case where the transcript's closed turn must NOT be believed.
            "blocked": blocked, "blocked_why": why, "unregistered": unreg,
            "probe_fp": _advance_fp(sid, updated, tpath),

            # invariant I4: attached=False means present-but-DETACHED (record frozen while the agent
            # demonstrably works, or an env/pointer mismatch proves the hook channel dead). None =
            # not present / unjudgeable. An idle agent reads attached=True (both clocks frozen equally).
            "attached": att["attached"], "attach_reasons": att["reasons"],
        })
    # cheapest-first triage: most-urgent state first, then longest-idle (oldest activity) within a state
    rows.sort(key=lambda r: (r["rank"], -(r["last_age_s"] or 0)))
    return rows


def _advance_fp(sid, updated, tpath):
    """The surface's ADVANCE MARKER: a fingerprint that moves iff the agent has done anything since we last
    looked. `(session, record updatedAt, transcript mtime+size)`.

    It is deliberately SENSITIVE rather than specific — it is allowed to move when nothing important
    happened (an extra probe is a wasted read, which is harmless), but it must never sit still while a
    dialog appears. It does not, and the reason is mechanical: every way a gate can arrive WRITES first.
    An AskUserQuestion/ExitPlanMode is an assistant message -> the transcript grows. A permission or codex
    approval prompt is preceded by the tool call that triggered it -> PreToolUse fires -> updatedAt moves,
    and the transcript grows. So the marker moves at the exact moment the verdict could change.

    '' when there is nothing to fingerprint (no record, no transcript) — the caller must then NEVER cache,
    because a constant marker would freeze a verdict forever. That is precisely the unregistered seat."""
    try:
        st = os.stat(tpath) if tpath else None
        tfp = f"{st.st_mtime_ns}:{st.st_size}" if st else ""
    except OSError:
        tfp = ""
    if not sid and not updated and not tfp:
        return ""
    return f"{sid}|{updated}|{tfp}"


_PROBE_MEMO = {}                                    # surface -> (advance_fp, blocked, why)


def probe_blocked(rows, cap=_cmux, memo=_PROBE_MEMO):
    """Settle the rows the cheap tier could not: ONE `cmux capture-pane` per row whose `blocked` is None,
    and none at all for the rest. Mutates rows in place; returns how many panes it actually READ.

    This is the escalation the design leans on: mid-turn, a long tool call and a silent dialog are
    IDENTICAL to every cheap signal (store, lifecycle, transcript all freeze the same way) — the screen is
    the only thing that can tell them apart.

    A probe is a READ. It cannot wedge anything — only a SEND can — so it carries no correctness risk at
    all, and the only cost is IPC. That asymmetry is why accuracy is NOT behind a flag: the entire point of
    this column is that nobody should have to memorize that `needsInput` means done-idle, and hiding the
    correct answer behind `--probe` would just swap one piece of trivia for another ("remember to pass
    --probe"). Correct by default; `--no-probe` exists only for someone who explicitly wants the cheap read.

    The watch loop stays cheap by NOT REPEATING WORK rather than by dropping accuracy: a surface whose
    advance marker has not moved since we last probed it reuses that verdict instead of re-reading the pane
    every tick (see _advance_fp — it moves the instant a gate could have appeared). A stable blocked agent
    therefore costs ONE probe, not one per refresh.

    Never cached: an UNREGISTERED seat (no record, no transcript -> no advance marker, so a cached verdict
    would freeze forever) and an INCONCLUSIVE pane (`?` is not a finding — retry it; an unreadable screen
    now may be readable next tick)."""
    probed = 0
    for r in rows:
        if r["blocked"] is not None or not r.get("surface"):
            continue
        fp = r.get("probe_fp") or ""
        cacheable = bool(fp) and not r.get("unregistered")
        if cacheable:
            hit = memo.get(r["surface"])
            if hit and hit[0] == fp:                # nothing has moved since we looked — reuse, don't re-read
                r["blocked"], r["blocked_why"] = hit[1], f"{hit[2]} [cached: surface has not advanced]"
                continue
        verdict = pane_gate(cap("capture-pane", "--surface", r["surface"]))
        probed += 1
        if verdict is None:
            continue                                # still unknown — say so rather than pick a side
        # Re-run the rule with the pane as the new evidence. Everything else is already known to be
        # inconclusive (that is WHY this row is None), so the pane is what decides — but `unregistered`
        # must ride along, or the verdict would explain itself with the wrong evidence.
        r["blocked"], r["blocked_why"] = blocked_of(
            present=True, feed_gate=False, transcript_gate=False, turn_done=False,
            unregistered=bool(r.get("unregistered")), pane=verdict)
        if cacheable:
            memo[r["surface"]] = (fp, r["blocked"], r["blocked_why"])
    return probed


# ─── helpers for rendering ────────────────────────────────────────────────────────────────────
def _age(secs):
    if secs is None:
        return "—"
    secs = int(secs)
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}"


def _winlabel(win):
    """Compact window size for display: 1_000_000 -> '1M', 200_000 -> '200k', 272_000 -> '272k'."""
    return f"{win // 1_000_000}M" if win >= 1_000_000 and win % 1_000_000 == 0 else f"{win // 1000}k"


def _ctx(r):
    """context used / REAL per-agent window + remaining % (Fix 1 makes the denominator per-agent, so it's
    shown: '456k/1M 54%'). '!' marks <=30% left; '—' when there's no parseable usage (Fix 3)."""
    if r["ctx_used"] is None:
        return "—"
    k = r["ctx_used"] / 1000.0
    pct = r["ctx_pct_remaining"]
    flag = "!" if (pct is not None and pct <= 30) else ""
    return f"{k:.0f}k/{_winlabel(r['window'])} {pct}%{flag}"


def _short_model(m):
    """Compact a model string for the table (full string is in --json): drop the tool prefix, KEEP the
    window flavor. 'claude-opus-4-8[1m]' -> 'opus-4-8[1m]'; 'gpt-5-codex' -> 'gpt-5-codex'; '' -> '-'."""
    if not m:
        return "-"
    for pre in ("claude-", "codex-", "openai-"):
        if m.startswith(pre):
            return m[len(pre):]
    return m


def _short_cwd(c):
    """The tail of a cwd for the table (full path is in --json): last two path segments, e.g.
    '/Users/.../cmux-fleet/.worktrees/x' -> '.worktrees/x'. '' -> '-'."""
    parts = [p for p in (c or "").rstrip("/").split("/") if p]
    return "/".join(parts[-2:]) if parts else "-"


def _fit(s, w):
    """Truncate s to width w (keeping columns aligned even with long labels/roles)."""
    s = str(s)
    return s if len(s) <= w else s[:w - 1] + "…"


# ─── vitals: cheapest-first triage table (+ context-remaining %) ───────────────────────────────
def _blk(r):
    """The blocked cell: yes / no / ? — the tri-state rendered. `?` is a first-class answer, not a gap."""
    return {True: "yes", False: "no", None: "?"}[r.get("blocked")]


def _render_vitals(rows):
    """Render the vitals board to a single string (the human table). Pure: no I/O. Shared by the
    one-shot `fleet vitals` and the `--watch` dock loop so they never drift."""
    lines = [f"FLEET VITALS ({len(rows)})   ctx = used / REAL per-agent window",
             f"    {'label':<17}{'state':<12}{'blocked':<9}{'ctx-left':<15}{'model':<13}{'eff':<7}{'cwd':<17}{'idle':<6}last"]
    for r in rows:
        glyph = {"error": "✗", "needs-input": "◍", "review": "⊙", "working": "▶", "detached": "⚠",
                 "done": "✓", "ready": "◌", "idle": "·", "pending": "…", "stale": "?", "gone": "✗"}.get(r["state"], "·")
        muted = " M" if r["muted"] else ""
        lines.append(f"  {glyph} {_fit(r['label'], 16):<17}{r['state']:<12}{_blk(r):<9}{_ctx(r):<15}"
                     f"{_fit(_short_model(r['model']), 12):<13}{_fit(r['effort'] or '-', 6):<7}"
                     f"{_fit(_short_cwd(r['cwd']), 16):<17}{_age(r['last_age_s']):<6}{_fit(r['last_text'], 26)}{muted}")
    waiting = [r for r in rows if r.get("blocked") is True]
    if waiting:
        lines.append(f"\n  ◍ {len(waiting)} WAITING ON YOU: " + ", ".join(r["label"] for r in waiting))
    unsure = [r for r in rows if r.get("blocked") is None]
    if unsure:
        lines.append(f"  ? {len(unsure)} can't tell: " + ", ".join(r["label"] for r in unsure)
                     + "  — `cmux capture-pane --surface <id>` and look before you send")
    near = [r for r in rows if r["ctx_pct_remaining"] is not None and r["ctx_pct_remaining"] <= 30]
    if near:
        lines.append(f"\n  ! {len(near)} near-full (<=30% ctx left): "
                     + ", ".join(r["label"] for r in near) + "  — recycle candidates")
    lines.append("\n(blocked = is this agent waiting on YOU: an OPEN GATE it cannot get past alone (feed row / "
                 "unanswered question in the transcript / dialog on the pane). NOT cmux's `needsInput`, which is "
                 "stamped ~60s after ANY turn and so reads the same on a done-idle agent. `?` = cannot tell — "
                 "look at the pane before sending, because a send into a busy pane wedges it. Why: --json.)")
    lines.append("(ctx = context REMAINING % of each agent's window — an explicit [1m]/[200k] flavor on the "
                 "launched model wins; else the fleet's declared window ([fleet].context_window); '—' = no usage "
                 "yet / unparseable. A bare model can't disambiguate 200k vs 1M, so we don't guess it. role in --json.)")
    return "\n".join(lines)


def _vitals_fp(rows):
    """Change-fingerprint for the watch loop: the fields that mean 'the board meaningfully changed'.
    Deliberately EXCLUDES idle/last-age (they tick every second → would force churn). A heartbeat in
    the loop refreshes ages anyway. Mirrors the on-change-only discipline of `_paint`.

    `blocked` is IN: an agent hitting a gate is the single most repaint-worthy event on the board, and it
    can flip without `state` moving at all (the feed row and the transcript gate are invisible to the
    lifecycle string — that is the entire reason the column exists)."""
    return "\n".join(f"{r['label']}|{r['state']}|{r.get('blocked')}|{r['ctx_pct_remaining']}|{r['last_text']}"
                     for r in rows)


def _apply_scope(rows, scope, caller):
    """Filter vitals snapshot rows to a SET-valued --scope (rows carry kind+parent+label, so the shared
    predicate applies directly). `all` is the whole board; `mine` is you + your direct children."""
    if scope == "all":
        return rows
    return [r for r in rows if fs.scope_matches(scope, r, r["label"], caller, include_self=True)]


def _mine_footer(scope, caller, rows, verb):
    """The one-line 'only you — no children' hint, appended when `--scope mine` resolved to just you."""
    if scope == "mine" and not any(r["label"] != caller for r in rows):
        return "\n" + fs.only_self_hint(verb)
    return ""


def cmd_vitals(argv):
    """fleet vitals [--scope mine|all|conductors|children] [--json] [--paint] [--no-probe] [--watch [--interval N]]
    one-glance triage: who needs you, who's near-full. Rows are most-urgent first (error/needs-input/
    review/working/done/idle). `blocked` is the decision column — yes/no/? for "is this agent waiting on
    ME", grounded in an actual gate and never in cmux's `needsInput`. `ctx` is context-REMAINING % from each
    agent's transcript token usage — a `!` marks <=30% left (recycle candidate). Scoped like every read:
    defaults `--scope mine` (you + your direct children); `--scope all` opens the whole fleet. `--watch` is
    the dock-pane mode: clears+reprints only on the fleet's change-fingerprint (no churn).

    `--no-probe` skips the pane read that settles the rows the cheap signals cannot (they stay `?`)."""
    as_json = "--json" in argv
    paint = "--paint" in argv
    watch = "--watch" in argv
    probe = "--no-probe" not in argv
    scope_arg, _ = fs.pop_scope(argv, default=None)
    scope, caller = fs.read_scope(scope_arg, "vitals")
    interval = 2.0
    if "--interval" in argv:
        try:
            interval = max(0.5, float(argv[argv.index("--interval") + 1]))
        except (ValueError, IndexError):
            interval = 2.0
    if watch and not as_json:
        return _watch_vitals(paint, interval, scope, caller, probe=probe)
    rows = snapshot()
    if probe:
        probe_blocked(rows)                # one capture-pane per UNSETTLED row; none for the rest
    if paint:
        _paint(rows)                       # sidebar sync stays full-fleet — the view scope is display-only
    rows = _apply_scope(rows, scope, caller)
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("(no live agents)" + _mine_footer(scope, caller, rows, "vitals"))
        return 0
    print(_render_vitals(rows) + _mine_footer(scope, caller, rows, "vitals"))
    return 0


def _watch_vitals(paint, interval, scope="all", caller="", probe=True):
    """Dock-pane loop: poll `snapshot()` every `interval`s; repaint the terminal only when the board's
    change-fingerprint moves (or on a slow heartbeat, so idle ages don't freeze). Uses ANSI cursor-home
    + clear-to-end instead of a full `clear` so the board sits still and readable instead of flashing.
    Applies the same `--scope` filter each poll (paint stays full-fleet — the scope is display-only).

    The pane probe runs here too (`--no-probe` to skip): the watch board is the one a conductor actually
    leaves open, so it is the LAST place that should quietly downgrade to a guess. It only ever reads the
    panes the cheap tier could not settle, so a quiet fleet costs nothing extra per poll."""
    HOME_CLEAR = "\x1b[H\x1b[J"            # cursor home, then erase from cursor to end of screen
    HEARTBEAT = 12.0                       # force a redraw at least this often (refresh idle ages)
    prev_fp, last_draw = None, 0.0
    try:
        while True:
            rows = snapshot()
            if probe:
                probe_blocked(rows)
            if paint:
                _paint(rows)
            rows = _apply_scope(rows, scope, caller)
            fp = _vitals_fp(rows)
            now = time.time()
            if fp != prev_fp or (now - last_draw) >= HEARTBEAT:
                body = (_render_vitals(rows) if rows else "(no live agents)") + _mine_footer(scope, caller, rows, "vitals")
                sys.stdout.write(HOME_CLEAR + body + "\n")
                sys.stdout.flush()
                prev_fp, last_draw = fp, now
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


# ─── usage: per-provider subscription windows (the providers feature) ───────────────────────────
_WIN_PRETTY = {"five_hour": "5h", "seven_day": "7day", "thirty_day": "30day"}
_WIN_MINUTES = {"five_hour": 300, "seven_day": 10080, "thirty_day": 43200}   # sort fallback


def _bar(pct, width=10):
    """A |####------| utilization bar. None/unparseable → an empty rail."""
    try:
        f = max(0.0, min(1.0, float(pct) / 100.0))
    except (TypeError, ValueError):
        return "|" + "-" * width + "|  ?"
    n = int(round(f * width))
    return "|" + "#" * n + "-" * (width - n) + f"| {float(pct):.0f}%"


def _countdown(epoch):
    """'resets in 3h16m' from a unix-epoch reset time; '' if absent/past."""
    if not epoch:
        return ""
    d = int(epoch) - int(time.time())
    if d <= 0:
        return "resets now"
    h, m = d // 3600, (d % 3600) // 60
    return f"resets in {h}h{m:02d}m" if h else f"resets in {m}m"


def _ago(epoch):
    """Renders a unix epoch as '5m ago'. Distinct from _age(secs), which renders a DURATION bare."""
    if not epoch:
        return "never"
    d = int(time.time()) - int(epoch)
    return f"{d}s ago" if d < 90 else (f"{d // 60}m ago" if d < 5400 else f"{d // 3600}h ago")


def cmd_usage(argv):
    """fleet usage [--json]   per-provider subscription windows (5h + weekly), reset countdowns, the
    metered-overage/Fable flags, which accounts are live-attributed, and the last poll age. Read-only;
    data comes from the daemon usage poller (provider-usage.json). Sibling to `fleet vitals`."""
    as_json = "--json" in argv
    snap = fs.provider_usage_read()
    # attribution: which live agents launched under each provider (recorded on the registry row)
    attrib = {}
    for label, e in fs.live_all().items():
        p = e.get("provider")
        if p:
            attrib.setdefault(p, []).append(label)
    if as_json:
        print(json.dumps({"providers": snap, "attribution": attrib}, indent=2))
        return 0
    if not snap:
        print("(no usage snapshot yet — the daemon poller writes provider-usage.json; "
              "start it with `fleet daemon start` or configure [providers] in fleet.toml)")
        return 0
    lines = ["USAGE (subscription windows; source: usage poller)"]
    for key in sorted(snap):
        r = snap[key]
        star = " *default" if r.get("is_default") else ""
        who = attrib.get(key) or []
        whom = f"  [{len(who)} live: {', '.join(who[:3])}{'…' if len(who) > 3 else ''}]" if who else ""
        lines.append(f"\n{key}{star}  ({r.get('type', '?')}, checked {_ago(r.get('checked_at'))}){whom}")
        if not r.get("ok"):
            lines.append(f"    !! not readable: {r.get('error', 'unknown')}")
            continue
        # windows are plan-dependent (claude: 5h+7day; codex Team: 5h+7day; codex Free: 30day only), so
        # render whatever the poller found, shortest window first. `<` marks the currently-binding limit.
        w = r.get("windows") or {}
        act = str(r.get("active_limit", ""))
        for name, win in sorted(w.items(), key=lambda kv: (kv[1].get("window_minutes") or _WIN_MINUTES.get(kv[0], 0))):
            mark = " <" if ((name == "five_hour" and act == "session")
                            or (name == "seven_day" and act.startswith("weekly"))) else ""
            lines.append(f"    {_WIN_PRETTY.get(name, name):<6}{_bar(win.get('pct'))}  "
                         f"{_countdown(win.get('resets_at'))}{mark}")
        for sc in (r.get("scoped") or []):
            lines.append(f"    {sc.get('label', 'scoped'):<5} {_bar(sc.get('pct'))}  {_countdown(sc.get('resets_at'))} (scoped)")
        xu = r.get("extra_usage") or {}
        if xu.get("enabled"):
            lines.append(f"    metered $  enabled  util {xu.get('pct')}")
        if r.get("stale"):
            lines.append("    (stale: newest rollout is old; % reflects this account's last activity)")
    print("\n".join(lines))
    return 0


# ─── find: content-aware session lookup ────────────────────────────────────────────────────────
def cmd_find(argv):
    """fleet find <query> [--turns N] [--json]   find an agent by label/role/cwd OR by what it has been
    SAYING. Scans live + archived agents and the last N turns of each transcript for the query, prints
    the match + the line it hit. The "which session was working on X" lookup."""
    # argparse so an OPTION VALUE (the N after --turns) is never folded into the query: a bare
    # `[a for a in argv if not a.startswith('-')]` made `find alpha --turns 3` search for "alpha 3".
    ap = argparse.ArgumentParser(prog="fleet find", add_help=True)
    ap.add_argument("query", nargs="+", help="text to match against label/role/cwd or transcript")
    ap.add_argument("--turns", type=int, default=6, help="transcript turns to scan per agent (default 6)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    turns, as_json = a.turns, a.json
    q = " ".join(a.query).lower()
    store = fs.read_hook_store()
    hits = []
    pools = [("live", fs.live_all()), ("archived", fs.archive_all())]
    for where, pool in pools:
        for label, e in pool.items():
            fields = {"label": label, "role": e.get("role", ""), "cwd": e.get("cwd", "")}
            why, snip = "", ""
            for f, val in fields.items():
                if q in str(val).lower():
                    why, snip = f, str(val)
                    break
            if not why:
                surf = e.get("surface", "")
                sess = _freshest_session(store, surf) if surf else {}
                path = sess.get("transcriptPath", "") or _archive_transcript(e)
                line = _scan_transcript(path, q, turns)
                if line:
                    why, snip = "transcript", line
            if why:
                hits.append({"label": label, "where": where, "role": e.get("role", ""),
                             "match": why, "snippet": snip[:160]})
    if as_json:
        print(json.dumps(hits, indent=2))
        return 0
    if not hits:
        print(f"(no agent matched '{q}')")
        return 1
    print(f"FIND '{q}' ({len(hits)} match):")
    for h in hits:
        print(f"  {h['label']:<22}[{h['where']}/{h['match']}]  {h['snippet']}")
    return 0


def _archive_transcript(entry):
    """Best-effort transcript path for a PARKED agent from its captured last_session id."""
    import glob
    sid = fs.bare_uuid(entry.get("last_session", "") or "")
    if not sid:
        return ""
    for pat in (f"~/.claude/projects/*/*{sid}*.jsonl", f"~/.codex/sessions/*/*/*/*{sid}*.jsonl"):
        paths = glob.glob(os.path.expanduser(pat))
        if paths:
            return max(paths, key=os.path.getmtime)
    return ""


def _scan_transcript(path, q, turns):
    """Return the most-recent transcript text line containing q (within the last `turns` messages)."""
    if not path or not os.path.exists(path):
        return ""
    texts = []
    try:
        for line in open(path):
            try:
                e = json.loads(line)
            except Exception:
                continue
            typ = e.get("type")
            t = ""
            if typ in ("user", "assistant"):
                c = (e.get("message") or {}).get("content")
                t = c if isinstance(c, str) else (
                    " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                    if isinstance(c, list) else "")
            elif typ == "event_msg":
                pl = e.get("payload") or {}
                if pl.get("type") in ("user_message", "agent_message"):
                    t = pl.get("message", "")
            if t and t.strip():
                texts.append(t.strip().replace("\n", " "))
    except Exception:
        return ""
    for t in reversed(texts[-turns:]):
        if q in t.lower():
            i = t.lower().index(q)
            return t[max(0, i - 40):i + 80]
    return ""


# ─── graph: text + HTML fleet tree from parentage ──────────────────────────────────────────────
def _tree(rows):
    """Build the parentage forest from live rows. Each row's `parent` is the parent's LABEL (the registry
    key), so nest by label directly. Roots = agents whose parent isn't a live label. NOTE the parentage
    graph CAN contain cycles (two conductors that list each other) — callers walk with a visited guard,
    and _emit_order() promotes any cycle-orphan (a node unreachable from a root) to a pseudo-root so no
    agent is ever dropped."""
    labels = {r["label"] for r in rows}
    children = {r["label"]: [] for r in rows}
    parent_of = {}
    for r in rows:
        p = r["parent"]
        if p in labels and p != r["label"]:
            parent_of[r["label"]] = p
            children[p].append(r["label"])
    roots = [r["label"] for r in rows if r["label"] not in parent_of]
    return roots, children, {r["label"]: r for r in rows}


def _reach(label, children):
    """Count of nodes reachable from label via children (cycle-safe). Used to pick the best pseudo-root
    when the graph has no true root (a pure parentage cycle): the node with the most descendants is the
    natural top, so leaves don't get promoted ahead of their own ancestors."""
    seen = set()

    def dfs(x):
        if x in seen:
            return
        seen.add(x)
        for k in children.get(x, []):
            dfs(k)
    dfs(label)
    return len(seen)


def _pseudo_root_order(rows, children):
    """Cycle-orphans ordered ancestor-first: most descendants first, label as tiebreak."""
    return sorted((r["label"] for r in rows),
                  key=lambda lbl: (-_reach(lbl, children), lbl))


def _emit_order(rows):
    """(label, depth) pairs in display order: true roots first (DFS), then any cycle-orphans promoted
    ancestor-first. Visited guard makes cycles terminate; nothing is ever dropped. Shared by the text
    and HTML renderers so they agree."""
    roots, children, byl = _tree(rows)
    seen, order = set(), []

    def walk(label, depth):
        if label in seen:
            return
        seen.add(label)
        order.append((label, depth))
        for k in sorted(children.get(label, [])):
            walk(k, depth + 1)

    for root in sorted(roots):
        walk(root, 0)
    for label in _pseudo_root_order(rows, children):         # cycle-orphans, ancestor-first
        if label not in seen:
            walk(label, 0)
    return order, children, byl


def _graph_text(rows):
    order, children, byl = _emit_order(rows)
    if not order:
        return "(no live agents)"
    out = []
    for label, depth in order:
        r = byl[label]
        indent = "  " * depth
        tip = "└─ " if depth else ""
        out.append(f"{indent}{tip}{r['state']:<12} {label:<22} ctx {_ctx(r)}  [{r['role']}/{r['tool']}]")
    return "\n".join(out)


def _graph_html(rows):
    roots, children, byl = _tree(rows)
    seen = set()

    def node(label):
        if label in seen:                                    # cycle guard
            return ""
        seen.add(label)
        r = byl[label]
        color = STATE_STYLE.get(r["state"], ("#8B8D98",))[0]
        ctx = _html.escape(_ctx(r))
        last = _html.escape(r["last_text"][:90])
        kids = [node(k) for k in sorted(children.get(label, []))]
        sub = ("<ul>" + "".join(k for k in kids if k) + "</ul>") if any(kids) else ""
        return (f'<li><div class="n"><span class="dot" style="background:{color}"></span>'
                f'<span class="lbl">{_html.escape(label)}</span>'
                f'<span class="state" style="color:{color}">{_html.escape(r["state"])}</span>'
                f'<span class="meta">{_html.escape(r["role"])}/{_html.escape(r["tool"])} · ctx {ctx}</span>'
                f'<div class="last">{last}</div></div>{sub}</li>')

    items = [node(r) for r in sorted(roots)]
    items += [node(lbl) for lbl in _pseudo_root_order(rows, children)     # cycle-orphans, ancestor-first
              if lbl not in seen]
    body = "".join(i for i in items if i) or "<li><em>no live agents</em></li>"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    # self-contained, dark, zero-dependency — design tokens echo the cmux visual-guide vocabulary.
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>cmux-fleet graph</title>
<style>
:root{{--bg:#0A0C10;--fg:#E6E6E6;--mut:#8B8D98;--line:#23262E;--accent:#FFD24A}}
*{{box-sizing:border-box}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 'JetBrains Mono',ui-monospace,Menlo,monospace;margin:0;padding:28px}}
h1{{font-size:16px;margin:0 0 4px;font-weight:600}}
.sub{{color:var(--mut);font-size:12px;margin-bottom:20px}}
ul{{list-style:none;margin:0;padding-left:22px;border-left:1px solid var(--line)}}
body>ul{{border-left:none;padding-left:0}}
li{{margin:6px 0}}
.n{{padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:#0E1117;display:inline-block;min-width:340px}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;vertical-align:middle}}
.lbl{{font-weight:600}}
.state{{margin-left:10px;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.meta{{color:var(--mut);font-size:12px;margin-left:10px}}
.last{{color:var(--mut);font-size:12px;margin-top:3px;max-width:520px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
</style></head><body>
<h1>⚓ cmux-fleet</h1><div class=sub>{len(rows)} live agent(s) · generated {ts} · read-only</div>
<ul>{body}</ul>
</body></html>"""


def _scope_subtree(rows, scope, caller):
    """Restrict the parentage forest to a --scope. `all` = the whole tree (the old default). `mine` = the
    subtree rooted at you (`caller`, resolved by read_scope). `conductors`/`children` = the union of
    subtrees rooted at every member of that kind. A bare <label> = the subtree rooted at that label."""
    if scope == "all":
        return rows
    _, children, _ = _tree(rows)
    keep = set()

    def dfs(x):
        if x in keep:
            return
        keep.add(x)
        for k in children.get(x, []):
            dfs(k)

    if scope == "mine":
        if caller:
            dfs(caller)
    elif scope in ("conductors", "children"):
        for r in rows:
            if fs.scope_matches(scope, r, r["label"], "", include_self=False):
                dfs(r["label"])
    else:                                                    # a specific label
        if scope not in {r["label"] for r in rows}:
            sys.exit(f"[fleet] graph --scope {scope}: no live label '{scope}'")
        dfs(scope)
    return [r for r in rows if r["label"] in keep]


def cmd_graph(argv):
    """fleet graph [--scope mine|all|conductors|children|<label>] [--json] [--html] [--out FILE]   the fleet
    as a parentage tree. Text by default; --json emits the scoped node rows (label + parent + kind/state)
    as machine output; --html writes a self-contained dark page (default $STATE/fleet-graph.html) and
    prints its path. Scoped like every read: defaults `--scope mine` (your subtree, rooted at you);
    `--scope all` is the full tree; a bare <label> roots the subtree there."""
    as_json = "--json" in argv
    scope_arg, argv = fs.pop_scope(argv, default=None)
    scope, caller = fs.read_scope(scope_arg, "graph", sets_only=False)
    rows = _scope_subtree(snapshot(), scope, caller)
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if "--html" not in argv:
        print(_graph_text(rows))
        if scope == "mine" and len(rows) <= 1:
            print(fs.only_self_hint("graph"))
        return 0
    out = os.path.join(STATE, "fleet-graph.html")
    if "--out" in argv:
        try:
            out = os.path.expanduser(argv[argv.index("--out") + 1])
        except IndexError:
            pass
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(_graph_html(rows))
    print(out)
    return 0


# ─── serve: THIN read-only localhost view (no daemon, no buttons, no analytics) ────────────────
def cmd_serve(argv):
    """fleet serve [--port N]   a THIN foreground localhost server: GET / -> the live graph HTML,
    GET /vitals.json -> the vitals rows. Regenerated from live state per request. No daemon, no actions,
    no analytics, no event engine. Ctrl-C to stop."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    port = 0
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype):
            b = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            rows = snapshot()
            if self.path.rstrip("/") in ("", ""):
                self._send(_graph_html(rows), "text/html; charset=utf-8")
            elif self.path.startswith("/vitals.json"):
                self._send(json.dumps(rows, indent=2), "application/json")
            else:
                self.send_response(404)
                self.end_headers()

    srv = HTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"[fleet serve] read-only fleet view at {url}  (vitals: {url}vitals.json)\n"
          f"[fleet serve] Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[fleet serve] stopped.")
    finally:
        srv.server_close()
    return 0


# ─── paint: native cmux sidebar telemetry (set-status / set-progress) ──────────────────────────
_SEP = "\x1f"                                                 # key delimiter (never appears in a label)


BLOB_TAG = "FLEET4"                                           # bump when the record shape changes
BLOB_FIELDS = 12                                              # surface label state ctx parent kind tool model effort cwd col last
USAGE_MARK = "⧗"                                              # separates the fleet-global usage panel from the
                                                             # blob (and each usage line); stripped from record text


def _blob_clean(s, n):
    """Free text -> one safe blob field. Strips the ~ and ; delimiters so a stray char can't break the
    parse, and NEVER returns empty: Swift's `split(separator:)` DROPS empty components, which would shift
    every later field's index in the sidebar. '-' is the empty sentinel."""
    v = str(s or "").replace("~", "-").replace(";", ",").replace("\n", " ").replace("#", "")
    v = v.replace(USAGE_MARK, "").strip()[:n]                 # no field may carry the usage-panel separator
    return v or "-"


def _fleet_blobs(rows, collapsed=None):
    """{workspace_uuid: 'FLEET4;rec;rec;…'} — ONE blob per workspace, carrying only that workspace's agents.

    Deliberately NOT coupled to a single marker workspace: each record is keyed by the agent's stable
    SURFACE uuid and the sidebar unions the blobs across every workspace, then groups by parent. That
    survives any placement model — agents-as-tabs put N records on one workspace; agents-as-workspaces put
    1 record on each; the render is identical either way, so a future placement change can't invalidate it.

    Record (12 fields, surface FIRST as the identity key):
      surface~label~state~ctx~parent~kind~tool~model~effort~cwd~col~last
    `col` is the collapse bit for a conductor ('1' collapsed). It round-trips: a sidebar tap rewrites the
    description with the bit flipped, and `collapsed` (surface -> '1'/'0') carries it back in here so a
    repaint never clobbers the user's choice. Records are emitted in a STABLE order (conductors by label,
    each followed by its children) so the sidebar never reshuffles."""
    collapsed = collapsed or {}

    def cwd_tail(p):                                            # last 3 path segments (repo/…/worktree)
        segs = [x for x in str(p or "").split("/") if x]
        return "/".join(segs[-3:])

    def rec(r):
        pct = r["ctx_pct_remaining"]
        surf = r.get("surface", "") or "-"
        col = collapsed.get(surf, "0") if r.get("kind") == "conductor" else "0"
        return "~".join([
            surf, _blob_clean(r["label"], 24), r["state"], (str(pct) if pct is not None else "-"),
            _blob_clean(r.get("parent"), 24), r.get("kind", "child") or "child",
            _blob_clean(r.get("tool"), 8), _blob_clean(_short_model(r.get("model") or ""), 16),
            _blob_clean(r.get("effort"), 6), _blob_clean(cwd_tail(r.get("cwd")), 40),
            col, _blob_clean(r.get("last_text"), 160),
        ])

    by_label = sorted(rows, key=lambda r: r["label"])
    conductors = [r for r in by_label if r.get("kind") == "conductor"]
    ordered, seen = [], set()
    for c in conductors:                                        # each conductor, then its children (stable)
        ordered.append(c); seen.add(c["label"])
        for r in by_label:
            if r.get("parent") == c["label"] and r.get("kind") != "conductor" and r["label"] not in seen:
                ordered.append(r); seen.add(r["label"])
    for r in by_label:                                          # orphans (parent isn't a live conductor)
        if r["label"] not in seen:
            ordered.append(r); seen.add(r["label"])

    blobs = {}
    for r in ordered:                                           # bucket each agent onto ITS workspace
        ws = r.get("ws")
        if not ws:
            continue
        blobs.setdefault(ws, [BLOB_TAG]).append(rec(r))
    return {ws: ";".join(recs) for ws, recs in blobs.items()}


def _ws_descriptions():
    """{workspace_uuid: description} as the custom sidebar sees them. Used to read back the collapse bits
    a sidebar tap wrote, so a repaint carries them forward instead of clobbering them."""
    try:
        d = json.loads(_cmux("rpc", "extension.sidebar.snapshot", "{}") or "{}")
        return {w.get("id"): (w.get("description") or "") for w in d.get("workspaces", []) if w.get("id")}
    except Exception:
        return {}


def _collapsed_map(descs):
    """{surface_uuid: '1'|'0'} — the collapse bit each conductor record currently carries, parsed out of
    the live workspace descriptions. Tolerant: any malformed record is skipped, never raises."""
    out = {}
    for desc in (descs or {}).values():
        if not desc.startswith(BLOB_TAG + ";"):
            continue
        for r in desc.split(";")[1:]:
            f = r.split("~")
            if len(f) == BLOB_FIELDS and f[10] in ("0", "1"):
                out[f[0]] = f[10]
    return out


# ─── legacy native-first DESCRIPTOR recognizer (cleanup only) ──────────────────────────────────
# An earlier build pushed a SHORT prose subtitle per agent workspace ("working · ↳berg-sandbox") and leaned
# on cmux's native fields (title/progress/latestMessage) for everything else. That over-corrected: model,
# effort and tool have NO native field (so they vanished), and native ctx/last-message don't match what
# `fleet vitals` shows. The board now rides in a full CLI-derived record again (`_fleet_blobs`, above),
# carrying model/effort/tool/state/ctx/last straight from the snapshot. This recognizer survives ONLY so a
# repaint can CLEAN UP a leftover prose subtitle from that era — never clobbering a user's own description.
DESC_CHILD, DESC_OPEN, DESC_SHUT = "↳", "▾", "▸"
DESC_SEP = " · "


def _is_descriptor(desc):
    """True for a legacy short-prose subtitle WE wrote. A user's own workspace description must never be
    parsed or clobbered, so we require our exact separator+glyph, not a bare glyph that could occur in
    ordinary prose."""
    d = desc or ""
    return any(f"{DESC_SEP}{g}" in d for g in (DESC_CHILD, DESC_OPEN, DESC_SHUT))


def _fmt_reset(secs):
    """Compact 'resets in' string: '45m' / '4h' / '5d' (largest whole unit). '-' when unknown."""
    if secs is None:
        return "-"
    secs = int(secs)
    if secs < 3600:
        return f"{max(0, secs) // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _usage_lines():
    """Compact per-SUBSCRIPTION usage for the sidebar footer, from the STABLE `usage_for_paint()` accessor
    (schema 1). ONE record per subscription provider, rendered on ONE line:
        label~stale~w1label~w1pct~w2label~w2pct~reset
    `label` is the REAL account (accessor's `label` = oauth display/email, falling back to the config id),
    never the config-id `account`. The first two NON-scoped windows (shortest first — typically 5h + 7d)
    carry their CONSUMED %; `reset` is the shortest window's 'resets in' (the soonest-refreshing limit).
    A poll that FAILED or is STALE serializes stale '1' and no numbers, so it renders as one clean line
    instead of confident-looking garbage. Provider-agnostic (skips api/vertex rows with no windows).
    Returns [] on no snapshot OR a schema mismatch — an unknown schema renders nothing, never mis-parses."""
    try:
        from . import providers as pv
        view = pv.usage_for_paint()
    except Exception:
        return []
    if view.get("schema") != 1:                              # gate on the shape THIS code was written against
        return []

    def pct(x):
        return str(int(x)) if isinstance(x, (int, float)) else "-"

    lines = []
    for p in view.get("providers", []):
        if p.get("kind") != "subscription":
            continue
        label = _blob_clean(p.get("label") or p.get("account") or "?", 32)   # fits a full email; swift truncates
        wins = [w for w in (p.get("windows") or []) if not w.get("scoped")]   # rolling windows, not scoped (Fable)
        if (not p.get("ok")) or p.get("stale") or not wins:
            lines.append("~".join([label, "1", "-", "-", "-", "-", "-"]))     # untrusted -> one clean stale line
            continue
        w1 = wins[0]
        w2 = wins[1] if len(wins) > 1 else {}
        lines.append("~".join([
            label, "0",
            _blob_clean(w1.get("label") or "", 6), pct(w1.get("pct")),
            _blob_clean(w2.get("label") or "", 6) if w2 else "-", pct(w2.get("pct")) if w2 else "-",
            _fmt_reset(w1.get("resets_in_s")),
        ]))
    return lines


def _progress_label(r, pct, shared=False):
    """The ctx bar's caption. Carries model·effort, which are NOT native cmux fields — they'd otherwise
    have to lengthen the workspace subtitle. Reads as prose under the bar: 'fable-5 · xhigh · 63% left'.
    The agent's name is prepended only on a SHARED workspace, where the title can't disambiguate it."""
    model = _short_model(r.get("model") or "")
    effort = r.get("effort") or ""
    meta = " · ".join(x for x in (model, effort) if x and x != "-")
    who = f"{r['label']} · " if shared else ""
    return f"{who}{meta} · {pct}% left" if meta else f"{who}{pct}% left"


def _paint(rows, sidebar_blob=False):
    """Push the live fleet onto the cmux BUILT-IN sidebar as native widgets:
      • one status PILL PER AGENT, keyed by the agent's label — so children that SHARE a conductor's
        workspace STACK into a per-agent pill strip instead of overwriting one 'fleet' pill (the old bug);
      • one context PROGRESS BAR per workspace, showing the WORST (lowest-remaining) agent on it — the
        recycle-first signal — since set-progress is per-workspace (only one bar exists to give).
    On-change-only via a per-key fingerprint file (no repaint churn), and agents that vanish get their
    pill CLEARED (`clear-status`) so the strip doesn't accumulate ghosts. Returns pills+bars (re)painted."""
    try:
        prev = json.load(open(PAINT_STATE))
    except Exception:
        prev = {}
    cur, painted = {}, 0
    # worst-case ctx per workspace: lowest remaining % among the agents sharing it (+ who it is).
    # how many agents share each workspace? per-agent workspaces -> 1; a conductor's tab-children -> N.
    worst, ws_count = {}, {}
    for r in rows:
        ws, pct = r["ws"], r["ctx_pct_remaining"]
        if not ws:
            continue
        ws_count[ws] = ws_count.get(ws, 0) + 1
        if pct is not None and (ws not in worst or pct < worst[ws][0]):
            worst[ws] = (pct, r)                              # keep the row -> the bar caption reads its model·effort
    # per-agent status pills — unique key per (workspace, label) so they coexist instead of clobbering.
    for r in rows:
        ws = r["ws"]
        if not ws:
            continue
        color, icon, rank = STATE_STYLE.get(r["state"], ("#8B8D98", "circle", 9))
        pct = r["ctx_pct_remaining"]
        # The pill VALUE is what renders. On a SHARED workspace (tab-children) pills stack, so lead with the
        # agent's LABEL (otherwise invisible) + its ctx%. On its OWN workspace (per-agent) the workspace TITLE
        # already shows the label, so the pill carries the STATE word instead (icon+color reinforce it; the
        # per-agent ctx BAR carries the %). Key stays the bare label so pills stack per-agent and clear cleanly.
        if ws_count.get(ws, 1) > 1:
            val = f"{r['label']} · {pct}%" if pct is not None else r["label"]
        else:
            val = r["state"]
        key = f"pill{_SEP}{ws}{_SEP}{r['label']}"
        fp = f"{val}|{color}|{icon}"
        cur[key] = fp
        if prev.get(key) == fp:
            continue                                          # unchanged -> skip (no churn)
        _cmux("set-status", r["label"], val, "--icon", icon, "--color", color,
              "--priority", str(100 - rank), "--workspace", ws)
        painted += 1
    # one ctx bar per workspace = its worst agent (paint once per ws, keyed apart from pills). The bar's
    # LABEL is a SECOND free-text channel: it renders as the caption under the bar in the built-in sidebar
    # (and binds as w.progress.label in a custom one), so model·effort ride HERE rather than lengthening
    # the workspace subtitle. The fingerprint covers the label too, or an effort change at unchanged ctx%
    # would never repaint.
    for ws, (pct, r) in worst.items():
        key = f"prog{_SEP}{ws}"
        prog = f"{(100 - pct) / 100:.2f}"
        label = _progress_label(r, pct, shared=ws_count.get(ws, 1) > 1)
        fp = f"{prog}|{label}"
        cur[key] = fp
        if prev.get(key) == fp:
            continue
        _cmux("set-progress", prog, "--label", label, "--workspace", ws)
        painted += 1
    # emit the fleet board for the custom sidebar (fleet.swift) — it can't read pills, only workspace
    # fields, so the board rides in workspace DESCRIPTIONS as a full CLI-derived record (`_fleet_blobs`):
    # one FLEET4 blob PER WORKSPACE, each record keyed by the agent's stable surface uuid, so whatever
    # placement model the fleet lands on (tabs vs per-agent workspaces) the render is unchanged. The record
    # carries model/effort/tool/state/ctx/last straight from the same snapshot `fleet vitals` reads — NOT
    # native cmux fields, which drop model/effort/tool and don't match vitals for ctx/last-message.
    # OFF by default: a blob shows as that workspace's SUBTITLE in the built-in sidebar (ugly), so plain
    # `fleet paint` / `vitals --paint` never write it. Opt in via `--sidebar` / FLEET_SIDEBAR_BLOB=1.
    want_sb = bool(sidebar_blob or os.environ.get("FLEET_SIDEBAR_BLOB"))
    prev_desc_ws = {k.split(_SEP, 1)[1] for k in prev if k.startswith(f"desc{_SEP}")}
    stale_keys = {k for k in prev if k.startswith(f"blob{_SEP}")}   # an even older PAINT_STATE key layout
    descs = _ws_descriptions() if (want_sb or prev_desc_ws or stale_keys) else {}

    def _ours(ws):                                             # a subtitle WE wrote — safe to rewrite/clear
        d = descs.get(ws, "") or ""
        return d.startswith(BLOB_TAG + ";") or _is_descriptor(d)

    if want_sb:
        collapsed = _collapsed_map(descs)                      # {surface: '0'/'1'} — read the user's taps back
        blobs = _fleet_blobs(rows, collapsed)                  # {ws: 'FLEET4;rec;…'} — model/effort/tool intact
        # fleet-GLOBAL subscription usage has no per-workspace home, so ride it on every CONDUCTOR's blob
        # (the sidebar reads it off whichever it renders first). '⧗line⧗line' appended after the record;
        # ⧗ is stripped from record text, so it never collides. Off when there's no subscription snapshot.
        ulines = _usage_lines()
        if ulines:
            tail = USAGE_MARK + USAGE_MARK.join(ulines)
            conductor_ws = {r["ws"] for r in rows if r.get("kind") == "conductor" and r.get("ws")}
            for ws in list(blobs):
                if ws in conductor_ws:
                    blobs[ws] = blobs[ws] + tail
        for ws, blob in blobs.items():
            cur[f"desc{_SEP}{ws}"] = blob
            if descs.get(ws) != blob:                          # diff the LIVE subtitle -> self-healing, no churn
                _cmux("workspace-action", "--action", "set-description", "--description", blob,
                      "--workspace", ws)
                painted += 1
        for ws in prev_desc_ws - set(blobs):                   # workspace lost its agent -> retire its subtitle
            if _ours(ws):
                _cmux("workspace-action", "--action", "clear-description", "--workspace", ws)
    else:
        for ws in prev_desc_ws | set(descs):                   # disabled -> retire every subtitle we own,
            if _ours(ws):                                      # including a live blob left by another process
                _cmux("workspace-action", "--action", "clear-description", "--workspace", ws)
    # retire pills for agents (and legacy single-'fleet' pills) that are no longer present.
    for stale in set(prev) - set(cur):
        parts = stale.split(_SEP)
        if parts[0] == "pill" and len(parts) == 3:
            _cmux("clear-status", parts[2], "--workspace", parts[1])
        elif parts[0] in ("blob", "desc"):
            continue                                           # subtitles are handled above, not pills
        elif _SEP not in stale:                                # old format: bare ws -> the 'fleet' pill
            _cmux("clear-status", "fleet", "--workspace", stale)
    try:
        os.makedirs(STATE, exist_ok=True)
        json.dump(cur, open(PAINT_STATE, "w"))
    except Exception:
        pass
    return painted


def cmd_paint(argv):
    """fleet paint [--sidebar]   sync the live fleet onto the cmux sidebar (status pills + context progress
    bars), once. Cheapest visualization — runs off live state, on-change-only. Re-run (or `vitals --paint`)
    to refresh. `--sidebar` ALSO pushes the full board — one CLI-derived FLEET4 record per workspace, with
    model/effort/tool/state/ctx/last from the same snapshot `fleet vitals` reads — into workspace
    descriptions for the custom `fleet.swift` sidebar to render. OFF by default because that record shows as
    the workspace's subtitle in the BUILT-IN sidebar (only enable it when you're actually using
    fleet.swift; the daemon auto-refreshes it when `sidebar_paint` is configured)."""
    sidebar = "--sidebar" in argv
    n = _paint(snapshot(), sidebar_blob=sidebar)
    extra = " + custom-sidebar blob" if sidebar else ""
    print(f"[fleet paint] synced sidebar{extra} ({n} update(s))")
    return 0
