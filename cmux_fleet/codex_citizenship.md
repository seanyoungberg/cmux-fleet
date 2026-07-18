# You are an agent in a cmux fleet

This applies **only if `$AGENT_LABEL` is set**. If it is empty you are somebody's own codex session, not a
fleet worker — ignore this whole block and carry on.

## Your completion reports itself. If you are blocked, ask.

When your turn ends the fleet router delivers your completion to the agent that launched you
automatically — the same inbox flow every child rides; you do not send a "done" message yourself.

What still needs YOUR word is being BLOCKED: if you cannot finish without an answer, ask the agent that
launched you and then read your inbox for the reply. You do not need to know its name — the fleet resolves
your parent from the registry:

```sh
fleet peer-msg --to-parent 'blocked: <the question>' --expect-reply
```

Reporting a block is worth as much as finishing; being silently stuck is the only unrecoverable move.

## Single-quote the body. Backticks are shell substitution.

```sh
#  WRONG — your shell RUNS `codex_seat_home` and eats the word. The message sends anyway, with a hole
#  in it, and neither end is told. This is silent, on both sides.
fleet peer-msg cmux-advisor "fixed `codex_seat_home`"

#  RIGHT
fleet peer-msg cmux-advisor 'fixed codex_seat_home'
fleet peer-msg cmux-advisor "$(cat report.md)"     # a long body: quote the substitution
```

## Read your inbox

Messages addressed to you — replies, instructions, alerts — land in an inbox, not in your prompt:

```sh
fleet inbox                 # what is waiting for you, oldest first
fleet inbox-ack <seq>       # once you have handled one, so it stops resurfacing
```

## Who you are

| | |
|---|---|
| `$AGENT_LABEL` | your name in the fleet — this is how others address you |
| `$AGENT_ROLE` | the role you were launched as |
| `$CMUX_SURFACE_ID` | the cmux surface you are running on |

Who launched you is not an env var — the fleet derives it from the registry (`fleet peer-msg --to-parent`
reaches your conductor without you naming it).

## Commit your own work, explicitly

Nothing auto-commits for you. Commit only the paths you touched — you are not the only agent in this tree,
and `git add -A` will sweep up someone else's half-finished work:

```sh
git -C <repo> add <the exact paths you changed>    # never `git add -A`
git -C <repo> commit -m '<msg>'
```

Use `git -C <dir>`, never `cd <dir> && git`.

## Durable output goes in your agent home, not your worktree

Your worktree is scratch: it gets reaped when you do, and anything left in it dies with it. A report, notes,
a handover — anything meant to outlive you — is written under your agent home.
