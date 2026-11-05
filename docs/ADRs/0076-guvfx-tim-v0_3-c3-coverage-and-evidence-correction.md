# ADR-0076 — GuvFX TIM v0.3 C3 Coverage & Evidence Correction (repository counterpart of GUVFX-TIM-ALIGN-006)

- **Status:** Accepted — repository-only, synthetic-only schema/validator/fixture/smoke/test correction
  (GFX-PKT-TIM-C3). Real acquisition, NAS changes, deployment, TIM-side code, training, operational signing
  keys and any trading remain **separately gated and NOT authorised** here. The PR carrying this change is
  opened with auto-merge OFF and is **not merged** without a new Sponsor authorisation.
- **Date:** 2026-11-05
- **Authoritative decision:** Notion *GUVFX-TIM-ALIGN-006* (the bounded corrective implementation the Sponsor
  authorised against the reconciled TIM-P5 section-K findings). Notion remains authoritative for the blueprint
  and owns lifecycle status; this ADR is the repository counterpart and does not advance any lifecycle state.
- **Supersedes nothing; preserves ADR-0070 and ADR-0073.** ADR-0070 (v0.2 foundation, GFX-PKT-TIM-C1) and
  ADR-0073 (v0.3 contract, GFX-PKT-TIM-C2) remain accepted and are **not** renumbered or overwritten. ADR-009
  (intelligence-vs-execution boundary) and ADR-0010 (GFX-PKT-006C market-data foundation) are unchanged.
