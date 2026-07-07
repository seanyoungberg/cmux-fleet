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
    "working":     ("#30A46C", "gearshape.fill",                3),
    "done":        ("#46A758", "checkmark.circle.fill",         4),
    "idle":        ("#8B8D98", "moon.zzz.fill",                 5),
    "pending":     ("#8B8D98", "hourglass",                     6),
    "stale":       ("#6F6E77", "questionmark.circle",           7),
    "gone":        ("#6F6E77", "xmark.circle",                  7),
}


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
    for key, win in (("haiku", 200000), ("sonnet", 200000), ("opus", 200000),
                     ("gpt-5", 272000), ("o3", 200000), ("codex", 272000), ("gemini", 1000000)):
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


# The interactive tool calls that genuinely BLOCK a turn on the human. A trailing, unanswered one of
# these is the ONLY needsInput state the fleet-doctor should alert on (fleet-doctor #iii). Everything
# else at needsInput — a completed turn idling >~60s at the prompt (which cmux also stamps needsInput via
# Claude's idle Notification hook), the feedback survey, a max-tokens stop — is a done-idle NON-gate.
_INPUT_GATE_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})
_GATE_TAIL_BYTES = 262144   # read only the transcript tail: a gate is always the last thing written


def pending_interactive_gate(transcript_path):
    """True iff the transcript's LAST assistant turn ends on an UNANSWERED interactive gate
    (AskUserQuestion / ExitPlanMode) with nothing after it — the one 'agent is blocked on the human'
    state a needsInput lifecycle can mean. This is the discriminator the needs-input predicate needs:
    cmux stamps needsInput for BOTH a real gate AND an ordinary done-idle turn (>~60s at the prompt), so
    the lifecycle string alone can't tell them apart, but the transcript can — a done-idle turn ends with
    stop_reason=end_turn, a gate ends on the tool_use.

    Reads only the tail (a gate is always the last write). FAILS CLOSED to False on any ambiguity —
    absent/unreadable transcript, end_turn, an answered gate, codex (no transcript) — so the predicate
    SUPPRESSES rather than alerts when it can't prove a gate. The needs-input FP flood is what we are
    killing, and the genuine gate still has the completion backstop + a human eventually noticing; a
    false SUPPRESS is strictly safer than the 100%-FP status quo."""
    if not transcript_path or not os.path.exists(transcript_path):
        return False
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _GATE_TAIL_BYTES), 0)
            chunk = f.read().decode("utf-8", "ignore")
    except Exception:
        return False
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
        return False
    # anything AFTER the last assistant answered it (a tool_result / a new user turn) -> not pending
    if any(r.get("type") == "user" for r in rows[last + 1:]):
        return False
    msg = rows[last].get("message") or {}
    if msg.get("stop_reason") not in ("tool_use", None):       # end_turn / max_tokens / stop -> done-idle
        return False
    return any(isinstance(c, dict) and c.get("type") == "tool_use"
               and c.get("name") in _INPUT_GATE_TOOLS
               for c in (msg.get("content") or []))


def _classify(life, has_session, last_text):
    """PURE state classifier, NO LLM (unit-testable). cmux's agentLifecycle is authoritative for live
    work; the keyword tables only REFINE an idle agent from its last transcript line. Stateless (no
    stickiness in v0.1)."""
    if life == "running":
        return "working"
    if life == "needsInput":
        return "needs-input"
    if life in ("", "ended", "unknown"):
        return "pending" if not has_session else "stale"
    text = (last_text or "").lower()                          # life == "idle": refine from last words
    if any(h in text for h in ERROR_HINTS):
        return "error"
    if any(h in text for h in BLOCK_HINTS):
        return "needs-input"
    if any(h in text for h in REVIEW_HINTS):
        return "review"
    if any(h in text for h in DONE_HINTS):
        return "done"
    return "idle"


def _infer_state(entry, session):
    """state for one agent: read live signals, then classify (the impure edge over _classify)."""
    return _classify(fs.lifecycle(entry.get("surface", "")), bool(entry.get("session")),
                     fs.last_agent_text(session.get("transcriptPath", ""), cap=400))


