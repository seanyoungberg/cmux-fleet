#!/usr/bin/env python3
# cmux_fleet/helpers.py — the agent-facing helper VERBS, folded out of the old standalone
# scripts/{drive-child,peer-msg,child-digest,inbox-ack}.py into `fleet` subcommands (Phase 2 / codex
# P2.1). One app, one entrypoint: a conductor now runs `fleet drive-child …` / `fleet peer-msg …` /
# `fleet child-digest …` / `fleet inbox-ack …` instead of shelling into per-plugin script paths. Kept
# in this module (not cli.py) to keep the 2k-line dispatch file from absorbing the bodies (P3.1).
import glob, json, os, secrets, subprocess, sys, time

from .config import CMUX          # cmux binary path resolver
from . import state as fs         # inbox / registry / wake primitives


# =================================================================================================
# drive-child — reliably submit a prompt to an agent TUI on a cmux surface (beats the paste-settle
# ENTER-RACE). `cmux send` with a trailing newline only TYPES into the input; we send the text and THEN
# a separate `send-key enter`, but an Enter fired before the paste finishes rendering never submits — so
# we SETTLE (poll until the paste shows in the box), then SUBMIT+VERIFY (re-kick the Enter until the box
# clears). FAILS LOUD: a failed send/send-key cmux call prints + exits non-zero.
# =================================================================================================
SETTLE_POLLS = 12          # ~6s: wait for the paste to render in the input box before the first Enter
SETTLE_FALLBACK = 3.0      # fixed settle when the input box can't be read back (berg's proven ~3s)
SUBMIT_TRIES = 4           # Enter re-kicks if the box doesn't clear (the enter-race)
VERIFY_POLLS = 6           # ~3s per Enter to observe the box clear / the turn start
POLL_INTERVAL = 0.5


def cmux(*args):
    """Fail-loud cmux call (send / send-key): non-zero exit -> print + exit non-zero."""
    r = subprocess.run([CMUX, *args], env=dict(os.environ, CMUX_QUIET="1"),
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"[drive] cmux {args[0]} failed (exit {r.returncode}): "
                         f"{(r.stderr or r.stdout or '').strip()}\n")
        sys.exit(r.returncode or 1)
    return r


def _capture(surf):
    """capture-pane, best-effort (never fails the drive on a read hiccup — reads only GATE the retries)."""
    r = subprocess.run([CMUX, "capture-pane", "--surface", surf],
                       env=dict(os.environ, CMUX_QUIET="1"), capture_output=True, text=True)
    return r.stdout or ""


def _norm(s):
    """Whitespace-collapsed text, so a match survives the TUI's own spacing/indent (but not a hard
    line-wrap that splits a token — that just falls back to the fixed settle, which is still correct)."""
    return " ".join((s or "").split())


def _input_line(pane):
    """The draft text currently in the TUI input box: everything after the ❯ marker on the last prompt
    line (the same '❯' convention the recycle quiet-gate uses). '' if no prompt line is visible."""
    prompts = [ln for ln in pane.splitlines() if "❯" in ln]
    return prompts[-1].split("❯", 1)[1].strip() if prompts else ""


def _submit(surf, text):
    """Land `text` in the input box and submit it, beating the paste-settle enter-race. Returns True once
    the box is observed to no longer hold our draft (submitted); False if it never cleared after retries."""
    cmux("send", "--surface", surf, text)
    tail = _norm(text)[-24:]                    # a distinctive tail to spot in the input box

    # (1) SETTLE — wait for the pasted text to actually render in the input box before pressing Enter.
    settled = False
    for _ in range(SETTLE_POLLS):
        if tail and tail in _norm(_input_line(_capture(surf))):
            settled = True
            break
        time.sleep(POLL_INTERVAL)
    if not settled:
        time.sleep(SETTLE_FALLBACK)             # readback unavailable / wrapped -> fixed settle fallback

    # (2) SUBMIT + VERIFY — Enter, then confirm the box cleared; re-kick the Enter (not the paste) if not.
    for _ in range(SUBMIT_TRIES):
        cmux("send-key", "--surface", surf, "enter")
        for _ in range(VERIFY_POLLS):
            if tail not in _norm(_input_line(_capture(surf))):
                return True                     # box no longer holds our draft -> the turn started
            time.sleep(POLL_INTERVAL)
    return False


