"""Strict, dependency-free validation of the v1 market-data contracts.

No JSON-Schema library is used (none is an authorised dependency). These pure
functions mirror the committed draft-07 schemas and enforce the cross-field rules
the schemas alone cannot (request/response match, ordering, prohibited keys).
"""

from __future__ import annotations

import calendar
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from functools import total_ordering

MINUTE_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:00Z$")
INSTANT_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$")
# Canonical UTC 'Z' instant split into a whole-second component and an optional,
# arbitrary-length fractional-second digit string (no float anywhere).
_CANONICAL_INSTANT_RE = re.compile(
    r"^(?P<whole>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<frac>[0-9]+))?Z$"
)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

REQUEST_SCHEMA_ID = "https://guvfx.local/schema/agent_history_export_request_v1.schema.json"
RESPONSE_SCHEMA_ID = "https://guvfx.local/schema/agent_history_export_response_v1.schema.json"

# Keys that must never appear anywhere in a payload (credential / account number).
PROHIBITED_KEYS = frozenset(
    {"password", "token", "secret", "api_key", "login", "account_number"}
)

REQUEST_FIELDS = (
    "schema_version", "operation", "source_id", "account_scope", "symbol",
    "timeframe", "representation", "range_start_utc", "range_end_utc",
    "range_semantics", "request_id",
)
REQUEST_FINGERPRINT_FIELDS = tuple(f for f in REQUEST_FIELDS if f != "request_id")


class ContractError(ValueError):
    """A contract validation failure."""


class ProhibitedKeyError(ContractError):
    """A prohibited credential/account-number key was present in a payload."""


# Exact integer Unix-epoch bounds of the canonical timestamp parser's calendar
# domain — earliest 0001-01-01T00:00:00Z and latest whole-second
# 9999-12-31T23:59:59Z. Derived once with standard-library date/integer
# arithmetic (calendar.timegm over an integer time tuple): no floating point and
# no datetime.timestamp(). Direct UtcInstant construction is confined to this
# range so it can never represent an instant the parser itself cannot.
MIN_CANONICAL_EPOCH_S = calendar.timegm(datetime(1, 1, 1, 0, 0, 0).timetuple())
MAX_CANONICAL_EPOCH_S = calendar.timegm(datetime(9999, 12, 31, 23, 59, 59).timetuple())

# The only code points admitted inside a normalized fractional-second string.
# Mirrors the canonical parser's ASCII ``[0-9]`` class. Python's str.isdigit()/
# isdecimal()/isnumeric() also accept non-ASCII digit characters (Arabic-Indic,
# extended Arabic-Indic, Devanagari, fullwidth, superscript, …), which must never
# enter an instant value, so they are rejected by an explicit ASCII membership test.
_ASCII_DIGITS = frozenset("0123456789")


