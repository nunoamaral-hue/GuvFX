# ADR-0126 — account-less withdrawal correlation fails closed on a same-owner reference collision

- **Status:** Accepted (repository-only; additive bug fix to existing correlation logic; migration-free; no production
  deploy, no flag, no schema/DB change, no broker/MT5/host contact).
- **Packet:** GFX-WITHDRAWAL-REFCOLLISION-FAIL-CLOSED-169 (Withdrawal Intelligence / correlation correctness).
- **Date:** 2026-11-24.
- **Relates to / amends:** ADR-0050 (owner-attributed, account-optional withdrawals) and ADR-0079 (Stream B neutral
  advance). Closes filed backlog item (c) in `docs/NEXT.md`.

## Context

`broker_intelligence.correlation._find_by_reference` resolves the single `Withdrawal` an event's broker reference
belongs to. Events for ONE real withdrawal can differ in account resolution (the request may resolve a GuvFX account
while a follow-up/completion carries only the shared reference — ADR-0050), so the finder bridges on the owner.

One owner can legitimately hold **two** withdrawals that share a `broker_reference_id` across **different** accounts:
`uniq_withdrawal_account_reference` is a **per-account** partial-unique constraint, so the same reference may exist on
account A1 and account A2. The account-RESOLVED bridge already guards against this — it bridges only to an
account-UNRESOLVED (`trading_account IS NULL`) sibling, never to a different account's row.

The **account-LESS** bridge did not. It ran `Withdrawal.objects.filter(owner_id, broker_reference_id=ref).first()` and
advanced an **arbitrary** matching row. So an account-less COMPLETION (the genuine payout email carries no account and
no rail wording — category UNKNOWN) whose reference collided across the owner's accounts advanced the **wrong**
withdrawal to COMPLETED (filed backlog item (c), LOW). The same unconstrained bridge served the neutral-advance pass
(ADR-0079), which had the identical exposure.

## Decision

Apply the module's own **"ambiguous ⇒ never guess"** rule (already used by the account-resolved bridge and by the
ref-less heuristic adopt, which requires `len(cands) == 1`) to the account-less bridge, in two scoped parts:

1. **`_find_by_reference` — fail closed on ambiguity.** The account-less bridge fetches up to two `(owner, ref)` rows
   and returns the row **only when exactly one** matches, else `None`:
   `bridged = list(Withdrawal.objects.filter(owner_id, broker_reference_id=ref)[:2]); return bridged[0] if len(bridged)==1 else None`.
   The account-less strict key `(owner, trading_account IS NULL, ref)` is tried first (and is unique via
   `uniq_withdrawal_owner_reference`), so the bridge is reached only when no account-NULL sibling exists — i.e. the
   candidates are account-resolved rows, and `≥2` of them is a genuine cross-account collision. The two **advance-only**
   passes (`correlate_completion_event`, `correlate_neutral_advance_event`) then return `OUT_UNRESOLVED` on `None`:
   they never fabricate and never advance, and the event stays re-evaluable (not marked CORRELATED).

2. **`_correlate_by_reference` (the create-capable pass 1) — AMBIGUOUS, never fabricate.** Because the hardened finder
   is shared, pass 1 (which *creates* a withdrawal when the finder returns `None`) would otherwise fabricate a phantom
   account-NULL withdrawal for an account-less `EXTERNAL_WITHDRAWAL` event on a collision — inflating the count. A guard
   after the `None` return marks such an ambiguous account-less collision **AMBIGUOUS** and returns `OUT_AMBIGUOUS`,
   never reaching the create/adopt path — mirroring `_correlate_by_heuristic`'s existing `len(cands) >= 2 ⇒ AMBIGUOUS`
   behaviour. A `count == 0` (no match) account-less event still creates its normal account-UNRESOLVED withdrawal
   (ADR-0050); a `count == 1` single match is returned by the finder and advanced as before.

The account-RESOLVED bridge, cross-owner isolation, idempotency, and all zero/single-match behaviour are unchanged.

## Consequences

- An account-less completion / neutral-advance / external-request event whose reference ambiguously collides across one
  owner's accounts no longer advances an arbitrary withdrawal, and no longer fabricates a phantom row. It is left
  UNRESOLVED (advance-only passes) or AMBIGUOUS (create-capable pass 1), re-evaluable as more evidence accrues — the
  module's documented under-claim-safe bias.
- Metric inflation remains structurally impossible: neither path can create or mis-advance on an ambiguous account-less
  reference.
- Repository-only, migration-free: no flag, schema, DB, or host change. The correlation engine remains DARK (no live
  caller enabled by this packet).

## Adversarial review and known limitations

A multi-lens adversarial review (fail-closed correctness; pass-1 create/double-count regression; advance-only
downstream safety) was run against part 1 alone. Two lenses were CLEAN; the **pass-1** lens surfaced a **confirmed
MEDIUM** — the shared fail-closed finder made the create-capable pass 1 fabricate a phantom account-NULL withdrawal for
an account-less `EXTERNAL_WITHDRAWAL` collision (empirically reproduced: post-fix rows = 3 / created = 1 vs pre-fix
rows = 2). Part 2 (the AMBIGUOUS guard) was added to resolve it, with a regression test asserting the row count stays 2,
the event is marked AMBIGUOUS (not CORRELATED), and re-runs stay idempotent. Residual: the real correlation engine is
operator/DARK-gated and not deployed by this packet; the fix is proven by `OwnerBridgeIsolation` (6 tests) plus the
broader correlation suite. The pre-existing broker-profile *migration* tests are environment-gated (they need the
broker-profile data-migration seed) and are unaffected by this correlation-logic change.
