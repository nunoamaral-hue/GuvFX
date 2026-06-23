# GuvFX Research Foundation

Minimal, reproducible research environment for market-data work. Established by
**GFX-PKT-005B** and **ADR 0010** (`docs/ADRs/0010-market-data-research-foundation.md`).

> **No real data exists from this packet.** Everything here operates on
> deterministic synthetic data clearly marked with source id
> `synthetic_test_only`. No provider, broker, account, NAS path or real EURUSD
> price is part of this foundation.

## Environment setup

The research environment is an isolated virtualenv built from the local
Python 3.14 interpreter, with **DuckDB as its only third-party dependency**:

```bash
python3 -m venv .venv-research
.venv-research/bin/python -m pip install --only-binary=:all: --no-deps -r requirements-research.txt
```

`/.venv-research/` is git-ignored and never staged. Do not upgrade pip and do not
add packages; `requirements-research.txt` pins exactly `duckdb==1.5.4`.

## Check command

```bash
make research-check
```

This fails with a setup instruction if `.venv-research/bin/python` is absent,
then runs the synthetic smoke harness and the research unit tests. It is a
**separate** target and is intentionally **not** part of the default `make check`
chain in this packet.

You can also run the pieces directly:

```bash
.venv-research/bin/python tools/research_smoke.py
.venv-research/bin/python -m unittest discover -s tests -p 'test_research_foundation.py' -v
```

## Why DuckDB-only is the minimal first dependency

Per `.claude/rules/architecture.md` ("no speculative infrastructure"), we add the
fewest dependencies that prove the loop. DuckDB is a single binary-only wheel that
reads and writes Parquet natively, so a quote/bar → Parquet → query round trip
needs **no pandas, PyArrow or Polars**. Heavier dataframe libraries are added only
on a **measured need** and via an approved decision.

## `GUVFX_DATA_ROOT`

Future real data will live under the **`GUVFX_DATA_ROOT`** environment variable.
It is **required** for any real-data work and **must point outside the Git
repository**. There is deliberately **no repository-path default** — code must not
fall back to a path inside the repo, and no real-data directory is committed.

## Logical data zones

When real data is eventually introduced (not in this packet), it is organised
under `GUVFX_DATA_ROOT` into logical zones:

- **raw** — immutable captured source objects; written once, never edited in
  place (`.claude/rules/data.md`).
- **normalised** — source records mapped onto the versioned contracts.
- **curated** — cleaned, analysis-ready datasets.
- **features** — derived features carrying the time they could first be computed.
- **artefacts** — dataset manifests, evidence and reproducible outputs.

Normalised / curated / features / artefacts are **rebuildable caches** —
reproducible from raw inputs plus recorded config; only raw is a source.

## Contracts

Versioned JSON Schemas under `research/contracts/`:

- `market_observation_v1.schema.json` — quote/bar observations with source and
  time lineage.
- `broker_cost_v1.schema.json` — point-in-time broker cost/contract specification.
- `dataset_manifest_v1.schema.json` — reproducible dataset manifest (carries a
  required `record_type` so quote vs bar datasets are unambiguous).

The synthetic smoke harness (`tools/research_smoke.py`) preserves **all** required
observation fields, the **five point-in-time timestamps**
(`observation`/`source`/`received`/`ingestion`/`availability`), the **raw-lineage**
fields (`raw_object_id`, `raw_object_sha256`), `quality_flags`, and the populated
quote/bar variant fields through the Parquet write and readback. Each reconstructed
record is re-validated and compared field-by-field to its source. The run emits
**separate** dataset manifests — quotes with `interval: event` and bars with
`interval: M1` — each referencing only its own raw objects, checksum and counts.

## Status / blockers

- No real data exists; the smoke harness uses synthetic data only.
- Sponsor/source decisions (provider, broker, account, NAS, granularity, date
  interval, storage backend) remain **Open** and **block ingestion**
  (`docs/DATA_CONTRACTS.md`, ADR 0010).
- Generated temporary smoke artefacts are written to a `TemporaryDirectory` and
  **deleted automatically** on exit; nothing persistent is produced.