@total_ordering
class UtcInstant:
    """An exact, immutable UTC instant for point-in-time comparison.

    Built from a canonical UTC 'Z' timestamp with ANY non-empty number of
    fractional-second digits. The whole-second component is validated as a real
    calendar instant and stored as an integer Unix epoch second. The fractional
    component is stored as a *normalized decimal-digit string* (trailing zeros
    removed; ``""`` means an exact whole second) — never converted to an integer,
    a power of ten or binary floating point. This is safe for arbitrarily long
    fractions: there is no dependence on CPython's int<->str digit limit, and
    comparison is a direct lexicographic walk over the normalized digits, which —
    for trailing-zero-normalized fractional strings — is exactly the numeric order.

    Direct construction is confined to the canonical parser's own domain: the
    epoch must lie within ``[MIN_CANONICAL_EPOCH_S, MAX_CANONICAL_EPOCH_S]`` (the
    years 0001–9999) and the fractional digits must be empty or normalized ASCII
    ``[0-9]+`` (non-ASCII digit characters are rejected), so a directly built
    instant can never represent state the parser itself could not produce.

    The value is immutable (attribute assignment/deletion raises) and is
    deliberately **unhashable** (``__hash__ = None``): it compares equal to a bare
    non-boolean integer Unix epoch second, so no tuple/int hash could honour
    Python's equal-objects-equal-hashes contract across both types.
    """

    __slots__ = ("_epoch_s", "_frac_digits")

    def __init__(self, epoch_s: int, frac_digits: str):
        if not isinstance(epoch_s, int) or isinstance(epoch_s, bool):
            raise ContractError("UtcInstant epoch must be an integer")
        if not (MIN_CANONICAL_EPOCH_S <= epoch_s <= MAX_CANONICAL_EPOCH_S):
            raise ContractError(
                "UtcInstant epoch is outside the canonical year 0001-9999 domain")
        if not isinstance(frac_digits, str):
            raise ContractError("UtcInstant fractional digits must be a string")
        # ASCII decimal only: an explicit membership test (never isdigit/isdecimal/
        # isnumeric, which admit non-ASCII digits) walked linearly over the string,
        # with a normalized, trailing-zero-free terminal digit. No int/float formed.
        if frac_digits and (
            not _ASCII_DIGITS.issuperset(frac_digits) or frac_digits[-1] == "0"
        ):
            raise ContractError(
                "UtcInstant fractional digits must be normalized ASCII decimal digits")
        object.__setattr__(self, "_epoch_s", epoch_s)
        object.__setattr__(self, "_frac_digits", frac_digits)

    def __setattr__(self, name, value):
        raise AttributeError("UtcInstant is immutable")

    def __delattr__(self, name):
        raise AttributeError("UtcInstant is immutable")

    @property
    def epoch_seconds(self) -> int:
        """The whole-second Unix epoch (microsecond zero), as an exact integer."""
        return self._epoch_s

    @staticmethod
    def _key_of(other):
        if isinstance(other, UtcInstant):
            return (other._epoch_s, other._frac_digits)
        if isinstance(other, int) and not isinstance(other, bool):
            return (other, "")
        return None

    def __eq__(self, other):
        key = self._key_of(other)
        if key is None:
            return NotImplemented
        # Lexicographic comparison of trailing-zero-normalized fractional digit
        # strings equals their numeric order (shorter-as-prefix is numerically
        # smaller because a normalized string never ends in a zero).
        return (self._epoch_s, self._frac_digits) == key

    def __lt__(self, other):
        key = self._key_of(other)
        if key is None:
            return NotImplemented
        return (self._epoch_s, self._frac_digits) < key

    # Equal to bare integer epochs across types ⇒ no consistent hash exists.
    __hash__ = None

    def __repr__(self):
        return f"UtcInstant(epoch_s={self._epoch_s}, frac_digits={self._frac_digits!r})"


def parse_canonical_utc_instant(value) -> UtcInstant:
    """Parse a canonical UTC 'Z' instant into an exact, comparable ``UtcInstant``.

    Accepts any non-empty number of fractional-second digits — including very long
    fractions — without converting the digit string to an integer (no dependence on
    CPython's int<->str digit limit) and without binary floating point. Rejects
    non-strings, non-canonical representations, impossible calendar/time values and
    any non-UTC form through ``ContractError``. No payload/value text appears in the
    message.
    """
    if not isinstance(value, str):
        raise ContractError("UTC instant must be a string")
    m = _CANONICAL_INSTANT_RE.match(value)
    if not m:
        raise ContractError("value is not a canonical UTC 'Z' instant")
    try:
        dt = datetime.fromisoformat(m.group("whole") + "+00:00").astimezone(timezone.utc)
    except ValueError:
        raise ContractError("value is not a valid calendar instant") from None
    epoch_s = calendar.timegm(dt.timetuple())  # integer epoch; no float conversion
    # Normalize the fractional digits by removing trailing zeros only; leading
    # zeros are significant and preserved. No int()/10**n/float is ever formed.
    digits = m.group("frac") or ""
    return UtcInstant(epoch_s, digits.rstrip("0"))


