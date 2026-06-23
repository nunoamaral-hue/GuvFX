# Active Packet

- **Packet ID:** GFX-PKT-005B
- **Title:** EURUSD Contract and DuckDB Research Foundation v0.1
- **Branch:** `chore/eurusd-data-foundation`
- **Status:** Executed — CI verified; PM review pending. **No merge is authorised.**

## Scope

An Amber, additive research-foundation packet that creates the provider-independent
market-data schema foundation and a minimal, reproducible DuckDB research
environment, proven by a synthetic round trip locally and in CI before any real
data is acquired:

- `docs/ADRs/0010-market-data-research-foundation.md` — Accepted ADR for the
  DuckDB-only research foundation and versioned contracts.
- `requirements-research.txt` — pins exactly `duckdb==1.5.4`.
- `research/contracts/*.schema.json` — versioned JSON Schemas for market
  observations, broker costs, and dataset manifests.
- `research/README.md` — research environment, `GUVFX_DATA_ROOT`, logical zones.
- `tools/research_smoke.py` — deterministic synthetic quote/bar → Parquet → DuckDB
  round trip (stdlib + DuckDB only).
- `tests/test_research_foundation.py` — research unit tests (stdlib `unittest`).
- `Makefile` — separate `research-check` target (not in `check`).
- `.github/workflows/ci.yml` — separate `research-foundation` job.
- `docs/DATA_CONTRACTS.md`, `docs/NOTION_MAP.md` — current-state + Notion titles.
- this pointer + a new GFX-EVD-005B evidence manifest.

## Non-goals / Prohibited in this packet

- No real market-data download or provider research.
- No NAS, broker, MT5 runtime, account, database, or Notion access.
- No pandas/PyArrow/Polars/Jupyter or optional DuckDB extras; no pip upgrade; no
  global/system Python or package changes.
- No application/business-logic changes.
- No real data or Parquet artefact committed.
- No edit outside the authorised file list; `.venv-research/` is ignored, never
  staged.
- No PR merge or production deployment.

## Evidence

- Expected path: `evidence/manifests/GFX-EVD-005B-research-foundation.json`

## Notion record

- Title: **GFX-PKT-005B — EURUSD Contract and DuckDB Research Foundation v0.1**
  (Notion is authoritative for full text and lifecycle status.)
