---
name: cmux-handover
description: Write a point-in-time session handover for the next instance of this agent. Use at session end, when context runs low, or before a relaunch. Lean and cwd-local (an agent's own working memory, NOT a vault entity).
---

# cmux-handover

> **Defers to `loom:handover` when present.** If the `loom` plugin is loaded, its `loom:handover` skill is
> the authority for this convention (tapestry-fleet's vault-seated agents) — use that one instead. This
> skill is the standalone fallback for cmux-fleet used without loom (the productized-plugin path).

A handover is a **point-in-time brain dump of this session** for whoever picks up this agent next — a relaunch of you, or the conductor reading you cold. It is NOT a vault entity and NOT maintained; it is working memory in the agent's own space.

## Where it lives
`<your cwd>/handover/<YYYY-MM-DD>.md`. Your cwd is your home (your identity seat). A second handover the same day → add a `-HHMM` suffix. Create `handover/` if it is not there; it is yours.

This is exactly the file `fleet recycle --fresh` auto-primes the next instance to read (the newest `handover/*.md` by mtime; a bare `fleet recycle` now RESUMES and does not prime). There is **no** separate top-level `HANDOVER.md` — that was the retired relauncher's convention; `handover/<date>.md` is the single source.

## The split (why your cwd, not the vault)
Two kinds of output, two homes — this is the clarity the fleet is built around:
- **Durable, curated knowledge** (architecture, decisions, research) → your project's curated docs (`docs/`, `decisions/`, ...). The shared, lifecycled record.
- **Session-ephemeral working context** (what I was doing, what's half-done, what to try next) → **here, your `handover/`**. Yours. It keeps the curated docs clean and gives you room to think out loud.

The handover bridges sessions *in your space*. Don't push session churn into the curated docs; don't bury durable knowledge in a handover.

## Write it (a brain dump, not a form)
Hit what's relevant, skip what's empty, add what's yours. Write for a reader with **zero memory of this session**:

- **State of the world** — 1-2 lines: where things stand right now.
- **What this session did** — the main work, briefly. Link commits/PRs, don't re-narrate them.
- **Current priorities / next** — what the next instance picks up first.
- **Open / dropped threads** — anything deferred, half-done, or that fell off. **Scan the whole session for these, don't just recall** — a long session drops threads silently, and this is the one place they get caught. Honest loose ends beat a tidy summary.
- **WIP pointers** — files, branches, commits, surfaces in flight; where to look.
- **What went well / gotchas** — discoveries to reuse, traps to avoid.
- **Fleet snapshot** *(conductors)* — paste `fleet ls` at handover + notes on children (who's archived/revivable, who's mid-task, who to clean up). Live truth is always `fleet ls` / `fleet.json`; this is just the picture at handover time, so the next instance isn't flying blind on boot. (No separate persistent state file — the registry already IS the live state.)
- **Read on resume** — the 2-3 pointers (docs, memory, this file) to load first. For conductors, the boot ritual also includes **`fleet inbox`** (pending completions/alerts/peer-msgs that queued while you were down — the push path can't replay them across a recycle) + **`fleet ls --scope mine`** (know your fleet: you + your children).

## Optional final step: recycle yourself
The natural tail of a handover is a **`fleet recycle --fresh`** — restart yourself into a fresh session in the same surface, shedding bloated context, and let the next instance boot and read the handover you just wrote. At handover time you want **`--fresh`** (shedding is the whole point). **Only do this when you are actually ready to relaunch** (most handovers don't recycle — you write one and keep going, or hand back to a human). Never recycle with a draft you haven't finished.

> NOTE (2026-07-01): `fleet recycle` now DEFAULTS to **RESUME** (preserve context — the least-disruptive default). A handover recycle must pass **`--fresh`** to get a clean session; a bare `fleet recycle` would just continue the same session (no shed, no handover prime).

**ALWAYS `--dry-run` first, and READ the `session-prefs:` line before the real recycle.** The dry-run prints the fully-composed launch plus a line like `session-prefs: effort=max (role-pin), model=claude-opus-4-8[1m] (floor)`. Confirm the **model and effort are what you expect** — if they're not what you intend (wrong model, an effort you didn't mean, a `(source)` you don't recognize), STOP and fix the cause (usually the role's toml or the `[tool.claude]` floor) *before* recycling. This is the cheap guard against a silent came-back-on-the-wrong-model recycle (the 2026-07-04 cmux-advisor incident: a `--model`-less recycle inherited a stale global default). The dry-run is a pure preview — it spawns nothing.

```
fleet recycle --fresh --dry-run    # STEP 1 — preview; verify the `session-prefs:` model/effort line
fleet recycle --fresh              # STEP 2 — only after the dry-run looks right: self, FRESH session, same surface
fleet recycle --fresh -- --effort xhigh --add-plugin <name>   # a one-off launch-param change (also dry-run it first)
```

What it does (see the `cmux-fleet` SKILL → recycle, and `docs/operations.md`): a **detached** helper waits until you go quiet (idle + empty input draft; `--force` to override), then uses cmux's native `respawn-pane` to tear you down and relaunch a fresh session **in the same surface** — so your label and all parent/child routing stay valid (only the session id changes). The launch is recomposed from cmux's own resume binding (accurate even if the registry is sparse), then it auto-primes the fresh instance to read this handover. A bare `fleet recycle` (no `--fresh`) instead RESUMES the same session — not what you want at handover time. You can recycle a **child** the same way: have it write its handover first, then `fleet recycle <label> --fresh`.

Because the helper is detached and waits for idle, the clean pattern is: write the handover → call `fleet recycle --fresh` → **end your turn**. It fires once you're quiet.

## Principles
- **Cold-read first.** Assume the reader knows nothing of this session.
- **Point, don't paste.** Durable sources (docs, memory, commits) get linked, not duplicated.
- **Honest over tidy.** Name what's unfinished and what you're unsure of.
- **Lean.** Every block earns its tokens for the next reader. A handover is not a status report for a human.
