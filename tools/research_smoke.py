#!/usr/bin/env python3
"""GuvFX research smoke harness (GFX-PKT-005B, GFX-PKT-005B-R1).

Proves a synthetic quote/bar -> DuckDB -> Parquet -> DuckDB round trip using only
the Python standard library and the DuckDB package. No pandas, PyArrow or Polars.
No network, no external DuckDB extensions, no real market data.

R1 hardens the round trip so the **full** versioned observation contract survives
Parquet write/readback (every required common field, all five point-in-time
timestamps, raw lineage, quality flags, and the populated quote/bar variant
fields). Reconstructed records are re-validated and compared field-by-field to the
source records. The run emits **separate** quote (interval ``event``) and bar
(interval ``M1``) dataset manifests.

All data here is deterministic and clearly fake (source id ``synthetic_test_only``).
Parquet artefacts are written into a ``tempfile.TemporaryDirectory`` (unless an
explicit test-only output directory is supplied) and removed on exit. The printed
JSON result contains no absolute personal path.

Run:
    .venv-research/bin/python tools/research_smoke.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile

import duckdb

# Canonical UTC 'Z' timestamp (no other timezone is assumed anywhere).
UTC_TS_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Clearly-fake source identifier. Never a real provider.
SYNTHETIC_SOURCE_ID = "synthetic_test_only"
INSTRUMENT_ID = "EURUSD"

# Fixed synthetic build instant keeps the manifests deterministic across runs.
SYNTHETIC_CREATED_AT = "2026-01-01T00:00:00Z"

# Required common fields shared by every market observation (v1 contract).
REQUIRED_COMMON_FIELDS = (
    "schema_version",
    "record_type",
    "instrument_id",
    "source_id",
    "broker_id",
    "account_type",
    "observation_time_utc",
    "source_time_utc",
    "received_time_utc",
    "ingestion_time_utc",
    "availability_time_utc",
    "quality_flags",
    "raw_object_id",
    "raw_object_sha256",
)
TIMESTAMP_FIELDS = (
    "observation_time_utc",
    "source_time_utc",
    "received_time_utc",
    "ingestion_time_utc",
    "availability_time_utc",
)
NULLABLE_TIMESTAMP_FIELDS = ("source_time_utc", "received_time_utc")

# Variant fields. Quote-only and bar-only sets are mutually exclusive so a field
# from the wrong variant is rejected as a cross-variant leak.
QUOTE_ONLY_FIELDS = ("bid", "ask", "bid_size", "ask_size")
BAR_ONLY_FIELDS = ("frequency", "open", "high", "low", "close", "volume", "volume_unit")

ALLOWED_QUOTE_FIELDS = frozenset(REQUIRED_COMMON_FIELDS) | frozenset(QUOTE_ONLY_FIELDS)
ALLOWED_BAR_FIELDS = frozenset(REQUIRED_COMMON_FIELDS) | frozenset(BAR_ONLY_FIELDS)

# Deterministic Parquet column order per variant (full contract surface).
QUOTE_COLUMNS = REQUIRED_COMMON_FIELDS + QUOTE_ONLY_FIELDS
BAR_COLUMNS = REQUIRED_COMMON_FIELDS + BAR_ONLY_FIELDS

# DuckDB column types keyed by contract field name.
_COLUMN_TYPES = {
    "schema_version": "VARCHAR",
    "record_type": "VARCHAR",
    "instrument_id": "VARCHAR",
    "source_id": "VARCHAR",
    "broker_id": "VARCHAR",
    "account_type": "VARCHAR",
    "observation_time_utc": "VARCHAR",
    "source_time_utc": "VARCHAR",
    "received_time_utc": "VARCHAR",
    "ingestion_time_utc": "VARCHAR",
    "availability_time_utc": "VARCHAR",
    "quality_flags": "VARCHAR[]",
    "raw_object_id": "VARCHAR",
    "raw_object_sha256": "VARCHAR",
    "bid": "DOUBLE",
    "ask": "DOUBLE",
    "bid_size": "DOUBLE",
    "ask_size": "DOUBLE",
    "frequency": "VARCHAR",
    "open": "DOUBLE",
    "high": "DOUBLE",
    "low": "DOUBLE",
    "close": "DOUBLE",
    "volume": "DOUBLE",
    "volume_unit": "VARCHAR",
}

QUOTE_PARQUET = "eurusd_quotes.parquet"
BAR_PARQUET = "eurusd_bars.parquet"


def _is_number(value) -> bool:
    """True for a real numeric value (bool is explicitly excluded)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_utc(value: str) -> str:
    """Validate a canonical UTC 'Z' timestamp; return it unchanged."""
    if not isinstance(value, str) or not UTC_TS_RE.match(value):
        raise ValueError(f"not a canonical UTC 'Z' timestamp: {value!r}")
    return value


