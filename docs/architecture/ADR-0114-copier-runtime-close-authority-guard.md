# ADR-0114 — Copier runtime close-authority guard: no OPEN without a durable, armed MODIFY+FULL_CLOSE path, revocation-surviving containment, teardown flatness proof

- **Status:** Proposed (repository-only; DARK). PM owns lifecycle status in Notion.
- **Packet:** GFX-COPIER-RUNTIME-CLOSE-AUTHORITY-GUARD-149
- **Date:** 2026-11-16
- **Extends:** ADR-0113 (lifecycle-arming safety). Additive to ADR-0057 (per-pair LIVE OPEN), ADR-0083 (LIVE
  MODIFY/PARTIAL/FULL_CLOSE + containment), ADR-0086/0087 (mutation dispatch/transport), ADR-0097 (one-action arm).
- **Closes:** the residual risk recorded in ADR-0113 §Deferred and `docs/KNOWN_ISSUES.md`.

## Context

PR #676 (ADR-0113) closed the stranding risk at the ARMING layer (the atomic arm + the legacy `copier_live_arm`
authority guard), but its independent review found a residual RUNTIME gap: the single runtime LIVE-OPEN admission
`trade_copier.live_execution.admit_open_live` still admitted a follower OPEN without verifying that the position
had a durable, armed path to MODIFY and FULL_CLOSE. Concretely, `admit_open_live` checked the OPEN flag stack,
the authorization window/identity/symbol/watermark/sizing/arm — but **not** `modify_authority` /
`full_close_authority`, nor the per-op MODIFY/FULL_CLOSE execution arms, nor that the close survives the entry
window's expiry. A copied LIVE OPEN could therefore be left with no executable CLOSE — a **stranded real
position** — via the legacy arm path (flags set piecemeal) that bypasses the atomic arm.

Three distinct lifecycle hazards had to be closed:

1. **OPEN admitted without an executable close** (the headline gap).
2. **A revocation TRAPS an open position.** ADR-0083's deliberate default is that a REVOKED authorization governs
   nothing (`_governing_authorization` excludes revoked → the close path dies), so an operator who revokes while a
   position is open can strand it.
3. **Teardown could declare DARK over an open position.** The ADR-0113 teardown withdrew host admission
   unconditionally and never proved the follower was flat first.

## Decision

A fail-closed, DARK-by-default runtime guard with three parts; repository-only, migration-free.

### 1. Runtime OPEN close-authority guard (`open_close_lifecycle_armable`)

`admit_open_live` (and, defence-in-depth under the authorization lock, `create_live_open_job`) now refuses a LIVE
OPEN unless the bound position will stay closeable for its whole life. Two layers, both required:

- **Authority** (immutable on the authorization, so it cannot drift after OPEN): `modify_authority` AND
  `full_close_authority` present, partial-close ABSENT, and `close_only_after_expiry` set (so the position stays
  closeable once the entry window lapses).
- **Execution arm** (the flags the close/modify path needs to reach the broker): the per-op MODIFY and FULL_CLOSE
  arms AND `copier_mutation_dispatch_enabled` AND `copier_mutation_transport_enabled` AND
  `copier_live_close_only_after_expiry_enabled`.

Enforced in the ONE runtime admission, so BOTH the atomic arm and the legacy `copier_live_arm` fail closed. A
legacy OPEN-only authorization now fails closed for new OPENs. A flag flip AFTER the OPEN is defended by part 2
(containment) + part 3 (teardown flatness), not here. This purely adds deny paths — it widens nothing. Because the
whole copier LIVE subsystem is DARK in production (all flags OFF), there is no production behaviour change today.

### 2. Revocation-surviving FULL_CLOSE containment (opt-in, auto-bounded) — **Amber**

A new default-OFF arm `copier_live_contain_after_revocation_enabled` permits exactly one deliberately-chosen
exception so a revocation can never TRAP an open position: a FULL CLOSE (never a MODIFY, never a partial, never an
entry) may still CONTAIN an already-bound copier position after its OWN governing authorization was revoked,
provided that authorization carried `full_close_authority`. It is implemented in two coherent halves:

- **Copier admission** (`_resolve_governing_with_revocation_containment` in `live_execution`): for a FULL CLOSE
  only, under the flag, re-resolves the recorded governing authorization INCLUDING a revoked row, bypasses the
  window-revoked denial, and (like the ADR-0083 expiry / kill-switch containment exemptions) is arm-exempt by
  construction — a revocation disengages the arm, so no arm can exist in this state. Every other preamble gate
  (identity, symbol, verified ticket, reducing-only, kill-switch policy, full-close authority) still applies, and
  the under-lock relock mirrors it.
