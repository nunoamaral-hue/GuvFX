# ADR-0092 — Staff-gated manual withdrawal correlation (broker-neutral, reference-less)

- Status: Accepted (repository-only; DARK — command gated, not applied in production)
- Date: 2026-11-10
- Packet: GFX-WITHDRAWAL-STAFF-CORRELATION-024-GO-001
- Extends: ADR-0079 (Stream B withdrawal lifecycle), ADR-0089 (unknown-sender discovery), ADR-0090 (Vantage
  reference-first correlation + fail-closed `requires_reference`)

> **Reconciliation note (GFX-WITHDRAWAL-PR637-RECONCILE-053):** this decision was originally filed as **ADR-0091**.
> While PR #637 was open, PR #636 merged to `main` first and claimed ADR-0091
> (`docs/ADRs/0091-hosted-observation-concurrency-budget-and-transport-taxonomy.md`). During branch reconciliation
> this decision was renumbered **0091 → 0092** (the next free number) to satisfy the programme-wide ADR-uniqueness
> check. Only the number changed — the decision, scope and behaviour are unchanged; PR #636's ADR-0091 is not altered.

## Context

ADR-0090's correlation engine merges a broker's withdrawal-lifecycle emails deterministically by a **stable broker
reference** (reference-first; a bounded amount+currency+time heuristic only for already-referenced brokers). Packet
023 established, from a PII-safe fixture of the two genuine Vantage emails, that **some brokers supply no stable
transaction reference at all** — one email is a bare "request successful" confirmation with no detail fields, so
there is not even a shared amount. Automatic correlation is therefore impossible without guessing, and guessing on
amount / currency / date / account hints would risk a double-count or a wrong merge. The correct state is a
**fail-closed hold**.

But a human reviewer, looking at the two candidates, can legitimately conclude they are one withdrawal lifecycle.
We need a way to act on that human judgement **without** (a) writing a broker-specific parser for every referenceless
broker, (b) weakening the automatic correlation policy, or (c) ever inferring completion.

## Decision

A **broker-neutral, staff-gated, append-only manual-correlation exception path**.

- New append-only model **`StaffWithdrawalCorrelation`**: owner (tenant) · explicitly-selected `broker_profile` ·
  M2M membership of discovery candidates · the candidates' salted provider-message-id hashes in trusted-timestamp
  order · `lifecycle_result` (always `REQUESTED`) · staff `reviewer` · sanitised `reason` · deterministic
  `idempotency_key` (unique) · nullable `resulting_withdrawal` · `status` (PROPOSED/APPLIED/REJECTED/SUPERSEDED).
  Write-once (`save()`/`delete()` refuse rewrites); a correction is a NEW decision — the audit is never destroyed.
- New `Withdrawal.CorrelationMethod.MANUAL_STAFF` and `UnknownSenderDiscoveryCandidate.ReviewState.CORRELATED`
  (additive choices; migration 0031, reversible, no data change).
- A dry-run-by-default staff command `correlate_withdrawal_candidates` + a pure service
  (`staff_correlation.plan_correlation` / `apply_correlation`).

### Why staff correlation is an exception path, not broker-specific development
The mechanism keys on the **generic** discovery-candidate attributes (owner, auth-approved registrable domain,
lifecycle class, trusted timestamp, message hashes) — nothing Vantage-specific. Any broker whose emails lack a stable
reference uses the identical command; no new backend code is needed per broker.

### Explicit broker selection (the critical binding rule)
The candidates' `associated_broker_profile` is NULL and MUST NOT be inferred or back-filled. The reviewer passes an
explicit `--broker-profile-id`; the service validates the profile is **active + operator-verified** and that **every
candidate's own auth-approved registrable domain is a sender domain of that profile** (fail-closed broker
mis-attribution guard), and refuses a candidate already bound to a different profile. It never mutates a candidate's
broker association as a side effect of refusal.

### Lifecycle truthfulness
The result is **always `REQUESTED` / in progress**. "Withdrawal Request Successful" (or any acceptance wording) is
NOT completion; `completed_at` stays NULL and no processing duration is published. A candidate whose own lifecycle
class is terminal (COMPLETED/CANCELLED/FAILED/REJECTED/EXPIRED) is refused — completion belongs to the automatic
evidence path, never a manual REQUESTED merge.

### Fail-closed, idempotent, concurrency-safe
Any conflicting recorded stable reference / amount / currency, cross-tenant or cross-broker membership, a non-staff
reviewer, an ineligible or already-correlated candidate, an ambiguous timestamp, or a duplicate candidate id refuses
the operation. `--apply` is required to mutate; it creates **exactly one** reference-less, owner-scoped `REQUESTED`
withdrawal under a row lock, links full provenance to both candidates (and their evidence), and marks the candidates
`CORRELATED`. The deterministic `idempotency_key` plus its unique constraint make a replay — and a concurrent
double-apply — a safe no-op (the loser's transaction rolls back; no duplicate withdrawal).

### Privacy
The decision stores only owner/broker FKs, the candidates' **salted** message-id hashes, the staff actor, a
sanitised bounded reason and enums — never a raw body, name, full account number, or other PII. Amount/currency are
used only when consistently present in already-redacted, already-approved evidence columns.

## Consequences

- A referenceless broker's confirmed lifecycle can surface as one truthful in-progress withdrawal, by explicit human
  decision, with a permanent audit — without weakening automatic correlation or writing a per-broker parser.
- The automatic reference-first / heuristic paths and existing brokers (Pepperstone, TradersWay, IS6) are unchanged;
  the manual withdrawal is reference-less and (unless consistently evidenced) amount-less, so the automatic heuristic
  cannot later match it.
- Future: when a broker begins supplying a stable reference, the automatic engine handles it and the manual path is
  not needed; the manual decisions remain as historical audit.
- This ADR/packet is repository-only: the command is **not applied in production** and is **not** run against the
  real Vantage candidates here.
