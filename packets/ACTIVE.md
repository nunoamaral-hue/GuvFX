# Active Packet

- **Packet ID:** GFX-PKT-006C
- **Title:** Synthetic Infrastructure and Contract Implementation v0.1
- **Branch:** `chore/market-data-synthetic-foundation`
- **Base:** `main` @ `80ef2f85d6546b7d62aea09ab5db39d8859482b0`
- **Status:** In progress — synthetic-only implementation; PR open and unmerged for
  PM review. **No merge and no real-data/NAS/broker/agent access are authorised.**

## Scope

A fail-closed, **synthetic-only** market-data acquisition foundation in the
canonical repository, proving:
synthetic MT5 history response → strict contract validation → immutable raw landing
→ SHA-256/idempotency/quarantine → timezone publication gate →
`market_observation_v1` normalisation → temporary Parquet/DuckDB → `dataset_manifest_v1`.

- `research/market_data/` package (config, contracts, chunking, agent_client,
  storage, timezone, normalise, orchestrator).
- Four versioned contracts + four synthetic fixtures
  (`research/contracts/agent_history_export_*`, `raw_market_data_manifest_v1`,
  `broker_timezone_evidence_v1`).
- `GUVFX_DATA_ROOT` wired into backend settings with **no default**; backend
  `core` test proves it is `None` when unset.
- `tools/market_data_synthetic_smoke.py`, `tests/test_market_data_foundation.py`.
- `make market-data-check` + composed `research-check`; CI job
  `market-data-foundation`.
- Docs aligned; this pointer; evidence manifest
  `evidence/manifests/GFX-EVD-006C-synthetic-market-data-foundation.json`.

## Non-goals / Prohibited

- No live MT5/agent/VPS/broker/NAS/database/production access; no data acquisition.
- No agent-host endpoint implementation; no real Parquet/raw committed.
- No new dependency, Django app, model, migration, API endpoint or execution change.
- No edits to the three existing v1 contract schemas, requirements, Makefile `check`
  chain semantics beyond the new targets, or Flow A / other workstreams.
- No PR merge or deployment.

## Evidence

- Expected path: `evidence/manifests/GFX-EVD-006C-synthetic-market-data-foundation.json`
- Prior evidence manifests are not modified.

## Notion record

- Title: **GFX-PKT-006C — Synthetic Infrastructure and Contract Implementation v0.1**
  (Notion is authoritative for full text and lifecycle status.)
