"""`fleet <verb> --help` — the anti-drift pin.

The bug this exists to stop (2026-07-11): only the verbs that happened to build an `ArgumentParser` got
`--help` for free. The 16 hand-rolled ones either swallowed `--help` as a positional label
(`fleet rm --help` -> "no such label '--help'") or — the dangerous half — IGNORED it and RAN THE VERB.
`fleet inbox --help` ran the inbox; `fleet paint --help` painted the sidebar; `fleet serve --help`
STARTED THE HTTP SERVER AND BLOCKED.

So the test is a LOOP over the whole dispatch table, not a list of the verbs that were broken that day:
a two-list design (hand-rolled vs argparse) rots as verbs gain parsers, and a fixed list of 16 would
never catch verb #17. Every verb in `cli.verb_table()` — internal `_`-prefixed workers included — must
answer `--help` by printing usage and doing NOTHING:

  * it must not BLOCK          (subprocess timeout -> fail; this is what catches `serve`)
  * it must exit 0             (catches `--help` swallowed as a label: archive/rm/mute/child-digest)
  * it must print usage        (catches the verb EXECUTING: `ls` prints its table, `paint` prints
                                "[fleet paint] synced", `inbox` prints "[inbox] ...". Note this must be
                                a startswith("usage:") — a mere `"usage" in stdout` passes on the `usage`
                                VERB, whose own output says "no usage snapshot yet")
  * it must not write a byte   (state dir + fleet root hashed before/after)
  * it must not touch cmux     (a RECORDING cmux stub: read-only verbs like `ls` write no files, but
                                anything that reaches the terminal shows up here)
"""
import hashlib
import os
import subprocess
import sys

import pytest

from cmux_fleet import cli

# The whole table, `_recycle-exec`/`_recycle-bulk-exec` included. A verb added tomorrow is in this list
# the moment it is added to the table — and fails here unless it answers --help.
VERBS = sorted(cli.verb_table())

HELP_TIMEOUT = 30            # generous: a --help is ~0.3s. Only a verb that EXECUTED can burn 30s.


def _snapshot(*dirs):
    """path -> content hash for every file under `dirs`. A `--help` must not change one byte."""
    snap = {}
    for d in dirs:
        for root, _subdirs, files in os.walk(d):
            for name in files:
                p = os.path.join(root, name)
                try:
                    with open(p, "rb") as fh:
                        snap[p] = hashlib.sha256(fh.read()).hexdigest()
                except OSError as e:                          # a file that vanished is still a change
                    snap[p] = f"unreadable: {e}"
    return snap


@pytest.fixture
def help_env(cli_env, tmp_path):
    """cli_env, but with a RECORDING cmux stub — every `cmux ...` the CLI shells out to appends a line.
    The read-only verbs (ls/vitals/inbox/graph) write no state files, so a file snapshot alone cannot see
    them execute; reaching for cmux (or printing something that isn't usage) is what gives them away."""
    log = tmp_path / "cmux-calls.log"
    stub = tmp_path / "cmux-recording"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 0\n')
    stub.chmod(0o755)
    return {**cli_env, "CMUX_BIN": str(stub)}, log


