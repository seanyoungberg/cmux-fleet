#!/usr/bin/env python3
# drive-child.py <surface-uuid> <prompt...> - reliably submit a prompt to an agent TUI on a cmux surface.
# `cmux send` with a trailing newline only TYPES into the input (the TUI treats it as a line break, not a
# submit), so we send the text and THEN a separate `send-key enter` to actually submit.
#
# Resolves the cmux binary through config.py (config.CMUX) like every other script, and FAILS LOUD: if
# either cmux call errors, it prints the error and exits non-zero. It never reports "submitted" unless
# both commands succeeded.
import os, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import CMUX  # path resolver


def cmux(*args):
    r = subprocess.run([CMUX, *args], env=dict(os.environ, CMUX_QUIET="1"),
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"[drive] cmux {args[0]} failed (exit {r.returncode}): "
                         f"{(r.stderr or r.stdout or '').strip()}\n")
        sys.exit(r.returncode or 1)
    return r


def main():
    if len(sys.argv) < 3:
        sys.exit('usage: drive-child.py <surface-uuid> <prompt...>')
    surf, text = sys.argv[1], " ".join(sys.argv[2:])
    cmux("send", "--surface", surf, text)
    cmux("send-key", "--surface", surf, "enter")
    print(f"[drive] submitted to {surf[:8]}")


if __name__ == "__main__":
    main()
