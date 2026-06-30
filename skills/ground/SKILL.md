---
name: ground
description: Orient a cmux-fleet agent at session start. Run FIRST in any cmux fleet session (conductor or worker) to learn where you are, how to dispatch, and the working conventions. The optional floor CLAUDE.md can point new agents here.
---

# ground

## Where you are
A cmux-native agent in a cmux-fleet. cmux owns sessions, lifecycle, transcripts, and the event bus; the `cmux-fleet` plugin adds the orchestration layer on top: the `fleet` CLI (on PATH), the roster at `$CMUX_FLEET_TOML`, and state under `$CMUX_STATE_DIR`. See `scripts/config.py` for how those paths resolve.

## Recall before re-deriving
Before researching context, check what is already filed: past handovers (`./handover/`), the project's own docs, and any memory tool you have configured (for example `memsearch:memory-recall`, if installed). Do not re-derive what is already written down.

## Conductors: dispatch
To spawn, drive, or observe child agents, use the **`/cmux-fleet:cmux-fleet`** skill: launch, drive, completions arrive on their own, digest (`fleet launch` / `recycle` / `archive`). Do not re-derive dispatch.

## Conductors: handover
At session end, when context runs low, or before a relaunch, write a point-in-time handover with **`/cmux-fleet:cmux-handover`**. It is the file `fleet recycle` auto-primes the next instance to read.

## Surfacing to the user (conductors)
When the user needs to SEE something (a file, a diff, a decision), surface it into a view pane rather than pasting walls of text. The recipe lives in the `/cmux-fleet:cmux-fleet` skill's Layout section.

## Laconic mode (optional working style)
Answer in as few words as the work allows. No preamble, no restating the question. Lead with the number, the verdict, or the decision; add reasoning only if it changes what the user would do. State the result and the next step, then stop.

Brevity never overrides rigor. Numerical results stay quantitative with uncertainties; distinct things stay distinct; an honest "unknown" beats a tidy false claim. When correctness needs length, take the length, and not one line more. Formal artifacts (decisions, handovers, docs) follow their own structural conventions; laconic mode governs chat reasoning, not document format.
