#!/usr/bin/env python3
# inbox-ack.py <seq> [--peer] [--surface <surfaceId>]
#
# A conductor runs this after handling the items it was shown, to mark them done so they stop
# re-surfacing. Acks an EXACT seq (race-safe: later arrivals have a higher seq and survive). Default
# kind is `completion`; pass --peer to ack the peer stream. Self-IDs via $CMUX_SURFACE_ID.
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root: locate cmux_fleet (Phase 2 folds this into a `fleet` subcommand)
from cmux_fleet import state as fs

args = sys.argv[1:]
surface = os.environ.get("CMUX_SURFACE_ID", "")
kind = "completion"
if "--peer" in args:
    args.remove("--peer"); kind = "peer"
if "--surface" in args:
    i = args.index("--surface"); surface = args[i + 1]; del args[i:i + 2]

if not args or not args[0].lstrip("-").isdigit():
    sys.exit("usage: inbox-ack.py <seq> [--peer] [--surface <surfaceId>]")
if not surface:
    sys.exit("inbox-ack: no surface (set $CMUX_SURFACE_ID or pass --surface)")

now = fs.inbox_ack(surface, kind, int(args[0]))
print(f"[inbox-ack] surface {surface[:8]} {kind} -> {now} (acked through seq {args[0]})")
