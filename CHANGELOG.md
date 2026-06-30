# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - unreleased

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
  - `fleet vitals [--json] [--paint]` — cheapest-first triage table, most-urgent
    first, with each agent's **context-remaining %** (`!` flags ≤30% left).
    Window configurable via `CMUX_FLEET_CONTEXT_WINDOW` / `[fleet].context_window`.
  - `fleet find <query> [--turns N] [--json]` — content-aware session lookup
    (label / role / cwd, or the agent's recent transcript).
  - `fleet graph [--html] [--out FILE]` — parentage tree (cycle-safe), text or a
    self-contained HTML page.
  - `fleet serve [--port N]` — thin read-only localhost view (graph HTML +
    `/vitals.json`); no daemon, no actions, no analytics.
  - `fleet paint` — native cmux sidebar telemetry: a status pill + context
    progress bar per workspace, on change only, additive.
  - Custom sidebar `sidebars/fleet.swift` (the cmux custom-sidebar mechanism) for
    a dedicated, tappable fleet board.
  - Unit tests in `tests/test_features.py`. Triage/no-LLM-status ideas adapted
    from agentmaster, the localhost-view shape from elevens (design-mined).
- **Test suite** (`tests/`, pytest-only — the one dev dependency). Three layers:
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
  descendants — and their hooks — stay on one build regardless of a child shell's ambient env (the
  hermetic guarantee). Ships `profiles/test.fleet.toml` (a sandbox roster) and `docs/profiles.md`
  (the permanent dev workflow for standing up an Nth build).

- **Built-in workspace-group handling (one conductor = one group).** A `place = workspace` conductor
  now anchors its own cmux workspace-group instead of aborting when the group does not exist: the fleet
  creates it on the conductor's own new workspace via `workspace-group create --from <that workspace>`
  (always an explicit `--from`, never the caller-adopting implicit form). A conductor with no explicit
  `group` defaults it to its label; a `place = workspace` child joins its parent conductor's group.
  Group name->ref resolution is centralized (`_group_ref`) so teardown uses a ref as cmux requires.
  `recycle`/`revive` preserve the group; `fleet rm <label> --with-group` dissolves it (default leaves
  members ungrouped). The sandbox profile is now turnkey (no manual `workspace-group create`).

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

[0.1.0]: https://github.com/seanyoungberg/cmux-fleet/releases/tag/v0.1.0
