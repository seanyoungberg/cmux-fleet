---
name: ground
description: Orient a cmux-fleet agent at session start. Run FIRST in any cmux fleet session (conductor or child) to learn where you are, how to dispatch, and the working conventions. The optional floor CLAUDE.md can point new agents here.
---

# ground

## Where you are
A cmux-native agent in a cmux-fleet. cmux owns sessions, lifecycle, transcripts, and the event bus; the `cmux-fleet` plugin adds the orchestration layer on top: the `fleet` CLI (on PATH), the roster at `$CMUX_FLEET_TOML`, and state under `$CMUX_STATE_DIR`. See `scripts/config.py` for how those paths resolve.

## Recall before re-deriving
Before researching context, check what is already filed: past handovers (`./handover/`), the project's own docs, and any memory tool you have configured (for example `memsearch:memory-recall`, if installed). Do not re-derive what is already written down.

## Take the tool update
If your tool interrupts you with an interactive "update now?" prompt, **take the update.** Do not dismiss it, do not work around it, and do not escalate it as a question. There is hardly ever a reason the user does not want Claude Code or codex current, and they have said so once, generally.

An update prompt is not a decision point; it is a modal that blocks a terminal. Treating it as a gate is how it becomes a wedge: a codex seat can boot straight into its update menu, sit there unbound forever, and `fleet launch` still reports success — the fleet believes it launched an agent and there is no agent.

Prefer the non-interactive path, so the prompt never appears:
- **codex** — run `codex update`. There is no setting that suppresses the prompt (checked `codex --help`, `codex update --help`, `codex features list`, and `~/.codex/config.toml`, 2026-07-10), so the subcommand is the whole of it.
- **Claude Code** — updates itself; nothing to do.

## Conductors: dispatch
To spawn, drive, or observe child agents, use the **`/cmux-fleet:cmux-fleet`** skill: launch, drive, completions arrive on their own, digest (`fleet launch` / `recycle` / `archive`). Do not re-derive dispatch.

## The `--scope` model (your fleet by default)
Every scope-aware verb takes **`--scope mine|all|conductors|children`** — one vocabulary, everywhere. The mental model: **your fleet by default.**
- **Reads default to `mine`** — `fleet ls`, `fleet vitals`, `fleet inbox`, `fleet graph` show *you + your direct children* (for inbox, your own inbox). Add **`--scope all`** for the whole fleet; `conductors`/`children` filter by kind. When `mine` is just you, a one-line hint points at `--scope all` so you don't mistake your corner for an empty fleet.
- **Acts require an explicit scope** — `fleet broadcast` **errors** without `--scope`; `fleet recycle` bare = just you, `fleet recycle --scope mine` = your children (gated bulk). No silent fan-out — you always say who.
- A bare **`<label>`** works where single-target makes sense: `fleet inbox --scope <label>` peeks that agent's inbox, `fleet graph --scope <label>` roots its subtree.
- **`mine` treats *you* asymmetrically, on purpose: a READ includes you (you + your children); an ACT excludes you (your children only).** Same word, deliberate safety asymmetry — a read shows you your own context, an act never fans out onto you. Self is always the bare form: bare `fleet recycle` = just you.

## Conductors: catch up on boot (and after a recycle)
Completions/peer-msgs/alerts arrive on their own **while you're live** — but a fresh instance (you, just now, especially after `fleet recycle`) never saw the wakes that queued while it was down. So at session start, pull the state the push path can't replay: run **`fleet inbox`** — your pending inbox (child completions + auto-archive/health alerts + peer messages, oldest first). Ack what you handle with `fleet inbox-ack`. This is the catch-up read; don't hand-read the state file. Then **`fleet ls`** / **`fleet vitals`** (both default `--scope mine`) for your live fleet picture.

## Conductors: handover
At session end, when context runs low, or before a relaunch, write a point-in-time handover with **`/cmux-fleet:cmux-handover`**. It is the file `fleet recycle --fresh` auto-primes the next instance to read (a bare `fleet recycle` now RESUMES — it does not prime).

## Surfacing to the user (conductors)
When the user needs to SEE something (a file, a diff, a decision), surface it into a view pane rather than pasting walls of text. The recipe lives in the `/cmux-fleet:cmux-fleet` skill's Layout section.

## Laconic mode (optional working style)
Answer in as few words as the work allows. No preamble, no restating the question. Lead with the number, the verdict, or the decision; add reasoning only if it changes what the user would do. State the result and the next step, then stop.

Brevity never overrides rigor. Numerical results stay quantitative with uncertainties; distinct things stay distinct; an honest "unknown" beats a tidy false claim. When correctness needs length, take the length, and not one line more. Formal artifacts (decisions, handovers, docs) follow their own structural conventions; laconic mode governs chat reasoning, not document format.
