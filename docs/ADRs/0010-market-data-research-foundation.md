# 0010 — Market-data / Research Foundation

- Date: 2026-06-23
- Status: Accepted

> Accepted under packet **GFX-PKT-005B — EURUSD Contract and DuckDB Research
> Foundation v0.1**. Establishes the provider-independent market-data schema
> foundation and a minimal, reproducible DuckDB research environment, proven by a
> synthetic round trip before any real data is acquired.

## Context

GuvFX needs a research/data foundation before downloading any real EURUSD market
data. `docs/DATA_CONTRACTS.md` already carries a **Candidate** market-data
contract and **Proposed** point-in-time requirements, but no schema files, no
research runtime, and no reproducible round-trip harness exist. We want to fix
the *shape* of observations, broker costs and dataset manifests, and prove a
quote/bar → Parquet → query loop works locally and in CI — without committing to
a provider, broker, account, NAS path, granularity or date interval, and without
introducing heavy dataframe machinery.

## Verified facts

- A Candidate market-data contract and Proposed point-in-time platform are
  documented in `docs/DATA_CONTRACTS.md`; no ingestion store of that shape exists
  in the repository.
- The repository's governance/evidence layer (rules, secret scanner, evidence
  manifests, active-packet pointer) is present on `main` (`docs/STATUS.md`).
- The local default `python3` is CPython 3.14.x (recorded in the GFX-EVD-005B
  evidence manifest).
- DuckDB 1.5.4 installs as a binary-only wheel for CPython 3.14 with no transitive
  dependencies (verified in this packet's isolated `.venv-research`).

## Assumptions

- A single-file analytical engine (DuckDB) reading/writing Parquet is sufficient
  for the initial research workload; this is assumed, not yet measured against a
  real dataset.
- Parquet is an adequate columnar format for derived/curated analytical files;
  assumed from common practice, revisited if a concrete need contradicts it.

## Decision drivers

- Reversibility and small blast radius (governance Amber, no real data).
- Reproducibility: a derived artefact must be rebuildable from raw + config.
- Point-in-time correctness must be expressible in the schema from day one.
- Provider/broker/account independence — no premature lock-in.
- Simplicity: the fewest dependencies that prove the loop (`.claude/rules/architecture.md`).

## Options considered

- **Option A — DuckDB-only + Parquet + versioned JSON Schemas (chosen).** One
  binary dependency, standard-library smoke/tests, schemas independent of any
  provider. Minimal, reversible, CI-friendly.
- **Option B — pandas/PyArrow/Polars stack now.** Richer tooling, but multiple
  heavy dependencies and build-from-source risk before any measured need; rejected
  under `.claude/rules/architecture.md` ("no speculative infrastructure").
- **Option C — PostgreSQL as the raw market-data store now.** Reuses the app DB
  engine, but conflates application data with a research/raw store and commits to
  storage decisions that are still Open; deferred.

## Decision

Adopt a provider-independent market-data schema foundation plus a minimal DuckDB
research environment:

- **DuckDB 1.5.4 is the only initial third-party research dependency**, installed
  binary-only into an **isolated `.venv-research`** built from the local **Python
  3.14** interpreter. No pip upgrade; no pandas/PyArrow/Polars/Jupyter/pytest or
  optional DuckDB extras until a **measured need** justifies an approved change.
- **Parquet** is the format for derived/curated analytical files. **Raw source
  objects remain immutable and live outside Git.**
- The future real-data root is referenced by the **`GUVFX_DATA_ROOT`** environment
  variable, with **no repository-path default**; this packet uses **synthetic
  temporary data only** and writes nothing persistent.
- Three **versioned JSON Schemas** (`market_observation_v1`, `broker_cost_v1`,
  `dataset_manifest_v1`) fix the observation/cost/manifest shapes, carrying source
  and time lineage so point-in-time correctness is expressible.
- **Provider, broker, account, NAS path, granularity and date interval remain
  Open** and are not chosen here.

## Consequences

- Positive: a reproducible, low-dependency research loop exists and is CI-gated; a
  stable contract shape is available for future ingestion; no real-data or
  trading capability is added.
- Negative / follow-ups: schemas are not yet exercised against real data; a local
  validator (in `tools/research_smoke.py`) stands in for full JSON-Schema
  validation since no validator library is installed; storage backend and provider
  decisions remain Open and block ingestion.

## Risks and controls

- **Scope creep into real data (Amber/Red).** Control: synthetic-only data,
  `GUVFX_DATA_ROOT` has no repo default, `.gitignore` excludes `/.venv-research/`
  and `/research-data/`, and ingestion is explicitly out of scope.
- **Dependency sprawl.** Control: `requirements-research.txt` pins exactly
  `duckdb==1.5.4`; adding any library requires an approved decision.
- **Hidden absolute paths in evidence/output.** Control: smoke output and manifest
  use logical references only; a test asserts no personal absolute path appears.

## Evidence / validation

- `.venv-research/bin/python tools/research_smoke.py` — synthetic quote/bar →
  Parquet → DuckDB round trip returns `status: PASS` with deterministic
  aggregates.
- `.venv-research/bin/python -m unittest discover -s tests -p
  'test_research_foundation.py' -v` — 16 tests pass (schemas parse, required
  fields, quote/bar distinctness, lineage/timestamp/bid-ask/high-low/availability
  rejections, round trip, determinism, no-absolute-path, no heavy libs, no
  residual files).
- `make governance-check` — secret scan + governance unit tests pass.
- Full machine-readable record: `evidence/manifests/GFX-EVD-005B-research-foundation.json`.
- Not covered: real data, provider selection, NAS/broker/account, full JSON-Schema
  validation via a library.

## Reversal path

Delete `requirements-research.txt`, `research/`, `tools/research_smoke.py`,
`tests/test_research_foundation.py`, the ADR, the `research-check` Make target and
the `research-foundation` CI job, and remove the two `.gitignore` entries. The
local `.venv-research` is ignored and can be removed independently. No data or
infrastructure state needs unwinding.

## Revisit trigger

Re-examine when a real EURUSD provider/broker/account is selected, when a NAS or
storage backend is chosen, when point-in-time ingestion is designed, or when a
measured need for pandas/PyArrow/Polars (or a DuckDB extension) arises.

## Approval

PM (Nuno Amaral) owns lifecycle status. Accepted under GFX-PKT-005B (Amber:
isolated package install, CI change, branch push; no real data or trading access).
