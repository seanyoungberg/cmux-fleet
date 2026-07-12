"""`fleet revive` on a dark surface — the cure that reproduced the disease.

THE PRODUCTION INCIDENT (2026-07-12, one hour after the launch fix was adopted). `revive` is the PRESCRIBED
REMEDY for a dark surface, and it had no dark check of its own. cmux-custom was archived and revived, the
revive landed it dark, and it spent the evening doing real work for Berg while the fleet insisted it was
parked.

TWO SYMPTOMS, ONE EVENT — and the second one is the reason this is not merely a missing check. `revive`
polled ONLY its own surface for the session (`poll_session`), with no adoption fallback. So when cmux
misfiled the session under a phantom surfaceId, revive saw "never bound", `sys.exit`ed — AFTER the agent was
already running — and never registered anything. Result: a LIVE agent on a surface nobody owned, its label
still in the ARCHIVE, `fleet ls` rendering the archived row over a working agent, and `vitals` dropping it
entirely. The launch path had the adoption fallback all along; revive never got it.

The rule, again: **a seat that cannot prove its agent is observable has not succeeded.** Same wound, same
guard — and ONE implementation of it, because two would be the bug we spent the previous night killing.
"""
import pytest


@pytest.fixture
def cli():
    """The in-process `cli` module — imported INSIDE the fixture, never bound at module import.

    THE TRAP (documented by move-refuse, and it caught me exactly as advertised): tests/test_features.py
    pops every `cmux_fleet.*` module out of sys.modules to re-import under a throwaway env. Any test file
    sorting after it that binds the module at IMPORT time holds a STALE TWIN — monkeypatches land on a
    module the code under test no longer imports, so the guard silently runs against the REAL process table
    and the real hook store. Green alone, failing in the suite. Import inside the fixture, always."""
    from cmux_fleet import cli as _cli
    return _cli


@pytest.fixture
def rs():
    """The in-process `resolve` module — same trap, same fix (see the `cli` fixture)."""
    from cmux_fleet import resolve as _rs
    return _rs


SURF = "DFDD9D90-2289-4492-BC6C-780DCBA891FE"      # the live specimen's surface
PHANTOM = "7A220F01-1111-1111-1111-111111111111"
WS = "3B45A9C9-0000-0000-0000-000000000000"


# --- symptom 2: the stranded label (the lying instrument) ------------------------------------------
def test_a_misfiled_session_is_ADOPTED_not_aborted(cli, monkeypatch, capsys):
    """THE production bug. cmux files the session elsewhere; the seated surface polls empty. Adopt it (the
    agent's own env PROVES it is ours) instead of exiting after the agent is already up. An implementation
    that only trusts `poll_session` strands a live agent in the archive — which is exactly what happened."""
    monkeypatch.setattr(cli, "_session_on_launched_surface", lambda s, l: "sess-abc12345")

    sid = cli._adopt_misfiled_session(SURF, "cmux-custom")

    assert sid == "sess-abc12345", "a misfiled session was not adopted — the label would strand in archive"
    assert "different surfaceId" in capsys.readouterr().out


def test_nothing_is_adopted_without_PROOF(cli, monkeypatch):
    """Adoption is proven from the live process's OWN env, never from cwd or a store record's surfaceId (the
    field that lies). No proof -> no sid -> the caller parks the label, which is recoverable. Adopting on a
    guess would bind the registry to someone else's terminal, which is not."""
    monkeypatch.setattr(cli, "_session_on_launched_surface", lambda s, l: "")
    assert cli._adopt_misfiled_session(SURF, "cmux-custom") == ""


# --- symptom 1: the dark surface, and ONE guard for every verb that seats ---------------------------
def _dark(cli, rs, monkeypatch, *, observable, alive=True):
    monkeypatch.setattr(cli, "_OBSERVABLE_TIMEOUT_S", 0)      # do not burn the real 8s window in a unit test
    monkeypatch.setattr(rs, "present", lambda s: observable)
    monkeypatch.setattr(rs, "stamps_since", lambda s, c: 0)
    monkeypatch.setattr(rs, "alive", lambda s, t=None, st=None: alive)
    monkeypatch.setattr(rs, "stamp_cursor", lambda: 0)


def test_a_dark_seat_is_RE_SEATED_by_the_shared_guard(cli, rs, monkeypatch, capsys):
    """The guard is verb-agnostic: only the DELIVERY differs (launch binds, revive resumes). Anything that
    seats an agent onto a fresh surface passes its own `redeliver` and gets the identical proof + repair."""
    seats = []
    _dark(cli, rs, monkeypatch, observable=False)
    monkeypatch.setattr(cli, "_signal_agent_pids", lambda *a, **k: [4989])
    monkeypatch.setattr(cli, "cmuxq", lambda *a, **k: "")
    monkeypatch.setattr(cli, "create_surface", lambda spec, p, d: (WS, "FRESH-SURFACE"))
    spec = {"tool": "claude", "label": "cmux-custom", "abs_cwd": "/tmp"}

    def redeliver(w, s):
        seats.append(s)
        _dark(cli, rs, monkeypatch, observable=True)          # the fresh surface IS filed
        return "sess-new"

    ws, surf, sid = cli._reseat_if_dark(WS, SURF, "sess-old", spec, "PARENT", "down", 0, redeliver)

    assert seats == ["FRESH-SURFACE"], "the dark surface was not re-seated"
    assert (surf, sid) == ("FRESH-SURFACE", "sess-new")
    assert "DARK SURFACE" in capsys.readouterr().out


