"""`fleet conformance` runs against a LIVE cmux with Berg's LIVE agents in it.

It creates and destroys workspaces, surfaces and agents. If it ever closes one of his working conductors,
it is worse than no conformance suite at all — so the requirement is not "careful", it is **INCAPABLE**.

These tests pin that. Two independent gates, and a structural one on top:

  1. POSITIVE OWNERSHIP — it may only destroy UUIDs it created, this run, recorded at creation. Not
     name-matched, not prefix-matched: a suite that trusts a label prefix is one typo away from closing
     `cmux-advisor`, and a worker once damaged a live conductor's record by misspelling an env var.
  2. NEGATIVE PROOF — production's registry is read once at startup and every id in it is untouchable for
     the life of the run, even if gate 1 were somehow fooled.
  3. STRUCTURAL — no destructive cmux verb may be invoked anywhere in the module except inside the Sandbox,
     so there is no code path at all from "a uuid I found somewhere" to "a thing I destroyed".
"""
import pytest


@pytest.fixture
def cf():
    """Imported INSIDE the fixture — tests/test_features.py pops cmux_fleet.* out of sys.modules, so a
    module-level bind is a stale twin whose monkeypatches land on nothing (the trap that bit move-refuse,
    and then me)."""
    from cmux_fleet import conformance as _cf
    return _cf


BERGS_SURFACE = "A63131E0-0000-0000-0000-000000000000"
BERGS_WS = "3B45A9C9-0000-0000-0000-000000000000"
MINE = "DEADBEEF-0000-0000-0000-000000000000"


def _sandbox(cf, calls=None):
    def fake_cmux(*args):
        if calls is not None:
            calls.append(args)
        return "workspace:99" if args[0] == "new-workspace" else "surface:99"
    return cf.Sandbox(fake_cmux, {"surface": {BERGS_SURFACE}, "workspace": {BERGS_WS},
                                  "label": {"cmux-advisor"}})


def test_it_CANNOT_close_a_live_fleet_members_surface(cf):
    """The one that matters. Berg's conductor, by uuid, straight at the destructive call."""
    sb = _sandbox(cf)
    with pytest.raises(cf.NotDisposable, match="LIVE FLEET MEMBER"):
        sb.close_surface(BERGS_SURFACE)


def test_it_CANNOT_close_a_live_fleet_members_workspace(cf):
    sb = _sandbox(cf)
    with pytest.raises(cf.NotDisposable, match="LIVE FLEET MEMBER"):
        sb.close_workspace(BERGS_WS)


def test_it_CANNOT_close_a_surface_it_merely_FOUND(cf):
    """Gate 1 on its own: not Berg's, not protected — just something it did not create. Still refused. "I
    did not make it" is sufficient grounds; it does not have to prove the thing is precious."""
    sb = _sandbox(cf)
    with pytest.raises(cf.NotDisposable, match="not created by this run"):
        sb.close_surface("11111111-2222-3333-4444-555555555555")


def test_it_CAN_close_what_it_made(cf):
    """Reachable-green: the gates must not be so tight that the suite cannot clean up after itself — an
    instrument that leaves orphans is its own kind of damage."""
    calls = []
    sb = _sandbox(cf, calls)
    sb.claim_surface(MINE)
    sb.close_surface(MINE)
    assert any(c[0] == "close-surface" for c in calls)


def test_an_id_that_COLLIDES_with_production_is_refused_at_CLAIM_time(cf):
    """Defence in depth. If a 'newly created' surface comes back with an id that is already a live fleet
    member's, the world is not sane — stop dead rather than reason about it."""
    sb = _sandbox(cf)
    with pytest.raises(cf.NotDisposable, match="ALREADY A LIVE FLEET MEMBER"):
        sb.claim_surface(BERGS_SURFACE)


def test_an_UNRESOLVABLE_create_is_never_owned(cf):
    """cmux prints a ref; the uuid only exists in the tree. If we cannot resolve it we cannot prove we own
    it — and a thing we cannot prove we own is a thing we may never destroy. It must not silently become ''
    and then match some other empty-string target."""
    sb = _sandbox(cf)
    with pytest.raises(cf.NotDisposable):
        sb.claim_surface("")


def test_the_gates_are_CASE_INSENSITIVE(cf):
    """cmux hands uuids back in mixed case in different places. A gate that compares raw strings would let a
    lowercase spelling of Berg's surface straight through — which is the misspelling class again."""
    sb = _sandbox(cf)
    with pytest.raises(cf.NotDisposable, match="LIVE FLEET MEMBER"):
        sb.close_surface(BERGS_SURFACE.lower())


# --- the structural guarantee: "incapable", not "careful" ------------------------------------------
def test_NO_destructive_cmux_call_exists_outside_the_sandbox():
    """The property the brief actually asked for. It is not enough that the checks are careful — there must
    be NO CODE PATH from an arbitrary uuid to a destroyed object. So: every `close-surface` /
    `close-workspace` in the whole module must live inside Sandbox, which is the only thing that can gate
    them. This catches a future check that decides to "just clean up" a surface it found lying around.
    """
    import ast
    import inspect
    from cmux_fleet import conformance as _cf

    tree = ast.parse(inspect.getsource(_cf))
    sandbox = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "Sandbox")
    inside = {id(n) for n in ast.walk(sandbox)}

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node.value in ("close-surface", "close-workspace") and id(node) not in inside:
            offenders.append(node.value)

    assert not offenders, (
        f"a destructive cmux verb {offenders} is invoked OUTSIDE conformance.Sandbox. Every destroy must go "
        f"through the sandbox's gates, or the suite is merely careful rather than incapable — and careful "
        f"is what kills one of Berg's agents at 2am.")


def test_the_structural_check_can_actually_FAIL():
    """Reachable-red: the rule above must be able to catch an offender, or it is decoration."""
    import ast
    bad = ast.parse('def cleanup(s):\n    _cmux("close-surface", "--surface", s)\n')
    strings = [n.value for n in ast.walk(bad)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert "close-surface" in strings          # <- exactly what the test above rejects outside Sandbox
