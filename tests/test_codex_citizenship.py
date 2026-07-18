"""The citizenship doc a codex worker boots with — $CODEX_HOME/AGENTS.md, fleet-owned.

WHY THE HOME AND NOT A cwd FILE. Codex loads no claude plugins, so none of the fleet's skills reach a codex
worker: it boots knowing nothing about the fleet it is a child of. Codex's instruction sources are the
global file in $CODEX_HOME and a project chain walked from the git root down to the cwd. A worker's cwd
varies (an agent home, a repo worktree, an ad-hoc dir), so NO file in that chain covers every worker. The
home file covers all of them. Verified against codex-cli 0.144.1 by canary: a doc in $CODEX_HOME/AGENTS.md
reached the model from a non-git cwd with no project AGENTS.md anywhere in it.

THE TRAP THESE TESTS EXIST FOR. At the global level codex reads ONE file: `AGENTS.override.md` if it
exists, else `AGENTS.md`. The override REPLACES AGENTS.md — it does not merge with it (same canary, both
files present: only the override's content came back). So installing into AGENTS.md when an operator has
written an override produces a file that sits in the home looking perfectly installed and that codex NEVER
READS. That failure is invisible on both ends, which is the worst kind available here, and it is what
test_installs_into_the_OVERRIDE_when_one_exists pins.
"""
import os

import pytest

from cmux_fleet import providers as pv


def _read(p):
    return open(p).read()


# --- which file codex will actually read ---------------------------------------------------------
def test_plain_home_gets_AGENTS_md(tmp_path):
    assert pv.codex_agents_file(str(tmp_path)) == os.path.join(str(tmp_path), "AGENTS.md")


def test_installs_into_the_OVERRIDE_when_one_exists(tmp_path):
    """The invisible-void trap. An operator's AGENTS.override.md SHADOWS AGENTS.md entirely, so citizenship
    must land in the override. An implementation that always writes AGENTS.md passes every other test here
    and ships a worker that reads none of it."""
    override = tmp_path / "AGENTS.override.md"
    override.write_text("# my own global instructions\nalways use tabs\n")

    path, status = pv.codex_install_citizenship(str(tmp_path))

    assert os.path.basename(path) == "AGENTS.override.md", "citizenship went into the file codex IGNORES"
    assert status == "installed"
    assert "fleet peer-msg" in _read(path)
    assert "always use tabs" in _read(path), "the operator's own instructions were clobbered"
    # and the shadowed file is left alone — we never write a doc into a file codex will not read
    assert not (tmp_path / "AGENTS.md").exists()


def test_an_EMPTY_override_does_not_count(tmp_path):
    """Codex takes the first NON-EMPTY file. An empty override is not an override."""
    (tmp_path / "AGENTS.override.md").write_text("   \n")
    assert os.path.basename(pv.codex_agents_file(str(tmp_path))) == "AGENTS.md"


# --- the install itself: fenced, idempotent, non-destructive --------------------------------------
def test_install_into_a_fresh_home_writes_the_doc(tmp_path):
    path, status = pv.codex_install_citizenship(str(tmp_path))
    assert status == "installed"
    text = _read(path)
    assert pv.CITIZEN_FENCE_BEGIN in text and pv.CITIZEN_FENCE_END in text
    assert "fleet peer-msg" in text


def test_reinstall_is_a_NO_OP_and_does_not_touch_the_file(tmp_path):
    """Idempotence is what makes sync-on-launch safe. Not just 'same content' — the file must not be
    REWRITTEN, or every launch would churn the mtime of a file in the operator's home."""
    path, _ = pv.codex_install_citizenship(str(tmp_path))
    before = os.stat(path).st_mtime_ns

    path2, status = pv.codex_install_citizenship(str(tmp_path))

    assert (path2, status) == (path, "current")
    assert os.stat(path).st_mtime_ns == before, "an already-current home was rewritten anyway"


def test_a_stale_block_is_UPDATED_in_place(tmp_path):
    """The drift fix: N homes with N hand-edited copies is the failure being designed out. A home holding
    an old block gets the CURRENT text, not a second block."""
    stale = f"{pv.CITIZEN_FENCE_BEGIN}\nold and wrong\n{pv.CITIZEN_FENCE_END}\n"
    (tmp_path / "AGENTS.md").write_text(stale)

    path, status = pv.codex_install_citizenship(str(tmp_path))

    text = _read(path)
    assert status == "updated"
    assert "old and wrong" not in text
    assert "fleet peer-msg" in text
    assert text.count(pv.CITIZEN_FENCE_BEGIN) == 1, "the doc was appended a second time instead of replaced"


def test_the_operators_own_text_outside_the_fence_SURVIVES(tmp_path):
    """The fleet owns its block, not the file. Clobbering a human's global instructions to install our own
    is not an acceptable trade — they would (rightly) delete the whole thing."""
    (tmp_path / "AGENTS.md").write_text("# mine\nprefer rust\n")

    path, _ = pv.codex_install_citizenship(str(tmp_path))
    text = _read(path)
    assert "prefer rust" in text and "fleet peer-msg" in text

    pv.codex_install_citizenship(str(tmp_path))       # and it survives a re-sync
    assert "prefer rust" in _read(path)


def test_status_is_a_PURE_READ(tmp_path):
    """--check must never write. A codex home can hold the wrong account, and the seat rules here are built
    on nothing-that-inspects-a-home-may-change-it."""
    path, status = pv.codex_citizenship_status(str(tmp_path))
    assert status == "installed"                       # i.e. "would install"
    assert not os.path.exists(path), "a read-only status check CREATED the file"


# --- the content contract: what a worker must actually be told ------------------------------------
@pytest.mark.parametrize("must_say, why", [
    ("fleet peer-msg", "how a blocked worker reaches its conductor and reads the reply"),
    ("--to-parent", "5d: no AGENT_CONDUCTOR env — a worker reaches its conductor via registry-derived parent addressing, without naming it"),
    ("fleet inbox", "how to read what is addressed to you"),
    ("git add -A", "named so it can be forbidden: a worker is not alone in the tree"),
    ("git -C", "never `cd <dir> && git`"),
    ("$AGENT_LABEL", "the self-gate: an unlabelled codex session is a human's, and must ignore this file"),
])
def test_the_doc_says_the_thing(must_say, why):
    assert must_say in pv.codex_citizenship_text(), why


def test_the_doc_does_NOT_carry_the_retired_AGENT_CONDUCTOR_env(tmp_path):
    """Ship 5d retired the AGENT_CONDUCTOR env var: parentage derives from the registry, and a worker
    reaches its conductor via `peer-msg --to-parent`, not a captured env label. The doc must not resurrect
    the env var — a codex worker told to `peer-msg "$AGENT_CONDUCTOR"` would address the empty string."""
    assert "$AGENT_CONDUCTOR" not in pv.codex_citizenship_text()


def test_the_doc_warns_that_backticks_are_EATEN_SILENTLY(tmp_path):
    """The trap that fails on BOTH ends without a word: a backtick in a peer-msg body is shell substitution,
    so the word vanishes — and the send still SUCCEEDS. A worker that does not know this cannot detect it."""
    text = pv.codex_citizenship_text()
    assert "Single-quote" in text
    assert "backtick" in text.lower()
