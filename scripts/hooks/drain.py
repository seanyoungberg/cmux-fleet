#!/usr/bin/env python3
# drain.py - Stop hook (the AUTO-CONTINUE path). Returns {decision:block, reason:...}
# so Claude continues the turn and processes pending work instead of stopping. Zero input touch.
#  - child completions: drained at Stop ONLY in autodrain/auto mode (the dial governs chasing children).
#  - peer messages: drained at Stop ALWAYS (a deliberate peer send wants attention now; not dial-gated).
# Per-kind block-mark guard: only block for items strictly newer than the last seq we blocked-for, so
# an un-acked set doesn't re-block forever (it falls back to the awareness hook). Fails open (exit 0).
import json, os, sys

try:
    sys.stdin.read()
except Exception:
    pass

try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/
    import fleet_state as fs

    surface = os.environ.get("CMUX_SURFACE_ID", "")
    if not surface:
        sys.exit(0)

    comp, comp_hi = [], 0
    if fs.autodrain_on():
        cp = fs.inbox_pending(surface, kind="completion")
        chi = fs.max_seq(cp)
        if cp and chi > fs.block_get(surface, "completion"):
            comp, comp_hi = cp, chi

    peers, peer_hi = [], 0
    pp = fs.inbox_pending(surface, kind="peer")
    phi = fs.max_seq(pp)
    if pp and phi > fs.block_get(surface, "peer"):
        peers, peer_hi = pp, phi

    if not comp and not peers:
        sys.exit(0)

    lines = []
    if comp:
        fs.block_set(surface, "completion", comp_hi)
        lines.append(f"You have {len(comp)} pending child completion(s) (notify-mode={fs.mode()}, "
                     f"auto-continuing). Process them now, then ack:")
        for r in comp:
            frag = (r.get("child_session", "")).replace("claude-", "")[:8]
            gist = (r.get("gist") or "").strip().replace("\n", " ")[:100]
            lines.append(f"  - seq {r.get('seq')}  {r.get('label','?')}: \"{gist}\"   "
                         f"full: python3 {fs.DIGEST} {frag} 5")
        lines.append(f"  ack: python3 {fs.ACK} {comp_hi}")
    if peers:
        fs.block_set(surface, "peer", peer_hi)
        lines.append(f"You have {len(peers)} peer message(s) (a DELIBERATE send from a peer conductor). "
                     f"Handle them now, then ack:")
        for r in peers:
            mid, ptype = r.get("msg_id", "?"), r.get("ptype", "peer-msg")
            rexp = "REPLY EXPECTED" if r.get("reply_expected") else "no reply needed"
            rt = f" re:{r.get('reply_to')}" if r.get("reply_to") else ""
            lines.append(f"  @{r.get('to_label','you')}: [{r.get('from_label','?')}] ({ptype} {mid}{rt} · {rexp})")
            for bl in ((r.get("body") or "").strip().splitlines() or [""]):
                lines.append(f"      {bl}")
            if r.get("reply_expected"):
                lines.append(f"      reply: python3 {fs.PEERMSG} {r.get('from_label')} \"<reply>\" --reply-to {mid}")
        lines.append(f"  ack peer msgs: python3 {fs.ACK} {peer_hi} --peer")

    print(json.dumps({"decision": "block", "reason": "\n".join(lines)}))
    sys.exit(0)
except Exception:
    sys.exit(0)
