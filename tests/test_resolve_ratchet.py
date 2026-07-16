# tests/test_resolve_ratchet.py — the resolve.py invariant, ENFORCED instead of merely asserted.
#
# resolve.py's header says, in prose:
#
#     "Do not add a new raw hook-store read anywhere outside this module: that is the review's
#      stale-ghost class (six instances, all fixed 2026-07-10)."
#
# Six instances existed. All were fixed. Nothing prevented a seventh — the invariant was WIRED but not
# ENFORCED, and it lived in a comment a new contributor (or an agent) will never read before violating
# it. This file is the ratchet.
#
# WHAT IS A RAW READ (the actual hazard, not a naming rule):
#   - calling `read_hook_store()`, and
#   - touching the store's raw record structure — `["sessions"]` / `.get("sessions")` /
#     `["activeSessionsBySurface"]` / `.get("activeSessionsBySurface")`.
# That combination is the stale-ghost class: hand-rolled record selection that picks a record WITHOUT the
# liveness rule (invariant I2 — the freshest LIVE-pid record IS the agent; a dead pid is absence, whatever
# the lifecycle string says). Every one of the six bugs was a caller assembling its own judgment from raw
# session dicts. resolve.records/live_records/freshest_live exist precisely so nobody has to.
#
# WHAT IS *NOT* FLAGGED, deliberately: calling the hardened predicates (surface_busy, lifecycle,
# surface_has_live_agent, surface_has_live_pid, resolve_bound_record). Those ARE the safe interface —
# they already apply the liveness rule. resolve.py DELEGATES to them in state.py on purpose (see its
# header: the suite's dominant patch seams are `state.read_hook_store` and those `state.*` names, and the
# delegation is what keeps every existing test seam live while call sites migrate onto resolve's
# interface). A "cleanup" that inlines those bodies into resolve.py and deletes the state.py names would
# silently detach a large part of the suite from the code it thinks it is patching. Do not do it; the
# last test in this file pins it.
#
# THE TWO EXEMPT MODULES:
#   resolve.py — THE resolver. This is where raw reads are supposed to live.
#   state.py   — the canonical home of the predicate bodies resolve delegates to (step 1 of the v2
#                migration; step 3 physically in-lines them here and deletes the state.py names).
#
# HOW THE RATCHET WORKS: the baseline below is the raw-store debt as it stands. The discovered set must
# EQUAL it — so the test fails in BOTH directions:
#   - you ADDED a raw read     -> RED. Route it through resolve (rs.seat / rs.freshest_live / rs.records).
#   - you REMOVED one          -> RED. Delete it from the baseline. The ratchet only ever tightens; a
#                                 baseline nobody prunes rots into a permission slip.
import ast
import os

import pytest

from conftest import REPO

PKG = os.path.join(REPO, "cmux_fleet")

RAW_READ = "read_hook_store"
RAW_KEYS = frozenset({"sessions", "activeSessionsBySurface"})
EXEMPT = frozenset({"resolve.py", "state.py"})

# The debt, as measured 2026-07-12: (module, enclosing function, what it touches). 24 sites, none of them
# new. They are NOT all bugs — most are the sanctioned "read the store ONCE, pass it down as `st=`"
# sharing pattern (cmd_ls, snapshot, fleet_doctor_sweep). The genuine hand-rolled selections still in here
# — features._freshest_session, cli._live_session_for, router._rec_by_session, helpers.cmd_child_digest —
# are the migration's remaining work, and they are listed so they cannot be forgotten. Nothing may join
# this list.
BASELINE = frozenset({
    # cli.py — the largest cluster: the ported cmux-placement helpers and the ls/poll paths.
    ("cli.py", "_store", "read_hook_store()"),
    ("cli.py", "ws_uuid_for_surface", '.get("sessions")'),
    ("cli.py", "poll_session", '.get("activeSessionsBySurface")'),
    ("cli.py", "poll_session", '.get("sessions")'),
    ("cli.py", "_surface_cwd", '.get("activeSessionsBySurface")'),
    ("cli.py", "_surface_cwd", '.get("sessions")'),
    ("cli.py", "cmd_ls", "read_hook_store()"),                   # sanctioned: one read, passed as st=
    ("cli.py", "_sessions_on_surface", '.get("sessions")'),
    ("cli.py", "_live_session_for", '.get("activeSessionsBySurface")'),
    ("cli.py", "_live_session_for", '.get("sessions")'),
    ("cli.py", "_discover_surface_for", '.get("sessions")'),
    ("cli.py", "_tool_for_surface", '.get("activeSessionsBySurface")'),
    ("cli.py", "_tool_for_surface", '.get("sessions")'),
    # features.py — the view layer.
    ("features.py", "_freshest_session", '.get("sessions")'),    # a selection that predates resolve
    ("features.py", "snapshot", "read_hook_store()"),            # sanctioned: one read, passed as st=
    ("features.py", "cmd_find", "read_hook_store()"),            # sanctioned: one read, passed as st=
    # helpers.py
    ("helpers.py", "cmd_child_digest", "read_hook_store()"),
    ("helpers.py", "cmd_child_digest", '.get("sessions")'),
    # router.py
    ("router.py", "store", "read_hook_store()"),
    ("router.py", "_rec_by_session", '.get("sessions")'),
    ("router.py", "transcript_of", '.get("activeSessionsBySurface")'),
    ("router.py", "transcript_of", '.get("sessions")'),
    ("router.py", "fleet_doctor_sweep", "read_hook_store()"),    # sanctioned: one read, shared per sweep
})