def validate_observation(record: dict) -> None:
    """Validate one observation against the v1 contract's local rules.

    Raises ValueError on any violation. Enforces: no unknown fields, required
    field presence, the record_type discriminator, strict common-field types,
    canonical UTC timestamps, raw lineage, the availability >= observation
    ordering, the quote/bar value constraints, and cross-variant rejection
    (bar-only fields on a quote, quote-only fields on a bar).
    """
    if not isinstance(record, dict):
        raise ValueError("record must be a dict")

    if record.get("record_type") not in ("quote", "bar"):
        raise ValueError("record_type must be 'quote' or 'bar'")
    allowed = ALLOWED_QUOTE_FIELDS if record["record_type"] == "quote" else ALLOWED_BAR_FIELDS

    # Reject unknown fields and cross-variant leaks in one pass.
    for key in record:
        if key not in allowed:
            raise ValueError(f"field not allowed for {record['record_type']}: {key}")

    for field in REQUIRED_COMMON_FIELDS:
        if field not in record:
            raise ValueError(f"missing required field: {field}")

    if record["schema_version"] != "1.0":
        raise ValueError("schema_version must be '1.0'")

    # Strict common-field types (do not rely on accidental comparison errors).
    if not isinstance(record["instrument_id"], str) or not record["instrument_id"]:
        raise ValueError("instrument_id must be a non-empty string")
    if not isinstance(record["source_id"], str) or not record["source_id"]:
        raise ValueError("source_id must be a non-empty string")
    for field in ("broker_id", "account_type"):
        if record[field] is not None and not isinstance(record[field], str):
            raise ValueError(f"{field} must be a string or null")
    if not isinstance(record["quality_flags"], list):
        raise ValueError("quality_flags must be an array")
    if not all(isinstance(flag, str) for flag in record["quality_flags"]):
        raise ValueError("quality_flags entries must be strings")

    # Raw lineage is mandatory and must look like a real digest.
    if not isinstance(record["raw_object_id"], str) or not record["raw_object_id"]:
        raise ValueError("raw_object_id must be a non-empty string")
    if not isinstance(record["raw_object_sha256"], str) or not SHA256_RE.match(record["raw_object_sha256"]):
        raise ValueError("raw_object_sha256 must be a 64-char hex digest")

    # Mandatory timestamps; nullable ones validated for format only when present.
    obs = _parse_utc(record["observation_time_utc"])
    _parse_utc(record["ingestion_time_utc"])
    avail = _parse_utc(record["availability_time_utc"])
    for field in NULLABLE_TIMESTAMP_FIELDS:
        if record[field] is not None:
            _parse_utc(record[field])

    if avail < obs:
        raise ValueError("availability_time_utc must be >= observation_time_utc")

    if record["record_type"] == "quote":
        for field in ("bid", "ask"):
            if not _is_number(record.get(field)):
                raise ValueError(f"quote {field} must be numeric")
        for field in ("bid_size", "ask_size"):
            if field in record and record[field] is not None and not _is_number(record[field]):
                raise ValueError(f"quote {field} must be numeric or null")
        if record["bid"] > record["ask"]:
            raise ValueError("quote bid must be <= ask")
    else:  # bar
        if not isinstance(record.get("frequency"), str) or not record["frequency"]:
            raise ValueError("bar frequency must be a non-empty string")
        for field in ("open", "high", "low", "close"):
            if not _is_number(record.get(field)):
                raise ValueError(f"bar {field} must be numeric")
        if "volume" in record and record["volume"] is not None and not _is_number(record["volume"]):
            raise ValueError("bar volume must be numeric or null")
        if "volume_unit" in record and record["volume_unit"] is not None and not isinstance(record["volume_unit"], str):
            raise ValueError("bar volume_unit must be a string or null")
        hi, lo = record["high"], record["low"]
        if hi < lo:
            raise ValueError("bar high must be >= low")
        for field in ("open", "close"):
            if not (lo <= record[field] <= hi):
                raise ValueError(f"bar {field} must lie within [low, high]")


def _common(record_type: str, obs: str, raw_suffix: str) -> dict:
    """Build the shared field block for a synthetic observation."""
    raw_id = f"synthetic_raw_{record_type}_{raw_suffix}"
    return {
        "schema_version": "1.0",
        "record_type": record_type,
        "instrument_id": INSTRUMENT_ID,
        "source_id": SYNTHETIC_SOURCE_ID,
        "broker_id": None,
        "account_type": None,
        "observation_time_utc": obs,
        "source_time_utc": obs,
        "received_time_utc": obs,
        "ingestion_time_utc": obs,
        "availability_time_utc": obs,
        "quality_flags": ["synthetic"],
        "raw_object_id": raw_id,
        "raw_object_sha256": hashlib.sha256(raw_id.encode("utf-8")).hexdigest(),
    }


