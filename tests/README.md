# tests

Test suite lands in P3.2. Planned layers (see `../docs/` and CONTRIBUTING.md):

- **static** — manifest / skill / hooks JSON well-formedness + schema.
- **unit** (pytest) — `config.py` resolution precedence (env > `[fleet]` toml > XDG default),
  `fleet_state` transitions (inbox / registry / archive), the hook stdin -> exit-code contract
  (inject a throwaway `$CMUX_STATE_DIR`).
- **e2e** — CLI lifecycle (launch -> ls -> recycle -> archive -> revive -> rm) against a throwaway
  cmux, and `claude --plugin-dir` load.

## Landed

- `test_worktree.py` (pytest, 13 tests) — `scripts/worktree.py` units (repo discovery, base
  resolution, idempotent create, gitignore hygiene, fail-closed dirty check, refuse-if-dirty /
  `--wip-commit` teardown, branch-survives, one-owner flag stripping) plus a subprocess CLI pass
  (`fleet launch --worktree --dry-run` cwd-swap + `-w` strip; `--no-worktree` override). Runs against
  a throwaway git repo, no cmux/network. Run: `uvx pytest tests/test_worktree.py -q` (or `pytest`).
