"""Strict, dependency-free validation of the GUVFX-TIM Contract v0.2 additive contracts (GFX-PKT-TIM-C1).

No JSON-Schema library is used (none is an authorised dependency). These pure functions mirror the committed
draft-07 schemas in ``research/contracts/`` and enforce the cross-field rules the schemas alone cannot express
(timestamp chain, half-open interval monotonicity, OHLC invariants, native/derived separation, watermark/gap/
quarantine rules, no-partial-release, and a signature-envelope check against an INJECTED SYNTHETIC verifier).

The existing v1 validators in ``contracts.py`` are untouched; the shared primitives are imported from there, not
re-implemented. Nothing here performs any MT5, broker, NAS, network or trading action.
"""
from __future__ import annotations

import calendar
import math
import re
from datetime import datetime, timezone

from .contracts import (  # reuse the v1 primitives verbatim — do not duplicate
    ContractError,
    ProhibitedKeyError,
    INSTANT_UTC_RE,
    MINUTE_UTC_RE,
    SHA256_RE,
    SLUG_RE,
    canonical_json_bytes,
    find_prohibited_key,
    parse_canonical_utc_instant,
    sha256_hex,
)

CANONICAL_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{2,31}$")
SCOPE_SLUG_RE = re.compile(r"^(?=.*[a-z])[a-z0-9][a-z0-9_-]*$")
REL_PATH_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*$")

TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1")
_TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
                      "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800}
ASSET_CLASSES = ("fx", "metal", "index", "commodity", "crypto", "equity")
LICENSING = ("internal_research_only", "licensed_redistribution", "unlicensed_quarantine")

SOURCE_SCHEMA_ID = "https://guvfx.local/schema/market_data_source_v1.schema.json"
EXPORT_REQUEST_V2_SCHEMA_ID = "https://guvfx.local/schema/agent_market_data_export_request_v2.schema.json"
EXPORT_RESPONSE_V2_SCHEMA_ID = "https://guvfx.local/schema/agent_market_data_export_response_v2.schema.json"
RELEASE_MANIFEST_V2_SCHEMA_ID = "https://guvfx.local/schema/dataset_release_manifest_v2.schema.json"

# The 12 additive v0.2 schema files (relative to research/contracts/), for the hash-regression evidence.
TIM_SCHEMA_FILES = (
    "market_data_source_v1", "canonical_instrument_v1", "source_instrument_mapping_v1",
    "agent_market_data_export_request_v2", "agent_market_data_export_response_v2",
    "raw_market_data_manifest_v2", "acquisition_watermark_v1", "market_data_gap_v1",
    "market_data_quality_report_v1", "dataset_release_manifest_v2",
    "dataset_release_signature_v1", "dataset_import_receipt_v1",
)


class QuarantineRequired(ContractError):
    """Raised when a record is structurally valid but must be quarantined/failed closed (ambiguity, incomplete
    bucket, unresolved DST, missing licensing, invalid mapping). A distinct type so callers can route to
    quarantine rather than treat it as a hard contract error."""


# --------------------------------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------------------------------
def _no_prohibited(obj, where: str) -> None:
    found = find_prohibited_key(obj)
    if found:
        raise ProhibitedKeyError(f"prohibited key in {where}: {found}")


def _exact_keys(obj, fields: tuple, where: str) -> None:
    if not isinstance(obj, dict):
        raise ContractError(f"{where} must be an object")
    keys = set(obj)
    extra = keys - set(fields)
    if extra:
        raise ContractError(f"unknown {where} fields: {sorted(extra)}")
    missing = set(fields) - keys
    if missing:
        raise ContractError(f"{where} missing fields: {sorted(missing)}")


def _str(obj, field, where, *, regex=None, enum=None, max_len=None, min_len=1):
    v = obj.get(field)
    if not isinstance(v, str) or isinstance(v, bool):
        raise ContractError(f"{where}.{field} must be a string")
    if min_len is not None and len(v) < min_len:
        raise ContractError(f"{where}.{field} must be non-empty")
    if max_len is not None and len(v) > max_len:
        raise ContractError(f"{where}.{field} exceeds {max_len} characters")
    if regex is not None and not regex.match(v):
        raise ContractError(f"{where}.{field} has an invalid format")
    if enum is not None and v not in enum:
        raise ContractError(f"{where}.{field} must be one of {list(enum)}")
    return v


def _const(obj, field, where, value):
    if obj.get(field) != value:
        raise ContractError(f"{where}.{field} must equal {value!r}")
    return value


def _bool(obj, field, where):
    v = obj.get(field)
    if not isinstance(v, bool):
        raise ContractError(f"{where}.{field} must be a boolean")
    return v


def _int(obj, field, where, *, minimum=0, maximum=None, nullable=False):
    v = obj.get(field)
    if v is None and nullable:
        return None
    if not isinstance(v, int) or isinstance(v, bool):
        raise ContractError(f"{where}.{field} must be an integer")
    if minimum is not None and v < minimum:
        raise ContractError(f"{where}.{field} must be >= {minimum}")
    if maximum is not None and v > maximum:
        raise ContractError(f"{where}.{field} must be <= {maximum}")
    return v


def _number(obj, field, where, *, nullable=False):
    v = obj.get(field)
    if v is None and nullable:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ContractError(f"{where}.{field} must be a finite number")
    if isinstance(v, float) and not math.isfinite(v):
        raise ContractError(f"{where}.{field} must be finite (no NaN/Infinity)")
    return v


def _array(obj, field, where):
    v = obj.get(field)
    if not isinstance(v, list):
        raise ContractError(f"{where}.{field} must be an array")
    return v


def _str_array(obj, field, where):
    v = _array(obj, field, where)
    for i, item in enumerate(v):
        if not isinstance(item, str) or isinstance(item, bool):
            raise ContractError(f"{where}.{field}[{i}] must be a string")
    return v


def _minute_epoch(value, field, where) -> int:
    """Epoch seconds for a minute-aligned UTC 'Z' instant (float-free, via calendar.timegm)."""
    if not isinstance(value, str) or not MINUTE_UTC_RE.match(value):
        raise ContractError(f"{where}.{field} must be a minute-aligned UTC 'Z' instant")
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:00Z")
    except ValueError as exc:
        raise ContractError(f"{where}.{field} is not a valid calendar instant") from exc
    return calendar.timegm(dt.replace(tzinfo=timezone.utc).utctimetuple())