def build_synthetic_quotes() -> list[dict]:
    """Deterministic synthetic EURUSD quotes (clearly fake)."""
    base = "2026-01-02T00:00:0"
    quotes = []
    for i in range(5):
        bid = round(1.10000 + i * 0.00010, 5)
        ask = round(bid + 0.00010, 5)
        rec = _common("quote", f"{base}{i}Z", str(i))
        rec.update({"bid": bid, "ask": ask, "bid_size": 1_000_000.0, "ask_size": 1_000_000.0})
        quotes.append(rec)
    return quotes


def build_synthetic_bars() -> list[dict]:
    """Deterministic synthetic EURUSD M1 bars (clearly fake)."""
    base = "2026-01-02T00:0"
    bars = []
    for i in range(5):
        open_ = round(1.10000 + i * 0.00050, 5)
        high = round(open_ + 0.00030, 5)
        low = round(open_ - 0.00020, 5)
        close = round(open_ + 0.00010, 5)
        rec = _common("bar", f"{base}{i}:00Z", str(i))
        rec.update(
            {
                "frequency": "M1",
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 100.0 + i,
                "volume_unit": "contracts",
            }
        )
        bars.append(rec)
    return bars


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _create_and_insert(con, table: str, columns: tuple, records: list[dict]) -> None:
    """Create a fully-typed table for the contract surface and insert records."""
    col_defs = ", ".join(f'"{c}" {_COLUMN_TYPES[c]}' for c in columns)
    con.execute(f"CREATE TABLE {table} ({col_defs})")
    placeholders = ", ".join("?" for _ in columns)
    con.executemany(
        f"INSERT INTO {table} VALUES ({placeholders})",
        [tuple(rec[c] for c in columns) for rec in records],
    )


def read_parquet_records(con, path: str, columns: tuple) -> list[dict]:
    """Read a Parquet file back into a list of contract records via DuckDB."""
    select = ", ".join(f'"{c}"' for c in columns)
    rows = con.execute(
        f"SELECT {select} FROM read_parquet(?) ORDER BY observation_time_utc", [path]
    ).fetchall()
    return [dict(zip(columns, row)) for row in rows]


def _normalise(value):
    """Deterministic normalisation for value comparison (floats rounded)."""
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


def _assert_records_match(source: list[dict], readback: list[dict], columns: tuple) -> None:
    """Prove every field (incl. lineage + all five timestamps) survives unchanged."""
    if len(source) != len(readback):
        raise ValueError("row count mismatch between source and readback")
    by_raw = {r["raw_object_id"]: r for r in readback}
    for src in source:
        rb = by_raw.get(src["raw_object_id"])
        if rb is None:
            raise ValueError(f"missing readback record for {src['raw_object_id']}")
        if set(rb) != set(columns):
            raise ValueError("readback column set does not match the contract surface")
        for col in columns:
            if _normalise(src[col]) != _normalise(rb[col]):
                raise ValueError(f"value drift on field {col}")
        # Lineage and the full point-in-time timestamp set survive unchanged.
        if rb["raw_object_id"] != src["raw_object_id"] or rb["raw_object_sha256"] != src["raw_object_sha256"]:
            raise ValueError("raw lineage drift")
        for ts in TIMESTAMP_FIELDS:
            if rb[ts] != src[ts]:
                raise ValueError(f"timestamp drift on {ts}")


def _manifest(record_type: str, interval: str, dataset_id: str, records: list[dict],
              parquet_name: str, checksum: str) -> dict:
    """Build a single-dataset manifest conforming to dataset_manifest_v1."""
    config_seed = f"{dataset_id}:0.1.0".encode("utf-8")
    return {
        "schema_version": "1.0",
        "dataset_id": dataset_id,
        "dataset_version": "0.1.0",
        "created_at_utc": SYNTHETIC_CREATED_AT,
        "record_type": record_type,
        "instrument_universe": [INSTRUMENT_ID],
        "interval": interval,
        "source_objects": [
            {"source_object_id": r["raw_object_id"], "source_object_sha256": r["raw_object_sha256"]}
            for r in records
        ],
        "schema_versions": ["1.0"],
        "code_commit": "synthetic_test_only",
        "config_hash": hashlib.sha256(config_seed).hexdigest(),
        "point_in_time_policy": "availability_time_utc gating; no look-ahead; synthetic only",
        "row_count": len(records),
        "partition_count": 1,
        "content_checksums": {parquet_name: checksum},
        "quality_result": "PASS",
        "artefact_location": f"synthetic://{dataset_id} (ephemeral; deleted on exit)",
        "limitations": [
            "Synthetic data only; no provider, broker, NAS or real EURUSD prices.",
            "Artefacts are temporary and removed when the run exits.",
        ],
    }


