#!/usr/bin/env python3
# awareness.py - UserPromptSubmit hook. THE input-safe channel: on every turn it
# injects the conductor's pending inbox (child completions + peer messages) into CONTEXT via
# hookSpecificOutput.additionalContext, never the input box. Always-on (only adds context). Self-IDs
# via $CMUX_SURFACE_ID. Emits nothing when empty. Fails open (any error -> exit 0). Reads the unified
# inbox; acks are per-kind so completions and peers ack independently.
import json, os, sys

try:
    sys.stdin.read()
except Exception:
    pass

try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root: locate cmux_fleet (Phase 3 makes these thin `fleet hook-*` shims)
    from cmux_fleet import state as fs

    surface = os.environ.get("CMUX_SURFACE_ID", "")
    comp = fs.inbox_pending(surface, kind="completion") if surface else []
    peers = fs.inbox_pending(surface, kind="peer") if surface else []
    if not comp and not peers:
        sys.exit(0)

    lines = []
    if comp:
        lines.append(f"[fleet] {len(comp)} child completion(s) pending your attention "
                     f"(input-safe context note, not from the human):")
        for r in comp:
            frag = (r.get("child_session", "")).replace("claude-", "")[:8]
            gist = (r.get("gist") or "").strip().replace("\n", " ")[:100]
            lines.append(f"  - seq {r.get('seq')}  {r.get('label','?')}: \"{gist}\"   "
                         f"full: {fs.DIGEST} {frag} 5")
        lines.append(f"  ack when handled: {fs.ACK} {fs.max_seq(comp)}")
    if peers:
        lines.append(f"[peer] {len(peers)} peer message(s) for you (a DELIBERATE send from a peer "
                     f"conductor, NOT a child you dispatched; input-safe context note, not from the human):")
        for r in peers:
            mid, ptype = r.get("msg_id", "?"), r.get("ptype", "peer-msg")
            rexp = "REPLY EXPECTED" if r.get("reply_expected") else "no reply needed"
            rt = f" re:{r.get('reply_to')}" if r.get("reply_to") else ""
            lines.append(f"  @{r.get('to_label','you')}: [{r.get('from_label','?')}] ({ptype} {mid}{rt} · {rexp})")
            for bl in ((r.get("body") or "").strip().splitlines() or [""]):
                lines.append(f"      {bl}")
            if r.get("reply_expected"):
                lines.append(f"      reply: {fs.PEERMSG} {r.get('from_label')} \"<reply>\" --reply-to {mid}")
        lines.append(f"  ack when handled: {fs.ACK} {fs.max_seq(peers)} --peer")

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(lines)}}))
    sys.exit(0)
except Exception:
    sys.exit(0)
