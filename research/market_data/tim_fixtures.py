"""Synthetic, fabricated fixtures for the GUVFX-TIM Contract v0.2 (GFX-PKT-TIM-C1).

EVERY value here is FABRICATED for offline validation. Nothing is derived from a broker, from production, or
from real market data. Prices are obvious synthetic ramps around 1.10 (EURUSD-shaped only in symbol name). The
module is dependency-free (stdlib + the sibling validators) so tests and the offline smoke share one source.

``positive_fixtures()``  -> {case_name: valid_payload} that MUST validate (one per contract, plus a derived
                            release and quarantine-record positives).
``adversarial_cases()``  -> [{name, contract, payload, expect}] that MUST be rejected or quarantined.
"""
from __future__ import annotations

from . import tim_contracts as T
from .contracts import canonical_json_bytes, sha256_hex

SYNTH = "SYNTHETIC_TEST_ONLY_FABRICATED_NO_PRODUCTION_DATA"
SOURCE_ID = "mt5_research_synth"
SCOPE = "research_synth"
SYMBOL = "EURUSD"


def _request():
    base = {
        "schema_version": "2.0", "operation": "copy_rates_range", "source_id": SOURCE_ID,
        "account_scope": SCOPE, "canonical_symbol": SYMBOL, "broker_native_symbol": "EURUSD",
        "timeframe": "M1", "representation": "bid_ohlc", "price_basis": "bid",
        "range_start_utc": "2026-01-05T00:00:00Z", "range_end_utc": "2026-01-05T00:03:00Z",
        "range_semantics": "[start,end)",
    }
    return {**base, "request_id": T.compute_export_request_id_v2({**base, "request_id": ""})}


def _bars(n):
    out = []
    for i in range(n):
        out.append({
            "event_start_utc": f"2026-01-05T00:{i:02d}:00Z",
            "event_end_utc": f"2026-01-05T00:{i + 1:02d}:00Z",
            "open": 1.1000 + i * 0.0001, "high": 1.1050 + i * 0.0001,
            "low": 1.0950 + i * 0.0001, "close": 1.1010 + i * 0.0001, "volume": 10 + i,
        })
    return out


def _response():
    req = _request()
    return {
        "schema_version": "2.0", "request_id": req["request_id"], "source_id": SOURCE_ID,
        "account_scope": SCOPE, "canonical_symbol": SYMBOL, "broker_native_symbol": "EURUSD",
        "timeframe": "M1", "representation": "bid_ohlc", "price_basis": "bid", "provenance": "NATIVE",
        "range_start_utc": "2026-01-05T00:00:00Z", "range_end_utc": "2026-01-05T00:03:00Z",
        "range_semantics": "[start,end)", "source_timezone": "Etc/UTC",
        "source_available_at_utc": "2026-01-05T00:03:02Z", "collector_observed_at_utc": "2026-01-05T00:03:05Z",
        "bar_count": 3, "bars": _bars(3), "raw_object_sha256": "a" * 64, "limitations": [SYNTH],
    }


def _positive_raw(**overrides):
    base = {
        "schema_version": "2.0", "raw_object_id": "a" * 64, "object_state": "ACCEPTED", "source_id": SOURCE_ID,
        "account_scope": SCOPE, "canonical_symbol": SYMBOL, "broker_native_symbol": "EURUSD", "asset_class": "fx",
        "timeframe": "M1", "provenance": "NATIVE", "representation": "bid_ohlc", "price_basis": "bid",
        "range_start_utc": "2026-01-05T00:00:00Z", "range_end_utc": "2026-01-05T00:03:00Z",
        "range_semantics": "[start,end)", "source_timezone": "Etc/UTC",
        "event_start_utc": "2026-01-05T00:00:00Z", "event_end_utc": "2026-01-05T00:03:00Z",
        "source_available_at_utc": "2026-01-05T00:03:02Z", "collector_observed_at_utc": "2026-01-05T00:03:05Z",
        "ingested_at_utc": "2026-01-05T00:03:06Z", "validated_at_utc": "2026-01-05T00:03:07Z",
        "request_schema_id": T.EXPORT_REQUEST_V2_SCHEMA_ID, "response_schema_id": T.EXPORT_RESPONSE_V2_SCHEMA_ID,
        "request_sha256": "a" * 64, "response_sha256": "b" * 64, "request_path": "raw/synth/request.json",
        "response_path": "raw/synth/response.json", "manifest_path": "raw/synth/manifest.json",
        "code_commit": "synthetic", "timezone_status": "VERIFIED", "row_count": 3, "file_size_bytes": 2048,
        "content_sha256": "c" * 64, "quarantine_reason": None, "limitations": [SYNTH],
    }
    base.update(overrides)
    return base


