"""Layer 3 — E2E CLI lifecycle against a throwaway.

Drives the real `fleet` CLI as a subprocess against the throwaway STATE + a stubbed cmux binary.
The lifecycle: launch (compose via --dry-run, since a real spawn needs a live cmux) -> ls -> archive
-> revive (--dry-run) -> rm, asserting exit codes AND the fleet.json/archive.json state transitions.
The state-moving verbs (archive, rm) run for real; the cmux-spawning verbs (launch, revive) run
through their --dry-run compose path, which exercises resolution end-to-end without a surface.
"""
import json
import subprocess
import sys

from conftest import FLEET_PY


def run_fleet(env, *args, expect=0):
    p = subprocess.run([sys.executable, FLEET_PY, *args], env=env, capture_output=True, text=True)
    if expect is not None:
        assert p.returncode == expect, f"`fleet {' '.join(args)}` rc={p.returncode}\n{p.stdout}\n{p.stderr}"
    return p


# --- the surface that doesn't need cmux ----------------------------------------------------------
def test_help_lists_verbs(cli_env):
    p = run_fleet(cli_env, "--help")
    assert "launch" in p.stdout and "archive" in p.stdout and "revive" in p.stdout


def test_unknown_subcommand_errors(cli_env):
    p = run_fleet(cli_env, "bogus-verb", expect=None)
    assert p.returncode != 0


def test_ls_empty(cli_env):
    p = run_fleet(cli_env, "ls")
    assert "LIVE FLEET (0)" in p.stdout


def test_launch_dry_run_composes(cli_env):
    p = run_fleet(cli_env, "launch", "--adhoc", "smoke", "--parent", "FAKEPARENT", "--dry-run")
    assert "dry-run" in p.stdout.lower()


def test_config_renders(cli_env, tmp_path):
    # `fleet config --cwd <dir>` reads the settings stack; no cmux, no roster needed.
    p = run_fleet(cli_env, "config", "--cwd", str(tmp_path))
    assert "fleet config" in p.stdout and "FLEET ADDS" in p.stdout


# --- the full state lifecycle --------------------------------------------------------------------
def test_lifecycle_ls_archive_revive_rm(cli_env, fs, state_dir):
    label = "e2e-worker"
    # "launch" is represented by a seeded live entry (a real launch needs a live cmux); everything
    # downstream is a real CLI invocation against the shared throwaway state.
    fs.live_put(label, {"role": "worker", "kind": "child", "tool": "claude",
                        "cwd": "workers/e2e", "parent": "P", "place": "tab",
                        "surface": "SURF-SEED", "session": "claude-deadbeef00"})

    # ls shows it live
    assert label in run_fleet(cli_env, "ls").stdout

    # archive: real transition live -> archive (cmux stub absorbs close-surface)
    run_fleet(cli_env, "archive", label)
    live = json.load(open(f"{state_dir}/fleet.json"))
    arch = json.load(open(f"{state_dir}/archive.json"))
    assert label not in live
    assert label in arch and arch[label]["last_session"] == "claude-deadbeef00"

    # ls now shows it under ARCHIVED
    assert "ARCHIVED" in run_fleet(cli_env, "ls").stdout

    # revive --dry-run: prints the plan, makes NO transition (still archived)
    p = run_fleet(cli_env, "revive", label, "--parent", "FAKEPARENT", "--dry-run")
    assert "dry-run" in p.stdout.lower()
    assert label in json.load(open(f"{state_dir}/archive.json"))

    # rm: drops it from the shelf
    run_fleet(cli_env, "rm", label)
    assert label not in json.load(open(f"{state_dir}/archive.json"))
    assert "LIVE FLEET (0)" in run_fleet(cli_env, "ls").stdout


def test_archive_unknown_label_errors(cli_env):
    p = run_fleet(cli_env, "archive", "no-such-label", expect=None)
    assert p.returncode != 0


def test_rm_unknown_label_errors(cli_env):
    p = run_fleet(cli_env, "rm", "no-such-label", expect=None)
    assert p.returncode != 0
