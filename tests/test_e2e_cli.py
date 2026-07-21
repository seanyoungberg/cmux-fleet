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

def run_fleet(env, *args, expect=0):
    # `python -m cmux_fleet` == the `fleet` console script; cli_env puts REPO on PYTHONPATH.
    p = subprocess.run([sys.executable, "-m", "cmux_fleet", *args], env=env, capture_output=True, text=True)
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


def test_launch_dry_run_composes(cli_env, tmp_path):
    # `--adhoc NAME` is an alias for the rostered `adhoc` role (5d): NAME becomes the label, cwd = the
    # role's ONE shared home (no per-name subdir). Needs a [role.adhoc] block in the toml.
    toml = tmp_path / "fleet.toml"
    toml.write_text('[role.adhoc]\ncwd = "agents/ad-hoc"\n[role.adhoc.claude]\n')
    env = {**cli_env, "CMUX_FLEET_TOML": str(toml)}
    p = run_fleet(env, "launch", "--adhoc", "smoke", "--parent", "FAKEPARENT", "--dry-run")
    assert "dry-run" in p.stdout.lower()
    assert "role/label=smoke" in p.stdout          # label = the ad-hoc name
    assert "agents/ad-hoc" in p.stdout             # cwd = the shared adhoc home, NOT agents/ad-hoc/smoke
    assert "ad-hoc/smoke" not in p.stdout          # ...specifically NO per-name subdir


def test_launch_adhoc_needs_role_block(cli_env, tmp_path):
    # opt-in: with no [role.adhoc] block, --adhoc errors (the off-roster per-name path is retired).
    toml = tmp_path / "fleet.toml"
    toml.write_text('[role.worker]\ncwd = "agents/w"\n[role.worker.claude]\n')
    env = {**cli_env, "CMUX_FLEET_TOML": str(toml)}
    p = run_fleet(env, "launch", "--adhoc", "smoke", "--parent", "FAKE", "--dry-run", expect=None)
    assert p.returncode != 0 and "role.adhoc" in (p.stdout + p.stderr)


def test_launch_plugin_unions_on_a_role(cli_env, tmp_path):
    # REGRESSION (2026-07-04): the launch plugin-add flag must union onto a roster-ROLE launch, not just an
    # --adhoc one (the contract is "pass any valid flag at launch/recycle and it takes"). Assert `--plugin`
    # unions onto a ROLE's composed loadout, with a control proving it is absent when not passed.
    mkt = tmp_path / "mkt"
    (mkt / "extrap" / ".claude-plugin").mkdir(parents=True)
    (mkt / "extrap" / ".claude-plugin" / "plugin.json").write_text('{"name":"extrap"}')
    index = tmp_path / "plugins.toml"
    index.write_text(f'[marketplace.local]\npath = "{mkt}"\n'
                     '[plugin.extrap]\ntype = "linked"\nsource = "local"\n')
    toml = tmp_path / "fleet.toml"
    toml.write_text('[role.worker]\nkind = "child"\ncwd = "workers/w"\n[role.worker.claude]\n')
    env = {**cli_env, "CMUX_FLEET_TOML": str(toml), "CMUX_FLEET_PLUGIN_INDEX": str(index)}
    with_flag = run_fleet(env, "launch", "worker", "--label", "w1", "--parent", "FAKE",
                          "--plugin", "extrap", "--dry-run")
    assert f"--plugin-dir {mkt / 'extrap'}" in with_flag.stdout   # unioned onto the ROLE launch (the fix)
    without = run_fleet(env, "launch", "worker", "--label", "w2", "--parent", "FAKE", "--dry-run")
    assert "extrap" not in without.stdout          # control: absent when the flag is not passed


def test_config_renders(cli_env, tmp_path):
    # `fleet config --cwd <dir>` reads the settings stack; no cmux, no roster needed. Pin a toml with
    # NEITHER [role.adhoc] NOR a [tool.claude] floor to prove the pure-cwd probe stays roster-independent
    # (5d: --adhoc is a rostered alias, but a bare --cwd inspect must not require it).
    toml = tmp_path / "fleet.toml"
    toml.write_text('[role.worker]\ncwd = "agents/w"\n[role.worker.claude]\n')
    env = {**cli_env, "CMUX_FLEET_TOML": str(toml)}
    p = run_fleet(env, "config", "--cwd", str(tmp_path))
    assert "fleet config" in p.stdout and "FLEET ADDS" in p.stdout


def _applied_line(stdout):
    # the single "CLAUDE.md applied: ..." line (NOT the suppressed-note line under it)
    return next((ln for ln in stdout.splitlines() if "CLAUDE.md applied:" in ln), "")


def test_config_claude_md_gated_on_setting_sources(cli_env, tmp_path):
    # `fleet config` must not claim CLAUDE.md loads when the role's setting_sources drops the project layer.
    # A default role (no setting_sources = claude's native default, project ON) DOES load the cwd CLAUDE.md;
    # a role narrowed to "user,local" (project OFF) does NOT — the F1 floor is dead-on-arrival there. ROOT is
    # an isolated tmp with no CLAUDE.md, so the cwd file is the only project-scope floor in play.
    proj = tmp_path / "proj"
    proj.mkdir()
    claude_md = proj / "CLAUDE.md"
    claude_md.write_text("# floor\n")
    toml = tmp_path / "fleet.toml"
    toml.write_text('[role.deflt]\ncwd = "agents/d"\n[role.deflt.claude]\n'
                    '[role.narrow]\ncwd = "agents/n"\n'
                    '[role.narrow.claude]\nsetting_sources = "user,local"\n')
    env = {**cli_env, "CMUX_FLEET_TOML": str(toml)}

    # default sources: project ON -> the cwd CLAUDE.md is claimed as applied
    d = run_fleet(env, "config", "deflt", "--cwd", str(proj))
    assert str(claude_md) in _applied_line(d.stdout)

    # narrowed to user,local: project OFF -> the cwd CLAUDE.md is NOT on the applied line, and the reason
    # (present-but-suppressed) is surfaced so the diagnostic doesn't silently drop it.
    n = run_fleet(env, "config", "narrow", "--cwd", str(proj))
    assert str(claude_md) not in _applied_line(n.stdout)
    assert "NOT loaded" in n.stdout and "excludes 'project'" in n.stdout and str(claude_md) in n.stdout


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