def _native_release():
    return {
        "schema_version": "2.0", "release_id": "rel_synth_native_1", "source_id": SOURCE_ID,
        "canonical_symbol": SYMBOL, "asset_class": "fx", "timeframe": "M1", "representation": "bid_ohlc",
        "price_basis": "bid", "provenance": "NATIVE", "derivation": None,
        "range_start_utc": "2026-01-05T00:00:00Z", "range_end_utc": "2026-01-12T00:00:00Z",
        "range_semantics": "[start,end)", "event_start_utc": "2026-01-05T00:00:00Z",
        "event_end_utc": "2026-01-12T00:00:00Z", "source_available_at_utc": "2026-01-12T00:00:02Z",
        "collector_observed_at_utc": "2026-01-12T00:00:05Z", "ingested_at_utc": "2026-01-12T00:00:06Z",
        "validated_at_utc": "2026-01-12T00:00:07Z", "published_at_utc": "2026-01-12T00:03:00Z",
        "row_count": 7200, "files": [
            {"rel_path": "exchange/releases/rel_synth_native_1/eurusd_m1.parquet", "role": "canonical_parquet",
             "content_sha256": "b" * 64, "file_size_bytes": 123456, "row_count": 7200},
            {"rel_path": "exchange/releases/rel_synth_native_1/quality.json", "role": "quality_report",
             "content_sha256": "c" * 64, "file_size_bytes": 512, "row_count": None},
            {"rel_path": "exchange/releases/rel_synth_native_1/READY", "role": "ready_marker",
             "content_sha256": "d" * 64, "file_size_bytes": 64, "row_count": None},
        ],
        "quality_report_path": "exchange/releases/rel_synth_native_1/quality.json",
        "watermark_path": "exchange/releases/rel_synth_native_1/watermark.json",
        "release_completeness": "COMPLETE", "partial_release_allowed": False,
        "producer_version": "guvfx-tim-0.2.0-synth", "contract_version": "0.2", "signing_key_id": "synthkey1",
        "signature_path": "exchange/releases/rel_synth_native_1/manifest.sig.json",
        "ready_marker": {"present": True, "written_last": True,
                         "ready_marker_path": "exchange/releases/rel_synth_native_1/READY",
                         "ready_marker_sha256": "d" * 64},
        "licensing_classification": "internal_research_only", "limitations": [SYNTH],
    }


def _derived_release():
    r = dict(_native_release())
    r.update({
        "release_id": "rel_synth_derived_h1_1", "timeframe": "H1", "provenance": "DERIVED",
        "derivation": {"source_timeframe": "M1", "derivation_version": "1.0", "calendar_id": "fx_247_synth",
                       "calendar_version": "2026.1", "bucket_policy": "left_closed_right_open",
                       "session_timezone": "Etc/UTC", "dst_policy": "utc_fixed", "price_basis": "bid"},
        "row_count": 120,
        "files": [
            {"rel_path": "exchange/releases/rel_synth_derived_h1_1/eurusd_h1.parquet", "role": "canonical_parquet",
             "content_sha256": "e" * 64, "file_size_bytes": 4096, "row_count": 120},
            {"rel_path": "exchange/releases/rel_synth_derived_h1_1/READY", "role": "ready_marker",
             "content_sha256": "f" * 64, "file_size_bytes": 64, "row_count": None},
        ],
        "quality_report_path": "exchange/releases/rel_synth_derived_h1_1/quality.json",
        "watermark_path": "exchange/releases/rel_synth_derived_h1_1/watermark.json",
        "signature_path": "exchange/releases/rel_synth_derived_h1_1/manifest.sig.json",
        "ready_marker": {"present": True, "written_last": True,
                         "ready_marker_path": "exchange/releases/rel_synth_derived_h1_1/READY",
                         "ready_marker_sha256": "f" * 64},
    })
    return r


