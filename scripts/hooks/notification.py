#!/usr/bin/env python3
# notification.py — THIN plugin shim for the Notification hook (machine-typed: agent_completed / idle_prompt
# / agent_needs_input / permission_prompt / …). ALL logic lives in the installed app's
# `fleet hook-notification` verb (cmux_fleet.hookverbs), which corroborates state (a completed/idle type
# clears any recorded API-error park) without overruling the Feed-gate logic. Fail-open; stdlib only.
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # locate the sibling _shim
import _shim  # noqa: E402

_shim.run("hook-notification")
