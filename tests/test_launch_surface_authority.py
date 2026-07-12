"""INVARIANT I5 — THE LAUNCHED SURFACE IS AUTHORITATIVE.

The 2026-07-11 mis-binding (cmux-advisor). `fleet launch cmux-dev --label doctor-stall` created and
launched onto surface E4CED20C…, then printed:

    [fleet] note: session bound to surface 3F2CDDD4-…, not the launched E4CED20C-…
            -- reconciled via AGENT_LABEL/cwd match in the hook store

3F2CDDD4 was an unrelated idle staging shell (`~/builds/staging-home`, ttys022), idling since ~Jul 4.
The registry row for `doctor-stall` was bound to it, and since a conductor drives the REGISTRY's surface,
`fleet drive-child doctor-stall` typed an entire brief into a bare zsh, which wedged at a `dquote>`
prompt. The real agent sat idle with no instructions. It was luck that the foreign surface was an idle
orphan and not a live session — the failure mode is "your prompt is delivered to an arbitrary terminal".

The message's account of itself was false, and that matters more than the incident:
  * the AGENT_LABEL arm CANNOT FIRE. Fleet passes AGENT_LABEL as an ENV VAR; cmux records
    `launchCommand` as a structured object holding the exec'd binary's ARGV, and argv never contains the
    `KEY=val` prefixes (the shell consumes them into the environment). So the precise arm is structurally
    dead and every discovery silently degrades to the loose one. (test_agent_label_arm_is_dead_on_a_
    structured_launchcommand pins this — it is the root of the loose matching.)
  * the loose arm matches on CWD — not an identity; every shell in the worktree shares it — and hands
    back the matched record's `surfaceId`, a hook-time ATTRIBUTION that can be wrong. It was: cmux had
    filed the freshly-launched agent's session under the staging shell's surface.

So the rule these tests pin: fleet CREATED the surface and delivered the process onto it, and cmux told
the process so itself (CMUX_SURFACE_ID in its env). A hook-store record may FILL IN a missing session id;
it may never CONTRADICT the surface. _bind_launched_session returns (ws, surf) unchanged, always.
"""
import pytest

from cmux_fleet import cli as fleet
from cmux_fleet import resolve as rs
from cmux_fleet import state as fs

# the incident's real ids
WS = "0BE1A5ED-0000-4000-8000-00000000000W"
LAUNCHED = "E4CED20C-DBDA-41D2-B4E6-B5D5E2EE13BD"      # the surface fleet created + launched onto
FOREIGN = "3F2CDDD4-BD68-4057-AC1A-2ED4B3E9A326"       # the idle staging shell the registry got pointed at
SID = "6ce0c46e-bcc6-4489-9154-7d019cb18f96"
LABEL = "doctor-stall"
CWD = "/w/cmux-fleet/.worktrees/doctor-stall"
PID = 78004

# The hook store EXACTLY as it was during the incident: cmux filed the live agent's session under the
# FOREIGN surface (the field is a hook-time attribution, and it was wrong), and pointed
# activeSessionsBySurface[FOREIGN] back at that same session. Nothing in the store mentions LAUNCHED, so
# poll_session(LAUNCHED) comes up empty and the reconcile path is entered. Note launchCommand is the
# STRUCTURED form — argv only, no environment — which is why AGENT_LABEL is nowhere in it.
LYING_STORE = {
    "sessions": {SID: {
        "sessionId": SID, "surfaceId": FOREIGN, "pid": PID, "agentLifecycle": "running",
        "cwd": CWD,
        "launchCommand": {"arguments": ["/Users/berg/.local/bin/claude", "--model", "claude-opus-4-8[1m]"],
                          "executablePath": "/Users/berg/.local/bin/claude",
                          "workingDirectory": CWD, "launcher": "claude"},
    }},
    "activeSessionsBySurface": {FOREIGN: {"sessionId": SID}},
}


@pytest.fixture
def launched(monkeypatch):
    """A launch that delivered fine but whose session the store attributes elsewhere: the direct bind
    poll on the LAUNCHED surface sees nothing (the store knows the session only under FOREIGN).

    Rebinds our `rs`/`fs` handles to the CURRENT modules first. test_features._reset_pkg_modules() drops
    cmux_fleet.{config,state,features} from sys.modules, and the code under test resolves `resolve`/
    `state` from sys.modules at CALL time — so patching our import-time handles would silently land on a
    stale module object and never reach it (the same hazard test_fleet_doctor's _sync fixture documents).
    Suite-order-dependent, and it bit exactly this file."""
    global rs, fs
    import cmux_fleet.resolve as _rs
    import cmux_fleet.state as _fs
    rs, fs = _rs, _fs
    monkeypatch.setattr(fleet, "cmuxq", lambda *a: "OK")
    monkeypatch.setattr(fleet, "poll_session", lambda surf, timeout=60: SID if surf == FOREIGN else "")
    monkeypatch.setattr(fleet, "_resume_menu_visible", lambda surf: False)
    monkeypatch.setattr(fleet, "_agent_surfaced", lambda surf: False)
    monkeypatch.setattr(fleet.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fleet, "_store", lambda: LYING_STORE)
    monkeypatch.setattr(fs, "pid_alive", lambda pid: pid == PID)