class _Scan(ast.NodeVisitor):
    """Every raw-store touch in a module, attributed to its enclosing top-level function.

    Pure AST, so it cannot be fooled by a string that merely LOOKS like a store key: `os.path.join(home,
    "sessions", ...)` in providers.py is a filesystem path, and a text/regex scan would flag it. It is a
    bare Call argument, not a subscript or a `.get` key, so this never sees it."""

    def __init__(self):
        self.hits, self._fn = [], "<module>"

    def visit_FunctionDef(self, node):
        prev = self._fn
        if prev == "<module>":              # a TOP-LEVEL def owns the attribution...
            self._fn = node.name
        self.generic_visit(node)            # ...and a nested def keeps its parent's: a closure is part of
        self._fn = prev                     # the function that owns it, and attributing to the inner name
                                            # would let a violation hide behind a one-line helper rename.

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        if name == RAW_READ:
            self.hits.append((self._fn, f"{RAW_READ}()"))
        elif (name == "get" and node.args and isinstance(node.args[0], ast.Constant)
              and node.args[0].value in RAW_KEYS):
            self.hits.append((self._fn, f'.get("{node.args[0].value}")'))
        self.generic_visit(node)

    def visit_Subscript(self, node):
        if isinstance(node.slice, ast.Constant) and node.slice.value in RAW_KEYS:
            self.hits.append((self._fn, f'["{node.slice.value}"]'))
        self.generic_visit(node)


def _modules():
    return sorted(f for f in os.listdir(PKG) if f.endswith(".py") and f not in EXEMPT)


def _discovered():
    found = set()
    for mod in _modules():
        tree = ast.parse(open(os.path.join(PKG, mod), encoding="utf-8").read(), filename=mod)
        scan = _Scan()
        scan.visit(tree)
        found.update((mod, fn, kind) for fn, kind in scan.hits)
    return found


def _fmt(sites):
    return "\n  ".join(f"{m}::{fn}  {kind}" for m, fn, kind in sorted(sites))


def test_no_new_raw_hook_store_read_outside_resolve():
    """THE RATCHET. A new raw hook-store read anywhere outside resolve.py fails this test.

    If you are here because this went red on your change: you hand-rolled a record read. Don't. Every
    question the store can answer already has a name in resolve.py, and each one applies the liveness rule
    you are about to forget:
        rs.seat(surface)          — everything about one seat, composed
        rs.freshest_live(surface) — the record that IS the agent (a dead pid is absence)
        rs.records(surface)       — all records claiming a surface, any liveness
        rs.present / rs.busy / rs.lifecycle / rs.bound_record
    Pass `st=` if you already hold a store snapshot. That is the whole migration."""
    added = _discovered() - BASELINE
    assert not added, (
        "NEW raw hook-store read(s) outside resolve.py — this is the stale-ghost class (six instances, "
        "all fixed 2026-07-10; this ratchet exists so there is never a seventh):\n  "
        + _fmt(added)
        + "\n\nRoute the read through resolve.py — rs.seat() / rs.freshest_live() / rs.records() — which "
          "applies the liveness rule (the freshest LIVE-pid record IS the agent; a dead pid is absence, "
          "whatever the lifecycle string says). Hand-rolled selection is what made the ghosts."
    )


def test_the_baseline_does_not_rot():
    """The other direction: a baselined site that no longer exists must be DELETED from the baseline.

    A ratchet that is never pruned stops being a ratchet and becomes a permission slip — the list drifts
    away from the code, and eventually someone re-adds a read that is 'already in the baseline'."""
    stale = BASELINE - _discovered()
    assert not stale, (
        "The baseline lists raw reads that are GONE — nice work; now delete them from BASELINE in this "
        "file so the ratchet tightens behind you:\n  " + _fmt(stale)
    )


def test_resolve_is_the_only_module_that_may_read_the_store():
    """The invariant stated positively, so the test reads as the rule rather than as a list of exceptions."""
    offenders = {m for m, _, _ in _discovered()}
    assert offenders <= {m for m, _, _ in BASELINE}, (
        f"module(s) newly reading the hook store raw: {sorted(offenders - {m for m, _, _ in BASELINE})}"
    )


@pytest.mark.parametrize("name", ["surface_has_live_agent", "surface_has_live_pid", "lifecycle",
                                  "surface_busy", "resolve_bound_record", "read_hook_store"])
def test_the_delegation_to_state_stays_load_bearing(name):
    """resolve.py DELEGATES the canonical predicate bodies to state.py, on purpose, and that delegation is
    what keeps the suite's dominant patch seams (`state.read_hook_store` and these `state.*` names) live
    while call sites migrate onto resolve's interface.

    Pinned here because it looks exactly like something to 'simplify'. Inline these bodies into resolve.py
    and delete the state.py names, and a large part of the suite goes on patching a seam nothing calls any
    more — green, and testing nothing. Step 3 (schema v2) moves them deliberately, together with the
    tests. Until then: both names exist, and resolve reaches state through them."""
    state_src = open(os.path.join(PKG, "state.py"), encoding="utf-8").read()
    resolve_src = open(os.path.join(PKG, "resolve.py"), encoding="utf-8").read()
    assert f"def {name}(" in state_src, f"state.py no longer defines {name} — the test seams patch it"
    assert "from . import state as fs" in resolve_src, "resolve.py must reach state.py through `fs`"
