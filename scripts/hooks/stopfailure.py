#!/usr/bin/env python3
# stopfailure.py — THIN plugin shim for the StopFailure hook (turn ended on an API error; Stop never fires
# on errors). ALL logic lives in the installed app's `fleet hook-stopfailure` verb (cmux_fleet.hookverbs),
# which records the structured halt (limit-parked / errored) so status derives from STRUCTURE at the source.
# This file just shells into it, fail-open, via the shared shim. Stdlib only; imports NO cmux_fleet.
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # locate the sibling _shim
import _shim  # noqa: E402

_shim.run("hook-stopfailure")
