"""THE seat-home sync: one pass, both halves, called from one place.

A codex home needs two things from the fleet, and they were failing for the same reason — nobody owned
them. The CITIZENSHIP doc, because a codex worker loads no claude plugins and so boots knowing nothing
about the fleet it is a child of (including that it must report its own completion). And cmux's HOOK
WIRING, because without it the worker's Stop hook fires into a void and no completion ever reaches the
router. They shipped as two separate preflights on adjacent lines of `cmd_launch`, which is precisely how
the next one gets forgotten — so they are now ONE function, `codex_seat_sync`, called from ONE place.

The invariant these tests defend: **a home is synced or it is not — never half.** In particular, hooks are
never installed without trust being (re)written in the same pass, because an untrusted hook does not run
and does not say so.
"""
import os

import pytest

from cmux_fleet import providers as pv


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A codex home with a FAKE `cmux hooks codex install` that behaves like the real one: it writes
    hooks.json AND the trust entry, together, always."""
    h = tmp_path / "codex-home"
    h.mkdir()
    calls = []

    def fake_run(argv, **kw):
        import subprocess as sp
        calls.append({"argv": argv, "home": (kw.get("env") or {}).get("CODEX_HOME")})
        target = (kw.get("env") or {}).get("CODEX_HOME")
        hooks = os.path.join(target, "hooks.json")
        with open(hooks, "w") as f:
            f.write('{"hooks":{"Stop":[]}}')
        with open(os.path.join(target, "config.toml"), "a") as f:
            f.write(f'[hooks.state."{os.path.realpath(hooks)}:stop:0:0"]\ntrusted_hash = "sha256:aa"\n')
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pv.subprocess, "run", fake_run)
    return {"path": str(h), "calls": calls}


def test_one_pass_does_BOTH_halves(home):
    """The merge, in one assertion: a bare home comes out with its doc AND its hooks, from a single call."""
    r = pv.codex_seat_sync(home["path"])

    assert r.ok
    assert r.doc_status == "installed"
    assert "fleet peer-msg" in open(r.doc_path).read()
    assert r.hooks_ok and pv.codex_hooks_ok(home["path"])


def test_hooks_are_never_installed_WITHOUT_trust(home):
    """The landmine, pinned. An untrusted hook does not run and does not say so — so a sync that writes
    hooks.json and stops has produced a home that looks wired and is silent. `codex_hooks_ok` is the gate,
    and it demands both; a sync that could not achieve both must report NOT ok, never a cheerful half-pass."""
    r = pv.codex_seat_sync(home["path"])
    cfg = open(os.path.join(home["path"], "config.toml")).read()

    assert os.path.exists(os.path.join(home["path"], "hooks.json"))
    assert "trusted_hash" in cfg, "hooks were installed with no trust — they will silently never fire"
    assert r.hooks_ok is True


def test_a_synced_home_is_NOT_touched_again(home):
    """Idempotence is what makes sync-on-launch safe. The second call must not rewrite the doc and must not
    re-shell to cmux — a preflight that churns the operator's home on every launch is one they will rip out."""
    first = pv.codex_seat_sync(home["path"])
    mtime = os.stat(first.doc_path).st_mtime_ns
    home["calls"].clear()

    second = pv.codex_seat_sync(home["path"])

    assert second.ok and second.doc_status == "current"
    assert second.hooks_detail == "already wired"
    assert os.stat(first.doc_path).st_mtime_ns == mtime, "the doc was rewritten on a no-op sync"
    assert home["calls"] == [], "re-shelled to `cmux hooks codex install` on an already-wired home"


def test_the_sync_keys_on_THIS_home(tmp_path, home):
    """The whole point of a per-seat home. `cmux hooks codex install` picks its target from $CODEX_HOME, so
    a sync that leaked the ambient one would wire the seat you happened to be standing in and leave the seat
    you asked for severed — which is exactly the state sean-flat was found in."""
    other = str(tmp_path / "sean-flat")
    pv.codex_seat_sync(other)

    assert home["calls"][-1]["home"] == other
    assert pv.codex_hooks_ok(other)
    assert not pv.codex_hooks_ok(home["path"]), "syncing one seat wired a different one"


def test_a_hook_failure_does_not_block_the_doc_or_the_launch(tmp_path, monkeypatch):
    """FAIL-OPEN, but never fail-SILENT. If cmux is not on PATH the worker still launches — an under-equipped
    worker is still a worker — but the report must say plainly that its completions will not arrive."""
    monkeypatch.setattr(pv, "CMUX", "/nonexistent/cmux")
    r = pv.codex_seat_sync(str(tmp_path / "h"))

    assert r.doc_status == "installed" and "fleet peer-msg" in open(r.doc_path).read()
    assert r.ok is False and r.hooks_ok is False
    joined = " ".join(r.report())
    assert "NOT wired" in joined
    assert "nothing will tell its conductor when it finishes" in joined
    assert "cmux hooks codex install" in joined          # and how to fix it


def test_status_is_a_PURE_READ(tmp_path, home):
    """`--check` must write nothing: a codex home can hold the wrong account, and a run in it mints a device
    id that supersedes the seat you were only trying to inspect."""
    fresh = str(tmp_path / "untouched")
    r = pv.codex_seat_status(fresh)

    assert r.ok is False
    assert not os.path.exists(fresh), "a read-only status check CREATED the home"
    assert home["calls"] == [], "a read-only status check shelled out to the installer"


def test_a_pure_read_does_not_speak_in_the_PAST_TENSE(tmp_path, home):
    """A --check that reports `citizenship=installed` while writing nothing is claiming credit for work it
    did not do — and an operator who reads it and moves on has been told the opposite of the truth. The same
    class of lie as a store read documented as "THE liveness answer". A read says what IS there."""
    r = pv.codex_seat_status(str(tmp_path / "bare"))
    assert r.doc_status == "MISSING"
    assert r.doc_status not in ("installed", "updated"), "a read-only check claimed it had written the doc"

    pv.codex_seat_sync(home["path"])                      # ...and a real sync still says what it DID
    assert pv.codex_seat_status(home["path"]).doc_status == "current"


def test_a_healthy_home_reports_NOTHING_at_launch(home):
    """The preflight runs on every codex launch. A home that was already correct must print not one line —
    a check that narrates its own no-ops is a check people learn to scroll past, and then miss the one time
    it says something."""
    pv.codex_seat_sync(home["path"])
    assert pv.codex_seat_sync(home["path"]).report() == []
