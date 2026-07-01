# tests

The cmux-fleet test suite. stdlib + **pytest only** (the one dev dependency); no plugins, no
external fixtures.

## Run

```sh
python3 -m venv .venv && .venv/bin/pip install pytest   # one-time
.venv/bin/python -m pytest tests/ -q
```

Latest: **117 tests.** All pass where a headless-runnable `claude` is on PATH; otherwise the one real
`claude --plugin-dir` load test skips, leaving **116 passed, 1 skipped**.

## Layers

- **static** (`test_static.py`): `plugin.json` / `marketplace.json` / `hooks.json` parse and match
  the schema (kebab name, semver, hook commands use `${CLAUDE_PLUGIN_ROOT}` and resolve to real
  scripts, manifest/marketplace versions agree); every skill ships a `SKILL.md` with `name` +
  `description` frontmatter.
- **unit** (pytest)
  - `test_config.py`: `config.py` resolution precedence `env > [fleet] toml > XDG/default`, the
    dirname-anchor (relative toml path resolves against the toml's dir; relative env path against cwd
    with a warning), and the malformed-vs-absent toml behavior (malformed warns + falls back; absent
    is silent). Each scenario runs in a fresh interpreter because `config.py` resolves at import time.
  - `test_fleet_state.py`: inbox put/pending/per-kind-ack/seq, the live registry, the archive
    shelf, the live<->archive transition, and the notify-mode dial.
  - `test_hooks.py`: the `awareness.py` / `drain.py` stdin->exit-0 contract: silent on an empty
    inbox, structured JSON when there is work, peer-always-drains vs completion-dial-gated, the
    block-guard that prevents a re-block loop, and fail-open on garbage stdin. Runs the hooks as real
    subprocesses against a throwaway `$CMUX_STATE_DIR`.
  - `test_worktree.py`: `scripts/worktree.py` units (repo discovery, base resolution, idempotent
    create, gitignore hygiene, fail-closed dirty check, refuse-if-dirty / `--wip-commit` teardown,
    branch-survives, one-owner flag stripping) plus a subprocess CLI pass (`fleet launch --worktree
    --dry-run` cwd-swap + `-w` strip; `--no-worktree` override). Runs against a throwaway git repo,
    no cmux/network.
  - `test_features.py`: the `fleet_features` read-only views (`vitals` triage ordering + context %,
    `find` matching, `graph` parentage tree, status inference without an LLM).
  - `test_recycle.py`: `_poll_session_back` exclude semantics (fresh-mode excludes old + pre sids; a
    new sid confirms; resume confirms on a live lifecycle and waits while ended).
  - `test_profile.py`: `fleet profile` emits all entrypoint pins (PATH + the CMUX_* knobs), `--base`
    layout, marketplace resolves this build, and `_profile_env` is absolute + complete.
  - `test_groups.py`: workspace-group name->ref resolution; bootstrap uses an explicit `--from`;
    join-when-exists doesn't recreate; conductor group defaults to label; `rm --with-group` dissolves
    by ref, without it leaves the group intact.
  - `test_daemon.py`: `fleet daemon` pidfile/liveness (status-not-running, stale-pidfile cleanup,
    start-refuses-if-running, stop-when-not-running) and the heartbeat filter (nudges only idle
    conductors with pending inbox; skips busy/muted/child). The double-fork daemonize is smoke-tested.
- **e2e**
  - `test_e2e_cli.py`: the CLI lifecycle `ls -> archive -> revive -> rm` driven as real subprocess
    invocations against the throwaway state with a **stubbed cmux binary**. Real launch/revive need a
    live cmux, so those run through their `--dry-run` compose path; the state-moving verbs (archive,
    rm) run for real and the transitions are asserted in `fleet.json`/`archive.json`.
  - `test_e2e_plugin_load.py`: a structural "would-load" validator (always runs) plus the real
    `claude --plugin-dir` load (skipped when no headless `claude` is available).

## How isolation works

`config.py` reads `$CMUX_STATE_DIR` **at import time**, so `conftest.py` points it at a throwaway dir
before any test imports `config`/`fleet_state`, and an autouse fixture wipes that dir between tests.
Subprocess tests (hooks, CLI) inherit the same `$CMUX_STATE_DIR` so in-process seeding is visible;
config-resolution tests spawn fresh interpreters with their own env/toml.
