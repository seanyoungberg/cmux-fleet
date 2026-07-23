# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.15.6] - 2026-07-22

**self-recycle ABORT reaches the seat that ran it.** v0.15.5 closed "a detached recycle's terminal result is
silent to the caller" everywhere except the fleet's single most common recycle shape — a conductor recycling
itself. Reported independently by both peer conductors (berg-sandbox, cf-conductor) within minutes, each with
a pending self-recycle. Suite green: 1289 tests.

### Fixed

- **A self-recycle that ABORTs now notifies its own seat.** `_recycle_notify_caller` skipped the invoker
  unconditionally whenever it was the recycled surface, justified as "its inbox is being respawned away" —
  true only for DONE. On a non-DONE outcome the recycle explicitly does *not* respawn (no half-kill), so the
  original process is still alive with an intact inbox, and it is precisely the party that needs to know its
  own recycle failed; it saw `SCHEDULED` and never learned otherwise. The skip is now conditioned on the
  outcome: self + DONE stays quiet (the respawned instance boots fresh off its handover — a completion note
  there is noise), self + any other terminal outcome is delivered. Non-self DONE/ABORT and the no-invoker
  operator case are unchanged. Delivery still runs through `state.wake_if_idle`, whose busy-check and
  screen-read prompt gate mean a seat that aborted *because* it was genuinely mid-turn gets a durable inbox
  row and no injection.

## [0.15.5] - 2026-07-22

**recycle quiet-gate pair** (cmux-advisor batch; berg-sandbox bug report). Both defects from the graph-view
incident — a lavish long-poll held the gate 180s to an ABORT while the agent was done-idle at its prompt,
and the calling conductor only ever saw `SCHEDULED`. Sharp during an account migration, where every conductor
recycling its subtree is exposed to the silent ABORT. Suite green: 1288 tests.

### Fixed

- **Recycle quiet-gate no longer blocks on a background shell.** A lavish long-poll (standard on review-driving
  agents) keeps the session PID alive for hours, so `_quiet_gate`'s live-pid check could never clear on a seat
  that was done-idle at its prompt — the gate burned its full 180s to an ABORT. The gate now also consults the
  transcript's turn-close signal (`features.turn_ended`, the same classifier signal `fleet ls`/vitals/`rm`
  read): a `running` seat whose last turn CLOSED is quiet even while cmux's lifecycle lags and a poll runs (a
  child process dies with the respawn — it is not a live TURN to interrupt, Berg's ruling). Fails CLOSED, so a
  genuinely mid-turn seat (turn not closed) still blocks, unchanged.
- **A detached recycle's terminal result now reaches the caller.** The recycle runs detached and only logged, so
  the calling conductor saw `SCHEDULED` and never learned DONE or ABORT (the quiet-gate ABORT was fully silent).
  The invoker is now recorded at schedule time (`_recycle_plan` reads `$CMUX_SURFACE_ID`) and every terminal
  result — DONE and every ABORT — is delivered to that surface completion-style (a `peer` inbox row + idle-wake,
  via `_recycle_notify_caller`, funneled through one `_finish` closure over all five terminal returns). It
  targets the ACTUAL caller (which need not be the seat's registry parent, whom `_escalate_recycle_failure`
  still alerts separately); skips an operator-driven CLI (no `$CMUX_SURFACE_ID`) and a self-recycle.

## [0.15.4] - 2026-07-22

**securestorage seeded-guard**: a claude securestorage namespace that was never logged in has no keychain
item, so claude SILENTLY falls back to the ambient credential — a pinned seat then runs and BILLS on the
wrong account with no error. `providers.securestorage_seeded()` detects it by keychain item EXISTENCE
(`security find-generic-password -s <service>` without `-w`: exit 0 = seeded, 44 = never seeded; no secret
decrypt, no prompt), and every spawn path (launch, recycle, revive) now emits a loud WARN naming both the
hazard and the fix. WARN, not ABORT, deliberately: seeding a namespace requires launching a pane on it and
running `/login` inside, which an ABORT would block. Fails OPEN where `security` is absent so it never cries
wolf on a real account. `securestorage:` is also now documented in fleet.toml.example.

**usage footer disambiguation**: same identity on two tools rendered as duplicate rows (one `sean@berglabs.net`
line for claude and another for codex). Field 0 of the usage carrier is now tool-prefixed
(`claude · sean@berglabs.net`), tool first so a truncation keeps the disambiguator.

## [0.15.3] - 2026-07-22

**role account-pin**: a DURABLE per-seat account. `[role.<r>.<tool>].account = "<name>"` layers ABOVE the
tool default and resolves through the ONE chokepoint every spawn path shares, so a seat stays on its account
across recycle/revive/launch instead of silently reverting to the default on the next respawn (the incident:
a seat drifting off `berg-max`). Plus two spawn-path account-correctness follow-ons: a claude securestorage
seeded-guard and a usage-footer display fix. Suite green: 1278 tests.

### Added