- **Runtime money-path boundary** (`copier_live_revocation_containment_authorized` in `execution.live_authz`,
  OR-ed into `allow_live_for_account`): a FOURTH, default-OFF, **tightly auto-bounded** branch that keeps
  `MT5_ALLOW_LIVE` on to FLATTEN such a position — ONLY while the arm is on, ONLY for a follower named by a
  recently-revoked full-close authorization whose pinned identity still matches, and ONLY while a
  copier-originated position is not yet proven flat. The instant the position is proven flat it returns False, so
  `MT5_ALLOW_LIVE` drops on the next render: the boundary is never left open after the stranded position is closed.

**This is an Amber change** — it touches the shared execution LIVE wall and changes what a revocation means for
`MT5_ALLOW_LIVE` (a revoked pair may keep the money path open, close-only, until flat). It is justified by the
packet's explicit requirement that the cleanup path survive revocation, and is bounded to be safe: default-OFF
(production byte-identical until a Sponsor arms it), FULL-CLOSE-only, exposure-reducing-only, identity/route
pinned, auto-closing once flat, and fail-closed on any error. It widens no entry/modify/partial authority.

### 3. Teardown flatness proof (`_open_copier_positions`)

`teardown_live_dark` now PROVES no copier-originated LIVE follower OPEN remains unflattened BEFORE any mutation
that would withdraw the money/close path. The proof is conservative backend evidence — three unflattened classes,
all over-reporting rather than under-reporting:

- a LIVE OPEN that reached execution (REQUESTED/VERIFIED) with no VERIFIED copier FULL CLOSE of its ticket;
- a LIVE OPEN not yet reconciled to a ticket (its fate is unproven);
- **a LIVE `PLACE_ORDER` reported FAILED that may still have FILLED** (the lost-ACK case `execution.execution_health`
  Control 5 defends, with its `FAILED_FILLED_LOOKBACK_SECONDS` window). The copier reconciler never revisits an
  `EXECUTION_FAILED` rep and such a rep has no `follower_ticket`, so it could otherwise be silently counted flat
  while the real position is open — the exact stranding this packet exists to prevent. Within the ambiguity window
  it is counted NOT flat; beyond it the execution_health backstop has run its course and it is treated as resolved.
  (Adversarial-review HIGH, fixed in this packet.)

If flatness cannot be proven, teardown STOPS in an explicit `cleanup_required` (non-DARK) state having changed
NOTHING — the arm stays engaged, the authorization stays active and host admission stays on — so the position can
still be flattened (for a reconciled ticket, through the close path; for a lost-ACK order, via an
identity-firewalled position read); the operator resolves it and re-runs teardown. The DARK verdict folds in this
proof (replacing the ADR-0113 blunt `follower_tickets==0` check, which a cleanly-closed position's retained ticket
row could never satisfy). The runtime boundary predicate (part 2) consumes the SAME proof and fails CLOSED on a
flatness DB error — `MT5_ALLOW_LIVE` is never authorized under uncertainty.

## Consequences

- No LIVE follower OPEN is admitted (or produced under the lock) unless the authorized MODIFY and FULL_CLOSE
  lifecycle is durable and armed; a copied OPEN can never be stranded with no executable close.
- A revocation can never trap an open position: with the opt-in arm the position is auto-flattened by a FULL CLOSE
  only, and the runtime boundary closes the instant it is flat. With the arm OFF (default) behaviour is unchanged.
- Teardown can never declare DARK over a possibly-open position; it halts in a reversible cleanup-required state.
- Repository-only, DARK: no flag/key/admission/host mutation in this packet; a real arm/teardown remains a
  separate, Sponsor-gated step.

## Blast radius (authorized)

The runtime guard applies to EVERY live-copier OPEN, so the ADR-0057/0094 OPEN test fixtures that used
OPEN-only authorizations (no modify/full-close authority, close path not armed) now correctly fail closed and were
updated to the complete, lifecycle-armed authorization (`tests_live_copier`, `tests_immediate_dispatch`,
`tests_synthetic_e2e_latency`). This is the Amber fixture change ADR-0113 deferred, now authorized by packet 149.