def signature_for(manifest: dict):
    """Return (manifest_bytes, signature_envelope) for a release manifest using the injected synthetic signer."""
    mb = canonical_json_bytes(manifest)
    key_id, pub_fp = "synthkey1", "1" * 64
    sig = {
        "schema_version": "1.0", "release_id": manifest["release_id"],
        "signed_manifest_path": manifest["signature_path"].replace("manifest.sig.json", "manifest.json"),
        "signed_manifest_sha256": sha256_hex(mb), "signing_key_id": key_id, "public_key_fingerprint": pub_fp,
        "algorithm": T.SYNTHETIC_ALGORITHM, "signature_scheme": "detached",
        "signature_value": T.synthetic_sign(mb, key_id, pub_fp), "synthetic": True,
        "created_at_utc": "2026-01-12T00:03:01Z", "limitations": [SYNTH],
    }
    return mb, sig


def positive_fixtures():
    nat = _native_release()
    return {
        "market_data_source_v1": {
            "schema_version": "1.0", "source_id": SOURCE_ID, "source_type": "mt5_research", "broker_id": "synthbroker",
            "server_identity": "SynthBroker-Research", "account_scope": SCOPE, "account_type": "research",
            "source_timezone": "Etc/UTC", "licensing_classification": "internal_research_only",
            "licensing_reference": "GFX-LIC-SYNTH-001", "capacity_certified": True,
            "capacity_certification_ref": "GFX-CAP-SYNTH-001", "limitations": [SYNTH]},
        "canonical_instrument_v1": {
            "schema_version": "1.0", "canonical_symbol": SYMBOL, "asset_class": "fx", "base_currency": "EUR",
            "quote_currency": "USD", "price_precision": 5, "volume_precision": 2, "price_basis_default": "bid",
            "limitations": [SYNTH]},
        "source_instrument_mapping_v1": {
            "schema_version": "1.0", "source_id": SOURCE_ID, "broker_native_symbol": "EURUSD",
            "canonical_symbol": SYMBOL, "asset_class": "fx", "price_precision": 5, "volume_precision": 2,
            "mapping_status": "VERIFIED", "ambiguity_reason": None, "limitations": [SYNTH]},
        "agent_market_data_export_request_v2": _request(),
        "agent_market_data_export_response_v2": _response(),
        "raw_market_data_manifest_v2": _positive_raw(),
        "acquisition_watermark_v1": {
            "schema_version": "1.0", "source_id": SOURCE_ID, "canonical_symbol": SYMBOL, "timeframe": "M1",
            "representation": "bid_ohlc", "price_basis": "bid", "watermark_event_end_utc": "2026-01-12T00:00:00Z",
            "range_semantics": "[start,end)", "last_raw_object_sha256": "a" * 64, "landed_and_verified": True,
            "validated": True, "quality_accepted": True, "advanced_at_utc": "2026-01-12T00:03:00Z",
            "limitations": [SYNTH]},
        "market_data_gap_v1": {
            "schema_version": "1.0", "gap_id": "9" * 64, "source_id": SOURCE_ID, "canonical_symbol": SYMBOL,
            "timeframe": "M1", "representation": "bid_ohlc", "gap_start_utc": "2026-01-10T22:00:00Z",
            "gap_end_utc": "2026-01-10T22:05:00Z", "range_semantics": "[start,end)", "gap_state": "REPAIRED",
            "declared_at_utc": "2026-01-10T22:10:00Z", "reason": "synthetic weekend/feed outage gap",
            "repair": {"repaired_at_utc": "2026-01-10T23:00:00Z", "repair_raw_object_sha256": "8" * 64,
                       "repair_evidence_path": "raw/synth/gap_repair.json", "supersedes_gap_id": None},
            "limitations": [SYNTH]},
        "market_data_quality_report_v1": {
            "schema_version": "1.0", "source_id": SOURCE_ID, "canonical_symbol": SYMBOL, "timeframe": "M1",
            "representation": "bid_ohlc", "price_basis": "bid", "provenance": "NATIVE",
            "range_start_utc": "2026-01-05T00:00:00Z", "range_end_utc": "2026-01-05T00:03:00Z",
            "range_semantics": "[start,end)", "expected_bar_count": 3, "observed_bar_count": 3,
            "duplicate_count": 0, "out_of_order_count": 0, "overlap_count": 0, "ohlc_violation_count": 0,
            "non_finite_count": 0, "gap_count": 0, "quality_status": "PASS", "quarantine_reasons": [],
            "evaluated_at_utc": "2026-01-05T00:03:08Z", "limitations": [SYNTH]},
        "dataset_release_manifest_v2": nat,
        "dataset_release_signature_v1": signature_for(nat)[1],
        "dataset_import_receipt_v1": {
            "schema_version": "1.0", "release_id": nat["release_id"], "importer_identity": "tim_reader_synth",
            "source_release_manifest_sha256": "b" * 64, "signature_verified": True, "hashes_verified": True,
            "completeness_verified": True, "import_state": "IMPORTED", "quarantine_reason": None,
            "imported_at_utc": "2026-01-12T00:10:00Z", "limitations": [SYNTH]},
        # Extra positives proving distinct-identity and quarantine-record validity:
        "dataset_release_manifest_v2__derived": _derived_release(),
        "market_data_quality_report_v1__quarantine": {
            "schema_version": "1.0", "source_id": SOURCE_ID, "canonical_symbol": SYMBOL, "timeframe": "M1",
            "representation": "bid_ohlc", "price_basis": "bid", "provenance": "NATIVE",
            "range_start_utc": "2026-01-05T00:00:00Z", "range_end_utc": "2026-01-05T00:03:00Z",
            "range_semantics": "[start,end)", "expected_bar_count": 3, "observed_bar_count": 2,
            "duplicate_count": 0, "out_of_order_count": 0, "overlap_count": 0, "ohlc_violation_count": 0,
            "non_finite_count": 0, "gap_count": 1, "quality_status": "QUARANTINE",
            "quarantine_reasons": ["missing_interval"], "evaluated_at_utc": "2026-01-05T00:03:08Z",
            "limitations": [SYNTH]},
        "raw_market_data_manifest_v2__quarantined": _positive_raw(
            object_state="QUARANTINED", validated_at_utc=None, timezone_status="CONFLICT",
            quarantine_reason="broker timezone conflict (synthetic)"),
        "dataset_import_receipt_v1__quarantined": {
            "schema_version": "1.0", "release_id": nat["release_id"], "importer_identity": "tim_reader_synth",
            "source_release_manifest_sha256": "b" * 64, "signature_verified": False, "hashes_verified": True,
            "completeness_verified": True, "import_state": "QUARANTINED",
            "quarantine_reason": "signature verification failed (synthetic)",
            "imported_at_utc": "2026-01-12T00:10:00Z", "limitations": [SYNTH]},
    }


