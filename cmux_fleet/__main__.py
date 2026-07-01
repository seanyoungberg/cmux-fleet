"""`python -m cmux_fleet ...` — the module entry, same as the `fleet` console script. Used by the
self-spawns (router/daemon, recycle-exec) and the checkout `bin/fleet` shim."""
from cmux_fleet.cli import main

raise SystemExit(main())