def snapshot():
    """The whole live fleet as a list of view-rows, cheapest signals first. One row per live agent:
        label role kind tool parent surface ws state rank ctx_used ctx_pct_remaining window
        model effort cwd last_text last_age_s
    Pure derive: registry + hook store + transcripts. No cmux screen reads (keeps it cheap)."""
    store = fs.read_hook_store()
    now = time.time()
    rows = []
    for label, e in fs.live_all().items():
        surf = e.get("surface", "")
        sess = _freshest_session(store, surf)
        state = _infer_state(e, sess)
        used, tmodel = _context_used(sess.get("transcriptPath", ""))
        # Fix 1: the LAUNCHED model carries the window flavor ([1m]); the transcript model doesn't.
        # Prefer it, fall back to the transcript's, then the tool keyword — window is derived from it.
        lmodel, effort = _launched_prefs(sess, e.get("tool", ""))
        model = lmodel or tmodel
        window = _context_window(model or e.get("tool", ""))
        pct_remaining = None if used is None else max(0, round(100 * (1 - used / window)))
        updated = sess.get("updatedAt") or 0
        rows.append({
            "label": label, "role": e.get("role", "-"), "kind": e.get("kind", "-"),
            "tool": e.get("tool", "-"), "parent": e.get("parent", ""), "surface": surf,
            "ws": sess.get("workspaceId", ""), "state": state,
            "rank": STATE_STYLE.get(state, ("", "", 9))[2],
            "ctx_used": used, "ctx_pct_remaining": pct_remaining, "window": window,
            "model": model, "effort": effort or "",                     # Fix 2: effort + cwd surfaced
            "cwd": e.get("cwd", "") or sess.get("cwd", ""), "muted": bool(e.get("muted")),
            "last_text": fs.last_agent_text(sess.get("transcriptPath", ""), cap=120),
            "last_age_s": (now - updated) if updated else None,
        })
    # cheapest-first triage: most-urgent state first, then longest-idle (oldest activity) within a state
    rows.sort(key=lambda r: (r["rank"], -(r["last_age_s"] or 0)))
    return rows


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
def _render_vitals(rows):
    """Render the vitals board to a single string (the human table). Pure: no I/O. Shared by the
    one-shot `fleet vitals` and the `--watch` dock loop so they never drift."""
    lines = [f"FLEET VITALS ({len(rows)})   ctx = used / REAL per-agent window",
             f"    {'label':<17}{'state':<12}{'ctx-left':<15}{'model':<13}{'eff':<7}{'cwd':<17}{'idle':<6}last"]
    for r in rows:
        glyph = {"error": "✗", "needs-input": "◍", "review": "⊙", "working": "▶",
                 "done": "✓", "idle": "·", "pending": "…", "stale": "?", "gone": "✗"}.get(r["state"], "·")
        muted = " M" if r["muted"] else ""
        lines.append(f"  {glyph} {_fit(r['label'], 16):<17}{r['state']:<12}{_ctx(r):<15}"
                     f"{_fit(_short_model(r['model']), 12):<13}{_fit(r['effort'] or '-', 6):<7}"
                     f"{_fit(_short_cwd(r['cwd']), 16):<17}{_age(r['last_age_s']):<6}{_fit(r['last_text'], 26)}{muted}")
    near = [r for r in rows if r["ctx_pct_remaining"] is not None and r["ctx_pct_remaining"] <= 30]
    if near:
        lines.append(f"\n  ! {len(near)} near-full (<=30% ctx left): "
                     + ", ".join(r["label"] for r in near) + "  — recycle candidates")
    lines.append("\n(ctx = context REMAINING % of each agent's window — an explicit [1m]/[200k] flavor on the "
                 "launched model wins; else the fleet's declared window ([fleet].context_window); '—' = no usage "
                 "yet / unparseable. A bare model can't disambiguate 200k vs 1M, so we don't guess it. role in --json.)")
    return "\n".join(lines)


