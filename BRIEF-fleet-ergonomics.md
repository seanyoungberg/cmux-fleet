# BRIEF — fleet ergonomics fixes (held dev, unattended-mandate night)

**From:** cmux-advisor · **To:** redesign-builder (you built this machinery) · **2026-07-18 eve**

## Context
I adopted the full Ship 5 stack tonight — **v0.11.0 is LIVE** (schema v2, `fleet migrate` run, tagged).
Berg is away and gave an unattended mandate to fix rough edges on sight. Three fleet-ergonomics fixes, all
in `cli.py`/`state.py` you wrote. These came out of tonight's real friction (berg-sandbox burned ~20 min
looping on a self-recycle).

## ⛔ HARD RULES
- **Work in an ISOLATED WORKTREE off main. Do NOT touch the main checkout** — it is the live plugin symlink
  AND the running v0.11.0 source.
  `git worktree add .worktrees/fleet-ergonomics -b dev/fleet-ergonomics main` then work there.
- **Develop + TEST green, then STOP.** Do NOT merge to main. Do NOT `uv tool install` / adopt.
  Leave the branch ready + report to me. I review; Berg adopts in the morning.
- Additive only; no architectural changes. Step-0: read `cmd_recycle`, `_quiet_gate`, `cmd_move`,
  `cmd_register`, `live_update`, `_lifecycle_owner_guard` before editing.

## FIX 1 — self-recycle self-detection (Berg called this out explicitly)
**Root cause (diagnosed live):** berg-sandbox's `fleet recycle --fresh` (non-forced) ABORTed 4× with
"surface never went quiet within 180s". `_quiet_gate` (cli.py ~4335) blocks a non-forced recycle until the
target surface reads `lifecycle != running` AND empty-draft. A **self-recycle can never satisfy that from
inside its own turn** — the caller IS the running activity the gate waits to clear, so it deadlocks to the
180s ABORT. (`--force` short-circuits the gate, which is why the one attempt that passed `--force` worked.)

**Fix:** in `cmd_recycle`, detect when the resolved target surface == the caller's own `$CMUX_SURFACE_ID`
(a self-recycle). My lean: **auto-apply force with a clear one-line notice** — e.g. `self-recycle: forcing
respawn (your own turn keeps the surface 'running'; the quiet-gate cannot clear from here)` — because the
caller explicitly asked to recycle itself and there is no human draft to protect. If you judge
fail-fast-with-guidance safer (print the reason + the `--force` remedy, exit non-zero, respawn nothing),
make that call and say why in your report. **Non-negotiable outcome: a self-recycle never silently burns
180s to an ABORT again.** Add a test proving a self-targeted recycle does not deadlock.

## FIX 2 — `fleet reparent <label> <parent|none>` verb
There is no clean in-place reparent today: `move` forces a workspace move + reparents under the caller;
`register` rebuilds and clobbers the whole spec. Tonight I set `cmux-advisor.parent=None` via the raw state
API because no verb existed. **Add a surgical verb:** reparent a live agent's registry `parent` ONLY (to
another label, or `none` → `None` for top-level), flocked via `live_update`, every other field preserved.
Apply the **cross-conductor ownership guard** (`_lifecycle_owner_guard`-style): reparenting a *different*
conductor's child needs the same `--force` + parent-notify as archive/rm; your own child / your own node /
anonymous CLI = allowed. Tests: `reparent X none` → parent None + `is_top_level`; `reparent X label` → that
parent; guard fires (refuse w/o `--force`, notify) for another conductor's child.

## FIX 3 — recycle writes back resolved `spec.plugins` (registry must not lie)
berg-sandbox relaunched with 6 plugins (from the toml) but its registry row still records 4. When recycle
recomposes the launch from the roster/toml, **write the resolved plugin set back to the row** (and any other
resolved spec drift you cleanly can). Test: a recycle whose toml plugin set differs from the recorded set
updates the row to match what actually launched.

## Acceptance (receipts required)
- Full suite green — expect **1120 + your new tests** — paste the summary line.
- Each fix has a test.
- A demonstration receipt for FIX 1 that a self-recycle no longer deadlocks (a dry-run or a test is fine;
  do NOT actually recycle a live fleet member).
- Report back to me (your completion rides to parent). **Do NOT adopt.** Model stays Opus xhigh.