- **ADR number reconciliation:** the TIM series lives in `docs/ADRs/` but shares one number space with
  `docs/architecture/`. The union maximum at authoring time is `0075` (`docs/architecture/ADR-0074`,
  `ADR-0075`); a naive `0073 -> 0074` would collide. Per the standing rule ("do not renumber or overwrite an
  existing ADR; add a tightly-scoped successor"), this correction is recorded as **ADR-0076**, chosen against
  both directories.

## Context

TIM-P5 (GUVFX-TIM-P5-001, a read-only consumer) accepted the v0.3 contracts merged by PR #611 (ADR-0073) and
then **correctly quarantined** GuvFX's own golden smoke release: it binds a 3-bar quality report to a
10,080-bar release, and the canonical validators never compared the quality evidence's scope to the release.
TIM raised nine producer-side findings (its §K). This ADR records the authorised, strictly-additive v0.3
correction for them. Everything is synthetic and repository-only.

## Decision

A **versioned additive v0.3 corrective revision**. Existing v0.3 schema/validator/fixture/test bytes change
where a finding requires it; the change is additive in intent (no capability removed, no validation weakened),
recorded here, and pinned afresh in `tests/test_tim_contract_v0_3.py` (`V03_SCHEMA_SHA256`). The frozen 006C v1
and v0.2 contracts (`research/market_data/contracts.py`, `tim_contracts.py`, `tim_fixtures.py`, and all
`research/contracts/*_v1`/`*_v2` top-level schemas) remain **byte-identical** and are now hash-guarded in the
v0.3 suite (`FROZEN_PY_SHA256`, Phase-9 test 20).

- **K-1 (HIGH) — quality coverage.** New contract `release_quality_coverage_v1` (schema + `validate_release_
  quality_coverage`) binds release id, canonical-file identity+digest, instrument, timeframe, first/last event
  timestamps, expected/observed/quality-assessed row counts, partition/file coverage, the quality/gap/watermark
  reference digests, and completeness. `quality_coverage` is a new **mandatory evidence role** in manifest v3.
  Two dependency-free cross-checks fail closed: `validate_release_coverage_consistency(manifest, coverage)`
  (identity/range/count must match the release; a row-count or range disagreement is a permanent REJECT, an
  honest coverage shortfall is a resolvable QUARANTINE) and `validate_quality_report_covers(coverage,
  quality_report)` (the bound v0.2 quality report must actually assess the attested rows — a 3-bar report
  under a 10,080-bar release quarantines as `incomplete_quality_coverage`). The coverage artefact is a member
  of the signed set, so it binds everything except the manifest digest (circular); the manifest digest stays
  bound by the READY marker and the signature envelope.
- **K-2 (MED) — timestamp consistency.** The canonical Parquet timestamp physical type is corrected to
  `TIMESTAMP(isAdjustedToUTC=false,unit=MICROS)` — what DuckDB actually writes. UTC is asserted by the contract
  and the `_utc` column naming, not by the Parquet flag. The descriptor adds `parquet_isadjustedtoutc=false`
  (enforced), a `time_semantics` block naming the seven distinct instants (market event, source availability,
  collector observation, ingestion, validation, publication, TIM import — the last consumer-side), and a
  `quality_flags_semantics` bitset definition (K-9). This is a documentation/descriptor correction to match the
  physical reality; it changes no release's data meaning, so it is a compatible correction (no new data format).
- **K-3 (MED) — formal evidence schemas.** New v0.3 schemas + validators for `timezone_evidence_v1`,
  `broker_server_identity_v1` and `licensing_v1` (previously ad-hoc objects). Each carries stable identity,
  version, issuer, observation/validity timestamps, an evidence digest and an explicit eligibility/verification
  status; ambiguous, missing, non-VERIFIED or inconsistent evidence quarantines the release. Synthetic only; no
  broker credential, personal data or operational key. The frozen 006C `broker_timezone_evidence_v1` is not
  edited.
- **K-4 (MED) — reason-code registry.** A versioned machine-readable `REASON_CODE_REGISTRY` (31 codes) with an
  explicit disposition per code; the receipt enum and validator derive strictly from it. The ACCEPTED /
  QUARANTINED / REJECTED distinction is never collapsed (quarantine is resolvable+retryable; reject is
  permanent). Added codes cover incomplete quality coverage, row-count / time-range / identity / timestamp-
  metadata mismatch, licensing ambiguity, broker-identity mismatch, duplicate JSON key, unsafe path,
  supersession loop, post-READY mutation, contract-invalid manifest, unknown predecessor and non-synthetic-in-
  synthetic.
- **K-5 (MED) — canonical path base.** The single base is the release root: every `relative_path`,
  `signature_path`, `ready_marker_path` and supersession ref must begin with `<release_id>/`. `REL_PATH_RE`
  already blocks absolute paths, backslashes and `.`/`..` segments and `_reject_control_strings` blocks control
  characters; C3 adds the release-root anchor and a case-collision guard. The pre-C3 share-relative
  `exchange/releases/<id>/` fixture base now fails closed and is preserved as a negative fixture.
- **K-6 (LOW) — receipt supersession.** `superseded_by_receipt_id` is always null on the immutable receipt;
  supersession is recorded forward by a new receipt. Documented in the schema; the field remains nullable.
- **K-7 (LOW) — signature binding.** `verify_release_signature_v2(..., manifest=None)` gains an additive
  cross-binding: when the parsed manifest is supplied it confirms the envelope's release id, both bundle
  digests, signing key and producer identity agree with the manifest (and that `signed_manifest_path` is
  `<release_id>/manifest.json`), returning False on disagreement. Default `manifest=None` preserves prior
  behaviour; `validate_manifest_signature_binding` is the explicit raising form.
- **K-8 (LOW) — duplicate JSON keys.** New `strict_json_loads_v0_3` rejects duplicate object keys and
  NaN/Infinity when parsing signed artefacts. The frozen 006C `strict_json_loads` is not modified.
- **K-9 (LOW) — quality_flags semantics.** The candle descriptor now defines the `quality_flags` uint32 bitset
  (0 == clean; named bits). The row validator accepts any uint32 and does not interpret the bits beyond this
  definition; per-bar quality-flag enforcement (e.g. reconciling a flagged-row count) is descriptive only in
  the pilot and is a recorded follow-up, not implemented here.

### Adversarial hardening (independent review round)

An independent 12-class adversarial review of this change confirmed two HIGH findings, both fixed here with
mutation-killing regression tests:

- **Physical row-count reconciliation (K-1).** The coverage checks compared self-declared counts to self-
  declared counts; nothing reconciled them with the physical canonical Parquet, so a producer could declare
  `row_count=3` over a real 10,080-row file and be ACCEPTED. `reconcile_physical_rowcount(manifest, coverage,
  physical_rows)` now asserts the physically-observed row count equals the manifest `row_count`, the
  canonical_parquet file-entry count and the coverage expected/observed/quality-assessed counts (disagreement
  is a permanent `row_count_mismatch` reject). The reference consumer recounts and re-describes the Parquet
  (no hard-coded `parquet_schema_verified`) and reconciles before accepting.
- **Evidence source-identity cross-check (K-3).** Evidence was hash-bound but its validators were never run by
  a consumer and its `source_id` was never checked against the release source, so another source's broker/
  timezone/licensing evidence could be substituted. `validate_release_evidence_identity(manifest,
  evidence_docs)` now runs each v0.3 evidence validator and rejects any evidence whose `source_id` differs from
  the release `source_id`. The VERIFIED-timezone check now consults the tz database (`zoneinfo`), not only a
  regex, so a non-existent IANA zone quarantines.

The reference consumer now derives its receipt disposition from the first failing check and that reason's
registry disposition — a permanent fault REJECTS, a coverage shortfall QUARANTINES — so the ACCEPTED /
QUARANTINED / REJECTED distinction is never collapsed. Remaining review findings were MEDIUM/LOW and are
recorded with their disposition in the evidence manifest; the composition of the coverage/evidence content
checks into a single manifest-level entrypoint, deep gap/watermark scope verification, and per-bar quality-flag
enforcement are recorded follow-ups (the manifest validator remains structural + hash-binding by design, with
content enforcement in the dependency-free consumer validators per ADR-0070).

## Consequences

- The pre-C3 golden release (now the committed negative fixture
  `tests/fixtures/market_data/tim/v0_3/negative/dataset_release_manifest_v3__incomplete_quality_coverage.json`)
  is rejected by the corrected validators — the intended behaviour. The corrected golden release (full-coverage
  quality evidence, release-root paths, formal evidence, quality-coverage attestation) is accepted. Partial
  releases remain prohibited.
- The v0.3 schema set grows from 7 to 11; the v0.3 suite grows from 32 to 52 tests (20 mandatory C3 regressions
  including the preserved negative fixture and the 006C/v0.2 byte-identity guards). The v0.2 suite (12) and both
  smokes stay green. The authoritative cross-field enforcement remains the dependency-free validators, not the
  JSON Schema alone (ADR-0070).
- **Not changed / not authorised:** no real data, NAS, broker, MT5, agent endpoint, acquisition service,
  deployment, TIM-side code, training, operational signing key or trading; the one-way intelligence→execution
  boundary and per-tenant isolation are untouched; no production mutation. Deep gap/watermark scope verification
  (beyond digest binding) and operational signing remain follow-ups outside this packet.
