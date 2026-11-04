# ADR-0073 — GuvFX TIM v0.3 Canonical Contract Correction (repository counterpart of GUVFX-TIM-ALIGN-004 / v0.3-RC1)

- **Status:** Accepted — repository-only schema/validator/fixture/smoke correction (GFX-PKT-TIM-C2). Real
  acquisition, NAS changes, deployment, TIM-side code, training, operational signing keys and any trading
  remain **separately gated and NOT authorised** here.
- **Date:** 2026-11-04
- **Authoritative decision:** Notion *GUVFX-TIM Contract Reconciliation v0.3-RC1 — G1–G9* (GUVFX-TIM-ALIGN-004),
  marker `GUVFX_TIM_ALIGN_004_RECONCILED — V0_3_RC1_PROPOSED`. Notion remains authoritative for the blueprint;
  this ADR is the repository counterpart.
- **Supersedes nothing; preserves ADR-0070.** ADR-0070 (GFX-PKT-TIM-C1) remains the accepted v0.2 foundation and
  is **not** renumbered or overwritten. ADR-009 (intelligence-vs-execution boundary) and ADR-0010 (market-data
  research foundation, GFX-PKT-006C) are unchanged.
- **ADR number reconciliation:** ALIGN-004 proposed "ADR-0072", but `0072` was taken by the live read-only
  positions gate (PR #610) after ALIGN-004 was written. Per the packet ("do not renumber or overwrite an
  existing ADR; add a tightly-scoped successor"), this correction is recorded as **ADR-0073**.

## Context — why v0.3 is required

The v0.2 foundation (ADR-0070, `26a2f6f0`) is a valid synthetic contract foundation but is **not ready for a
persistent TIM consumer**. The v0.2 reconciliation found: the smoke emitted NDJSON as the data payload (not
canonical Parquet); it wrote TIM receipts inside the release directory after READY; the v2 manifest lacked
sufficient signed, hashed references for source eligibility, mapping, timezone, licensing, gap, watermark and
validator evidence; and ADR-0070's publication-time wording did not keep source availability and GuvFX
publication distinct. Persistent TIM P5 is blocked pending this correction.

## Decision (G1–G9)

1. **G1 — Canonical format is Parquet.** Canonical analytical candle payloads are Parquet
   (`canonical_candles_parquet_v1`): `event_start_utc`/`event_end_utc` as UTC-microsecond timestamps; OHLC as
   `DECIMAL(38,18)`; `tick_volume` `INT64`; `quality_flags` `UINT32`; optional `real_volume`/`spread_points`/
   `source_available_at_utc`/`source_sequence`. Compressed NDJSON is permitted **only** as optional raw
   evidence; an **NDJSON-only canonical release is rejected** (not import-eligible). The uniqueness key is
   `event_start_utc`; rows are strictly increasing, half-open `[start,end)`.
2. **G2 — Immutability.** STAGING is mutable and not consumable; `READY` permanently closes the release
   namespace; publication order is stage → payload/evidence → final manifest → detached signature → **READY
   last**. The detached signature and the READY marker are **external** artefacts (not members of the signed
   `files[]`), which avoids a circular digest. **TIM never writes into a GuvFX release;** receipts live in a
   separate consumer-owned namespace keyed to the immutable release/manifest/signature identities.
3. **G3 — Provenance/eligibility.** The release binds source eligibility, broker/server identity, approved
   instrument mapping, timezone evidence, licensing/permitted-use, quality, gap and watermark evidence by
   contract version, path and SHA-256. Ambiguous/missing/contradictory evidence quarantines. No real
   broker-licensing assertion is invented; synthetic eligibility is explicitly `synthetic`/`non_operational`.
4. **G4 — Point-in-time.** Event, source-availability, collector-observation, ingestion, validation,
   GuvFX-publication and TIM-import instants are kept **separate**. Historical source-side research gates on
   evidenced source availability; GuvFX-published-intelligence research gates on publication (and TIM import).
   Unknown source availability is `UNKNOWN_RETROSPECTIVE` — retrospective-only, no point-in-time claim.
5. **G5 — Supersession.** Corrections create a **new immutable release** with an explicit supersession chain
   (reason + scope); nothing is overwritten or deleted. A release may not supersede itself (loop guard).
6. **G6 — Evidence hashes.** Quality, gap, watermark, eligibility, identity, mapping, timezone and licensing
   evidence are mandatory hash-bound references, each backed by a release file of the matching role + digest.
7. **G7 — Receipts.** `ACCEPTED` (research-admissible), `QUARANTINED` (retained, potentially resolvable, not
   admissible, retryable), `REJECTED` (permanent integrity/security/unsupported-major, not admissible, not
   retryable). Receipts are consumer-owned and immutable; a new receipt may supersede an old one.
8. **G8 — Validator pinning.** The producer validator id/version/digest is signed with the release; the
   consumer records its own independently-approved validator id/version/digest in the receipt; unsupported
   versions fail closed.
9. **G9 — Signature envelope v2.** An algorithm-agile detached envelope binds a domain separator, release id,
   manifest digest, contract-bundle digest, validator-bundle digest, producer identity and signing-key
   identity. READY binds the manifest and signature-envelope digests. The signing boundary is an **injected
   synthetic placeholder** (`synthetic-placeholder-v0`, `synthetic: true`); **no real/operational signing key or
   algorithm is created or chosen**.

## Compatibility and transition

- **v0.2 status:** retained and **byte-identical** for synthetic compatibility; its tests/smoke are unchanged
  and still pass. v0.3 is **strictly additive** — new schemas live under `research/contracts/v0_3/`, new
  modules (`tim_contracts_v0_3.py`, `tim_fixtures_v0_3.py`, `tim_release_v0_3_synthetic_smoke.py`) and a new
  test (`test_tim_contract_v0_3.py`); no existing research/contract/test file is modified.
- **v0.3 status:** the canonical contract a persistent TIM consumer implements. The temporary v0.2 compatibility
  adapter is **deprecated** by this canonical contract (v0.2 remains readable for synthetic compatibility only).
- **006C v1 preservation:** all 006C v1 contracts remain byte-identical (pinned SHA-256 in
  `tests/test_tim_contract_v0_3.py` and the C2 evidence manifest).

## Ownership and boundaries

- **GuvFX (producer):** owns acquisition, immutable landing, validation, canonicalisation, release construction,
  signing and publication. This packet delivers only the repository schemas/validators/fixtures/smoke/CI.
- **TIM (consumer):** read-only consumer of completed, signed releases; validates Parquet/signature/evidence,
  writes receipts **outside** releases, enforces quarantine/rejection and the point-in-time gates. TIM-side
  implementation is **not** authorised here.
- **One-way publication boundary; no trading/execution coupling.** No execution/order path exists in, or is
  reachable from, these contracts (enforced by an isolation test). TIM never connects to MT5 or a broker, holds
  credentials, places/sizes an order, modifies a GuvFX release, or changes NAS permissions.

## Authority boundaries

GFX-PKT-TIM-C2 authorises **repository-only** schemas, dependency-free validators, synthetic fixtures, a genuine
synthetic Parquet smoke and CI — and nothing else. It does **not** authorise MT5/broker access, real market-data
acquisition, NAS permission changes, production deployment, TIM-side implementation, training, model promotion,
operational signing keys, or any trading action. Merge and deployment are separately gated; no production deploy
follows this merge.
