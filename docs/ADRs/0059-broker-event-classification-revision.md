# ADR-0059 — Append-only BrokerEvent classification revision (effective category)

- **Status:** Accepted (Amber). Mechanism is additive + DARK-safe; it changes nothing until the
  `reclassify_broker_event` command is run against an explicit bounded target.
- **Decision class:** **Amber** — it introduces a second input to *what the withdrawal projection treats as an
  external withdrawal*. It does **not** change the classifier, the evidence model's immutability, dedup, or any
  Copy Trader / FX / erasure / audit-control state.
- **Relates:** closes the WV1 controlled-launch acceptance gap diagnosed under Directive K — a genuine external
  withdrawal (`EvidenceBlob` event ingested under the older parser) is immutably stored `UNKNOWN` and cannot be
  refreshed in place (`BrokerEvent` is append-only: only `correlation_status` is mutable; `delete()` forbidden;
  `UNIQUE(owner, evidence)` blocks a corrected re-ingest). Does not reclassify the genuine internal transfers
  (the `/withdrawal/confirmInner` + source→destination + no-external-rail events) — those stay `INTERNAL_TRANSFER`.

## Context
The deterministic TradersWay classifier is **correct**: a message is `EXTERNAL_WITHDRAWAL` only on positive
structured evidence that money left the broker (a whitelisted `Payment method:` rail + withdrawal direction, no
internal transfer-clause, no veto). A genuine external-withdrawal request was ingested **before** that certified
external path was deployed to production, so its immutable `transaction_category` is a stale `UNKNOWN`. The
append-only evidence invariant (data.md) means the stored category is never edited and the message cannot be
re-ingested into a corrected event. We need to surface the genuine withdrawal **without** mutating or deleting any
evidence, and **without** reclassifying legitimate internal transfers (the forbidden over-claim, Sponsor §8).

## Decision
Introduce a write-once **`BrokerEventClassificationRevision`** linked to the original `BrokerEvent`:

- Fields: `event`, `derived_category`, `original_category` (audit snapshot), `original_parser_version`,
  corrected `parser_name`/`parser_version`, `classification_rule_version`, `reason` code, `source_evidence_sha256`,
  `actor`, `supersedes` (self FK), a deterministic unique `idempotency_key`, `created_at`. Write-once (no update)
  and never deleted — **DB-enforced** by BEFORE-UPDATE / BEFORE-DELETE triggers (migration 0024), exactly like the
  evidence it corrects, so `QuerySet.update()` / `QuerySet.delete()` / raw SQL cannot silently flip the effective
  category (over-claim) or erase a correction — not app-layer (`save()`/`delete()`) only.
- **Effective category** = the latest valid revision's `derived_category`, else the original immutable
  `transaction_category` (`BrokerEvent.effective_transaction_category()`; deterministic = newest revision by id).
- The **withdrawal projection consumes the effective category only through the correlation engine** (a batch job):
  `run_correlation` selects pass-1 external-withdrawal events by an annotated effective category, and
  `correlate_event`'s guard uses it. `metrics.py` is unchanged — it still projects the correlated `Withdrawal`
  records (every `Withdrawal` is still `EXTERNAL_WITHDRAWAL`-only by construction). **No per-page reclassify;**
  nothing is recomputed on a read.
- A revision is **only ever parser-derived** by the `reclassify_broker_event` command on an explicit bounded
  `(owner, event)` target: re-read the immutable evidence (integrity-verified), re-run the current certified
  parser, and append a revision **only** when the derived category differs from the effective one. There is **no**
  operator-supplied category override. Fail-closed on owner / evidence-hash / parser-provenance / scope mismatch;
  atomic; idempotent (the `derived == effective` short-circuit + the unique `idempotency_key`); dry-run supported.
  It prints counts and safe hashes only — never an email body, confirmation URL, token, full account or request
  number.

## Consequences
- The genuine external withdrawal (bounded, one event) surfaces after a single `reclassify_broker_event` run +
  correlation, with the original event, its stored category, and the evidence all intact and auditable.
- The correction is fully attributable and reversible-by-supersession (a later revision supersedes an earlier one);
  the history is never rewritten.
- Internal transfers are untouched (the parser still derives `INTERNAL_TRANSFER` for them; no revision is appended).
- No change to Copy Trader, FX, erasure, audit controls, dedup, or the evidence-immutability invariant.
- **Known limitation (no retraction).** The withdrawal projection is monotonic / positive-evidence-only: a later
  revision that flips an *already-correlated* event to a non-external category does NOT retract the `Withdrawal`
  already created from it (correlation pass 1 only reprocesses UNRESOLVED/AMBIGUOUS events). The intended and
  exercised use is `UNKNOWN → EXTERNAL_WITHDRAWAL` surfacing; retracting a surfaced withdrawal is out of scope and
  not claimed here.

## Alternatives rejected
- **Mutate the stored category** — violates the append-only evidence invariant (and the DB trigger forbids it).
- **Delete + re-ingest** — `BrokerEvent.delete()` is forbidden; `UNIQUE(owner, evidence)` blocks a second event.
- **Reclassify at read time in the projection** — would recompute on every page load, risks drift, and makes the
  member surface depend on live parser behaviour; rejected for a durable, auditable, batch-correlated projection.
- **Reclassify the `/confirmInner` internal transfers as withdrawals** — the forbidden over-claim; the evidence
  (source→destination clause, no external rail, internal confirm path) establishes them as internal.
