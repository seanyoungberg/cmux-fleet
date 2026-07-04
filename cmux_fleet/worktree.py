#!/usr/bin/env python3
# cmux_fleet/worktree.py — git-worktree lifecycle for cmux-fleet. Config-gated, DEFAULT-OFF.
#
# ONE owner per worktree = the fleet. We run `git worktree add` ourselves, point the agent's cwd at
# the result, and own teardown. We do NOT hook Claude's WorktreeCreate/WorktreeRemove and we do NOT
# pass `claude -w` (those make Claude a second owner — the documented double-cleanup / lock-race / branch-
# collision failure mode). Codex needs no special flag: the launcher already `cd`s into the worktree.
#
# Layout:  <repo>/<worktree_dir>/<label>/        (worktree_dir default ".worktrees", gitignored)
# Branch:  <prefix><label>                        (prefix default "fleet/")
# Base:    explicit > <default> > origin/<default> > HEAD   (no auto-fetch in v0.1; local default
#          first so an unpushed local-merge session never pins new trees to a stale origin ref)
#
# Teardown is REFUSE-IF-DIRTY by default (an explicit --wip-commit is the escape hatch) and ALWAYS
# keeps the branch. Nothing here ever merges or auto-deletes a branch. See docs/operations.md and
# the design note P1-worktrees.md.
import contextlib
import os
import subprocess
import time


class WorktreeError(Exception):
    """A git-worktree operation failed in a way the caller should surface, not swallow."""


def _git(repo, *args, check=True):
    """Run `git -C <repo> <args>` (never a shell). Return (returncode, stdout-stripped).
    Raises WorktreeError when check and the command fails."""
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        msg = (p.stderr.strip() or p.stdout.strip() or f"exit {p.returncode}")
        raise WorktreeError(f"git {' '.join(args)}: {msg}")
    return p.returncode, p.stdout.strip()


# ---------------------------------------------------------------- discovery
def repo_root(cwd):
    """Top-level of the git repo containing `cwd`, or None if `cwd` isn't inside a repo."""
    if not cwd or not os.path.isdir(cwd):
        return None
    rc, out = _git(cwd, "rev-parse", "--show-toplevel", check=False)
    return out if rc == 0 and out else None


def worktree_path(repo, worktree_dir, label):
    """Absolute path a label's worktree lives at: <repo>/<worktree_dir>/<label>."""
    return os.path.join(repo, worktree_dir or ".worktrees", label)


def _default_branch(repo):
    """The repo's default branch name (origin/HEAD target if known, else the current branch, else main)."""
    rc, out = _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if rc == 0 and out:
        return out.rsplit("/", 1)[-1]
    rc, out = _git(repo, "symbolic-ref", "--short", "--quiet", "HEAD", check=False)
    return out if rc == 0 and out else "main"


def _ref_exists(repo, ref):
    rc, _ = _git(repo, "rev-parse", "--verify", "--quiet", ref, check=False)
    return rc == 0


def _branch_exists(repo, branch):
    return _ref_exists(repo, f"refs/heads/{branch}")


def resolve_base(repo, explicit=""):
    """Resolve the base ref a NEW worktree branch is cut from. Cascade:
    explicit (then origin/<explicit>) > <default> > origin/<default> > HEAD. No fetch (v0.1).
    LOCAL <default> before origin/<default>: in a local-merge dev session (merges never pushed),
    origin/<default> can sit frozen-stale for days -- preferring it silently pinned every new worktree
    to whatever was last pushed instead of the current local default (confirmed 2026-07-03: a batch
    worktree branched off a days-old origin/main mid-session)."""
    if explicit:
        if _ref_exists(repo, explicit):
            return explicit
        if _ref_exists(repo, f"origin/{explicit}"):
            return f"origin/{explicit}"
        raise WorktreeError(f"base ref '{explicit}' not found (tried '{explicit}' and 'origin/{explicit}')")
    default = _default_branch(repo)
    for ref in (default, f"origin/{default}", "HEAD"):
        if _ref_exists(repo, ref):
            return ref
    return "HEAD"


def list_worktrees(repo):
    """Parse `git worktree list --porcelain` into dicts: {path, branch?, head?, detached?, locked?}."""
    rc, out = _git(repo, "worktree", "list", "--porcelain", check=False)
    if rc != 0:
        return []
    entries, cur = [], {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
                cur = {}
            continue
        if line.startswith("worktree "):
            cur["path"] = line[len("worktree "):]
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].replace("refs/heads/", "")
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line.strip() == "detached":
            cur["detached"] = True
        elif line.startswith("locked"):
            cur["locked"] = True
    if cur:
        entries.append(cur)
    return entries


def find_worktree(repo, path):
    """The registered worktree entry whose path matches `path` (realpath-compared), or None."""
    rp = os.path.realpath(path)
    for w in list_worktrees(repo):
        if os.path.realpath(w.get("path", "")) == rp:
            return w
    return None


