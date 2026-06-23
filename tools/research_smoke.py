#!/usr/bin/env python3
"""GuvFX research smoke harness (GFX-PKT-005B).

Proves a synthetic quote/bar -> DuckDB -> Parquet -> DuckDB round trip using only
the Python standard library and the DuckDB package. No pandas, PyArrow or Polars.
No network, no external DuckDB extensions, no real market data.

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
from datetime import datetime, timezone

import duckdb

# Canonical UTC 'Z' timestamp (no other timezone is assumed anywhere).
UTC_TS_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Clearly-fake source identifier. Never a real provider.
SYNTHETIC_SOURCE_ID = "synthetic_test_only"
INSTRUMENT_ID = "EURUSD"

# Fixed synthetic build instant keeps the manifest deterministic across runs.
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

QUOTE_PARQUET = "eurusd_quotes.parquet"
BAR_PARQUET = "eurusd_bars.parquet"


def _parse_utc(value: str) -> datetime:
    """Parse a canonical UTC 'Z' timestamp into an aware datetime."""
    if not isinstance(value, str) or not UTC_TS_RE.match(value):
        raise ValueError(f"not a canonical UTC 'Z' timestamp: {value!r}")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def validate_observation(record: dict) -> None:
    """Validate one observation against the v1 contract's local rules.

    Raises ValueError on any violation. Enforces: required field presence, the
    record_type discriminator, canonical UTC timestamps, raw lineage, the
    availability >= observation ordering, and the quote/bar value constraints.
    """
    for field in REQUIRED_COMMON_FIELDS:
        if field not in record:
            raise ValueError(f"missing required field: {field}")

    if record["schema_version"] != "1.0":
        raise ValueError("schema_version must be '1.0'")
    if record["record_type"] not in ("quote", "bar"):
        raise ValueError("record_type must be 'quote' or 'bar'")

    if not isinstance(record["instrument_id"], str) or not record["instrument_id"]:
        raise ValueError("instrument_id must be a non-empty string")
    if not isinstance(record["source_id"], str) or not record["source_id"]:
        raise ValueError("source_id must be a non-empty string")
    if not isinstance(record["quality_flags"], list):
        raise ValueError("quality_flags must be an array")

    # Raw lineage is mandatory and must look like a real digest.
    if not isinstance(record["raw_object_id"], str) or not record["raw_object_id"]:
        raise ValueError("raw_object_id must be a non-empty string")
    if not isinstance(record["raw_object_sha256"], str) or not SHA256_RE.match(record["raw_object_sha256"]):
        raise ValueError("raw_object_sha256 must be a 64-char hex digest")

    # Mandatory timestamps.
    obs = _parse_utc(record["observation_time_utc"])
    _parse_utc(record["ingestion_time_utc"])
    avail = _parse_utc(record["availability_time_utc"])
    # Nullable timestamps: validate format only when present.
    for field in ("source_time_utc", "received_time_utc"):
        if record[field] is not None:
            _parse_utc(record[field])

    if avail < obs:
        raise ValueError("availability_time_utc must be >= observation_time_utc")

    if record["record_type"] == "quote":
        if "bid" not in record or "ask" not in record:
            raise ValueError("quote requires bid and ask")
        if record["bid"] > record["ask"]:
            raise ValueError("quote bid must be <= ask")
    else:  # bar
        for field in ("frequency", "open", "high", "low", "close"):
            if field not in record:
                raise ValueError(f"bar requires {field}")
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
        con.execute(
            "CREATE TABLE quotes ("
            "instrument_id VARCHAR, source_id VARCHAR, "
            "observation_time_utc VARCHAR, availability_time_utc VARCHAR, "
            "bid DOUBLE, ask DOUBLE)"
        )
        con.executemany(
            "INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?)",
            [
                (q["instrument_id"], q["source_id"], q["observation_time_utc"],
                 q["availability_time_utc"], q["bid"], q["ask"])
                for q in quotes
            ],
        )
        con.execute(
            "CREATE TABLE bars ("
            "instrument_id VARCHAR, source_id VARCHAR, "
            "observation_time_utc VARCHAR, availability_time_utc VARCHAR, "
            "frequency VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE)"
        )
        con.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (b["instrument_id"], b["source_id"], b["observation_time_utc"],
                 b["availability_time_utc"], b["frequency"], b["open"], b["high"],
                 b["low"], b["close"])
                for b in bars
            ],
        )

        quote_written = con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        bar_written = con.execute("SELECT COUNT(*) FROM bars").fetchone()[0]

        # Write Parquet (DuckDB native writer; no external extension).
        con.execute(f"COPY quotes TO '{quote_path}' (FORMAT PARQUET)")
        con.execute(f"COPY bars TO '{bar_path}' (FORMAT PARQUET)")

        # Read back via DuckDB and verify.
        quote_read = con.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [quote_path]
        ).fetchone()[0]
        bar_read = con.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [bar_path]
        ).fetchone()[0]
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

    if quote_read != quote_written or bar_read != bar_written:
        raise ValueError("row count mismatch after Parquet round trip")

    checksums = {
        QUOTE_PARQUET: _sha256_file(quote_path),
        BAR_PARQUET: _sha256_file(bar_path),
    }

    manifest = {
        "schema_version": "1.0",
        "dataset_id": "synthetic_eurusd_smoke",
        "dataset_version": "0.1.0",
        "created_at_utc": SYNTHETIC_CREATED_AT,
        "instrument_universe": [INSTRUMENT_ID],
        "interval": "M1",
        "source_objects": [
            {"source_object_id": q["raw_object_id"], "source_object_sha256": q["raw_object_sha256"]}
            for q in quotes
        ]
        + [
            {"source_object_id": b["raw_object_id"], "source_object_sha256": b["raw_object_sha256"]}
            for b in bars
        ],
        "schema_versions": ["1.0"],
        "code_commit": "synthetic_test_only",
        "config_hash": hashlib.sha256(b"synthetic_eurusd_smoke:0.1.0").hexdigest(),
        "point_in_time_policy": "availability_time_utc gating; no look-ahead; synthetic only",
        "row_count": quote_written + bar_written,
        "partition_count": 2,
        "content_checksums": checksums,
        "quality_result": "PASS",
        "artefact_location": "synthetic://eurusd-smoke (ephemeral; deleted on exit)",
        "limitations": [
            "Synthetic data only; no provider, broker, NAS or real EURUSD prices.",
            "Artefacts are temporary and removed when the run exits.",
        ],
    }

    return {
        "status": "PASS",
        "source_id": SYNTHETIC_SOURCE_ID,
        "instrument_id": INSTRUMENT_ID,
        "counts": {
            "quote_written": quote_written,
            "quote_read": quote_read,
            "bar_written": bar_written,
            "bar_read": bar_read,
        },
        "aggregates": {
            "quote_spread_sum": quote_spread_sum,
            "bar_close_avg": bar_close_avg,
            "bar_high_max": bar_high_max,
        },
        "duckdb_version": duckdb.__version__,
        "checksums": checksums,
        "manifest": manifest,
    }


def main() -> int:
    result = run_smoke()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