def _vitals_fp(rows):
    """Change-fingerprint for the watch loop: the fields that mean 'the board meaningfully changed'.
    Deliberately EXCLUDES idle/last-age (they tick every second → would force churn). A heartbeat in
    the loop refreshes ages anyway. Mirrors the on-change-only discipline of `_paint`."""
    return "\n".join(f"{r['label']}|{r['state']}|{r['ctx_pct_remaining']}|{r['last_text']}" for r in rows)


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
    """fleet vitals [--scope mine|all|conductors|children] [--json] [--paint] [--watch [--interval N]]
    one-glance triage: who needs you, who's near-full. Rows are most-urgent first (error/needs-input/
    review/working/done/idle). `ctx` is context-REMAINING % from each agent's transcript token usage — a
    `!` marks <=30% left (recycle candidate). Scoped like every read: defaults `--scope mine` (you + your
    direct children); `--scope all` opens the whole fleet. `--watch` is the dock-pane mode: clears+reprints
    only on the fleet's change-fingerprint (no churn)."""
    as_json = "--json" in argv
    paint = "--paint" in argv
    watch = "--watch" in argv
    scope_arg, _ = fs.pop_scope(argv, default=None)
    scope, caller = fs.read_scope(scope_arg, "vitals")
    interval = 2.0
    if "--interval" in argv:
        try:
            interval = max(0.5, float(argv[argv.index("--interval") + 1]))
        except (ValueError, IndexError):
            interval = 2.0
    if watch and not as_json:
        return _watch_vitals(paint, interval, scope, caller)
    rows = snapshot()
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


def _watch_vitals(paint, interval, scope="all", caller=""):
    """Dock-pane loop: poll `snapshot()` every `interval`s; repaint the terminal only when the board's
    change-fingerprint moves (or on a slow heartbeat, so idle ages don't freeze). Uses ANSI cursor-home
    + clear-to-end instead of a full `clear` so the board sits still and readable instead of flashing.
    Applies the same `--scope` filter each poll (paint stays full-fleet — the scope is display-only)."""
    HOME_CLEAR = "\x1b[H\x1b[J"            # cursor home, then erase from cursor to end of screen
    HEARTBEAT = 12.0                       # force a redraw at least this often (refresh idle ages)
    prev_fp, last_draw = None, 0.0
    try:
        while True:
            rows = snapshot()
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


def _age(epoch):
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
        lines.append(f"\n{key}{star}  ({r.get('type', '?')}, checked {_age(r.get('checked_at'))}){whom}")
        if not r.get("ok"):
            lines.append(f"    !! not readable: {r.get('error', 'unknown')}")
            continue
        w = r.get("windows") or {}
        fh, sd = w.get("five_hour") or {}, w.get("seven_day") or {}
        act5 = " <" if r.get("active_limit") == "session" else ""
        actw = " <" if str(r.get("active_limit", "")).startswith("weekly") else ""
        lines.append(f"    5h    {_bar(fh.get('pct'))}  {_countdown(fh.get('resets_at'))}{act5}")
        lines.append(f"    7day  {_bar(sd.get('pct'))}  {_countdown(sd.get('resets_at'))}{actw}")
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


def _fleet_blob(rows):
    """Serialize the whole fleet board into ONE delimited string for the custom sidebar (fleet.swift),
    which can read only cmux workspace fields — so the board rides in a workspace DESCRIPTION and the
    .swift splits it. Format (11 fields):
      'FLEET3;label~state~ctx~parent~kind~surface~tool~model~effort~cwd~last;...'
    The ~ and ; delimiters are stripped from free text so a stray char can't break the parse. Rows are
    emitted in a STABLE order (conductors by label, each followed by its children by label) so the sidebar
    doesn't reshuffle every repaint."""
    def clean(s, n):
        return str(s or "").replace("~", "-").replace(";", ",").replace("\n", " ").strip()[:n]
    def cwd_tail(p):                                            # last 3 path segments (repo/…/worktree) so the repo shows
        segs = [x for x in str(p or "").split("/") if x]
        return "/".join(segs[-3:])
    def rec(r):
        pct = r["ctx_pct_remaining"]
        return "~".join([
            clean(r["label"], 24), r["state"], (str(pct) if pct is not None else "-"),
            clean(r.get("parent"), 24), r.get("kind", "child"), r.get("surface", ""),
            clean(r.get("tool"), 8), clean(_short_model(r.get("model") or ""), 16),
            clean(r.get("effort"), 6), clean(cwd_tail(r.get("cwd")), 40),
            clean(r.get("last_text"), 160),
        ])
    by_label = sorted(rows, key=lambda r: r["label"])
    conductors = [r for r in by_label if r.get("kind") == "conductor"]
    recs = ["FLEET3"]
    seen = set()
    for c in conductors:                                       # each conductor, then its children (stable)
        recs.append(rec(c)); seen.add(c["label"])
        for r in by_label:
            if r.get("parent") == c["label"] and r.get("kind") != "conductor" and r["label"] not in seen:
                recs.append(rec(r)); seen.add(r["label"])
    for r in by_label:                                          # any orphans (parent not a conductor)
        if r["label"] not in seen:
            recs.append(rec(r)); seen.add(r["label"])
    return ";".join(recs)


