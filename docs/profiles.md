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
roster from `fleet.toml.example`). After `eval`, that shell, and everything it launches, is pinned to
that build and profile. Defaults:

- `CMUX_STATE_DIR`  -> `$XDG_STATE_HOME/cmux-fleet-<name>`
- `CMUX_FLEET_TOML` -> `$XDG_CONFIG_HOME/cmux-fleet-<name>/fleet.toml`
- `CMUX_FLEET_ROOT` -> `$HOME` (override with `--root DIR`)
- `CMUX_FLEET_MARKETPLACE` -> the build's parent dir, so a roster `plugins = ["<build-dirname>"]` loads this build
- `PATH` -> the build's `bin/` first

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

# 4. start THIS build's own router daemon against THIS profile's state
fleet daemon start
fleet daemon status                             # confirm it routes this profile's state dir

# 5. work in this shell: every `fleet ...` and every agent it launches is pinned to the dev build
fleet launch --adhoc scratch
fleet ls
```

A second shell with `eval "$(.../cmux-fleet/bin/fleet profile prod)"` runs the stable build at the same
time. The two share no state dir, no roster, no router, and no plugin code path.

## Developing cmux-fleet with cmux-fleet (the meta-dogfood loop)

> Status: intended workflow, **not yet run end to end.** The pieces (profiles, `fleet daemon`, worktrees)
> each work in prod; the combined dev-on-dev loop below has not been exercised. Treat the steps as the
> plan, and verify each one the first time you run it.

Prod runs on the stable build at the XDG homes. To change cmux-fleet itself, run a **dev profile** on a
second checkout so a broken router, a bad hook, or a wedged agent in dev cannot touch the prod fleet. The
dev build gets its own state, config, router daemon, and workspace group; git worktrees give each change
its own branch. You develop cmux-fleet using cmux-fleet, off to the side, with zero prod blast radius.

Stand it up (in a dedicated shell, so the `eval` does not leak into prod shells):

```sh
# 1. dev build = a second checkout (or a worktree of the repo)
git clone <repo> ~/builds/cmux-fleet-dev

# 2. activate its profile: isolated state / config / PATH / marketplace
eval "$(~/builds/cmux-fleet-dev/bin/fleet profile dev --init)"
cp ~/builds/cmux-fleet-dev/profiles/test.fleet.toml "$CMUX_FLEET_TOML"

# 3. start the dev profile's own daemon (routes the dev state dir only)
fleet daemon start
fleet daemon status                    # confirm state = the dev CMUX_STATE_DIR
ps aux | grep 'router.py --live'       # prod's router + this one; no strays

# 4. launch a sandbox conductor in its own group and drive it
fleet launch sandbox-conductor         # auto-anchors its own workspace group
```

The sandbox conductor is itself running the dev build's plugin, so it exercises your in-progress changes.
Give it a coding role with `worktree = true` (see `docs/operations.md`) and each change lands on its own
`fleet/<label>` branch, isolated from the dev build's own working tree.

Tear it down when the change is done:

```sh
fleet rm sandbox-conductor --with-group   # dissolve the group, sweep its members
fleet daemon stop                         # stop the dev router (from the dev shell)
```

Reclaim any worktrees the sweep left behind (`--with-group` deletes the registry rows, so `fleet worktree
clean` can no longer find them): `git worktree list`, then `git worktree remove <path>` and, if you want
it gone, `git branch -D fleet/<label>`. Prod is untouched throughout: it kept its own state dir, roster,
and daemon the whole time.

## Sandbox / acceptance profile

`profiles/test.fleet.toml` is a ready isolated roster (a sandbox conductor + one worker, both loading
this build's plugin, cwds under a scratch root). Activate the `test` profile, copy that roster in, start
the profile's router, then exercise `launch` / `recycle` / `vitals` / `find` / `graph` / `worktree`
with zero effect on any other build's state. Point `CMUX_FLEET_ROOT` (`--root`) at a scratch dir so the
sandbox agents' cwds never land in a real project.

## Workspace groups: one conductor = one group

A conductor that launches with `place = workspace` anchors its **own** cmux workspace-group, so the
conductor and all its children form one collapsible sidebar group: clean visual separation per build.

- **Auto-anchor (no pre-create).** On launch, if the conductor's group does not exist, the fleet creates
  it anchored on the conductor's own new workspace (`workspace-group create --from <that workspace>`,
  always with an explicit `--from` so it never adopts the caller's workspace). If the group already
  exists, the agent just joins it.
- **Default group name.** A conductor with no explicit `group` defaults it to the conductor's **label**,
  so every conductor gets its own group with zero config. Set `group = "..."` for a friendlier name.
- **Children join the parent's group.** `place = tab|pane` children live in the conductor's workspace
  already; a `place = workspace` child with no explicit group joins its parent conductor's group.
- **Lifecycle.** `recycle` and `revive` preserve the group (the surface stays in place, or is recreated
  into the existing group). `fleet rm <conductor>` removes only that workspace by default and leaves any
  other members ungrouped; `fleet rm <conductor> --with-group` dissolves the whole group (deletes it by
  ref, closing every member) and sweeps all of the group's members out of the registry. Swept members'
  worktree dirs and branches are left unmanaged (their registry rows are gone, so `fleet worktree clean`
  cannot find them); reclaim manually with `git worktree list` / `git worktree remove <path>` (and
  `git branch -D fleet/<label>` for the branch).

So a second build's sandbox conductor lands in its own separate group, and tearing the build down is one
`fleet rm sandbox-conductor --with-group`.

## Gotchas

- Always run `fleet daemon start` **inside** the activated shell (so the router reads the profile's
  `CMUX_STATE_DIR`). `fleet daemon` is per-state: started without the profile it manages a different
  build's router. `fleet daemon status` shows which state dir it routes; `ps aux | grep 'router.py
  --live'` should show one router per active profile, no more.
- `eval` runs the env block in the **current** shell. A subshell or a new terminal needs its own `eval`.
- The build's directory basename is the plugin name the roster's `plugins = [...]` resolves to under
  `CMUX_FLEET_MARKETPLACE`. If you rename the dir, update the roster or use an absolute plugin path.