def _bind():
    # timeout=0: the bind poll on LAUNCHED can never succeed here (the store filed the session under
    # FOREIGN), and we are testing the reconcile TAIL, not the wait. A real timeout would just busy-wait.
    return fleet._bind_launched_session(WS, LAUNCHED, f"cd {CWD} && AGENT_LABEL={LABEL} claude",
                                        "claude", LABEL, CWD, [], lazy=False, timeout=0)


def test_launched_surface_survives_a_lying_hook_store(launched, monkeypatch):
    """THE REGRESSION. The store says the session lives on FOREIGN; the agent's own env says LAUNCHED.
    The env wins, the surface never moves, and the missing session id is filled in."""
    monkeypatch.setattr(rs, "proc_ident", lambda pid: (LAUNCHED, LABEL))   # cmux told the process itself
    ws, surf, sid = _bind()
    assert surf == LAUNCHED, "the registry was bound to a surface fleet never launched onto"
    assert ws == WS, "the workspace was re-resolved to follow a surface we never launched onto"
    assert sid == SID, "the session id was recoverable from the live process and should be filled in"


def test_never_adopts_a_session_running_on_another_surface(launched, monkeypatch):
    """The staging-shell case. A record matches our cwd (a worktree is shared by anything sitting in it)
    but its PROCESS says it is on another surface. cwd is not an identity: adopt nothing, move nothing."""
    monkeypatch.setattr(rs, "proc_ident", lambda pid: (FOREIGN, ""))       # genuinely someone else's seat
    ws, surf, sid = _bind()
    assert (ws, surf) == (WS, LAUNCHED)
    assert sid == "", "adopted a session whose process is demonstrably on a different surface"


def test_never_adopts_another_fleet_members_session_on_our_surface(launched, monkeypatch):
    """Belt and braces: right surface, wrong AGENT_LABEL -> not ours."""
    monkeypatch.setattr(rs, "proc_ident", lambda pid: (LAUNCHED, "some-other-agent"))
    ws, surf, sid = _bind()
    assert (ws, surf, sid) == (WS, LAUNCHED, "")


def test_no_env_proof_leaves_the_sid_empty_rather_than_guessing(launched, monkeypatch):
    """When the env can't be read (`ps` unavailable, process gone), we do NOT fall back to a looser
    signal. An empty sid is a RECOVERABLE gap -- cmd_launch aborts without registering, leaves the
    surface up, and signposts `fleet register <label> --surface <launched>`. A registry row pointing at
    someone else's terminal is not recoverable, because a conductor will type into it."""
    monkeypatch.setattr(rs, "proc_ident", lambda pid: ("", ""))
    ws, surf, sid = _bind()
    assert (ws, surf, sid) == (WS, LAUNCHED, "")


def test_a_dead_pid_is_never_adopted(launched, monkeypatch):
    """A dead process cannot be the agent we just started, whatever the store says about it."""
    monkeypatch.setattr(fs, "pid_alive", lambda pid: False)
    monkeypatch.setattr(rs, "proc_ident", lambda pid: pytest.fail("must not read the env of a dead pid"))
    ws, surf, sid = _bind()
    assert (ws, surf, sid) == (WS, LAUNCHED, "")


@pytest.mark.parametrize("ident", [(LAUNCHED, LABEL), (FOREIGN, ""), (FOREIGN, LABEL), ("", ""),
                                   (LAUNCHED.lower(), LABEL)])
def test_the_surface_is_never_reassigned_whatever_the_store_says(launched, monkeypatch, ident):
    """I5 stated directly, over a matrix of hostile stores/envs: whatever we learn afterwards, the
    (ws, surf) we launched onto come back UNCHANGED. This is the invariant; the sid is the only thing
    a reconciliation may ever fill in."""
    monkeypatch.setattr(rs, "proc_ident", lambda pid: ident)
    ws, surf, sid = _bind()
    assert (ws, surf) == (WS, LAUNCHED)


def test_agent_label_arm_is_dead_on_a_structured_launchcommand(monkeypatch):
    """The ROOT of the loose matching, pinned. cmux records launchCommand as argv (a structured object);
    fleet passes AGENT_LABEL as an ENV var, which argv by construction excludes. So _discover_surface_for's
    "exact AGENT_LABEL match wins outright" arm cannot fire on this build, and discovery silently degrades
    to the cwd arm -- which is what handed the launch a foreign surface. `fleet register` (the other
    caller) still relies on that arm; see REPORT-doctor-stall-discrimination.md for the follow-up."""
    monkeypatch.setattr(fleet, "_store", lambda: LYING_STORE)          # hermetic: never the host's store
    rec = LYING_STORE["sessions"][SID]
    assert "AGENT_LABEL" not in fleet._launchcmd(rec), "the label arm would fire -- premise changed"
    # ...and so discovery falls through to cwd, which resolves to the record's (wrong) surfaceId.
    assert fleet._discover_surface_for(LABEL, CWD)[0] != LAUNCHED
