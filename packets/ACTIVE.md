# Active Packet

- **Packet ID:** GFX-PKT-005B-R1 (continuation of GFX-PKT-005B)
- **Title:** Full-Lineage Parquet and Manifest Remediation v0.1
- **Branch:** `chore/eurusd-data-foundation`
- **Status:** Remediation in progress. **No merge is authorised.**

## Scope

A Green, bounded contract-integrity remediation on the existing PR #33 branch,
addressing the four areas raised in PM review:

- `tools/research_smoke.py` — the synthetic Parquet round trip now preserves the
  **full** versioned observation contract (every required common field, all five
  point-in-time timestamps, raw lineage, `quality_flags`, and the populated
  quote/bar variant fields); reconstructed records are re-validated and compared
  field-by-field to the source.
- `tools/research_smoke.py` — emits **separate** quote (`interval: event`) and bar
  (`interval: M1`) dataset manifests with distinct dataset IDs, each referencing
  only its own raw objects, checksum and counts (no combined `interval: M1`
  manifest that mischaracterises quotes).
- `research/contracts/market_observation_v1.schema.json` + the local validator —
  quote rows reject bar-only fields, bar rows reject quote-only fields, and unknown
  fields are rejected; stricter common-field type checks.
- `research/contracts/dataset_manifest_v1.schema.json` — adds required
  `record_type` (enum quote|bar), `source_objects` `minItems: 1`,
  `content_checksums` `minProperties: 1`, and a SHA-256 pattern on `config_hash`.
- `tests/test_research_foundation.py` — tests for all of the above.
- `research/README.md`, `docs/DATA_CONTRACTS.md`, `docs/NOTION_MAP.md` — docs.
- this pointer + a new GFX-EVD-005B-R1 evidence manifest.

## Non-goals / Prohibited in this packet

- No real market data or committed Parquet artefact.
- No new/updated dependency or package installation.
- No broker-cost schema expansion.
- No provider, broker, account, NAS, MT5 runtime, database, or production access.
- No application, Makefile, ADR, requirements, or CI workflow changes.
- No branch switch, fetch, rebase, reset, force push, or PR merge.

## Evidence

- Expected path: `evidence/manifests/GFX-EVD-005B-R1-contract-integrity.json`
- The original `evidence/manifests/GFX-EVD-005B-research-foundation.json` is not
  modified.

## Notion record

- Title: **GFX-PKT-005B-R1 — Full-Lineage Parquet and Manifest Remediation v0.1**
  (Notion is authoritative for full text and lifecycle status.)