def cmd_drive_child(argv):
    if len(argv) < 2:
        sys.exit('usage: fleet drive-child <surface-uuid> <prompt...>')
    surf, text = argv[0], " ".join(argv[1:])
    if _submit(surf, text):
        print(f"[drive] submitted to {surf[:8]}")
        return 0
    sys.stderr.write(f"[drive] WARN: could not confirm submission to {surf[:8]} after "
                     f"{SUBMIT_TRIES} Enter retries; the prompt may still be sitting in the input "
                     f"box — check the surface.\n")
    return 1


# =================================================================================================
# peer-msg — deliberate A2A: one conductor messages a PEER. Input-safe delivery via the unified inbox
# (kind=peer): the recipient's awareness hook surfaces it into CONTEXT, never the input box. A fresh
# message EXPECTS a reply by default; --no-reply = informational; --reply-to <id> makes THIS a reply.
# Wake is the DEFAULT (idle peer woken now); --no-wake leaves it for the peer's next turn.
# =================================================================================================
def cmd_peer_msg(argv):
    flags, pos = {"no_reply": False, "expect_reply": False, "no_wake": False, "reply_to": None}, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--no-reply":
            flags["no_reply"] = True; i += 1
        elif a == "--expect-reply":
            flags["expect_reply"] = True; i += 1
        elif a == "--no-wake":
            flags["no_wake"] = True; i += 1
        elif a == "--wake":
            i += 1
        elif a == "--reply-to":
            flags["reply_to"] = argv[i + 1] if i + 1 < len(argv) else None; i += 2
        else:
            pos.append(a); i += 1
    if len(pos) < 2:
        sys.exit('usage: fleet peer-msg <to-label> "<body>" [--no-reply] [--reply-to <id>] [--expect-reply] [--no-wake]')
    to_label, body = pos[0], " ".join(pos[1:])

    from_surface = os.environ.get("CMUX_SURFACE_ID", "")
    if not from_surface:
        sys.exit("peer-msg: no $CMUX_SURFACE_ID (run inside a conductor's cmux terminal)")
    from_label = fs.label_for_surface(from_surface) or from_surface[:8]
    to_surface = fs.surface_for_label(to_label)
    if not to_surface:
        known = ", ".join(fs.live_all().keys()) or "(none)"
        sys.exit(f"peer-msg: no live peer labeled '{to_label}'. Known: {known}")

    is_reply = flags["reply_to"] is not None
    reply_expected = (not flags["no_reply"]) and (flags["expect_reply"] or not is_reply)
    msg_id = secrets.token_hex(3)
    fs.inbox_put("peer", to_surface, {
        "ptype": "peer-reply" if is_reply else "peer-msg",
        "to_label": to_label, "from_surface": from_surface, "from_label": from_label,
        "msg_id": msg_id, "reply_to": flags["reply_to"], "reply_expected": reply_expected, "body": body,
    }, event_key=f"peer:{msg_id}")
    rt = f", re {flags['reply_to']}" if is_reply else ""
    print(f"[peer-msg] {from_label} -> {to_label} (msg {msg_id}{rt}, reply: {'expected' if reply_expected else 'none'})")

    if flags["no_wake"]:
        return 0
    if not fs.idlewake_on():                            # 'passive' is the fleet-wide wake mute; the inbox row is already written
        print(f"[peer-msg] no wake (notify-mode passive): {to_label} sees it on its next turn")
        return 0
    if fs.wake_if_idle(to_surface, "(peer-wake) you have a new peer message waiting in your context; handle it"):
        # cooldown: a successful direct peer-wake shows the row NOW, so the heartbeat must not
        # immediately re-nudge the same peer msg on its next tick (audit issue #6).
        fs.presented_mark(to_surface, [{"event_key": f"peer:{msg_id}"}], "wake")
        print(f"[peer-msg] woke {to_label} to process it now")
    else:
        print(f"[peer-msg] no wake: {to_label} is busy or has a draft; it sees the msg on its next turn")
    return 0