- **`[role.<role>.<tool>].account` pin (review #7 seam).** A role can now name the account its seat launches
  under, durably. Precedence, proven end-to-end: **`--provider` flag (one-off) > role `account` pin >
  `[providers.<tool>].default`**. Only `account` is read — `provider` is deliberately NOT accepted (one name,
  one meaning; the registry's `provider` field records the RESOLVED choice, not the pin). `providers.role_account`
  reads it through the same `_load_fleet_toml` the `[providers]` table uses, so an unreadable toml ABORTS
  loudly (unknown-is-not-absence) rather than silently reading as "no pin" and reverting the seat.
- **claude securestorage seeded-guard (silent-wrong-account).** An UNSEEDED `securestorage:<dir>` namespace
  has no keychain item, so claude silently falls back to the ambient credential — a spawn that BILLS THE
  WRONG ACCOUNT under a pin (proven 2026-07-22: pinned `berg-max` windows read identical to `berglabs` until
  the namespace was `/login`-seeded). `providers.securestorage_seeded` probes keychain-item existence
  (`security find-generic-password`, no secret decrypt, no prompt) — the sound signal an attrs-only probe
  missed. Wired as a loud WARN on launch/recycle/revive (never silent). WARN, not ABORT: seeding *requires*
  launching a pane on the namespace and running `/login` inside it, so an abort would block the bootstrap.
  Fail-OPEN where `security` is absent (Linux/headless), so it never cries wolf on a real account.

### Changed

- **One chokepoint for account resolution across launch, recycle, and revive.** `_resolve_account_name`
  (which recycle/revive already resolved through) now parses the pin, and `cmd_launch` was pointed at it —
  so "launch-minus-flag == the shared function" is finally TRUE (the recycle-path docstring promised it; the
  launch path had its own duplicated `default_provider` call). A recycle/revive re-resolves the pin exactly
  as it re-resolves the loadout: a recorded account ≠ the pin now moves to the pin, with the existing loud
  `account MOVED` warn; the healthy no-pin case is byte-identical to before.
- **Failure semantics unchanged, now covering the pin.** A NAMED account that fails to resolve — including a
  pin naming an account with no `[providers.<tool>]` block — ABORTS loudly on every path, never a silent
  ambient fallback. The launch abort message is now source-neutral (`[fleet] ABORT:`) since flag, pin, and
  default all resolve through the one path.
- **`fleet config` account honesty.** The effective-config dump now prints the account a launch WILL resolve
  for the role and WHERE it came from (`source: role pin …` / `default …` / `none — ambient`), flagging a
  pinned account that resolves to no provider entry (which would ABORT at spawn) so it's seen in config, not
  discovered at launch.
- **Usage footer distinguishes same-email accounts.** The sidebar subscriptions footer (`features._usage_lines`)
  renders one row per subscription keyed on its identity email; two accounts on the same email but different
  tools (`claude:berglabs-max` / `codex:berglabs`, both `sean@berglabs.net`) rendered as an apparent duplicate
  row (fleet.swift draws field 0 only). Field 0 is now tool-prefixed (`claude · sean@…` / `codex · sean@…`),
  tool first so a truncation keeps the disambiguator; append-only-safe (free-text field, no index shift).

## [0.15.2] - 2026-07-21

**status-truth**: agent status now derives from STRUCTURE, never prose (Berg mandate 2026-07-20). Six fixes
+ a stale-ref sweep. Suite green: 1256 tests. MERGE != LIVE.

### Changed

- **F1 — killed prose status classification.** Deleted the `ERROR_HINTS` / `REVIEW_HINTS` / `DONE_HINTS`
  keyword tables (and the dead `BLOCK_HINTS`) and the `_refine` keyword path. Matching an agent's own words
  stamped a state on any agent that merely MENTIONED the phrase — "rate limit" / "usage limit" / "compact"
  read as `error`, "opened pull request" as `review`. The classifier is now structure-only: cmux lifecycle
  + open Feed gate + the transcript's end-of-turn + the last row's structured halt fields. The prose-derived
  `error` / `review` / `done` states are retired.
- **F3 — unified `fleet ls` onto the shared classifier.** `fleet ls` classified from raw lifecycle with no
  transcript read, so a seat frozen at `running` showed `working` for 18h while `fleet vitals` said idle. Its
  state column now routes through the same `features.seat_state` path vitals and paint use (reads the
  transcript end-of-turn + structured halt + the I4 detached overlay), so the two views can never disagree.

### Added

- **F2 — hooks-first structured API-error states (the centerpiece).** Claude Code's **`StopFailure`** hook
  fires when a turn ends on an API error (Stop never does), typed `error_type` + `error_message`. Two new
  fleet hook shims (thin, fail-open, like the existing awareness/drain) record status from STRUCTURE at the
  source — never a prose scan:
    - **`StopFailure`** → `fleet hook-stopfailure`: `error_type=rate_limit` (or a 429) records a
      **`limit-parked`** halt — a transient server throttle OR an account park, structurally indistinguishable
      and self-healing either way (warm amber, never red, never named error; the `resets HH:MM` is extracted
      from `error_message` into the row detail when the banner carries one); any other typed error records
      **`errored`** (the type in the detail); an UNKNOWN `error_type` is still recorded (never dropped) and
      logged loudly to `hook-anomalies.log`.
    - **`Notification`** → `fleet hook-notification` (machine-typed): a `agent_completed` / `idle_prompt` /
      `auth_success` type clears the recorded park (ready corroboration); `needs-input` / `permission` types
      are left to the authoritative Feed gate; the prose `body` is display-only, never a state input.
  The halt rides a per-surface fleet store (`state.halt_*`), session-tagged so a recycled surface can't
  inherit a stale park, and cleared on any forward progress (new prompt, clean Stop, completed/idle). The
  transcript-structured read (`state.last_halt`, parsing `isApiErrorMessage` / `error` / `apiErrorStatus` /
  `stop_reason`) stays as the **catch-up layer** that reconstructs the same halt after hook/daemon downtime.
  `STATE_STYLE`, the vitals/ls render, and `sidebars/fleet.swift` (`stateOf`/`stateColor`/`stateIcon`) carry
  the new `limit-parked` / `errored` vocabulary. *DOCS-SILENT caveat (verify on first live fire): whether
  `StopFailure` fires on a USAGE-limit exhaustion vs an API 429 is unconfirmed upstream; the specimen's
  transcript classified the session-limit halt as `error: rate_limit`, so it is expected to.*
- **F6 — drive-child delivery guard.** A long `cmux send` can drop the MIDDLE of a prompt while head+tail
  survive, so `_submit`'s tail-only settle check passes blind (this corrupted a real dispatch). After submit,
  `fleet drive-child` now reads the child's last USER row back from its transcript (new `state.last_user_text`)
  and compares it to what was sent; a mismatch emits a loud `[drive] DELIVERY-TRUNCATED` with a resend / use-
  the-inbox-path hint and a non-zero exit. Fail-safe: an unreadable row is `UNVERIFIED`, never a false alarm.

### Fixed

- **F4 — launch group-ensure no longer mints a child anchor or a duplicate group.** `create_surface`'s
  workspace bootstrap titled a `Conductor - <label>` scaffold anchor + minted a cmux group with no kind guard,
  so a worker placed `--place workspace` with an unresolved group minted `Conductor - <worker>` furniture +
  a duplicate group. The bootstrap is now conductor-only (a non-conductor lands standalone), and it reuses an
  existing conductor group by name-to-ref across the `Conductor - <name>` / bare-`<label>` convention split
  instead of duplicating it.
- **F5 — doctor DETACHED row now inspect-first + a cutover-window runbook caveat.** The per-agent hook-store
  record is written by cmux, not the fleet; during an app-swap + daemon-restart cutover cmux can transiently
  freeze that write while its bus keeps emitting Stops, so behavioral-detach paints a healthy agent DETACHED
  for a few minutes until its next turn's hooks self-heal it. The doctor's child `detached` line (previously a
  generic "needs attention" that dropped the evidence) now leads with INSPECT, names the transient cutover
  freeze + self-heal, and carries the runnable capture-pane + recycle remedy. `docs/operations.md`'s cutover
  runbook gains a matching "do not recycle on a transient DETACHED" caveat.
- **Swept stale `fleet-proto` sidebar references** (`daemon.py`, `tests/test_daemon.py`) — the sidebar is
  `fleet.swift` now (the `fleet-proto` → `fleet` rename landed in 85bc620).

## [0.15.1] - 2026-07-21

Two small doctor-reliability / diagnostics fixes logged during the T6 arc. Patch on top of v0.15.0; no
behavior change beyond the two verdicts below. Suite green: 1237 tests.

### Fixed

- **DETACHED false-alert on crash-restored / long-turn agents (doctor-reliability).** The `env` reason in
  `resolve.attachment()` — a `CMUX_WORKSPACE_ID` env that disagrees with the tree workspace — fired on
  `record_age` alone. A crash-restored agent carries a stale env (a live process's env can't be rewritten)
  and, mid long-turn, freezes its record at the `running` stamp while its transcript keeps advancing, so
  env-detach condemned a demonstrably healthy seat (graph-view, flagged DETACHED 3x/day while Berg-driven
  and completing turns). The env reason now reuses the SAME transcript-advance tooth the behavioral/stall
  gates use: it fires only when the record AND the transcript are both frozen past `ATTACH_SKEW_S` — an
  agent still advancing either clock is not actionably detached. The behavioral reason and the genuinely-
  dark discriminator (env mismatch + both clocks quiet) are unchanged.
- **`fleet config` claimed CLAUDE.md loads regardless of setting_sources.** The "CLAUDE.md applied" line
  listed the cwd / vault-root / user `CLAUDE.md` paths by existence alone, blind to the resolved
  `setting_sources` gate. A role that narrows `setting_sources` (e.g. `user,local`, dropping `project`)
  does not load the project/vault-root floor, yet `fleet config` still reported it as applied. The line is
  now setting-sources-aware: the project-scope paths are claimed only when `project` is in the effective
  sources (an empty `setting_sources` is claude's native default — all layers on), the user memory only
  when `user` is; a present-but-suppressed project floor is surfaced with the reason rather than silently
  dropped.

## [0.15.0] - 2026-07-20

F1: the configurable floor FILE. Fleet now **places** a user-defined floor file into an agent's cwd (or
home) at launch — declaratively, per-tool — so a vendored install with no vault-root `CLAUDE.md` still
delivers a floor, and the backup floor is fleet-managed rather than hand-placed. This unifies the two
placement mechanisms that already existed unreconciled: the ad-hoc `CLAUDE.md` symlink and codex
citizenship's fenced `AGENTS.md` write. It only *places* the file; loading rides the live `setting_sources`
project walk (claude) / `$CODEX_HOME` (codex), adding no new load path and not colliding with the turn-one
boot prompt (which stays the primary floor). Rebased onto v0.14.0 (mint); combined suite green: 1234 tests.

### Added

- **`[tool.<t>.floor_file]` — a declarative floor file placed at launch.** Keys: `source` (a path to a
  file OR literal inline content, dual-meaning like `boot_prompt`; a bare string is shorthand for
  `source`), `mode`, `target` (`cwd` | `home`), and `filename`. Composes per-tool with a per-role override
  in `[role.<name>.<t>.floor_file]` via the existing resolve merge (whole-table replace). The two runtimes
  read different filenames, so the default is per-tool: claude → `CLAUDE.md`, codex → `AGENTS.md`.
- **Four placement modes, clobber-safe and idempotent.** `append` (default) writes a fenced,
  marker-guarded block that never clobbers the user's own text and never duplicates on re-launch — with
  markers distinct from codex citizenship's, so both blocks coexist in one `AGENTS.md`. `write` places only
  into an empty slot (skip + warn if a file exists). `overwrite` replaces the whole file (the only
  clobbering mode). `symlink` writes a relative symlink, skipped if a real file exists (the legacy adhoc
  floor behavior, generalized). Placement is **fail-open**: a floor that can't be placed warns and the
  launch proceeds.
- **Per-tool `target` default.** claude → the cwd (loaded via the `setting_sources` project walk); codex →
  `$CODEX_HOME` (the one file codex reads from every cwd). Overridable with `target = "home" | "cwd"`.

### Changed

- **`[fleet].floor_claudemd` (env `CMUX_FLEET_FLOOR`) is now the legacy fallback.** The ad-hoc `CLAUDE.md`
  symlink it configures still works, but only fires when no `[tool.<t>.floor_file]` is configured —
  superseded by the first-class, per-tool floor file. Non-breaking: existing setups are unchanged.

## [0.14.0] - 2026-07-20

`fleet mint` — define a new role from the CLI. Closes the
can't-spawn-a-new-top-level-conductor gap: a new role (a top-level conductor especially) no longer needs a
hand-edit of `fleet.toml`. Composes with the v0.13.0 boot contract — a minted agent gets the turn-one boot
prompt and `--brief` for free via the shared launch path. Suite green: 1210 tests.

### Added

- **`fleet mint <name>` — define a role (home + roster block), optionally launch it.** DEFINE (the
  default, idempotent) creates the role's home directory, seeds a thin identity stub, and registers the
  role by appending a `[role.<name>]` block to `fleet.toml`. `--kind conductor` mints a top-level
  conductor (own workspace group `Conductor - <name>`, home under `_meta/agents/conductors/`, launches
  `--parent none --place workspace`); the default `child` gets a home under `_meta/agents/` and joins the
  dispatcher's group. `--cwd` / `--group` override the conventions; `--dry-run` previews the block and
  launch argv without writing.
- **`--launch` opt-in.** DEFINE is config; `--launch` also spawns, handing off to the existing `fleet
  launch` path unchanged — so a minted agent gets the v0.13.0 turn-one boot prompt for free and the
  workspace group-join uses launch's own robust machinery (mint never name-keys a group). A minted
  conductor's `kind` resolves for `/loom:prime` because the same `fleet.toml` it lands in is the roster
  the prime kind-resolver reads.
- **Append-only roster write.** The `[role.<name>]` block is *appended* as text; no existing byte is ever
  rewritten, so hand-authored roles and comments survive by construction. `mint` only ever *creates* a
  role (edit/remove stays a hand-edit) and refuses a name that already exists (a `tomllib` read-first
  check). Absent roster → refuses with the seed hint rather than minting a preamble-less phantom file.
- **Thin identity stub, F1-configurable.** The seeded `CLAUDE.md` is a strict pointer to `/loom:prime`
  (never boot content — role knowledge lives in the governed boot pages, per the role-owned-agent-homes
  ruling); it never clobbers an existing `CLAUDE.md`. Wording is configurable via
  `[fleet].mint_identity_template` (env `CMUX_FLEET_MINT_IDENTITY_TEMPLATE`), the same cwd-floor-file seam,
  with `{name}`/`{kind}` substituted.

## [0.13.0] - 2026-07-20

The T6 boot contract: `fleet launch` now owns turn one. A fleet-launched agent primes itself and can be
dispatched in one command — closing the silent-delivery hole where a launched agent loaded no home floor at
all under `setting_sources = user,local` (only auto-memory `MEMORY.md` survived), so every floor file was
dead-on-arrival. Suite green: 1184 tests.

### Added

- **Launch sends a machine-composed boot prompt as turn one.** After the surface binds, `fleet launch`
  composes and sends the agent's first turn itself — an identity line, **run `/loom:prime`** (the agent
  primes: kind from the live roster, boot pages, latest handover), report-ready, then drain `fleet inbox`
  for its brief. The dispatcher never hand-types a prime prompt. Converged onto ONE source shared with
  `recycle --fresh` / `revive --fresh` (`_boot_prime_prompt`), so there is exactly one boot-path template,
  never two to drift.
- **`fleet launch <role> --brief "<task>"` — one-command dispatch.** The brief is queued to the child's
  INBOX at launch (input-safe, label-addressed, new inbox `kind="brief"`) and surfaces via idle-wake the
  moment the child goes idle AFTER priming — turn one is the boot prompt, prime runs, *then* the brief. An
  unprimed agent can never receive a raw brief: the brief is queued only after the boot prompt is
  submitted, so nothing wakes the child for it until its first post-prime idle, and the router self-wakes
  the child on a pending brief. `--brief` cannot combine with `--no-prime`.
- **User-configurable boot prompt: `[fleet].boot_prompt` (env `CMUX_FLEET_BOOT_PROMPT`).** The composer
  reads its wording from config at compose time (a toml edit goes live with no reinstall), defaulting to
  the co-signed frozen prime-architect template; the value is a literal string OR a path to a template
  file. `{AGENT_ROLE}`/`{AGENT_LABEL}` are substituted by the launcher; `--prime "<text>"` overrides per
  launch, `--no-prime` opts out. One config value serves both launch and recycle.

### Fixed

- **`recycle --fresh` / `revive --fresh` now actually invoke `/loom:prime`.** They previously sent an
  inline "re-orient from your latest handover" prompt that never told the agent to prime — the same
  dead-on-arrival floor bug as launch, latent on the recycle/revive path. Converging them onto the shared
  boot prompt gives every fresh boot a real prime.

## [0.12.0] - 2026-07-19

Adopts three branches cut off v0.11.0 and independently verified before the merge: fleet ergonomics, the
doctor-reliability liveness rework, and the Ship 2 restore-reconciliation brought forward onto the v2
registry. No single branch held all three, so the union was verified as a clean superset per file and the
COMBINED tree run green — 1172 tests, 0 failures.

### Added

- **`fleet reparent <label> <parent|none>` — a surgical in-place reparent.** `move` forced a workspace move +
  reparent-under-caller and `register` rebuilt the whole spec; there was no way to change *only* an agent's
  registry `parent`. `reparent` sets it alone (to another label, or `none` → top-level), flocked through
  `live_update`, every other field preserved, and cross-conductor guarded so you cannot reparent another
  conductor's child out from under it.
- **`fleet reconcile-restore [--close]` + an automatic relaunch-burst reconcile.** After cmux replays its
  crash-restore snapshot the registry can hold husks (dead fleet-origin surfaces) and resume-orphans (a live
  agent on a surface nobody owns). The verb surveys both; `--close` archives-first then closes only the
  DETERMINISTIC husks (snapshot agent=nil + no live agent + not registered + fleet-origin), never a live agent
  or a human shell. The router now also detects the `surface.created` burst a restore emits and fires one
  debounced background reconcile on its own — the heal no longer waits for the next daemon start. LIVE +
  reconcile-knob gated. (Ship 2, reworked onto v2: `surface_has_live_agent` moved state→resolve.)

### Fixed

- **A self-recycle no longer deadlocks to a 180s ABORT.** `fleet recycle --fresh` (non-forced) targeting your
  OWN surface could never clear the quiet-gate — the caller *is* the running activity the gate waits to see
  drain — so it burned 180s and aborted (berg-sandbox lost ~20 min to this). recycle now detects target ==
  `$CMUX_SURFACE_ID` and auto-applies force with a one-line notice; a self-targeted recycle has no human draft
  to protect. Folds in recycle plugin-writeback and a short-surface-prefix accept for `drive-child`.
- **doctor liveness no longer false-alarms long-idle or long-turn agents.** The detached / stuck / stale-gone
  verdicts were wall-clock based, so a live slow turn (pane streaming tokens, no hook events for 10+ minutes)
  and a long idle looked identical to a dead stream — the heartbeat flagged healthy conductors STUCK and a
  44-minute-idle worker STALE/surface-gone. All three verdicts, for workers as well as conductors, now gate on
  **transcript-advance / token-flow**: a `running` record frozen past the threshold whose transcript is still
  advancing is a live long turn, not a stall. Death-detection is preserved (tested both directions) and the
  live-pid-is-never-told-DOWN safety invariant is byte-untouched — this kills noise, it does not add teeth.

## [0.11.0] - 2026-07-18

### Added

- **`fleet conformance` — an instrument that tells you whether a cmux build actually WORKS.** Berg is about
  to run nightly (1100+ commits ahead) beside stable and does not want to read 1100 commits to find out
  what breaks. Run it on stable, run it on nightly, `diff` the two JSON reports: that diff IS the
  breaking-change report, derived empirically instead of inferred from commit subjects. 17 checks, tri-state
  (PASS/FAIL/UNKNOWN — an UNKNOWN is never laundered into a pass), each proving the **effect** and never the
  invocation. Every result names the cmux build it ran against.
  - **Safe by construction, not by care.** It creates its own workspace, its own agents and its own
    throwaway fleet state, and it is *structurally incapable* of touching a fleet member: it may only
    destroy UUIDs it created (recorded at creation — never name- or prefix-matched), production's registry
    is read once and made untouchable, and an AST test pins that no destructive cmux verb is invoked
    anywhere outside the sandbox. It refuses to run at all if its state resolves onto production's.
  - **A deliberate break exposed a FALSE PASS in the suite itself.** The first `paint` check asserted
    "the label appears in cmux's sidebar snapshot" — and passed against a doctored cmux whose `set-status`
    was a no-op, because the label was in the snapshot anyway *as the workspace's name*. The suite built to
    catch "the artifact looks right but the effect never happened" contained exactly that bug. It now writes
    a nonce into the field the fleet really uses (the workspace description) and reads it back.

### Fixed

- **Subscription-usage sidebar: split by provider, email labels, per-window resets, Fable, codex handled.**
  The footer now reads correctly per account once the fleet polls several claude/codex subscriptions at once.
  Six things were wrong or missing, all verified by RUNNING the interpreter (the `sidebar-probe` harness)
  against the live board, not reasoning about it:
  - **Colliding account name.** `usage_for_paint`'s display label preferred the claude `display_name`, which
    is "Berg" for *both* of Berg's accounts (one capital-B, one lower) — so two rows rendered as "Berg"/"berg".
    It now prefers the **email** (`seanyoungberg@gmail.com` vs `sean@berglabs.net`), unique and matching what
    codex already does. A dim `·config-id` tag (`·berg-max`) ties the email back to the config/dir naming.
  - **Split by provider.** Every usage line now carries a provider chip (claude ✻ / codex ‹∕›), reusing the
    per-row tool-icon vocabulary, so a claude and a codex subscription are never confused.
  - **Per-window reset timers.** The line carried ONE countdown (the shortest window's). Each rolling window
    (5h, 7d) now shows its OWN `resets in`, so a 5h reset and a 7d reset are independent.
  - **The claude weekly scoped sub-limit (Fable/Opus) is now surfaced** with its own %+reset — it was being
    dropped from the footer entirely (Berg's berg-max was sitting at 95% on it, invisible).
  - **Codex 5h window.** Confirmed the codex `/backend-api/codex/usage` payload exposes only a 7-day window
    for these team seats (`secondary_window` is null); the sidebar correctly shows 7d only. Nothing dropped —
    the length-labelled normalizer will surface a 5h automatically if the server ever returns one.
  - The `_usage_lines` record is an **append-only superset** of the old 7-field shape (0-6 unchanged, 7-12
    new), so `fleet.swift` (live via symlink the moment it's committed) and the daemon painter (live only
    after `uv tool install --force`) degrade gracefully in both directions during the adopt window — proven
    by rendering both shapes through the interpreter.
  - Also folds in the per-row tool-type icon for agent rows (every row declares its tool; a new tool is one
    line in `toolSpec`), previously stranded on `dev/sidebar-tool-icon`.

- **`fleet revive` could land an agent on a dark surface — the cure reproduced the disease.** `revive` is
  the *prescribed remedy* for a dark surface, and it had no dark check of its own. It bit production an hour
  after the launch fix shipped: `cmux-custom` was archived and revived, the revive landed it dark, and it
  spent the evening doing real work while the fleet insisted it was parked.
  - **The stranded label was the same event, not a second bug.** `revive` polled only its own surface
    (`poll_session`) with **no adoption fallback**, so a misfiled session read as "never bound" — and revive
    `sys.exit`ed *after the agent was already running*. A live agent on a surface nobody owned, its label
    still in the ARCHIVE, `ls` rendering the archived row over a working agent, and `vitals` dropping it
    entirely. The launch path had had the adoption fallback all along. `revive` now adopts (proven from the
    live process's own env) instead of aborting, and registers.
  - **One guard, every seating verb.** The dark-surface proof + re-seat is factored out of `launch`:
    `_reseat_if_dark(..., redeliver)` — the only per-verb part is the delivery (launch binds; revive resumes
    through the summary menu). Two implementations of this would have been the exact bug the previous night
    was spent killing.
  - **A structural test now pins the rule, not the call sites:** *any function that seats an agent onto a
    fresh surface must prove that surface is observable.* Unwiring the guard from `launch` or `revive` — a
    mutation no unit test caught — now fails, and so will a verb nobody has written yet.
  - `register` audited: it adopts an **existing** surface, so it cannot *create* a dark one, but it would
    register one and print DONE. It now WARNS and names the remedy. It must never re-seat — the agent is
    already working, and a re-seat there would destroy the context the operator ran `register` to rescue.
    `recycle` is sound by construction (it re-execs on the *same* surface, and its rebind gate already
    requires `present`, so a misfile surfaces as a loud rebind failure, never a silent dark row).

- **ONE liveness authority, and it is `resolve`.** Two branches independently built a pid-authoritative
  liveness guard — one in `resolve.py`, one in `cli.py` — and they did not textually conflict, which is
  *worse* than if they had: two authorities that never collide are two authorities that drift, silently.
  It is the same "nobody owned it, so two places implemented it" disease both branches were written to
  cure, and `resolve.py`'s own header already forbade it ("do not add a new raw hook-store read anywhere
  outside this module"). They are now one: `resolve.agent_pids` / `liveness` / `alive` / `dark`, with `move`
  and `launch` and the router all asking the same question of the same code.
  - **Liveness is a TRI-STATE, because "I could not look" is not "nothing is there".** `_ps_axeww` swallows
    a timeout or an exec error and returns `""` — and a box always has processes, so an empty sweep is a
    **failed** sweep. A guard that reads it as "nothing is running here" authorizes, on the strength of its
    own blindness, exactly the destruction it exists to prevent. `liveness()` returns `LIVE`/`GONE`/
    `UNKNOWN`; anything with a destructive remedy refuses on `UNKNOWN`. `alive()` is the boolean
    convenience for callers that only *warn*, and says so in its own docstring.
  - **The merge closed a hole in the launch path too.** `launch_verdict` could previously convict on an
    empty sweep — its printed cure is `fleet rm --kill`. It now returns `unproven` and refuses to condemn
    on a blind eye. That fix came from `move`'s guard being the stronger one; unifying let it flow both ways.
  - **Positive evidence of life needs one witness; only the negative conclusion needs a working sweep.** A
    live store pid now settles `LIVE` without consulting the process table at all. Checking the sweep first
    threw away a record that already proved the agent was running — and told a live conductor it was DOWN.
  - The suite's hermetic `ps` stub returned `""`, which under the corrected semantics means *the sweep
    failed*. Every test was silently simulating a broken `ps`. It now returns a real-shaped table with no
    agent in it, so "swept and found nothing" is the default and `UNKNOWN` means what it says.

- **`fleet launch` was unsound in BOTH directions. One principle fixes both: only an authoritative signal
  may condemn.** The house rule is now in the code (`resolve.alive` vs `resolve.present`), not just in a
  commit message: **the process table decides verdicts; a heuristic may WARN and may never CONDEMN; and the
  remedy must be proportionate to the confidence of the alarm.** Before shipping a check, ask what its cure
  does *when the alarm is wrong* — that is the question this codebase kept failing to ask.
  - **It invented failures, and handed you a destructive cure.** A perfectly healthy codex worker was
    reported `!!! LAUNCH FAILED — the process exited on spawn`, with `fleet rm --kill` as the printed
    remedy, because the first line of its pane was rc noise from the operator's `~/.zshrc` — printed
    *before codex was even exec'd*. Both existing guards missed it: `agent_tui_visible` looks for
    `Context N% left`, which codex only paints after its first turn, and the "scan below the launch line"
    rule assumed exec delivery has no shell (it runs `zsh -ilc`, which sources the rc file like any other).
    The verdict is now **pid-authoritative** (`launch_verdict`, pure + mutation-tested): a live process is
    never a failed launch, whatever the pane says. A live process with an ugly pane gets a *note* and an
    inspect command. `failed` requires BOTH no live process AND a startup error — and only then may the
    remedy be destructive, because by then there is nothing alive left to destroy.
  - **It missed real failures: the DARK SURFACE.** On 2 of 4 launches cmux files the agent's session under
    a surfaceId that is not the one it seated the agent on, and keeps stamping that phantom. The fleet
    already kept the *registry* right (it adopts the session against the live process's own env), but
    everything cmux keys by surface — `vitals`, `ls`, the sidebar — then looks straight through the agent.
    It runs, takes work, and completes turns, permanently invisible. Two specimens stamped 94 and 66 status
    updates onto surfaces that do not exist in the cmux tree, and 0 onto their own. A dark agent reads
    exactly like a dead one to every store-derived check, and the reflex cure for death is to relaunch —
    which lands a SECOND agent on the same worktree and branch as the first, which is still alive.
    Launch now **proves observability before it reports DONE** and re-seats onto a fresh surface when it
    cannot — at t=0, where the agent holds no context and the repair is free. Bounded, and non-destructive
    when it gives up: a still-dark agent is kept, registered, and explained (`archive` + `revive`, never
    `recycle` — that re-execs onto the same dark surface). *(An in-place store repair was tried and
    falsified: the hook re-created the phantom mapping and stamped it anyway. A fresh surface is the only
    repair.)*
  - **The router could tell a live agent it was dead.** `_alert_conductor_peers` gated its wording on
    `rs.present()` under a comment claiming "PID authority" that it did not have — `present()` reads cmux's
    *store*, so a dark conductor would be announced to its peers as "appears DOWN … `fleet revive`", and
    revive archives and relands it. The gate now asks the process table.
### Changed

- **`fleet move` REFUSES a live agent, and `--archive-revive` relocates it honestly.** Moving a live
  surface across workspaces **permanently destroys that surface's agent-status registration inside the
  cmux app** — surface-scoped, survives a process restart, and `fleet recycle` cannot repair it (it
  re-execs the pane on the *same* surface and comes back dark; a dark agent usually cannot even pass
  recycle's quiet-gate, which reads the very lifecycle the break freezes). The verb spent a day calling
  itself "the one safe verb" — true at the fleet layer, false at the cmux layer — and then shipped a
  WARNING printed next to the completed damage. A warning is not a guard.
  - `fleet move <label> … --archive-revive` archives the agent and revives it onto a **FRESH surface** in
    the target workspace (a fresh surface being the one thing that was broken), **resuming the full
    session — never the compact summary**. The fresh surface is *born* in the destination, never
    born-then-moved. Bystanders are safe: the teardown downgrades to `close-surface` and keeps the
    workspace when a sibling agent lives there.
  - Scope of the damage is **observability only** — a darkened agent still works, still receives its
    inbox, still answers. Do not panic-wake one.
  - Still allowed: a plain move of a **husk** surface (no live agent ⇒ no registration to destroy). An
    **archived** label has no surface at all, so a plain move is refused and `--archive-revive` simply
    does the revive half, landing it in the target.
  - The liveness test behind the refusal is **PID-authoritative** (hook-store live pids ∪ process-table
    seat-agent pids) and never reads `agentLifecycle` — the field a dark agent *freezes*, and the field
    `fleet ls`'s STALE predicate ANDs in. A lifecycle-gated guard would go quiet exactly when it is
    needed. A failed `ps` sweep reads as UNKNOWN and refuses, never as "nothing here".

- **`create_surface` resolved the parent surface's workspace from the hook store while resolving its pane
  from the tree.** That split made `launch --place tab|pane` depend on the parent having a live hook-store
  record — which a bare shell surface never has, and a *dark* agent's surface does not either. Measured on
  this box: an agent sitting plainly in `cmux tree` could not host a single tab child ("cannot resolve
  conductor workspace from --parent"). Both answers now come from one tree read (the visual ground truth),
  with the store as fallback only when the tree cannot be read.
- **`--place workspace` with no group aborted** instead of minting a standalone workspace, making a
  groupless workspace-placed agent unreachable from `launch` and `revive` — though `move --own-workspace`
  produced exactly that shape. It now mints one.

### Added

- **The codex seat home is now a fleet-owned thing, synced in ONE pass — `fleet codex-sync [acct] [--check]`,
  and on every `fleet launch --tool codex`.** A codex worker loads no claude plugins, so nothing the fleet
  ships to a claude agent — the `ground` skill, the dispatch conventions, the completion hook — ever reached
  it. It booted knowing nothing about the fleet it was a child of, and nothing carried its completion home.
  Two gaps, one cause: **nobody owned the home.** So one function owns it now, called from one place; a home
  that is already correct is not written to at all, and a seat added next month cannot miss either half.
  - **Citizenship: `$CODEX_HOME/AGENTS.md`.** The only instruction file a worker reads *regardless of its
    cwd* — and a worker's cwd varies (an agent home, a repo worktree, an ad-hoc dir). Codex's other source
    is a project chain walked from the git root down, and no file in that chain covers every worker.
    Verified against codex-cli 0.144.1, not assumed.
  - **An `AGENTS.override.md` REPLACES `AGENTS.md`; it does not merge with it.** So citizenship is installed
    *there* when an operator has one. Writing to `AGENTS.md` in that case yields a file that sits in the home
    looking installed and that codex never reads a line of — invisible on both ends. Mutation-tested.
  - **Hooks, installed and TRUSTED together — the completion push the seat migration had severed.** cmux
    wires a worker's `Stop` hook by writing `hooks.json` into the codex home, and only ever wrote one into
    `~/.codex`; moving every seat into its own `CODEX_HOME` moved every worker out of the one home that had
    hooks, so they fired `Stop` into a void. (Codex was never the problem — it *has* a Stop hook and it
    fires. `SessionEnd` does not.) **Trust is the half that fails silently:** codex will not run an untrusted
    hook and, under `exec`, does not prompt and does not complain — it just skips it. Trust is a
    content-bound `trusted_hash` in the home's own `config.toml`, so **hooks written without re-trusting in
    the same pass are exactly as dead as no hooks, while looking installed** — the same shape as the
    `AGENTS.override.md` trap. Delegated to `cmux hooks codex install`, which honours `$CODEX_HOME` and
    writes both halves, rather than re-implementing cmux's hash format. **No
    `--dangerously-bypass-hook-trust`**: it runs *untrusted* hooks, which is not the problem we had.
  - **`$AGENT_CONDUCTOR`** is now in every agent's launch env. `fleet peer-msg` addresses by label, and a
    child knew its own label and its role but never who launched it — so it could not report to its
    conductor even when it wanted to. Re-derived from the registry on recycle/revive, so it survives a
    restart.
  - `--check` is a pure read, and it says so: it reports `MISSING`/`STALE`, never `installed`. A check that
    claims credit for work it did not do is the same class of lie as a store read documented as "THE
    liveness answer".
- **`fleet vitals` grows a `blocked` column — the decision column, grounded in an actual gate.** It answers
  the question a conductor actually has (*is this agent waiting on ME?*) and it is emphatically NOT cmux's
  `needsInput`, which is stamped ~60s after ANY turn and therefore reads identically on a real gate and on
  an ordinary done-idle agent. Live-confirmed while building it: an agent sat at `needsInput` with a
  half-typed human draft in its input box, gated on nobody. Every conductor had to learn that trivia
  individually; the ones who didn't read it as "blocked" and were wrong.
  - **Three states, because both errors are expensive and neither may be guessed.** A false `yes` sends
    text into a busy pane and a mid-turn session send WEDGES the agent — the cure damages a healthy agent.
    A false `no` strands one forever. So `yes` and `no` each require positive evidence, and when the
    evidence is missing or self-contradictory the column says `?` rather than pick a side. In `--json` the
    tri-state is `true`/`false`/`null` (never strings), so the naive `if row["blocked"]` collapses unknown
    to the SAFE side; `blocked_why` always names the evidence that decided it.
  - **Evidence, cheapest first.** `yes`: an unreplied cmux Feed gate row; an unanswered
    AskUserQuestion/ExitPlanMode in the transcript (which still speaks for a DETACHED agent, whose gate
    never reaches the Feed at all); a selection dialog on the pane. `no`: the turn provably CLOSED — a gate
    always leaves it open — which also retires a stale feed row that a key-send never marked terminal; or
    the normal prompt chrome on the pane with no dialog over it.
  - **The pane is read ONLY for rows the cheap signals cannot settle** (`--no-probe` opts out). Mid-turn, a
    long tool call and a silent dialog are identical to every cheap signal, so the screen is the only thing
    that can tell them apart. A healthy fleet probes a handful of rows, not the board.
  - An **unregistered seat** (a live process with no hook-store record) never trusts a closed turn: that
    transcript belongs to a PRIOR session. This is the `claude --resume` picker window — hung at a dialog,
    having never taken a turn. `BOUND` is the visible guard at the short-circuit, and it is not redundant.
  - **Both TUIs, captured not guessed.** Codex draws nothing like claude (caret `›` U+203A vs `❯` U+276F;
    "Would you like to run the following command?" / "Press enter to confirm"), so its markers come from a
    real codex seat driven into a genuine approval prompt. The structural claim the whole pane read rests on
    — *a dialog REPLACES the normal chrome rather than rendering above it* — is now **measured** for both
    tools (16 live codex frames: 7 pre-gate carry the chrome and no gate, 9 gated carry the gate and no
    chrome). Had it been false for codex, "chrome present" would prove nothing and every gated codex seat
    would have read `no`. Note the fleet's codex floor pins `--dangerously-bypass-approvals-and-sandbox`
    (and codex refuses to combine it with `-a`/`-s`), so **no fleet-launched codex agent can raise an
    approval dialog** — the markers matter the moment one runs without the bypass.
  - **The `--watch` probe is debounced, not weakened.** A probe is a READ — it cannot wedge anything, only a
    SEND can — so accuracy is never behind a flag. Cheapness comes from not repeating work: a surface whose
    advance marker (session | record `updatedAt` | transcript mtime+size) has not moved reuses its verdict
    rather than re-reading the pane each tick, so a stable blocked agent costs ONE probe. The marker cannot
    sit still while a dialog appears, because every way a gate can ARRIVE writes first. Never cached: an
    unregistered seat (no marker → a frozen verdict) or an inconclusive pane (`?` is not a finding).
  - Verified end-to-end against the live 16-agent fleet by raising a REAL gate and reading the board from
    outside: `blocked=True (feed: unreplied gate row for this session)` naming exactly one agent, zero false
    positives across the other 16 in six consecutive samples.

- **Codex per-seat homes: concurrent codex seats, PROVEN.** Every codex seat declares its own
  `auth = "codex-home:<path>"` and runs as its own device. Three seats (two of them on ONE shared team
  subscription) ran concurrently and each produced real assistant output; none revoked another. This replaces
  and DELETES the env-token unified-home model, which was not merely limited but actively broken: the ChatGPT
  backend keys one active session per DEVICE, the device id (`installation_id`) is a file inside the codex
  *home* (not the OAuth client, which is shared, and not `auth.json`), so N seats sharing one `~/.codex` are
  ONE device and every login supersedes the last. A home is therefore a credential boundary, and the fleet
  now REQUIRES one per seat and NEVER guesses (a guessed home silently aims a seat at another seat's
  credentials). `~/.codex` itself is a legal, first-class home for exactly one seat.
  - `fleet codex-login [acct]` — with no acct it CYCLES every seat, and SKIPS any that already verifies. The
    skip is a safety property, not an optimization: a login supersedes, so re-logging a working seat is how
    you break it. Verification is a backend 200 **and the model actually speaking**.
  - **Wrong-account interlock**, keyed on the PERSON (`chatgpt_user_id`) and never the SUBSCRIPTION
    (`chatgpt_account_id`). Different people legitimately share one team plan — that is what a team seat is —
    so a subscription-keyed guard blocks a valid setup. The same person in two homes is the hazard, and it
    happens for real: a login that reuses a signed-in chatgpt.com session authenticates whoever the browser
    already was. The check is a PURE READ that runs BEFORE anything touches the home, because verifying a
    home RUNS codex, and a codex run is what mints the second device — the check would otherwise destroy the
    thing it was checking.
  - `fleet codex-setup` and `auth = "codex-token:…"` are **refused**, not deprecated. They provisioned the
    shared-home model, which IS the supersession bug; leaving them selectable invites its return.
  - Health and the usage poller both read each seat's OWN home, so they cannot disagree, and every seat
    reports its own identity and its own usage to `fleet usage` and the sidebar.
- **Codex account health monitor + offline alert.** The daemon checks each codex seat hourly, reading that
  seat's OWN home (never the fleet cred store, which was seeded from the shared `~/.codex` and goes stale the
  moment a seat moves into its own home — that staleness is exactly why a demonstrably healthy seat kept
  reporting `revoked`). It notifies (a surfaceless desktop banner) ONLY when an account newly goes offline and
  needs a human `fleet codex-login <acct>`.
  Edge-triggered (one alert per outage, re-armed on recovery; never a storm).
  Deliberately distinct from "usage stale" (no recent CLI activity: the account is fine and is never
  alerted). `unseeded` accounts (configured but not yet set up) are not alerted either. Health is decided
  by **two layers**, because the clock alone is false-healthy (see Fixed): a refresh check, then a
  read-only backend probe that catches a token the backend has revoked despite a still-future expiry.
- **Real account identity in the usage accessor.** `identity` (`{email, display}`) and a ready-to-render
  `label` per provider, so the sidebar shows the actual oauth account (e.g. "Berg") instead of the config
  id; falls back to the config id when identity is unreadable.
- **Codex agents are first-class in `fleet vitals`.** Context %, model, and effort were blank for codex
  agents (`—` / `-`) because `_context_used` and `_launched_prefs` read the claude-shaped transcript, while
  codex records all three in its rollout JSONL. `_codex_rollout_stats()` does one newest-wins pass over the
  rollout the fleet already tracks as the agent's transcript: context from `token_count` ->
  `last_token_usage.input_tokens` over the model's REAL `model_context_window` (e.g. gpt-5.5 = 258400, more
  precise than the old keyword guess), and the EFFECTIVE model + effort from `turn_context` (populated even
  when no `--effort` flag was passed; the field is `effort`, not `reasoning_effort`). A rollout with no
  token count still shows `—`, exactly as claude does. The claude path is untouched.
- **Codex turn completion is a real done-signal.** Codex fires no `SessionEnd`, so `turn_ended()` returned
  False for every codex agent: a finished codex agent showed "working" forever, and a plain `fleet rm`
  refused it as mid-turn (forcing `--force`). Codex does record the boundary in its rollout, so
  `_codex_turn_ended()` closes the turn iff the last `event_msg` boundary is `task_complete` with no
  `task_started`/`user_message` after it. Two consumers pick it up for free: observability flips the lagged
  "working" to ready/idle, and `cmd_rm`'s mid-turn guard lets a finished codex agent through. It fails
  closed, so it only ever NARROWS the refusal; a genuinely mid-turn agent still refuses.
- **Clean codex config for fleet workers.** Fleet codex workers ran the interactive TUI against the unified
  `~/.codex` and so inherited the Codex *desktop* app config: 5 MCP servers (2 of them dead), 13 plugins, 5
  marketplaces, i.e. connection errors and boot latency on every launch, none of it wanted for a worker. The
  interactive codex cannot `--ignore-user-config` (exec-only) and `-c mcp_servers={}` merges rather than
  clears (both verified against codex 0.144.1), and the config must still load anyway so the fleet's own
  `[model_providers.*]` blocks survive. So each cruft surface is disabled explicitly: `--disable plugins`
  (plugins + their MCP servers + marketplaces in one flag, leaving `features.hooks` intact so fleet
  lifecycle hooks still fire) plus one `-c mcp_servers.<n>.enabled=false` per server read from the LIVE
  config at launch, which is drift-robust (a server added later is auto-disabled for workers too).
- **Server-side codex usage API.** `GET /backend-api/codex/usage` with the seat's OAuth token, the codex
  analog to `poll_claude`'s `/api/oauth/usage`. Terminal-independent (no agent need have run), and strictly
  richer than the rollout scrape it replaces: a live reset countdown, email + plan in-call, and the
  metered/hard-cap signals (credits, spend control, `limit_reached`). Windows are normalized by LENGTH so
  the accessor renders codex identically to claude. Confirms the primary window is **5h exactly** (18000s,
  server-authoritative, fixed-epoch).
- **Usage accessor: badge, subscription grouping, hard-cap signal.** `usage_for_paint()` now carries a
  `badge` (source chip per tool), a `subscription` grouping key so seats sharing one bill read as one
  (codex seats share `account_id`; claude and api-key fall back to a `tool:account` singleton, so no
  subscription-only shape is assumed), and `limit_reached`, the hard-cap boolean that is distinct from the
  % bars. Additive fields; schema stays 1.

### Changed

- **`cmux-handover` no longer defers to `loom:handover`; it is unconditionally THE handover skill.** The
  skill opened by handing authority to a separate, private, vault-specific plugin when that plugin happened
  to be loaded — which a productized standalone cmux-fleet cannot do (its users will never have loom), and
  which forked the skill's behavior on the loadout. It also folds in the **commit chore**, until now tribal
  knowledge: a handover commits **its own paths** and nothing else — never `git add -A` (it sweeps a
  sibling's in-flight edit into a commit under your name), pass the paths to `commit` too (a bare commit
  takes everything STAGED, and the index is not exclusively yours), never `--amend` under an active fleet
  (a sibling may have committed underneath you), and `git -C <repo>` rather than `cd && git` (your cwd is
  your identity seat).
- **The resolve.py raw-hook-store invariant is now ENFORCED, not just asserted in a comment.** Its header
  said "do not add a new raw hook-store read anywhere outside this module — the stale-ghost class (six
  instances, all fixed 2026-07-10)". Six existed, all were fixed, and nothing prevented a seventh. A pure-AST
  ratchet (`tests/test_resolve_ratchet.py`) now fails on any new `read_hook_store()` call or raw store-key
  touch outside `resolve.py`/`state.py`. It is a BASELINE ratchet — the invariant as literally written was
  never true (24 raw touches exist today) — and it fails in BOTH directions, so fixing one and leaving it
  listed is also red: a baseline nobody prunes rots into a permission slip. It also pins resolve's
  deliberate delegation to `state.py`, which looks exactly like something to "simplify" and whose removal
  would silently detach much of the suite from the code it thinks it is patching. Mutation-tested four ways.

- **Workspace-group anchor model flipped A→B (empty-anchor), ratified 2026-07-10.** A conductor's group
  anchor is now the EMPTY scaffold workspace `workspace-group create` always mints, titled
  `Conductor - <label>`; the conductor runs as an ordinary MEMBER in its own `<label>` workspace. The old
  model re-anchored the group onto the conductor's own workspace and reaped the scaffold — which rendered
  the conductor as a bare folder shim in the native sidebar and forced its workspace title to the group
  name (shown by `fleet.swift` as the label). Three sites flipped: the launch bootstrap (`create_surface`)
  and `fleet group init` now KEEP + title the scaffold instead of `set-anchor`+close (strictly less code —
  the `_close_group_scaffold` reaper is gone); the archive re-anchor path (`_reanchor_group_off`) now mints
  a FRESH empty scaffold and anchors there when an anchor workspace is removed, NEVER onto a surviving
  member conductor (which would recreate model A). Live groups already restructured by hand are untouched:
  archiving a Model B member conductor closes only its member workspace, never the empty anchor. Contract
  verified live (`create --from` mints the scaffold anchor; `rename-workspace` titles it).
- **Sidebar state model: no post-turn lag, and "ready" no longer reads as "asleep".** A real-time,
  transcript-based turn-end signal clears a just-finished agent's "working" in ~5s instead of ~60s (cmux's
  own idle timer lags at `running` post-turn); it fails closed, so it only ever *clears* a stale "working".
  Ready-vs-idle is now time-based — a recently-finished agent reads "ready", only a long-dormant one reads
  "idle". The subscription usage footer is one line per subscription (5h %, 7d %, and soonest reset
  together), a larger font, real account names, and a single clean "usage stale" line for a stale provider.

### Fixed

- **A Cloudflare 403 is not a revocation: only the API may condemn a token.** `chatgpt.com` sits behind
  Cloudflare, which 403s an unrecognized client with an HTML challenge page — and the health probe (which
  identified as `cmux-fleet-health`) mapped ANY 401/403 to `revoked`. So a bot block became a REVOCATION
  VERDICT, and it condemned all three of Berg's seats minutes after each had demonstrably spoken; every one
  returned HTTP 200 the instant a codex User-Agent was used. The verdict was also DESTRUCTIVE, which is what
  makes it severe rather than merely wrong: the remedy it prints is `fleet codex-login <acct>`, and a login
  SUPERSEDES that seat — the false alarm would have killed the healthy seat it misdiagnosed. Fixed on both
  halves: every `chatgpt.com` call now identifies as codex (the usage poller was borrowing the *Anthropic*
  poller's UA, so it 403'd too and silently fell back to the ROLLOUT SCRAPE — painting a healthy-looking
  sidebar bar from stale files for a seat the API was refusing to talk to), and a 401/403 now means `revoked`
  only when the API *answered* it (a JSON body). An HTML body is the network refusing to carry the question,
  not the backend rejecting the credential: that is `unreachable` (transient, no alert).
- **An unseeded codex seat says `unseeded`, not "no rollout sessions found".** A seat that was never logged
  in fell through to the rollout scrape, which reported a scrape artifact — an unseeded home has no rollouts
  *either*, so the wrong probe returned a plausible answer that reads as "this seat just hasn't run lately",
  sending the operator to look for work when the seat simply needed one `fleet codex-login`. It also made the
  poller and health disagree about the same seat. The poller now speaks health's vocabulary.
- **The codex cruft-stripping flags are enumerated from the home the launch will ACTUALLY use.** They were
  always enumerated from Berg's desktop `~/.codex` (6 MCP servers) and then applied to a *seat's* home, which
  declares none — and `-c mcp_servers.<n>.enabled=false` on a server that home never declared CREATES a
  transport-less `[mcp_servers.<n>]`, so codex refuses to load its config at all (`invalid transport in
  mcp_servers.basic-memory`) and the agent never starts. A per-seat home is already clean, so the correct
  count of mcp flags for it is ZERO. Found only by launching a REAL agent; `codex exec` takes another path.
- **fleet-doctor: a LIVE agent is never called DOWN, and never handed a destructive remedy.** The doctor
  told the fleet that conductor `berg-sandbox` "appears DOWN (stall); check it and `fleet revive
  berg-sandbox` if it is". It was not down — Berg was sitting in it *typing*: pid up, 88% context. Obeying
  the alert would have destroyed the live session, because `revive` archives the agent and relands it on a
  FRESH surface: the advertised remedy for the false positive kills the thing it falsely accused. Root
  cause: `_alert_conductor_peers` hardcoded the DOWN script for every reason routed through it — but two of
  them (`stall`, `detached`) fire ONLY on a surface the sweep just proved PRESENT, and their positive signal
  ("the bound 'running' record stopped advancing") is exactly what a human typing produces. The alert also
  contradicted its own inbox header ("still LIVE — a health alert, not an archive"). The wording now comes
  from `router.conductor_alert_text()`, gated on a fresh pid read (**PID authority**, not the `agentLifecycle`
  string): live → INSPECT, and it says outright that a human may simply be typing; dead → the DOWN text,
  `revive` included, unchanged. Deriving it in the one place the words are written makes it structural — no
  reason, present or future, can hand a live agent a revive/archive/--force. `tests/test_fleet_doctor.py`
  pins the invariant on every channel the advice travels (peer wake, desktop banner, inbox row).

- **A launch could bind an agent's registry row to a completely unrelated live surface.** `fleet launch
  --label doctor-stall` created and launched onto surface `E4CED20C…`, then bound the registry to
  `3F2CDDD4…` — an idle staging shell that had been sitting there for a week. Because a conductor drives the
  *registry's* surface, `fleet drive-child` typed an entire brief into a bare zsh, which wedged at a
  `dquote>` prompt while the real agent sat idle with no instructions. (`fleet vitals` read it `detached`,
  which was correct: there was no agent on the surface the registry believed in.) The note it printed —
  "reconciled via AGENT_LABEL/cwd match in the hook store" — was false about itself: fleet passes
  `AGENT_LABEL` as an ENV VAR, while cmux records `launchCommand` as the exec'd binary's **argv**, which by
  construction excludes the `KEY=val` prefixes the shell consumes — so the precise label arm is structurally
  dead and every discovery silently degraded to the loose **cwd** arm, which is not an identity (every shell
  in a worktree shares it) and which returns the record's `surfaceId` — a hook-time attribution that was, in
  this case, simply wrong. **Invariant I5: the launched surface is authoritative.** `_bind_launched_session`
  now returns `(ws, surf)` exactly as they came in; a reconciliation may only FILL IN the missing session id,
  and only against proof from the live process's own environment (`resolve.proc_ident` → `ps eww` →
  `CMUX_SURFACE_ID`/`AGENT_LABEL`), never from the store's `surfaceId` and never from cwd. When that proof is
  unavailable the sid stays empty and `cmd_launch` aborts safely without registering, leaving the surface up
  and signposting `fleet register --surface <launched>`: an empty sid is a recoverable gap; a registry row
  pointing at someone else's terminal is not. `tests/test_launch_surface_authority.py` pins it.

- **`fleet <verb> --help` no longer RUNS the verb.** Only the verbs that happened to build an
  `ArgumentParser` got `--help` for free. The 18 hand-rolled ones either swallowed it as a positional label
  (`fleet rm --help` → "no such label '--help'") or — the dangerous half — ignored it and executed:
  `fleet inbox --help` ran the inbox, `fleet paint --help` painted the sidebar, and **`fleet serve --help`
  started the HTTP server and blocked**. The top-level usage blob is now a `VERB_USAGE` dict (one source of
  truth: `fleet --help` prints the joined values, byte-identical to before), and `main()` prints the matching
  entry for a hand-rolled verb *before* dispatch. The guard fires ONLY when `-h`/`--help` is the FIRST token,
  so a `peer-msg`/`drive-child`/`broadcast` body that merely mentions `--help` is still delivered, never
  swallowed. Argparse verbs keep their own richer auto-generated help. `tests/test_help.py` loops the WHOLE
  dispatch table (internal workers included) and fails a verb that blocks, exits non-zero, prints anything
  but usage, writes state, or shells out to cmux — so a verb added tomorrow cannot regress this.

- **`fleet move` told operators to run the one command that cannot fix a moved surface.** Its post-move
  WARNING blamed the stale `CMUX_WORKSPACE_ID` and directed the operator to `fleet recycle <label>` "to
  re-export the env and rebind the hooks"; `resolve.py` likewise recorded the remedy for a detached agent
  as "a reseat (recycle resume)". The 2026-07-10 root-cause falsified both. The break is **surface-scoped**:
  moving a live surface permanently destroys its agent-status registration inside the cmux app, it survives
  a process restart, and the stale env is a fellow-traveller of the move rather than its cause (probed
  post-move with the CORRECT current workspace id: still no stamp). A `recycle` re-execs the pane on the
  SAME surface, so the agent comes back dark, and a dark agent usually cannot even complete a recycle (it
  fails the quiet-gate, which reads the very lifecycle the break freezes, so `--force` is needed just to
  attempt the thing that will not work). The guidance now states the real remedy, `fleet archive` +
  `fleet revive` (revive lands on a FRESH surface, verified live), states the real scope (an OBSERVABILITY
  break: liveness, completion routing, and inbox delivery are all UNAFFECTED, so nobody should panic-wake a
  detached agent), and points at prevention (launch children straight into their final workspace). The env
  mismatch is retained where it was actually load-bearing: as a deterministic *detector* of a moved surface
  in the attachment axis. Text and diagnostics only, no behavior change; the write-only `move_env_stale`
  event is renamed `move_surface_detached` to stop encoding the falsified theory.

- **Codex health check saw an expired token but not a REVOKED one (false-healthy).** `codex_health_check`
  short-circuited on the clock: `codex_ensure_fresh` returns the stored token with no server call whenever
  it is more than 30 min from expiry, so a token the ChatGPT backend had already revoked still reported
  `healthy`. Live-confirmed 2026-07-10: 2 of 3 seeded seats were backend-revoked (superseded, per the
  unified-home limitation above) yet had a future JWT `exp` AND returned 200 from the IdP `userinfo`
  endpoint. Expiry and userinfo are BOTH false-healthy; only the ChatGPT backend can see the revocation.
  New `codex_probe_backend()` does a read-only `GET chatgpt.com/backend-api/me` (no refresh, no mint, no
  token spend) returning live / revoked (401-403) / unreachable, and runs as layer 2 after `ensure_fresh`,
  so a backend revocation despite a future expiry now edge-triggers the existing re-login alert. A follow-up
  (`44bf1c8`, caught by a cross-model review) maps `unreachable` to `error` rather than `healthy`: a network
  blip means the token could not be VERIFIED that tick, so calling it healthy overclaims and would falsely
  signal recovery. Transient probe failures alert nobody and simply retry next tick.

- **Stale usage line no longer draws twice in the sidebar.** A stale/failed provider rendered a
  "usage stale" line *over* a phantom "-% -%" row (two broken lines). The cmux-sidebar interpreter treats
  a `some View` func as a view *builder* — it collects every view expression whose guard holds and *ignores*
  `return`, so `if stale { return staleLine } return freshLine` drew BOTH. `usageLine` now delegates to two
  positive-guarded sub-views (`usageStale`/`usageFresh`, exactly one is non-`EmptyView`); the same latent
  pattern in `ctxRow`/`cwdLine`/`lastLine`/`usageWindow`/`resetView` was flipped positive too. Render layer
  only — the poller's stale state is untouched.

- **`fleet rm --with-group` no longer refuses to dissolve any Model-B group.** The dissolve preflight
  cross-checks the registry's believed group membership against cmux's REAL membership and aborts on
  divergence (the 2026-07-02 registry-integrity guard). Under the new empty-anchor model that check
  compared against cmux's *full* membership, which always includes the AGENTLESS scaffold anchor no
  registry row can ever occupy — so every Model-B group looked like a mismatch and `--with-group`
  refused to dissolve any of them (regression caught by the 2026-07-10 live acceptance). The guard now
  subtracts the group's anchor scaffold (resolved via `anchor_workspace_ref`) before comparing agent
  workspaces. Its real purpose is unchanged: a genuine divergence among *agent* workspaces, an
  unverifiable registry row (agent with no workspace), or unreadable cmux group data still aborts. The
  dissolve itself already closes the scaffold (`workspace-group delete <ref>` closes every member).

## [0.10.0] - 2026-07-10

### Added

- **Custom fleet sidebar restored and made live.** `fleet.swift` renders model·effort, tool, ctx,
  status, and last message per agent again, all sourced from the same `snapshot()` the CLI uses (the
  earlier native-first rewrite had dropped them for cmux native fields that did not match `fleet vitals`).
  Model·effort also rides the built-in ctx-bar caption. A compact per-subscription usage footer reads
  `usage_for_paint()`. The daemon now auto-repaints the board (~4s, on-change, opt-in via
  `[fleet].sidebar_paint`) so it self-refreshes instead of drifting stale between manual `fleet paint` runs.
- **`usage_for_paint()` accessor + pluggable poller registry** (`providers.py`). A stable, versioned
  (`schema:1`), provider-agnostic view of subscription usage for the sidebar or any consumer; adding a
  provider (Vertex, Gemini, a direct API) is a `register_poller` plus config, not a rewrite.
- **`cmux_fleet/resolve.py` — the one resolver** (agent-management v2, step 1). `seat(surface)` answers
  presence, live pids, workspace, pane and attachment for a surface; `snapshot()` batches that across
  the registry in one store read plus one tree read; `group_members()` reads membership from cmux, never
  from the registry. Presence is the pid rule, stated once: an agent is present iff its surface holds a
  hook-store record with a live pid whose `ps` identity matches the agent's tool. Seven ad-hoc predicates
  that each restated some version of that rule (`surface_has_live_agent`, `surface_has_live_pid`,
  `_live_bound_sid`, `ws_uuid_for_surface`, `current_ws_for_surface`, `resolve_bound_record`, and
  `poll_session`'s fallback) now route through it, so `ls`, `vitals`, `doctor`, the wake gate, the launch
  guards, recycle's confirm and `rm`'s stops all read one truth. Topology (workspace, pane, group) is
  derived from `cmux tree` at read time; the registry's stored `workspace` / `status` / `place` fields
  still exist but nothing reads them (removing them is step 3, not yet authorized). Shipped `4b19974`.
- **The attachment axis and the `detached` doctor condition** (invariant I4, ships with step 1). An agent
  can be *present but detached*: its process works while its hook channel is dead, so its completions stop
  reaching its parent, its Feed gates go invisible, and `updatedAt` freezes. Detached is a conjunction —
  record frozen **and** evidence of activity — never a frozen record alone, because that describes every
  idle agent. Evidence is behavioral (the agent's last turn is recent while its record is frozen) or
  deterministic (`ps eww` reports a `CMUX_WORKSPACE_ID` differing from the tree's, which is what a
  relocation leaves behind). The doctor names the state; the remedy is a reseat (`fleet recycle`). The
  fleet never auto-heals it.
- **`cmux_fleet/adapter.py` — exec-delivery for every process start** (agent-management v2, step 2).
  `launch` and `revive` now deliver the tool as the pane PROCESS via `respawn-pane`, the path `recycle`
  already used, so all three start a process the same way. The resume-summary menu dismisser moved here
  and is shared by all three. `CMUX_FLEET_EXEC_LAUNCH=0` reverts every verb together. The paste tower is
  retained, unused on the default path, for a one-week soak. Shipped `2963dfc`.

### Changed

- **Recycle launch is exec-style — the paste class is dead.** The relaunch is now delivered as the pane
  PROCESS via a second `respawn-pane` (`/bin/zsh -ilc '<launch>; exec /bin/zsh -il'`), not a paste into
  a shell: the command travels as one argv element end-to-end, so the `[Pasted text #1]` large-paste
  collapse, the enter-race, and the self-heal re-fire cannot happen on this path (live probe: a
  2898-byte command executed byte-exact). The chained trailing shell is load-bearing — a bare `-ilc`
  pane dies WITH its command and cmux destroys the whole surface, so the chain makes a crashed launch
  degrade to the old recoverable bare-shell husk instead. The old agent is still verified dead on a
  bare-shell respawn BEFORE the launch exists (keeping the live-pid confirm semantics intact), and the
  TUI-up guard carries over (a respawn over a live agent would destroy it — refuse + escalate). Default
  ON for `fleet recycle`; `CMUX_FLEET_EXEC_LAUNCH=0` falls back to the paste path, which also remains
  the automatic degradation on a respawn-pane error. `prime`/`drive-child`/resume-menu keystrokes still
  `send`. See `docs/design-exec-launch.md` (now IMPLEMENTED, with the corrected exit-semantics finding).

### Fixed

- **`fleet usage` labeled codex windows by slot, not by duration.** The poller mapped `primary`/`secondary`
  positionally (5h/7d), but a free-plan account returns a 30-day `primary` and a null `secondary`, so a
  "5h" window showed "resets in 719h". Windows are now labeled by `window_minutes`.
- **The `_agent_pid_check` real-`ps` test hardcoded `"python"` and was red in the canonical checkout.** Its
  interpreter reports argv0 basename `Python` (Homebrew framework) vs `python` (a uv venv); the positive
  assertion now derives the expected name from `ps` instead of hardcoding it. The negative cases (not
  `claude`, dead pid, garbage) stay fixed.
- **An idle agent read `detached`, because the activity signal was the transcript's file mtime.** Step 1's
  behavioral detector compared the hook-store record's `updatedAt` against the transcript file's mtime.
  claude appends `system` / `permission-mode` / `bridge-session` bookkeeping lines to an idle agent's
  transcript long after its last turn, so mtime advances while the agent sits at the prompt, and every
  idle agent tripped the detector. Caught on the live fleet minutes after merge, with the daemon running
  it: two of three idle agents read detached, and the doctor emits `detached` straight off `attachment()`,
  so it would have paged a conductor and the desktop for each. The signal is now the newest turn timestamp
  parsed from a bounded 256KB tail of the transcript, and it **abstains** (returns no reading) when no turn
  parses, so an unreadable transcript can never manufacture a detach. Turn timestamps are UTC; they are
  read with `calendar.timegm`, not `mktime`, which was silently an hour off and would have masked any
  sub-hour skew. The invariant this restores is the one the design ratified: an idle agent must never read
  detached. Fixed `a51d288`.
- **`rm` and `archive` would have refused to close any conductor, forever.** The never-orphan pid set took
  the process-table half unfiltered, and a conductor's surface env is legitimately inherited by three to
  five never-dying daemons, routers and servers. Every conductor close saw a live "agent" pid that would
  not die. The substring identity check also matched marketplace hook-script paths. The rule is now applied
  per source: store pids block as-is; a process-table pid blocks only if it passes the seat-agent test.
  Fixed `055da1a`, then sharpened in `a3286a7` to the exact discriminator — the claude wrapper exports
  `CMUX_CLAUDE_PID=$$` and then execs, so the seat agent is the one process whose `CMUX_CLAUDE_PID` equals
  its own pid; descendants inherit the value under different pids, which also closes the `claude -p`
  summarizer residual. Both landed with a regression test that deliberately un-stubs the `ps` sweep.
- **The never-orphan check did not exist at all for codex.** The codex fallback matched argv0 by basename,
  but parsed the TTY column as argv0: `ps axeww` prints PID, TT, STAT, TIME, COMMAND, and argv0 is field 5.
  The process-table half of the union was therefore always empty for a codex seat. A fake two-column test
  fixture had let it ship green. Every `ps` fixture is now copied from real output shape, and the codex
  assertion fails the old parser by construction. Fixed `0a13eec`. Verified live: a lazy, unbound codex
  probe with an empty hook store resolved to its pid, and `rm` signalled it.
- **`fleet vitals` and `fleet sessions` printed every age as `495464h ago`.** On 2026-07-07 the
  providers work added a second `def _age(epoch)` to `features.py`, beside the `def _age(secs)` that
  had been there since 06-29. Python bound the later one, so the two callers that pass a DURATION
  (`vitals`' idle column, `sessions`' per-session age) silently got the EPOCH formatter and rendered
  `now - 120s` as ~56 years; `sessions` also doubled the suffix (`495464h ago ago`). Nothing raised —
  the argument is a plausible int either way — and the suite stayed green for three days. The epoch
  formatter is now `_ago()`; `_age()` is the duration formatter again. Note which signal this blanked:
  `sessions`' age column is the operator's only guardrail when choosing a session to revive.
- **A duplicate `_store()` in `cli.py`** (verbatim, harmless) is deleted, and
  `tests/test_no_shadowed_defs.py` now fails any module that defines a top-level name twice — the
  class-level guard for both bugs above.
- **`fleet rm --with-group` never signals its own caller or a bystander conductor** — two hard guards
  run before the confirm gate and are NOT bypassable by `--force`/`--yes`: (1) if the group contains the
  CALLER's own surface (self-ID via `$CMUX_SURFACE_ID`), refuse — the member-stop loop would otherwise
  SIGINT the caller's own pid mid-dissolve and complete the teardown with no clean error; (2) if the
  group contains a conductor that is not the named target, refuse — a child's group dissolve never takes
  a conductor as collateral (retire a conductor's group by naming the conductor as the target, from
  outside the group). Both refusals fire with zero signals, nothing closed, registry untouched, and name
  the blocking agent by label and kind. Live shape that motivated it: a conductor sharing its group with
  two children meant `rm <child> --with-group --yes` would have SIGINT'd the conductor. Bulk recycle
  already skipped self; the dissolve was the outlier.
- **`fleet rm --with-group` no longer leaks live members** — the dissolve stops EVERY member's agent
  (live-only, identity-checked, death-verified) BEFORE `workspace-group delete`, all-or-nothing: a
  group-wide pre-flight identity check refuses with ZERO signals fired if any live pid can't be
  identified (never half-kill a group, then discover a foreign pid), and any member whose agent survives
  SIGINT x2 refuses the WHOLE dissolve — no group delete, no sibling closes, no registry change, the
  blocking member named for the operator. A partial dissolve that strands one agent while tearing down
  its neighbours would leave the survivor invisible AND groupless. `--force` does not bypass. This was
  the last kill site not on the live-pid truth.
- **`fleet rm` / `fleet archive` no longer leak live agents** — both verbs now stop the agent via the
  recycle tail's live-only, identity-checked kill selector (`_signal_agent_pids`) and VERIFY death before
  closing the surface; if a live agent on the surface won't die or can't be identified, the verb REFUSES
  (registry untouched, seat left open + reachable) instead of closing over the survivor — even under
  `--force`. The old form SIGINT'd the first hook-store record's pid (no aliveness check) and closed
  unconditionally: on a multi-record surface that stranded the real agent alive with no pane, no `fleet
  ls` row, and no way to find it (four live 1M-ctx orphans found on the box 2026-07-10, two from that
  day's rms). `_pid_for_surface` — the first-record lookup that fed every kill site the wrong target —
  is deleted with zero callers left.
- **Recycle kill path targets live pids, identity-checked** — the graceful close and the direct-kill
  fallback now SIGINT every ALIVE agent pid whose hook-store record maps to the surface, each re-verified
  as this tool's live process via `ps` immediately before signalling (the pid-reuse guard). The old form
  drew a single target from the FIRST hook-store record with no aliveness check: on a surface with several
  lingering records it SIGINT'd dead ghosts (corpses 76035/70208, live incident 2026-07-10) while the real
  agent survived orphaned on an abandoned tty and the verify — correctly — refused every subsequent
  recycle until a human killed it. With live-only targeting the direct-kill fallback is also the orphan
  reaper: the abandoned-but-alive agent still maps to the surface, so it is selected and cleanly SIGINT'd
  instead of wedging the seat. A live pid that fails the identity check is skipped loudly (abort +
  escalate beats signalling a foreign process).
- **Recycle: fresh confirm is live-pid-resolved** — `_poll_session_back` fresh mode now confirms on the
  freshest hook-store record with an ALIVE pid (`_live_bound_sid`), replacing the sid-exclusion confirm
  that rode poll_session's arbitrary-first-record fallback and could stare at the dead lingering ghost
  forever while a healthy fresh agent sat on the seat unconfirmed (four identical berg-sandbox
  misdetects, 2026-07-09). A live bind equal to the OLD sid does not confirm as fresh (a cmux
  restart-resume zombie is live, not fresh) — it falls through to WARN + escalation.
- **Recycle: never paste a launch into a live agent** — `_fire_launch` refuses to fire when an agent TUI
  (or its resume menu) is already up on the surface, covering both the initial fire and the self-heal
  re-fire. Kills the garbled-inert-draft class: the old self-heal re-pasted the launch into the live
  TUI it had just misdetected. The bare-shell self-heal (PATH-not-ready crash recovery) is preserved.
- **Recycle: failures escalate to an actor** — every terminal recycle failure (respawn-not-confirmed,
  no-session-after-launch, resume-menu-wedged) now routes a `recycle-failed` doctor alert to the failed
  agent's parent conductor (inbox + wake, per-attempt event key) or — for conductors — fans out to peer
  conductors + the desktop like conductor-down. Previously the only signals were a banner on the failed
  seat itself (which nobody is watching, by definition) and a `recycle_abort` log line.

- **Notification dedup: event-key ack** — every inbox row now carries a durable `event_key`, and one
  `fleet inbox-ack <seq>` clears that event on **every** presentation path at once (awareness, Stop-drain,
  heartbeat, the router wake gate, `fleet inbox`) and refuses a producer re-put of it. A bare
  `inbox-ack <seq>` acks the kind the seq actually points at (no more advancing the completion cursor
  because `--doctor` was forgotten); the kind flags stay as compat fallbacks. Kills the daemon-restart /
  dedup-loss replay of already-handled doctor alerts. (audit fix-order #5)
- **Notification dedup: presentation cooldown** — a presentation ledger (distinct from ack) records which
  events each surface was shown recently. The heartbeat now **reminds** on an interval instead of
  re-nudging every tick: it wakes only for rows no path (direct wake / Stop-drain / awareness / a prior
  reminder) has surfaced within `HEARTBEAT_REMIND_S`, and a genuinely-ignored unacked row still gets a
  reminder once the window elapses. Kills the heartbeat re-waking a row a direct peer/doctor/completion
  wake or a drain block already put in front of the agent. (audit fix-order #4)

## [0.9.0] - 2026-07-08

### Changed

- **Plugin loadout is now one key + one flag.** The mechanism-agnostic roster key `use` is renamed
  **`plugins`**, and the launch/recycle/revive add-flag is a single repeatable **`--plugin`** (replacing
  `--use` / `--plugins` / `--add-plugin`). A role names its plugins in one `plugins = [...]` list; the index
  (`plugins.toml`) still decides linked (`--plugin-dir`) vs enabled (`enabledPlugins`) per plugin, so the
  type distinction stays index-internal. A name not in the index loads as a linked `--plugin-dir` (default
  marketplace / absolute path), exactly as before.
- **Marketplaces are declared explicitly; the config self-documents sources.** The `[fleet].marketplace` /
  `$CMUX_FLEET_MARKETPLACE` shim is **removed** — there is no implicit `default` marketplace. Marketplaces
  come only from `[marketplace.<name>]` blocks in `plugins.toml`, so every plugin's `source` names a real,
  visible marketplace. A linked plugin resolves via its declared marketplace or an absolute path — a bare
  *unindexed* name no longer resolves under a hidden env-var dir. Build hermeticity is now carried by pinning
  the plugin INDEX (`CMUX_FLEET_PLUGIN_INDEX`) into child launches + `fleet profile` (children resolve the
  same declared marketplaces), replacing the old `CMUX_FLEET_MARKETPLACE` pin. A local marketplace with no
  `marketplace.json` is still fully scanned (descriptions from each plugin.json, `origin=path`).
- **`fleet plugins add <ref> --as linked` now records the plugin in the marketplace's `marketplace.json`,**
  so a reconcile derives an honest `origin`: a git URL → `origin=url`, a local path → `origin=path` (the
  manifest is created if the marketplace has none). Added `--name <n>` to index/clone under a chosen name.

### Fixed

- **`fleet plugins add` reports a basename collision instead of a misleading no-op.** When the ref's derived
  name is already indexed from a *different* marketplace (e.g. `orgB/tools` vs an existing `tools`), `add`
  now STOPs and points at `--name <other>` rather than printing "already indexed; nothing to do" and aiming
  the user at the wrong plugin.

### Removed

- **The legacy plugin keys and flags (no external users).** The pre-index roster keys `plugins` (linked-only,
  index-bypassing) and `enable_plugins` (enabled-only) are deleted — their capabilities are fully covered by
  the index-resolved `plugins` key (its not-in-index fall-through is the old `plugins` behavior; its enabled
  type is the old `enable_plugins`). The `--plugins` and `--add-plugin` flags are deleted (subsumed by
  `--plugin`, which reaches both plugin types). The live roster was already 100% on `use`, so migration was a
  straight `use → plugins` rename.

## [0.8.0] - 2026-07-08

### Added

- **Mass-close confirm-gate** — `fleet rm --with-group` on a consequential group (a live conductor or bound
  children) now previews the full list-what-dies and requires `--yes` before acting (agent-safe: a preview +
  `--yes` re-run, never an interactive prompt that would hang a conductor mid-turn). Prevents the group-dissolve
  mass-close accident class.
- **Conductor-down detection** — the fleet-doctor sweep no longer skips conductors: a stalled conductor turn, a
  registry-live conductor sitting as a bare-shell husk (a failed self-recycle), or a closed conductor surface now
  alerts every live peer conductor plus a surfaceless desktop banner. Guarded by transition-only firing
  (process-local, defuses the reboot storm) and a 600s grace window (a legit recycle rebinding within it never
  fires).

### Fixed

- **needs-input false-positives eliminated** — the doctor's needs-input predicate flagged ~100% false (cmux
  stamps `needsInput` ~60s after ANY turn ends, indistinguishable from a real gate at the lifecycle level).
  Replaced with a transcript discriminator: alert only on an actually-unanswered `AskUserQuestion`/`ExitPlanMode`;
  suppress done-idle / survey / anything-else (fail-safe). Also guards the stall predicate against false-firing on
  a live tool-less extended-think.
- **Recovery-primitive residuals** — signposted the `fleet register`-after path on a bind-timeout, scaled the
  post-menu poll for heavy loadouts, and made the checkpoint-heal loud + fail-loud when no resumable id exists.

## [0.7.0] - 2026-07-07

### Added

- **`fleet move` + `fleet group`** — relocate a live child to its own workspace (or another) as one atomic,
  wake-safe step, and manage the conductor's workspace-group (init/add) so children can launch straight into
  it. Replaces the manual `move-surface` + `register` dance.

### Fixed

- **Router no longer archives a live child on a workspace MOVE.** The surface-close reconciler confirms
  against cmux's live tree and archives only on a true close (surface gone), reconciling the registry
  `workspace` on a move. Fails closed (unreadable tree still archives). Fixes the incident where relocating
  three live children auto-archived them.
- **Tool-aware launch flags.** `--effort`/`--model`/permission flags translate per tool at the adapter
  boundary (claude `--effort` → codex `-c model_reasoning_effort=`, etc.), so a codex child no longer dies on
  a claude-only flag. Reasoning tier passes through (codex accepts `xhigh`).
- **Launch verification + never-bound sweep.** A launch that dies on arrival (bad flag, missing binary) is
  caught loudly instead of sitting `pending` forever; the daemon detects and alerts a child that launched but
  never bound.

## [0.6.0] - 2026-07-07

### Added

- **Provider config + usage tracking (`[providers]`, `fleet usage`, `--provider`)** — a `[providers]` section
  in `fleet.toml` (per tool: subscription/api/vertex; current accounts as defaults, inert until configured);
  a Claude usage poller (`GET /api/oauth/usage` → 5h / 7day / Fable-scoped / metered) and a codex usage poller
  (newest rollout `rate_limits`, zero-auth, stale-flagged) driven on the daemon timer; a read-only
  `fleet usage` view; and a `--provider tool:name` launch flag with claude token-file injection (tokens
  resolve under the fleet state dir, `0600`). Codex account-selection is a marked-provisional stub pending a
  live mechanism test; recycle-with-account and policy auto-switch are deferred to later phases. Phase 1 of
  the usage-ops provider-config design.

## [0.5.2] - 2026-07-07

### Added

- **`--json` on `fleet ls` and `fleet graph`** — machine-readable output for the two listing verbs that
  lacked it (`ls` reconciles once, then renders JSON or a byte-identical text table).

### Changed

- **Backward-compat cruft removed** (no external users): `broadcast --target` and `recycle`'s bulk
  `--all/--conductors/--children/--my-children` alias flags are gone — `--scope` is the only spelling. The
  `--resume` no-op alias is removed (RESUME is the default). Retired flags scrubbed from examples.
- **Vocabulary consistency:** prose synonyms unified to one term per concept (`worker`→`child`,
  `seat`/`terminal`→`surface`, `park`→`archive`, `queue`→`inbox`; the handover-skill `worker`-overload
  fixed); stale tool references updated (`fleet.py`/`router.py` → the `cmux_fleet` package +
  `fleet daemon start`); `main()` usage synced with the dispatch table; a session-vs-transcript and
  `--force` glossary added.

## [0.5.1] - 2026-07-07

### Fixed

- **Fleet-doctor sweep no longer over-fires** — three `needs-input`/doctor false-positive classes fixed:
  - **Restart no longer replays handled alerts.** The doctor's condition dedup is now persisted
    (`doctor-dedup.json`, keyed by `(reason,label,session)`), so a daemon restart stops re-alerting
    steady-state conditions already seen in a prior process.
  - **Completion + needs-input double-fire suppressed.** A just-finished child no longer produces both a
    completion and a redundant `needs-input` doctor alert (suppressed within a 120s co-incidence window,
    with an `updatedAt`-transition guard so a genuine later gate still alerts).
  - **Archived/closing surfaces skipped.** The sweep now honors the expected-close tombstone and the
    `surface_has_live_agent` live-truth boundary (parity with bulk-recycle/ls), fixing the archived-surface
    race.

## [0.5.0] - 2026-07-07

### Added

- **`fleet inbox` verb** — an on-demand read of your pending inbox (child completions,
  auto-archive/health alerts, and peer messages, oldest-first, each with its `inbox-ack`
  command). The catch-up read for wakes that queued while an agent was down: run it at session
  start or after a recycle, since the push path can't replay across a fresh session.
  `fleet inbox [--scope mine|<label>|all] [--json]`.
- **Unified `--scope` model across every scope-aware verb.** One vocabulary —
  `--scope mine|all|conductors|children` (plus a bare `<label>` where a verb single-targets) — on
  `ls`, `vitals`, `inbox`, `graph`, `recycle`, `broadcast`, and `mute`. Only the default varies, by
  risk: **read verbs default to `mine`** (you + your children; `--scope all` for the whole fleet,
  with a hint when `mine` is just you), while **act verbs require an explicit scope** (`recycle`
  bare = self, `broadcast` errors without `--scope`). A human at a plain shell with no
  `$CMUX_SURFACE_ID` still gets the whole fleet by default. One shared `scope_matches` predicate
  backs every selector so a read's view set and an act's target set can't drift; on `mine`, reads
  include you and acts exclude you (self is always the bare form).

### Changed

- **`broadcast --target` and `recycle`'s bulk `--all/--conductors/--children/--my-children`** migrate
  onto the unified `--scope`; the old spellings are kept as hidden, deprecated aliases.
- The conductor boot skills (`ground`, `cmux-fleet`, `cmux-handover`) now teach the `--scope` model
  and the boot ritual: run `fleet inbox` at session start (instead of hand-reading state) and
  `fleet ls --scope mine` to know your fleet.

## [0.4.0] - 2026-07-07

### Added

- **Plugin index system (`plugins.toml` + `use`).** cmux-fleet now carries a
  first-class plugin index: a `plugins.toml` spine that catalogs each plugin's
  type (linked `--plugin-dir` vs enabled `enabledPlugins`), source marketplace,
  tools, and description, resolved through a new `use = [...]` roster key
  (unioned floor∪role like `plugins`) so a single name reaches BOTH plugin
  channels. It layers additively over the legacy `plugins`/`enable_plugins`/
  `marketplace` keys — a config with no `use` composes byte-identically. Adds
  discovery verbs (`fleet plugins ls|show|describe`), a `fleet plugins reconcile`
  that derives the index from local marketplaces + `~/.claude` settings while
  preserving hand-authored fields (and reporting drift rather than clobbering
  curated ones), a dynamic `--use NAME` on launch and recycle (the index-aware
  successor to `--plugins`/`--add-plugin`, and the first CLI add-surface that
  reaches an *enabled* plugin at launch or recycle), and `fleet plugins add
  <ref>` to clone/wire a new plugin from a git URL or local path at a
  deliberately SAFE default — the verb writes ONLY the index; it never enables
  the plugin, edits a role's `use`, or runs its hooks (a human flips it on later
  via `fleet recycle <agent> --use`). Every write path aborts rather than clobber
  a malformed-but-populated index (a hand-authored-data-loss guard) and surfaces
  cross-marketplace name collisions instead of resolving them silently.

- **`fleet vitals --watch` — a live, non-flickering fleet board.** A dock-pane
  watch mode that repaints only on a real change (ANSI home-clear, not a full
  clear; a 12s heartbeat refreshes ages), sharing one pure renderer with the
  one-shot table so the painted output stays byte-identical to `fleet vitals`.

- **Fleet-doctor: proactive parent alerts on an unhealthy child.** The router now
  watches for children that go bad and alerts the parent conductor on the same
  inbox + wake rail completions ride, via two mechanisms. Event-driven
  **stale-surface reconciliation**: on a tracked child's `surface.closed` (an
  accidental tab close or workspace teardown — anything outside `fleet
  rm`/`archive`) the registry row is immediately archived and the parent gets a
  `kind=stale` "revive?" alert (registry-integrity signal, quieter than a
  completion — no desktop banner). And a heartbeat **sweep** that once per tick
  emits a deduped `kind=doctor` alert on each **stall** (a bound `running` record
  frozen past a fresh window — a dead stream that fired no Stop), **low-context**
  (≤30% remaining), or **needs-input** child. Edge-triggered dedup plus healthy /
  conductor / muted skips prevent an alarm storm, and a deliberate `fleet
  rm`/`archive` writes a short-lived expected-close tombstone so an intentional
  retirement never reads as an accidental external close.

### Changed

- **`fleet rm` default flips to close + archive; launch refuses to overwrite a
  live label.** Bare `fleet rm <label>` now closes the surface and force-archives
  (SIGINT ladder + close-surface + force-archive) so removing a label can no
  longer silently abandon a still-live pane (the root of the ~40h book-keeper
  zombie). `--detach` is the explicit opt-in for the old drop-the-row-only
  behavior; `--kill` stays as an alias whose remaining job is worktree teardown;
  and a mid-turn (`running`) surface refuses without `--force`. Same failure
  family, other entry point: `fleet launch` now refuses to overwrite an
  already-live label (a clearly-stale row still relaunches freely; anything else
  fails closed, `--force` overrides). Recycled/revived ledger events now carry the
  same `effective` {model, effort, plugins} field that `launched` already did.

- **`fleet vitals` reports a real per-agent context window.** The
  context-remaining column is now derived per agent from its effective launched
  model's window (precedence: explicit `[1m]`/`[Nk]` flavor > fleet-declared
  window > keyword guess > 200k) instead of one static global — killing the false
  "over-full, recycle now" alarms a mixed-window fleet produced. The table also
  surfaces model / effort / cwd, and a transcript with no real usage record
  renders `—` instead of a garbage `0k 100%`.

### Fixed

- **Dead-agent recycle brick: `fleet recycle` is now pid-authoritative.** A self-recycle that
  left its seat DEAD with a hook-store record frozen at a non-terminal `agentLifecycle`
  (`running`) and a dead/`None` pid could never be recovered: the respawn-verify confirmed "old
  agent gone" ONLY via a terminal lifecycle string, which is SessionEnd-driven. But an abrupt
  death (SIGKILL) fires no SessionEnd, and even one that *does* fire can be clobbered by a cmux
  store-write race under load (the live 2026-07-05 incident), leaving the string frozen. Every
  recycle (even `--force`) then aborted forever ("old session still ALIVE"). The confirm is now
  **pid-authoritative**: a dead/`None` old pid is conclusive proof the agent is gone (a dead pid
  cannot host a TUI), with a pre-respawn live-pid snapshot as the safety floor, so if the original
  claude survives the respawn (wedged cmux) its pid is still alive and the verify correctly
  refuses, never typing into a live TUI. The quiet-gate is likewise pid-aware, so a frozen
  `running` ghost recovers on a plain `fleet recycle` (no `--force`). Grounded by a sandbox
  kill-mechanism matrix (SIGKILL freezes the record; SIGTERM/SIGINT×2/respawn-pane fire SessionEnd
  and clear it), so `recycle` now does a bounded graceful SIGINT×2 close before respawn to keep
  cmux state honest.

- **Pid-aware liveness at every "is-it-live" site.** A dead-pid ghost now reads gone everywhere,
  not just in recycle: all such checks route through a shared `state.surface_has_live_agent()`
  predicate (non-terminal lifecycle AND a live pid), so the pid, not the string, is the authority.
  `fleet ls` flags a dead-pid `running` ghost as `STALE` (was a false `live`); `fleet rm` no longer
  refuses a plain remove on a dead ghost (only a genuinely mid-turn agent is protected); `launch`'s
  overwrite-guard and `worktree clean` treat a dead ghost as gone; bulk-recycle skips it as stale;
  `register` won't bind onto it; and the wake gate never reads it busy. Adds `fleet unstick
  [label]` to reap a frozen dead-pid record without hand-editing cmux's hook store. Codex fires no
  SessionEnd at all (its record *always* lingers non-terminal after death), so this pid authority
  is what makes codex `ls`/recycle/reap honest — verified end-to-end against a live codex agent.

- **Recycle reliability — no more silent self-recycle failures.** Three gaps
  behind the ~9h-undetected-down incidents are closed: recycle now verifies the
  launch ENTER actually submitted the paste (re-kicking a bare Enter, never
  resending the text — resending on top of an unsubmitted draft was the
  doubled/tripled-draft failure) and verifies a fresh shell surfaced before
  firing the launch, falling back to a cmux-independent SIGINTx2 kill when an
  async respawn hangs past the settle; a "launch sent but no session bound" tail
  now ESCALATES (cmux notify + logged `recycle_abort`) instead of warning into
  the void; `--force` short-circuits the entire quiet-gate (a desynced/stale
  surface no longer burns the full 180s to an abort); and a roster role with no
  `--model` pinned anywhere now warns that it is riding the ambient default (the
  Sonnet-instead-of-Opus surprise). Bulk recycle prints the same per-agent
  resolved effort/model + warning as the single-target path.

- **Destructive-op + recovery-path safety.** Root-cause fixes for the
  workspace-group cascade-close incident: `fleet launch --resume <id>` no longer
  blind-kicks Enter into claude's resume-summary menu (it dismisses to "full
  session as-is" the same way `recycle` does, and aborts without registering on a
  resume-gate timeout rather than binding the lossy "resume from summary"
  default); `fleet rm --with-group` cross-checks the registry's belief about a
  group's membership against cmux's real membership and refuses on any
  disagreement (and lists what is about to die before deleting); `--kill`
  force-archives before teardown so a killed agent leaves a recovery trace; and
  every logged event now stamps an `invoker` so a destructive op's origin is
  reconstructable.

- **Cross-tool `Stop` no longer re-queues a stale completion.** A codex `Stop`
  resolving to a claude-typed registry entry (or vice versa) already refused to
  write the mismatched session id, but the router still fell through and
  re-delivered that entry's last-known completion on every such Stop — the ~80s
  ack-loop that hammered a conductor while Berg typed in the codex session. A
  tool mismatch now stops routing entirely.

- **`--plugins` unions onto a role launch.** `fleet launch <role> --plugins <p>`
  was gated behind `--adhoc` and silently dropped on a roster-role launch; it now
  unions unconditionally, aligning launch with recycle's `--add-plugin`.

- **Worktree base prefers local `main` over a stale `origin/main`.** In a
  local-merge dev flow (merges held local, never pushed) `origin/<default>` sits
  frozen for days, so a new fleet worktree silently branched off stale code; base
  resolution is now explicit > `<default>` > `origin/<default>` > HEAD.

## [0.3.1] - 2026-07-01

### Fixed

- **Moved-child completion routing (root cause #3, the v0.3.0 known issue).** When a
  child's Stop arrives but its hook-store `sessions{}` record has vanished — a running
  child whose surface was moved across workspaces loses its live session record,
  leaving only a frozen `activeSessionsBySurface` pointer — the router no longer
  silently drops the completion. It falls back to fleet-registry truth
  (`_member_by_session`, tool-aware + fail-open), recovers the member's surface +
  parent, and runs the normal queue/notify/wake path; a thin/empty gist is used if the
  cmux transcript is gone rather than dropping. Completes the notifications root-cause
  set (with the v0.3.0 stale-`running` wake-gate fix). Adds a router regression test.

## [0.3.0] - 2026-07-01

Lifecycle + notifications hardening. Two reviewed features (each went through a
cross-model adversarial review before merge).

### Added

- **Recycle/revive lifecycle hardening.** `fleet recycle` default flips
  **FRESH → RESUME** (the least-disruptive action; `--fresh` is the explicit
  context-shed, `--resume` kept as a no-op alias). `--session <id>` resumes an
  arbitrary prior session, and `fleet sessions <label>` lists resumable sessions
  so an operator can pick one. Registry↔session **reconciliation** (tool-aware:
  router Stop-time + bind-time + archive-time) keeps the recorded session honest
  against cmux's ground truth, killing the "No conversation found" class on
  archive/revive. **Bulk restart** selectors (`--all`/`--conductors`/`--children`/
  `--my-children`, sequential + gated, skips self + muted + non-live). Effort/model
  **provenance** on recycle/launch + first-class `--effort`/`--model` overrides.
- **Notifications: wake-now by default.** The idle-wake gate no longer trusts a
  stale/foreign `running` record — a bound-session + freshness cross-check plus the
  on-screen prompt as arbiter fixes the idle-conductor-never-woken stall. The
  `notify-mode` dial is demoted to a single **`passive` mute** (honored across
  completions, peer-msg, broadcast, and heartbeat); wake-now is the default. Adds a
  bounded event-driven wake **retry** (only when genuinely mid-turn at event time),
  **router self-health** (bus-consumption stamp + alive-but-wedged detection), and a
  **stale-draft-gate** (an abandoned draft is clobbered-with-log after it ages past a
  threshold, so a walked-away draft can't silence a conductor forever; active typing
  is preserved).

### Known issues

- **Moved-child completion routing (root cause #3):** moving an already-running
  child's surface across workspaces can desync its hook-store binding so its
  completion is dropped before it's queued. Operational guard: don't `move-surface`
  a running child (recycle to rebind). A router registry-fallback fix is landing as
  a fast-follow (v0.3.1).

## [0.2.0] - 2026-07-01

Packaging release. cmux-fleet is now an installable **uv tool** (`fleet` CLI + a
supervised, launchd-persistent router daemon) with a **thin, fail-open Claude
plugin** (hook shims resolve `fleet` on PATH; no baked app spec). Folds in the
daemon-hardening, resume-menu gate, and `fleet register` work landed since v0.1.0.
This is the version the live fleet was cut over to on 2026-07-01.

### Added

- **`fleet daemon start --foreground` for launchd/systemd (packaging P2.3).** Runs
  the supervised router in the current process — no fork/detach/stdio-redirect — so
  a supervisor's KeepAlive owns it directly and captures its output. Preserves the
  `daemon <start|stop|status|restart>` grammar (a parser test pins the exact plist
  command; a bare `fleet daemon --foreground` is rejected). `restart` still
  re-detaches. `fleet daemon status` now also reports **which build owns the
  daemon** — app `version`, the `python` running it, and the `cmux_fleet` package
  dir (recorded in `router.daemon.json` at start) — so a migration can prove the
  code path, not just state dir + pid (P2.5).
- **Two-step install + migration runbook (packaging P1.4/P2.5).** README quickstart
  is now two independent installs — `uv tool install` the app, then install the
  plugin your way (the app installer never touches the plugin). `docs/operations.md`
  gains a launchd reboot-persistence section (plist + bootstrap/kickstart/bootout)
  and an app/plugin **cutover runbook** written around real running-process
  behavior: it gates prod cutover on the Phase 3 thin shims, inventories each live
  agent's baked PATH/`--plugin-dir`/`CMUX_*`, forces an explicit
  repointable-path-vs-recycle decision, records current-vs-new daemon build
  identity + the `hash -r`/`rehash` rollback caveat + launchd ordering, and does
  NOT advertise "no conductor recycle" until a staging run proves a live conductor
  resolves the installed app for hook verbs.

- **Router daemon manager** (`scripts/fleet_daemon.py`). `fleet daemon
  start|stop|status|restart` runs `router.py --live` as a properly detached
  daemon: `start` double-forks with `setsid` (its own session, no controlling
  terminal) and leads its own process group, so the router survives the starting
  shell exiting, an agent Bash-tool process-group cleanup, and a conductor
  self-recycle (a bare `nohup &` router does not; it also risks surviving as a
  stray duplicate that double-processes the bus). Pidfile (supervisor pid), meta,
  and log under `$CMUX_STATE_DIR` (one set per state/profile); refuses to
  double-start, cleans a stale pidfile, and `stop` signals the whole process
  group (router included). `--heartbeat [SECS]` adds a Tier-1 tick (default 540s)
  that re-nudges only LIVE-IDLE conductors with a pending inbox through the
  input-safe `wake_if_idle` gate (skips busy/human-draft/muted/non-conductor);
  no dead-session detection or auto-recycle. `restart` preserves the running
  heartbeat setting unless overridden.

- **`fleet register <label> [--surface UUID] [--parent] [--session]`.** Manual
  escape hatch to pull a LIVE-but-unregistered agent into the registry — recovery
  for a skipped auto-register (see the resume gate below) or an agent launched
  outside fleet, for which no command previously existed. Derives
  tool/session/workspace/cwd from the live surface (cmux hook store) and rebuilds
  the spec from the roster role (toml-authoritative), falling back to the
  archive/live entry or the surface's own `AGENT_ROLE`/binding for off-roster
  agents; promotes a parked label to live, idempotent on the same surface, and
  refuses to move a label already live under a different surface.

### Changed

- **Conductor hooks are now thin, fail-open shims over `fleet hook-*` verbs
  (packaging P1.2/P1.3/P1.5).** The awareness/drain logic moved into the app as
  `fleet hook-awareness` (UserPromptSubmit) and `fleet hook-drain` (Stop), in
  `cmux_fleet/hookverbs.py`. The plugin's `scripts/hooks/{awareness,drain}.py` are
  now stdlib-only python shims (`scripts/hooks/_shim.py`) that shell into the
  installed `fleet` and forward its stdout ONLY on rc0 + valid expected-shape JSON;
  every other path (app missing, timeout, nonzero exit, stdout noise, wrong shape)
  fails open with blank stdout and exit 0. **The uvx network fallback was dropped:**
  the plugin requires the `fleet` app on PATH; a per-turn `uvx` in the hot path risked
  first-run/offline cost, private-repo auth latency, and the harness's 10s hook
  timeout killing the shim before it could fail open. Without the app, fleet hooks
  silently no-op and the rest of Claude Code is unaffected. **Version is now
  single-sourced** at `cmux_fleet/__init__.py::__version__` (pyproject reads it via
  `[tool.hatch.version]`); a test keeps plugin.json + marketplace.json in lockstep,
  and there is no hook-fallback pin to sync (one fewer version surface).

- **Agent helpers folded into `fleet` subcommands (packaging P2.1).** The four
  standalone plugin scripts — `scripts/{drive-child,child-digest,peer-msg,inbox-ack}.py`
  — are now `fleet drive-child` / `fleet child-digest` / `fleet peer-msg` /
  `fleet inbox-ack` (bodies in `cmux_fleet/helpers.py`, kept out of the 2k-line
  `cli.py` per P3.1). One app, one entrypoint: a conductor runs the verb via the
  `fleet` on PATH instead of shelling into a per-plugin script path. The
  awareness/drain hook context notes now emit the `fleet <verb>` forms. **Breaking:**
  the old `scripts/<helper>.py` paths are removed; any external caller that copied
  a script path must switch to the subcommand. A new static test
  (`tests/test_static.py::test_no_stale_helper_or_router_script_refs`) fails the
  release if any doc/skill/profile/README/hook still names a deleted
  `scripts/<helper>.py` or `scripts/router.py`.

### Fixed

- **Hook-shim hardening (codex P2-P4 re-review should-fixes).** `scripts/hooks/_shim.py`:
  (1) `$CMUX_FLEET_BIN` is now authoritative — set-but-invalid fails open blank
  instead of falling through to an ambient `which fleet`, so a strategy-A cutover
  can't silently run a stale binary off a live agent's baked `PATH`;
  (2) `CMUX_FLEET_HOOK_TIMEOUT` is clamped below the 10s harness timeout (max 9s;
  bad/oversized values ignored) so an override can't recreate the timed-out-hook
  failure; (3) stricter output validation — a non-string `additionalContext`/`reason`,
  or an awareness payload missing `hookEventName`, is treated as corrupt and blanked.
  Docs (`docs/profiles.md`, `docs/operations.md`) refreshed for the installed-app
  model (profile omits the marketplace pin unless explicit; `fleet` resolves from
  the installed app or a checkout shim).

- **`fleet profile` works from an installed wheel (packaging P1.1).** Phase 1's
  package move broke `fleet profile` for a `uv tool install`/venv install: it
  derived a checkout-style `PLUGIN_ROOT` by walking up from `cli.py`, so a wheel
  install emitted a nonexistent `site-packages/bin` on PATH, pointed
  `CMUX_FLEET_MARKETPLACE` at the Python lib dir, and silently skipped the
  `fleet.toml` seed. Now three concepts are resolved separately: the PATH pin
  comes from the actual invoked `fleet` (`$CMUX_FLEET_BIN` > `sys.argv[0]` >
  `which fleet`, checkout `bin/` only for a real plugin checkout); the
  marketplace pin is emitted only from explicit config or a real checkout (never
  inferred from a wheel's site-packages — omitted otherwise); and the seed roster
  is read via `importlib.resources` (force-included in the wheel), falling back to
  the repo-root example for a checkout. New `tests/test_packaging_smoke.py` builds
  a real wheel, installs it into a throwaway venv, and asserts the installed
  `fleet profile --init` pins the installed console-script dir, seeds the roster,
  and never emits a lib-dir path.

- **Router bus singleton guard (no more double-processing).** A stray
  `router.py --live` on the same bus double-processed every event (during the
  cutover, 3 strays triple-processed the bus and duplicate child completions
  reached conductors). The live router now acquires an exclusive, non-blocking
  `flock` on a per-state lockfile before consuming; a second live router that
  can't get it exits instead of processing in parallel. `fleet daemon start`
  reaps a stray live router holding the lock first (matched by lockfile pid + a
  `ps` cmdline check, so a pid-reused unrelated process is left alone).

- **Recycle/revive auto-resumes the FULL session.** `claude --resume` on an old
  or large session shows a summary-vs-full menu that hung an automated respawn
  and false-passed the confirm (the respawn keyed off a stale session while the
  menu blocked). The relaunch now auto-picks the full session and dismisses the
  menu, so recycle and revive resume complete context instead of stalling or
  silently compacting.

- **Resume-menu watch is event-driven and gates registration.** The dismiss used
  a fixed window that closed before a heavy loadout (e.g. 6 plugins, 30-40s boot)
  rendered the menu, so revive was left at the shell. It now polls for one of
  three states (menu / already-running / still-booting) under a generous,
  plugin-count-scaled ceiling. Crucially, the menu *gates* the session bind, so a
  timed-out dismiss used to fall through and skip `register()` — leaving the agent
  running but UNREGISTERED (a live pane still shown as archived). Revive and
  recycle now abort loudly on timeout instead of half-binding (revive before
  `archive_del`, so the label stays parked and re-runnable).

## [0.1.0] - 2026-06-30

Initial port. The native-cmux parent/child orchestration spine, extracted and
cleaned from an internal `cmux-conductor` plugin and decoupled from any single
vault or machine.

### Added

- **Git worktrees** (`scripts/worktree.py`, config-gated, default-off). A role
  with `worktree = true` (or `fleet launch ... --worktree`) runs each agent in
  its own worktree at `<repo>/.worktrees/<label>` on branch `fleet/<label>`. The
  fleet is the sole owner: it runs `git worktree add` itself and launches the
  tool into the directory (`claude` plain, no `-w`; codex via `cd`), strips
  Claude's `-w`/`--worktree` from passthrough, and never hooks
  `WorktreeCreate`/`WorktreeRemove`. Idempotent create under a per-repo lock;
  teardown refuses on a dirty tree (`--wip-commit` overrides) and always keeps
  the branch. New verbs `fleet worktree ls`/`clean <label>`; `fleet rm --kill`
  tears the tree down; post-launch placement reconciliation fails loud if a
  workspace collapsed off the worktree. Roster keys `worktree`/`worktree_base`/
  `worktree_dir`/`worktree_branch_prefix`; CLI `--worktree [BRANCH]`/
  `--no-worktree`/`--worktree-base`.
- **`fleet` CLI** (`scripts/fleet.py`, on PATH via `bin/fleet`). Tool-agnostic
  command builder over cmux primitives. Verbs: `launch`, `config`, `ls`,
  `archive`, `revive`, `recycle`, `broadcast`, `mute`/`unmute`, `rm`. Roster is
  role-first and tool-nested (`[defaults]`, `[tool.<t>]`, `[role.<name>]`,
  `[role.<name>.<t>]`); claude and codex adapters, with full `--` flag
  passthrough. Off-roster agents via `--adhoc`.
- **Router daemon** (`scripts/router.py`). One process serves every conductor.
  Listens on cmux's agent event bus, maps a child `Stop` to its parent via the
  live registry, and delivers a completion. Bus is the doorbell, cmux's hook
  store is truth, the transcript is content.
- **Unified, input-safe inbox** (`scripts/fleet_state.py`). One append-only
  stream folds child completions and peer messages, with per-surface, per-kind
  ack cursors. Completions and peer messages reach an agent through context, not
  its input box.
- **Two conductor hooks**. `awareness.py` (UserPromptSubmit) injects the pending
  inbox into context each turn. `drain.py` (Stop) auto-continues a turn to
  process pending work, gated by the mode dial.
- **Peer messaging** (`scripts/peer-msg.py`) and supporting helpers
  (`scripts/child-digest.py`, `scripts/inbox-ack.py`, `scripts/drive-child.py`).
- **`config.py` decoupling**. One path/setting resolver, precedence
  `env > [fleet] toml > XDG default`. Introduces `CMUX_FLEET_ROOT`; moves state
  under `$XDG_STATE_HOME` (`CMUX_STATE_DIR`); resolves the cmux binary via
  `which(cmux)` with a macOS app-bundle fallback (`CMUX_BIN`); makes the
  marketplace and floor `CLAUDE.md` optional (default off, no vault assumption).
- **Plugin packaging**. The repo is its own Claude Code marketplace
  (`marketplace.json` source `./`, strict), with `plugin.json`, `hooks.json`,
  and a conductor skill under `skills/`.
- **Fleet views** (`scripts/fleet_features.py`). Read-only, derived from live
  state every call (no daemon, no stored status). Status is inferred **without an
  LLM** (cmux `agentLifecycle` authoritative, refined by keyword tables).
  - `fleet vitals [--json] [--paint]`: cheapest-first triage table, most-urgent
    first, with each agent's **context-remaining %** (`!` flags <=30% left).
    Window configurable via `CMUX_FLEET_CONTEXT_WINDOW` / `[fleet].context_window`.
  - `fleet find <query> [--turns N] [--json]`: content-aware session lookup
    (label / role / cwd, or the agent's recent transcript).
  - `fleet graph [--html] [--out FILE]`: parentage tree (cycle-safe), text or a
    self-contained HTML page.
  - `fleet serve [--port N]`: thin read-only localhost view (graph HTML +
    `/vitals.json`); no daemon, no actions, no analytics.
  - `fleet paint`: native cmux sidebar telemetry: a status pill + context
    progress bar per workspace, on change only, additive.
  - Custom sidebar `sidebars/fleet.swift` (the cmux custom-sidebar mechanism) for
    a dedicated, tappable fleet board.
  - Unit tests in `tests/test_features.py`. Triage/no-LLM-status ideas adapted
    from agentmaster, the localhost-view shape from elevens (design-mined).
- **Test suite** (`tests/`, pytest-only, the one dev dependency). Three layers:
  static manifest/skill/hooks schema validators; pytest units for `fleet_state`
  transitions (inbox/registry/archive/dial), the hook stdin->exit-0 contract,
  `config.py` resolution precedence (`env > [fleet] toml > XDG`, dirname-anchor,
  malformed-warn), `scripts/worktree.py`, and the `fleet_features` views; and an
  e2e CLI lifecycle (`ls`/`archive`/`revive`/`rm` against a throwaway state with a
  stubbed cmux) plus a `claude --plugin-dir` load check. One skip expected (real
  claude load, skipped when no headless `claude` is present).

- **Multi-build isolation (profiles).** `fleet profile <name> [--base DIR] [--root DIR] [--init]`
  emits a sourceable env block that pins every entrypoint (the `fleet` CLI via PATH, `CMUX_STATE_DIR`,
  `CMUX_FLEET_TOML`, `CMUX_FLEET_ROOT`, `CMUX_FLEET_MARKETPLACE`, `CMUX_BIN`) at one build, so
  independent builds run side by side with no shared config, state, or daemons. The launcher now
  injects those same paths into every child it spawns (`_profile_env`), so a conductor and all its
  descendants, and their hooks, stay on one build regardless of a child shell's ambient env (the
  hermetic guarantee). Ships `profiles/test.fleet.toml` (a sandbox roster) and `docs/profiles.md`
  (the permanent dev workflow for standing up an Nth build).

- **Built-in workspace-group handling (one conductor = one group).** A `place = workspace` conductor
  now anchors its own cmux workspace-group instead of aborting when the group does not exist: the fleet
  creates it on the conductor's own new workspace via `workspace-group create --from <that workspace>`
  (always an explicit `--from`, never the caller-adopting implicit form). A conductor with no explicit
  `group` defaults it to its label; a `place = workspace` child joins its parent conductor's group.
  Group name->ref resolution is centralized (`_group_ref`) so teardown uses a ref as cmux requires.
  `recycle`/`revive` preserve the group; `fleet rm <label> --with-group` dissolves it and sweeps all of
  the group's members out of the registry (worktree branches are kept), while plain `rm` leaves members
  ungrouped. The sandbox profile is now turnkey (no manual `workspace-group create`).

### Fixed

- **Recycle relaunch is timing- and crash-safe.** A recycled agent relaunched
  into a fresh login shell whose `PATH` had not finished resolving the real
  binary, so the cmux wrapper exited 127 (`claude not found in PATH`); the
  fresh-mode confirm then matched a stale hook-store session id and reported
  success, priming a dead shell. Now the relaunch is `PATH`-guarded
  (`~/.local/bin` + homebrew prepended), the pre-relaunch session id is snapshotted
  and excluded from the fresh-mode confirm (a crashed launch resolves to "no
  session" instead of false success), the launch self-heals by re-firing once if
  no fresh session binds, and the post-respawn settle is 2s -> 3s.

[Unreleased]: https://github.com/seanyoungberg/cmux-fleet/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/seanyoungberg/cmux-fleet/compare/v0.3.1...v0.4.0
[0.1.0]: https://github.com/seanyoungberg/cmux-fleet/releases/tag/v0.1.0
