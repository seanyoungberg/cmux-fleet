# Profiles: running independent builds side by side

cmux-fleet has no compile step. A **build** is just a checkout directory, and which build is "active"
is decided by several wiring points at once, not one. A **profile** is the single switch that pins all
of them at one build so two builds (say a stable one and a dev one) run with fully separate config,
state, and daemons. Nothing is shared.

## What a profile pins

| Wiring point | How it resolves | Pinned by |
| --- | --- | --- |
| `fleet` CLI | PATH order finds a `bin/fleet`, which execs the `scripts/fleet.py` next to it | `PATH` (build's `bin/` first) |
| hooks + skills | the `--plugin-dir` baked into each agent's launch command | `CMUX_FLEET_MARKETPLACE` + the roster's `plugins` |
| router daemon | reads `CMUX_STATE_DIR` from the env it was started in | `CMUX_STATE_DIR` |
| state (registry/inbox/archive) | `config.py` resolves it at import | `CMUX_STATE_DIR` |
| roster | `config.py` resolves it at import | `CMUX_FLEET_TOML` |
| relative role cwds | composed against the root | `CMUX_FLEET_ROOT` |
| cmux binary | `CMUX_BIN`, else `which cmux` | `CMUX_BIN` |

PATH alone is not enough: a conductor's `fleet` could resolve to one build while its hooks resolve to
another. So `fleet profile` sets the whole set, and the launcher **injects the same paths into every
child it spawns** (`_profile_env`), so a conductor and all its descendants stay on one build even if a
child's shell carries different ambient values. That is the hermetic guarantee.

## Activate a profile

```sh
eval "$(/path/to/<build>/bin/fleet profile <name> --init)"
```

`fleet profile` prints a sourceable env block (and `--init` also creates the state dir and seeds the
roster from `fleet.toml.example`). After `eval`, that shell — and everything it launches — is pinned to
that build and profile. Defaults:

- `CMUX_STATE_DIR`  → `$XDG_STATE_HOME/cmux-fleet-<name>`
- `CMUX_FLEET_TOML` → `$XDG_CONFIG_HOME/cmux-fleet-<name>/fleet.toml`
- `CMUX_FLEET_ROOT` → `$HOME` (override with `--root DIR`)
- `CMUX_FLEET_MARKETPLACE` → the build's parent dir, so a roster `plugins = ["<build-dirname>"]` loads this build
- `PATH` → the build's `bin/` first

Use `--base DIR` to keep one profile's state and toml together under a single dir instead of the XDG
defaults.

## Stand up an Nth build (the permanent dev workflow)

```sh
# 1. get the build (a second checkout; the dir basename is the plugin name the roster references)
git clone <repo> ~/builds/cmux-fleet-dev        # or copy; any path

# 2. activate its profile in a shell (state/config/PATH/marketplace all isolated)
eval "$(~/builds/cmux-fleet-dev/bin/fleet profile dev --init)"

# 3. give it a roster (or start from the seeded one)
cp ~/builds/cmux-fleet-dev/profiles/test.fleet.toml "$CMUX_FLEET_TOML"

# 4. start THIS build's own router against THIS profile's state
python3 ~/builds/cmux-fleet-dev/scripts/router.py --live &

# 5. work in this shell: every `fleet ...` and every agent it launches is pinned to the dev build
fleet launch --adhoc scratch
fleet ls
```

A second shell with `eval "$(.../cmux-fleet/bin/fleet profile prod)"` runs the stable build at the same
time. The two share no state dir, no roster, no router, and no plugin code path.

## Sandbox / acceptance profile

`profiles/test.fleet.toml` is a ready isolated roster (a sandbox conductor + one worker, both loading
this build's plugin, cwds under a scratch root). Activate the `test` profile, copy that roster in, start
the profile's router, then exercise `launch` / `recycle` / `vitals` / `find` / `graph` / `worktree`
with zero effect on any other build's state. Point `CMUX_FLEET_ROOT` (`--root`) at a scratch dir so the
sandbox agents' cwds never land in a real project.

## Gotchas

- Always start the router **inside** the activated shell (so it reads the profile's `CMUX_STATE_DIR`).
  A router started without the profile serves a different build's state.
- `eval` runs the env block in the **current** shell. A subshell or a new terminal needs its own `eval`.
- The build's directory basename is the plugin name the roster's `plugins = [...]` resolves to under
  `CMUX_FLEET_MARKETPLACE`. If you rename the dir, update the roster or use an absolute plugin path.