def _instant(value, field, where):
    """A float-free, totally-ordered UtcInstant for an instant UTC 'Z' string."""
    if not isinstance(value, str) or not INSTANT_UTC_RE.match(value):
        raise ContractError(f"{where}.{field} must be a UTC 'Z' instant")
    return parse_canonical_utc_instant(value)


def _assert_chain(pairs, where, *, exception=None) -> None:
    """Assert a list of (field_name, UtcInstant) is NON-DECREASING (event -> ... -> published). A documented,
    tested exception name may be supplied to relax exactly one adjacent pair (none is needed for the pilot)."""
    for i in range(1, len(pairs)):
        (pname, prev), (cname, cur) = pairs[i - 1], pairs[i]
        if cur < prev:
            if exception == (pname, cname):
                continue
            raise ContractError(f"{where}: {cname} must be >= {pname} (timestamp chain out of order)")


# --------------------------------------------------------------------------------------------------
# 1. market_data_source_v1
# --------------------------------------------------------------------------------------------------
SOURCE_FIELDS = ("schema_version", "source_id", "source_type", "broker_id", "server_identity",
                 "account_scope", "account_type", "source_timezone", "licensing_classification",
                 "licensing_reference", "capacity_certified", "capacity_certification_ref", "limitations")


def validate_market_data_source(obj) -> None:
    _no_prohibited(obj, "market_data_source")
    _exact_keys(obj, SOURCE_FIELDS, "market_data_source")
    _const(obj, "schema_version", "market_data_source", "1.0")
    _str(obj, "source_id", "market_data_source", regex=SLUG_RE, max_len=64)
    _str(obj, "source_type", "market_data_source", enum=("mt5_demo", "mt5_research", "mt5_live"))
    if obj["broker_id"] is not None:
        _str(obj, "broker_id", "market_data_source", regex=SLUG_RE, max_len=64)
    _str(obj, "server_identity", "market_data_source", max_len=160)
    _str(obj, "account_scope", "market_data_source", regex=SCOPE_SLUG_RE, max_len=64)
    _str(obj, "account_type", "market_data_source", enum=("research", "demo", "paper"))
    _str(obj, "source_timezone", "market_data_source", max_len=64)
    lic = _str(obj, "licensing_classification", "market_data_source", enum=LICENSING)
    if obj["licensing_reference"] is not None:
        _str(obj, "licensing_reference", "market_data_source", max_len=512)
    _bool(obj, "capacity_certified", "market_data_source")
    if obj["capacity_certification_ref"] is not None:
        _str(obj, "capacity_certification_ref", "market_data_source", max_len=512)
    _str_array(obj, "limitations", "market_data_source")
    # Fail closed: a source with no licensing approval or without capacity certification may not be used.
    if lic == "unlicensed_quarantine" or obj["licensing_reference"] is None:
        raise QuarantineRequired("market_data_source: missing/invalid licensing approval -> quarantine")
    if not obj["capacity_certified"]:
        raise QuarantineRequired("market_data_source: capacity not certified -> quarantine")


# --------------------------------------------------------------------------------------------------
# 2. canonical_instrument_v1
# --------------------------------------------------------------------------------------------------
CANONICAL_INSTRUMENT_FIELDS = ("schema_version", "canonical_symbol", "asset_class", "base_currency",
                               "quote_currency", "price_precision", "volume_precision",
                               "price_basis_default", "limitations")


def validate_canonical_instrument(obj) -> None:
    _no_prohibited(obj, "canonical_instrument")
    _exact_keys(obj, CANONICAL_INSTRUMENT_FIELDS, "canonical_instrument")
    _const(obj, "schema_version", "canonical_instrument", "1.0")
    _str(obj, "canonical_symbol", "canonical_instrument", regex=CANONICAL_SYMBOL_RE)
    _str(obj, "asset_class", "canonical_instrument", enum=ASSET_CLASSES)
    _str(obj, "base_currency", "canonical_instrument", regex=re.compile(r"^[A-Z]{3}$"), min_len=3, max_len=3)
    _str(obj, "quote_currency", "canonical_instrument", regex=re.compile(r"^[A-Z]{3}$"), min_len=3, max_len=3)
    _int(obj, "price_precision", "canonical_instrument", minimum=0, maximum=12)
    _int(obj, "volume_precision", "canonical_instrument", minimum=0, maximum=12)
    _str(obj, "price_basis_default", "canonical_instrument", enum=("bid", "ask", "mid"))
    _str_array(obj, "limitations", "canonical_instrument")


# --------------------------------------------------------------------------------------------------
# 3. source_instrument_mapping_v1
# --------------------------------------------------------------------------------------------------
MAPPING_FIELDS = ("schema_version", "source_id", "broker_native_symbol", "canonical_symbol", "asset_class",
                  "price_precision", "volume_precision", "mapping_status", "ambiguity_reason", "limitations")


