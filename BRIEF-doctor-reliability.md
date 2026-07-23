# BRIEF — doctor-reliability: token-flow liveness (held dev, unattended-mandate night)

**From:** cmux-advisor · **To:** redesign-builder · **2026-07-18 eve**

Excellent work on the ergonomics batch (verified TIGHT, 1141 green, held for Berg). Next highest-value open
rough edge — the one the container backlog flagged "RIPE FOR EXTRACTION" with 4 specimens.

## ⛔ HARD RULES (same as last batch)
- **Isolated worktree off main** (`git worktree add .worktrees/doctor-reliability -b dev/doctor-reliability main`).
  Do NOT touch the main checkout. **Develop + TEST green, then STOP. Do NOT merge/adopt.** Held for Berg.
- If your context is heavy after the ergonomics batch, recycle `--fresh --force` first (the manual --force —
  your self-recycle guard is on the unadopted branch, so the installed `fleet` still needs it), re-prime, then
  pick this up from this brief. Your call.
- Step-0: read the doctor/heartbeat verdict paths before editing (`router.py` ~350/407 the STUCK/DOWN gate;
  `resolve.py` the I4 attachment heuristic + liveness; wherever `detached` / `stuck` / `stale`(surface-gone)
  verdicts are derived).

## The problem (cmux/_inbox/backlog.md 2026-07-16, 4 specimens)
Clock-based liveness verdicts **false-positive on long-idle OR long-turn agents, of ANY kind**:
- detached: a freshly-moved agent reads `state=detached` (I4 firing on just-moved data) though status=live.
- stuck: the heartbeat flagged cmux-advisor STUCK (detached) during a healthy 10-min single tool-running turn
  (pane streaming tokens, Berg driving live) — a long turn emits no hook events, so record-age alone reads dead.
  (3 specimens: berg-sandbox, cf-conductor, all inspected-live-touched-nothing.)
- stale-gone: `fleet ls` flagged redesign-builder (a WORKER) STALE/surface-gone during a 44-min idle;
  capture-pane confirmed it live+idle at an empty prompt (73% ctx).
**Root cause:** wall-clock elapsed ≠ liveness. A live slow turn and a long idle look identical to a dead
stream *by clock alone*. The discriminator is **token-flow / transcript-advance**, not elapsed time.

## The fix
Replace (or gate) the wall-clock basis of the liveness verdicts with a **transcript-advance / token-flow**
discriminator: is the agent's transcript or hook record actually ADVANCING (tokens flowing / new events /
session file growing) within the window? If yes → live, regardless of elapsed idle/turn time. Fold in a
grace-period for a turn past N minutes as a fallback. **Must cover all THREE verdicts (detached, stuck,
stale-gone) AND workers**, not just conductor-detached — the 4th specimen proved it is not conductor-only.

## ⚠️ This is ALARM logic — the doctrine applies (do not get this wrong)
- **PRESERVE the safety invariant:** a live pid is NEVER told DOWN; the STUCK-live message stays inspect-framed
  with NO destructive remedy (router.py `'STUCK' if live else 'DOWN'`). You are killing NOISE, not adding teeth.
- **Reachable-green AND still-discriminating:** the new check must NOT alarm a healthy long-turn/long-idle
  agent (the whole point) — but it must STILL catch a genuinely dead/wedged one. Do not flip the failure mode
  from "false alarm" to "misses a real death." Test BOTH directions: (a) a healthy agent mid-long-turn / long
  idle whose transcript is advancing → NOT flagged; (b) a genuinely dead/stalled agent whose transcript is
  frozen → still flagged.
- Never verify the discriminator with its own instrument; drive it from real (or realistic fixture) transcript
  state, not from the verdict it produces.

## Acceptance (receipts required)
- Full suite green (expect 1141 + your new tests) — paste the summary line.
- Tests for both directions of each affected verdict (healthy-advancing → clear; frozen → flagged).
- Report back to me (completion rides to parent). **Do NOT adopt.** Model stays Opus xhigh.
- If the fix reveals the verdict basis is deeper than a tuning change (an architectural shift), STOP and flag
  it to me as a fork rather than committing to it — this is held, unattended, no new architectural commitments.