def _run(env, *args, timeout=HELP_TIMEOUT):
    try:
        return subprocess.run([sys.executable, "-m", "cmux_fleet", *args], env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail(f"`fleet {' '.join(args)}` BLOCKED for {timeout}s instead of printing help — it "
                    f"EXECUTED the verb. (This is the original `fleet serve --help` bug: it started the "
                    f"HTTP server and never returned.)")


# --- the loop: every verb in the table, both spellings -------------------------------------------
@pytest.mark.parametrize("flag", ["--help", "-h"])
@pytest.mark.parametrize("verb", VERBS)
def test_verb_help_prints_usage_and_does_nothing(help_env, state_dir, verb, flag):
    env, cmux_log = help_env
    root = env["CMUX_FLEET_ROOT"]
    before = _snapshot(state_dir, root)

    p = _run(env, verb, flag)                                 # blocks -> pytest.fail inside _run

    ctx = f"`fleet {verb} {flag}` rc={p.returncode}\n--- stdout ---\n{p.stdout}\n--- stderr ---\n{p.stderr}"
    assert p.returncode == 0, f"help must exit 0, not error out on '{flag}' as a positional.\n{ctx}"
    first = p.stdout.splitlines()[0] if p.stdout else ""
    assert first.startswith("usage:"), f"help must PRINT USAGE, not run the verb.\n{ctx}"
    assert verb in first, f"the usage line must name the verb it is for.\n{ctx}"

    assert _snapshot(state_dir, root) == before, f"`fleet {verb} {flag}` wrote to state — it executed.\n{ctx}"
    assert not cmux_log.exists(), (f"`fleet {verb} {flag}` shelled out to cmux — it executed.\n"
                                   f"cmux calls:\n{cmux_log.read_text()}\n{ctx}")


# --- the guard fires on the FIRST token only ------------------------------------------------------
# peer-msg/drive-child/broadcast carry FREE TEXT. A body that mentions --help must be DELIVERED, never
# swallowed by help. This is why the guard checks rest[0] and never scans the tail.
def test_help_inside_a_peer_msg_body_is_not_help(help_env):
    env, cmux_log = help_env
    p = _run(env, "peer-msg", "ghost-label", "does --help work on this thing?")
    assert not p.stdout.startswith("usage: fleet peer-msg"), \
        f"the guard fired on a --help buried in the message BODY; the message was never sent:\n{p.stdout}"
    assert p.returncode != 0                                  # it tried to DELIVER, and there is no such peer


def test_help_as_a_drive_child_prompt_is_not_help(help_env):
    env, cmux_log = help_env
    # The exact footgun: the prompt to submit to the child IS the string "--help".
    p = _run(env, "drive-child", "FAKE-SURFACE-UUID", "--help")
    assert not p.stdout.startswith("usage: fleet drive-child"), \
        f"the guard fired on the PROMPT text; the child never got it:\n{p.stdout}"
    assert cmux_log.exists(), "drive-child should have reached for cmux to submit the prompt, not printed help"


def test_help_inside_a_broadcast_body_is_not_help(help_env):
    env, _log = help_env
    p = _run(env, "broadcast", "heads up: run --help for the new flags", "--scope", "mine", "--dry-run")
    assert not p.stdout.startswith("usage: fleet broadcast"), \
        f"the guard scanned the tail and fired on a --help inside the broadcast body:\n{p.stdout}"


# --- the usage dict is the ONE source of truth ----------------------------------------------------
def test_every_verb_can_answer_help(help_env):
    """The invariant behind the loop above, asserted directly: a verb either owns its help (argparse /
    sub-verb dispatcher) or has a VERB_USAGE entry for the guard to print. A new verb with neither would
    fall through the guard and EXECUTE on --help."""
    orphans = [v for v in VERBS if v not in cli.SELF_HELP_VERBS and cli.usage_for(v) is None]
    assert not orphans, (f"verbs in the dispatch table with no usage text and no parser of their own: "
                         f"{orphans}. Add a VERB_USAGE entry (or, if it grew an ArgumentParser, add it to "
                         f"SELF_HELP_VERBS).")


def test_top_level_help_is_the_joined_dict(help_env):
    """`fleet --help` is exactly the header + every VERB_USAGE value, in order — that is what makes the
    dict the single source of truth rather than a second copy that drifts from the blob."""
    env, _log = help_env
    p = _run(env, "--help")
    assert p.returncode == 0
    assert p.stdout == cli.USAGE_HEADER + "\n" + "\n".join(cli.VERB_USAGE.values()) + "\n"
    for verb in cli.VERB_USAGE:
        assert verb in p.stdout
    for internal in cli.INTERNAL_USAGE:                       # the `_`-prefixed workers stay out of it
        assert internal not in p.stdout