def validate_source_instrument_mapping(obj) -> None:
    _no_prohibited(obj, "source_instrument_mapping")
    _exact_keys(obj, MAPPING_FIELDS, "source_instrument_mapping")
    _const(obj, "schema_version", "source_instrument_mapping", "1.0")
    _str(obj, "source_id", "source_instrument_mapping", regex=SLUG_RE, max_len=64)
    _str(obj, "broker_native_symbol", "source_instrument_mapping", max_len=64)
    status = _str(obj, "mapping_status", "source_instrument_mapping", enum=("VERIFIED", "AMBIGUOUS", "UNMAPPED"))
    if obj["canonical_symbol"] is not None:
        _str(obj, "canonical_symbol", "source_instrument_mapping", regex=CANONICAL_SYMBOL_RE)
    if obj["asset_class"] is not None:
        _str(obj, "asset_class", "source_instrument_mapping", enum=ASSET_CLASSES)
    if obj["price_precision"] is not None:
        _int(obj, "price_precision", "source_instrument_mapping", minimum=0, maximum=12)
    if obj["volume_precision"] is not None:
        _int(obj, "volume_precision", "source_instrument_mapping", minimum=0, maximum=12)
    if obj["ambiguity_reason"] is not None:
        _str(obj, "ambiguity_reason", "source_instrument_mapping", max_len=512)
    _str_array(obj, "limitations", "source_instrument_mapping")
    if status == "VERIFIED":
        # A verified mapping must be complete and unambiguous.
        if (obj["canonical_symbol"] is None or obj["asset_class"] is None
                or obj["price_precision"] is None or obj["volume_precision"] is None):
            raise ContractError("source_instrument_mapping: VERIFIED requires a complete canonical identity")
        if obj["ambiguity_reason"] is not None:
            raise ContractError("source_instrument_mapping: VERIFIED must not carry an ambiguity_reason")
    else:
        # AMBIGUOUS / UNMAPPED fail closed.
        if obj["ambiguity_reason"] is None:
            raise ContractError(f"source_instrument_mapping: {status} requires an ambiguity_reason")
        raise QuarantineRequired(f"source_instrument_mapping: {status} mapping -> quarantine")


# --------------------------------------------------------------------------------------------------
# 4. agent_market_data_export_request_v2
# --------------------------------------------------------------------------------------------------
EXPORT_REQUEST_V2_FIELDS = ("schema_version", "operation", "source_id", "account_scope", "canonical_symbol",
                            "broker_native_symbol", "timeframe", "representation", "price_basis",
                            "range_start_utc", "range_end_utc", "range_semantics", "request_id")
EXPORT_REQUEST_V2_FINGERPRINT = tuple(f for f in EXPORT_REQUEST_V2_FIELDS if f != "request_id")


def compute_export_request_id_v2(request: dict) -> str:
    subset = {}
    for f in EXPORT_REQUEST_V2_FINGERPRINT:
        if f not in request:
            raise ContractError(f"export request missing fingerprint field: {f}")
        subset[f] = request[f]
    return sha256_hex(canonical_json_bytes(subset))


def validate_export_request_v2(obj) -> None:
    _no_prohibited(obj, "export_request")
    _exact_keys(obj, EXPORT_REQUEST_V2_FIELDS, "export_request")
    _const(obj, "schema_version", "export_request", "2.0")
    _const(obj, "operation", "export_request", "copy_rates_range")
    _str(obj, "source_id", "export_request", regex=SLUG_RE, max_len=64)
    _str(obj, "account_scope", "export_request", regex=SCOPE_SLUG_RE, max_len=64)
    _str(obj, "canonical_symbol", "export_request", regex=CANONICAL_SYMBOL_RE)
    _str(obj, "broker_native_symbol", "export_request", max_len=64)
    _str(obj, "timeframe", "export_request", enum=("M1",))
    _str(obj, "representation", "export_request", enum=("bid_ohlc",))
    _str(obj, "price_basis", "export_request", enum=("bid",))
    start = _minute_epoch(obj["range_start_utc"], "range_start_utc", "export_request")
    end = _minute_epoch(obj["range_end_utc"], "range_end_utc", "export_request")
    _const(obj, "range_semantics", "export_request", "[start,end)")
    _str(obj, "request_id", "export_request", regex=SHA256_RE, min_len=64, max_len=64)
    if end <= start:
        raise ContractError("export_request: range_end_utc must be after range_start_utc (half-open)")
    expected = compute_export_request_id_v2(obj)
    if obj["request_id"] != expected:
        raise ContractError("export_request: request_id does not match the canonical fingerprint")


# --------------------------------------------------------------------------------------------------
# bar-sequence validation (shared by export response + quality)
# --------------------------------------------------------------------------------------------------
def _validate_ohlc(bar, where):
    o = _number(bar, "open", where)
    h = _number(bar, "high", where)
    low = _number(bar, "low", where)
    c = _number(bar, "close", where)
    _number(bar, "volume", where, nullable=True)
    if not (h >= o and h >= c and h >= low):
        raise ContractError(f"{where}: high must be >= open, close and low")
    if not (low <= o and low <= c and low <= h):
        raise ContractError(f"{where}: low must be <= open, close and high")


def validate_bar_sequence(bars, timeframe, range_start, range_end, where):
    """Half-open, minute-aligned bars: each event_end == event_start + tf; strictly increasing; no duplicate,
    no overlap; each within [range_start, range_end); OHLC invariants + finite numbers. Returns observed count.
    Gaps (a jump > tf between consecutive bars) are permitted here and surfaced by the quality report."""
    step = _TIMEFRAME_SECONDS[timeframe]
    prev_end = None
    seen_starts = set()
    for i, bar in enumerate(bars):
        bw = f"{where}.bars[{i}]"
        _exact_keys(bar, ("event_start_utc", "event_end_utc", "open", "high", "low", "close", "volume"), bw)
        s = _minute_epoch(bar["event_start_utc"], "event_start_utc", bw)
        e = _minute_epoch(bar["event_end_utc"], "event_end_utc", bw)
        if e != s + step:
            raise ContractError(f"{bw}: event_end must equal event_start + {timeframe} ({step}s, half-open)")
        if s < range_start or e > range_end:
            raise ContractError(f"{bw}: bar interval lies outside the declared [range_start,range_end)")
        if s in seen_starts:
            raise ContractError(f"{bw}: duplicate bar event_start")
        seen_starts.add(s)
        if prev_end is not None:
            if s < prev_end:
                raise ContractError(f"{bw}: overlapping/out-of-order bar (start < previous end)")
        prev_end = e
        _validate_ohlc(bar, bw)
    return len(bars)


# --------------------------------------------------------------------------------------------------
# 5. agent_market_data_export_response_v2
# --------------------------------------------------------------------------------------------------
EXPORT_RESPONSE_V2_FIELDS = ("schema_version", "request_id", "source_id", "account_scope", "canonical_symbol",
                             "broker_native_symbol", "timeframe", "representation", "price_basis", "provenance",
                             "range_start_utc", "range_end_utc", "range_semantics", "source_timezone",
                             "source_available_at_utc", "collector_observed_at_utc", "bar_count", "bars",
                             "raw_object_sha256", "limitations")


