# ADR-0060 — Ref-less requesting-lifecycle events open a withdrawal (advance-else-open)

- **Status:** Accepted (Amber). Narrow correlation-semantics change; over-claim-safe by construction.
- **Decision class:** **Amber** — it changes *which events open a withdrawal* in the ref-less heuristic path. It does
  NOT change classification, the external-evidence rule, dedup, the evidence-immutability invariant, or any Copy
  Trader / FX / erasure / audit-control state.
- **Relates:** completes the WV1 controlled-launch acceptance (Directive K/L). Builds on ADR-0059 (the genuine
  external withdrawal, event 2, is reclassified to `EXTERNAL_WITHDRAWAL`); this ADR lets the projection actually
  surface it.

## Context
The correlation engine's ref-less heuristic (`_correlate_by_heuristic`) only OPENS a withdrawal for
`WITHDRAWAL_REQUESTED` (`_OPENING_EVENTS`); every other lifecycle is treated as ADVANCING — it attaches to an
existing ref-less withdrawal or, finding none, stays `UNRESOLVED` (never fabricated). But TradersWay's genuine
external-withdrawal flow's **first observable step** is a `WITHDRAWAL_CONFIRMATION_REQUIRED` email ("Please confirm
your withdrawal request") — there is no separate prior `WITHDRAWAL_REQUESTED` email, and these events are commonly
ref-less. So a genuine, correctly-classified `EXTERNAL_WITHDRAWAL` confirmation-required event could never open a
withdrawal and therefore never appear on the member's page.

## Decision
In the ref-less heuristic's **zero-candidate** branch, a **requesting-family** lifecycle
(`WITHDRAWAL_CONFIRMATION_REQUIRED` / `WITHDRAWAL_CONFIRMED` — `_REQUEST_OPENING_FALLBACK`) OPENS a new withdrawal
(via `_create_heuristic`) instead of staying `UNRESOLVED`. This is **advance-else-open**:
- It is reached **only after finding ZERO matching candidates**, so it can never double-count a withdrawal that a
  `WITHDRAWAL_REQUESTED` (or an earlier confirmation) already opened — a later requesting event for the same
  withdrawal finds the existing record (same owner/account + amount + currency + window) and ADVANCES it.
- It is reached **only for an `EXTERNAL_WITHDRAWAL` event** (pass-1 effective-category filter + `correlate_event`
  guard) that carries **amount + currency** (the pre-existing never-fabricate guard above it).
- Every other lifecycle with no matching request (e.g. a lone `WITHDRAWAL_PROCESSING` / `COMPLETED`) still stays
  `UNRESOLVED` — unchanged, under-claim-safe.

## Consequences
- The genuine ref-less external withdrawal surfaces (one record), with status derived from its lifecycle
  (confirmation-required ⇒ in-flight / pending — never a fabricated completion or zero duration).
- **Over-claim-safe.** The only residual double-count vector is the pre-existing ref-less limitation (two genuinely
  distinct ref-less requesting events whose amount/currency/window do NOT match are two records) — this ADR adds no
  new inflation class; the advance-else-open ordering specifically prevents a confirmation from duplicating a
  request it should have advanced.
- No change to Copy Trader / FX / erasure / audit controls / dedup / classification / evidence immutability.
