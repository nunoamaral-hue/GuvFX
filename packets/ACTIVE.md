# Active Packet

- **Packet ID:** GFX-PKT-006C-R1 (continuation of GFX-PKT-006C)
- **Title:** Client and Raw-Integrity Remediation v0.1
- **Branch:** `chore/market-data-synthetic-foundation`
- **Base:** `main` @ `80ef2f85d6546b7d62aea09ab5db39d8859482b0`
- **Status:** Remediation in progress — synthetic-only; PR #34 remains **draft and
  unmerged** for PM review (not accepted). **No merge and no real-data/NAS/broker/
  agent access are authorised.**

## R1 remediation scope (closes five PM findings)

1. Actual standard-library HTTP history-export client (`agent_client.py`) — inert
   unless `allow_network=True`; injectable opener; exact POST/headers/body/timeout;
   one attempt; byte cap; redacted token/body in repr and all exceptions.
2. Strict JSON decode (reject invalid UTF-8/JSON and NaN/Infinity/-Infinity) +
   `math.isfinite` OHLC + schema-aligned type/length bounds (`contracts.py`,
   `timezone.py`, `orchestrator.py`).
3. Malformed bytes scanned for quoted prohibited JSON key tokens → digest-only
   `security_stop`, never persisting the body/value.
4. Exact idempotency/quarantine: `ALREADY_PRESENT` requires BOTH stored request and
   response SHA-256 and returns stored manifest values; deterministic quarantine
   identity over request+response+reason; canonical-request-byte guard; fail closed
   on corrupt accepted manifest (`storage.py`).
5. Exact authoritative Notion-map titles (`docs/NOTION_MAP.md`).

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