def validate_export_response_v2(obj) -> None:
    _no_prohibited(obj, "export_response")
    _exact_keys(obj, EXPORT_RESPONSE_V2_FIELDS, "export_response")
    _const(obj, "schema_version", "export_response", "2.0")
    _str(obj, "request_id", "export_response", regex=SHA256_RE, min_len=64, max_len=64)
    _str(obj, "source_id", "export_response", regex=SLUG_RE, max_len=64)
    _str(obj, "account_scope", "export_response", regex=SCOPE_SLUG_RE, max_len=64)
    _str(obj, "canonical_symbol", "export_response", regex=CANONICAL_SYMBOL_RE)
    _str(obj, "broker_native_symbol", "export_response", max_len=64)
    tf = _str(obj, "timeframe", "export_response", enum=("M1",))
    _str(obj, "representation", "export_response", enum=("bid_ohlc",))
    _str(obj, "price_basis", "export_response", enum=("bid",))
    _const(obj, "provenance", "export_response", "NATIVE")
    start = _minute_epoch(obj["range_start_utc"], "range_start_utc", "export_response")
    end = _minute_epoch(obj["range_end_utc"], "range_end_utc", "export_response")
    _const(obj, "range_semantics", "export_response", "[start,end)")
    _str(obj, "source_timezone", "export_response", max_len=64)
    avail = _instant(obj["source_available_at_utc"], "source_available_at_utc", "export_response")
    observed = _instant(obj["collector_observed_at_utc"], "collector_observed_at_utc", "export_response")
    count = _int(obj, "bar_count", "export_response", minimum=0)
    bars = _array(obj, "bars", "export_response")
    _str(obj, "raw_object_sha256", "export_response", regex=SHA256_RE, min_len=64, max_len=64)
    _str_array(obj, "limitations", "export_response")
    if end <= start:
        raise ContractError("export_response: range_end_utc must be after range_start_utc (half-open)")
    observed_count = validate_bar_sequence(bars, tf, start, end, "export_response")
    if observed_count != count:
        raise ContractError("export_response: bar_count does not match the number of bars")
    # PIT at the response layer: the source cannot make the window available before its last bar has closed.
    if avail < parse_canonical_utc_instant(obj["range_end_utc"]):
        raise ContractError("export_response: source_available_at_utc must be >= range_end_utc (window not yet closed)")
    _assert_chain([("source_available_at_utc", avail), ("collector_observed_at_utc", observed)],
                  "export_response")


# --------------------------------------------------------------------------------------------------
# 6. raw_market_data_manifest_v2
# --------------------------------------------------------------------------------------------------
RAW_MANIFEST_V2_FIELDS = ("schema_version", "raw_object_id", "object_state", "source_id", "account_scope",
                          "canonical_symbol", "broker_native_symbol", "asset_class", "timeframe", "provenance",
                          "representation", "price_basis", "range_start_utc", "range_end_utc", "range_semantics",
                          "source_timezone", "event_start_utc", "event_end_utc", "source_available_at_utc",
                          "collector_observed_at_utc", "ingested_at_utc", "validated_at_utc", "request_schema_id",
                          "response_schema_id", "request_sha256", "response_sha256", "request_path",
                          "response_path", "manifest_path", "code_commit", "timezone_status", "row_count",
                          "file_size_bytes", "content_sha256", "quarantine_reason", "limitations")


def validate_raw_manifest_v2(obj) -> None:
    _no_prohibited(obj, "raw_manifest")
    _exact_keys(obj, RAW_MANIFEST_V2_FIELDS, "raw_manifest")
    _const(obj, "schema_version", "raw_manifest", "2.0")
    _str(obj, "raw_object_id", "raw_manifest", regex=SHA256_RE, min_len=64, max_len=64)
    state = _str(obj, "object_state", "raw_manifest", enum=("ACCEPTED", "QUARANTINED"))
    _str(obj, "source_id", "raw_manifest", regex=SLUG_RE, max_len=64)
    _str(obj, "account_scope", "raw_manifest", regex=SCOPE_SLUG_RE, max_len=64)
    _str(obj, "canonical_symbol", "raw_manifest", regex=CANONICAL_SYMBOL_RE)
    _str(obj, "broker_native_symbol", "raw_manifest", max_len=64)
    _str(obj, "asset_class", "raw_manifest", enum=ASSET_CLASSES)
    _str(obj, "timeframe", "raw_manifest", enum=("M1",))
    _const(obj, "provenance", "raw_manifest", "NATIVE")
    _str(obj, "representation", "raw_manifest", enum=("bid_ohlc",))
    _str(obj, "price_basis", "raw_manifest", enum=("bid",))
    rs = _minute_epoch(obj["range_start_utc"], "range_start_utc", "raw_manifest")
    re_ = _minute_epoch(obj["range_end_utc"], "range_end_utc", "raw_manifest")
    _const(obj, "range_semantics", "raw_manifest", "[start,end)")
    _str(obj, "source_timezone", "raw_manifest", max_len=64)
    es = _minute_epoch(obj["event_start_utc"], "event_start_utc", "raw_manifest")
    ee = _minute_epoch(obj["event_end_utc"], "event_end_utc", "raw_manifest")
    avail = _instant(obj["source_available_at_utc"], "source_available_at_utc", "raw_manifest")
    observed = _instant(obj["collector_observed_at_utc"], "collector_observed_at_utc", "raw_manifest")
    ingested = _instant(obj["ingested_at_utc"], "ingested_at_utc", "raw_manifest")
    validated = None
    if obj["validated_at_utc"] is not None:
        validated = _instant(obj["validated_at_utc"], "validated_at_utc", "raw_manifest")
    _str(obj, "request_schema_id", "raw_manifest", max_len=256)
    _str(obj, "response_schema_id", "raw_manifest", max_len=256)
    _str(obj, "request_sha256", "raw_manifest", regex=SHA256_RE, min_len=64, max_len=64)
    _str(obj, "response_sha256", "raw_manifest", regex=SHA256_RE, min_len=64, max_len=64)
    _str(obj, "request_path", "raw_manifest", regex=REL_PATH_RE, max_len=512)
    _str(obj, "response_path", "raw_manifest", regex=REL_PATH_RE, max_len=512)
    _str(obj, "manifest_path", "raw_manifest", regex=REL_PATH_RE, max_len=512)
    _str(obj, "code_commit", "raw_manifest", max_len=64)
    _str(obj, "timezone_status", "raw_manifest", enum=("NOT_EVALUATED", "VERIFIED", "INCONCLUSIVE", "CONFLICT"))
    _int(obj, "row_count", "raw_manifest", minimum=0)
    _int(obj, "file_size_bytes", "raw_manifest", minimum=0)
    _str(obj, "content_sha256", "raw_manifest", regex=SHA256_RE, min_len=64, max_len=64)
    if obj["quarantine_reason"] is not None:
        _str(obj, "quarantine_reason", "raw_manifest", max_len=512)
    _str_array(obj, "limitations", "raw_manifest")
    if re_ <= rs or ee <= es:
        raise ContractError("raw_manifest: ranges must be half-open (end > start)")
    # Chain: event_end <= source_available <= collector_observed <= ingested [<= validated].
    chain = [("event_end_utc", parse_canonical_utc_instant(obj["event_end_utc"])),
             ("source_available_at_utc", avail), ("collector_observed_at_utc", observed),
             ("ingested_at_utc", ingested)]
    if validated is not None:
        chain.append(("validated_at_utc", validated))
    _assert_chain(chain, "raw_manifest")
    if state == "ACCEPTED":
        if obj["quarantine_reason"] is not None:
            raise ContractError("raw_manifest: ACCEPTED must not carry a quarantine_reason")
        if obj["validated_at_utc"] is None:
            raise ContractError("raw_manifest: ACCEPTED requires validated_at_utc")
        if obj["timezone_status"] != "VERIFIED":
            raise ContractError("raw_manifest: ACCEPTED requires timezone_status VERIFIED")
    else:  # QUARANTINED
        if obj["quarantine_reason"] is None:
            raise ContractError("raw_manifest: QUARANTINED requires a quarantine_reason")


