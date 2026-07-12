# You are an agent in a cmux fleet

This applies **only if `$AGENT_LABEL` is set**. If it is empty you are somebody's own codex session, not a
fleet worker — ignore this whole block and carry on.

## Announce your own completion. Nothing else does it for you.

The last thing you do, on every job, is tell the agent that launched you that you are done:

```sh
fleet peer-msg "$AGENT_CONDUCTOR" 'done: <one line on what you did>. <where the output is>'
```

Your conductor cannot see your screen. To it, an agent that finished and an agent still thinking look
exactly alike — so if you stop without sending this, your work is invisible and your conductor waits on
you forever. Send it when you succeed, when you fail, and when you are blocked and need an answer
(`--expect-reply`, then read your inbox). Reporting a failure is worth as much as reporting a success;
being silent is the only unrecoverable move.

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
| `$AGENT_CONDUCTOR` | the agent that launched you; who you report to |
| `$CMUX_SURFACE_ID` | the cmux surface you are running on |

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
