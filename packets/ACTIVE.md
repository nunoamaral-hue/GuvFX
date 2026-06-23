# Active Packet

- **Packet ID:** GFX-PKT-005B-R2 (continuation of GFX-PKT-005B / R1)
- **Title:** UTC Semantic and Nullable-Field Remediation v0.1
- **Branch:** `chore/eurusd-data-foundation`
- **Status:** Remediated — CI verified; PM review pending. **No merge is authorised.**

## Scope

A Green, bounded final foundation correction on the existing PR #33 branch,
addressing the point-in-time defect raised in PM review:

- `tools/research_smoke.py` — `_parse_utc` now parses canonical `Z` timestamps into
  timezone-aware UTC `datetime` instants (rejecting impossible calendar/time values
  and non-UTC representations); `availability_time_utc >= observation_time_utc` is
  compared as instants, not lexicographically, so fractional-second ordering is
  correct. Original timestamp strings are preserved in records and Parquet.
- `tools/research_smoke.py` — synthetic inputs now include a quote with null
  `source_time_utc`/`received_time_utc` and omitted bid/ask sizes, and a bar with
  omitted volume/unit; insertion uses null for absent optional columns and the
  field comparison treats an absent optional source field and a null read-back as
  equivalent. Required common fields remain mandatory.
- `tests/test_research_foundation.py` — tests for invalid calendar/time values,
  fractional-second ordering both directions, equal instants, and null round-trip
  of the optional timestamp/size/volume fields.
- `research/README.md`, `docs/DATA_CONTRACTS.md`, `docs/NOTION_MAP.md` — docs.
- this pointer + a new GFX-EVD-005B-R2 evidence manifest.

## Non-goals / Prohibited in this packet

- No schema, dependency, requirements, ADR, Makefile, or CI workflow change.
- No real market data or committed Parquet artefact.
- No provider, broker, account, NAS, MT5 runtime, database, or production access.
- No application code change.
- No branch switch, fetch, rebase, reset, force push, or PR merge.

## Evidence

- Expected path: `evidence/manifests/GFX-EVD-005B-R2-utc-nullability.json`
- Prior evidence manifests (005B, 005B-R1) are not modified.

## Notion record

- Title: **GFX-PKT-005B-R2 — UTC Semantic and Nullable-Field Remediation v0.1**
  (Notion is authoritative for full text and lifecycle status.)
