# Unit + CLI tests for the git-worktree feature (scripts/worktree.py + the fleet.py wiring).
# Standard library + pytest only. No network, no cmux: every test runs against a throwaway git repo.
import os
import subprocess
import sys
import textwrap

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from cmux_fleet import worktree as wt  # noqa: E402


def _run(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repo with one commit on `main`."""
    r = tmp_path / "repo"
    r.mkdir()
    _run(r, "git", "init", "-q", "-b", "main")
    _run(r, "git", "config", "user.email", "t@t")
    _run(r, "git", "config", "user.name", "t")
    (r / "a.txt").write_text("hi\n")
    _run(r, "git", "add", "-A")
    _run(r, "git", "commit", "-qm", "init")
    return str(r)


# ---------------------------------------------------------------- discovery
def test_repo_root(repo, tmp_path):
    assert wt.repo_root(repo) == repo
    assert wt.repo_root(str(tmp_path)) is None      # tmp_path itself is not a repo
    assert wt.repo_root("/no/such/dir") is None


def test_resolve_base(repo):
    assert wt.resolve_base(repo, "") == "main"      # no origin -> local default branch
    assert wt.resolve_base(repo, "main") == "main"  # explicit, exists
    with pytest.raises(wt.WorktreeError):
        wt.resolve_base(repo, "does-not-exist")


def test_resolve_base_prefers_local_default_over_stale_origin(repo):
    # 2026-07-03 dispatch bug: a local-merge session (merges never pushed) leaves origin/<default>
    # frozen stale; the old cascade tried origin/<default> FIRST, so every new worktree silently
    # branched off the last-pushed commit instead of current local main. Local default must win.
    subprocess.run(["git", "-C", repo, "update-ref", "refs/remotes/origin/main", "HEAD"],
                   check=True, capture_output=True)             # origin/main pinned at the old tip
    (open(os.path.join(repo, "b.txt"), "w")).write("new\n")
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-qm", "local advance")          # local main moves past origin/main
    assert wt.resolve_base(repo, "") == "main"                   # NOT "origin/main"
    # sanity: the two refs really diverge, so the assertion above is meaningful
    local = subprocess.run(["git", "-C", repo, "rev-parse", "main"],
                           capture_output=True, text=True).stdout.strip()
    origin = subprocess.run(["git", "-C", repo, "rev-parse", "origin/main"],
                            capture_output=True, text=True).stdout.strip()
    assert local != origin


# ---------------------------------------------------------------- create
def test_ensure_worktree_creates_and_is_idempotent(repo):
    p = wt.worktree_path(repo, ".worktrees", "alpha")
    assert wt.ensure_worktree(repo, p, "fleet/alpha") == p
    assert os.path.exists(os.path.join(p, ".git"))
    assert wt._branch_exists(repo, "fleet/alpha")
    # second call reuses the same tree, no error
    assert wt.ensure_worktree(repo, p, "fleet/alpha") == p


def test_ensure_worktree_reuses_existing_branch(repo):
    p = wt.worktree_path(repo, ".worktrees", "beta")
    wt.ensure_worktree(repo, p, "fleet/beta")
    wt.teardown(repo, p, "beta")                     # removes tree, keeps branch
    assert wt._branch_exists(repo, "fleet/beta")
    # re-create on the kept branch (revive/recycle path) must not fail
    assert wt.ensure_worktree(repo, p, "fleet/beta") == p


def test_ensure_gitignored(repo):
    wt.ensure_gitignored(repo, ".worktrees")
    exclude = os.path.join(repo, ".git", "info", "exclude")
    assert ".worktrees/" in open(exclude).read()
    wt.ensure_gitignored(repo, ".worktrees")         # idempotent: no duplicate line
    assert open(exclude).read().count(".worktrees/") == 1


def test_refuses_to_clobber_nonworktree_dir(repo):
    p = wt.worktree_path(repo, ".worktrees", "gamma")
    os.makedirs(p)
    (open(os.path.join(p, "stray.txt"), "w")).write("x")
    with pytest.raises(wt.WorktreeError):
        wt.ensure_worktree(repo, p, "fleet/gamma")


# ---------------------------------------------------------------- teardown
def test_has_changes_failclosed():
    assert wt.has_changes("/no/such/path") is True   # unconfirmable -> dirty


def test_teardown_clean_removes_and_keeps_branch(repo):
    p = wt.worktree_path(repo, ".worktrees", "delta")
    wt.ensure_worktree(repo, p, "fleet/delta")
    removed, msg = wt.teardown(repo, p, "delta")
    assert removed and not os.path.exists(p)
    assert wt._branch_exists(repo, "fleet/delta")    # branch survives


def test_teardown_dirty_refuses_then_wip_commits(repo):
    p = wt.worktree_path(repo, ".worktrees", "eps")
    wt.ensure_worktree(repo, p, "fleet/eps")
    open(os.path.join(p, "wip.txt"), "w").write("x")
    removed, msg = wt.teardown(repo, p, "eps")
    assert removed is False and "REFUSED" in msg and os.path.exists(p)
    removed, msg = wt.teardown(repo, p, "eps", wip_commit_flag=True)
    assert removed and not os.path.exists(p)
    assert wt._branch_exists(repo, "fleet/eps")


def test_teardown_missing_is_noop(repo):
    p = wt.worktree_path(repo, ".worktrees", "ghost")
    removed, msg = wt.teardown(repo, p, "ghost")
    assert removed and "already gone" in msg


# ---------------------------------------------------------------- one-owner guardrail
def test_strip_owner_flags():
    assert wt.strip_owner_flags(["--model", "opus", "-w", "--foo"]) == (["--model", "opus", "--foo"], True)
    assert wt.strip_owner_flags(["--worktree", "feat", "--bar"]) == (["--bar"], True)
    assert wt.strip_owner_flags(["--worktree=feat", "--bar"]) == (["--bar"], True)
    assert wt.strip_owner_flags(["--model", "opus"]) == (["--model", "opus"], False)


# ---------------------------------------------------------------- CLI integration (subprocess, no cmux)
def _toml(tmp_path, root, state):
    p = tmp_path / "fleet.toml"
    p.write_text(textwrap.dedent(f"""
        [fleet]
        root = "{root}"
        state_dir = "{state}"
        [defaults]
        tool = "claude"
        [tool.claude]
        flags = "--effort high"
        [role.coder]
        cwd = "repo"
        place = "workspace"
        group = "coders"
        worktree = true
    """))
    return str(p)


def _fleet(toml, state, *args):
    env = dict(os.environ, CMUX_FLEET_TOML=toml, CMUX_STATE_DIR=state,
               PYTHONPATH=REPO + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return subprocess.run([sys.executable, "-m", "cmux_fleet", *args],
                          capture_output=True, text=True, env=env)


def test_launch_dryrun_swaps_cwd_and_strips_w(repo, tmp_path):
    state = str(tmp_path / "state")
    toml = _toml(tmp_path, str(tmp_path), state)
    # role cwd is "repo" under root=tmp_path; the fixture put the repo at tmp_path/repo
    r = _fleet(toml, state, "launch", "coder", "--parent", "fake", "--dry-run", "--", "-w", "--model", "opus")
    assert r.returncode == 0, r.stderr
    assert ".worktrees/coder" in r.stdout                 # cwd swapped to the worktree
    assert "stripped Claude -w" in r.stdout               # owner-flag guardrail fired
    assert "claude --effort high --model opus" in r.stdout
    assert " -w " not in (" " + r.stdout.split("launch:")[-1] + " ")
    assert not os.path.exists(os.path.join(repo, ".worktrees"))  # dry-run created nothing


def test_launch_no_worktree_override(repo, tmp_path):
    state = str(tmp_path / "state")
    toml = _toml(tmp_path, str(tmp_path), state)
    r = _fleet(toml, state, "launch", "coder", "--parent", "fake", "--no-worktree", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert ".worktrees" not in r.stdout


# --- `fleet worktree clean` precondition (needs the registry row; refuses while live) -------------
def test_worktree_clean_refuses_while_live(monkeypatch):
    from cmux_fleet import cli as fleet
    from cmux_fleet import state as fs
    fs.live_put("wc-live", {"role": "r", "kind": "child", "tool": "claude", "surface": "S1",
                            "status": "live",
                            "worktree": {"repo": "/r", "path": "/r/.worktrees/wc-live", "branch": "fleet/wc-live"}})
    monkeypatch.setattr(fs, "lifecycle", lambda s: "running")
    monkeypatch.setattr(fs, "surface_has_live_pid", lambda s: True)   # genuinely live (non-terminal AND live pid)
    with pytest.raises(SystemExit):                      # live -> refuse (archive or rm --kill instead)
        fleet.cmd_worktree(["clean", "wc-live"])


def test_worktree_clean_proceeds_over_dead_pid_ghost(monkeypatch):
    # round-2 gap (2026-07-06): a FROZEN 'running' record on a DEAD pid (SessionEnd-less brick) must NOT
    # block worktree teardown -- there's no live work to protect. surface_has_live_agent reads it gone, so
    # clean proceeds (previously the dead ghost tripped the live-guard and stranded the worktree).
    from cmux_fleet import cli as fleet
    from cmux_fleet import state as fs
    fs.live_put("wc-ghost", {"role": "r", "kind": "child", "tool": "claude", "surface": "S1",
                             "status": "live",
                             "worktree": {"repo": "/r", "path": "/r/.worktrees/wc-ghost", "branch": "fleet/wc-ghost"}})
    monkeypatch.setattr(fs, "lifecycle", lambda s: "running")         # frozen non-terminal string...
    monkeypatch.setattr(fs, "surface_has_live_pid", lambda s: False)  # ...but the process is DEAD
    monkeypatch.setattr(wt, "teardown",
                        lambda repo, path, label, wip_commit_flag=False, force=False: (True, "removed"))
    rc = fleet.cmd_worktree(["clean", "wc-ghost"])       # past the guard -> real teardown (stubbed)
    assert rc == 0
    assert (fs.live_get("wc-ghost") or {}).get("worktree") is None    # tree marker nulled, row kept


def test_worktree_clean_works_on_archived(monkeypatch):
    from cmux_fleet import cli as fleet
    from cmux_fleet import state as fs
    fs.archive_put("wc-arch", {"role": "r", "kind": "child", "tool": "claude", "status": "archived",
                               "worktree": {"repo": "/r", "path": "/r/.worktrees/wc-arch", "branch": "fleet/wc-arch"}})
    monkeypatch.setattr(wt, "teardown",
                        lambda repo, path, label, wip_commit_flag=False, force=False: (True, "removed"))
    rc = fleet.cmd_worktree(["clean", "wc-arch"])        # archived row exists -> supported reclaim path
    assert rc == 0
    assert (fs.archive_get("wc-arch") or {}).get("worktree") is None   # tree marker nulled, archive row kept