# =================================================================================================
# child-digest — give a conductor the REAL context of a child on drain, not just a doorbell. The bus
# tells us a child finished + its session_id, but the content is redacted from the event; this reads the
# child's transcript JSONL and prints its last N turns. TOOL-AGNOSTIC: resolves the transcript from
# cmux's per-agent hook stores and parses both claude and codex transcript dialects.
# =================================================================================================
def cmd_child_digest(argv):
    frag = fs.bare_uuid(argv[0] if len(argv) > 0 else "")
    N = int(argv[1]) if len(argv) > 1 else 3
    MAX = 900  # per-message char cap so a long turn doesn't blow the conductor's context

    # Prefer cmux's AUTHORITATIVE transcriptPath from its hook stores (recorded from the hook, never
    # guessed) over globbing. The union store carries the right path for ANY tool; the globs are fallback.
    from . import resolve as rs
    path = rs.session_transcript(frag) or None
    if not path:
        for pat in (f"~/.claude/projects/*/*{frag}*.jsonl",          # claude
                    f"~/.codex/sessions/*/*/*/*{frag}*.jsonl"):       # codex
            paths = glob.glob(os.path.expanduser(pat))
            if paths:
                path = max(paths, key=os.path.getmtime)               # newest if the fragment is ambiguous
                break
    if not path:
        print(f"child-digest: no transcript found for '{frag}'")
        return 1

    msgs = []
    for line in open(path):
        try:
            e = json.loads(line)
        except Exception:
            continue
        typ = e.get("type")
        role = text = ""
        if typ in ("user", "assistant"):                             # claude
            content = (e.get("message") or {}).get("content")
            text = content if isinstance(content, str) else (
                "\n".join(b.get("text", "") for b in content
                          if isinstance(b, dict) and b.get("type") == "text")
                if isinstance(content, list) else "")
            role = typ
        elif typ == "event_msg":                                     # codex
            pl = e.get("payload") or {}
            if pl.get("type") == "user_message":
                role, text = "user", pl.get("message", "")
            elif pl.get("type") == "agent_message":
                role, text = "assistant", pl.get("message", "")
        text = (text or "").strip()
        if role and text:
            msgs.append((role, text))

    tail = msgs[-(N * 2):] if msgs else []
    print(f"# child-digest: {os.path.basename(path)}  ({len(tail)} of {len(msgs)} messages, last ~{N} turns)")
    for role, text in tail:
        tag = "USER" if role == "user" else "ASSISTANT"
        snip = text if len(text) <= MAX else text[:MAX] + " […]"
        print(f"\n[{tag}]\n{snip}")
    return 0


