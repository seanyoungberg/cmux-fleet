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

## cmux restore-record interaction (fix #2)

_Empirical findings recorded below once the close test runs._
