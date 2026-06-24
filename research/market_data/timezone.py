"""Broker-timezone verification gate (GFX-PKT-006C).

Normalisation is permitted ONLY against a VERIFIED assessment that matches the raw
object's source/scope and whose covered interval includes every bar instant. There
is NO default offset and NO hardcoded broker constant. Inconclusive, conflicting,
mismatched or under-covering evidence fails closed.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from .contracts import (
    INSTANT_UTC_RE,
    SHA256_RE,
    SLUG_RE,
    ContractError,
    _require_bounded_str,
    canonical_json_bytes,
    sha256_hex,
)

EVIDENCE_FIELDS = (
    "schema_version", "source_id", "account_scope", "assessment_status",
    "evidence_method", "assessed_at_utc", "covered_start_utc", "covered_end_utc",
    "observations", "dst_behaviour", "evidence_fingerprint", "limitations",
)
STATUSES = ("VERIFIED", "INCONCLUSIVE", "CONFLICT")


class TimezoneError(RuntimeError):
    """Fail-closed timezone gate error."""


def _instant(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not INSTANT_UTC_RE.match(value):
        raise ContractError(f"{field} must be a UTC 'Z' instant")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise ContractError(f"{field} is not a valid calendar instant") from None


def compute_evidence_fingerprint(evidence: dict) -> str:
    """Deterministic fingerprint over the evidence minus the fingerprint field."""
    body = copy.deepcopy(evidence)
    body.pop("evidence_fingerprint", None)
    return sha256_hex(canonical_json_bytes(body))


def validate_timezone_evidence(evidence: dict) -> None:
    if not isinstance(evidence, dict) or set(evidence) != set(EVIDENCE_FIELDS):
        raise ContractError("timezone evidence has wrong field set")
    if evidence["schema_version"] != "1.0":
        raise ContractError("timezone evidence schema_version must be '1.0'")
    # Type-check slugs before regex use; enforce maximum length.
    if not isinstance(evidence["source_id"], str) or not SLUG_RE.match(evidence["source_id"]):
        raise ContractError("timezone evidence source_id must be a slug")
    if len(evidence["source_id"]) > 64:
        raise ContractError("timezone evidence source_id exceeds 64 characters")
    if not isinstance(evidence["account_scope"], str) or not SLUG_RE.match(evidence["account_scope"]):
        raise ContractError("timezone evidence account_scope must be a slug")
    if len(evidence["account_scope"]) > 64:
        raise ContractError("timezone evidence account_scope exceeds 64 characters")
    if not any(c.isalpha() for c in evidence["account_scope"]):
        raise ContractError("timezone evidence account_scope must not be all digits")
    if evidence["assessment_status"] not in STATUSES:
        raise ContractError("timezone assessment_status invalid")
    _require_bounded_str(evidence["evidence_method"], "evidence_method", 256)
    _require_bounded_str(evidence["dst_behaviour"], "dst_behaviour", 256)
    _instant(evidence["assessed_at_utc"], "assessed_at_utc")
    start = _instant(evidence["covered_start_utc"], "covered_start_utc")
    end = _instant(evidence["covered_end_utc"], "covered_end_utc")
    if not (end > start):
        raise ContractError("covered_end_utc must be later than covered_start_utc")
    if not isinstance(evidence["observations"], list):
        raise ContractError("observations must be a list")
    for obs in evidence["observations"]:
        if not isinstance(obs, dict) or set(obs) != {
            "observed_at_utc", "server_clock_epoch_s", "utc_clock_epoch_s",
            "implied_offset_seconds",
        }:
            raise ContractError("observation has wrong field set")
        obs_dt = _instant(obs["observed_at_utc"], "observation.observed_at_utc")
        for f in ("server_clock_epoch_s", "utc_clock_epoch_s", "implied_offset_seconds"):
            if not isinstance(obs[f], int) or isinstance(obs[f], bool):
                raise ContractError(f"observation.{f} must be an integer")
        # Offset arithmetic must be internally consistent.
        if obs["server_clock_epoch_s"] - obs["utc_clock_epoch_s"] != obs["implied_offset_seconds"]:
            raise ContractError("observation implied_offset_seconds does not match clock difference")
        # Each observation must fall within the covered interval [start, end),
        # compared as exact aware-UTC instants (no fractional-second truncation).
        if not (start <= obs_dt < end):
            raise ContractError("observation observed_at_utc outside covered interval")
    if not SHA256_RE.match(evidence["evidence_fingerprint"]):
        raise ContractError("evidence_fingerprint must be 64 lowercase hex")
    if compute_evidence_fingerprint(evidence) != evidence["evidence_fingerprint"]:
        raise ContractError("evidence_fingerprint does not match evidence content")
    if not isinstance(evidence["limitations"], list) or not all(
        isinstance(x, str) for x in evidence["limitations"]
    ):
        raise ContractError("limitations must be a list of strings")


def gate_for_normalisation(evidence: dict, *, source_id: str, account_scope: str,
                           bar_epochs_s) -> None:
    """Raise TimezoneError unless evidence permits normalising the given bars."""
    try:
        validate_timezone_evidence(evidence)
    except ContractError as exc:
        raise TimezoneError(f"invalid timezone evidence: {exc}") from exc

    if evidence["assessment_status"] != "VERIFIED":
        raise TimezoneError(
            f"timezone status is {evidence['assessment_status']}, not VERIFIED; "
            "normalisation blocked"
        )
    if evidence["source_id"] != source_id or evidence["account_scope"] != account_scope:
        raise TimezoneError("timezone evidence source/scope does not match raw object")

    bars = list(bar_epochs_s)
    if not bars:
        raise TimezoneError("no bar timestamps to cover")
    covered_start = _instant(evidence["covered_start_utc"], "covered_start_utc")
    covered_end = _instant(evidence["covered_end_utc"], "covered_end_utc")
    # Compare each bar instant as an exact aware-UTC datetime against the covered
    # interval [start, end) — no integer-epoch truncation of fractional seconds.
    for epoch in bars:
        bar_dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        if not (covered_start <= bar_dt < covered_end):
            raise TimezoneError("timezone assessment interval does not cover all bar instants")