# =================================================================================================
# inbox — the on-demand READ of a pending inbox. The awareness/drain hooks push these same rows into a
# conductor's context as they land; this is the PULL for when a conductor missed the live wakes — most of
# all right after a recycle, where a fresh instance has no memory of completions/peer-msgs/alerts that
# queued while it was down. Thin read-only wrapper over fs.inbox_pending() (ALL kinds, oldest→newest);
# clear items with `fleet inbox-ack`. Scoped like every read: default `--scope mine` (your own inbox);
# `--scope <label|surfaceId>` peeks one agent's inbox (this REPLACES the old --surface, kept as a hidden
# alias); `--scope all|conductors|children` = the multi-inbox triage view (every / by-kind member inbox).
# =================================================================================================
def _inbox_line(r):
    """One compact line for a pending row, uniform across kinds: seq · [kind] · who-it's-from · a
    one-line gist. Condenses what the awareness hook renders in full, so a catch-up read fits at a glance."""
    seq, kind = r.get("seq"), r.get("kind", "?")
    who = r.get("from_label") or r.get("label", "?")           # peer carries from_label; the rest carry label
    if kind == "completion":
        gist = (r.get("gist") or "").strip().replace("\n", " ")[:100]
        summary = f'"{gist}"' if gist else "(done, no gist)"
    elif kind == "stale":
        summary = (f"surface {(r.get('child_surface') or '')[:8]} closed ({r.get('origin','?')}); "
                   f"revive: fleet revive {r.get('label','?')}")
    elif kind == "doctor":
        if r.get("reason") == "never-bound":
            summary = f"never-bound — launched but died on spawn, no session; rm --kill + relaunch (fix flags)"
        elif r.get("reason") == "recycle-failed":
            summary = (f"recycle FAILED ({r.get('failure','?')}) — check the seat, then re-run "
                       f"fleet recycle {r.get('label','?')}")
        else:
            summary = f"{r.get('reason','?')} — still LIVE, needs attention (inspect/drive/recycle)"
    elif kind == "peer":
        body = (r.get("body") or "").strip().replace("\n", " ")[:100]
        rexp = " · REPLY EXPECTED" if r.get("reply_expected") else ""
        summary = f"({r.get('ptype','peer-msg')} {r.get('msg_id','?')}) {body}{rexp}"
    else:
        summary = "(unknown kind)"
    return f"seq {seq}  [{kind}]  {who}: {summary}"


def _print_inbox_block(label, surface, pending):
    """Render one agent's pending inbox: header, one line per row, then the per-kind ack hint (each stream
    has its own high-water, so you ack through the last seq shown per kind). Shared by the single-target
    read and each member of the multi-target triage view."""
    if not pending:
        print(f"[inbox] {label} (surface {surface[:8]}): 0 pending")
        return
    print(f"[inbox] {label} (surface {surface[:8]}): {len(pending)} pending (oldest first)")
    for r in pending:
        print("  " + _inbox_line(r))
    flag = {"completion": "", "peer": " --peer", "stale": " --stale", "doctor": " --doctor"}
    hints = [f"{fs.ACK} {fs.max_seq([r for r in pending if r.get('kind') == k])}{flag[k]}"
             for k in ("completion", "peer", "stale", "doctor")
             if any(r.get("kind") == k for r in pending)]
    print("  ack when handled: " + "   ".join(hints))


def _inbox_targets(scope):
    """Resolve a --scope value to the list of (label, surface) inboxes to read.
      mine                      -> just you (self-ID via $CMUX_SURFACE_ID)
      all / conductors / children -> every / by-kind LIVE member's inbox (triage)
      <label> or <surfaceId>    -> that one agent (label resolved via the registry; a raw UUID used as-is)
    Returns (targets, single) — `single` is True for the one-inbox reads (mine / a specific agent)."""
    if scope == "mine":
        surface = os.environ.get("CMUX_SURFACE_ID", "")
        if not surface:
            sys.exit("inbox: --scope mine needs $CMUX_SURFACE_ID (run inside a conductor); "
                     "use --scope <label|surfaceId|all>")
        return [(fs.label_for_surface(surface) or surface[:8], surface)], True
    if scope in ("all", "conductors", "children"):
        members = [(l, v.get("surface", "")) for l, v in fs.live_all().items()
                   if scope == "all" or fs.scope_matches(scope, v, l, "", include_self=False)]
        return [(l, s) for l, s in members if s], False
    # a specific label or a raw surface UUID (the folded-in --surface path)
    surf = fs.surface_for_label(scope)
    if surf:
        return [(scope, surf)], True
    return [(fs.label_for_surface(scope) or scope[:8], scope)], True