# --------------------------------------------------------------------------------------------------
# 7. acquisition_watermark_v1
# --------------------------------------------------------------------------------------------------
WATERMARK_FIELDS = ("schema_version", "source_id", "canonical_symbol", "timeframe", "representation",
                    "price_basis", "watermark_event_end_utc", "range_semantics", "last_raw_object_sha256",
                    "landed_and_verified", "validated", "quality_accepted", "advanced_at_utc", "limitations")


def validate_acquisition_watermark(obj) -> None:
    _no_prohibited(obj, "watermark")
    _exact_keys(obj, WATERMARK_FIELDS, "watermark")
    _const(obj, "schema_version", "watermark", "1.0")
    _str(obj, "source_id", "watermark", regex=SLUG_RE, max_len=64)
    _str(obj, "canonical_symbol", "watermark", regex=CANONICAL_SYMBOL_RE)
    _str(obj, "timeframe", "watermark", enum=TIMEFRAMES)
    _str(obj, "representation", "watermark", enum=("bid_ohlc",))
    _str(obj, "price_basis", "watermark", enum=("bid",))
    _minute_epoch(obj["watermark_event_end_utc"], "watermark_event_end_utc", "watermark")
    _const(obj, "range_semantics", "watermark", "[start,end)")
    if obj["last_raw_object_sha256"] is not None:
        _str(obj, "last_raw_object_sha256", "watermark", regex=SHA256_RE, min_len=64, max_len=64)
    landed = _bool(obj, "landed_and_verified", "watermark")
    validated = _bool(obj, "validated", "watermark")
    quality = _bool(obj, "quality_accepted", "watermark")
    _instant(obj["advanced_at_utc"], "advanced_at_utc", "watermark")
    _str_array(obj, "limitations", "watermark")
    # A watermark may advance ONLY after immutable landing + checksum verification + validation + quality accept.
    if not (landed and validated and quality):
        raise ContractError("watermark: advance requires landed_and_verified AND validated AND quality_accepted")


# --------------------------------------------------------------------------------------------------
# 8. market_data_gap_v1
# --------------------------------------------------------------------------------------------------
GAP_FIELDS = ("schema_version", "gap_id", "source_id", "canonical_symbol", "timeframe", "representation",
              "gap_start_utc", "gap_end_utc", "range_semantics", "gap_state", "declared_at_utc", "reason",
              "repair", "limitations")
REPAIR_FIELDS = ("repaired_at_utc", "repair_raw_object_sha256", "repair_evidence_path", "supersedes_gap_id")


def validate_market_data_gap(obj) -> None:
    _no_prohibited(obj, "gap")
    _exact_keys(obj, GAP_FIELDS, "gap")
    _const(obj, "schema_version", "gap", "1.0")
    _str(obj, "gap_id", "gap", regex=SHA256_RE, min_len=64, max_len=64)
    _str(obj, "source_id", "gap", regex=SLUG_RE, max_len=64)
    _str(obj, "canonical_symbol", "gap", regex=CANONICAL_SYMBOL_RE)
    _str(obj, "timeframe", "gap", enum=TIMEFRAMES)
    _str(obj, "representation", "gap", enum=("bid_ohlc",))
    gs = _minute_epoch(obj["gap_start_utc"], "gap_start_utc", "gap")
    ge = _minute_epoch(obj["gap_end_utc"], "gap_end_utc", "gap")
    _const(obj, "range_semantics", "gap", "[start,end)")
    state = _str(obj, "gap_state", "gap", enum=("DECLARED", "REPAIRED", "UNREPAIRABLE"))
    _instant(obj["declared_at_utc"], "declared_at_utc", "gap")
    _str(obj, "reason", "gap", max_len=512)
    _str_array(obj, "limitations", "gap")
    if ge <= gs:
        raise ContractError("gap: gap_end_utc must be after gap_start_utc (half-open)")
    repair = obj["repair"]
    if state == "REPAIRED":
        if not isinstance(repair, dict):
            raise ContractError("gap: REPAIRED requires a repair object (new immutable evidence)")
        _exact_keys(repair, REPAIR_FIELDS, "gap.repair")
        _instant(repair["repaired_at_utc"], "repaired_at_utc", "gap.repair")
        _str(repair, "repair_raw_object_sha256", "gap.repair", regex=SHA256_RE, min_len=64, max_len=64)
        _str(repair, "repair_evidence_path", "gap.repair", regex=REL_PATH_RE, max_len=512)
        if repair["supersedes_gap_id"] is not None:
            _str(repair, "supersedes_gap_id", "gap.repair", regex=SHA256_RE, min_len=64, max_len=64)
    else:
        if repair is not None:
            raise ContractError(f"gap: {state} must not carry a repair object")


