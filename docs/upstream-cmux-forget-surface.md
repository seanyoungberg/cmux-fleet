# Upstream cmux ask (DRAFT) — reap an orphaned single-surface workspace + forget it from restore

> Status: **DRAFT for Berg's review.** Not filed. The upstream submit is Berg's button (per the upstream-contribution-playbook gate). This is the issue body + proposed change, prepped for `gh issue create`.

## Environment
- cmux 0.64.17 (97) [9ed29d81a]
- macOS 26.5.2 (arm64)

## Summary
There is no way, from the CLI, to close a surface that is the **only** surface in its workspace, nor to close a workspace, nor to remove an orphaned surface from cmux's session-restore record. This blocks automated cleanup of the inert login shells that session-restore replays on reboot.

## Background / motivation
On reboot, cmux's session-restore reopens stale surfaces and replays their captured launch command as a bare login shell. When an agent workspace was a single-surface workspace, that surface is now an inert husk (a dead `zsh -il` with a replayed command, no live process). These accumulate one-per-reboot and there is no clean CLI path to reap them.

## Reproduction
A workspace whose only surface is such a husk (e.g. an agent workspace after the agent process exited):

```
# 1) global UUID does not resolve a restored surface:
$ cmux close-surface --surface 72C89319-2D50-4A6E-BD8C-0FAD80F69F6F
Error: not_found: Surface not found            # exit 1  (yet `cmux tree --all` lists it)

# 2) with a workspace context it resolves, but refuses the last surface:
$ cmux close-surface --surface 72C89319-… --workspace workspace:42
Error: invalid_state: Cannot close the last surface   # exit 1

# 3) no verb closes the workspace or the last surface:
#    close-window   -> whole window (too broad; the window holds every workspace)
#    workspace-action close-others / close-above / close-below  -> never "close THIS one"
#    tab-action     close-left / close-right / close-others     -> same
```

The surface also remains in `~/Library/Application Support/cmux/session-com.cmuxterm.app.json`, so it reopens on the next reboot.

## Expected
A way to (a) close a surface even when it is the last in its workspace (tearing down the now-empty pane/workspace), resolvable by **global surface UUID**, and (b) drop that surface/workspace from the session-restore record so it does not reopen.

## Actual
Neither is possible; the husk is un-reapable and resurrects every reboot.

## Proposed change (one of)
1. **`cmux close-workspace --workspace <id|ref|uuid>`** — close a specific workspace (and its surfaces), resolvable globally; drops it from the restore record. Symmetric with `close-window`.
2. **or `cmux close-surface --force`** — allow closing the last surface in a workspace, tearing down the emptied pane/workspace, and drop it from restore; fix global-UUID resolution so `--surface <full-uuid>` resolves without a `--workspace` context (the docs already say "explicit surface UUIDs resolve globally").
3. **or `cmux forget-surface --surface <uuid>`** — a restore-record-only verb: remove a surface from the saved session so it will not be re-restored, independent of whether it is currently open.

Any one unblocks automated husk cleanup. (1) or (2) is preferred — (3) alone leaves the live husk on screen until the next restart.

## Verification the fleet would add downstream
`fleet reap-surfaces --close` gates every close on a multi-signal husk fingerprint (fleet env prefix + tail guard + no live agent), archives the resume pointer first, and re-verifies the UUID immediately before the close — so the new cmux verb would only ever be handed a confirmed orphan.
