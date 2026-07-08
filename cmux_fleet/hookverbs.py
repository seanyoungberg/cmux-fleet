#!/usr/bin/env python3
# cmux_fleet/hookverbs.py — the conductor hook LOGIC, folded out of the old standalone
# scripts/hooks/{awareness,drain}.py into `fleet hook-awareness` / `fleet hook-drain` (Phase 3 / codex
# P1.2/P1.3). The plugin now ships THIN python shims that just shell into these verbs (see
# scripts/hooks/_shim.py); all the real inbox logic lives here, in the app, versioned with it.
#
# OUTPUT CONTRACTS (the shim validates these exact shapes before passing stdout to the harness):
#   hook-awareness  (UserPromptSubmit) -> {"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",
#                                          "additionalContext": <str>}}   (blank stdout when nothing pending)
#   hook-drain      (Stop)             -> {"decision":"block","reason": <str>}   (blank stdout = don't block)
#
# Both READ + DISCARD stdin (the harness pipes the event JSON) so a future verb can inspect the payload,
# self-ID via $CMUX_SURFACE_ID, read state under $CMUX_STATE_DIR, and FAIL OPEN: any error -> blank
# stdout, exit 0. Kept out of the 2k-line cli.py per P3.1; cli.main dispatches the two verbs early,
# before the heavier feature/daemon imports, to keep the per-turn hot path lean.
import json, os, sys

from . import state as fs


def _read_stdin():
    """Consume the event payload (bytes) so the pipe drains and future verbs can inspect it. Best-effort."""
    try:
        return sys.stdin.buffer.read()
    except Exception:
        return b""


def _doctor_line(r):
    """One rendered line for a kind='doctor' heartbeat-sweep alert. Most rows flag a still-LIVE member
    (inspect/drive/recycle, NOT revive — contrast the archived 'stale' rows); the two conductor-liveness
    rows (conductor-down / conductor-closed) flag a PEER conductor that looks DOWN and route to the OTHER
    conductors + Berg's desktop, where the affordance IS revive. Unknown reasons degrade to a generic
    'needs attention' rather than dropping."""
    label = r.get("label", "?")
    surf = (r.get("child_surface") or "")[:8]
    reason = r.get("reason", "?")
    if reason == "stall":
        secs = int(r.get("stalled_s") or 0)
        detail = (f"STALLED — turn 'running' but frozen ~{secs // 60}m (dead stream, no Stop hook fired); "
                  f"check it, then recycle if wedged")
    elif reason == "low-ctx":
        detail = (f"LOW CONTEXT — {r.get('ctx_pct_remaining', '?')}% left; wrap up + recycle before it "
                  f"runs out mid-task")
    elif reason == "needs-input":
        detail = "NEEDS INPUT — waiting at a question/permission gate; answer or drive it"
    elif reason == "never-bound":
        mins = int(r.get("pending_s") or 0) // 60
        err = (r.get("pane_error") or "").strip()
        detail = (f"NEVER BOUND — launched ~{mins}m ago, no session; the process died on spawn"
                  + (f" ({err})" if err else "") + f". `fleet rm {label} --kill` + relaunch (fix the flags)")
    elif reason == "conductor-down":
        mins = int(r.get("down_s") or 0) // 60
        detail = (f"CONDUCTOR DOWN — no live agent on its surface for ~{mins}m (a bricked recycle husk); "
                  f"check it and `fleet revive {label}` if it is dead")
    elif reason == "conductor-closed":
        detail = (f"CONDUCTOR SURFACE CLOSED — its surface vanished and it was archived; "
                  f"`fleet revive {label}` to bring it back")
    else:
        detail = f"needs attention ({reason})"
    return f"seq {r.get('seq')}  {label} (surface {surf}): {detail}"