# --------------------------------------------------------------------------------------------------
# 9. market_data_quality_report_v1
# --------------------------------------------------------------------------------------------------
QUALITY_FIELDS = ("schema_version", "source_id", "canonical_symbol", "timeframe", "representation", "price_basis",
                  "provenance", "range_start_utc", "range_end_utc", "range_semantics", "expected_bar_count",
                  "observed_bar_count", "duplicate_count", "out_of_order_count", "overlap_count",
                  "ohlc_violation_count", "non_finite_count", "gap_count", "quality_status",
                  "quarantine_reasons", "evaluated_at_utc", "limitations")
_VIOLATION_COUNTS = ("duplicate_count", "out_of_order_count", "overlap_count", "ohlc_violation_count",
                     "non_finite_count")


def validate_quality_report(obj) -> None:
    _no_prohibited(obj, "quality")
    _exact_keys(obj, QUALITY_FIELDS, "quality")
    _const(obj, "schema_version", "quality", "1.0")
    _str(obj, "source_id", "quality", regex=SLUG_RE, max_len=64)
    _str(obj, "canonical_symbol", "quality", regex=CANONICAL_SYMBOL_RE)
    _str(obj, "timeframe", "quality", enum=TIMEFRAMES)
    _str(obj, "representation", "quality", enum=("bid_ohlc",))
    _str(obj, "price_basis", "quality", enum=("bid",))
    _str(obj, "provenance", "quality", enum=("NATIVE", "DERIVED"))
    rs = _minute_epoch(obj["range_start_utc"], "range_start_utc", "quality")
    re_ = _minute_epoch(obj["range_end_utc"], "range_end_utc", "quality")
    _const(obj, "range_semantics", "quality", "[start,end)")
    expected = _int(obj, "expected_bar_count", "quality", minimum=0)
    observed = _int(obj, "observed_bar_count", "quality", minimum=0)
    for f in _VIOLATION_COUNTS:
        _int(obj, f, "quality", minimum=0)
    gap_count = _int(obj, "gap_count", "quality", minimum=0)
    status = _str(obj, "quality_status", "quality", enum=("PASS", "QUARANTINE", "FAIL"))
    reasons = _str_array(obj, "quarantine_reasons", "quality")
    _instant(obj["evaluated_at_utc"], "evaluated_at_utc", "quality")
    _str_array(obj, "limitations", "quality")
    if re_ <= rs:
        raise ContractError("quality: range_end_utc must be after range_start_utc")
    # Completeness is a DERIVED truth: expected_bar_count must equal the half-open range divided by the timeframe,
    # so a short self-reported expected can never let an incomplete window certify as complete.
    tf_seconds = _TIMEFRAME_SECONDS[obj["timeframe"]]
    span = re_ - rs
    if span % tf_seconds != 0:
        raise ContractError("quality: [range_start,range_end) is not a whole multiple of the timeframe")
    if expected != span // tf_seconds:
        raise ContractError("quality: expected_bar_count must equal (range_end - range_start) / timeframe")
    clean = (all(obj[f] == 0 for f in _VIOLATION_COUNTS) and gap_count == 0 and observed == expected)
    if status == "PASS":
        if not clean:
            raise ContractError("quality: PASS requires zero violations/gaps AND observed == expected (complete)")
        if reasons:
            raise ContractError("quality: PASS must not carry quarantine_reasons")
    else:
        if not reasons:
            raise ContractError(f"quality: {status} requires at least one quarantine reason")


# --------------------------------------------------------------------------------------------------
# 10. dataset_release_manifest_v2
# --------------------------------------------------------------------------------------------------
RELEASE_FIELDS = ("schema_version", "release_id", "source_id", "canonical_symbol", "asset_class", "timeframe",
                  "representation", "price_basis", "provenance", "derivation", "range_start_utc", "range_end_utc",
                  "range_semantics", "event_start_utc", "event_end_utc", "source_available_at_utc",
                  "collector_observed_at_utc", "ingested_at_utc", "validated_at_utc", "published_at_utc",
                  "row_count", "files", "quality_report_path", "watermark_path", "release_completeness",
                  "partial_release_allowed", "producer_version", "contract_version", "signing_key_id",
                  "signature_path", "ready_marker", "licensing_classification", "limitations")
DERIVATION_FIELDS = ("source_timeframe", "derivation_version", "calendar_id", "calendar_version", "bucket_policy",
                     "session_timezone", "dst_policy", "price_basis")
RELEASE_FILE_FIELDS = ("rel_path", "role", "content_sha256", "file_size_bytes", "row_count")
READY_MARKER_FIELDS = ("present", "written_last", "ready_marker_path", "ready_marker_sha256")
_FILE_ROLES = ("canonical_parquet", "raw_ndjson", "quality_report", "watermark", "gap", "signature", "ready_marker")


