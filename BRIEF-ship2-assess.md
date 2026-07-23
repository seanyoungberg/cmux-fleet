# BRIEF — Ship 2 (reconciliation) adoptability assessment (held, unattended-mandate night)

**From:** cmux-advisor · **To:** redesign-builder (you have the unique context: you built Ship 5 AND
doctor-reliability) · **2026-07-18 eve**

This is an ASSESSMENT, not a blind verify. Ship 2 is a **pre-Ship-5 branch** and may not cleanly apply.

## What Ship 2 is
`4e48f2f` on `dev/fleet-state-redesign`, dated **2026-07-15** (BEFORE Ship 5 / the thin-registry refactor /
v0.11.0): "restore reconciliation — close deterministic husks, flag resume-orphans." It addresses backlog #1
(post-restart reconcile gap: live-duplicate-pid, no detector). Touches: new `reconcile.py` (344), `router.py`
(+51), `cli.py` (+5), `config.py` (+9), `daemon.py` (+14), new `test_reconcile.py` (225).

## The question — is it still adoptable after Ship 5 + v0.11.0?
- **It predates the thin-registry v2 + the resolve/state/router refactors you did.** Does `reconcile.py` still
  make sense against the v2 machinery (identity+spec+binding, the `e_*` accessors, live_all/live_put)? Do its
  `router.py`/`cli.py`/`daemon.py` hooks still apply, or do they patch code that Ship 5 moved/rewrote?
- **Conflict surface with the two held branches:** it touches `router.py` (your doctor-reliability also does)
  and `cli.py` (the ergonomics branch also does). Does it compose, or collide?
- **Interaction with doctor-reliability:** both touch presence/liveness. Ship 2 detects live-duplicate-pids
  (one label, two live pids); your doctor-reliability changed how liveness verdicts derive. Do they agree, or
  does Ship 2's husk/orphan logic need to route through your new transcript-advance discriminator?

## How to assess
- Worktree off main (`git worktree add .worktrees/ship2-assess -b dev/ship2-assess main`); try to bring
  `4e48f2f` forward (cherry-pick / rebase onto v0.11.0). Report whether it applies clean, conflicts (where),
  or is partly superseded.
- If it applies: run its tests + the full suite; note green/red.
- Read `reconcile.py` against the current v2 code and give a verdict.

## Output — a verdict for Berg (HELD, do NOT adopt)
One of: **CLEAN** (rebases + tests green, ready to adopt) · **NEEDS-REWORK** (what specifically — which hooks
must rebase onto the v2 code / onto doctor-reliability) · **SUPERSEDED** (Ship 5 already covers part/all —
say which). Receipts (rebase result, test output, the specific conflict/supersession points). Report to me.
**Do NOT adopt or merge.** Model Opus xhigh. If your context is too heavy to hold Ship 5 + doctor-reliability
+ this at once, say so and I will re-scope — the interaction analysis is the valuable part, so keep that context if you can.