def cmd_hook_awareness(argv):
    """UserPromptSubmit: inject the conductor's pending inbox (child completions + peer messages) into
    CONTEXT via hookSpecificOutput.additionalContext — never the input box. Blank when empty. Fails open."""
    _read_stdin()
    try:
        surface = os.environ.get("CMUX_SURFACE_ID", "")
        comp = fs.inbox_pending(surface, kind="completion") if surface else []
        stale = fs.inbox_pending(surface, kind="stale") if surface else []
        doctor = fs.inbox_pending(surface, kind="doctor") if surface else []
        peers = fs.inbox_pending(surface, kind="peer") if surface else []
        if not comp and not stale and not doctor and not peers:
            return 0
        # Presentation cooldown (audit fix #4): every prompt shows the FULL pending inbox in context, so
        # mark all of it PRESENTED. This is the heartbeat's re-nudge suppressor — an agent actively
        # submitting prompts plainly sees its inbox, so the backstop stays quiet until the reminder
        # window elapses. NOT an ack: the rows stay pending until the agent runs `fleet inbox-ack`.
        fs.presented_mark(surface, comp + stale + doctor + peers, "awareness")

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
        if stale:
            lines.append(f"[fleet] {len(stale)} fleet member(s) auto-archived — surface closed outside "
                         f"the fleet CLI; registry already reconciled (input-safe context note, not from "
                         f"the human):")
            for r in stale:
                lines.append(f"  - seq {r.get('seq')}  {r.get('label','?')}: surface "
                             f"{(r.get('child_surface') or '')[:8]} closed ({r.get('origin','?')}); "
                             f"revive with: fleet revive {r.get('label','?')}")
            lines.append(f"  ack when handled: {fs.ACK} {fs.max_seq(stale)} --stale")
        if doctor:
            lines.append(f"[fleet-doctor] {len(doctor)} tracked member(s) flagged by the heartbeat sweep "
                         f"(still LIVE — a health alert, not an archive; input-safe context note, not from "
                         f"the human):")
            for r in doctor:
                lines.append("  - " + _doctor_line(r))
            lines.append(f"  ack when handled: {fs.ACK} {fs.max_seq(doctor)} --doctor")
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
        return 0
    except Exception:
        return 0


def cmd_hook_drain(argv):
    """Stop: return {decision:block, reason:...} so the turn auto-continues to process pending work.
      - child completions: drained unless the dial is 'passive' (wake-now default; passive mutes).
      - stale-member alerts: same dial as completions (passive is a fleet-wide push mute; the alert
        still lands via awareness on the conductor's next turn).
      - peer messages: drained ALWAYS (a deliberate peer send wants attention now).
    Per-kind block-mark guard prevents re-blocking an un-acked set forever. Blank = don't block. Fails open."""
    _read_stdin()
    try:
        surface = os.environ.get("CMUX_SURFACE_ID", "")
        if not surface:
            return 0

        comp, comp_hi = [], 0
        stale, stale_hi = [], 0
        if fs.autodrain_on():
            cp = fs.inbox_pending(surface, kind="completion")
            chi = fs.max_seq(cp)
            if cp and chi > fs.block_get(surface, "completion"):
                comp, comp_hi = cp, chi
            sp = fs.inbox_pending(surface, kind="stale")
            shi = fs.max_seq(sp)
            if sp and shi > fs.block_get(surface, "stale"):
                stale, stale_hi = sp, shi

        peers, peer_hi = [], 0
        pp = fs.inbox_pending(surface, kind="peer")
        phi = fs.max_seq(pp)
        if pp and phi > fs.block_get(surface, "peer"):
            peers, peer_hi = pp, phi

        if not comp and not stale and not peers:
            return 0
        # Presentation cooldown (audit fix #4): the Stop-hook block auto-continues the turn with these
        # rows in the reason, so mark them PRESENTED. Keeps the heartbeat from re-nudging a row the drain
        # just surfaced. Doctor rows never drain (by design), so they're not marked here — awareness /
        # the heartbeat's own mark cover them.
        fs.presented_mark(surface, comp + stale + peers, "drain")

        lines = []
        if comp:
            fs.block_set(surface, "completion", comp_hi)
            lines.append(f"You have {len(comp)} pending child completion(s) (notify-mode={fs.mode()}, "
                         f"auto-continuing). Process them now, then ack:")
            for r in comp:
                frag = (r.get("child_session", "")).replace("claude-", "")[:8]
                gist = (r.get("gist") or "").strip().replace("\n", " ")[:100]
                lines.append(f"  - seq {r.get('seq')}  {r.get('label','?')}: \"{gist}\"   "
                             f"full: {fs.DIGEST} {frag} 5")
            lines.append(f"  ack: {fs.ACK} {comp_hi}")
        if stale:
            fs.block_set(surface, "stale", stale_hi)
            lines.append(f"{len(stale)} fleet member(s) auto-archived (surface closed outside the fleet "
                         f"CLI; registry already reconciled). Decide whether to revive/replace, then ack:")
            for r in stale:
                lines.append(f"  - seq {r.get('seq')}  {r.get('label','?')}: surface "
                             f"{(r.get('child_surface') or '')[:8]} closed ({r.get('origin','?')}); "
                             f"revive with: fleet revive {r.get('label','?')}")
            lines.append(f"  ack: {fs.ACK} {stale_hi} --stale")
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
                    lines.append(f"      reply: {fs.PEERMSG} {r.get('from_label')} \"<reply>\" --reply-to {mid}")
            lines.append(f"  ack peer msgs: {fs.ACK} {peer_hi} --peer")

        print(json.dumps({"decision": "block", "reason": "\n".join(lines)}))
        return 0
    except Exception:
        return 0