def validate_release_manifest_v2(obj) -> None:
    _no_prohibited(obj, "release")
    _exact_keys(obj, RELEASE_FIELDS, "release")
    _const(obj, "schema_version", "release", "2.0")
    _str(obj, "release_id", "release", regex=SLUG_RE, max_len=64)
    _str(obj, "source_id", "release", regex=SLUG_RE, max_len=64)
    _str(obj, "canonical_symbol", "release", regex=CANONICAL_SYMBOL_RE)
    _str(obj, "asset_class", "release", enum=ASSET_CLASSES)
    _str(obj, "timeframe", "release", enum=TIMEFRAMES)
    _str(obj, "representation", "release", enum=("bid_ohlc",))
    _str(obj, "price_basis", "release", enum=("bid",))
    provenance = _str(obj, "provenance", "release", enum=("NATIVE", "DERIVED"))
    rs = _minute_epoch(obj["range_start_utc"], "range_start_utc", "release")
    re_ = _minute_epoch(obj["range_end_utc"], "range_end_utc", "release")
    _const(obj, "range_semantics", "release", "[start,end)")
    ev_s = parse_canonical_utc_instant(_min_as_instant(obj["event_start_utc"], "event_start_utc"))
    ev_e = parse_canonical_utc_instant(_min_as_instant(obj["event_end_utc"], "event_end_utc"))
    avail = _instant(obj["source_available_at_utc"], "source_available_at_utc", "release")
    observed = _instant(obj["collector_observed_at_utc"], "collector_observed_at_utc", "release")
    ingested = _instant(obj["ingested_at_utc"], "ingested_at_utc", "release")
    validated = _instant(obj["validated_at_utc"], "validated_at_utc", "release")
    published = _instant(obj["published_at_utc"], "published_at_utc", "release")
    _int(obj, "row_count", "release", minimum=0)
    files = _array(obj, "files", "release")
    _str(obj, "quality_report_path", "release", regex=REL_PATH_RE, max_len=512)
    _str(obj, "watermark_path", "release", regex=REL_PATH_RE, max_len=512)
    completeness = _str(obj, "release_completeness", "release", enum=("COMPLETE", "INCOMPLETE"))
    if obj["partial_release_allowed"] is not False:
        raise ContractError("release: partial_release_allowed must be false (pilot prohibits partial releases)")
    _str(obj, "producer_version", "release", max_len=64)
    _const(obj, "contract_version", "release", "0.2")
    _str(obj, "signing_key_id", "release", regex=SLUG_RE, max_len=64)
    _str(obj, "signature_path", "release", regex=REL_PATH_RE, max_len=512)
    _str(obj, "licensing_classification", "release", enum=LICENSING)
    _str_array(obj, "limitations", "release")
    if re_ <= rs:
        raise ContractError("release: range_end_utc must be after range_start_utc (half-open)")
    # Native vs derived identity separation.
    if provenance == "DERIVED":
        d = obj["derivation"]
        if not isinstance(d, dict):
            raise ContractError("release: DERIVED requires a derivation lineage object")
        _exact_keys(d, DERIVATION_FIELDS, "release.derivation")
        _str(d, "source_timeframe", "release.derivation", enum=("M1", "M5", "M15", "M30", "H1", "H4", "D1"))
        _str(d, "derivation_version", "release.derivation", max_len=32)
        _str(d, "calendar_id", "release.derivation", regex=SLUG_RE, max_len=64)
        _str(d, "calendar_version", "release.derivation", max_len=32)
        _const(d, "bucket_policy", "release.derivation", "left_closed_right_open")
        _str(d, "session_timezone", "release.derivation", max_len=64)
        _str(d, "dst_policy", "release.derivation", enum=("calendar_versioned", "utc_fixed"))
        _str(d, "price_basis", "release.derivation", enum=("bid",))
        # A derived series must be strictly COARSER than its source and an exact integer multiple — never an
        # upsample (fabricating sub-source structure) and never a 1:1 identity relabel of native data.
        src_s, tgt_s = _TIMEFRAME_SECONDS[d["source_timeframe"]], _TIMEFRAME_SECONDS[obj["timeframe"]]
        if not (tgt_s > src_s and tgt_s % src_s == 0):
            raise ContractError("release: DERIVED timeframe must be strictly coarser than, and an integer "
                                "multiple of, source_timeframe")
    else:  # NATIVE
        if obj["derivation"] is not None:
            raise ContractError("release: NATIVE must not carry a derivation object")
    # Files.
    if not files:
        raise ContractError("release: files must be non-empty")
    for i, f in enumerate(files):
        fw = f"release.files[{i}]"
        _exact_keys(f, RELEASE_FILE_FIELDS, fw)
        _str(f, "rel_path", fw, regex=REL_PATH_RE, max_len=512)
        _str(f, "role", fw, enum=_FILE_ROLES)
        _str(f, "content_sha256", fw, regex=SHA256_RE, min_len=64, max_len=64)
        _int(f, "file_size_bytes", fw, minimum=0)
        _int(f, "row_count", fw, minimum=0, nullable=True)
    # Ready marker.
    rm = obj["ready_marker"]
    _exact_keys(rm, READY_MARKER_FIELDS, "release.ready_marker")
    present = _bool(rm, "present", "release.ready_marker")
    written_last = _bool(rm, "written_last", "release.ready_marker")
    _str(rm, "ready_marker_path", "release.ready_marker", regex=REL_PATH_RE, max_len=512)
    _str(rm, "ready_marker_sha256", "release.ready_marker", regex=SHA256_RE, min_len=64, max_len=64)
    # Timestamp chain: event_end <= source_available <= collector_observed <= ingested <= validated <= published.
    if ev_e <= ev_s:
        raise ContractError("release: event_end_utc must be after event_start_utc (half-open)")
    _assert_chain([("event_end_utc", ev_e), ("source_available_at_utc", avail),
                   ("collector_observed_at_utc", observed), ("ingested_at_utc", ingested),
                   ("validated_at_utc", validated), ("published_at_utc", published)], "release")
    # Completeness / READY-last: a visible (COMPLETE) release requires the signed READY marker present AND last.
    if completeness == "COMPLETE":
        if not (present and written_last):
            raise ContractError("release: COMPLETE requires a READY marker present AND written last")
        if obj["row_count"] <= 0:
            raise ContractError("release: COMPLETE requires row_count > 0 (no empty, data-less release)")
        if not any(f["role"] in ("canonical_parquet", "raw_ndjson") and (f["row_count"] or 0) > 0 for f in files):
            raise ContractError("release: COMPLETE requires at least one canonical data file with row_count > 0")
        if not any(f["role"] == "ready_marker" and f["rel_path"] == rm["ready_marker_path"]
                   and f["content_sha256"] == rm["ready_marker_sha256"] for f in files):
            raise ContractError("release: COMPLETE requires a ready_marker file entry matching ready_marker_path/sha256")


