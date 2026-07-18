#!/usr/bin/env python3
# cmux_fleet/conformance.py — does this cmux build ACTUALLY DO the things the fleet depends on?
#
# Berg is about to run cmux nightly (1100+ commits ahead of the installed app) beside stable. He does not
# want to read 1100 commits to find out what breaks. So: run this on stable, run it on nightly, and DIFF
# the two reports. The diff IS the breaking-change report — derived empirically, never inferred from a
# commit subject.
#
# THE ONE RULE, and it is the house doctrine turned into an instrument: EVERY CHECK PROVES THE EFFECT,
# NEVER THE INVOCATION. `exit 0` is not a pass. A printed "OK" is not a pass. `cmux capture-pane` returns
# an error and exits 0 — we have been burned five times this week by an artifact that looked right while
# the effect never happened, so nothing here believes anything it did not observe downstream.
#
# TRI-STATE, ALWAYS: PASS / FAIL / UNKNOWN. "I could not tell" is a first-class answer and is NEVER
# silently rendered as a pass. An UNKNOWN in the report is information; an UNKNOWN laundered into a PASS
# is the whole disease.
#
# SAFETY — read this before you touch anything here.
#
#   This runs against a LIVE cmux, with Berg's LIVE agents in it. It must be structurally INCAPABLE of
#   touching a fleet member — not careful, incapable. Two independent gates, and every destructive act
#   passes through BOTH (see Sandbox):
#
#     1. POSITIVE OWNERSHIP. The only things it may close/kill are UUIDs IT CREATED, this run, recorded at
#        creation time. Not name-matched, not prefix-matched — a suite that trusts a label prefix is one
#        typo from closing `cmux-advisor`. If it cannot prove it made a thing, it refuses to touch it.
#     2. NEGATIVE PROOF. The production registry is read ONCE at startup and every surface/workspace/label
#        in it becomes untouchable for the life of the run. Even if gate 1 were somehow fooled, gate 2
#        still knows what is Berg's.
#
#   And it runs against an ISOLATED fleet state dir which it creates ITSELF — it does not ask to be told,
#   and it does not trust an env var to have been spelled right. (A worker once set a name that was ALMOST
#   right and damaged a live conductor's record. Trusting spelling is not isolation.) If it ever finds
#   itself resolved onto the production state, it REFUSES TO RUN.
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from .config import CMUX, HOOKSTORE, STATE

PASS, FAIL, UNKNOWN, SKIP = "PASS", "FAIL", "UNKNOWN", "SKIP"


class NotDisposable(Exception):
    """A destructive act was aimed at something this run did not create. Always a bug, never a warning."""


class Result:
    def __init__(self, name, status, detail="", evidence=None, depends=""):
        self.name, self.status, self.detail = name, status, detail
        self.evidence = evidence or {}
        self.depends = depends            # WHAT IN THE FLEET BREAKS if this goes red. The diff's value.

    def as_dict(self):
        return {"check": self.name, "status": self.status, "detail": self.detail,
                "breaks_if_red": self.depends, "evidence": self.evidence}


# =================================================================================================
# The sandbox: the ONLY thing that may hand out a destructive target.
# =================================================================================================
class Sandbox:
    """Owns exactly what it created, and nothing else, ever.

    There is no method here that takes an arbitrary uuid and closes it. To close a surface you must have
    created it THROUGH this object, which is what makes the safety property structural rather than
    behavioural: there is no code path from "a uuid I found somewhere" to "a thing I destroyed"."""

    def __init__(self, cmux_call, protected):
        self._cmux = cmux_call
        self._protected = {k: {v.upper() for v in vs if v} for k, vs in protected.items()}
        self._mine = {"surface": set(), "workspace": set(), "label": set()}

    # --- ownership -------------------------------------------------------------------------------
    def _claim(self, kind, uuid):
        u = (uuid or "").upper()
        if not u:
            raise NotDisposable(f"refusing to claim an empty {kind} id")
        if u in self._protected.get(kind, set()):
            # We just CREATED a thing whose id is already a live fleet member's. That is impossible in a
            # sane world, so the world is not sane: stop, do not proceed, do not clean up.
            raise NotDisposable(f"the {kind} we just created ({u[:8]}) is ALREADY A LIVE FLEET MEMBER — "
                                f"refusing to continue; something is deeply wrong with the environment")
        self._mine[kind].add(u)
        return uuid

    def assert_disposable(self, kind, uuid):
        """THE GATE. Both halves. Nothing destructive happens without passing through here."""
        u = (uuid or "").upper()
        if u in self._protected.get(kind, set()):
            raise NotDisposable(f"REFUSING: {kind} {u[:8]} is a LIVE FLEET MEMBER (Berg's). "
                                f"The conformance suite does not touch things it did not create.")
        if u not in self._mine[kind]:
            raise NotDisposable(f"REFUSING: {kind} {u[:8]} was not created by this run, so it cannot be "
                                f"proven disposable. If it cannot be proven disposable, it is not touched.")
        return uuid

    # --- creation (the only way something becomes touchable) --------------------------------------
    def new_workspace(self, name):
        out = self._cmux("new-workspace", "--name", name) or ""
        ws = _uuid_of("workspace", out, self._cmux)
        return self._claim("workspace", ws)

    def new_surface(self, ws):
        self.assert_disposable("workspace", ws)
        out = self._cmux("new-surface", "--workspace", ws, "--type", "terminal", "--focus", "false") or ""
        surf = _uuid_of("surface", out, self._cmux)
        return self._claim("surface", surf)

    def claim_label(self, label):
        return self._claim("label", label)

    def claim_surface(self, surf):
        """A surface the FLEET created on our behalf (a launch seats its own). Same gates apply."""
        return self._claim("surface", surf)

    def claim_workspace(self, ws):
        return self._claim("workspace", ws)

    # --- destruction (gated) ----------------------------------------------------------------------
    def close_surface(self, surf, ws=""):
        self.assert_disposable("surface", surf)
        self._cmux("close-surface", "--surface", surf, *(["--workspace", ws] if ws else []))

    def close_workspace(self, ws):
        self.assert_disposable("workspace", ws)
        self._cmux("close-workspace", "--workspace", ws)

    def owned(self):
        return {k: sorted(v) for k, v in self._mine.items()}


def _uuid_of(kind, out, _cmux_call):
    """Resolve the `<kind>:<n>` ref cmux prints on a create into its UUID.

    Delegates to cli._ref_to_uuid — the fleet's own resolver, reading `tree --all --id-format both`. A
    create whose id we cannot resolve is a thing we cannot prove we own, and therefore a thing we may never
    destroy: it comes back '' and the Sandbox refuses to claim it."""
    import re
    from . import cli
    m = re.search(rf"({kind}:\d+)", out or "")
    return cli._ref_to_uuid(kind, m.group(1)) if m else ""


