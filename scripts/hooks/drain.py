#!/usr/bin/env python3
# drain.py — THIN plugin shim for the Stop hook (Phase 3). ALL logic lives in the installed app's
# `fleet hook-drain` verb (cmux_fleet.hookverbs); this file just shells into it, fail-open, via the
# shared shim. Stdlib only; imports NO cmux_fleet (the plugin must not need the app's checkout on
# sys.path). See scripts/hooks/_shim.py for the contract + the dropped-uvx-fallback rationale.
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # locate the sibling _shim
import _shim  # noqa: E402

_shim.run("hook-drain")