# ---------------------------------------------------------------- concurrency
@contextlib.contextmanager
def _lock(repo, timeout=30.0, interval=0.3):
    """mkdir-based lock shared across all linked worktrees (keyed on the common git dir), so concurrent
    fan-out launches don't race on `worktree add`/`prune`. mkdir is atomic; a stale lock from a crash is
    the one failure mode — surfaced as a timeout with the path so an operator can rmdir it."""
    rc, common = _git(repo, "rev-parse", "--git-common-dir", check=False)
    common = common if (rc == 0 and common) else os.path.join(repo, ".git")
    if not os.path.isabs(common):
        common = os.path.normpath(os.path.join(repo, common))
    lockdir = os.path.join(common, "cmux-fleet-worktree.lock")
    end = time.time() + timeout
    while True:
        try:
            os.mkdir(lockdir)
            break
        except FileExistsError:
            if time.time() > end:
                raise WorktreeError(f"timed out acquiring worktree lock (stale? rmdir {lockdir})")
            time.sleep(interval)
    try:
        yield
    finally:
        try:
            os.rmdir(lockdir)
        except OSError:
            pass


# ---------------------------------------------------------------- create
def ensure_gitignored(repo, worktree_dir):
    """Make sure the worktree dir is ignored, once, via .git/info/exclude (shared by all linked
    worktrees, never committed, never pollutes a contributor's tracked .gitignore)."""
    rc, info = _git(repo, "rev-parse", "--git-common-dir", check=False)
    info = info if (rc == 0 and info) else os.path.join(repo, ".git")
    if not os.path.isabs(info):
        info = os.path.normpath(os.path.join(repo, info))
    exclude = os.path.join(info, "info", "exclude")
    entry = (worktree_dir or ".worktrees").rstrip("/") + "/"
    try:
        existing = ""
        if os.path.exists(exclude):
            with open(exclude) as f:
                existing = f.read()
        if any(line.strip() == entry for line in existing.splitlines()):
            return
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        with open(exclude, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"# cmux-fleet worktrees\n{entry}\n")
    except OSError:
        pass  # best-effort hygiene; a failure here is not worth aborting a launch


def ensure_worktree(repo, path, branch, base=""):
    """Idempotent. If a valid worktree already exists at `path`, reuse it (recycle/revive-safe). Else
    create it: reuse `branch` if it exists, otherwise cut it fresh from the resolved base. Runs under the
    repo lock and prunes stale registrations first. Returns `path`."""
    with _lock(repo):
        _git(repo, "worktree", "prune", check=False)
        existing = find_worktree(repo, path)
        if existing:
            if os.path.exists(os.path.join(path, ".git")):
                return path
            # registered but the dir is gone: prune above should have cleared it; if not, force-remove the
            # dangling registration so the add below succeeds.
            _git(repo, "worktree", "remove", "--force", path, check=False)
        if os.path.exists(path) and os.listdir(path):
            raise WorktreeError(f"{path} exists and is not a fleet worktree; refusing to clobber")
        if _branch_exists(repo, branch):
            _git(repo, "worktree", "add", path, branch)
        else:
            _git(repo, "worktree", "add", "-b", branch, path, resolve_base(repo, base))
        return path


# ---------------------------------------------------------------- teardown
def has_changes(path):
    """True if the worktree has uncommitted/untracked changes. FAILS CLOSED: any error (not a repo,
    git missing, path gone) returns True, so a refuse-if-dirty guard never green-lights an
    unconfirmable tree."""
    try:
        p = subprocess.run(["git", "-C", path, "status", "--porcelain"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return True
        return bool(p.stdout.strip())
    except Exception:
        return True


def wip_commit(path, label):
    """Stage everything and commit a WIP snapshot inside the worktree. Returns True on success.
    Used only as the explicit --wip-commit escape hatch on a dirty teardown."""
    rc, _ = _git(path, "add", "-A", check=False)
    if rc != 0:
        return False
    rc, _ = _git(path, "commit", "--no-verify", "-m", f"fleet WIP: {label}", check=False)
    return rc == 0


def teardown(repo, path, label, wip_commit_flag=False, force=False):
    """Remove a worktree, keeping its branch. Refuse-if-dirty by default; --wip-commit commits first.
    Returns (removed: bool, message: str). Never deletes a branch, never merges."""
    with _lock(repo):
        _git(repo, "worktree", "prune", check=False)
        w = find_worktree(repo, path)
        if not w and not os.path.exists(path):
            return True, f"no worktree at {path} (already gone)"
        branch = (w or {}).get("branch", "?")
        if has_changes(path):
            if wip_commit_flag:
                if not wip_commit(path, label):
                    return False, (f"REFUSED: {path} is dirty and the WIP commit failed; "
                                   f"resolve it by hand. Branch '{branch}' kept.")
            else:
                return False, (f"REFUSED: {path} has uncommitted changes. Commit/stash them, or re-run "
                               f"with --wip-commit. Branch '{branch}' kept.")
        try:
            _git(repo, "worktree", "remove", *(["--force"] if force else []), path)
        except WorktreeError as e:
            return False, f"REFUSED: {e}. Branch '{branch}' kept."
        return True, f"removed worktree {path} (branch '{branch}' kept)"


# ---------------------------------------------------------------- one-owner guardrails
def strip_owner_flags(caller_tokens):
    """Remove Claude's worktree-ownership flags (`-w`, `--worktree`, `--worktree=X`) from caller
    passthrough when the fleet owns the worktree. Returns (clean_tokens, stripped_any). The long form
    optionally consumes a following non-dash value (its branch/name)."""
    out, stripped, i = [], False, 0
    toks = list(caller_tokens or [])
    while i < len(toks):
        t = toks[i]
        if t in ("-w", "--worktree"):
            stripped = True
            if t == "--worktree" and i + 1 < len(toks) and not toks[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
            continue
        if t.startswith("--worktree="):
            stripped = True
            i += 1
            continue
        out.append(t)
        i += 1
    return out, stripped
