#!/usr/bin/env python3
"""Synthetic end-to-end market-data smoke (GFX-PKT-006C). synthetic_test_only.

Proves, with ZERO network and ZERO real systems:
  synthetic request -> contract validation -> immutable raw landing ->
  idempotent rerun -> VERIFIED timezone gate -> market_observation_v1 normalisation
  -> temporary Parquet/DuckDB round trip -> dataset_manifest_v1

Uses only committed synthetic fixtures and an injected fixture transport, inside an
explicit TemporaryDirectory data root that is deleted on exit. Emits deterministic
JSON with no absolute path.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from research.market_data import config, normalise, orchestrator  # noqa: E402
from research.market_data import timezone as tzgate  # noqa: E402
from research.market_data.agent_client import FixtureTransport, network_blocked  # noqa: E402
from research.market_data.storage import RawStore  # noqa: E402
from tools import research_smoke as rf  # noqa: E402


def _verified_timezone_evidence(request: dict) -> dict:
    """Deterministic VERIFIED evidence whose observation falls within coverage.

    Built in-memory (the committed verified fixture intentionally violates the R2
    observation-coverage rule and is retained as a negative test).
    """
    evidence = {
        "schema_version": "1.0",
        "source_id": request["source_id"],
        "account_scope": request["account_scope"],
        "assessment_status": "VERIFIED",
        "evidence_method": "synthetic_clock_compare",
        "assessed_at_utc": "2025-01-01T00:00:00Z",
        "covered_start_utc": "2025-01-01T00:00:00Z",
        "covered_end_utc": "2025-02-01T00:00:00Z",
        "observations": [
            {"observed_at_utc": "2025-01-01T00:00:00Z", "server_clock_epoch_s": 1735689600,
             "utc_clock_epoch_s": 1735689600, "implied_offset_seconds": 0}
        ],
        "dst_behaviour": "synthetic_none",
        "limitations": ["synthetic_test_only", "invented_offset_for_interface_test"],
    }
    evidence["evidence_fingerprint"] = tzgate.compute_evidence_fingerprint(evidence)
    return evidence

BANNER = "synthetic_test_only"
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "market_data")

# Fixed synthetic instants keep the output deterministic across runs.
CODE_COMMIT = "synthetic_test_only"
ACQUIRED_AT = "2026-01-01T00:00:00Z"
RECEIVED_AT = "2026-01-01T00:00:00Z"
INGESTION_AT = "2026-01-01T00:00:05Z"
PARQUET_NAME = "eurusd_m1_2025_bid.parquet"


def _load(name: str):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


def _norm(v):
    return round(v, 9) if isinstance(v, float) else v


def _assert_match_by_time(source: list[dict], readback: list[dict]) -> None:
    """All bars share one raw object, so match by unique observation_time_utc."""
    if len(source) != len(readback):
        raise RuntimeError("row count drift after Parquet round trip")
    by_time = {r["observation_time_utc"]: r for r in readback}
    for src in source:
        rb = by_time.get(src["observation_time_utc"])
        if rb is None or set(rb) != set(rf.BAR_COLUMNS):
            raise RuntimeError("readback record missing or wrong column set")
        for col in rf.BAR_COLUMNS:
            if _norm(src.get(col)) != _norm(rb.get(col)):
                raise RuntimeError(f"value drift on field {col}")
        if rb["raw_object_id"] != src["raw_object_id"] or rb["raw_object_sha256"] != src["raw_object_sha256"]:
            raise RuntimeError("raw lineage drift")


def run_smoke() -> dict:
    request = json.loads(_load("synthetic_history_request.json"))
    response_bytes = _load("synthetic_history_response.json")
    response = json.loads(response_bytes)
    tz_verified = _verified_timezone_evidence(request)

    with network_blocked():
        with tempfile.TemporaryDirectory(prefix="guvfx_md_smoke_") as tmp:
            root = config.synthetic_data_root(tmp)
            store = RawStore(root.path)
            transport = FixtureTransport(response_bytes)

            acq = orchestrator.acquire(
                request, transport, store, code_commit=CODE_COMMIT,
                acquired_at_utc=ACQUIRED_AT, received_at_utc=RECEIVED_AT,
            )
            if acq["status"] != "ACCEPTED":
                raise RuntimeError(f"expected ACCEPTED, got {acq['status']}")

            # Identical idempotent rerun must not modify accepted bytes.
            rerun = orchestrator.acquire(
                request, transport, store, code_commit=CODE_COMMIT,
                acquired_at_utc=ACQUIRED_AT, received_at_utc=RECEIVED_AT,
            )
            if rerun["status"] != "ALREADY_PRESENT":
                raise RuntimeError(f"expected ALREADY_PRESENT, got {rerun['status']}")

            # Gated publication: request/response/match + VERIFIED timezone evidence
            # are validated INSIDE publish_observations before any record is produced.
            records = normalise.publish_observations(
                request, response, tz_verified, response_bytes=response_bytes,
                raw_object_id=acq["raw_object_id"],
                response_sha256=acq["response_sha256"], received_time_utc=RECEIVED_AT,
                ingestion_time_utc=INGESTION_AT, synthetic=True,
            )

            # Temporary Parquet round trip via the pinned DuckDB (research foundation).
            import duckdb
            parquet_path = os.path.join(tmp, "work", PARQUET_NAME)
            os.makedirs(os.path.dirname(parquet_path), exist_ok=True)
            con = duckdb.connect(database=":memory:")
            try:
                rf._create_and_insert(con, "bars", rf.BAR_COLUMNS, records)
                con.execute(f"COPY bars TO '{parquet_path}' (FORMAT PARQUET)")
                readback = rf.read_parquet_records(con, parquet_path, rf.BAR_COLUMNS)
            finally:
                con.close()

            for rec in readback:
                rf.validate_observation(rec)
            _assert_match_by_time(records, readback)

            with open(parquet_path, "rb") as fh:
                parquet_sha = hashlib.sha256(fh.read()).hexdigest()

            dataset_manifest = {
                "schema_version": "1.0",
                "dataset_id": "synthetic_eurusd_m1_2025_slice",
                "dataset_version": "0.1.0",
                "created_at_utc": INGESTION_AT,
                "record_type": "bar",
                "instrument_universe": [response["symbol"]],
                "interval": "M1",
                "source_objects": [
                    {"source_object_id": acq["raw_object_id"],
                     "source_object_sha256": acq["response_sha256"]}
                ],
                "schema_versions": ["1.0"],
                "code_commit": CODE_COMMIT,
                "config_hash": hashlib.sha256(
                    b"synthetic_eurusd_m1_2025_slice:0.1.0"
                ).hexdigest(),
                "point_in_time_policy": (
                    "availability_time_utc gating; VERIFIED timezone evidence required; "
                    "no look-ahead; synthetic only"
                ),
                "row_count": len(records),
                "partition_count": 1,
                "content_checksums": {PARQUET_NAME: parquet_sha},
                "quality_result": "PASS",
                "artefact_location": "synthetic://eurusd-m1-2025 (ephemeral; deleted on exit)",
                "limitations": [
                    "synthetic_test_only; no provider, broker, NAS or real EURUSD prices.",
                    "Bid OHLC only; no ask/spread/tick data.",
                ],
            }

            result = {
                "banner": BANNER,
                "status": "PASS",
                "acquire_status": acq["status"],
                "rerun_status": rerun["status"],
                "timezone_status": tz_verified["assessment_status"],
                "normalised_count": len(records),
                "parquet_rows_read": len(readback),
                "raw_object_id": acq["raw_object_id"],
                "request_sha256": acq["request_sha256"],
                "response_sha256": acq["response_sha256"],
                "parquet_sha256": parquet_sha,
                "raw_paths": {
                    "request_path": acq["request_path"],
                    "response_path": acq["response_path"],
                    "manifest_path": acq["manifest_path"],
                },
                "dataset_manifest": dataset_manifest,
            }
        # TemporaryDirectory deleted here; nothing persistent remains.
    return result


def main() -> int:
    result = run_smoke()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