def cmd_inbox(argv):
    scope_arg, args = fs.pop_scope(argv, default=None)
    as_json = "--json" in args
    if as_json:
        args.remove("--json")
    if "--surface" in args:                                    # deprecated alias -> --scope <surfaceId>
        i = args.index("--surface"); surf = args[i + 1] if i + 1 < len(args) else ""; del args[i:i + 2]
        sys.stderr.write("[fleet] inbox: --surface is deprecated — use --scope <label|surfaceId>\n")
        if scope_arg is None:                                 # explicit --scope always wins over the alias
            scope_arg = surf
    # read_scope handles the default (mine), the graceful no-surface fallback (omitted -> all), and the
    # explicit-mine-without-surface error; a bare <label>/surfaceId passes through (sets_only=False).
    scope, _ = fs.read_scope(scope_arg, "inbox", sets_only=False)

    targets, single = _inbox_targets(scope)

    if as_json:
        if single:
            print(json.dumps(fs.inbox_pending(targets[0][1]), indent=2))   # raw rows (single-target shape)
        else:
            print(json.dumps({"scope": scope, "inboxes": [
                {"label": l, "surface": s, "pending": fs.inbox_pending(s)} for l, s in sorted(targets)]},
                indent=2))
        return 0

    if single:
        label, surface = targets[0]
        _print_inbox_block(label, surface, fs.inbox_pending(surface))
        return 0

    # multi-target triage: show only members WITH pending, then a summary so 'all clear' is unambiguous
    shown = 0
    for label, surface in sorted(targets):
        pending = fs.inbox_pending(surface)
        if pending:
            _print_inbox_block(label, surface, pending)
            shown += 1
    if shown:
        print(f"[inbox] scope {scope}: {shown} of {len(targets)} inbox(es) have pending items")
    else:
        print(f"[inbox] scope {scope}: all clear (0 pending across {len(targets)} inbox(es))")
    return 0


# =================================================================================================
# inbox-ack — a conductor runs this after handling the items it was shown, to mark them done so they
# stop re-surfacing. Acks an EXACT seq (race-safe: later arrivals have a higher seq and survive).
# EVENT-KEY ack: the seq names a ROW, and the row knows its own kind + event key — so a bare
# `inbox-ack <seq>` acks what you actually pointed at (no more advancing the completion cursor because
# a --doctor flag was forgotten). The kind flags stay as compat fallbacks for a seq that no longer
# resolves to a row. Recording the cleared rows' event keys is what makes ONE ack clear every
# presentation path (awareness/drain/heartbeat/wake/`fleet inbox`) and refuse a producer replay of the
# same event (state.inbox_put) — the per-kind cursor alone can't stop a re-put under a new seq.
# Self-IDs via $CMUX_SURFACE_ID.
# =================================================================================================
def cmd_inbox_ack(argv):
    args = list(argv)
    surface = os.environ.get("CMUX_SURFACE_ID", "")
    kind_flag = None
    if "--peer" in args:
        args.remove("--peer"); kind_flag = "peer"
    if "--stale" in args:
        args.remove("--stale"); kind_flag = "stale"
    if "--doctor" in args:
        args.remove("--doctor"); kind_flag = "doctor"
    if "--surface" in args:
        i = args.index("--surface"); surface = args[i + 1]; del args[i:i + 2]

    if not args or not args[0].lstrip("-").isdigit():
        sys.exit("usage: fleet inbox-ack <seq> [--peer | --stale | --doctor] [--surface <surfaceId>]")
    if not surface:
        sys.exit("inbox-ack: no surface (set $CMUX_SURFACE_ID or pass --surface)")
    seq = int(args[0])

    row = next((r for r in fs.inbox_read()
                if int(r.get("seq", 0)) == seq and r.get("to") == surface), None)
    kind = row.get("kind") if row else (kind_flag or "completion")
    if row and kind_flag and kind_flag != kind:
        print(f"[inbox-ack] note: seq {seq} is a {kind} row, not {kind_flag} — acking the {kind} stream")
    cleared = [r for r in fs.inbox_pending(surface, kind=kind) if int(r.get("seq", 0)) <= seq]
    fs.ack_events(surface, cleared)                    # event-level: clears every path, refuses replays
    now = fs.inbox_ack(surface, kind, seq)             # per-kind cursor: the batch high-water (compat)
    print(f"[inbox-ack] surface {surface[:8]} {kind} -> {now} "
          f"(acked through seq {seq}; {len(cleared)} event(s) cleared on every path)")
    return 0
