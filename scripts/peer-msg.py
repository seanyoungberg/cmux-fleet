#!/usr/bin/env python3
# peer-msg.py <to-label> "<body>" [--no-reply] [--reply-to <msg_id>] [--expect-reply] [--no-wake]
#
# Deliberate A2A: one conductor messages a PEER (a conscious choice; the peer isn't expecting it).
# Delivery is input-safe via the unified inbox (kind=peer): the recipient's awareness hook surfaces
# it into CONTEXT, never the input box. Carries the @recipient / [sender] markers.
#
# Reply protocol: a fresh message EXPECTS a reply by default; --no-reply = informational;
# --reply-to <id> makes THIS a reply (expects none further unless --expect-reply).
# Wake is the DEFAULT: an idle peer (at the prompt, no draft) is woken now; a busy peer sees it on
# its next turn; --no-wake leaves it for the next turn. (The wake gate lives in fleet_state.)
import os, sys, secrets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root: locate cmux_fleet (Phase 2 folds this into a `fleet` subcommand)
from cmux_fleet import state as fs


def main():
    args, flags, pos = sys.argv[1:], {"no_reply": False, "expect_reply": False, "no_wake": False, "reply_to": None}, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--no-reply":
            flags["no_reply"] = True; i += 1
        elif a == "--expect-reply":
            flags["expect_reply"] = True; i += 1
        elif a == "--no-wake":
            flags["no_wake"] = True; i += 1
        elif a == "--wake":
            i += 1
        elif a == "--reply-to":
            flags["reply_to"] = args[i + 1] if i + 1 < len(args) else None; i += 2
        else:
            pos.append(a); i += 1
    if len(pos) < 2:
        sys.exit('usage: peer-msg.py <to-label> "<body>" [--no-reply] [--reply-to <id>] [--expect-reply] [--no-wake]')
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
    })
    rt = f", re {flags['reply_to']}" if is_reply else ""
    print(f"[peer-msg] {from_label} -> {to_label} (msg {msg_id}{rt}, reply: {'expected' if reply_expected else 'none'})")

    if flags["no_wake"]:
        return
    if fs.wake_if_idle(to_surface, "(peer-wake) you have a new peer message waiting in your context; handle it"):
        print(f"[peer-msg] woke {to_label} to process it now")
    else:
        print(f"[peer-msg] no wake: {to_label} is busy or has a draft; it sees the msg on its next turn")


if __name__ == "__main__":
    main()
