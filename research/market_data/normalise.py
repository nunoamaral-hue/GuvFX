"""Gated synthetic M1 bid-OHLC publication into market_observation_v1 (GFX-PKT-006C,
R2).

The single supported public entry point is ``publish_observations``. It fails
closed: it requires timezone evidence and internally validates the request, the
response, the request/response match, and a matching ``VERIFIED`` timezone
assessment (covering every bar) BEFORE any record is produced. The mapping helper
``_map_bid_ohlc`` is private, not re-exported, and must only be called after the
gate has passed. No record is returned and no output path is created on any gate
failure. Bid-OHLC only — never ask, spread or quote fields.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from . import timezone as tzgate
from .contracts import (
    SHA256_RE,
    sha256_hex,
    strict_json_loads,
    validate_request,
    validate_request_response_match,
    validate_response,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class PublicationError(RuntimeError):
    """Fail-closed publication-gate error."""


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


def publish_observations(request: dict, response: dict, timezone_evidence: dict, *,
                         response_bytes: bytes, raw_object_id: str, response_sha256: str,
                         received_time_utc: str, ingestion_time_utc: str,
                         synthetic: bool) -> list[dict]:
    """Fail-closed publication: validate everything, then map. Returns records.

    Requires the EXACT raw response bytes and the acquired raw lineage. Raises
    before producing any record (and without creating any output) if the timezone
    evidence is absent/invalid/non-VERIFIED/mismatched/under-covering, if the
    request/response/contract checks fail, or if the supplied raw lineage does not
    bind exactly to the response bytes.
    """
    # 1. Timezone evidence must be present.
    if timezone_evidence is None:
        raise PublicationError("timezone evidence is required for publication")
    # 1b. Exact raw lineage binding (before the gate or mapping).
    if not isinstance(response_bytes, (bytes, bytearray)):
        raise PublicationError("response_bytes must be the exact raw response bytes")
    if strict_json_loads(response_bytes) != response:
        raise PublicationError("response object does not match the exact response bytes")
    if not isinstance(response_sha256, str) or not SHA256_RE.match(response_sha256):
        raise PublicationError("response_sha256 must be a lowercase SHA-256 digest")
    if response_sha256 != sha256_hex(bytes(response_bytes)):
        raise PublicationError("response_sha256 does not match the exact response bytes")
    if raw_object_id != request["request_id"]:
        raise PublicationError("raw_object_id must equal the request id")
    # 2-4. Contract validation (raises ContractError on any problem).
    validate_request(request)
    validate_response(response)
    validate_request_response_match(request, response)
    # 5. Timezone gate over EVERY bar epoch (raises TimezoneError on failure).
    bar_epochs = [bar["time_epoch_s"] for bar in response["bars"]]
    tzgate.gate_for_normalisation(
        timezone_evidence, source_id=request["source_id"],
        account_scope=request["account_scope"], bar_epochs_s=bar_epochs,
    )
    # 6. Only now map.
    return _map_bid_ohlc(
        response, raw_object_id=raw_object_id, response_sha256=response_sha256,
        received_time_utc=received_time_utc, ingestion_time_utc=ingestion_time_utc,
        synthetic=synthetic,
    )


def _map_bid_ohlc(response: dict, *, raw_object_id: str, response_sha256: str,
                  received_time_utc: str, ingestion_time_utc: str,
                  synthetic: bool) -> list[dict]:
    """PRIVATE mapper — only valid AFTER publish_observations' gate has passed.

    Maps validated bid-OHLC bars into market_observation_v1 bar records.
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
