# `fleet reap-surfaces` — orphaned bare-shell husk reaper

Closes the orphaned bare-shell **husk** surfaces that accumulate in fleet workspaces: a terminal surface carrying a fleet launch artifact but with **no live agent** and **no registry entry**. Two sources:

- **Reboot-restore replay** — cmux's app-level session-restore reopens a stale surface and replays its captured launch command as an inert login shell (the command sits unsubmitted at the prompt).
- **Exited-agent** — a fleet agent's `claude` ran, printed `Resume this session with: claude --resume <id>`, and exited; the bare shell remains, and the fleet never archived it.

`reap-surfaces` is the **only** fleet verb that closes a live cmux surface, so it is **DRY-RUN by default** and the classifier carries the whole safety burden.

## The safety gate (a surface is a husk candidate only if ALL hold)

Exclude on any of:
- it is a **live fleet member's** surface (registry);
- a **live agent** occupies it: a live pid + non-terminal lifecycle in EITHER hook store (`surface_has_live_agent`, the union of claude + codex stores — codex-aware), OR a painted agent TUI;
- teardown window (`expected_close_recent`) or a failed re-verify (the `--close` path);
- **human activity after the launch artifact** — any human-typed command after the fleet launch line (the tail guard).

Require (the positive fingerprint):
- a bare login shell carrying the **fleet env prefix** (`AGENT_ROLE=` / `AGENT_LABEL=` / `CMUX_FLEET_*`) as the pane's **tail**. This env prefix is emitted only by `fleet launch`/recycle; a human's shell never contains it, which is what makes closing a matched surface safe.

The pure classifier is `cli._husk_evidence` (unit-tested in `tests/test_reap_surfaces.py` with fixtures distilled from real live panes).

## Rollout

- `fleet reap-surfaces` — DRY-RUN: classify every terminal surface into tracked / live-agent / human-shell / husk-candidate; print candidates + per-candidate evidence + the identity that `--close` would archive. **Closes nothing.**
- `fleet reap-surfaces --all` — survey every workspace (default: fleet-managed only; a husk with a fleet `AGENT_LABEL` is always in scope regardless of workspace).
- `fleet reap-surfaces --close` — **review-gated**, refuses until signed off. When built: for each candidate, harvest `AGENT_LABEL` + `claude --resume <id>`, write the fleet **archive record first** (never close a resume pointer we cannot record — this doubles as the archive-on-unexpected-exit path), re-verify the UUID, then `cmux close-surface`.

## Live dry-run (2026-07-08) — the `--close` review artifact

```
[reap-surfaces] DRY-RUN — 31 terminal surfaces in fleet-managed workspaces (use --all for every workspace). Closes NOTHING.

  TRACKED (live fleet member): 11
  LIVE AGENT (live pid / painted TUI): 2
  HUMAN SHELL (no fleet launch signature, or human-touched): 15
  HUSK CANDIDATE (reapable): 3

  husk candidates:
    - 72C89319  label=prior-art-intel  resume=b1cae473…  "…/_meta/agents/research-agent"
        archive-on-close would record: label=prior-art-intel, resume=b1cae473-af75-4049-87f3-ec894748c518
    - FA14FB03  label=recovery-safety  resume=30295b76…  "…/agents/ad-hoc/recovery-safety"
        archive-on-close would record: label=recovery-safety, resume=30295b76-381e-4c8f-a050-6ccfd05ee523
    - 541A7145  label=usage-ops  resume=2a94e2aa…  "…/_meta/agents/usage-ops"
        archive-on-close would record: label=usage-ops, resume=2a94e2aa-2c63-45e2-887f-a818842acbaa

  reapable in scope: 3
```

Both live-caught safety refinements are proven here: surface `0A3A252A` (a fleet launch line followed by a manual `cd`) and the live codex surface are BOTH out of the husk bucket.

## Empirical close + restore-record findings (2026-07-08) — the `--close` blocker

Ran the close test on husk `72C89319` (prior-art-intel; its resume id was recorded first). Result: **today's dedicated-workspace husks cannot be reaped with cmux's current CLI.**

1. `cmux close-surface --surface <full-uuid>` (global) → `Error: not_found: Surface not found`. The documented "explicit surface UUIDs resolve globally" does NOT hold for a session-restored husk surface; it needs a workspace context to resolve.
2. `cmux close-surface --surface <uuid> --workspace <ws>` → `Error: invalid_state: Cannot close the last surface`. cmux refuses to close a workspace's only surface. **All three husks today are the sole terminal surface in their own dedicated workspace** (prior-art-intel / recovery-safety / usage-ops), so every one hits this.
3. ~~There is **no cmux CLI verb to close a single workspace or a last surface**~~ — **wrong, corrected 2026-07-10.** `cmux close-workspace --workspace <id|ref|index>` exists and closes a specific workspace with its surfaces; it was already present on 0.64.17 when this was written. What remains true: `close-window` is whole-window (too broad), and `workspace-action` / `tab-action` offer only `close-others` / `close-above|below` / `close-left|right`, never "close THIS one". So the last-surface refusal in (2) is real, and `close-workspace` is the way around it.
4. The husk stays in cmux's session-restore record (`~/Library/Application Support/cmux/session-com.cmuxterm.app.json`) throughout — so on reboot it reopens. Whether a *successful* close clears the record is untestable via this path (close is refused).

Note the topology split: the original hand-swept husks (issue #6) lived in a **shared** workspace (cmux-advisor's) with other surfaces, so `close-surface` worked on them; **dedicated-workspace** husks (an agent's own workspace, which is what today's exited agents leave) are un-closeable.

**Consequences for `--close`:**
- It can reap SHARED-workspace husks via `close-surface` (with the surface's resolved workspace context, not a bare global UUID).
- It CAN reap dedicated-workspace husks, via `cmux close-workspace --workspace <ws>` (corrected 2026-07-10; the verb exists and `cmux_fleet/cli.py` already uses it to reap empty scaffold workspaces). The old text here claimed no such verb existed and pointed at an upstream ask; that was never true on 0.64.17. `--close` has not yet been wired to it — that is open work, not a cmux gap.
- Clearing the restore record is a second, still-open cmux gap. Whether `close-workspace` clears it is **unverified**; test against a disposable workspace before relying on it.

Upstream draft: [`docs/upstream-cmux-forget-surface.md`](upstream-cmux-forget-surface.md) (DRAFT, and partly obsolete for exactly this reason; the submit is Berg's button).
