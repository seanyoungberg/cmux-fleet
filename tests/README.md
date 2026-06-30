# tests

Test suite lands in P3.2. Planned layers (see `../docs/` and CONTRIBUTING.md):

- **static** — manifest / skill / hooks JSON well-formedness + schema.
- **unit** (pytest) — `config.py` resolution precedence (env > `[fleet]` toml > XDG default),
  `fleet_state` transitions (inbox / registry / archive), the hook stdin -> exit-code contract
  (inject a throwaway `$CMUX_STATE_DIR`).
- **e2e** — CLI lifecycle (launch -> ls -> recycle -> archive -> revive -> rm) against a throwaway
  cmux, and `claude --plugin-dir` load.
