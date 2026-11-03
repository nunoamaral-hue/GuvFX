#!/usr/bin/env python3
"""Synthetic end-to-end GUVFX-TIM v0.2 dataset-release smoke (GFX-PKT-TIM-C1). synthetic_test_only.

Proves, with ZERO network and ZERO real systems, inside an explicit TemporaryDirectory that is deleted on exit:

  synthetic export request/response (contract v2)
    -> synthetic 7-day EURUSD M1 NATIVE dataset (10080 half-open bars, fabricated ramp)
    -> contract + quality validation (completeness, ordering, OHLC, finiteness)
    -> JSON manifests (raw manifest, quality report, watermark, release manifest v2)
    -> real SHA-256 file digests recorded in the release manifest
    -> INJECTED SYNTHETIC detached signature envelope (no real key/algorithm)
    -> signature + file-hash verification, tamper detection
    -> READY marker written LAST (release becomes visible only when complete)
    -> quarantine case (incomplete bucket) and gap-repair case (new immutable evidence)
    -> prohibited-key scan + no-absolute-path assertion over every emitted artefact

It performs NO MT5, broker, NAS, credential, production or trading action. All data is fabricated.
Emits deterministic JSON (sorted keys, no absolute path, no wall-clock).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from research.market_data import tim_contracts as T  # noqa: E402
from research.market_data import tim_fixtures as F  # noqa: E402
from research.market_data.agent_client import network_blocked  # noqa: E402
from research.market_data.contracts import (  # noqa: E402
    canonical_json_bytes, find_prohibited_key, scan_raw_for_prohibited_key_tokens, sha256_hex,
)

START = datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)
DAYS = 7
MINUTES = DAYS * 1440  # 10080
SYMBOL = "EURUSD"
SOURCE_ID = "mt5_research_synth"


def _min(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:00Z")


def _bar(i: int) -> dict:
    base = 1.10000 + (i % 500) * 0.00001  # deterministic synthetic ramp — NOT real market data
    s = START + timedelta(minutes=i)
    return {
        "event_start_utc": _min(s), "event_end_utc": _min(s + timedelta(minutes=1)),
        "open": round(base, 5), "high": round(base + 0.00010, 5),
        "low": round(base - 0.00010, 5), "close": round(base + 0.00002, 5), "volume": 1 + (i % 10),
    }


def _write(root: str, rel: str, data) -> tuple:
    """Write canonical bytes to root/rel, return (rel, sha256, size). Refuses absolute/traversal paths."""
    if rel.startswith("/") or ".." in rel.split("/") or "\\" in rel:
        raise AssertionError(f"unsafe rel_path: {rel!r}")
    if isinstance(data, (bytes, bytearray)):
        body = bytes(data)
    else:
        body = canonical_json_bytes(data)
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body)
    return rel, sha256_hex(body), len(body)


def run_smoke(root: str | None = None) -> dict:
    with network_blocked():
        if root is not None:
            return _run(root)
        with tempfile.TemporaryDirectory(prefix="tim_release_smoke_") as tmp:
            return _run(tmp)


def _run(root: str) -> dict:
    emitted = []  # (rel, bytes) for the prohibited-key / no-absolute-path sweep

    def emit(rel, data):
        rel, digest, size = _write(root, rel, data)
        with open(os.path.join(root, rel), "rb") as fh:
            emitted.append((rel, fh.read()))
        return rel, digest, size

    rel_dir = "exchange/releases/rel_synth_eurusd_m1_7d"

    # 1. export request/response contracts (small slice) — exercises the v2 export pair.
    req = F._request()
    T.validate_export_request_v2(req)
    resp = F._response()
    T.validate_export_response_v2(resp)

    # 2. full 7-day NATIVE M1 dataset (fabricated), validated as a half-open bar sequence.
    range_start, range_end = _min(START), _min(START + timedelta(minutes=MINUTES))
    bars = [_bar(i) for i in range(MINUTES)]
    rs = T._minute_epoch(range_start, "range_start_utc", "smoke")
    re_ = T._minute_epoch(range_end, "range_end_utc", "smoke")
    observed = T.validate_bar_sequence(bars, "M1", rs, re_, "smoke")
    assert observed == MINUTES, (observed, MINUTES)

    # canonical data as deterministic NDJSON (compressed NDJSON is permitted for raw payloads).
    ndjson = b"\n".join(canonical_json_bytes(b) for b in bars) + b"\n"
    f_data = emit(f"{rel_dir}/eurusd_m1.ndjson", ndjson)

    # 3. quality report (PASS: complete, zero violations).
    quality = {
        "schema_version": "1.0", "source_id": SOURCE_ID, "canonical_symbol": SYMBOL, "timeframe": "M1",
        "representation": "bid_ohlc", "price_basis": "bid", "provenance": "NATIVE",
        "range_start_utc": range_start, "range_end_utc": range_end, "range_semantics": "[start,end)",
        "expected_bar_count": MINUTES, "observed_bar_count": observed, "duplicate_count": 0,
        "out_of_order_count": 0, "overlap_count": 0, "ohlc_violation_count": 0, "non_finite_count": 0,
        "gap_count": 0, "quality_status": "PASS", "quarantine_reasons": [],
        "evaluated_at_utc": "2026-01-12T00:02:00Z", "limitations": [F.SYNTH]}
    T.validate_quality_report(quality)
    f_quality = emit(f"{rel_dir}/quality.json", quality)

    # 4. watermark (advanced only after landing+verify+validate+quality).
    watermark = {
        "schema_version": "1.0", "source_id": SOURCE_ID, "canonical_symbol": SYMBOL, "timeframe": "M1",
        "representation": "bid_ohlc", "price_basis": "bid", "watermark_event_end_utc": range_end,
        "range_semantics": "[start,end)", "last_raw_object_sha256": f_data[1], "landed_and_verified": True,
        "validated": True, "quality_accepted": True, "advanced_at_utc": "2026-01-12T00:02:30Z",
        "limitations": [F.SYNTH]}
    T.validate_acquisition_watermark(watermark)
    f_watermark = emit(f"{rel_dir}/watermark.json", watermark)

    # 5. READY marker content (written LAST, hash embedded in the manifest beforehand).
    ready_rel = f"{rel_dir}/READY"
    ready_body = canonical_json_bytes({"release_id": "rel_synth_eurusd_m1_7d", "state": "READY", "synthetic": True})
    ready_sha = sha256_hex(ready_body)

    # 6. release manifest v2 (NATIVE), with REAL file digests.
    manifest = {
        "schema_version": "2.0", "release_id": "rel_synth_eurusd_m1_7d", "source_id": SOURCE_ID,
        "canonical_symbol": SYMBOL, "asset_class": "fx", "timeframe": "M1", "representation": "bid_ohlc",
        "price_basis": "bid", "provenance": "NATIVE", "derivation": None,
        "range_start_utc": range_start, "range_end_utc": range_end, "range_semantics": "[start,end)",
        "event_start_utc": range_start, "event_end_utc": range_end,
        "source_available_at_utc": "2026-01-12T00:00:02Z", "collector_observed_at_utc": "2026-01-12T00:00:05Z",
        "ingested_at_utc": "2026-01-12T00:00:06Z", "validated_at_utc": "2026-01-12T00:02:00Z",
        "published_at_utc": "2026-01-12T00:03:00Z", "row_count": MINUTES,
        "files": [
            {"rel_path": f_data[0], "role": "raw_ndjson", "content_sha256": f_data[1], "file_size_bytes": f_data[2], "row_count": MINUTES},
            {"rel_path": f_quality[0], "role": "quality_report", "content_sha256": f_quality[1], "file_size_bytes": f_quality[2], "row_count": None},
            {"rel_path": f_watermark[0], "role": "watermark", "content_sha256": f_watermark[1], "file_size_bytes": f_watermark[2], "row_count": None},
            {"rel_path": ready_rel, "role": "ready_marker", "content_sha256": ready_sha, "file_size_bytes": len(ready_body), "row_count": None},
        ],
        "quality_report_path": f_quality[0], "watermark_path": f_watermark[0],
        "release_completeness": "COMPLETE", "partial_release_allowed": False,
        "producer_version": "guvfx-tim-0.2.0-synth", "contract_version": "0.2", "signing_key_id": "synthkey1",
        "signature_path": f"{rel_dir}/manifest.sig.json",
        "ready_marker": {"present": True, "written_last": True, "ready_marker_path": ready_rel, "ready_marker_sha256": ready_sha},
        "licensing_classification": "internal_research_only", "limitations": [F.SYNTH]}
    T.validate_release_manifest_v2(manifest)
    manifest_bytes = canonical_json_bytes(manifest)
    f_manifest = emit(f"{rel_dir}/manifest.json", manifest)

    # 7. synthetic detached signature over the manifest bytes.
    key_id, pub_fp = "synthkey1", sha256_hex(b"synthetic-public-material")
    signature = {
        "schema_version": "1.0", "release_id": manifest["release_id"], "signed_manifest_path": f_manifest[0],
        "signed_manifest_sha256": sha256_hex(manifest_bytes), "signing_key_id": key_id,
        "public_key_fingerprint": pub_fp, "algorithm": T.SYNTHETIC_ALGORITHM, "signature_scheme": "detached",
        "signature_value": T.synthetic_sign(manifest_bytes, key_id, pub_fp), "synthetic": True,
        "created_at_utc": "2026-01-12T00:03:01Z", "limitations": [F.SYNTH]}
    T.validate_release_signature(signature)
    emit(manifest["signature_path"], signature)

    sig_ok = T.verify_release_signature(signature, manifest_bytes)
    tamper_detected = not T.verify_release_signature(signature, manifest_bytes + b" ")

    # 8. READY marker written LAST.
    emit(ready_rel, ready_body)

    # 9. re-read + verify every manifest-declared file digest matches what is on disk.
    hash_matches = True
    for entry in manifest["files"]:
        with open(os.path.join(root, entry["rel_path"]), "rb") as fh:
            if sha256_hex(fh.read()) != entry["content_sha256"]:
                hash_matches = False

    # 10. import receipt (TIM read-only consumer) — IMPORTED only when everything verified.
    receipt = {
        "schema_version": "1.0", "release_id": manifest["release_id"], "importer_identity": "tim_reader_synth",
        "source_release_manifest_sha256": sha256_hex(manifest_bytes), "signature_verified": bool(sig_ok),
        "hashes_verified": bool(hash_matches), "completeness_verified": True,
        "import_state": "IMPORTED" if (sig_ok and hash_matches) else "QUARANTINED",
        "quarantine_reason": None if (sig_ok and hash_matches) else "verification failed",
        "imported_at_utc": "2026-01-12T00:10:00Z", "limitations": [F.SYNTH]}
    T.validate_import_receipt(receipt)
    emit(f"{rel_dir}/import_receipt.json", receipt)

    # 11. quarantine case (incomplete bucket) + 12. gap-repair case (new immutable evidence).
    pf = F.positive_fixtures()
    quarantine_q = pf["market_data_quality_report_v1__quarantine"]
    T.validate_quality_report(quarantine_q)
    emit(f"{rel_dir}/quarantine/quality.json", quarantine_q)
    gap = pf["market_data_gap_v1"]
    T.validate_market_data_gap(gap)
    emit(f"{rel_dir}/gap_repair.json", gap)

    # 13. prohibited-key + no-absolute-path sweep over every emitted artefact.
    prohibited = None
    for rel, body in emitted:
        if rel.startswith("/") or ".." in rel.split("/"):
            raise AssertionError(f"absolute/traversal path emitted: {rel!r}")
        if scan_raw_for_prohibited_key_tokens(body):
            prohibited = rel
        try:
            if find_prohibited_key(json.loads(body.decode("utf-8"))):
                prohibited = rel
        except (ValueError, UnicodeDecodeError):
            pass  # the NDJSON data file is not a single JSON object; token scan above covers it

    # MEASURE (not assert) that the READY marker was the LAST of the release artefacts to be written.
    order = [rel for rel, _ in emitted]
    release_arts = [f_data[0], f_quality[0], f_watermark[0], f_manifest[0], manifest["signature_path"]]
    ready_written_last = all(order.index(ready_rel) > order.index(a) for a in release_arts)

    assert sig_ok and tamper_detected and hash_matches and prohibited is None and ready_written_last

    return {
        "ok": True, "synthetic": True,
        "release_id": manifest["release_id"], "symbol": SYMBOL, "timeframe": "M1", "days": DAYS,
        "bar_count": MINUTES, "range_start_utc": range_start, "range_end_utc": range_end,
        "manifest_sha256": sha256_hex(manifest_bytes), "data_sha256": f_data[1], "ready_sha256": ready_sha,
        "signature_verified": bool(sig_ok), "tamper_detected": bool(tamper_detected),
        "all_file_hashes_match": bool(hash_matches), "ready_written_last": bool(ready_written_last),
        "import_state": receipt["import_state"], "quarantine_case_validated": True,
        "gap_repair_case_validated": True, "emitted_file_count": len(emitted),
        "prohibited_key_found": prohibited, "no_absolute_paths": True,
        "network": "blocked", "nas": "none", "mt5": "none", "broker": "none",
        "credentials": "none", "production": "none", "real_signing_key": "none",
    }


def main() -> int:
    summary = run_smoke()
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