def canonical_json_bytes(obj) -> bytes:
    """Deterministic UTF-8 JSON: sorted keys, compact separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_constant(token: str):
    raise ContractError("non-standard JSON constant rejected")


def strict_json_loads(data: bytes):
    """Decode UTF-8 JSON strictly. No body appears in any error message.

    Rejects invalid UTF-8, invalid JSON, and the non-standard constants NaN,
    Infinity and -Infinity (which Python's json accepts by default).
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ContractError("payload must be bytes")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        raise ContractError("payload is not valid UTF-8") from None
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError:
        raise ContractError("payload is not valid JSON") from None


# Quoted JSON key token for a prohibited key, e.g. "token" : ... (case-insensitive,
# optional whitespace before the colon). Deliberately conservative: it only matches
# a *quoted key* followed by a colon, not an arbitrary occurrence in a value.
_PROHIBITED_KEY_TOKEN_RE = re.compile(
    rb'"(?:password|token|secret|api_key|login|account_number)"\s*:',
    re.IGNORECASE,
)


def scan_raw_for_prohibited_key_tokens(raw: bytes) -> bool:
    """True if raw bytes appear to contain a prohibited JSON key token."""
    if not isinstance(raw, (bytes, bytearray)):
        return False
    return bool(_PROHIBITED_KEY_TOKEN_RE.search(bytes(raw)))


def find_prohibited_key(obj) -> str | None:
    """Recursively scan for a prohibited key; return the first found or None."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.strip().lower() in PROHIBITED_KEYS:
                return key
            found = find_prohibited_key(value)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found = find_prohibited_key(item)
            if found:
                return found
    return None


def _parse_minute_utc(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not MINUTE_UTC_RE.match(value):
        raise ContractError(f"{field} must be a minute-aligned UTC 'Z' instant")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise ContractError(f"{field} is not a valid calendar instant: {value!r}") from exc


def _parse_instant_utc(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not INSTANT_UTC_RE.match(value):
        raise ContractError(f"{field} must be a UTC 'Z' instant")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise ContractError(f"{field} is not a valid calendar instant: {value!r}") from exc


def _require_scope(value, field: str) -> None:
    if not isinstance(value, str) or not SLUG_RE.match(value):
        raise ContractError(f"{field} must be a lowercase safe slug")
    if len(value) > 64:
        raise ContractError(f"{field} exceeds 64 characters")
    if not any(c.isalpha() for c in value):
        raise ContractError(f"{field} must contain at least one letter and not be all digits")


def _require_bounded_str(value, field: str, max_len: int) -> None:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    if not value:
        raise ContractError(f"{field} must be non-empty")
    if len(value) > max_len:
        raise ContractError(f"{field} exceeds {max_len} characters")


def compute_request_id(request: dict) -> str:
    """SHA-256 over canonical JSON of all request fields except request_id."""
    subset = {}
    for field in REQUEST_FINGERPRINT_FIELDS:
        if field not in request:
            raise ContractError(f"request missing field for fingerprint: {field}")
        subset[field] = request[field]
    return sha256_hex(canonical_json_bytes(subset))


def validate_request(request: dict) -> None:
    if not isinstance(request, dict):
        raise ContractError("request must be an object")
    prohibited = find_prohibited_key(request)
    if prohibited:
        raise ProhibitedKeyError(f"prohibited key in request: {prohibited}")
    extra = set(request) - set(REQUEST_FIELDS)
    if extra:
        raise ContractError(f"unknown request fields: {sorted(extra)}")
    for field in REQUEST_FIELDS:
        if field not in request:
            raise ContractError(f"request missing field: {field}")
    if request["schema_version"] != "1.0":
        raise ContractError("request schema_version must be '1.0'")
    if request["operation"] != "copy_rates_range":
        raise ContractError("operation must be copy_rates_range")
    if not isinstance(request["source_id"], str) or not SLUG_RE.match(request["source_id"]):
        raise ContractError("source_id must be a lowercase safe slug")
    if len(request["source_id"]) > 64:
        raise ContractError("source_id exceeds 64 characters")
    _require_scope(request["account_scope"], "account_scope")
    if not isinstance(request["symbol"], str) or not SYMBOL_RE.match(request["symbol"]):
        raise ContractError("symbol must match ^[A-Z0-9]{3,12}$")
    if request["timeframe"] != "M1":
        raise ContractError("v1 supports only timeframe M1")
    if request["representation"] != "bid_ohlc":
        raise ContractError("v1 supports only representation bid_ohlc")
    start = _parse_minute_utc(request["range_start_utc"], "range_start_utc")
    end = _parse_minute_utc(request["range_end_utc"], "range_end_utc")
    if request["range_semantics"] != "[start,end)":
        raise ContractError("range_semantics must be [start,end)")
    if not (end > start):
        raise ContractError("range_end_utc must be later than range_start_utc")
    if not isinstance(request["request_id"], str) or not SHA256_RE.match(request["request_id"]):
        raise ContractError("request_id must be 64 lowercase hex")
    expected = compute_request_id(request)
    if request["request_id"] != expected:
        raise ContractError("request_id does not match canonical fingerprint")


def validate_response(response: dict) -> None:
    if not isinstance(response, dict):
        raise ContractError("response must be an object")
    prohibited = find_prohibited_key(response)
    if prohibited:
        raise ProhibitedKeyError(f"prohibited key in response: {prohibited}")
    allowed = {
        "schema_version", "request_id", "ok", "source", "symbol", "timeframe",
        "representation", "range_start_utc", "range_end_utc", "range_semantics",
        "exported_at_utc", "bars", "limitations",
    }
    extra = set(response) - allowed
    if extra:
        raise ContractError(f"unknown response fields: {sorted(extra)}")
    for field in allowed:
        if field not in response:
            raise ContractError(f"response missing field: {field}")
    if response["schema_version"] != "1.0":
        raise ContractError("response schema_version must be '1.0'")
    if response["ok"] is not True:
        raise ContractError("response ok must be true (success contract only)")
    if not isinstance(response["request_id"], str) or not SHA256_RE.match(response["request_id"]):
        raise ContractError("response request_id must be 64 lowercase hex")

    source = response["source"]
    src_allowed = {
        "source_id", "account_scope", "broker_reported", "server_reported",
        "account_type", "terminal_build", "adapter_operation",
    }
    if not isinstance(source, dict) or set(source) != src_allowed:
        raise ContractError("response.source has wrong field set")
    if not isinstance(source["source_id"], str) or not SLUG_RE.match(source["source_id"]):
        raise ContractError("source.source_id must be a slug")
    if len(source["source_id"]) > 64:
        raise ContractError("source.source_id exceeds 64 characters")
    _require_scope(source["account_scope"], "source.account_scope")
    if source["account_type"] not in ("demo", "live", "contest"):
        raise ContractError("source.account_type invalid")
    if source["adapter_operation"] != "copy_rates_range":
        raise ContractError("source.adapter_operation must be copy_rates_range")
    _require_bounded_str(source["broker_reported"], "source.broker_reported", 128)
    _require_bounded_str(source["server_reported"], "source.server_reported", 128)
    _require_bounded_str(source["terminal_build"], "source.terminal_build", 64)

    if not isinstance(response["symbol"], str) or not SYMBOL_RE.match(response["symbol"]):
        raise ContractError("response symbol invalid")
    if response["timeframe"] != "M1":
        raise ContractError("response timeframe must be M1")
    if response["representation"] != "bid_ohlc":
        raise ContractError("response representation must be bid_ohlc")
    start = _parse_minute_utc(response["range_start_utc"], "response.range_start_utc")
    end = _parse_minute_utc(response["range_end_utc"], "response.range_end_utc")
    if response["range_semantics"] != "[start,end)":
        raise ContractError("response range_semantics must be [start,end)")
    if not (end > start):
        raise ContractError("response range_end must be later than range_start")
    _parse_instant_utc(response["exported_at_utc"], "exported_at_utc")
    if not isinstance(response["limitations"], list) or not all(
        isinstance(x, str) for x in response["limitations"]
    ):
        raise ContractError("limitations must be a list of strings")

    bars = response["bars"]
    if not isinstance(bars, list) or not bars:
        raise ContractError("bars must be a non-empty list")
    bar_fields = {"sequence", "time_epoch_s", "open", "high", "low", "close"}
    seen_seq: set[int] = set()
    seen_time: set[int] = set()
    prev_time: int | None = None
    start_epoch = int(start.timestamp())
    end_epoch = int(end.timestamp())
    for bar in bars:
        if not isinstance(bar, dict) or set(bar) != bar_fields:
            raise ContractError("bar has wrong field set (only sequence/time/OHLC allowed)")
        seq = bar["sequence"]
        t = bar["time_epoch_s"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ContractError("bar.sequence must be a non-negative integer")
        if not isinstance(t, int) or isinstance(t, bool) or t < 0:
            raise ContractError("bar.time_epoch_s must be a non-negative integer")
        if seq in seen_seq:
            raise ContractError("bar.sequence must be unique")
        if t in seen_time:
            raise ContractError("bar.time_epoch_s must be unique")
        if prev_time is not None and t <= prev_time:
            raise ContractError("bar timestamps must be strictly increasing")
        if not (start_epoch <= t < end_epoch):
            raise ContractError("bar time outside half-open request range")
        o, h, low_, c = bar["open"], bar["high"], bar["low"], bar["close"]
        for name, val in (("open", o), ("high", h), ("low", low_), ("close", c)):
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ContractError(f"bar.{name} must be numeric")
            if not math.isfinite(val):
                raise ContractError(f"bar.{name} must be a finite number")
        if h < low_:
            raise ContractError("bar high must be >= low")
        if not (low_ <= o <= h) or not (low_ <= c <= h):
            raise ContractError("bar open/close must lie within [low, high]")
        seen_seq.add(seq)
        seen_time.add(t)
        prev_time = t


def validate_request_response_match(request: dict, response: dict) -> None:
    """Response must echo the request exactly on the identity/range fields."""
    if response["request_id"] != request["request_id"]:
        raise ContractError("response.request_id does not match request")
    if response["source"]["source_id"] != request["source_id"]:
        raise ContractError("response source_id does not match request")
    if response["source"]["account_scope"] != request["account_scope"]:
        raise ContractError("response account_scope does not match request")
    for field in ("symbol", "timeframe", "representation", "range_start_utc",
                  "range_end_utc", "range_semantics"):
        if response[field] != request[field]:
            raise ContractError(f"response {field} does not match request")