def test_an_OBSERVABLE_seat_is_left_completely_alone(cli, rs, monkeypatch):
    """Reachable-green, and it is the one that matters: the overwhelmingly common case is a healthy revive.
    A guard that re-seats a working agent would be far worse than the bug — it would destroy the session the
    operator was rescuing. It must not even DELIVER."""
    _dark(cli, rs, monkeypatch, observable=True)
    boom = lambda w, s: pytest.fail("re-seated an agent that cmux was filing perfectly well")
    spec = {"tool": "claude", "label": "kid", "abs_cwd": "/tmp"}

    assert cli._reseat_if_dark(WS, SURF, "sess-1", spec, "P", "down", 0, boom) == (WS, SURF, "sess-1")


def test_a_seat_with_NOTHING_ALIVE_is_not_dark_and_is_not_re_seated(cli, rs, monkeypatch):
    """Darkness is ALIVE-but-unfiled. Nothing alive is a different problem (a dead seat), and re-seating it
    would spawn a second agent to chase a first that never came up."""
    _dark(cli, rs, monkeypatch, observable=False, alive=False)
    boom = lambda w, s: pytest.fail("re-seated a surface with no live agent on it")
    spec = {"tool": "claude", "label": "kid", "abs_cwd": "/tmp"}

    assert cli._reseat_if_dark(WS, SURF, "", spec, "P", "down", 0, boom) == (WS, SURF, "")


def test_a_still_dark_reseat_KEEPS_the_agent_and_explains(cli, rs, monkeypatch, capsys):
    """NON-DESTRUCTIVE on giving up. If the re-seat is dark too, the agent is alive and healthy — killing it
    would be the worse error. An alarm that cannot fix a thing does not get to destroy it."""
    _dark(cli, rs, monkeypatch, observable=False)             # never becomes observable
    monkeypatch.setattr(cli, "_signal_agent_pids", lambda *a, **k: [1])
    monkeypatch.setattr(cli, "cmuxq", lambda *a, **k: "")
    monkeypatch.setattr(cli, "create_surface", lambda spec, p, d: (WS, "FRESH"))
    spec = {"tool": "claude", "label": "kid", "abs_cwd": "/tmp"}

    ws, surf, sid = cli._reseat_if_dark(WS, SURF, "s", spec, "P", "down", 0, lambda w, s: "s2")

    out = capsys.readouterr().out
    assert (surf, sid) == ("FRESH", "s2"), "the agent must be KEPT, not abandoned"
    assert "being KEPT" in out and "killing it would be the worse error" in out
    assert "fleet archive" in out and "NOT `recycle`" in out      # and the real remedy is named


def test_the_reseat_is_BOUNDED(cli, rs, monkeypatch):
    """It must not loop forever against a surface that will never come good — one repair, then hand it back."""
    _dark(cli, rs, monkeypatch, observable=False)
    monkeypatch.setattr(cli, "_signal_agent_pids", lambda *a, **k: [1])
    monkeypatch.setattr(cli, "cmuxq", lambda *a, **k: "")
    monkeypatch.setattr(cli, "create_surface", lambda spec, p, d: (WS, "FRESH"))
    calls = []
    spec = {"tool": "claude", "label": "kid", "abs_cwd": "/tmp"}

    cli._reseat_if_dark(WS, SURF, "s", spec, "P", "down", 0,
                        lambda w, s: (calls.append(s) or "s2"), attempts=1)

    assert len(calls) == 1, f"the re-seat ran {len(calls)} times; it is meant to be bounded"


# --- the WIRING, pinned structurally --------------------------------------------------------------
# Deleting the guard's CALL from `revive` broke nothing above: the unit tests exercise `_reseat_if_dark`
# directly, so a refactor could silently unwire it and ship a dark revive again — which is precisely the
# drift this whole workstream exists to kill. So pin the RULE rather than the call sites:
#
#     ANY function that seats an agent onto a FRESH surface must prove that surface is observable.
#
# That is checkable, and it holds for verbs nobody has written yet. `create_surface` is the one way a fresh
# surface comes into being, so it is the marker: call it, and you owe the proof.
def test_every_verb_that_seats_a_surface_MUST_prove_it_is_observable():
    import ast
    import inspect
    from cmux_fleet import cli as _cli

    tree = ast.parse(inspect.getsource(_cli))

    def calls_in(fn):
        out = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
        return out

    offenders = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        c = calls_in(fn)
        if "create_surface" not in c:
            continue
        if fn.name == "_reseat_if_dark":            # IS the guard — it creates the replacement surface
            continue
        if "_reseat_if_dark" not in c:
            offenders.append(fn.name)

    assert not offenders, (
        f"{offenders} seat an agent onto a fresh surface without proving it is observable. cmux misfiles "
        f"the session intermittently; an unproven seat can land a live agent on a surface `vitals`/`ls`/the "
        f"sidebar look straight through, permanently. Call _reseat_if_dark (pass your own `redeliver`).")


def test_the_wiring_check_can_actually_FAIL():
    """Reachable-red: the guard above must be able to catch an offender, or it is decoration. Synthesize the
    exact shape it hunts for — a function that seats a surface and never proves it."""
    import ast

    bad = ast.parse("def cmd_newverb(a):\n    ws, surf = create_surface(spec, p, 'down')\n    register(surf)\n")
    fn = bad.body[0]
    names = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

    assert "create_surface" in names and "_reseat_if_dark" not in names   # <- what the test above rejects
