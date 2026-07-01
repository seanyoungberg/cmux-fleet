#!/usr/bin/env python3
# child-digest.py <session-id-or-fragment> [N]
#
# Give a conductor the REAL context of a child on drain, not just a doorbell. The bus tells us a child
# finished + its session_id, but the content is redacted from the event. This reads the child's
# transcript JSONL and prints its last N turns (user prompt + assistant text) — what a conductor
# "taking back over" needs. TOOL-AGNOSTIC: resolves the transcript from cmux's per-agent hook stores
# (so it works for claude, codex, ...) and parses both transcript dialects:
#   claude -> {"type":"user"|"assistant","message":{"content": str | [{"type":"text","text":...}]}}
#   codex  -> {"type":"event_msg","payload":{"type":"user_message"|"agent_message","message":...}}
# Bus session_id is tool-prefixed (claude-<uuid> / codex-<uuid>); the store + files key on the bare uuid.
#
#   python3 child-digest.py 70daaccf 3        # last 3 turns of the child whose id contains 70daaccf
import sys, glob, json, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root: locate cmux_fleet (Phase 2 folds this into a `fleet` subcommand)
from cmux_fleet import state as fs

frag = fs.bare_uuid(sys.argv[1] if len(sys.argv) > 1 else "")
N = int(sys.argv[2]) if len(sys.argv) > 2 else 3
MAX = 900  # per-message char cap so a long turn doesn't blow the conductor's context

# Prefer cmux's AUTHORITATIVE transcriptPath from its hook stores (recorded from the hook, never
# guessed) over globbing. The union store carries the right path for ANY tool; the globs are fallback.
path = None
for s in (fs.read_hook_store().get("sessions") or {}).values():
    if frag and frag in (s.get("sessionId") or "") and s.get("transcriptPath"):
        path = s["transcriptPath"]
        break
if not path:
    for pat in (f"~/.claude/projects/*/*{frag}*.jsonl",          # claude
                f"~/.codex/sessions/*/*/*/*{frag}*.jsonl"):       # codex
        paths = glob.glob(os.path.expanduser(pat))
        if paths:
            path = max(paths, key=os.path.getmtime)               # newest if the fragment is ambiguous
            break
if not path:
    print(f"child-digest: no transcript found for '{frag}'")
    sys.exit(1)

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
