# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-01

Packaging release. cmux-fleet is now an installable **uv tool** (`fleet` CLI + a
supervised, launchd-persistent router daemon) with a **thin, fail-open Claude
plugin** (hook shims resolve `fleet` on PATH; no baked app spec). Folds in the
daemon-hardening, resume-menu gate, and `fleet register` work landed since v0.1.0.
This is the version the live fleet was cut over to on 2026-07-01.

### Added

- **`fleet daemon start --foreground` for launchd/systemd (packaging P2.3).** Runs
  the supervised router in the current process — no fork/detach/stdio-redirect — so
  a supervisor's KeepAlive owns it directly and captures its output. Preserves the
  `daemon <start|stop|status|restart>` grammar (a parser test pins the exact plist
  command; a bare `fleet daemon --foreground` is rejected). `restart` still
  re-detaches. `fleet daemon status` now also reports **which build owns the
  daemon** — app `version`, the `python` running it, and the `cmux_fleet` package
  dir (recorded in `router.daemon.json` at start) — so a migration can prove the
  code path, not just state dir + pid (P2.5).
- **Two-step install + migration runbook (packaging P1.4/P2.5).** README quickstart
  is now two independent installs — `uv tool install` the app, then install the
  plugin your way (the app installer never touches the plugin). `docs/operations.md`
  gains a launchd reboot-persistence section (plist + bootstrap/kickstart/bootout)
  and an app/plugin **cutover runbook** written around real running-process
  behavior: it gates prod cutover on the Phase 3 thin shims, inventories each live
  agent's baked PATH/`--plugin-dir`/`CMUX_*`, forces an explicit
  repointable-path-vs-recycle decision, records current-vs-new daemon build
  identity + the `hash -r`/`rehash` rollback caveat + launchd ordering, and does
  NOT advertise "no conductor recycle" until a staging run proves a live conductor
  resolves the installed app for hook verbs.

- **Router daemon manager** (`scripts/fleet_daemon.py`). `fleet daemon
  start|stop|status|restart` runs `router.py --live` as a properly detached
  daemon: `start` double-forks with `setsid` (its own session, no controlling
  terminal) and leads its own process group, so the router survives the starting
  shell exiting, an agent Bash-tool process-group cleanup, and a conductor
  self-recycle (a bare `nohup &` router does not; it also risks surviving as a
  stray duplicate that double-processes the bus). Pidfile (supervisor pid), meta,
  and log under `$CMUX_STATE_DIR` (one set per state/profile); refuses to
  double-start, cleans a stale pidfile, and `stop` signals the whole process
  group (router included). `--heartbeat [SECS]` adds a Tier-1 tick (default 540s)
  that re-nudges only LIVE-IDLE conductors with a pending inbox through the
  input-safe `wake_if_idle` gate (skips busy/human-draft/muted/non-conductor);
  no dead-session detection or auto-recycle. `restart` preserves the running
  heartbeat setting unless overridden.

- **`fleet register <label> [--surface UUID] [--parent] [--session]`.** Manual
  escape hatch to pull a LIVE-but-unregistered agent into the registry — recovery
  for a skipped auto-register (see the resume gate below) or an agent launched
  outside fleet, for which no command previously existed. Derives
  tool/session/workspace/cwd from the live surface (cmux hook store) and rebuilds
  the spec from the roster role (toml-authoritative), falling back to the
  archive/live entry or the surface's own `AGENT_ROLE`/binding for off-roster
  agents; promotes a parked label to live, idempotent on the same surface, and
  refuses to move a label already live under a different surface.

### Changed

- **Conductor hooks are now thin, fail-open shims over `fleet hook-*` verbs
  (packaging P1.2/P1.3/P1.5).** The awareness/drain logic moved into the app as
  `fleet hook-awareness` (UserPromptSubmit) and `fleet hook-drain` (Stop), in
  `cmux_fleet/hookverbs.py`. The plugin's `scripts/hooks/{awareness,drain}.py` are
  now stdlib-only python shims (`scripts/hooks/_shim.py`) that shell into the
  installed `fleet` and forward its stdout ONLY on rc0 + valid expected-shape JSON;
  every other path (app missing, timeout, nonzero exit, stdout noise, wrong shape)
  fails open with blank stdout and exit 0. **The uvx network fallback was dropped:**
  the plugin requires the `fleet` app on PATH; a per-turn `uvx` in the hot path risked
  first-run/offline cost, private-repo auth latency, and the harness's 10s hook
  timeout killing the shim before it could fail open. Without the app, fleet hooks
  silently no-op and the rest of Claude Code is unaffected. **Version is now
  single-sourced** at `cmux_fleet/__init__.py::__version__` (pyproject reads it via
  `[tool.hatch.version]`); a test keeps plugin.json + marketplace.json in lockstep,
  and there is no hook-fallback pin to sync (one fewer version surface).

