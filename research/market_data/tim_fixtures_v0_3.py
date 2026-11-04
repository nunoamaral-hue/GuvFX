"""Synthetic, fabricated fixtures for the GUVFX-TIM Contract v0.3 (GFX-PKT-TIM-C2).

EVERY value here is FABRICATED for offline validation. Nothing is derived from a broker, from production, or from
real market data. Prices are obvious synthetic ramps around 1.10 (EURUSD-shaped only in symbol name). The module
is dependency-free (stdlib + the sibling v0.3 validators) so tests and the offline smoke share one source.

``canonical_candle_contract_doc()`` -> the committed canonical_candles_parquet_v1 descriptor.
``positive_fixtures()``             -> {case_name: valid_payload} that MUST validate (one per contract + variants).
``adversarial_cases()``             -> [{name, contract, payload, expect}] that MUST be rejected or quarantined.
``synthetic_candle_rows(n)``        -> decoded canonical candle rows (datetime + Decimal) for the row validator.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import tim_contracts_v0_3 as V
from .contracts import sha256_hex

SYNTH = "SYNTHETIC_TEST_ONLY_FABRICATED_NO_PRODUCTION_DATA"
SOURCE_ID = "mt5_research_synth"
SYMBOL = "EURUSD"
RELEASE_ID = "rel_synth_v3_eurusd_m1_7d"
REL_DIR = "exchange/releases/" + RELEASE_ID

# Per-column metadata for the committed canonical_candles_parquet_v1 descriptor (full schema surface).
_COLUMN_META = {
    "event_start_utc": ("timestamp_utc", "TIMESTAMP(isAdjustedToUTC=true,unit=MICROS)", "microseconds_utc", None, None, "Half-open bar start (UTC microseconds)."),
    "event_end_utc": ("timestamp_utc", "TIMESTAMP(isAdjustedToUTC=true,unit=MICROS)", "microseconds_utc", None, None, "Half-open bar end (UTC microseconds)."),
    "open": ("decimal", "DECIMAL(38,18)", None, 38, 18, "Open price (bid)."),
    "high": ("decimal", "DECIMAL(38,18)", None, 38, 18, "High price (bid)."),
    "low": ("decimal", "DECIMAL(38,18)", None, 38, 18, "Low price (bid)."),
    "close": ("decimal", "DECIMAL(38,18)", None, 38, 18, "Close price (bid)."),
    "tick_volume": ("int", "INT64", None, None, None, "Tick volume."),
    "quality_flags": ("uint", "UINT32", None, None, None, "Bitset of per-bar quality flags."),
    "real_volume": ("decimal", "DECIMAL(38,18)", None, 38, 18, "Real volume where available."),
    "spread_points": ("int", "INT32", "points", None, None, "Spread in points where available."),
    "source_available_at_utc": ("timestamp_utc", "TIMESTAMP(isAdjustedToUTC=true,unit=MICROS)", "microseconds_utc", None, None, "Source availability instant where available."),
    "source_sequence": ("int", "INT64", None, None, None, "Source sequence/revision where available."),
}


def _col(name, duckdb_type, nullable):
    logical, parquet_phys, unit, precision, scale, desc = _COLUMN_META[name]
    return {"name": name, "logical_type": logical, "parquet_physical_type": parquet_phys,
            "duckdb_type": duckdb_type, "nullable": nullable, "unit": unit,
            "precision": precision, "scale": scale, "description": desc}


def canonical_candle_contract_doc():
    return {
        "schema_version": "1.0", "canonical_format": "parquet", "timeframe_pilot": "M1",
        "utc_representation": "microseconds_since_epoch_utc_adjusted", "interval_semantics": "[start,end)",
        "row_order": "ascending_by_event_start_utc", "uniqueness_key": list(V.CANDLE_UNIQUENESS_KEY),
        "mandatory_columns": [_col(n, t, nl) for n, t, nl in V.CANON_CANDLE_MANDATORY],
        "optional_columns": [_col(n, t, nl) for n, t, nl in V.CANON_CANDLE_OPTIONAL],
        "partitioning": {"scheme": "single_file_per_release",
                         "note": "Pilot forbids partial releases; one Parquet file per immutable release."},
        "native_vs_derived": "declared_in_release_manifest",
        "limitations": [SYNTH],
    }


# Deterministic, distinct, valid synthetic digests for the committed manifest fixture's files/evidence.
def _d(label):
    return sha256_hex(("GFX-TIM-C2-SYNTH-DIGEST\x00" + label).encode("utf-8"))


def source_eligibility(**overrides):
    base = {
        "schema_version": "1.0", "source_id": SOURCE_ID, "broker_id": "synthbroker",
        "server_identity": "SynthBroker-Research", "source_account_reference": "synth-research-acct-ref-0001",
        "eligibility_status": "APPROVED", "licensing_classification": "internal_research_only",
        "licensing_reference": "GFX-LIC-SYNTH-001", "permitted_use": "internal_research_only",
        "capacity_certified": True, "capacity_certification_ref": "GFX-CAP-SYNTH-001",
        "synthetic": True, "non_operational": True, "approved_at_utc": "2026-01-12T00:00:01Z",
        "limitations": [SYNTH],
    }
    base.update(overrides)
    return base


def release_supersession(**overrides):
    base = {
        "schema_version": "1.0", "release_id": "rel_synth_v3_eurusd_m1_7d_r2",
        "supersedes_release_id": RELEASE_ID, "reason": "synthetic correction of a late-arriving bar",
        "scope": "CORRECTION", "correction_window": None, "created_at_utc": "2026-01-13T00:00:00Z",
        "synthetic": True, "limitations": [SYNTH],
    }
    base.update(overrides)
    return base


def ready_marker(manifest_digest, signature_digest, **overrides):
    base = {
        "schema_version": "1.0", "release_id": RELEASE_ID, "state": "READY",
        "manifest_digest": manifest_digest, "signature_envelope_digest": signature_digest,
        "contract_version": "0.3", "synthetic": True, "created_at_utc": "2026-01-12T00:03:02Z",
        "limitations": [SYNTH],
    }
    base.update(overrides)
    return base


# File roster for the native manifest (the SIGNED set): (role, rel_name, media_type, sv, vv, row_count). The
# detached signature and the READY marker are EXTERNAL artefacts (written after the manifest) and are NOT listed.
_FILES = [
    ("canonical_parquet", "eurusd_m1.parquet", V._MEDIA_PARQUET, "1.0", "0.3.0", 10080),
    ("raw_ndjson", "eurusd_m1.raw.ndjson", V._MEDIA_NDJSON, "raw", "0.3.0", 10080),
    ("quality_report", "quality.json", "application/json", "1.0", "0.2.0", None),
    ("gap_report", "gap.json", "application/json", "1.0", "0.2.0", None),
    ("watermark", "watermark.json", "application/json", "1.0", "0.2.0", None),
    ("source_eligibility", "eligibility.json", "application/json", "1.0", "0.3.0", None),
    ("broker_server_identity", "broker_server.json", "application/json", "1.0", "0.2.0", None),
    ("instrument_mapping", "mapping.json", "application/json", "1.0", "0.2.0", None),
    ("timezone_evidence", "timezone.json", "application/json", "1.0", "0.2.0", None),
    ("licensing", "licensing.json", "application/json", "1.0", "0.3.0", None),
]


def _native_manifest_v3(**overrides):
    files = []
    evidence = {}
    for role, rel_name, media, sv, vv, rc in _FILES:
        rel_path = REL_DIR + "/" + rel_name
        dval = _d(role)
        files.append({
            "relative_path": rel_path, "media_type": media, "byte_size": 1024,
            "digest": {"algorithm": "sha256", "value": dval}, "logical_role": role,
            "schema_version": sv, "validator_version": vv, "row_count": rc,
        })
        if role in V._EVIDENCE_ROLES:
            evidence[role] = {"contract_version": sv, "relative_path": rel_path, "sha256": dval}
    manifest = {
        "schema_version": "3.0", "contract_version": "0.3", "release_id": RELEASE_ID,
        "supersedes_release_id": None, "supersession": None, "source_id": SOURCE_ID,
        "canonical_symbol": SYMBOL, "broker_native_symbol": "EURUSD", "asset_class": "fx", "timeframe": "M1",
        "representation": "bid_ohlc", "price_basis": "bid", "provenance": "NATIVE", "derivation": None,
        "canonical_format": "parquet", "range_start_utc": "2026-01-05T00:00:00Z",
        "range_end_utc": "2026-01-12T00:00:00Z", "range_semantics": "[start,end)",
        "point_in_time": {
            "event_start_utc": "2026-01-05T00:00:00Z", "event_end_utc": "2026-01-12T00:00:00Z",
            "source_available_at_utc": "2026-01-12T00:00:02Z", "source_availability_classification": "EVIDENCED",
            "collector_observed_at_utc": "2026-01-12T00:00:05Z", "ingested_at_utc": "2026-01-12T00:00:06Z",
            "validated_at_utc": "2026-01-12T00:02:00Z", "published_at_utc": "2026-01-12T00:03:00Z",
        },
        "row_count": 10080, "uniqueness_key": ["event_start_utc"], "files": files, "evidence": evidence,
        "validator_pinning": {"schema_version": "3.0", "validator_id": "guvfx_tim_v0_3",
                              "validator_version": "0.3.0", "validator_digest": _d("validator"),
                              "compatibility_range": ">=0.3.0,<0.4.0"},
        "contract_bundle_digest": _d("contract_bundle"), "validator_bundle_digest": _d("validator_bundle"),
        "release_completeness": "COMPLETE", "partial_release_allowed": False,
        "producer_identity": "guvfx_producer_synth", "producer_version": "guvfx-tim-0.3.0-synth",
        "signing_key_id": "synthkey1", "signature_path": REL_DIR + "/manifest.sig.json",
        "ready_marker": {"present": True, "written_last": True, "ready_marker_path": REL_DIR + "/READY"},
        "licensing_classification": "internal_research_only", "synthetic": True, "limitations": [SYNTH],
    }
    manifest.update(overrides)
    return manifest


def signature_v2_for(manifest_bytes, release_id, *, signing_key_id="synthkey1",
                     contract_bundle_digest=None, validator_bundle_digest=None, **overrides):
    cbd = contract_bundle_digest or _d("contract_bundle")
    vbd = validator_bundle_digest or _d("validator_bundle")
    pub_fp = _d("public_key")
    manifest_digest = sha256_hex(bytes(manifest_bytes))
    sig = {
        "schema_version": "2.0", "domain_separator": V.SIGNATURE_DOMAIN_SEPARATOR, "release_id": release_id,
        "signed_manifest_path": REL_DIR + "/manifest.json", "manifest_digest": manifest_digest,
        "contract_bundle_digest": cbd, "validator_bundle_digest": vbd,
        "producer_identity": "guvfx_producer_synth", "signing_key_id": signing_key_id,
        "public_key_fingerprint": pub_fp, "algorithm": V.SYNTHETIC_ALGORITHM_V2, "signature_scheme": "detached",
        "signature_value": V.synthetic_sign_v2(release_id=release_id, manifest_digest=manifest_digest,
                                                contract_bundle_digest=cbd, validator_bundle_digest=vbd,
                                                producer_identity="guvfx_producer_synth",
                                                signing_key_id=signing_key_id, public_key_fingerprint=pub_fp),
        "synthetic": True, "created_at_utc": "2026-01-12T00:03:01Z", "key_validity": None, "limitations": [SYNTH],
    }
    sig.update(overrides)
    return sig


def import_receipt_v2(**overrides):
    base = {
        "schema_version": "2.0", "release_id": RELEASE_ID, "source_release_manifest_sha256": _d("manifest_bytes"),
        "source_signature_envelope_sha256": _d("signature_bytes"), "consumer_identity": "tim_reader_synth",
        "consumer_validator_id": "tim_consumer_v0_3", "consumer_validator_version": "0.3.0",
        "consumer_validator_digest": _d("consumer_validator"),
        "checks": {"signature_verified": True, "manifest_hashes_verified": True, "parquet_schema_verified": True,
                   "evidence_complete": True, "point_in_time_admissible": True},
        "import_state": "ACCEPTED", "reason_code": None, "retryable": False, "research_admissible": True,
        "superseded_by_receipt_id": None, "imported_at_utc": "2026-01-12T00:10:00Z", "synthetic": True,
        "limitations": [SYNTH],
    }
    base.update(overrides)
    return base


def _standalone_signature():
    """A standalone, self-consistent signature fixture (its manifest_digest matches a crafted manifest-bytes)."""
    return signature_v2_for(b"synthetic-manifest-bytes-for-signature-fixture", RELEASE_ID)


def positive_fixtures():
    nat = _native_manifest_v3()
    sig = _standalone_signature()
    # A NATIVE release that ALSO carries a raw NDJSON payload alongside the canonical Parquet (permitted).
    with_raw = _native_manifest_v3()
    return {
        "canonical_candles_parquet_v1": canonical_candle_contract_doc(),
        "dataset_release_manifest_v3": nat,
        "dataset_release_manifest_v3__with_raw_ndjson": with_raw,
        "dataset_release_signature_v2": sig,
        "dataset_import_receipt_v2": import_receipt_v2(),
        "dataset_import_receipt_v2__quarantined": import_receipt_v2(
            checks={"signature_verified": True, "manifest_hashes_verified": True, "parquet_schema_verified": True,
                    "evidence_complete": False, "point_in_time_admissible": True},
            import_state="QUARANTINED", reason_code="missing_eligibility_evidence", retryable=True,
            research_admissible=False),
        "dataset_import_receipt_v2__rejected": import_receipt_v2(
            checks={"signature_verified": False, "manifest_hashes_verified": True, "parquet_schema_verified": True,
                    "evidence_complete": True, "point_in_time_admissible": True},
            import_state="REJECTED", reason_code="signature_invalid", retryable=False, research_admissible=False),
        "release_ready_marker_v1": ready_marker(_d("manifest_bytes"), _d("signature_bytes")),
        "source_eligibility_v1": source_eligibility(),
        "release_supersession_v1": release_supersession(),
        "release_supersession_v1__partial": release_supersession(
            release_id="rel_synth_v3_eurusd_m1_7d_r3", scope="PARTIAL_WINDOW",
            correction_window={"range_start_utc": "2026-01-10T22:00:00Z", "range_end_utc": "2026-01-10T22:05:00Z"}),
    }


def contract_of(case_name):
    return case_name.split("__", 1)[0]


def adversarial_cases():
    pf = positive_fixtures()
    nat = pf["dataset_release_manifest_v3"]
    sig = pf["dataset_release_signature_v2"]
    rec = pf["dataset_import_receipt_v2"]
    elig = pf["source_eligibility_v1"]
    cases = []

    def add(name, contract, payload, expect):
        cases.append({"name": name, "contract": contract, "payload": payload, "expect": expect})

    # --- manifest (G1/G2/G3/G4/G5/G6) ---
    ndjson_only = _native_manifest_v3()
    ndjson_only["files"] = [f for f in ndjson_only["files"] if f["logical_role"] != "canonical_parquet"]
    add("ndjson_only_canonical", "dataset_release_manifest_v3", ndjson_only, "reject")

    miss_ev = _native_manifest_v3()
    miss_ev["evidence"] = {k: v for k, v in miss_ev["evidence"].items() if k != "quality_report"}
    add("missing_evidence_role", "dataset_release_manifest_v3", miss_ev, "reject")

    unbound_ev = _native_manifest_v3()
    unbound_ev["evidence"]["licensing"] = {**unbound_ev["evidence"]["licensing"], "sha256": "0" * 64}
    add("evidence_hash_not_bound", "dataset_release_manifest_v3", unbound_ev, "reject")

    collapsed = _native_manifest_v3()
    collapsed["point_in_time"] = {**collapsed["point_in_time"],
                                  "collector_observed_at_utc": "2026-01-12T00:00:01Z"}  # < source_available
    add("collapsed_timestamps", "dataset_release_manifest_v3", collapsed, "reject")

    unknown_src_avail = _native_manifest_v3()
    unknown_src_avail["point_in_time"] = {**unknown_src_avail["point_in_time"],
                                          "source_availability_classification": "UNKNOWN_RETROSPECTIVE"}
    add("unknown_classification_with_avail", "dataset_release_manifest_v3", unknown_src_avail, "reject")

    partial = _native_manifest_v3(); partial["partial_release_allowed"] = True
    add("forbidden_partial_release", "dataset_release_manifest_v3", partial, "reject")

    self_sup = _native_manifest_v3()
    self_sup["supersedes_release_id"] = RELEASE_ID
    self_sup["supersession"] = {"reason": "loop", "scope": "FULL",
                                "supersession_ref": {"contract_version": "1.0",
                                                     "relative_path": REL_DIR + "/supersession.json",
                                                     "sha256": _d("sup")}}
    add("self_supersession_loop", "dataset_release_manifest_v3", self_sup, "reject")

    orphan_sup = _native_manifest_v3()
    orphan_sup["supersession"] = {"reason": "x", "scope": "FULL",
                                  "supersession_ref": {"contract_version": "1.0",
                                                       "relative_path": REL_DIR + "/s.json", "sha256": _d("s")}}
    add("supersession_without_predecessor", "dataset_release_manifest_v3", orphan_sup, "reject")

    native_deriv = _native_manifest_v3()
    native_deriv["derivation"] = {"source_timeframe": "M1", "derivation_version": "1.0", "calendar_id": "c",
                                  "calendar_version": "1", "bucket_policy": "left_closed_right_open",
                                  "session_timezone": "Etc/UTC", "dst_policy": "utc_fixed", "price_basis": "bid"}
    add("native_with_derivation", "dataset_release_manifest_v3", native_deriv, "reject")

    traversal = _native_manifest_v3()
    traversal["files"] = [{**traversal["files"][0], "relative_path": "../escape/eurusd.parquet"}] + traversal["files"][1:]
    add("path_traversal_file", "dataset_release_manifest_v3", traversal, "reject")

    not_last = _native_manifest_v3()
    not_last["ready_marker"] = {**not_last["ready_marker"], "written_last": False}
    add("complete_without_ready_last", "dataset_release_manifest_v3", not_last, "reject")

    zero_rows = _native_manifest_v3(); zero_rows["row_count"] = 0
    add("complete_zero_rows", "dataset_release_manifest_v3", zero_rows, "reject")

    two_parquet = _native_manifest_v3()
    extra = {**two_parquet["files"][0], "relative_path": REL_DIR + "/eurusd_m1_dup.parquet"}
    two_parquet["files"] = two_parquet["files"] + [extra]
    add("two_canonical_parquet", "dataset_release_manifest_v3", two_parquet, "reject")

    # --- signature envelope (G9) ---
    add("signature_not_synthetic", "dataset_release_signature_v2", {**sig, "synthetic": False}, "reject")
    add("signature_wrong_algorithm", "dataset_release_signature_v2", {**sig, "algorithm": "ed25519"}, "reject")
    add("signature_bad_domain", "dataset_release_signature_v2", {**sig, "domain_separator": "WRONG"}, "reject")

    # --- import receipt (G7/G8) ---
    add("receipt_accepted_but_unverified", "dataset_import_receipt_v2",
        {**rec, "checks": {**rec["checks"], "signature_verified": False}}, "reject")
    add("receipt_accepted_with_reason", "dataset_import_receipt_v2", {**rec, "reason_code": "incomplete_release"}, "reject")
    add("receipt_quarantined_permanent_reason", "dataset_import_receipt_v2",
        {**pf["dataset_import_receipt_v2__quarantined"], "reason_code": "signature_invalid"}, "reject")
    add("receipt_rejected_but_admissible", "dataset_import_receipt_v2",
        {**pf["dataset_import_receipt_v2__rejected"], "research_admissible": True}, "reject")

    # --- ready marker ---
    add("ready_marker_wrong_state", "release_ready_marker_v1",
        {**pf["release_ready_marker_v1"], "state": "STAGING"}, "reject")

    # --- eligibility (G3) ---
    add("eligibility_pending", "source_eligibility_v1", {**elig, "eligibility_status": "PENDING"}, "quarantine")
    add("eligibility_approved_no_license", "source_eligibility_v1",
        {**elig, "licensing_reference": None}, "quarantine")
    add("synthetic_mislabeled_as_operational", "source_eligibility_v1",
        {**elig, "non_operational": False}, "reject")
    add("synthetic_claims_redistribution", "source_eligibility_v1",
        {**elig, "licensing_classification": "licensed_redistribution",
         "permitted_use": "licensed_redistribution"}, "reject")

    # --- supersession (G5) ---
    add("supersession_self_loop", "release_supersession_v1",
        {**pf["release_supersession_v1"], "supersedes_release_id": pf["release_supersession_v1"]["release_id"]}, "reject")
    add("supersession_partial_no_window", "release_supersession_v1",
        {**pf["release_supersession_v1"], "scope": "PARTIAL_WINDOW"}, "reject")

    # --- prohibited key + unknown field ---
    add("prohibited_key_login", "source_eligibility_v1", {**elig, "login": "123456"}, "reject")
    add("unknown_field_manifest", "dataset_release_manifest_v3", {**nat, "surprise": 1}, "reject")

    # --- control-character smuggling (D2 regression): trailing/embedded newline must not slip past ^...$ regexes ---
    nl_path = _native_manifest_v3()
    nl_path["files"] = [{**nl_path["files"][0], "relative_path": nl_path["files"][0]["relative_path"] + "\n"}] \
        + nl_path["files"][1:]
    add("control_char_newline_in_path", "dataset_release_manifest_v3", nl_path, "reject")
    add("control_char_in_identity", "source_eligibility_v1", {**elig, "server_identity": "Synth\nServer"}, "reject")
    return cases


def synthetic_candle_rows(n, *, start=None):
    """Decoded canonical candle rows (datetime + Decimal) — the input to validate_canonical_candle_rows."""
    start = start or datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        s = start + timedelta(minutes=i)
        base = Decimal("1.10000") + (Decimal(i % 500) * Decimal("0.00001"))
        rows.append({
            "event_start_utc": s, "event_end_utc": s + timedelta(minutes=1),
            "open": base, "high": base + Decimal("0.00010"), "low": base - Decimal("0.00010"),
            "close": base + Decimal("0.00002"), "tick_volume": 1 + (i % 10), "quality_flags": 0,
            "real_volume": None, "spread_points": 2, "source_available_at_utc": None, "source_sequence": i,
        })
    return rows
