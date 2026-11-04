"""Strict, dependency-free validation of the GUVFX-TIM Contract v0.3 corrective contracts (GFX-PKT-TIM-C2).

v0.3 corrects the v0.2 foundation for a persistent TIM consumer (GUVFX-TIM-ALIGN-004 / v0.3-RC1, G1-G9):
  G1 canonical analytical candles are PARQUET (NDJSON only as optional raw evidence; NDJSON-only is not
     canonical-import eligible); G2 immutable READY-last releases, receipts live OUTSIDE the release; G3 signed
     hash-bound provenance/eligibility; G4 point-in-time instants kept separate with explicit admissibility;
     G5 supersession (new immutable release, never overwrite); G6 mandatory evidence hash binding; G7 receipts
     ACCEPTED/QUARANTINED/REJECTED; G8 validator pinning (producer + consumer); G9 algorithm-agile signature
     envelope binding release/manifest/contract-bundle/validator-bundle digests.

These pure functions mirror the committed draft-07 schemas in ``research/contracts/v0_3/`` and enforce the
cross-field rules the schemas alone cannot express. The v0.2 validators (``tim_contracts.py``) and the v1
primitives (``contracts.py``) are reused verbatim and NOT modified. Nothing here performs any MT5, broker, NAS,
network, credential or trading action; the signature boundary is an INJECTED SYNTHETIC placeholder.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .contracts import (  # v1 primitives — reused verbatim
    ContractError,
    ProhibitedKeyError,
    SHA256_RE,
    SLUG_RE,
    parse_canonical_utc_instant,
    sha256_hex,
)
from .tim_contracts import (  # v0.2 helpers/constants — reused verbatim, not duplicated
    ASSET_CLASSES,
    CANONICAL_SYMBOL_RE,
    LICENSING,
    REL_PATH_RE,
    TIMEFRAMES,
    QuarantineRequired,
    _TIMEFRAME_SECONDS,
    _array,
    _bool,
    _const,
    _exact_keys,
    _instant,
    _int,
    _minute_epoch,
    _no_prohibited,
    _str,
    _str_array,
)

CONTRACT_VERSION = "0.3"
SYNTHETIC_ALGORITHM_V2 = "synthetic-placeholder-v0"
SIGNATURE_DOMAIN_SEPARATOR = "GUVFX-TIM-RELEASE-SIGNATURE-v2"

# The 7 additive v0.3 schema files (relative to research/contracts/v0_3/), for the hash-regression evidence.
TIM_V0_3_SCHEMA_FILES = (
    "canonical_candles_parquet_v1", "dataset_release_manifest_v3", "dataset_release_signature_v2",
    "dataset_import_receipt_v2", "release_ready_marker_v1", "source_eligibility_v1",
    "release_supersession_v1",
)

# ==================================================================================================
# G1 — canonical Parquet candle column contract
# ==================================================================================================
# (name, duckdb_type, nullable). Mandatory columns are non-null; optional columns may be absent, but
# when present MUST match the declared type and nullability. UTC timestamps are microsecond-precision,
# UTC-adjusted; OHLC are decimal128(38,18); tick_volume int64; quality_flags uint32.
CANON_CANDLE_MANDATORY = (
    ("event_start_utc", "TIMESTAMP", False),
    ("event_end_utc", "TIMESTAMP", False),
    ("open", "DECIMAL(38,18)", False),
    ("high", "DECIMAL(38,18)", False),
    ("low", "DECIMAL(38,18)", False),
    ("close", "DECIMAL(38,18)", False),
    ("tick_volume", "BIGINT", False),
    ("quality_flags", "UINTEGER", False),
)
CANON_CANDLE_OPTIONAL = (
    ("real_volume", "DECIMAL(38,18)", True),
    ("spread_points", "INTEGER", True),
    ("source_available_at_utc", "TIMESTAMP", True),
    ("source_sequence", "BIGINT", True),
)
_MANDATORY_NAMES = tuple(n for n, _, _ in CANON_CANDLE_MANDATORY)
_OPTIONAL_NAMES = tuple(n for n, _, _ in CANON_CANDLE_OPTIONAL)
_CANDLE_TYPES = {n: t for n, t, _ in (*CANON_CANDLE_MANDATORY, *CANON_CANDLE_OPTIONAL)}
_UINT32_MAX = 2 ** 32 - 1
_INT32_MIN, _INT32_MAX = -(2 ** 31), 2 ** 31 - 1
CANDLE_UNIQUENESS_KEY = ("event_start_utc",)


def validate_canonical_candle_parquet_schema(observed) -> None:
    """Validate an OBSERVED Parquet schema against the canonical candle contract.

    ``observed`` is an ordered list of {"name": str, "type": str (DuckDB type), "nullable": bool} as read back
    from the Parquet file (e.g. via DuckDB ``DESCRIBE``). Enforces: every mandatory column present with the exact
    type; every optional column, if present, with the exact type; no unknown columns; mandatory columns appear
    first and in contract order (so the canonical surface is unambiguous and import-stable). NOTE: Parquet
    physical nullability is advisory only — DuckDB writes every column as OPTIONAL regardless of a NOT NULL
    source constraint, so the non-null guarantee for mandatory columns is enforced at the ROW level by
    ``validate_canonical_candle_rows`` (every mandatory value present), not from the Parquet flag."""
    if not isinstance(observed, (list, tuple)) or not observed:
        raise ContractError("parquet_schema: observed schema must be a non-empty ordered column list")
    names = []
    by_name = {}
    for i, col in enumerate(observed):
        if not isinstance(col, dict) or set(col) != {"name", "type", "nullable"}:
            raise ContractError(f"parquet_schema[{i}]: each column needs exactly name/type/nullable")
        name = col["name"]
        if name in by_name:
            raise ContractError(f"parquet_schema: duplicate column {name!r}")
        names.append(name)
        by_name[name] = col
    unknown = [n for n in names if n not in _CANDLE_TYPES]
    if unknown:
        raise ContractError(f"parquet_schema: unknown column(s) {unknown}")
    for n, t, _nullable in CANON_CANDLE_MANDATORY:
        if n not in by_name:
            raise ContractError(f"parquet_schema: missing mandatory column {n!r}")
        col = by_name[n]
        if str(col["type"]).upper() != t:
            raise ContractError(f"parquet_schema: {n} must be {t}, got {col['type']!r}")
        if not isinstance(col["nullable"], bool):
            raise ContractError(f"parquet_schema: {n!r} nullable must be a boolean")
    for n, t, _nullable in CANON_CANDLE_OPTIONAL:
        if n in by_name and str(by_name[n]["type"]).upper() != t:
            raise ContractError(f"parquet_schema: optional {n} must be {t}, got {by_name[n]['type']!r}")
    # Mandatory columns first and in contract order.
    head = names[: len(_MANDATORY_NAMES)]
    if tuple(head) != _MANDATORY_NAMES:
        raise ContractError("parquet_schema: mandatory columns must appear first, in contract order")


def _as_decimal(value, where):
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)  # a Python int is always finite
    elif isinstance(value, str):
        try:
            d = Decimal(value)
        except Exception as exc:  # noqa: BLE001
            raise ContractError(f"candle.{where}: not a decimal") from exc
    else:
        raise ContractError(f"candle.{where}: OHLC must be decimal, got {type(value).__name__}")
    if not d.is_finite():  # fail CLOSED with a ContractError (never let NaN/Infinity through, never raise InvalidOperation)
        raise ContractError(f"candle.{where}: must be finite (no NaN/Infinity)")
    return d


def _reject_control_strings(obj, where) -> None:
    """Reject any string anywhere in a v0.3 payload that carries a C0 control character (newline/CR/TAB/...).
    Closes the trailing-newline strictness gap (anchored regexes + ``re.match`` would otherwise admit a single
    trailing ``\\n``) uniformly for every v0.3 field, without modifying the shared v0.2 primitives."""
    stack = [(where, obj)]
    while stack:
        w, o = stack.pop()
        if isinstance(o, str):
            if any(ord(ch) < 0x20 for ch in o):
                raise ContractError(f"{w}: control character not permitted in a contract string")
        elif isinstance(o, dict):
            for k, v in o.items():
                stack.append((f"{w}.{k}", v))
        elif isinstance(o, list):
            for i, v in enumerate(o):
                stack.append((f"{w}[{i}]", v))


def validate_canonical_candle_rows(rows, *, timeframe="M1") -> int:
    """Validate decoded canonical candle rows (as read back from Parquet via DuckDB).

    Each row is a dict keyed by the canonical column names. Enforces, per G1: half-open M1 interval
    (event_end == event_start + timeframe); strictly increasing, unique event_start (the uniqueness key);
    OHLC invariants; non-negative integer volumes in range; optional columns typed; and, when present,
    source_available_at_utc >= event_end (the source cannot publish a bar before it closes). Returns the count."""
    if timeframe not in _TIMEFRAME_SECONDS:
        raise ContractError(f"candle: unknown timeframe {timeframe!r}")
    step_us = _TIMEFRAME_SECONDS[timeframe] * 1_000_000
    prev_start = None
    seen = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ContractError(f"candle[{i}]: row must be a mapping")
        missing = [n for n in _MANDATORY_NAMES if n not in row]
        if missing:
            raise ContractError(f"candle[{i}]: missing mandatory column(s) {missing}")
        unknown = [n for n in row if n not in _CANDLE_TYPES]
        if unknown:
            raise ContractError(f"candle[{i}]: unknown column(s) {unknown}")
        for n in _MANDATORY_NAMES:
            if row[n] is None:
                raise ContractError(f"candle[{i}].{n}: mandatory column must be non-null")
        s = _epoch_us_of(row["event_start_utc"], f"candle[{i}].event_start_utc")
        e = _epoch_us_of(row["event_end_utc"], f"candle[{i}].event_end_utc")
        if e != s + step_us:
            raise ContractError(f"candle[{i}]: event_end must equal event_start + {timeframe}")
        if s % step_us != 0:
            raise ContractError(f"candle[{i}]: event_start must be aligned to the {timeframe} grid")
        if s in seen:
            raise ContractError(f"candle[{i}]: duplicate event_start (uniqueness key violated)")
        seen.add(s)
        if prev_start is not None and s <= prev_start:
            raise ContractError(f"candle[{i}]: rows must be strictly increasing by event_start")
        prev_start = s
        o = _as_decimal(row["open"], f"[{i}].open")
        h = _as_decimal(row["high"], f"[{i}].high")
        low = _as_decimal(row["low"], f"[{i}].low")
        c = _as_decimal(row["close"], f"[{i}].close")
        if not (h >= o and h >= c and h >= low):
            raise ContractError(f"candle[{i}]: high must be >= open, close and low")
        if not (low <= o and low <= c and low <= h):
            raise ContractError(f"candle[{i}]: low must be <= open, close and high")
        tv = row["tick_volume"]
        if not isinstance(tv, int) or isinstance(tv, bool) or tv < 0:
            raise ContractError(f"candle[{i}].tick_volume: must be a non-negative int64")
        qf = row["quality_flags"]
        if not isinstance(qf, int) or isinstance(qf, bool) or not (0 <= qf <= _UINT32_MAX):
            raise ContractError(f"candle[{i}].quality_flags: must be a uint32")
        if row.get("real_volume") is not None:
            if _as_decimal(row["real_volume"], f"[{i}].real_volume") < 0:
                raise ContractError(f"candle[{i}].real_volume: must be >= 0")
        if row.get("spread_points") is not None:
            sp = row["spread_points"]
            if not isinstance(sp, int) or isinstance(sp, bool) or not (_INT32_MIN <= sp <= _INT32_MAX) or sp < 0:
                raise ContractError(f"candle[{i}].spread_points: must be a non-negative int32")
        if row.get("source_sequence") is not None:
            ss = row["source_sequence"]
            if not isinstance(ss, int) or isinstance(ss, bool):
                raise ContractError(f"candle[{i}].source_sequence: must be an int64")
        if row.get("source_available_at_utc") is not None:
            avail = _epoch_us_of(row["source_available_at_utc"], f"candle[{i}].source_available_at_utc")
            if avail < e:
                raise ContractError(f"candle[{i}]: source_available_at_utc must be >= event_end (bar not closed)")
    return len(rows)


def _epoch_us_of(value, where):
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(round(dt.astimezone(timezone.utc).timestamp() * 1_000_000))
    if isinstance(value, str):
        inst = parse_canonical_utc_instant(value)  # validates format + raises on bad calendar
        # Derive epoch microseconds from the canonical string deterministically.
        base = value[:19]  # 'YYYY-MM-DDTHH:MM:SS'
        dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        us = 0
        if "." in value:
            frac = value[value.index(".") + 1: -1]
            us = int((frac + "000000")[:6])
        _ = inst
        return int(dt.timestamp()) * 1_000_000 + us
    raise ContractError(f"{where}: unsupported timestamp type {type(value).__name__}")


def validate_canonical_candles_parquet_contract(obj) -> None:
    """Validate the committed canonical_candles_parquet_v1 CONTRACT DESCRIPTOR document (not a Parquet file)."""
    _no_prohibited(obj, "parquet_contract")
    _reject_control_strings(obj, "parquet_contract")
    if not isinstance(obj, dict):
        raise ContractError("parquet_contract must be an object")
    _const(obj, "schema_version", "parquet_contract", "1.0")
    _const(obj, "canonical_format", "parquet_contract", "parquet")
    _str(obj, "timeframe_pilot", "parquet_contract", enum=("M1",))
    _const(obj, "utc_representation", "parquet_contract", "microseconds_since_epoch_utc_adjusted")
    _const(obj, "interval_semantics", "parquet_contract", "[start,end)")
    _const(obj, "row_order", "parquet_contract", "ascending_by_event_start_utc")
    uk = _str_array(obj, "uniqueness_key", "parquet_contract")
    if tuple(uk) != list(CANDLE_UNIQUENESS_KEY) and uk != list(CANDLE_UNIQUENESS_KEY):
        raise ContractError("parquet_contract: uniqueness_key must be ['event_start_utc']")
    mand = _array(obj, "mandatory_columns", "parquet_contract")
    opt = _array(obj, "optional_columns", "parquet_contract")
    got_mand = tuple((c.get("name"), str(c.get("duckdb_type")).upper(), c.get("nullable")) for c in mand)
    if got_mand != CANON_CANDLE_MANDATORY:
        raise ContractError("parquet_contract: mandatory_columns must match the canonical candle contract exactly")
    got_opt = tuple((c.get("name"), str(c.get("duckdb_type")).upper(), c.get("nullable")) for c in opt)
    if got_opt != CANON_CANDLE_OPTIONAL:
        raise ContractError("parquet_contract: optional_columns must match the canonical candle contract exactly")
    _str_array(obj, "limitations", "parquet_contract")


# ==================================================================================================
# G9 — signature envelope v2 (algorithm-agile; injected synthetic signer)
# ==================================================================================================
SIGNATURE_V2_FIELDS = ("schema_version", "domain_separator", "release_id", "signed_manifest_path",
                       "manifest_digest", "contract_bundle_digest", "validator_bundle_digest",
                       "producer_identity", "signing_key_id", "public_key_fingerprint", "algorithm",
                       "signature_scheme", "signature_value", "synthetic", "created_at_utc", "key_validity",
                       "limitations")


def synthetic_sign_v2(*, release_id, manifest_digest, contract_bundle_digest, validator_bundle_digest,
                      producer_identity, signing_key_id, public_key_fingerprint) -> str:
    """INJECTED SYNTHETIC signer: a deterministic, clearly non-cryptographic digest over a domain-separated
    concatenation of the G9-bound identities/digests (incl. producer identity). NOT a real signature; the
    operational algorithm is NOT chosen."""
    material = b"\x00".join([
        SIGNATURE_DOMAIN_SEPARATOR.encode("utf-8"), release_id.encode("utf-8"),
        manifest_digest.encode("utf-8"), contract_bundle_digest.encode("utf-8"),
        validator_bundle_digest.encode("utf-8"), producer_identity.encode("utf-8"),
        signing_key_id.encode("utf-8"), public_key_fingerprint.encode("utf-8"),
    ])
    return sha256_hex(material)


def validate_release_signature_v2(obj) -> None:
    _no_prohibited(obj, "signature_v2")
    _reject_control_strings(obj, "signature_v2")
    _exact_keys(obj, SIGNATURE_V2_FIELDS, "signature_v2")
    _const(obj, "schema_version", "signature_v2", "2.0")
    _const(obj, "domain_separator", "signature_v2", SIGNATURE_DOMAIN_SEPARATOR)
    _str(obj, "release_id", "signature_v2", regex=SLUG_RE, max_len=64)
    _str(obj, "signed_manifest_path", "signature_v2", regex=REL_PATH_RE, max_len=512)
    for f in ("manifest_digest", "contract_bundle_digest", "validator_bundle_digest", "public_key_fingerprint"):
        _str(obj, f, "signature_v2", regex=SHA256_RE, min_len=64, max_len=64)
    _str(obj, "producer_identity", "signature_v2", regex=SLUG_RE, max_len=64)
    _str(obj, "signing_key_id", "signature_v2", regex=SLUG_RE, max_len=64)
    _str(obj, "algorithm", "signature_v2", max_len=64)
    _const(obj, "signature_scheme", "signature_v2", "detached")
    _str(obj, "signature_value", "signature_v2", regex=SHA256_RE, min_len=64, max_len=64)
    # Repository pilot: the signature MUST be the injected synthetic placeholder — no operational algorithm.
    if obj["synthetic"] is not True:
        raise ContractError("signature_v2: synthetic must be true (no operational signing in this packet)")
    if obj["algorithm"] != SYNTHETIC_ALGORITHM_V2:
        raise ContractError(f"signature_v2: algorithm must be {SYNTHETIC_ALGORITHM_V2!r} in the repository pilot")
    _instant(obj["created_at_utc"], "created_at_utc", "signature_v2")
    kv = obj["key_validity"]
    if kv is not None:
        _exact_keys(kv, ("not_before_utc", "not_after_utc"), "signature_v2.key_validity")
        nb = _instant(kv["not_before_utc"], "not_before_utc", "signature_v2.key_validity")
        na = _instant(kv["not_after_utc"], "not_after_utc", "signature_v2.key_validity")
        if na < nb:
            raise ContractError("signature_v2.key_validity: not_after_utc must be >= not_before_utc")
    _str_array(obj, "limitations", "signature_v2")


def verify_release_signature_v2(signature, manifest_bytes, *, verifier=synthetic_sign_v2) -> bool:
    """Structure-validate, bind the manifest digest, and verify the value via an INJECTED verifier (synthetic
    default). Fail-closed: returns False on any digest/value mismatch; raises on structural error."""
    validate_release_signature_v2(signature)
    if sha256_hex(bytes(manifest_bytes)) != signature["manifest_digest"]:
        return False
    expected = verifier(
        release_id=signature["release_id"], manifest_digest=signature["manifest_digest"],
        contract_bundle_digest=signature["contract_bundle_digest"],
        validator_bundle_digest=signature["validator_bundle_digest"],
        producer_identity=signature["producer_identity"],
        signing_key_id=signature["signing_key_id"], public_key_fingerprint=signature["public_key_fingerprint"])
    return expected == signature["signature_value"]


# ==================================================================================================
# G2 — READY marker v1
# ==================================================================================================
READY_MARKER_FIELDS = ("schema_version", "release_id", "state", "manifest_digest", "signature_envelope_digest",
                       "contract_version", "synthetic", "created_at_utc", "limitations")


def validate_ready_marker(obj) -> None:
    _no_prohibited(obj, "ready_marker")
    _reject_control_strings(obj, "ready_marker")
    _exact_keys(obj, READY_MARKER_FIELDS, "ready_marker")
    _const(obj, "schema_version", "ready_marker", "1.0")
    _str(obj, "release_id", "ready_marker", regex=SLUG_RE, max_len=64)
    _const(obj, "state", "ready_marker", "READY")
    _str(obj, "manifest_digest", "ready_marker", regex=SHA256_RE, min_len=64, max_len=64)
    _str(obj, "signature_envelope_digest", "ready_marker", regex=SHA256_RE, min_len=64, max_len=64)
    _const(obj, "contract_version", "ready_marker", CONTRACT_VERSION)
    _bool(obj, "synthetic", "ready_marker")
    _instant(obj["created_at_utc"], "created_at_utc", "ready_marker")
    _str_array(obj, "limitations", "ready_marker")


# ==================================================================================================
# G3 — source eligibility v1
# ==================================================================================================
ELIGIBILITY_FIELDS = ("schema_version", "source_id", "broker_id", "server_identity", "source_account_reference",
                      "eligibility_status", "licensing_classification", "licensing_reference", "permitted_use",
                      "capacity_certified", "capacity_certification_ref", "synthetic", "non_operational",
                      "approved_at_utc", "limitations")


def validate_source_eligibility(obj) -> None:
    _no_prohibited(obj, "eligibility")
    _reject_control_strings(obj, "eligibility")
    _exact_keys(obj, ELIGIBILITY_FIELDS, "eligibility")
    _const(obj, "schema_version", "eligibility", "1.0")
    _str(obj, "source_id", "eligibility", regex=SLUG_RE, max_len=64)
    if obj["broker_id"] is not None:
        _str(obj, "broker_id", "eligibility", regex=SLUG_RE, max_len=64)
    _str(obj, "server_identity", "eligibility", max_len=160)
    _str(obj, "source_account_reference", "eligibility", max_len=160)
    status = _str(obj, "eligibility_status", "eligibility", enum=("APPROVED", "PENDING", "DENIED"))
    lic = _str(obj, "licensing_classification", "eligibility", enum=LICENSING)
    if obj["licensing_reference"] is not None:
        _str(obj, "licensing_reference", "eligibility", max_len=512)
    _str(obj, "permitted_use", "eligibility", enum=("internal_research_only", "licensed_redistribution"))
    _bool(obj, "capacity_certified", "eligibility")
    if obj["capacity_certification_ref"] is not None:
        _str(obj, "capacity_certification_ref", "eligibility", max_len=512)
    synthetic = _bool(obj, "synthetic", "eligibility")
    non_operational = _bool(obj, "non_operational", "eligibility")
    if obj["approved_at_utc"] is not None:
        _instant(obj["approved_at_utc"], "approved_at_utc", "eligibility")
    _str_array(obj, "limitations", "eligibility")
    # No invented real licensing: a synthetic record is non-operational and internal-research-only.
    if synthetic:
        if not non_operational:
            raise ContractError("eligibility: a synthetic record must be non_operational")
        if lic != "internal_research_only" or obj["permitted_use"] != "internal_research_only":
            raise ContractError("eligibility: a synthetic record must be internal_research_only")
    if status == "APPROVED":
        if lic == "unlicensed_quarantine" or obj["licensing_reference"] is None:
            raise QuarantineRequired("eligibility: APPROVED requires a licensing_reference and license -> quarantine")
        if not obj["capacity_certified"] or obj["capacity_certification_ref"] is None:
            raise QuarantineRequired("eligibility: APPROVED requires capacity certification -> quarantine")
        if obj["approved_at_utc"] is None:
            raise ContractError("eligibility: APPROVED requires approved_at_utc")
    else:
        raise QuarantineRequired(f"eligibility: {status} source is not release-eligible -> quarantine")


# ==================================================================================================
# G5 — supersession v1
# ==================================================================================================
SUPERSESSION_FIELDS = ("schema_version", "release_id", "supersedes_release_id", "reason", "scope",
                       "correction_window", "created_at_utc", "synthetic", "limitations")


def validate_release_supersession(obj) -> None:
    _no_prohibited(obj, "supersession")
    _reject_control_strings(obj, "supersession")
    _exact_keys(obj, SUPERSESSION_FIELDS, "supersession")
    _const(obj, "schema_version", "supersession", "1.0")
    rid = _str(obj, "release_id", "supersession", regex=SLUG_RE, max_len=64)
    sid = _str(obj, "supersedes_release_id", "supersession", regex=SLUG_RE, max_len=64)
    _str(obj, "reason", "supersession", max_len=512)
    scope = _str(obj, "scope", "supersession", enum=("FULL", "PARTIAL_WINDOW", "CORRECTION"))
    cw = obj["correction_window"]
    _instant(obj["created_at_utc"], "created_at_utc", "supersession")
    _bool(obj, "synthetic", "supersession")
    _str_array(obj, "limitations", "supersession")
    if rid == sid:
        raise ContractError("supersession: a release may not supersede itself (loop guard)")
    if scope == "PARTIAL_WINDOW":
        if not isinstance(cw, dict):
            raise ContractError("supersession: PARTIAL_WINDOW requires a correction_window")
        _exact_keys(cw, ("range_start_utc", "range_end_utc"), "supersession.correction_window")
        rs = _minute_epoch(cw["range_start_utc"], "range_start_utc", "supersession.correction_window")
        re_ = _minute_epoch(cw["range_end_utc"], "range_end_utc", "supersession.correction_window")
        if re_ <= rs:
            raise ContractError("supersession.correction_window: end must be after start (half-open)")
    elif cw is not None:
        raise ContractError(f"supersession: {scope} must not carry a correction_window")


# ==================================================================================================
# G2/G3/G4/G6/G8 — release manifest v3
# ==================================================================================================
_EVIDENCE_ROLES = ("source_eligibility", "broker_server_identity", "instrument_mapping", "timezone_evidence",
                   "licensing", "quality_report", "gap_report", "watermark")
_MEDIA_PARQUET = "application/vnd.apache.parquet"
_MEDIA_NDJSON = "application/x-ndjson"
# The signed set. The detached signature and the READY marker are EXTERNAL artefacts written AFTER the manifest
# (they bind the manifest digest), so they are deliberately NOT members of files[] — this avoids a circular
# digest (a file hash feeding the very manifest digest the file is derived from).
_FILE_ROLES = ("canonical_parquet", "raw_ndjson", "quality_report", "gap_report", "watermark",
               "source_eligibility", "broker_server_identity", "instrument_mapping", "timezone_evidence",
               "licensing", "supersession")
RELEASE_V3_FIELDS = ("schema_version", "contract_version", "release_id", "supersedes_release_id", "supersession",
                     "source_id", "canonical_symbol", "broker_native_symbol", "asset_class", "timeframe",
                     "representation", "price_basis", "provenance", "derivation", "canonical_format",
                     "range_start_utc", "range_end_utc", "range_semantics", "point_in_time", "row_count",
                     "uniqueness_key", "files", "evidence", "validator_pinning", "contract_bundle_digest",
                     "validator_bundle_digest", "release_completeness", "partial_release_allowed",
                     "producer_identity", "producer_version", "signing_key_id", "signature_path", "ready_marker",
                     "licensing_classification", "synthetic", "limitations")
PIT_FIELDS = ("event_start_utc", "event_end_utc", "source_available_at_utc", "source_availability_classification",
              "collector_observed_at_utc", "ingested_at_utc", "validated_at_utc", "published_at_utc")
FILE_FIELDS = ("relative_path", "media_type", "byte_size", "digest", "logical_role", "schema_version",
               "validator_version", "row_count")
PINNING_FIELDS = ("schema_version", "validator_id", "validator_version", "validator_digest", "compatibility_range")
RM3_FIELDS = ("present", "written_last", "ready_marker_path")
DERIVATION_FIELDS = ("source_timeframe", "derivation_version", "calendar_id", "calendar_version", "bucket_policy",
                     "session_timezone", "dst_policy", "price_basis")


def _assert_instant_chain(pairs, where):
    for i in range(1, len(pairs)):
        (pn, pv), (cn, cv) = pairs[i - 1], pairs[i]
        if cv < pv:
            raise ContractError(f"{where}: {cn} must be >= {pn} (point-in-time chain out of order)")


def _digest(obj, where):
    if not isinstance(obj, dict):
        raise ContractError(f"{where}: digest must be an object")
    _exact_keys(obj, ("algorithm", "value"), where)
    _const(obj, "algorithm", where, "sha256")
    _str(obj, "value", where, regex=SHA256_RE, min_len=64, max_len=64)
    return obj["value"]


def _evidence_ref(obj, where):
    if not isinstance(obj, dict):
        raise ContractError(f"{where}: evidence ref must be an object")
    _exact_keys(obj, ("contract_version", "relative_path", "sha256"), where)
    _str(obj, "contract_version", where, max_len=16)
    _str(obj, "relative_path", where, regex=REL_PATH_RE, max_len=512)
    _str(obj, "sha256", where, regex=SHA256_RE, min_len=64, max_len=64)
    return obj


def validate_release_manifest_v3(obj) -> None:
    _no_prohibited(obj, "release")
    _reject_control_strings(obj, "release")
    _exact_keys(obj, RELEASE_V3_FIELDS, "release")
    _const(obj, "schema_version", "release", "3.0")
    _const(obj, "contract_version", "release", CONTRACT_VERSION)
    rid = _str(obj, "release_id", "release", regex=SLUG_RE, max_len=64)
    # G5 supersession pairing.
    sup_id = obj["supersedes_release_id"]
    sup = obj["supersession"]
    if sup_id is not None:
        _str(obj, "supersedes_release_id", "release", regex=SLUG_RE, max_len=64)
        if sup_id == rid:
            raise ContractError("release: supersedes_release_id must differ from release_id (loop guard)")
        if not isinstance(sup, dict):
            raise ContractError("release: supersedes_release_id requires a supersession object")
        _exact_keys(sup, ("reason", "scope", "supersession_ref"), "release.supersession")
        _str(sup, "reason", "release.supersession", max_len=512)
        _str(sup, "scope", "release.supersession", enum=("FULL", "PARTIAL_WINDOW", "CORRECTION"))
        _evidence_ref(sup["supersession_ref"], "release.supersession.supersession_ref")
    elif sup is not None:
        raise ContractError("release: supersession must be null when supersedes_release_id is null")
    _str(obj, "source_id", "release", regex=SLUG_RE, max_len=64)
    _str(obj, "canonical_symbol", "release", regex=CANONICAL_SYMBOL_RE)
    _str(obj, "broker_native_symbol", "release", max_len=64)
    _str(obj, "asset_class", "release", enum=ASSET_CLASSES)
    _str(obj, "timeframe", "release", enum=TIMEFRAMES)
    _str(obj, "representation", "release", enum=("bid_ohlc",))
    _str(obj, "price_basis", "release", enum=("bid",))
    provenance = _str(obj, "provenance", "release", enum=("NATIVE", "DERIVED"))
    _const(obj, "canonical_format", "release", "parquet")
    rs = _minute_epoch(obj["range_start_utc"], "range_start_utc", "release")
    re_ = _minute_epoch(obj["range_end_utc"], "range_end_utc", "release")
    _const(obj, "range_semantics", "release", "[start,end)")
    if re_ <= rs:
        raise ContractError("release: range_end_utc must be after range_start_utc (half-open)")
    # Provenance / derivation.
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
        src_s, tgt_s = _TIMEFRAME_SECONDS[d["source_timeframe"]], _TIMEFRAME_SECONDS[obj["timeframe"]]
        if not (tgt_s > src_s and tgt_s % src_s == 0):
            raise ContractError("release: DERIVED timeframe must be strictly coarser than, and an integer "
                                "multiple of, source_timeframe")
    elif obj["derivation"] is not None:
        raise ContractError("release: NATIVE must not carry a derivation object")
    # G4 point-in-time block.
    pit = obj["point_in_time"]
    if not isinstance(pit, dict):
        raise ContractError("release.point_in_time must be an object")
    _exact_keys(pit, PIT_FIELDS, "release.point_in_time")
    ev_s = _minute_epoch(pit["event_start_utc"], "event_start_utc", "release.point_in_time")
    ev_e = _minute_epoch(pit["event_end_utc"], "event_end_utc", "release.point_in_time")
    if ev_e <= ev_s:
        raise ContractError("release.point_in_time: event_end must be after event_start (half-open)")
    classification = _str(pit, "source_availability_classification", "release.point_in_time",
                          enum=("EVIDENCED", "UNKNOWN_RETROSPECTIVE"))
    observed = _instant(pit["collector_observed_at_utc"], "collector_observed_at_utc", "release.point_in_time")
    ingested = _instant(pit["ingested_at_utc"], "ingested_at_utc", "release.point_in_time")
    validated = _instant(pit["validated_at_utc"], "validated_at_utc", "release.point_in_time")
    published = _instant(pit["published_at_utc"], "published_at_utc", "release.point_in_time")
    ev_e_inst = parse_canonical_utc_instant(pit["event_end_utc"])
    if classification == "EVIDENCED":
        if pit["source_available_at_utc"] is None:
            raise ContractError("release.point_in_time: EVIDENCED requires a source_available_at_utc")
        avail = _instant(pit["source_available_at_utc"], "source_available_at_utc", "release.point_in_time")
        _assert_instant_chain([("event_end_utc", ev_e_inst), ("source_available_at_utc", avail),
                               ("collector_observed_at_utc", observed), ("ingested_at_utc", ingested),
                               ("validated_at_utc", validated), ("published_at_utc", published)],
                              "release.point_in_time")
    else:  # UNKNOWN_RETROSPECTIVE: no PIT claim; source availability must be explicitly unknown (null).
        if pit["source_available_at_utc"] is not None:
            raise ContractError("release.point_in_time: UNKNOWN_RETROSPECTIVE must leave source_available_at_utc null")
        _assert_instant_chain([("event_end_utc", ev_e_inst), ("collector_observed_at_utc", observed),
                               ("ingested_at_utc", ingested), ("validated_at_utc", validated),
                               ("published_at_utc", published)], "release.point_in_time")
    _int(obj, "row_count", "release", minimum=0)
    uk = _str_array(obj, "uniqueness_key", "release")
    if not uk:
        raise ContractError("release: uniqueness_key must be non-empty")
    # Files + hash binding.
    files = _array(obj, "files", "release")
    if not files:
        raise ContractError("release: files must be non-empty")
    file_by_role = {}
    file_by_path = {}
    canonical_parquet = None
    for i, f in enumerate(files):
        fw = f"release.files[{i}]"
        _exact_keys(f, FILE_FIELDS, fw)
        path = _str(f, "relative_path", fw, regex=REL_PATH_RE, max_len=512)
        media = _str(f, "media_type", fw, enum=(_MEDIA_PARQUET, _MEDIA_NDJSON, "application/json", "text/plain"))
        _int(f, "byte_size", fw, minimum=0)
        dval = _digest(f["digest"], f"{fw}.digest")
        role = _str(f, "logical_role", fw, enum=_FILE_ROLES)
        _str(f, "schema_version", fw, max_len=16)
        _str(f, "validator_version", fw, max_len=16)
        _int(f, "row_count", fw, minimum=0, nullable=True)
        if path in file_by_path:
            raise ContractError(f"{fw}: duplicate relative_path {path!r}")
        file_by_path[path] = {"role": role, "sha256": dval, "media": media, "row_count": f["row_count"]}
        file_by_role.setdefault(role, []).append(path)
        if role == "canonical_parquet":
            if media != _MEDIA_PARQUET:
                raise ContractError(f"{fw}: canonical_parquet must be {_MEDIA_PARQUET}")
            canonical_parquet = f
        if role == "raw_ndjson" and media != _MEDIA_NDJSON:
            raise ContractError(f"{fw}: raw_ndjson must be {_MEDIA_NDJSON}")
    # G1: a canonical release MUST carry exactly one canonical Parquet; NDJSON alone is not canonical-eligible.
    if canonical_parquet is None:
        raise ContractError("release: a canonical release requires a canonical_parquet file (NDJSON-only rejected)")
    if len(file_by_role.get("canonical_parquet", [])) != 1:
        raise ContractError("release: exactly one canonical_parquet file is permitted")
    # G6/G3: every evidence role is present, hash-bound and backed by a release file of the matching role+digest.
    evidence = obj["evidence"]
    if not isinstance(evidence, dict):
        raise ContractError("release.evidence must be an object")
    _exact_keys(evidence, _EVIDENCE_ROLES, "release.evidence")
    for role in _EVIDENCE_ROLES:
        ref = _evidence_ref(evidence[role], f"release.evidence.{role}")
        paths = file_by_role.get(role, [])
        if ref["relative_path"] not in paths:
            raise ContractError(f"release.evidence.{role}: must reference a release file of role {role!r}")
        bound = file_by_path[ref["relative_path"]]
        if bound["sha256"] != ref["sha256"]:
            raise ContractError(f"release.evidence.{role}: sha256 must match the bound file digest")
    # G8 validator pinning.
    pin = obj["validator_pinning"]
    if not isinstance(pin, dict):
        raise ContractError("release.validator_pinning must be an object")
    _exact_keys(pin, PINNING_FIELDS, "release.validator_pinning")
    _const(pin, "schema_version", "release.validator_pinning", "3.0")
    _str(pin, "validator_id", "release.validator_pinning", regex=SLUG_RE, max_len=64)
    _str(pin, "validator_version", "release.validator_pinning", max_len=32)
    _str(pin, "validator_digest", "release.validator_pinning", regex=SHA256_RE, min_len=64, max_len=64)
    _str(pin, "compatibility_range", "release.validator_pinning", max_len=32)
    _str(obj, "contract_bundle_digest", "release", regex=SHA256_RE, min_len=64, max_len=64)
    _str(obj, "validator_bundle_digest", "release", regex=SHA256_RE, min_len=64, max_len=64)
    completeness = _str(obj, "release_completeness", "release", enum=("COMPLETE", "INCOMPLETE"))
    if obj["partial_release_allowed"] is not False:
        raise ContractError("release: partial_release_allowed must be false (pilot prohibits partial releases)")
    _str(obj, "producer_identity", "release", regex=SLUG_RE, max_len=64)
    _str(obj, "producer_version", "release", max_len=64)
    _str(obj, "signing_key_id", "release", regex=SLUG_RE, max_len=64)
    _str(obj, "signature_path", "release", regex=REL_PATH_RE, max_len=512)
    _str(obj, "licensing_classification", "release", enum=LICENSING)
    _bool(obj, "synthetic", "release")
    _str_array(obj, "limitations", "release")
    # G2 READY marker: present+last for a COMPLETE release; bound file entry matching path+sha; parquet rows > 0.
    rm = obj["ready_marker"]
    if not isinstance(rm, dict):
        raise ContractError("release.ready_marker must be an object")
    _exact_keys(rm, RM3_FIELDS, "release.ready_marker")
    present = _bool(rm, "present", "release.ready_marker")
    written_last = _bool(rm, "written_last", "release.ready_marker")
    _str(rm, "ready_marker_path", "release.ready_marker", regex=REL_PATH_RE, max_len=512)
    if completeness == "COMPLETE":
        if not (present and written_last):
            raise ContractError("release: COMPLETE requires a READY marker present AND written last")
        if obj["row_count"] <= 0:
            raise ContractError("release: COMPLETE requires row_count > 0 (no empty release)")
        if (canonical_parquet["row_count"] or 0) <= 0:
            raise ContractError("release: COMPLETE requires the canonical_parquet file to have row_count > 0")


# ==================================================================================================
# G7 — import receipt v2 (consumer-owned)
# ==================================================================================================
IMPORT_RECEIPT_V2_FIELDS = ("schema_version", "release_id", "source_release_manifest_sha256",
                            "source_signature_envelope_sha256", "consumer_identity", "consumer_validator_id",
                            "consumer_validator_version", "consumer_validator_digest", "checks", "import_state",
                            "reason_code", "retryable", "research_admissible", "superseded_by_receipt_id",
                            "imported_at_utc", "synthetic", "limitations")
CHECK_FIELDS = ("signature_verified", "manifest_hashes_verified", "parquet_schema_verified", "evidence_complete",
                "point_in_time_admissible")
_PERMANENT_REASONS = ("signature_invalid", "manifest_hash_mismatch", "unsupported_validator_version",
                      "parquet_schema_mismatch", "ndjson_only_not_canonical", "path_traversal",
                      "supersession_invalid", "duplicate_candle_key")
_RESOLVABLE_REASONS = ("missing_eligibility_evidence", "missing_quality_evidence", "timezone_ambiguous",
                       "point_in_time_unavailable", "incomplete_release")


def validate_import_receipt_v2(obj) -> None:
    _no_prohibited(obj, "import_receipt")
    _reject_control_strings(obj, "import_receipt")
    _exact_keys(obj, IMPORT_RECEIPT_V2_FIELDS, "import_receipt")
    _const(obj, "schema_version", "import_receipt", "2.0")
    _str(obj, "release_id", "import_receipt", regex=SLUG_RE, max_len=64)
    _str(obj, "source_release_manifest_sha256", "import_receipt", regex=SHA256_RE, min_len=64, max_len=64)
    _str(obj, "source_signature_envelope_sha256", "import_receipt", regex=SHA256_RE, min_len=64, max_len=64)
    _str(obj, "consumer_identity", "import_receipt", regex=SLUG_RE, max_len=64)
    _str(obj, "consumer_validator_id", "import_receipt", regex=SLUG_RE, max_len=64)
    _str(obj, "consumer_validator_version", "import_receipt", max_len=32)
    _str(obj, "consumer_validator_digest", "import_receipt", regex=SHA256_RE, min_len=64, max_len=64)
    checks = obj["checks"]
    if not isinstance(checks, dict):
        raise ContractError("import_receipt.checks must be an object")
    _exact_keys(checks, CHECK_FIELDS, "import_receipt.checks")
    for c in CHECK_FIELDS:
        _bool(checks, c, "import_receipt.checks")
    state = _str(obj, "import_state", "import_receipt", enum=("ACCEPTED", "QUARANTINED", "REJECTED"))
    reason = obj["reason_code"]
    if reason is not None:
        _str(obj, "reason_code", "import_receipt", enum=(_PERMANENT_REASONS + _RESOLVABLE_REASONS))
    retryable = _bool(obj, "retryable", "import_receipt")
    admissible = _bool(obj, "research_admissible", "import_receipt")
    if obj["superseded_by_receipt_id"] is not None:
        _str(obj, "superseded_by_receipt_id", "import_receipt", regex=SLUG_RE, max_len=64)
    _instant(obj["imported_at_utc"], "imported_at_utc", "import_receipt")
    _bool(obj, "synthetic", "import_receipt")
    _str_array(obj, "limitations", "import_receipt")
    all_checks = all(checks[c] for c in CHECK_FIELDS)
    if state == "ACCEPTED":
        if not all_checks:
            raise ContractError("import_receipt: ACCEPTED requires every check to pass")
        if reason is not None:
            raise ContractError("import_receipt: ACCEPTED must not carry a reason_code")
        if not admissible or retryable:
            raise ContractError("import_receipt: ACCEPTED must be research_admissible and not retryable")
    elif state == "QUARANTINED":
        if reason is None or reason not in _RESOLVABLE_REASONS:
            raise ContractError("import_receipt: QUARANTINED requires a resolvable reason_code")
        if admissible or not retryable:
            raise ContractError("import_receipt: QUARANTINED is not research_admissible and is retryable")
    else:  # REJECTED
        if reason is None or reason not in _PERMANENT_REASONS:
            raise ContractError("import_receipt: REJECTED requires a permanent reason_code")
        if admissible or retryable:
            raise ContractError("import_receipt: REJECTED is neither research_admissible nor retryable")


# ==================================================================================================
# G4 — point-in-time admissibility (consumer-side helpers)
# ==================================================================================================
def _is_research_release(manifest) -> bool:
    """A release is research-usable at all only when it is a finalised, non-empty COMPLETE release. An INCOMPLETE
    release (a legitimate enum value, meant to be quarantined) is never admissible regardless of its timestamps."""
    return manifest.get("release_completeness") == "COMPLETE" and (manifest.get("row_count") or 0) > 0


def admissible_source_side(manifest, cutoff_utc) -> bool:
    """Historical source-side research: admissible only when the release is COMPLETE/non-empty, source
    availability is EVIDENCED, and the evidenced source_available_at_utc is <= the modelled cutoff.
    UNKNOWN_RETROSPECTIVE is never source-side admissible."""
    if not _is_research_release(manifest):
        return False
    pit = manifest["point_in_time"]
    if pit["source_availability_classification"] != "EVIDENCED" or pit["source_available_at_utc"] is None:
        return False
    return parse_canonical_utc_instant(pit["source_available_at_utc"]) <= parse_canonical_utc_instant(cutoff_utc)


def admissible_published(manifest, publication_cutoff_utc, import_cutoff_utc, imported_at_utc) -> bool:
    """GuvFX-published-intelligence research: admissible only when the release is COMPLETE/non-empty, GuvFX
    published by the publication cutoff AND TIM imported by the import cutoff."""
    if not _is_research_release(manifest):
        return False
    published = parse_canonical_utc_instant(manifest["point_in_time"]["published_at_utc"])
    if published > parse_canonical_utc_instant(publication_cutoff_utc):
        return False
    return parse_canonical_utc_instant(imported_at_utc) <= parse_canonical_utc_instant(import_cutoff_utc)


# Validator registry (name -> callable) for the rule matrix / fixture-driven tests.
VALIDATORS_V0_3 = {
    "canonical_candles_parquet_v1": validate_canonical_candles_parquet_contract,
    "dataset_release_manifest_v3": validate_release_manifest_v3,
    "dataset_release_signature_v2": validate_release_signature_v2,
    "dataset_import_receipt_v2": validate_import_receipt_v2,
    "release_ready_marker_v1": validate_ready_marker,
    "source_eligibility_v1": validate_source_eligibility,
    "release_supersession_v1": validate_release_supersession,
}

__all__ = [
    "CONTRACT_VERSION", "SYNTHETIC_ALGORITHM_V2", "SIGNATURE_DOMAIN_SEPARATOR", "TIM_V0_3_SCHEMA_FILES",
    "CANON_CANDLE_MANDATORY", "CANON_CANDLE_OPTIONAL", "CANDLE_UNIQUENESS_KEY",
    "validate_canonical_candle_parquet_schema", "validate_canonical_candle_rows",
    "validate_canonical_candles_parquet_contract", "synthetic_sign_v2", "validate_release_signature_v2",
    "verify_release_signature_v2", "validate_ready_marker", "validate_source_eligibility",
    "validate_release_supersession", "validate_release_manifest_v3", "validate_import_receipt_v2",
    "admissible_source_side", "admissible_published", "VALIDATORS_V0_3",
    "ContractError", "ProhibitedKeyError", "QuarantineRequired",
]