# Canonical-contract name for each positive fixture key (strips the "__variant" suffix).
def contract_of(case_name: str) -> str:
    return case_name.split("__", 1)[0]


def adversarial_cases():
    pf = positive_fixtures()
    resp = pf["agent_market_data_export_response_v2"]
    nat = pf["dataset_release_manifest_v2"]
    der = pf["dataset_release_manifest_v2__derived"]
    cases = []

    def add(name, contract, payload, expect):
        cases.append({"name": name, "contract": contract, "payload": payload, "expect": expect})

    dup = dict(resp); dup["bars"] = [resp["bars"][0], dict(resp["bars"][0])]; dup["bar_count"] = 2
    dup["range_end_utc"] = "2026-01-05T00:01:00Z"
    add("duplicate_bars", "agent_market_data_export_response_v2", dup, "reject")

    ov = dict(resp); b2 = dict(resp["bars"][1]); b2["event_start_utc"] = "2026-01-05T00:00:00Z"
    b2["event_end_utc"] = "2026-01-05T00:01:00Z"; ov["bars"] = [resp["bars"][0], b2]; ov["bar_count"] = 2
    ov["range_end_utc"] = "2026-01-05T00:02:00Z"
    add("overlapping_bars", "agent_market_data_export_response_v2", ov, "reject")

    bad = dict(resp); b0 = dict(resp["bars"][0]); b0["high"] = 0.5; bad["bars"] = [b0]; bad["bar_count"] = 1
    bad["range_end_utc"] = "2026-01-05T00:01:00Z"
    add("invalid_ohlc", "agent_market_data_export_response_v2", bad, "reject")

    nf = dict(resp); b0 = dict(resp["bars"][0]); b0["close"] = float("inf"); nf["bars"] = [b0]; nf["bar_count"] = 1
    nf["range_end_utc"] = "2026-01-05T00:01:00Z"
    add("non_finite_value", "agent_market_data_export_response_v2", nf, "reject")

    oo = dict(resp); oo["collector_observed_at_utc"] = "2026-01-05T00:03:01Z"
    oo["source_available_at_utc"] = "2026-01-05T00:03:05Z"
    add("timestamp_chain_out_of_order", "agent_market_data_export_response_v2", oo, "reject")

    add("ambiguous_mapping", "source_instrument_mapping_v1",
        {**pf["source_instrument_mapping_v1"], "mapping_status": "AMBIGUOUS", "canonical_symbol": None,
         "ambiguity_reason": "two canonical candidates (synthetic)"}, "quarantine")

    add("native_with_derivation", "dataset_release_manifest_v2", {**nat, "derivation": der["derivation"]}, "reject")
    add("derived_without_derivation", "dataset_release_manifest_v2", {**der, "derivation": None}, "reject")

    mh = dict(nat); mh["files"] = [dict(nat["files"][0], content_sha256="z" * 10)] + nat["files"][1:]
    add("mismatched_file_hash", "dataset_release_manifest_v2", mh, "reject")

    inc = dict(nat); inc["ready_marker"] = {**nat["ready_marker"], "written_last": False}
    add("complete_without_ready_last", "dataset_release_manifest_v2", inc, "reject")

    add("forbidden_partial_release", "dataset_release_manifest_v2", {**nat, "partial_release_allowed": True}, "reject")

    sig = pf["dataset_release_signature_v1"]
    add("signature_not_marked_synthetic", "dataset_release_signature_v1", {**sig, "synthetic": False}, "reject")
    add("signature_wrong_algorithm", "dataset_release_signature_v1", {**sig, "algorithm": "ed25519"}, "reject")

    add("import_imported_but_unverified", "dataset_import_receipt_v1",
        {**pf["dataset_import_receipt_v1"], "signature_verified": False}, "reject")
    add("watermark_without_quality", "acquisition_watermark_v1",
        {**pf["acquisition_watermark_v1"], "quality_accepted": False}, "reject")
    add("repaired_gap_without_repair", "market_data_gap_v1", {**pf["market_data_gap_v1"], "repair": None}, "reject")
    add("declared_gap_with_repair", "market_data_gap_v1", {**pf["market_data_gap_v1"], "gap_state": "DECLARED"}, "reject")
    add("prohibited_key_login", "market_data_source_v1", {**pf["market_data_source_v1"], "login": "123456"}, "reject")
    add("unknown_field", "canonical_instrument_v1", {**pf["canonical_instrument_v1"], "surprise": 1}, "reject")
    add("quality_pass_with_violation", "market_data_quality_report_v1",
        {**pf["market_data_quality_report_v1"], "ohlc_violation_count": 1}, "reject")
    add("unlicensed_source", "market_data_source_v1", {**pf["market_data_source_v1"], "licensing_reference": None}, "quarantine")
    return cases
