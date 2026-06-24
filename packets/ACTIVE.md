# Active Packet

- **Packet ID:** GFX-PKT-006C-R4 (continuation of GFX-PKT-006C / R1 / R2 / R3)
- **Title:** Post-Merge Exact-Time and Quarantine Provenance Closure v0.3
- **Branch:** `fix/market-data-r4-closure`
- **Base:** `main` @ `6dfe22aef7d6bc1a82cbd712536989c10e342602`
- **Status:** Synthetic-only forward remediation. The R3 synthetic foundation is on
  `main` at/after `6dfe22a…`; R4 closes the remaining post-merge exact-time and
  quarantine-provenance gaps. **Lifecycle and merge status are authoritative in
  Notion** (PM-owned). **No merge, deployment, or real-data/NAS/broker/agent/
  credential access is authorised by this packet.**

## R4 remediation scope (exact time + quarantine provenance)

1. One shared arbitrary-precision UTC-instant primitive (`contracts.py`) parses and
   compares canonical `Z` timestamps preserving every admitted fractional digit
   (no float; exact comparison to integer epoch seconds); used by the timezone
   gate (`timezone.py`), research point-in-time ordering (`tools/research_smoke.py`)
   and manifest timestamps (`storage.py`).
2. Every ordinary quarantine is bound to its exact parsed/validated stored request
   (`storage.py`): identity, range and directory derive from the request, and the
   16-hex quarantine id is recomputed from exact request bytes, response bytes and
   reason. Canonical request bytes are required except for the explicit
   `noncanonical_request_bytes` attempt, which stays quarantine/conflict evidence
   and never becomes accepted/idempotent.
3. Malformed and contract-invalid responses remain retainable as immutable evidence
   and are never validated as a success response.
4. `publish_observations` validates the request first, so invalid request input
   fails through governed `ContractError`/`PublicationError` (never raw
   `KeyError`/`TypeError`) and yields no records.

## R3 remediation scope (semantic time + provenance)

1. Exact-instant timezone semantics (`timezone.py`) — UTC parser raises governed
   `ContractError` on impossible calendar/time; coverage compares aware UTC
   `datetime` instants (no integer-epoch truncation); half-open preserved.
2. Semantic manifest timestamps (`storage.py`) — all four UTC fields parsed,
   `range_end > range_start` as instants, `timeframe = M1`, source/account ≤64,
   `request_schema_id`/`response_schema_id` equal the canonical v1 IDs.
3. Manifest provenance binding (`storage.py`) — ACCEPTED manifests are bound to the
   exact stored request/response (canonical request bytes, `validate_request`/
   `validate_response`/match, identity + field + derived-directory equality);
   quarantine directories bound to manifest identity; any independent field change
   fails closed.
4. Publication raw-lineage binding (`normalise.publish_observations`) — requires
   the exact response bytes; `strict_json_loads(bytes) == response`,
   `response_sha256 == sha256(bytes)`, `raw_object_id == request_id`, before the
   gate/mapper.
5. Historical evidence byte discipline — R1 evidence restored to its parent form
   with only the additive `superseded_by_r2_evidence` pointer; R2 gains only an
   additive `superseded_by_r3_evidence` pointer.

The R1/R2 scopes remain in force. The frozen `synthetic_timezone_verified.json`
fixture stays unchanged (negative coverage test); positive evidence is in-memory.

## R2 remediation scope (closes eight findings)

1. Non-bypassable timezone-gated publication API (`normalise.publish_observations`)
   — requires evidence, validates request/response/match and a `VERIFIED`,
   bar-covering timezone assessment before any record; private `_map_bid_ohlc`
   mapper; no output on gate failure.
2. Timezone runtime bounds — source/account type-checked + ≤64; `evidence_method`/
   `dst_behaviour` non-empty ≤256 (`timezone.py`).
3. Timezone offset arithmetic — `server_clock - utc_clock == implied_offset`.
4. Timezone observation coverage — each `observed_at_utc` within `[covered_start,
   covered_end)`.
5. Raw-manifest `rel_path` schema rejects absolute/backslash/empty-segment/`.`/`..`/
   leading-dot segments (`raw_market_data_manifest_v1.schema.json`).
6. Strict runtime manifest validation before every write and on every read: exact
   field set, path safety, paths tied to the expected object dir, raw-object/dir
   identity, and stored request/response files verified by exact SHA-256; fails
   closed on any tamper/missing/unsafe state (`storage.py`).
7. Concurrency-safe staging — unique `mkdtemp` per attempt, self-clean only,
   foreign staging preserved; late identical race → `ALREADY_PRESENT`, late
   conflict → quarantine; no overwrite/partial/loss (`storage.py`).
8. HTTP client hardening — positive-int `max_response_bytes`, reject URL userinfo,
   redact status/read I/O errors, always close the response (`agent_client.py`).

The R1 five-finding scope is retained; R2 completes the remaining publication and
persisted-evidence boundaries. The pre-existing `synthetic_timezone_verified.json`
fixture (observation outside coverage) is **retained unchanged** and is now used as
a negative coverage test; positive VERIFIED cases build evidence in-memory.

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
