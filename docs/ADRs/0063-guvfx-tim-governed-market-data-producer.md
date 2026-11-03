# ADR-0063 — GuvFX Publishes Governed Market Data; TIM Is a Read-Only Consumer (repository counterpart of GFX-ADR-TIM-001)

- **Status:** Accepted — repository-only schema/validator/fixture foundation (GFX-PKT-TIM-C1). Implementation of
  real acquisition, NAS changes, deployment, TIM-side code, training and operational signing keys remains
  **separately gated and NOT authorised** here.
- **Date:** 2026-11-03
- **Authoritative decision:** Notion GFX-ADR-TIM-001 and GUVFX-TIM Contract Definition v0.2 (Sponsor-approved
  2026-11-03). This ADR is the repository counterpart; Notion remains authoritative for the blueprint.
- **Preserves:** ADR-009 (the GuvFX intelligence-vs-execution boundary — "GuvFX creates intelligence; execution
  places orders") is unchanged and not redefined. ADR-0010 (market-data research foundation, GFX-PKT-006C) is
  the foundation this extends. The existing 006C v1 contracts are byte-identical (see the pinned hashes in
  `tests/test_tim_contract_v0_2.py` and the TIM evidence manifest).

## Decision

1. **Producer / consumer boundary.** GuvFX owns broker/MT5 market-data acquisition, immutable raw landing,
   validation, canonicalisation, dataset-release construction, signing and publication. TIM is a **read-only
   consumer** of completed, signed GuvFX dataset releases. TIM must never connect to MT5 or a broker, hold broker
   credentials, place or size an order, modify a GuvFX release, or change NAS permissions. No execution/order
   path exists in, or is reachable from, this foundation (enforced by an isolation test).

2. **Additive contract set (v1 byte-stable).** Twelve additive JSON Schemas under `research/contracts/`
   (`market_data_source_v1`, `canonical_instrument_v1`, `source_instrument_mapping_v1`,
   `agent_market_data_export_request_v2`, `agent_market_data_export_response_v2`, `raw_market_data_manifest_v2`,
   `acquisition_watermark_v1`, `market_data_gap_v1`, `market_data_quality_report_v1`,
   `dataset_release_manifest_v2`, `dataset_release_signature_v1`, `dataset_import_receipt_v1`). The v2 export
   contracts succeed the existing `agent_history_export_*_v1` (the 006C v1 request/response), which stay
   byte-identical — the v1 files are **not** renamed or modified. Dependency-free validators mirror the schemas
   in `research/market_data/tim_contracts.py`; the v1 validators in `contracts.py` are untouched.

3. **Immutable, signed release semantics.** A release becomes visible to TIM only when it is COMPLETE and a signed
   READY marker is written **last**. Partial releases are prohibited during the pilot (`partial_release_allowed`
   is a const `false`). The signing boundary is **injectable and synthetic** here: `dataset_release_signature_v1`
   carries an explicit `synthetic: true` and a `synthetic-placeholder-v0` algorithm. **No real/operational
   signing key or algorithm is created or chosen under this packet.**

4. **Quarantine / fail-closed.** Incomplete buckets, unresolved DST, invalid/ambiguous instrument mappings,
   missing licensing approval and un-certified source capacity quarantine or fail closed. Watermarks advance only
   after immutable landing + checksum verification + validation + quality acceptance. Gap repair creates **new**
   immutable evidence and never overwrites or invents market data.

5. **Native vs derived candle separation.** Broker-native and derived candles are **distinct identities and
   distinct releases**. A NATIVE release carries no derivation; a DERIVED release declares full lineage
   (`source_timeframe`, `derivation_version`, `calendar_id` + `calendar_version`, `bucket_policy`,
   `session_timezone`, `dst_policy`, `price_basis`). They must never be conflated even when OHLC values and
   intervals coincide.

6. **Event / observation / availability time.** Every record preserves the distinct instants
   `event_start_utc` / `event_end_utc` (broker event), `source_available_at_utc` (source availability),
   `collector_observed_at_utc` (GuvFX observation), `ingested_at_utc`, `validated_at_utc`, `published_at_utc`,
   normalised to canonical UTC. Intervals are half-open `[start,end)`. **Point-in-time research gates on
   availability (`published_at_utc`), not observation alone.** A documented, tested exception hook exists in the
   validator; none is required for the pilot.

## Authority boundaries

This ADR and GFX-PKT-TIM-C1 authorise **repository-only schemas, dependency-free validators, synthetic fixtures,
an offline synthetic smoke and CI** — and nothing else. They do **not** authorise: MT5 or broker access,
acquisition of real market data, NAS permission changes, production deployment, TIM-side implementation, training,
model promotion, operational signing keys, or any trading action. Merge and deployment are separately gated.

## Reconciliation of repository truth (recorded at build time)

- The packet's `agent_market_data_export_*` v1 names do **not** exist in the repo; the actual 006C v1
  request/response are `agent_history_export_request/response_v1`. The v2 export contracts are therefore new,
  additive successors; the v1 files are preserved byte-identical.
- "ADR-009" in this repo is the intelligence/execution boundary (there is no `0009` ADR file); the market-data
  foundation ADR is `0010`. Both are preserved.
- Base `main` at build time: `d92aca95abbb29cfeafc2b9c54d66df3a13192b3`. Private 006D reconciliation pin
  (`46c81057…`, per the contract) and the 006C v1 byte-lock are consistent with the live tree.

## Consequences

- The contracts and validators are inert, synthetic foundation: they describe and verify releases but acquire,
  publish and sign nothing real. The Synology ACL layout in the contract is documented for a later, separately
  authorised NAS assessment — no ACL is read or changed here.
- A later packet wires acquisition/publication, performs the NAS ACL assessment, and selects the operational
  signing key + algorithm (replacing the injected synthetic verifier), each under its own Sponsor authority.