def _min_as_instant(value, field):
    if not isinstance(value, str) or not MINUTE_UTC_RE.match(value):
        raise ContractError(f"release.{field} must be a minute-aligned UTC 'Z' instant")
    return value


# --------------------------------------------------------------------------------------------------
# 11. dataset_release_signature_v1  (+ injected synthetic verifier)
# --------------------------------------------------------------------------------------------------
SIGNATURE_FIELDS = ("schema_version", "release_id", "signed_manifest_path", "signed_manifest_sha256",
                    "signing_key_id", "public_key_fingerprint", "algorithm", "signature_scheme",
                    "signature_value", "synthetic", "created_at_utc", "limitations")
SYNTHETIC_ALGORITHM = "synthetic-placeholder-v0"


def synthetic_sign(manifest_bytes: bytes, signing_key_id: str, public_key_fingerprint: str) -> str:
    """The INJECTED SYNTHETIC signer: a deterministic, clearly-non-cryptographic digest over a domain-separated
    concatenation. This is NOT a real signature and the operational algorithm is deliberately NOT chosen here."""
    material = (b"GFX-TIM-SYNTHETIC-PLACEHOLDER\x00"
                + signing_key_id.encode("utf-8") + b"\x00"
                + public_key_fingerprint.encode("utf-8") + b"\x00"
                + bytes(manifest_bytes))
    return sha256_hex(material)


def validate_release_signature(obj) -> None:
    _no_prohibited(obj, "signature")
    _exact_keys(obj, SIGNATURE_FIELDS, "signature")
    _const(obj, "schema_version", "signature", "1.0")
    _str(obj, "release_id", "signature", regex=SLUG_RE, max_len=64)
    _str(obj, "signed_manifest_path", "signature", regex=REL_PATH_RE, max_len=512)
    _str(obj, "signed_manifest_sha256", "signature", regex=SHA256_RE, min_len=64, max_len=64)
    _str(obj, "signing_key_id", "signature", regex=SLUG_RE, max_len=64)
    _str(obj, "public_key_fingerprint", "signature", regex=SHA256_RE, min_len=64, max_len=64)
    _const(obj, "algorithm", "signature", SYNTHETIC_ALGORITHM)
    _const(obj, "signature_scheme", "signature", "detached")
    _str(obj, "signature_value", "signature", regex=SHA256_RE, min_len=64, max_len=64)
    _const(obj, "synthetic", "signature", True)
    _instant(obj["created_at_utc"], "created_at_utc", "signature")
    _str_array(obj, "limitations", "signature")


def verify_release_signature(signature: dict, manifest_bytes: bytes, *, verifier=synthetic_sign) -> bool:
    """Structure-validate the envelope, then verify its value + bound manifest digest using an INJECTED verifier
    (synthetic by default). Returns True only when the envelope is well-formed, the manifest digest matches, and
    the recomputed synthetic signature matches. Fail-closed (returns False / raises on structural error)."""
    validate_release_signature(signature)
    if sha256_hex(bytes(manifest_bytes)) != signature["signed_manifest_sha256"]:
        return False
    expected = verifier(manifest_bytes, signature["signing_key_id"], signature["public_key_fingerprint"])
    return expected == signature["signature_value"]


# --------------------------------------------------------------------------------------------------
# 12. dataset_import_receipt_v1
# --------------------------------------------------------------------------------------------------
IMPORT_RECEIPT_FIELDS = ("schema_version", "release_id", "importer_identity", "source_release_manifest_sha256",
                         "signature_verified", "hashes_verified", "completeness_verified", "import_state",
                         "quarantine_reason", "imported_at_utc", "limitations")


def validate_import_receipt(obj) -> None:
    _no_prohibited(obj, "import_receipt")
    _exact_keys(obj, IMPORT_RECEIPT_FIELDS, "import_receipt")
    _const(obj, "schema_version", "import_receipt", "1.0")
    _str(obj, "release_id", "import_receipt", regex=SLUG_RE, max_len=64)
    _str(obj, "importer_identity", "import_receipt", regex=SLUG_RE, max_len=64)
    _str(obj, "source_release_manifest_sha256", "import_receipt", regex=SHA256_RE, min_len=64, max_len=64)
    sig = _bool(obj, "signature_verified", "import_receipt")
    hsh = _bool(obj, "hashes_verified", "import_receipt")
    comp = _bool(obj, "completeness_verified", "import_receipt")
    state = _str(obj, "import_state", "import_receipt", enum=("IMPORTED", "QUARANTINED", "REJECTED"))
    if obj["quarantine_reason"] is not None:
        _str(obj, "quarantine_reason", "import_receipt", max_len=512)
    _instant(obj["imported_at_utc"], "imported_at_utc", "import_receipt")
    _str_array(obj, "limitations", "import_receipt")
    if state == "IMPORTED":
        if not (sig and hsh and comp):
            raise ContractError("import_receipt: IMPORTED requires signature, hashes AND completeness verified")
        if obj["quarantine_reason"] is not None:
            raise ContractError("import_receipt: IMPORTED must not carry a quarantine_reason")
    else:
        if obj["quarantine_reason"] is None:
            raise ContractError(f"import_receipt: {state} requires a quarantine_reason")


# Validator registry (name -> callable) for the rule matrix / fixture-driven tests.
VALIDATORS = {
    "market_data_source_v1": validate_market_data_source,
    "canonical_instrument_v1": validate_canonical_instrument,
    "source_instrument_mapping_v1": validate_source_instrument_mapping,
    "agent_market_data_export_request_v2": validate_export_request_v2,
    "agent_market_data_export_response_v2": validate_export_response_v2,
    "raw_market_data_manifest_v2": validate_raw_manifest_v2,
    "acquisition_watermark_v1": validate_acquisition_watermark,
    "market_data_gap_v1": validate_market_data_gap,
    "market_data_quality_report_v1": validate_quality_report,
    "dataset_release_manifest_v2": validate_release_manifest_v2,
    "dataset_release_signature_v1": validate_release_signature,
    "dataset_import_receipt_v1": validate_import_receipt,
}
