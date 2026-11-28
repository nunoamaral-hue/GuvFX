# ADR-0130 — Session-aware market-data releases (TIM v0.4)

- **Status:** Proposed (repository-only; no real-data signing, NAS publication, or host change under this packet)
- **Date:** 2026-11-28
- **Supersedes / relates to:** ADR-0127 (GO #2d release CLI), ADR-0124 (v3 signing envelope), ADR-0070
  (producer-signs / TIM-verifies boundary). Additive over the v0.3 contract (ADR-0073/0076).

## Context

The first real-data pilot (IS6FX account #50, EURUSD.std M1) reached release assembly and surfaced a concrete
limitation of the v0.3 release contract: it assumes a **continuous** series.

- `market_data_quality_report_v1` requires `expected_bar_count == (range_end − range_start) / timeframe` —
  i.e. every calendar minute is expected. Real FX closes on weekends, so observed (35,997) ≠ calendar
  minutes (~46,200) and the report cannot pass without declaring ~10,200 benign weekend minutes as defects.
- `market_data_gap_v1` models a **single DEFECT** gap (`DECLARED | REPAIRED | UNREPAIRABLE`) with a mandatory
  repair for `REPAIRED`. A real slice has **zero defect gaps** but several benign gaps (weekend closures,
  no-tick minutes); there is no truthful single-record representation, and inventing "defect" gaps for benign
  closures is forbidden by the evidence rules.

7 of 9 evidence roles completed truthfully for the real slice; these two could not. The gap is architectural,
not operational: the contract does not model real-market session structure.

## Decision

Introduce a **v0.4 session-aware release contract**, additive over v0.3. v0.3 releases
(`contract_version == "0.3"`) keep validating under the **frozen** v0.3 rules; v0.4 releases
(`contract_version == "0.4"`) add:

1. **Pinned, source-specific trading-session calendar** (`research/market_data/tim_session_calendar.py`).
   Self-contained and dependency-free: timezone/DST are declared as **explicit, dated UTC-offset rules** (no
   `zoneinfo`/`tzdata`), plus per-weekday local sessions, fully-closed **holidays**, and
   **exceptional/partial** sessions. The calendar is content-addressed (`calendar_digest`).
2. **Expected bars derived from the calendar**, not from raw calendar minutes.
3. **Gap classification kept distinct** (`research/market_data/tim_contracts_v0_4.py`): a gap **set**
   (`gap_set_v1`) with zero or many entries, each classified `SCHEDULED_CLOSURE` (never recorded — it is just
   "closed"), `VERIFIED_NO_TICK`, `UNEXPLAINED`, or the preserved defect states. **An unexplained in-session
   miss stays visible and drives the quality verdict** (`PASS` iff zero unexplained).
4. **Session coverage** (`session_quality_coverage_v2`): expected / observed-in-session / scheduled-closure /
   verified-no-tick / unexplained kept as distinct counts; fail-closed arithmetic + verdict.
5. **Calendar bound into the signed manifest** (`session_calendar.{calendar_id, calendar_digest, source_id}`)
   via a version-normalised reuse of the **frozen** v3 manifest validator (schema `3.1`, contract `0.4`).
6. **Consumer independently recomputes** (`research/market_data/tim_session_consumer.py`, duckdb-free): it
   re-derives the expected count, the observed-in-session count and the exact UNEXPLAINED set from the
   **signed raw NDJSON** (the actual bars) and the **pinned calendar**, then checks them against the coverage
   and gap evidence. A hidden dropped in-session bar — even with a re-hashed, re-signed manifest — is caught
   by `coverage_recompute_mismatch`, beyond the file-hash/signature checks.

The producer builder (`tools/tim_session_release_builder.py`) emits v0.4 candidates; the GO #2d CLL
(`assemble`/`publish`/`validate`) is wired to seal a v0.4 READY marker and dispatch the v0.4 consumer. The
producer-signs / TIM-verifies boundary (ADR-0070) is unchanged: this packet adds **no signing primitive**.

## Consequences

- **Backward compatible.** Existing v0.3 releases verify unchanged (dispatch by `contract_version`). Proven by
  the retained v0.3 test suites staying green and a back-compat test here.
- The legacy v0.2 `quality_report` role stays **per-continuous-range** and is retained for defect counts
  (dup/overlap/OHLC); the **session coverage** (`quality_coverage` role) is the authoritative session-aware
  completeness artifact for v0.4. Making `quality_report` itself session-aware is a scoped follow-up.
- `release_completeness` (manifest) remains the **file-set/range** axis (always COMPLETE, no partial releases);
  the **data-quality** verdict (gaps) is a distinct axis carried by the session coverage.
- JSON Schema files for the v0.4 artifacts (`research/contracts/v0_4/`) are a follow-up; the Python validators
  are the authoritative, tested enforcement in this packet.
- **Before the first real v0.4 signing**, a real, source-specific IS6FX session calendar (true trading hours,
  holiday list, exceptional sessions, and the EET/EEST offset rules for the window) must be obtained and
  pinned — this is the one piece of source-calendar evidence still outstanding for the #50 release.

## Scope boundary

Repository-only. No real-data signing, NAS publication, forward-test fitting, or live-host change. The
disposable Ed25519 test signer lives only in the test (ADR-0070). The preserved IS6FX #50 artifacts, the
R1/R2 study receipts, the burned hold-out, and the frozen forward-test preregistration are untouched.