- **Agent helpers folded into `fleet` subcommands (packaging P2.1).** The four
  standalone plugin scripts — `scripts/{drive-child,child-digest,peer-msg,inbox-ack}.py`
  — are now `fleet drive-child` / `fleet child-digest` / `fleet peer-msg` /
  `fleet inbox-ack` (bodies in `cmux_fleet/helpers.py`, kept out of the 2k-line
  `cli.py` per P3.1). One app, one entrypoint: a conductor runs the verb via the
  `fleet` on PATH instead of shelling into a per-plugin script path. The
  awareness/drain hook context notes now emit the `fleet <verb>` forms. **Breaking:**
  the old `scripts/<helper>.py` paths are removed; any external caller that copied
  a script path must switch to the subcommand. A new static test
  (`tests/test_static.py::test_no_stale_helper_or_router_script_refs`) fails the
  release if any doc/skill/profile/README/hook still names a deleted
  `scripts/<helper>.py` or `scripts/router.py`.

### Fixed

- **Hook-shim hardening (codex P2-P4 re-review should-fixes).** `scripts/hooks/_shim.py`:
  (1) `$CMUX_FLEET_BIN` is now authoritative — set-but-invalid fails open blank
  instead of falling through to an ambient `which fleet`, so a strategy-A cutover
  can't silently run a stale binary off a live agent's baked `PATH`;
  (2) `CMUX_FLEET_HOOK_TIMEOUT` is clamped below the 10s harness timeout (max 9s;
  bad/oversized values ignored) so an override can't recreate the timed-out-hook
  failure; (3) stricter output validation — a non-string `additionalContext`/`reason`,
  or an awareness payload missing `hookEventName`, is treated as corrupt and blanked.
  Docs (`docs/profiles.md`, `docs/operations.md`) refreshed for the installed-app
  model (profile omits the marketplace pin unless explicit; `fleet` resolves from
  the installed app or a checkout shim).

