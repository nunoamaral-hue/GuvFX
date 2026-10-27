# ADR-0062 — Reviewed instrument-mapping controls: discovery approval vs execution authority

- Status: Accepted (DARK; repository-only; no production persistence)
- Date: 2026-10-27
- Workstream: GuvFX Copy Trading — multi-broker instrument matrix
- Supersedes/amends: extends ADR for the `InstrumentMapping` discovery matrix (PR #582); does **not** change
  `CopierSymbolMap` execution semantics.

## Context

The Copy Trading controlled rollout reached the point of persisting the non-Pepperstone directional
mappings. A Sponsor packet required persisting **only reviewer-approved SAFE-EXACT** candidates, each with a
**named reviewer and review evidence**, and keeping REVIEW-REQUIRED / REJECTED candidates unpersisted.

The deployed `InstrumentMapping` model and `refresh_instrument_mappings` command could not honour that
contract (documented as a Phase-B STOP):

1. `InstrumentMapping` had no reviewer-identity / review-status field.
2. The governed command persisted the **entire** directional candidate set (every classification), not a
   SAFE-EXACT-only subset.
3. By design the command never self-marks `VERIFIED` (that is reflected only from an execution-authoritative
   `CopierSymbolMap`), so "approve a SAFE-EXACT mapping" had no representation.

The STOP was accepted as correct. This ADR records the governed fix.

## Decision

Add an explicit **human discovery-approval layer** to `InstrumentMapping`, kept strictly separate from the
machine classification and from execution authority:

- `review_status` ∈ {`UNREVIEWED`, `APPROVED`, `REJECTED`} (default `UNREVIEWED`).
- `reviewed_by` (durable operator identity), `reviewed_at`, `review_evidence_ref` (bounded, non-secret
  provenance), optional `review_note`.

Add a governed **SAFE-EXACT-only** mode to `refresh_instrument_mappings`
(`--safe-exact-only`, `--reviewer`, `--evidence`, `--review-note`) that persists **only** candidates passing
the deterministic predicate `matrix.is_safe_exact`, each stamped `review_status=APPROVED`.

### The SAFE-EXACT predicate (deterministic, fail-closed)

`is_safe_exact(candidate)` is true iff ALL hold:

- `classification == UNVERIFIED` — which already guarantees a single tradable follower match whose canonical
  identity matches and whose HARD dimensions (quote currency, settlement currency, contract type) are
  compatible, and excludes VERIFIED / AMBIGUOUS / STALE / INELIGIBLE / ABSENT;
- `compatibility_warnings` is empty — no SOFT material warning (e.g. contract_size or margin_currency
  difference), so economic exposure matches exactly;
- `identity_confidence == HIGH`;
- a non-empty `follower_symbol`.

### Separation invariant (the core of this ADR)

**Discovery approval is NOT execution authorization.** `review_status=APPROVED`:

- does **not** change `classification` (stays `UNVERIFIED` until an execution-authoritative `CopierSymbolMap`
  exists), and does **not** set `verified_via_symbol_map`;
- does **not** create, update or delete any `CopierSymbolMap`;
- does **not** make the row executable — the copier execution path does **not** read `InstrumentMapping` at
  all (it resolves follower symbols via `symbols.resolve_follower_symbol` / `CopierSymbolMap`).

Execution authority continues to require the existing `CopierSymbolMap` verified gate and the five-layer LIVE
wall, untouched by this change.

## Consequences

- A reviewer can durably approve clean SAFE-EXACT discovery mappings for the audit matrix without granting
  any execution capability, and REVIEW-REQUIRED / REJECTED candidates are never persisted by this mode.
- Re-running is idempotent (upsert on the native directional pair); directions remain additive and
  independent; self-pairs and cross-tenant pairs are refused; dry-run is inert; commit fails closed without a
  named reviewer and an evidence reference.
- The change is additive (migration `trade_copier/0007`, five nullable/defaulted columns) and reverses
  cleanly. No production persistence is authorized by this ADR; a separate Sponsor GO governs persisting the
  twelve reviewed non-Pepperstone directions. Pepperstone #49 remains MATERIAL-RISK and excluded.
