#!/usr/bin/env python3
# drive-child.py <surface-uuid> <prompt...> - reliably submit a prompt to an agent TUI on a cmux surface.
# `cmux send` with a trailing newline only TYPES into the input (the TUI treats it as a line break, not a
# submit), so we send the text and THEN a separate `send-key enter` to actually submit.
#
# ENTER-RACE (the reason this file is more than two cmux calls): an Enter fired immediately after the
# `send` is often processed BEFORE the terminal finishes rendering the just-pasted text, so it never
# submits — the prompt sits typed in the input box, the turn never starts. We beat the race by (1)
# SETTLING: polling capture-pane until the pasted text actually shows in the input box (fixed ~3s pause
# as a fallback), then (2) submitting and VERIFYING the box cleared, RETRYING the Enter if it didn't.
#
# Resolves the cmux binary through config.py (config.CMUX) like every other script, and FAILS LOUD: if a
# `send`/`send-key` cmux call errors, it prints the error and exits non-zero. It never reports "submitted"
# unless the text was sent AND the input box was observed to clear (or the readback path is unavailable,
# in which case it degrades to send + fixed-settle + enter, no worse than the old behavior).
import os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root: locate cmux_fleet (Phase 2 folds this into a `fleet` subcommand)
from cmux_fleet.config import CMUX  # path resolver

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


def main():
    if len(sys.argv) < 3:
        sys.exit('usage: drive-child.py <surface-uuid> <prompt...>')
    surf, text = sys.argv[1], " ".join(sys.argv[2:])
    if _submit(surf, text):
        print(f"[drive] submitted to {surf[:8]}")
    else:
        sys.stderr.write(f"[drive] WARN: could not confirm submission to {surf[:8]} after "
                         f"{SUBMIT_TRIES} Enter retries; the prompt may still be sitting in the input "
                         f"box — check the surface.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