- **`fleet profile` works from an installed wheel (packaging P1.1).** Phase 1's
  package move broke `fleet profile` for a `uv tool install`/venv install: it
  derived a checkout-style `PLUGIN_ROOT` by walking up from `cli.py`, so a wheel
  install emitted a nonexistent `site-packages/bin` on PATH, pointed
  `CMUX_FLEET_MARKETPLACE` at the Python lib dir, and silently skipped the
  `fleet.toml` seed. Now three concepts are resolved separately: the PATH pin
  comes from the actual invoked `fleet` (`$CMUX_FLEET_BIN` > `sys.argv[0]` >
  `which fleet`, checkout `bin/` only for a real plugin checkout); the
  marketplace pin is emitted only from explicit config or a real checkout (never
  inferred from a wheel's site-packages — omitted otherwise); and the seed roster
  is read via `importlib.resources` (force-included in the wheel), falling back to
  the repo-root example for a checkout. New `tests/test_packaging_smoke.py` builds
  a real wheel, installs it into a throwaway venv, and asserts the installed
  `fleet profile --init` pins the installed console-script dir, seeds the roster,
  and never emits a lib-dir path.

- **Router bus singleton guard (no more double-processing).** A stray
  `router.py --live` on the same bus double-processed every event (during the
  cutover, 3 strays triple-processed the bus and duplicate child completions
  reached conductors). The live router now acquires an exclusive, non-blocking
  `flock` on a per-state lockfile before consuming; a second live router that
  can't get it exits instead of processing in parallel. `fleet daemon start`
  reaps a stray live router holding the lock first (matched by lockfile pid + a
  `ps` cmdline check, so a pid-reused unrelated process is left alone).

- **Recycle/revive auto-resumes the FULL session.** `claude --resume` on an old
  or large session shows a summary-vs-full menu that hung an automated respawn
  and false-passed the confirm (the respawn keyed off a stale session while the
  menu blocked). The relaunch now auto-picks the full session and dismisses the
  menu, so recycle and revive resume complete context instead of stalling or
  silently compacting.

- **Resume-menu watch is event-driven and gates registration.** The dismiss used
  a fixed window that closed before a heavy loadout (e.g. 6 plugins, 30-40s boot)
  rendered the menu, so revive was left at the shell. It now polls for one of
  three states (menu / already-running / still-booting) under a generous,
  plugin-count-scaled ceiling. Crucially, the menu *gates* the session bind, so a
  timed-out dismiss used to fall through and skip `register()` — leaving the agent
  running but UNREGISTERED (a live pane still shown as archived). Revive and
  recycle now abort loudly on timeout instead of half-binding (revive before
  `archive_del`, so the label stays parked and re-runnable).

## [0.1.0] - 2026-06-30

Initial port. The native-cmux parent/child orchestration spine, extracted and
cleaned from an internal `cmux-conductor` plugin and decoupled from any single
vault or machine.

### Added

- **Git worktrees** (`scripts/worktree.py`, config-gated, default-off). A role
  with `worktree = true` (or `fleet launch ... --worktree`) runs each agent in
  its own worktree at `<repo>/.worktrees/<label>` on branch `fleet/<label>`. The
  fleet is the sole owner: it runs `git worktree add` itself and launches the
  tool into the directory (`claude` plain, no `-w`; codex via `cd`), strips
  Claude's `-w`/`--worktree` from passthrough, and never hooks
  `WorktreeCreate`/`WorktreeRemove`. Idempotent create under a per-repo lock;
  teardown refuses on a dirty tree (`--wip-commit` overrides) and always keeps
  the branch. New verbs `fleet worktree ls`/`clean <label>`; `fleet rm --kill`
  tears the tree down; post-launch placement reconciliation fails loud if a
  workspace collapsed off the worktree. Roster keys `worktree`/`worktree_base`/
  `worktree_dir`/`worktree_branch_prefix`; CLI `--worktree [BRANCH]`/
  `--no-worktree`/`--worktree-base`.
- **`fleet` CLI** (`scripts/fleet.py`, on PATH via `bin/fleet`). Tool-agnostic
  command builder over cmux primitives. Verbs: `launch`, `config`, `ls`,
  `archive`, `revive`, `recycle`, `broadcast`, `mute`/`unmute`, `rm`. Roster is
  role-first and tool-nested (`[defaults]`, `[tool.<t>]`, `[role.<name>]`,
  `[role.<name>.<t>]`); claude and codex adapters, with full `--` flag
  passthrough. Off-roster agents via `--adhoc`.
- **Router daemon** (`scripts/router.py`). One process serves every conductor.
  Listens on cmux's agent event bus, maps a child `Stop` to its parent via the
  live registry, and delivers a completion. Bus is the doorbell, cmux's hook
  store is truth, the transcript is content.
- **Unified, input-safe inbox** (`scripts/fleet_state.py`). One append-only
  stream folds child completions and peer messages, with per-surface, per-kind
  ack cursors. Completions and peer messages reach an agent through context, not
  its input box.
- **Two conductor hooks**. `awareness.py` (UserPromptSubmit) injects the pending
  inbox into context each turn. `drain.py` (Stop) auto-continues a turn to
  process pending work, gated by the mode dial.
- **Peer messaging** (`scripts/peer-msg.py`) and supporting helpers
  (`scripts/child-digest.py`, `scripts/inbox-ack.py`, `scripts/drive-child.py`).
- **`config.py` decoupling**. One path/setting resolver, precedence
  `env > [fleet] toml > XDG default`. Introduces `CMUX_FLEET_ROOT`; moves state
  under `$XDG_STATE_HOME` (`CMUX_STATE_DIR`); resolves the cmux binary via
  `which(cmux)` with a macOS app-bundle fallback (`CMUX_BIN`); makes the
  marketplace and floor `CLAUDE.md` optional (default off, no vault assumption).
- **Plugin packaging**. The repo is its own Claude Code marketplace
  (`marketplace.json` source `./`, strict), with `plugin.json`, `hooks.json`,
  and a conductor skill under `skills/`.
- **Fleet views** (`scripts/fleet_features.py`). Read-only, derived from live
  state every call (no daemon, no stored status). Status is inferred **without an
  LLM** (cmux `agentLifecycle` authoritative, refined by keyword tables).
  - `fleet vitals [--json] [--paint]`: cheapest-first triage table, most-urgent
    first, with each agent's **context-remaining %** (`!` flags <=30% left).
    Window configurable via `CMUX_FLEET_CONTEXT_WINDOW` / `[fleet].context_window`.
  - `fleet find <query> [--turns N] [--json]`: content-aware session lookup
    (label / role / cwd, or the agent's recent transcript).
  - `fleet graph [--html] [--out FILE]`: parentage tree (cycle-safe), text or a
    self-contained HTML page.
  - `fleet serve [--port N]`: thin read-only localhost view (graph HTML +
    `/vitals.json`); no daemon, no actions, no analytics.
  - `fleet paint`: native cmux sidebar telemetry: a status pill + context
    progress bar per workspace, on change only, additive.
  - Custom sidebar `sidebars/fleet.swift` (the cmux custom-sidebar mechanism) for
    a dedicated, tappable fleet board.
  - Unit tests in `tests/test_features.py`. Triage/no-LLM-status ideas adapted
    from agentmaster, the localhost-view shape from elevens (design-mined).
- **Test suite** (`tests/`, pytest-only, the one dev dependency). Three layers:
  static manifest/skill/hooks schema validators; pytest units for `fleet_state`
  transitions (inbox/registry/archive/dial), the hook stdin->exit-0 contract,
  `config.py` resolution precedence (`env > [fleet] toml > XDG`, dirname-anchor,
  malformed-warn), `scripts/worktree.py`, and the `fleet_features` views; and an
  e2e CLI lifecycle (`ls`/`archive`/`revive`/`rm` against a throwaway state with a
  stubbed cmux) plus a `claude --plugin-dir` load check. One skip expected (real
  claude load, skipped when no headless `claude` is present).

- **Multi-build isolation (profiles).** `fleet profile <name> [--base DIR] [--root DIR] [--init]`
  emits a sourceable env block that pins every entrypoint (the `fleet` CLI via PATH, `CMUX_STATE_DIR`,
  `CMUX_FLEET_TOML`, `CMUX_FLEET_ROOT`, `CMUX_FLEET_MARKETPLACE`, `CMUX_BIN`) at one build, so
  independent builds run side by side with no shared config, state, or daemons. The launcher now
  injects those same paths into every child it spawns (`_profile_env`), so a conductor and all its
  descendants, and their hooks, stay on one build regardless of a child shell's ambient env (the
  hermetic guarantee). Ships `profiles/test.fleet.toml` (a sandbox roster) and `docs/profiles.md`
  (the permanent dev workflow for standing up an Nth build).

- **Built-in workspace-group handling (one conductor = one group).** A `place = workspace` conductor
  now anchors its own cmux workspace-group instead of aborting when the group does not exist: the fleet
  creates it on the conductor's own new workspace via `workspace-group create --from <that workspace>`
  (always an explicit `--from`, never the caller-adopting implicit form). A conductor with no explicit
  `group` defaults it to its label; a `place = workspace` child joins its parent conductor's group.
  Group name->ref resolution is centralized (`_group_ref`) so teardown uses a ref as cmux requires.
  `recycle`/`revive` preserve the group; `fleet rm <label> --with-group` dissolves it and sweeps all of
  the group's members out of the registry (worktree branches are kept), while plain `rm` leaves members
  ungrouped. The sandbox profile is now turnkey (no manual `workspace-group create`).

### Fixed

- **Recycle relaunch is timing- and crash-safe.** A recycled agent relaunched
  into a fresh login shell whose `PATH` had not finished resolving the real
  binary, so the cmux wrapper exited 127 (`claude not found in PATH`); the
  fresh-mode confirm then matched a stale hook-store session id and reported
  success, priming a dead shell. Now the relaunch is `PATH`-guarded
  (`~/.local/bin` + homebrew prepended), the pre-relaunch session id is snapshotted
  and excluded from the fresh-mode confirm (a crashed launch resolves to "no
  session" instead of false success), the launch self-heals by re-firing once if
  no fresh session binds, and the post-respawn settle is 2s -> 3s.

[Unreleased]: https://github.com/seanyoungberg/cmux-fleet/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/seanyoungberg/cmux-fleet/releases/tag/v0.1.0