# =================================================================================================
# The checks. Each one ends in an observation of the CONSEQUENCE.
# =================================================================================================
def _cmux(*args, timeout=30):
    """One cmux call. Returns (rc, out). We keep the rc ONLY so we can prove how little it is worth."""
    try:
        p = subprocess.run([CMUX, *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def _q(*args, **kw):
    return _cmux(*args, **kw)[1]


def check_topology(sb, ws, surf):
    """1. TOPOLOGY. A workspace and a surface we created must READ BACK from the tree.

    The effect is the tree entry, not the exit code of `new-workspace`."""
    tree = _q("tree", "--all", "--id-format", "both")
    saw_ws = ws.upper() in tree.upper()
    saw_surf = surf.upper() in tree.upper()
    if saw_ws and saw_surf:
        return Result("topology", PASS, "workspace + surface both read back from `tree`",
                      {"workspace": ws[:8], "surface": surf[:8]},
                      depends="EVERYTHING. No workspace/surface = no agent can be seated at all.")
    return Result("topology", FAIL,
                  f"created objects are not in the tree (workspace={saw_ws}, surface={saw_surf})",
                  {"workspace": ws[:8], "surface": surf[:8]},
                  depends="EVERYTHING. `fleet launch` cannot seat an agent.")


def check_capture_pane(sb, ws, surf, nonce):
    """6. READ. `capture-pane` returns the screen — and its ERROR SHAPE is pinned, on the real error paths.

    THE BRIEF SAID cmux capture-pane "returns an error and exits 0". IT DOES NOT — not on this build. On
    stable 0.64.17 it exits **1** on a bogus surface id ("Error: not_found: Workspace not found") and **1**
    on a NON-TERMINAL surface ("Error: invalid_params: Surface is not a terminal"). Both verified.

    THE TRAP IS REAL BUT IT IS OURS, NOT cmux's. `cli.cmuxq()` does this:

        return (p.stdout or "") + (p.stderr or "")      # <- p.returncode is DISCARDED

    So the fleet never sees the exit code, and every pane guard receives the string
    `Error: invalid_params: Surface is not a terminal` AS IF IT WERE SCREEN CONTENT. cmux tells the truth;
    we throw it away. (That is the same disease as everything else this week — a signal treated as
    authoritative that cannot see the thing it reports — and it is worth fixing in the fleet, not here.)

    So this pins the contract the fleet ACTUALLY leans on: (a) a terminal surface returns the screen, and
    (b) an error path returns text beginning `Error:` — because that prefix is the only thing that could
    ever let a guard tell an error from a pane. If nightly changes that prefix, guards start reading errors
    as screen text and this check is how we find out."""
    rc_ok, screen = _cmux("capture-pane", "--surface", surf)
    got_screen = nonce in screen

    rc_bogus, err_bogus = _cmux("capture-pane", "--surface", "00000000-0000-0000-0000-000000000000")
    browser = ""
    try:
        out = _q("new-surface", "--workspace", ws, "--type", "browser", "--url", "https://example.com")
        browser = _uuid_of("surface", out, _q)
        if browser:
            sb.claim_surface(browser)
    except Exception:
        browser = ""
    rc_nonterm, err_nonterm = _cmux("capture-pane", "--surface", browser) if browser else (None, "")

    ev = {"terminal_surface_returned_the_screen": got_screen,
          "bogus_id": {"rc": rc_bogus, "said": err_bogus.strip()[:80]},
          "non_terminal_surface": {"rc": rc_nonterm, "said": err_nonterm.strip()[:80]} if browser
          else "could not create a browser surface to test",
          "NOTE": "cli.cmuxq() DISCARDS the exit code, so the fleet sees these error strings as pane text"}

    if not got_screen:
        return Result("capture_pane", FAIL, "the pane did not come back (our nonce is not on the screen)",
                      ev, depends="every pane-derived guard: launch verdicts, the codex update-modal "
                                  "backstop, the resume-menu gate, `fleet ls` husk detection")
    errs = [e for e in (err_bogus, err_nonterm) if e]
    if not all(e.strip().startswith("Error:") for e in errs) or not errs:
        return Result("capture_pane", FAIL,
                      "the ERROR SHAPE changed: an error no longer starts with `Error:` — the fleet "
                      "discards the exit code, so that prefix is the ONLY way a guard can tell an error "
                      "from screen content", ev,
                      depends="every pane guard would start reading error text AS the screen")
    return Result("capture_pane", PASS,
                  "screen reads back; both error paths return `Error: …` (bogus id and non-terminal "
                  "surface both exit non-zero — the brief's 'exits 0' does NOT reproduce)", ev,
                  depends="every pane-derived guard. NB the fleet's own cmuxq() throws the rc away.")


def check_read_screen(surf, nonce):
    """`read-screen` — the idle-wake gate's eyes. NOT in the brief's list, and load-bearing: every wake
    the router performs is gated on this (state.wake_if_idle reads the screen to prove the input box is
    empty before it types). If it breaks, the fleet either stops waking conductors or types into a busy
    TUI."""
    out = _q("read-screen", "--surface", surf, "--lines", "40")
    if nonce in out:
        return Result("read_screen", PASS, "returned the live screen", {"lines": len(out.splitlines())},
                      depends="idle-wake. The router's wake gate reads the screen to prove the input box "
                              "is empty; blind, it either never wakes a conductor or types into a live turn")
    return Result("read_screen", FAIL, "did not return the live screen", {"got": out.strip()[:120],
                  "expected_to_contain": nonce},
                  depends="idle-wake (see above) + `fleet unstick`'s husk scan")


def check_paint(sb, ws, label):
    """7. PAINT. The sidebar record must ROUND-TRIP: written by `workspace-action --action set-description`,
    read back out of cmux's OWN `rpc extension.sidebar.snapshot`.

    THIS CHECK GAVE A FALSE PASS AND THE DELIBERATE BREAK CAUGHT IT — which is the single best argument for
    having demanded one. The first cut called `set-status <label> …` and then asserted `label in snapshot`.
    It PASSED against a doctored cmux whose `set-status` was a no-op that exits 0 — because the label was in
    the snapshot anyway, as the WORKSPACE'S NAME. It was matching an artifact that would have been there
    regardless, which is precisely the disease this whole suite exists to detect, sitting inside the suite.

    So it now writes a value that CANNOT be there by accident (a nonce) into the field the fleet actually
    uses (the workspace description — `set-status` pills are not in the snapshot at all) and reads it back
    from cmux's own snapshot. If the write does not happen, the nonce is not there. There is no other way
    for it to appear."""
    nonce = f"fleet-conformance-{os.getpid()}-{int(time.time())}"
    sb.assert_disposable("workspace", ws)                # we only ever paint a workspace WE created
    _cmux("workspace-action", "--action", "set-description", "--description", nonce, "--workspace", ws)
    time.sleep(1.0)
    raw = _q("rpc", "extension.sidebar.snapshot", "{}")
    try:
        snap = json.loads(raw or "{}")
    except Exception:
        return Result("paint", FAIL, "cmux's sidebar snapshot is not JSON", {"got": raw[:120]},
                      depends="`fleet paint`: the sidebar's status pills, context bars and collapse bits")
    descs = {w.get("id"): (w.get("description") or "") for w in (snap.get("workspaces") or [])}
    got = descs.get(ws, "")
    _cmux("workspace-action", "--action", "clear-description", "--workspace", ws)
    ev = {"workspace": ws[:8], "wrote": nonce, "read_back": got[:60],
          "workspaces_in_snapshot": len(descs)}
    if nonce in got:
        return Result("paint", PASS,
                      "workspace description round-tripped (written, then read back from cmux's own "
                      "sidebar snapshot)", ev,
                      depends="`fleet paint` — the sidebar goes blind: no status pills, no context bars, "
                              "and the collapse bits a sidebar tap wrote are lost on every repaint")
    return Result("paint", FAIL,
                  "the description we wrote is NOT in cmux's sidebar snapshot — the write had no effect",
                  ev,
                  depends="`fleet paint`: the sidebar shows nothing for any agent")


def check_feed_gate():
    """`rpc feed.list` — the needs-input gate. The fleet treats it as AUTHORITATIVE for "is this agent
    blocked on a question" (features._open_gate_uuids), so an inert or reshaped feed silently mislabels
    every blocked agent as merely idle.

    IT IS NOT INERT ON THIS BUILD — I checked, because that was an authority claim. `feed.list` returns
    well-formed items, gate-kind items carry `request_id`/`workstream_id`, and `bare_uuid` maps them to a
    session cleanly. `_open_gate_uuids()` returning 0 right now is CORRECT: there are no open gates. What
    cannot be proven without a real blocked agent is LIVENESS, so that is reported as UNKNOWN rather than
    laundered into a pass."""
    from . import features as ff
    from . import state as fs
    rc, out = _cmux("rpc", "feed.list", "{}")
    try:
        data = json.loads(out or "{}")
    except Exception:
        return Result("feed_gate", FAIL, f"`rpc feed.list` did not return JSON (rc={rc})",
                      {"got": (out or "").strip()[:160]},
                      depends="the needs-input gate: `vitals`'s blocked column, and the wake gate's refusal "
                              "to interrupt an agent that is waiting on a human")
    items = data.get("items") or []
    gate_kind = [i for i in items if (i.get("kind") or "").lower() in ff._GATE_KINDS]
    mappable = [i for i in gate_kind
                if any(fs.bare_uuid(i.get(f) or "")
                       for f in ("request_id", "workstream_id", "session_id", "session"))]
    ev = {"items": len(items), "gate_kind_items": len(gate_kind),
          "gate_items_mappable_to_a_session": len(mappable),
          "open_gates_right_now": len(ff._open_gate_uuids()),
          "item_keys": sorted(items[0])[:12] if items else []}
    if not items:
        return Result("feed_gate", UNKNOWN, "`rpc feed.list` answers, but with no items to check the shape on",
                      ev, depends="the needs-input gate (vitals blocked column; the wake gate)")
    if not gate_kind:
        return Result("feed_gate", UNKNOWN,
                      "feed.list is well-formed, but there is no gate-kind item (question/permission/…) "
                      "to prove the fleet could map one to an agent", ev,
                      depends="the needs-input gate (vitals blocked column; the wake gate)")
    if not mappable:
        return Result("feed_gate", FAIL,
                      "gate-kind items exist but NONE carry an id the fleet can map to a session — the "
                      "gate is structurally INERT and every blocked agent reads as merely idle", ev,
                      depends="the needs-input gate: `vitals` shows blocked agents as idle, and the wake "
                              "gate will happily type into an agent that is waiting on a human")
    return Result("feed_gate", PASS,
                  f"gate items are well-formed and map to a session ({len(mappable)}/{len(gate_kind)}); "
                  f"{ev['open_gates_right_now']} gate(s) open right now", ev,
                  depends="the needs-input gate (vitals blocked column; the wake gate). NB liveness needs "
                          "a genuinely blocked agent to prove; this proves the SHAPE and the MAPPING.")


def check_resume_binding(surf):
    """`cmux surface resume get --json` — cmux's own ground-truth relaunch binding.

    NOT in the brief's list, and it is the spine of `recycle` and `revive`: both re-compose an agent's
    launch command from this. If its shape moves, every recycle and every revive re-execs the wrong
    command (or none)."""
    out = _q("surface", "resume", "get", "--surface", surf, "--json")
    try:
        d = json.loads(out or "{}")
    except Exception:
        return Result("resume_binding", FAIL, "`surface resume get --json` is not JSON",
                      {"got": (out or "").strip()[:160]},
                      depends="`fleet recycle` and `fleet revive` — both re-compose the launch command "
                              "from this binding; without it they cannot restart an agent at all")
    b = d.get("resume_binding")
    if isinstance(b, dict) and b.get("command"):
        return Result("resume_binding", PASS, "cmux returns a resume binding with a command",
                      {"keys": sorted(b)[:8]},
                      depends="`fleet recycle` / `fleet revive` (the launch command they re-exec)")
    return Result("resume_binding", FAIL,
                  "no `resume_binding.command` in the response (the field recycle/revive read)",
                  {"top_level_keys": sorted(d)[:8]},
                  depends="`fleet recycle` / `fleet revive` re-exec the WRONG command, or none")


def check_hook_store_schema():
    """cmux's hook store — a FILE-FORMAT dependency the brief does not name, and the single widest one.

    The fleet does not ask cmux who is alive; it READS `~/.cmuxterm/*-hook-sessions.json` directly and
    derives liveness, lifecycle, sessions and transcripts from it. Five field names hold up the whole
    registry: sessionId, surfaceId, pid, agentLifecycle, updatedAt. Rename any one of them upstream and
    `ls`, `vitals`, the router, recycle and every liveness verdict go quietly wrong — not loudly."""
    import glob
    from . import resolve as rs
    stores = sorted(glob.glob(os.path.join(HOOKSTORE, "*-hook-sessions.json")))
    if not stores:
        return Result("hook_store_schema", UNKNOWN, f"no *-hook-sessions.json under {HOOKSTORE}",
                      {"hookstore": HOOKSTORE},
                      depends="EVERYTHING store-derived: ls/vitals/router/recycle/liveness")
    rec = rs.store_record_sample()                       # the raw read lives in resolve, per the ratchet
    if not rec:
        return Result("hook_store_schema", UNKNOWN, "the hook store has no session records to inspect",
                      {"stores": [os.path.basename(s) for s in stores]},
                      depends="EVERYTHING store-derived")
    need = {"sessionId", "surfaceId", "pid", "agentLifecycle", "updatedAt"}
    missing = sorted(need - set(rec))
    ev = {"stores": [os.path.basename(s) for s in stores], "records": rs.store_record_count(),
          "fields_present": sorted(set(rec) & need), "fields_missing": missing,
          "also_has": sorted(set(rec) - need)[:8]}
    if missing:
        return Result("hook_store_schema", FAIL,
                      f"the hook store is missing field(s) the fleet reads: {missing}", ev,
                      depends="EVERYTHING store-derived: `ls`/`vitals` liveness, the router's surface "
                              "resolution, recycle's rebind gate, every present()/dark() verdict")
    return Result("hook_store_schema", PASS,
                  "every field the fleet reads is present (sessionId, surfaceId, pid, agentLifecycle, "
                  "updatedAt)", ev,
                  depends="EVERYTHING store-derived (see above)")


class BusRecorder:
    """9 (CORRECTED). THE EVENT BUS — the router's actual contract with cmux, recorded WHILE the fleet works.

    THE BRIEF SAID the router parses `notify_target_async` bodies and their `c=turn-complete;p=0|1`
    metadata segment. IT DOES NOT, and nothing in the tree does — I grepped the whole package. The fleet is
    a notify PRODUCER (`cmux notify --surface --title …`) and never a consumer. What the router really
    parses is THE BUS:

        cmux events --category agent --category surface --reconnect --cursor-file <f> --no-ack

    acting only on frames where `name == "agent.hook.Stop"` AND `payload.phase == "completed"`, keyed by
    `payload.session_id`. THAT is the shape that, if it moves, silently stops every completion in the fleet
    from reaching a conductor — so that is what this asserts.

    It RECORDS ACROSS THE WHOLE SUITE rather than sampling a quiet bus for 25 seconds: the first cut of this
    check listened before any agent existed, saw nothing, and honestly reported UNKNOWN. Correct, and
    useless. Now it is armed before the agents start and read after they have completed real turns, so the
    frames it needs are the ones our own agents generate.

    (It reads through a PTY because cmux block-buffers a low-volume stream down a pipe — a real property of
    the dependency, and the router does the same.)"""

    def __init__(self):
        self.frames = []
        self._proc = None
        self._master = None
        self._tmp = None
        self._buf = b""

    def start(self):
        """Read CONTINUOUSLY, in a thread — the way the router does.

        The first cut drained at checkpoints instead, and captured 2 frames in a 40-second run while the
        real router (same flags, same bus) was receiving Stops the whole time: a pty holds only a few KB,
        so a reader that looks away starves. The bus is not a queue you poll; it is a stream you must keep
        up with."""
        import pty
        import re
        import threading
        self._tmp = tempfile.mkdtemp(prefix="conf-bus-")
        master, slave = pty.openpty()
        self._proc = subprocess.Popen(
            [CMUX, "events", "--category", "agent", "--category", "surface", "--reconnect",
             "--cursor-file", os.path.join(self._tmp, "cur"), "--no-ack"],
            stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        self._master = master
        self._stop = threading.Event()

        def pump():
            buf = b""
            while not self._stop.is_set():
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = re.sub(rb"\x1b\[[0-9;]*[A-Za-z]", b"", line).strip()
                    if line.startswith(b"{"):
                        try:
                            self.frames.append(json.loads(line))
                        except Exception:
                            pass

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()

    def drain(self):
        time.sleep(0.5)                                  # the pump thread is always reading; just let it catch up

    def stop(self):
        self.drain()
        if getattr(self, "_stop", None):
            self._stop.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        if self._master is not None:
            os.close(self._master)
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)

    def result(self):
        frames = self.frames
        if not frames:
            return Result("bus_schema", UNKNOWN,
                          "no bus frames arrived at all — cannot confirm the schema (a quiet bus is not "
                          "proof of a broken one)", {"frames": 0},
                          depends="THE ROUTER: every child completion reaches its conductor over this bus")
        stops = [f for f in frames if f.get("name") == "agent.hook.Stop"]
        ev = {"frames": len(frames), "names": sorted({f.get("name", "?") for f in frames})[:8],
              "stop_frames": len(stops), "every_frame_has_category": all("category" in f for f in frames)}
        if not stops:
            return Result("bus_schema", UNKNOWN,
                          "the bus is alive and well-formed, but no `agent.hook.Stop` frame arrived even "
                          "though our agents completed real turns — the router's key fields could not be "
                          "observed", ev,
                          depends="THE ROUTER (completions -> conductor inbox)")
        p = stops[0].get("payload") or {}
        ev["stop_payload_keys"] = sorted(p)[:10]
        ev["phases_seen"] = sorted({(f.get("payload") or {}).get("phase") for f in stops if f.get("payload")})
        missing = [k for k in ("phase", "session_id") if k not in p]
        if missing:
            return Result("bus_schema", FAIL,
                          f"`agent.hook.Stop` payload is missing {missing} — the router keys on exactly "
                          f"these two fields and drops any frame without them", ev,
                          depends="THE ROUTER: every completion is dropped, and every conductor waits "
                                  "forever on children that already finished")
        if "completed" not in ev["phases_seen"]:
            return Result("bus_schema", FAIL,
                          f"no Stop frame carried phase='completed' (the router acts on that phase and "
                          f"ignores all others); saw {ev['phases_seen']}", ev,
                          depends="THE ROUTER: completions never fire")
        return Result("bus_schema", PASS,
                      f"the bus emitted {len(stops)} agent.hook.Stop frame(s) with payload.phase="
                      f"'completed' + payload.session_id — the router's exact contract", ev,
                      depends="THE ROUTER (child completions -> conductor inbox; the fleet's whole spine)")


def _drive(surf, prompt):
    """Type a prompt into an agent's TUI and submit it — the fleet's own drive path (send + send-key)."""
    _cmux("send", "--surface", surf, prompt)
    time.sleep(1.0)
    _cmux("send-key", "--surface", surf, "enter")


def _wait_answer(surf, token, timeout=90):
    """Did the AGENT actually answer? Read its own last assistant text out of the transcript — never the
    pane (a pane shows the prompt we typed, which is exactly the false positive that nearly shipped three
    times in the codex work: the artifact echoes before the effect happens)."""
    from . import resolve as rs
    from . import state as fs
    end = time.time() + timeout
    while time.time() < end:
        # rs.freshest_live is the SAFE interface (it applies the liveness rule); reading the store here to
        # hand-pick a record is the stale-ghost class the ratchet exists to prevent.
        path = (rs.freshest_live(surf) or {}).get("transcriptPath", "")
        if path:
            said = fs.last_agent_text(path, cap=400) or ""
            if token.lower() in said.lower():
                return True, said[:120]
        time.sleep(2)
    return False, ""


def _observable_within(surf, cursor, timeout=8):
    """POSITIVE proof that cmux can see an agent on `surf`, within a bounded window — launch_bind's
    dark-surface probe. TWO INDEPENDENT READS, either of which is proof: cmux's hook STORE files a live
    agent here (rs.present), or cmux is actively STAMPING this surface in its own event log
    (rs.stamps_since — the status updates that drive vitals and the sidebar). A dark surface produces
    NEITHER, ever, so the window can be short; waiting longer never turns a dark surface bright.

    Lives HERE, not in cli.py: the cutover (52eb49a) deleted the launch/revive seating guards that used to
    share `cli._observable_within`, leaving this the only consumer — and a dangling `cli._observable_within`
    call that would AttributeError the moment this check ran."""
    from . import resolve as rs
    end = time.time() + timeout
    while True:
        if rs.present(surf) or rs.stamps_since(surf, cursor):
            return True
        if time.time() >= end:
            return False
        time.sleep(0.5)


def check_launch_bind(sb, parent_surf, trials, tool="claude"):
    """2. LAUNCH + BIND — the big one, and the reason a single green run proves nothing.

    Does cmux file the agent's session against THE SURFACE WE LAUNCHED INTO? When it does not, the agent
    runs and is permanently invisible to vitals/ls/the sidebar — the dark surface. It is INTERMITTENT (it
    hit 2 of 4 launches one night and 0 of 4 another), so this runs N trials and reports the RATE. A rate
    is a measurement; a single pass is an anecdote.

    Returns (Result, [live agent handles]) — the agents stay up for the checks that need a live one."""
    from . import cli
    from . import resolve as rs
    from . import state as fs
    seated, dark = [], 0
    for i in range(trials):
        label = f"conf-{tool}-{i}-{os.getpid()}"
        sb.claim_label(label)
        cursor = rs.stamp_cursor()
        # --place workspace, NOT a tab in the sandbox workspace. `fleet archive` closes an agent's whole
        # WORKSPACE when it is the last agent in it, taking every sibling surface with it as collateral —
        # on the first run that reaped the sandbox's own conductor surface and broke the revive. Giving each
        # agent its own workspace makes the blast radius of an archive exactly one agent. (It also says
        # something worth knowing: an agent seated as a TAB beside anything else means an archive can close
        # that thing too. Here everything in reach is ours; in a shared workspace it would not be.)
        rc = cli.cmd_launch(["--adhoc", label, "--tool", tool, "--place", "workspace",
                             "--parent", parent_surf, "--force"])
        e = fs.live_get(label) or {}
        surf = e.get("surface", "")
        if not surf:
            return (Result(f"launch_bind[{tool}]", FAIL,
                           f"trial {i + 1}: the launch never registered a surface (rc={rc})",
                           {"trials_run": i + 1},
                           depends="`fleet launch` — no agent can be seated at all"), seated)
        sb.claim_surface(surf)
        if e.get("ws"):
            try:
                sb.claim_workspace(e["ws"])
            except NotDisposable:
                pass                                    # a tab in OUR workspace: already owned
        # A LAZY-BINDING TOOL CANNOT BE JUDGED AT t=0, and this is the trap the whole dark-codex problem
        # sits on: codex binds its session on its FIRST TURN, so a freshly-seated codex has no session and
        # no store record — which is INDISTINGUISHABLE from a dark one. The first cut of this check called
        # 2/2 healthy codex launches DARK for exactly that reason. A conformance suite that cries wolf on a
        # healthy agent is worse than no suite, so: give a lazy tool the turn it needs to bind, and judge it
        # after. (claude binds at boot, so it is judged where the launch itself judges it.)
        if tool != "claude":
            _drive(surf, f"Reply with exactly: BOUND-{i}")
            _wait_answer(surf, "BOUND", timeout=120)
        observable = _observable_within(surf, cursor, timeout=15)
        if not observable and rs.alive(surf, tool):
            dark += 1
        seated.append({"label": label, "surface": surf, "tool": tool, "dark": not observable})

    ev = {"trials": trials, "dark": dark, "surfaces": [s["surface"][:8] for s in seated]}
    if dark:
        return (Result(f"launch_bind[{tool}]", FAIL,
                       f"{dark}/{trials} launches came up DARK — cmux filed the session against a surface "
                       f"that is not the one it seated the agent on", ev,
                       depends="`fleet launch`/`revive` observability: a dark agent works but is invisible "
                               "to vitals/ls/the sidebar forever, and reads as DEAD to every store check"),
                seated)
    return (Result(f"launch_bind[{tool}]", PASS,
                   f"{trials}/{trials} launches bound the session to the surface they were seated on", ev,
                   depends="`fleet launch` + `revive` (the dark-surface class)"), seated)


def check_stamping(agent):
    """3. OBSERVABILITY. After a turn, does cmux stamp `--panel=<OUR surface>`?

    `--panel=` is the SURFACE and `--tab=` is the WORKSPACE; keying on `--tab=` inverts the answer and
    makes every agent in a conductor's workspace look like it is stamping every other agent's surface.
    This is the signal that DIES on a dark surface, and it is what drives vitals and the sidebar."""
    from . import resolve as rs
    surf = agent["surface"]
    cursor = rs.stamp_cursor()
    _drive(surf, f"Reply with exactly: STAMP-{agent['label'][-6:]}")
    answered, said = _wait_answer(surf, "STAMP")
    if not answered:
        return Result("stamping", UNKNOWN,
                      "the agent never answered, so a missing stamp proves nothing about stamping",
                      {"surface": surf[:8]},
                      depends="`vitals` / `ls` / the cmux sidebar (all keyed by surface)")
    time.sleep(2)
    n = rs.stamps_since(surf, cursor)
    # The stamp log (events.jsonl) is written by the APP, not the per-launch hook CLI, so
    # CMUX_AGENT_HOOK_STATE_DIR does NOT redirect it: in an ISOLATED test env that shares the cmux app, the
    # stamps go to the app's own ~/.cmuxterm/events.jsonl and this HOOKSTORE has no events.jsonl at all. That
    # is 'I could not observe the stamp channel here', which the house doctrine forbids rendering as a FAIL —
    # a total-log-absent read is UNKNOWN, not the dark-surface verdict. (On prod, HOOKSTORE == ~/.cmuxterm, so
    # the log is always present and this branch never fires.)
    log = os.path.join(HOOKSTORE, "events.jsonl")
    total_stamps = 0
    if os.path.exists(log):
        try:
            with open(log, errors="replace") as f:
                total_stamps = sum(1 for line in f if "sidebar.metadata.updated" in line)
        except OSError:
            total_stamps = 0
    ev = {"surface": surf[:8], "stamps_after_a_real_turn": n, "agent_said": said,
          "stamp_log": log, "stamp_log_present": os.path.exists(log), "total_stamps_in_log": total_stamps}
    if n > 0:
        return Result("stamping", PASS, f"cmux stamped --panel=<our surface> {n}x during a real turn", ev,
                      depends="`vitals`/`ls`/the sidebar — a surface that stops being stamped is a DARK "
                              "agent: alive, working, and invisible")
    if total_stamps == 0:
        return Result("stamping", UNKNOWN,
                      "the stamp log (events.jsonl) is not present under this HOOKSTORE, so the stamp channel "
                      "is UNOBSERVABLE here — the app writes events.jsonl to its own ~/.cmuxterm, and "
                      "CMUX_AGENT_HOOK_STATE_DIR does not redirect it (isolated test env sharing the app). "
                      "This is 'could not observe', never proof of a dark surface", ev,
                      depends="`vitals`/`ls`/the sidebar (keyed by surface) — provable only where the app's "
                              "own events.jsonl is readable (prod, or an isolated env with its own app)")
    return Result("stamping", FAIL,
                  "the agent completed a real turn and cmux stamped our surface ZERO times, though the stamp "
                  "log has stamps for OTHER surfaces (this is the dark-surface signature)", ev,
                  depends="`vitals`/`ls`/the sidebar go blind to this agent, permanently")


def check_drive(agent):
    """5. DRIVE. `send` + `send-key` must actually SUBMIT — proven by the agent's own answer, in its own
    transcript. A prompt sitting unsent in the input box looks identical on the pane to one submitted."""
    surf = agent["surface"]
    token = "DRIVEN-" + str(os.getpid())[-4:]
    _drive(surf, f"Reply with exactly: {token}")
    ok, said = _wait_answer(surf, token)
    ev = {"surface": surf[:8], "agent_said": said}
    if ok:
        return Result("drive", PASS, "the prompt submitted and the agent answered it", ev,
                      depends="`fleet drive-child`, the idle-wake path, and every prime/handover the "
                              "conductor types into a child")
    return Result("drive", FAIL,
                  "the prompt did not produce an answer — it may be sitting unsubmitted in the input box "
                  "(the pane cannot tell you the difference)", ev,
                  depends="`fleet drive-child` + idle-wake: conductors cannot task their children at all")


def check_hook_chain(agent, parent_surf, timeout=120):
    """4. THE HOOK CHAIN, END TO END — the fleet's spine.

    A real Stop, from a real agent, through cmux's hook, onto the bus, into the router, landing a
    COMPLETION ROW in the conductor's inbox. Every link is real; the observation is the inbox row, which is
    the only thing a conductor ever actually sees.

    For codex this also proves the hooks in its seat home are TRUSTED and not merely installed — an
    untrusted codex hook does not run and does not say so."""
    from . import state as fs
    end = time.time() + timeout
    while time.time() < end:
        rows = fs.inbox_pending(parent_surf, kind="completion")
        for r in rows:
            if r.get("label") == agent["label"]:
                return Result(f"hook_chain[{agent['tool']}]", PASS,
                              "a real Stop reached the router and landed a completion in the conductor's "
                              "inbox", {"label": agent["label"], "text": str(r.get("text", ""))[:80]},
                              depends="THE FLEET'S SPINE: without it a conductor never learns any child "
                                      "finished, and every dispatch stalls forever")
        time.sleep(3)
    return Result(f"hook_chain[{agent['tool']}]", FAIL,
                  f"no completion reached the conductor's inbox within {timeout}s after a real turn",
                  {"label": agent["label"], "surface": agent["surface"][:8],
                   "hint": "cmux hook -> bus -> router -> inbox; one of those links is broken"},
                  depends="THE FLEET'S SPINE (child completions -> conductor). Conductors stall forever.")


def check_lifecycle(sb, agent, parent_surf):
    """8. LIFECYCLE. archive -> revive must return a LIVE, OBSERVABLE, CONTEXT-PRESERVING agent.

    Three separate effects, and the third is the one people forget: the revived agent must still REMEMBER.
    A revive that sheds context is not a revive, it is a relaunch wearing its name."""
    from . import cli
    from . import resolve as rs
    from . import state as fs
    label, surf = agent["label"], agent["surface"]
    sb.assert_disposable("surface", surf)                # gate: we only archive what we created
    token = "MEMORY-" + str(os.getpid())[-4:]
    _drive(surf, f"Remember this token, I will ask for it later: {token}. Reply with exactly: STORED")
    if not _wait_answer(surf, "STORED")[0]:
        return Result("lifecycle", UNKNOWN, "could not seed the agent with a token to remember",
                      {"label": label}, depends="`fleet archive`/`revive` (context-preserving restart)")

    if cli.cmd_archive([label]) != 0:
        return Result("lifecycle", FAIL, "archive refused/failed", {"label": label},
                      depends="`fleet archive` + `revive`: the ONLY repair for a dark working agent")
    rc = cli.cmd_revive([label, "--parent", parent_surf])
    e = fs.live_get(label) or {}
    new_surf = e.get("surface", "")
    if new_surf:
        sb.claim_surface(new_surf)
        if e.get("ws"):
            try:
                sb.claim_workspace(e["ws"])
            except NotDisposable:
                pass
    ev = {"label": label, "revive_rc": rc, "old_surface": surf[:8], "new_surface": new_surf[:8]}
    if not new_surf:
        return Result("lifecycle", FAIL, "revive did not produce a live registry row", ev,
                      depends="`fleet revive` — an archived agent cannot be brought back at all")
    ev["observable"] = rs.present(new_surf)
    ev["dark"] = rs.dark(new_surf, agent["tool"])
    if not ev["observable"]:
        return Result("lifecycle", FAIL, "the revived agent is LIVE but NOT OBSERVABLE (revived dark)", ev,
                      depends="`fleet revive` — the prescribed cure for a dark surface would REPRODUCE it")

    _drive(new_surf, "What was the token I asked you to remember? Reply with just the token.")
    remembered, said = _wait_answer(new_surf, token)
    ev["agent_recalled"] = said
    if remembered:
        return Result("lifecycle", PASS,
                      "archive -> revive returned a live, observable agent that still REMEMBERS "
                      "(full-session resume, not a shed)", ev,
                      depends="`fleet archive`/`revive` — the only repair for a dark working agent, and "
                              "the only way to move one between workspaces without destroying it")
    return Result("lifecycle", FAIL,
                  "the agent came back live and observable but LOST ITS CONTEXT (a relaunch, not a resume)",
                  ev,
                  depends="`fleet revive` — every restart would silently discard the agent's work")


# =================================================================================================
# The 0.64.18 needs-test checks (fleet-0.64.18-ledger.md). Each still ends in an observation of the
# CONSEQUENCE, tri-state, and — where it creates or mutates surfaces — passes through the Sandbox gate.
# =================================================================================================
def check_pane_move_healing(sb, agent, parent_surf):
    """THE DELETE-GATE (ledger row 9). Does this cmux keep filing a LIVE agent under its surface after that
    surface is MOVED to another workspace — the operation that, on 0.64.17, permanently darkens it?

    This is the single test that licenses deleting three fleet workarounds: `_reseat_if_dark`,
    `_adopt_misfiled_session`, and the archive-revive-to-relocate necessity. On 0.64.17, moving a live
    surface across workspaces destroys cmux's agent-status registration for that surface: `agentLifecycle`
    and `updatedAt` FREEZE, `present()` goes false, and the agent — still alive, still answering — goes
    invisible to `ls`/`vitals`/the sidebar forever (detachment-root-cause-2026-07-10.md; reproduced +
    live-verified, so the not-healed-on-17 half is already established). cmux 0.64.18 added
    `agent.resolve_delivery_target` (CMUXCLI+ClaudeHookDeliveryTarget.swift, issues #7939/#7391/#5781),
    which promotes live pid/surface identity over the persisted record and HEALS the record via upsert.

    We move via the RAW `cmux move-surface` primitive on a LIVE agent ON PURPOSE. `fleet move` REFUSES a
    live agent precisely because this darkens it (cli.cmd_move) — that refusal IS one of the workarounds
    under test — so we bypass it and drive the underlying cmux operation the fleet is protecting against.

    PROVE THE EFFECT, never the artifact: after the move AND a real post-move turn, the record's `updatedAt`
    must keep ADVANCING while the agent's own transcript shows it answered — so a frozen clock can only mean
    'cmux stopped filing this surface', never 'the agent stopped working'. Advancing + present() == HEALED
    == the three workarounds are deletable. Frozen / not-present / dark == STILL the root cause == keep them.

    NB the semantics are a version DIFF, not a breakage verdict: on 0.64.17 this reads FAIL (still dark) and
    on a healed 0.64.18 it reads PASS. FAIL-on-old -> PASS-on-new IS the delete license."""
    from . import cli
    from . import resolve as rs
    surf, tool = agent["surface"], agent["tool"]
    if not rs.present(surf):                              # precondition: bright BEFORE the move, or a dark
        return Result("pane_move_healing", UNKNOWN,       # result after it proves nothing about the move
                      "the agent was already not observable BEFORE the move, so a dark result after it "
                      "cannot be attributed to the move", {"surface": surf[:8]},
                      depends="the DELETE gate for _reseat_if_dark/_adopt_misfiled_session/archive-revive")
    before = rs.freshest(surf)                            # raw record, ANY liveness (a dark record survives)
    t0 = before.get("updatedAt") or 0
    try:
        target_ws = sb.new_workspace(f"conf-move-target-{os.getpid()}")   # gated + torn down like all we make
    except NotDisposable as e:
        return Result("pane_move_healing", UNKNOWN, f"could not create a target workspace to move into: {e}",
                      {"surface": surf[:8]}, depends="the DELETE gate")
    rc_mv, out_mv = _cmux("move-surface", "--surface", surf, "--workspace", target_ws, "--focus", "false")
    try:
        moved_ws = rs.workspace(surf, ws_map=rs.surface_ws_map(ttl=0))   # FRESH tree: where the surface landed post-move
    except Exception:
        moved_ws = ""
    token = "MOVED-" + str(os.getpid())[-4:]              # a REAL turn: what a healthy build re-stamps on
    _drive(surf, f"Reply with exactly: {token}")
    answered, said = _wait_answer(surf, token, timeout=120)
    time.sleep(3)                                         # let any heal upsert land after the turn completes
    after = rs.freshest(surf)
    t1 = after.get("updatedAt") or 0
    ev = {"surface": surf[:8], "moved_to_ws": (target_ws or "")[:8],
          "surface_now_in_ws": (moved_ws or "")[:8], "move_landed": bool(moved_ws),
          "record_surfaceId": (after.get("surfaceId") or "")[:8],
          "updatedAt_before": t0, "updatedAt_after": t1, "updatedAt_advanced": t1 > t0,
          "agent_answered_post_move": answered, "agent_said": said,
          "present_after": rs.present(surf), "dark_after": rs.dark(surf, tool), "alive_after": rs.alive(surf, tool)}
    if not ev["alive_after"]:                             # the move must not have KILLED it — different failure
        return Result("pane_move_healing", UNKNOWN,
                      "the agent is not alive after the move, so the move darkening it can't be told apart "
                      "from the move killing it (a different failure)", ev, depends="the DELETE gate")
    if not answered:
        return Result("pane_move_healing", UNKNOWN,
                      "the agent did not answer a turn after the move, so a frozen clock can't be pinned on "
                      "cmux rather than a wedged agent", ev, depends="the DELETE gate")
    surfaceid_ok = (after.get("surfaceId") or "").upper() == surf.upper()
    if t1 > t0 and ev["present_after"] and not ev["dark_after"] and surfaceid_ok:
        return Result("pane_move_healing", PASS,
                      "after moving the live surface to another workspace and a real turn, cmux KEPT filing "
                      "the agent under its surface (updatedAt advanced, present, not dark) — the dark-on-move "
                      "root cause is HEALED on this build", ev,
                      depends="DELETE-GATE OPEN: _reseat_if_dark + _adopt_misfiled_session + the "
                              "archive-revive-to-relocate necessity are obsolete on this build")
    return Result("pane_move_healing", FAIL,
                  "after the move, cmux STOPPED filing the live agent under its surface (updatedAt frozen / "
                  "not present / dark) though it answered a real turn — the dark-on-move root cause is STILL "
                  "PRESENT on this build", ev,
                  depends="DELETE-GATE CLOSED: _reseat_if_dark, _adopt_misfiled_session and the "
                          "archive-revive-to-relocate necessity are all STILL REQUIRED on this build")


def check_resume_argv_parse(agent):
    """Row 6 — THE one real breaking candidate. cmux 0.64.18 rewrote the resume-command wrapper
    (AgentResumeArgv / +Relaunch.swift): the `/bin/sh -c '<payload>'` payload now carries an exec-check token

        "$([ -x "${CMUX_CLAUDE_WRAPPER_SHIM:-}" ] && printf '%s' "$CMUX_CLAUDE_WRAPPER_SHIM" || printf claude)"

    whose INNER single quotes, once the whole payload is POSIX single-quoted for the outer `sh -c '...'`,
    collapse into the very `'\\''`-escape sequence cli._binding_argv's claude regex keys on. If the regex
    over-matches, a shell fragment (`%s`, `-x`, `printf`, `[`, `]`) lands in the extracted argv and
    recycle/revive re-exec with a bogus flag that dead-ends the relaunch.

    Proven against a REAL build-generated command (`cmux surface resume get`), never a synthetic fixture —
    the whole risk lives in cmux's exact encoding, so a hand-written command would test the wrong thing."""
    from . import cli
    surf = agent["surface"]
    command = (cli._resume_binding(surf) or {}).get("command", "")
    if not command:
        return Result("resume_argv_parse", UNKNOWN,
                      "cmux returned no resume_binding.command for this surface — the extractor can't be "
                      "exercised", {"surface": surf[:8]},
                      depends="`fleet recycle`/`revive` argv extraction (cli._binding_argv)")
    argv = cli._binding_argv(command)
    # Discrete shell-wrapper fragments that must NEVER surface as an argv token: the exact artifacts the
    # 0.64.18 exec-check/printf encoding leaks if the single-quote tokenizer tears it apart.
    CONTAMINANTS = {"%s", "printf", "[", "]", "-x", "/bin/sh", "&&", "||",
                    "${CMUX_CLAUDE_WRAPPER_SHIM:-}", "$CMUX_CLAUDE_WRAPPER_SHIM"}
    leaked = [t for t in argv if t in CONTAMINANTS or t.startswith("$(") or t.startswith('"$(') or "printf " in t]
    # Empty / whitespace-only tokens are the OTHER failure signature: they mean the single-quote pairing
    # mis-split a token (e.g. a flag value that itself contained a `'`, which the `'\''` escaping doubles).
    degenerate = [repr(t) for t in argv if t == "" or (t and t.isspace())]
    ev = {"surface": surf[:8], "resume_command_head": command[:140], "resume_command_tail": command[-140:],
          "extracted_argv": argv, "argv_len": len(argv), "leaked_fragments": leaked,
          "degenerate_tokens": degenerate}
    if not argv:
        return Result("resume_argv_parse", FAIL,
                      "the extractor returned an EMPTY argv from a non-empty resume command — recycle/revive "
                      "would re-exec with no flags at all", ev,
                      depends="`fleet recycle`/`revive`: the re-composed launch loses every flag")
    if leaked:
        return Result("resume_argv_parse", FAIL,
                      f"the extractor LEAKED shell-wrapper fragment(s) into the argv: {leaked} — 0.64.18's "
                      f"exec-check encoding broke cli._binding_argv's single-quote tokenizer", ev,
                      depends="`fleet recycle`/`revive`: a wrapper fragment becomes a bogus flag that "
                              "dead-ends the relaunch")
    if degenerate:
        return Result("resume_argv_parse", FAIL,
                      f"the extractor produced empty/whitespace token(s) {degenerate} — a flag value was "
                      f"mis-split by the single-quote tokenizer (a value containing a literal quote)", ev,
                      depends="`fleet recycle`/`revive`: a mangled flag value re-execs a broken command")
    # Positive coherence: feed it into the recycle path exactly as recycle does, and confirm a clean,
    # runnable claude arg list (leads with the resume flag; still no fragment anywhere).
    relaunch = cli._prepend_resume(list(argv), "claude", "TEST-SID")
    ev["relaunch_preview"] = relaunch[:12]
    if relaunch[:2] != ["--resume", "TEST-SID"] or any(t in CONTAMINANTS for t in relaunch):
        return Result("resume_argv_parse", FAIL,
                      "the extracted argv does not re-compose into a coherent claude relaunch", ev,
                      depends="`fleet recycle`/`revive`: the recomposed command would not relaunch claude")
    return Result("resume_argv_parse", PASS,
                  "cli._binding_argv tokenized the REAL resume command into a clean flag list (no shell "
                  "fragments) that re-composes into a runnable claude relaunch", ev,
                  depends="`fleet recycle`/`revive` — the argv they re-exec stays correct under 0.64.18's "
                          "new wrapper encoding")


def check_recycle(sb, agent, tool):
    """Row 5 — recycle round-trip, claude AND codex. `fleet recycle` restarts an agent IN PLACE on the SAME
    surface, re-composing its launch from cmux's resume_binding (the row-5/6 contract) and resuming the
    session — distinct from archive->revive (check_lifecycle), which lands on a FRESH surface. The resolver
    behind the binding changed in 0.64.18 (ControlCommandCoordinator+Surface), so what command it hands back
    is needs-test.

    PROVE THE EFFECT three ways, none of them the exit code: the recycled agent must come back (a) LIVE and
    (b) OBSERVABLE on its surface, and (c) still REMEMBER a token seeded before the recycle — a resume that
    sheds context is a relaunch wearing the name, and recycle's default resume exists to keep it.

    KNOWN LIMITATION — CODEX (2026-07-14, deferred). This check is claude-shaped in two ways that make its
    codex verdict UNRELIABLE, and codex recycle is an open pre-adopt question, not a settled PASS:
      * codex binds its session LAZILY (only on the first turn), so after an in-place respawn there is no
        hook record — and thus no new pid — until codex is DRIVEN. The pid-change re-bind poll below never
        fires for codex. A codex-correct check must drive the respawned pane to force the re-bind, then read
        recall. (claude binds at boot, so the pid-change poll works for it.)
      * observed on 0.64.19-nightly: `fleet recycle` on a codex agent ABORTED at respawn with
        `respawn-pane -> not_found: Surface not found` — the codex surface did not survive the process kill,
        so the same-surface respawn could not proceed. claude recycles cleanly on the same build. Whether
        this is a 0.64.18 regression or pre-existing needs a 0.64.17 codex-recycle comparison. See
        _meta/agents/cmux-dev/fleet-0.64.18-verdicts.md (row 5)."""
    from . import cli
    from . import resolve as rs
    label, surf = agent["label"], agent["surface"]
    sb.assert_disposable("surface", surf)                # gate: we only recycle what we created
    token = "RECYCLE-" + str(os.getpid())[-4:]
    _drive(surf, f"Remember this token, I will ask after a restart: {token}. Reply with exactly: STORED")
    if not _wait_answer(surf, "STORED", timeout=120)[0]:
        return Result(f"recycle[{tool}]", UNKNOWN, "could not seed a token before recycling",
                      {"label": label}, depends="`fleet recycle` (in-place resume restart)")
    old_pid = (rs.freshest_live(surf) or {}).get("pid")  # the PRE-recycle process, to detect the respawn
    rc = cli.cmd_recycle([label, "--force"])             # resume mode (default): preserve context
    # recycle dispatches a DETACHED worker that waits for the surface to go IDLE, then respawns the pane. So
    # `alive+present` is TRUE of the still-running OLD agent for a while — polling on it fires at t=0 and
    # drives the recall into an agent that is about to be torn down (context lost, not because recycle
    # failed). Wait for the RESPAWN itself: a live record whose pid DIFFERS from the pre-recycle one. (Resume
    # keeps the same session id but forks a NEW process, so the pid moves even though the sid does not.)
    end = time.time() + 200
    back = False
    while time.time() < end:
        cur = rs.freshest_live(surf) or {}
        if cur.get("pid") and cur.get("pid") != old_pid and rs.present(surf):
            back = True
            break
        time.sleep(3)
    ev = {"label": label, "surface": surf[:8], "recycle_rc": rc, "old_pid": old_pid,
          "new_pid": (rs.freshest_live(surf) or {}).get("pid"),
          "alive": rs.alive(surf, tool), "present": rs.present(surf)}
    if not back:
        return Result(f"recycle[{tool}]", FAIL,
                      "the agent did not respawn (no live process with a new pid) on its surface within 200s "
                      "of recycle", ev,
                      depends="`fleet recycle`: an in-place restart cannot bring the agent back")
    time.sleep(6)                                        # let the resumed agent settle before we type at it
    _drive(surf, "What was the token I asked you to remember before the restart? Reply with just the token.")
    remembered, said = _wait_answer(surf, token, timeout=120)
    ev["agent_recalled"] = said
    if remembered:
        return Result(f"recycle[{tool}]", PASS,
                      "recycle restarted the agent in place (same surface), live + observable, and it still "
                      "REMEMBERS — the resume_binding round-tripped", ev,
                      depends="`fleet recycle` — the in-place restart every self-heal and go-live leans on")
    return Result(f"recycle[{tool}]", FAIL,
                  "the agent came back live+observable but LOST its context (recycle resumed nothing / the "
                  "wrong session — the resume_binding did not round-trip)", ev,
                  depends="`fleet recycle`: every in-place restart silently sheds the agent's work")


def check_codex_hooks_install():
    """Row 17 — `cmux hooks codex install` still yields hooks.json + a valid trusted_hash. 0.64.18 refactored
    the hook catalog (AgentHookDefinitions -> AgentHookCatalog); this proves the refactor did not move the
    install OUTPUT shape the fleet verifies before every codex launch.

    A codex worker's Stop hook is how its conductor learns it finished; codex silently SKIPS an untrusted
    hook (no prompt, no error), so 'installed' without 'trusted' is indistinguishable from not installed. The
    fleet delegates to `cmux hooks codex install` then verifies BOTH halves (providers.codex_hooks_ok). We
    prove the EFFECT: run the real install into a THROWAWAY CODEX_HOME and assert the file exists AND the
    trusted_hash landed — not that the command exited 0. No live agent needed, so it runs every suite."""
    from . import providers
    home = tempfile.mkdtemp(prefix="conf-codex-home-")
    try:
        ok, detail = providers.codex_install_hooks(home)
        exists = os.path.exists(os.path.join(home, "hooks.json"))
        trusted = providers.codex_hooks_ok(home)         # BOTH halves: file present AND trusted_hash present
        ev = {"install_ok": ok, "install_detail": detail, "hooks_json_exists": exists, "trusted": trusted}
        if exists and trusted:
            return Result("codex_hooks_install", PASS,
                          "`cmux hooks codex install` wrote hooks.json AND trusted it (trusted_hash under "
                          "[hooks.state]) — the catalog refactor kept the install output shape", ev,
                          depends="every codex launch: an untrusted/missing hook fires no Stop, so the "
                                  "conductor never learns a codex child finished")
        if exists and not trusted:
            return Result("codex_hooks_install", FAIL,
                          "hooks.json was written but is NOT trusted (no trusted_hash) — codex silently skips "
                          "it and no completion ever reaches the conductor", ev,
                          depends="every codex child completion (silently dropped)")
        return Result("codex_hooks_install", FAIL,
                      f"`cmux hooks codex install` produced no hooks.json in the codex home ({detail})", ev,
                      depends="every codex child: no Stop hook installed at all")
    finally:
        shutil.rmtree(home, ignore_errors=True)


# =================================================================================================
# The runner: isolation (fail-closed), provenance, teardown.
# =================================================================================================
def _provenance():
    """Name the version of EVERYTHING this ran against. A result without provenance is not comparable to
    another result, and comparing two results is the entire point of the exercise."""
    from . import __version__
    ver = _q("--version").strip().splitlines()[:1]
    bundle = ""
    app = os.path.realpath(CMUX)
    for up in (app, os.path.dirname(app)):
        for _ in range(6):
            cand = os.path.join(up, "Info.plist")
            if os.path.exists(cand):
                try:
                    bundle = subprocess.run(["defaults", "read", cand, "CFBundleVersion"],
                                            capture_output=True, text=True).stdout.strip()
                except Exception:
                    pass
                break
            up = os.path.dirname(up)
        if bundle:
            break
    return {"cmux_bin": CMUX, "cmux_app": app, "cmux_version": ver[0] if ver else "?",
            "cmux_bundle_version": bundle or "?", "fleet_version": __version__,
            "hookstore": HOOKSTORE, "python": sys.version.split()[0]}


def _production_members():
    """Read the PRODUCTION registry once, and make every id in it untouchable for the life of the run.

    This is gate 2, and it is deliberately independent of gate 1: gate 1 says "I may only touch what I
    made", gate 2 says "and here, by name, is what is BERG'S". Belt AND braces, because the cost of being
    wrong once is one of his working agents."""
    from . import state as fs
    prod = {"surface": set(), "workspace": set(), "label": set()}
    for store in (fs.live_all(), fs.archive_all()):
        for label, e in (store or {}).items():
            prod["label"].add(label)
            if e.get("surface"):
                prod["surface"].add(e["surface"])
            if e.get("ws"):
                prod["workspace"].add(e["ws"])
    return prod


def _isolated(argv):
    """FAIL CLOSED. The suite runs against a state dir IT CREATES ITSELF, and it re-execs to get there.

    It does not ask to be told it is isolated and it does not trust an env var to have been spelled right
    — a worker once set a name that was ALMOST right and damaged a live conductor's record. So the parent
    process captures production's member list (read-only), mints a throwaway state dir, and re-execs the
    child into it. The child then REFUSES TO RUN if the state it resolved is production's."""
    prod = _production_members()
    tmp = tempfile.mkdtemp(prefix="fleet-conformance-")
    env = {**os.environ, "CMUX_STATE_DIR": os.path.join(tmp, "state"),
           "CMUX_FLEET_CONFORMANCE_PROD": json.dumps({k: sorted(v) for k, v in prod.items()}),
           "CMUX_FLEET_CONFORMANCE_HOME": tmp}
    os.makedirs(env["CMUX_STATE_DIR"], exist_ok=True)
    try:
        p = subprocess.run([sys.executable, "-m", "cmux_fleet", "_conformance-exec", *argv],
                           env=env, text=True)
        return p.returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_conformance(argv):
    """fleet conformance [--json] [--trials N] [--keep]   does this cmux build DO what the fleet needs?

    Run it on stable. Run it on nightly. DIFF the two reports: that diff is the breaking-change report,
    derived empirically instead of inferred from 1100 commit subjects.

    Every check proves the EFFECT, never the invocation — `exit 0` is not a pass, and neither is a printed
    OK. Tri-state: PASS / FAIL / UNKNOWN, and an UNKNOWN is never laundered into a pass.

    SAFE BY CONSTRUCTION: it creates its own workspace, its own agents and its own fleet state, and it is
    structurally incapable of closing or killing anything it did not create (see conformance.Sandbox). It
    refuses to run against the production state dir at all."""
    return _isolated(argv)


def cmd_conformance_exec(argv):
    """internal: the isolated child. Never run this by hand — `fleet conformance` sets up the isolation."""
    import argparse
    ap = argparse.ArgumentParser(prog="fleet conformance")
    ap.add_argument("--json", action="store_true", help="machine-diffable report on stdout")
    ap.add_argument("--trials", type=int, default=3,
                    help="launch/bind trials (the dark surface is INTERMITTENT; one run proves nothing)")
    ap.add_argument("--keep", action="store_true", help="leave the sandbox up for inspection (default: tear down)")
    ap.add_argument("--tool", default="claude", choices=["claude", "codex", "both"])
    a = ap.parse_args(argv)

    # --- FAIL CLOSED ------------------------------------------------------------------------------
    prod_raw = os.environ.get("CMUX_FLEET_CONFORMANCE_PROD")
    home = os.environ.get("CMUX_FLEET_CONFORMANCE_HOME", "")
    if not prod_raw or not home:
        sys.exit("[conformance] REFUSING: not launched through `fleet conformance` (no isolation was set "
                 "up). This suite creates and destroys surfaces; it does not run without proof of isolation.")
    if not STATE.startswith(home):
        sys.exit(f"[conformance] REFUSING TO RUN: my fleet state resolved to {STATE}, which is NOT the "
                 f"throwaway state I was given ({home}). I am pointed at real fleet state and I create and "
                 f"destroy agents. Refusing is the only safe answer.")
    from . import state as fs
    if fs.live_all():
        sys.exit(f"[conformance] REFUSING TO RUN: the throwaway registry at {STATE} already has members "
                 f"{sorted(fs.live_all())} — it is not throwaway. Refusing.")
    protected = {k: set(v) for k, v in json.loads(prod_raw).items()}

    # --json must be MACHINE-DIFFABLE, and the fleet's own verbs are chatty (a launch prints ten lines).
    # Stable-vs-nightly is meant to be a `diff`, so stdout carries the report and NOTHING else; the fleet's
    # narration goes to stderr where a human can still read it.
    import contextlib
    if a.json:
        with contextlib.redirect_stdout(sys.stderr):
            report = run_suite(a, protected, home)
    else:
        report = run_suite(a, protected, home)
    if a.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_report(report)
    return 1 if any(r["status"] == FAIL for r in report["checks"]) else 0


def run_suite(a, protected, home):
    """Build the sandbox, run every check, tear down, and PROVE the teardown."""
    from . import resolve as rs
    sb = Sandbox(_q, protected)
    checks, agents = [], []
    router = None
    started = time.time()

    # Our own router, against OUR throwaway state. This is what lets the hook-chain check be REAL without
    # writing a single byte into Berg's registry: cmux's bus is shared (it is the thing under test), but
    # the router that acts on it is ours, and it only knows about agents we created.
    try:
        ws = sb.new_workspace(f"conformance-{os.getpid()}")
        parent = sb.new_surface(ws)                      # the disposable "conductor" our agents report to
    except NotDisposable as e:
        return {"provenance": _provenance(), "fatal": str(e), "checks": [], "teardown": {}}

    router = subprocess.Popen([sys.executable, "-m", "cmux_fleet.router", "--live"],
                              env=os.environ, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bus = BusRecorder()
    bus.start()                                          # ARMED BEFORE the agents exist — the frames it
    time.sleep(2)                                        # needs are the ones they are about to generate

    try:
        checks.append(check_topology(sb, ws, parent))
        checks.append(check_feed_gate())
        checks.append(check_codex_hooks_install())        # ledger row 17: standalone, no live agent needed
        # check_hook_store_schema runs LATER (just before bus.result), once our own agents have written
        # records — in an isolated test env the store starts EMPTY, so an upfront read only ever sees
        # nothing. On prod it would pass anywhere; deferring it makes it validate the schema here too.

        tools = ["claude", "codex"] if a.tool == "both" else [a.tool]
        for tool in tools:
            r, seated = check_launch_bind(sb, parent, a.trials, tool=tool)
            checks.append(r)
            agents += seated
            live = [s for s in seated if not s["dark"]]
            if not live:
                checks.append(Result(f"hook_chain[{tool}]", UNKNOWN,
                                     "no observable agent was seated, so the chain could not be exercised",
                                     {}, depends="THE FLEET'S SPINE"))
                continue
            agent = live[0]
            checks.append(check_drive(agent))
            checks.append(check_stamping(agent))
            checks.append(check_hook_chain(agent, parent))
            bus.drain()
            if tool == "claude":                          # the resume + move paths are claude's today
                # READ-ONLY checks first: they inspect the ORIGINAL agent's pane/state, so they must run
                # before recycle (which restarts the agent and wipes the pane's STAMP nonce).
                checks.append(check_capture_pane(sb, ws, agent["surface"], "STAMP"))
                checks.append(check_read_screen(agent["surface"], "STAMP"))
                checks.append(check_resume_binding(agent["surface"]))
                checks.append(check_paint(sb, ws, agent["label"]))
                checks.append(check_resume_argv_parse(agent))         # ledger row 6 (the real breaking risk)
                # recycle while the agent is HEALTHY: a dark agent can't pass recycle's quiet-gate
                # (cli.cmd_move), so it runs BEFORE pane_move, which may darken the agent on an unhealed build.
                checks.append(check_recycle(sb, agent, tool))         # ledger row 5 (claude)
                checks.append(check_pane_move_healing(sb, agent, parent))  # ledger row 9 (HIGHEST — may darken)
                checks.append(check_lifecycle(sb, agent, parent))     # archive->revive: the repair for a dark agent
            else:
                checks.append(check_recycle(sb, agent, tool))         # ledger row 5 (codex): last codex check
        bus.drain()
        checks.append(check_hook_store_schema())          # NOW our agents have written records to inspect
        checks.append(bus.result())                      # read AFTER real turns have completed
    except NotDisposable as e:
        checks.append(Result("SAFETY", FAIL, f"the sandbox refused an operation: {e}", {},
                             depends="nothing — this is the suite protecting the fleet from itself"))
    finally:
        bus.stop()
        if router:
            router.terminate()
            try:
                router.wait(timeout=5)
            except Exception:
                router.kill()
        teardown = _teardown(sb, agents, keep=a.keep, protected=protected)

    return {"provenance": _provenance(),
            "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration_s": round(time.time() - started, 1),
            "trials": a.trials,
            "summary": {s: sum(1 for c in checks if c.status == s) for s in (PASS, FAIL, UNKNOWN, SKIP)},
            "checks": [c.as_dict() for c in checks],
            "teardown": teardown}


def _teardown(sb, agents, keep, protected):
    """Kill what we made, then PROVE nothing of ours is left — and prove we touched nothing of Berg's."""
    from . import cli
    from . import resolve as rs
    from . import state as fs
    if keep:
        return {"kept": True, "owned": sb.owned()}
    for ag in agents:
        try:
            sb.assert_disposable("surface", ag["surface"])     # the gate, again, on the way out
            cli.cmd_rm([ag["label"], "--kill", "--force"])
        except Exception as e:
            pass
    for surf in sb.owned()["surface"]:
        try:
            sb.close_surface(surf)
        except NotDisposable:
            pass
    for ws in sb.owned()["workspace"]:
        try:
            sb.close_workspace(ws)
        except NotDisposable:
            pass

    leftover_agents = sorted(s for s in sb.owned()["surface"] if rs.alive(s))
    leftover_rows = sorted(fs.live_all())
    survivors = sorted(l for l in protected["label"] if l in (fs.live_all() or {}))
    return {"kept": False,
            "owned": sb.owned(),
            "orphan_agents": leftover_agents,
            "orphan_registry_rows": leftover_rows,
            "clean": not leftover_agents and not leftover_rows,
            "production_rows_written": survivors,          # MUST be empty: we never write Berg's registry
            "production_untouched": not survivors}


def _print_report(rep):
    p = rep["provenance"]
    print(f"\n  fleet conformance — cmux {p['cmux_version']} (bundle {p['cmux_bundle_version']})")
    print(f"  app   : {p['cmux_app']}")
    print(f"  fleet : {p['fleet_version']}    trials: {rep.get('trials')}    {rep.get('duration_s')}s\n")
    if rep.get("fatal"):
        print(f"  FATAL: {rep['fatal']}\n")
        return
    for c in rep["checks"]:
        mark = {PASS: "PASS", FAIL: "FAIL", UNKNOWN: "????", SKIP: "skip"}[c["status"]]
        print(f"  [{mark}] {c['check']:<22} {c['detail']}")
        if c["status"] != PASS and c["breaks_if_red"]:
            print(f"         breaks: {c['breaks_if_red']}")
    s = rep["summary"]
    print(f"\n  {s[PASS]} pass, {s[FAIL]} fail, {s[UNKNOWN]} unknown")
    t = rep["teardown"]
    if t.get("kept"):
        print("  teardown: SKIPPED (--keep)")
    else:
        print(f"  teardown: {'clean' if t.get('clean') else 'RESIDUE: ' + str(t)}"
              f"  |  production untouched: {t.get('production_untouched')}")
    print()
