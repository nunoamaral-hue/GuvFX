"""Synthetic M1 bid-OHLC normalisation into market_observation_v1 (GFX-PKT-006C).

Only invoked after the timezone gate has passed. Produces bar-variant observation
records (bid OHLC only); never ask, spread or quote fields. Each record is
validated against the existing research-foundation observation rules without
weakening that schema.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class NormalisationError(RuntimeError):
    pass


def _observation_validator():
    """Lazily import the canonical observation validator (single source of truth)."""
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    from tools.research_smoke import validate_observation  # noqa: E402
    return validate_observation


def _epoch_to_z(epoch_s: int) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise_bid_ohlc(response: dict, *, raw_object_id: str, response_sha256: str,
                       received_time_utc: str, ingestion_time_utc: str,
                       synthetic: bool) -> list[dict]:
    """Map validated bid-OHLC bars into market_observation_v1 bar records.

    Caller must have validated the response contract AND passed the timezone gate.
    """
    bars = response["bars"]
    if not bars:
        raise NormalisationError("no bars to normalise")

    source = response["source"]
    instrument_id = response["symbol"]
    flags = ["historical_backfill", "bid_ohlc"]
    if synthetic:
        flags.append("synthetic")

    validate_observation = _observation_validator()
    records: list[dict] = []
    prev_t: int | None = None
    seen: set[int] = set()
    for bar in bars:
        t = bar.get("time_epoch_s")
        if not isinstance(t, int) or isinstance(t, bool):
            raise NormalisationError("bar missing integer time_epoch_s")
        if t in seen:
            raise NormalisationError("duplicate bar timestamp")
        if prev_t is not None and t <= prev_t:
            raise NormalisationError("out-of-order bar timestamp")
        seen.add(t)
        prev_t = t

        obs_z = _epoch_to_z(t)
        record = {
            "schema_version": "1.0",
            "record_type": "bar",
            "instrument_id": instrument_id,
            "source_id": source["source_id"],
            "broker_id": source["broker_reported"],
            "account_type": source["account_type"],
            "observation_time_utc": obs_z,
            "source_time_utc": obs_z,
            "received_time_utc": received_time_utc,
            "ingestion_time_utc": ingestion_time_utc,
            # Historical backfill: availability is the ingestion/validation instant,
            # never fabricated as the historical bar time.
            "availability_time_utc": ingestion_time_utc,
            "quality_flags": list(flags),
            "raw_object_id": raw_object_id,
            "raw_object_sha256": response_sha256,
            "frequency": "M1",
            "open": bar["open"],
            "high": bar["high"],
            "low": bar["low"],
            "close": bar["close"],
            "volume": None,
            "volume_unit": None,
        }
        validate_observation(record)  # fail closed on any contract violation
        records.append(record)

    return records