def run_smoke(output_dir: str | None = None) -> dict:
    """Run the synthetic round trip. Returns a result dict with no absolute path.

    If ``output_dir`` is supplied (test-only) artefacts are written there and left
    for the caller to inspect/clean; otherwise a TemporaryDirectory is used and
    everything is deleted on exit.
    """
    quotes = build_synthetic_quotes()
    bars = build_synthetic_bars()
    for rec in (*quotes, *bars):
        validate_observation(rec)

    if output_dir is not None:
        return _execute(output_dir, quotes, bars)
    with tempfile.TemporaryDirectory(prefix="guvfx_research_smoke_") as tmp:
        return _execute(tmp, quotes, bars)


def _execute(work_dir: str, quotes: list[dict], bars: list[dict]) -> dict:
    quote_path = os.path.join(work_dir, QUOTE_PARQUET)
    bar_path = os.path.join(work_dir, BAR_PARQUET)

    con = duckdb.connect(database=":memory:")
    try:
        _create_and_insert(con, "quotes", QUOTE_COLUMNS, quotes)
        _create_and_insert(con, "bars", BAR_COLUMNS, bars)

        quote_written = con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        bar_written = con.execute("SELECT COUNT(*) FROM bars").fetchone()[0]

        # Write Parquet (DuckDB native writer; no external extension).
        con.execute(f"COPY quotes TO '{quote_path}' (FORMAT PARQUET)")
        con.execute(f"COPY bars TO '{bar_path}' (FORMAT PARQUET)")

        # Read back the FULL contract surface and verify the column set.
        quote_back = read_parquet_records(con, quote_path, QUOTE_COLUMNS)
        bar_back = read_parquet_records(con, bar_path, BAR_COLUMNS)

        qcur = con.execute("SELECT * FROM read_parquet(?) LIMIT 0", [quote_path])
        quote_cols = [d[0] for d in qcur.description]
        bcur = con.execute("SELECT * FROM read_parquet(?) LIMIT 0", [bar_path])
        bar_cols = [d[0] for d in bcur.description]

        quote_spread_sum = con.execute(
            "SELECT ROUND(SUM(ask - bid), 8) FROM read_parquet(?)", [quote_path]
        ).fetchone()[0]
        bar_close_avg = con.execute(
            "SELECT ROUND(AVG(close), 8) FROM read_parquet(?)", [bar_path]
        ).fetchone()[0]
        bar_high_max = con.execute(
            "SELECT ROUND(MAX(high), 8) FROM read_parquet(?)", [bar_path]
        ).fetchone()[0]
    finally:
        con.close()

    if set(quote_cols) != set(QUOTE_COLUMNS):
        raise ValueError("quote Parquet column set does not match the contract surface")
    if set(bar_cols) != set(BAR_COLUMNS):
        raise ValueError("bar Parquet column set does not match the contract surface")

    # Re-validate every reconstructed record and prove field-for-field fidelity.
    for rec in (*quote_back, *bar_back):
        validate_observation(rec)
    _assert_records_match(quotes, quote_back, QUOTE_COLUMNS)
    _assert_records_match(bars, bar_back, BAR_COLUMNS)

    if len(quote_back) != quote_written or len(bar_back) != bar_written:
        raise ValueError("row count mismatch after Parquet round trip")

    checksums = {
        QUOTE_PARQUET: _sha256_file(quote_path),
        BAR_PARQUET: _sha256_file(bar_path),
    }

    quote_manifest = _manifest(
        "quote", "event", "synthetic_eurusd_quotes", quotes, QUOTE_PARQUET, checksums[QUOTE_PARQUET]
    )
    bar_manifest = _manifest(
        "bar", "M1", "synthetic_eurusd_bars", bars, BAR_PARQUET, checksums[BAR_PARQUET]
    )

    return {
        "status": "PASS",
        "source_id": SYNTHETIC_SOURCE_ID,
        "instrument_id": INSTRUMENT_ID,
        "counts": {
            "quote_written": quote_written,
            "quote_read": len(quote_back),
            "bar_written": bar_written,
            "bar_read": len(bar_back),
        },
        "columns": {
            "quote": list(QUOTE_COLUMNS),
            "bar": list(BAR_COLUMNS),
        },
        "aggregates": {
            "quote_spread_sum": quote_spread_sum,
            "bar_close_avg": bar_close_avg,
            "bar_high_max": bar_high_max,
        },
        "full_field_roundtrip_verified": True,
        "duckdb_version": duckdb.__version__,
        "checksums": checksums,
        "quote_manifest": quote_manifest,
        "bar_manifest": bar_manifest,
    }


def main() -> int:
    result = run_smoke()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