def _paint(rows):
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
    worst = {}
    for r in rows:
        ws, pct = r["ws"], r["ctx_pct_remaining"]
        if ws and pct is not None and (ws not in worst or pct < worst[ws][0]):
            worst[ws] = (pct, r["label"])
    # per-agent status pills — unique key per (workspace, label) so they coexist instead of clobbering.
    for r in rows:
        ws = r["ws"]
        if not ws:
            continue
        color, icon, rank = STATE_STYLE.get(r["state"], ("#8B8D98", "circle", 9))
        pct = r["ctx_pct_remaining"]
        # the pill's VALUE is what renders — lead with the agent's LABEL (identity, otherwise invisible)
        # and carry its own ctx% as text, since only ONE progress BAR exists per workspace. State is the
        # icon+color. Key stays the bare label so pills stack per-agent and clear cleanly.
        val = f"{r['label']} · {pct}%" if pct is not None else r["label"]
        key = f"pill{_SEP}{ws}{_SEP}{r['label']}"
        fp = f"{val}|{color}|{icon}"
        cur[key] = fp
        if prev.get(key) == fp:
            continue                                          # unchanged -> skip (no churn)
        _cmux("set-status", r["label"], val, "--icon", icon, "--color", color,
              "--priority", str(100 - rank), "--workspace", ws)
        painted += 1
    # one ctx bar per workspace = its worst agent (paint once per ws, keyed apart from pills).
    for ws, (pct, lbl) in worst.items():
        key = f"prog{_SEP}{ws}"
        prog = f"{(100 - pct) / 100:.2f}"
        cur[key] = prog
        if prev.get(key) == prog:
            continue
        _cmux("set-progress", prog, "--label", f"{lbl} {pct}% left", "--workspace", ws)
        painted += 1
    # emit the whole fleet board as one delimited blob into a MARKER workspace's description, for the
    # custom sidebar (fleet.swift) — it can't read pills, only workspace fields. Marker = the first
    # conductor's workspace (deterministic); on-change only; the sidebar finds it by the 'FLEET2;' prefix.
    blob = _fleet_blob(rows)
    marker_ws = (next((r["ws"] for r in rows if r.get("kind") == "conductor" and r["ws"]), "")
                 or next((r["ws"] for r in rows if r["ws"]), ""))
    if marker_ws:
        cur[f"blob{_SEP}mark"] = marker_ws
        cur[f"blob{_SEP}val"] = blob
        if prev.get(f"blob{_SEP}val") != blob or prev.get(f"blob{_SEP}mark") != marker_ws:
            _cmux("workspace-action", "--action", "set-description", "--description", blob,
                  "--workspace", marker_ws)
            painted += 1
        old_mark = prev.get(f"blob{_SEP}mark")
        if old_mark and old_mark != marker_ws:                 # marker moved -> clear the stale blob
            _cmux("workspace-action", "--action", "clear-description", "--workspace", old_mark)
    # retire pills for agents (and legacy single-'fleet' pills) that are no longer present.
    for stale in set(prev) - set(cur):
        parts = stale.split(_SEP)
        if parts[0] == "pill" and len(parts) == 3:
            _cmux("clear-status", parts[2], "--workspace", parts[1])
        elif parts[0] == "blob":
            continue                                           # blob marker handled above, not a pill
        elif _SEP not in stale:                                # old format: bare ws -> the 'fleet' pill
            _cmux("clear-status", "fleet", "--workspace", stale)
    try:
        os.makedirs(STATE, exist_ok=True)
        json.dump(cur, open(PAINT_STATE, "w"))
    except Exception:
        pass
    return painted


def cmd_paint(argv):
    """fleet paint   sync the live fleet onto the cmux sidebar (status pills + context progress bars),
    once. Cheapest visualization — runs off live state, on-change-only. Re-run (or `vitals --paint`) to
    refresh; pair with the custom sidebar `fleet.swift` for the native tree-view."""
    n = _paint(snapshot())
    print(f"[fleet paint] synced sidebar ({n} workspace(s) updated)")
    return 0
