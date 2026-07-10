# tests/test_no_shadowed_defs.py — a module may not define the same top-level name twice.
#
# Why this earns its keep: on 2026-07-07 `features.py` grew a second `def _age(epoch)` beside the
# 2026-06-29 `def _age(secs)`. Python bound the later one, so every caller passing a DURATION silently
# got the EPOCH formatter: `fleet vitals` printed "495464h ago" in its idle column and `fleet sessions`
# printed "495464h ago ago" against every resumable session, for three days, with a green suite. Nothing
# raised — the argument was a plausible int either way. Pure AST; no imports, no live cmux.
import ast
import os

import pytest

from conftest import REPO

PKG = os.path.join(REPO, "cmux_fleet")


def _modules():
    return sorted(f for f in os.listdir(PKG) if f.endswith(".py"))


@pytest.mark.parametrize("mod", _modules())
def test_no_duplicate_top_level_definitions(mod):
    """No module defines the same top-level function/class name twice."""
    tree = ast.parse(open(os.path.join(PKG, mod), encoding="utf-8").read(), filename=mod)

    seen, dupes = {}, []
    for node in tree.body:                       # top-level only: nested/conditional defs are legitimate
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in seen:
                dupes.append(f"{node.name}: line {seen[node.name]} shadowed by line {node.lineno}")
            seen[node.name] = node.lineno

    assert not dupes, f"{mod} redefines top-level name(s) — the later def silently wins:\n  " + "\n  ".join(dupes)


def test_age_and_ago_are_distinct_formatters():
    """The specific regression: _age takes a duration (bare), _ago takes an epoch (suffixed)."""
    from cmux_fleet import features as ff

    assert ff._age(5) == "5s"                    # duration -> bare, no "ago"
    assert ff._age(120) == "2m"
    assert ff._age(None) == "—"

    import time
    assert ff._ago(int(time.time()) - 300) == "5m ago"   # epoch -> suffixed
    assert ff._ago(None) == "never"

    # the bug: a duration fed to the epoch formatter reads as ~56 years, not ~2 minutes
    assert "495" not in ff._age(120)
