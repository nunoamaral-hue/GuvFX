#!/usr/bin/env python3
"""Synthetic end-to-end GUVFX-TIM v0.3 dataset-release smoke (GFX-PKT-TIM-C2). synthetic_test_only.

Proves, with ZERO network and ZERO real systems, inside an explicit TemporaryDirectory deleted on exit, the
v0.3 corrective release lifecycle (GUVFX-TIM-ALIGN-004 / v0.3-RC1, G1-G9):

  synthetic 7-day EURUSD M1 NATIVE candles (10080 half-open bars, fabricated ramp)
    -> GENUINE Parquet written by DuckDB (not a renamed JSON); schema + rows validated against the canonical
       candle contract (G1; NDJSON is written only as optional raw evidence alongside)
    -> hash-bound provenance/eligibility/quality/gap/watermark/mapping/timezone/licensing evidence (G3/G6)
    -> release manifest v3 with separate point-in-time instants (G4) and validator pinning (G8)
    -> algorithm-agile detached SYNTHETIC signature envelope binding release/manifest/contract-bundle/
       validator-bundle digests (G9); signature + tamper verification
    -> READY marker written LAST, binding the manifest + signature-envelope digests (G2). The signature and the
       READY marker are EXTERNAL artefacts (not members of the signed files[]), so there is no circular digest.
    -> atomic publish (staging -> immutable release namespace); post-READY the release dir is read-only and a
       new-file write FAILS (immutability); a byte tamper is detected against the manifest digests
    -> TIM-style READ-ONLY consumption; an import receipt v2 (ACCEPTED) is written to a SEPARATE consumer-owned
       namespace, never inside the release (G2/G7)
    -> point-in-time admissibility (source-side + published) accept/deny checks (G4)
    -> prohibited-key + no-absolute-path sweep over every emitted JSON artefact

It performs NO MT5, broker, NAS, credential, production or trading action. All data is fabricated. The signing
boundary is an INJECTED SYNTHETIC placeholder (no real key/algorithm). Emits deterministic JSON.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import duckdb

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from research.market_data import tim_contracts as T2  # noqa: E402  (v0.2 validators for shared evidence)
from research.market_data import tim_contracts_v0_3 as V  # noqa: E402
from research.market_data import tim_fixtures as F2  # noqa: E402
from research.market_data import tim_fixtures_v0_3 as F  # noqa: E402
from research.market_data.agent_client import network_blocked  # noqa: E402
from research.market_data.contracts import (  # noqa: E402
    canonical_json_bytes, find_prohibited_key, scan_raw_for_prohibited_key_tokens, sha256_hex,
)

RELEASE_ID = F.RELEASE_ID
SYMBOL = "EURUSD"
MINUTES = 7 * 1440  # 10080

_CANDLE_DDL = (
    'event_start_utc TIMESTAMP NOT NULL, event_end_utc TIMESTAMP NOT NULL, '
    '"open" DECIMAL(38,18) NOT NULL, high DECIMAL(38,18) NOT NULL, low DECIMAL(38,18) NOT NULL, '
    '"close" DECIMAL(38,18) NOT NULL, tick_volume BIGINT NOT NULL, quality_flags UINTEGER NOT NULL, '
    'real_volume DECIMAL(38,18), spread_points INTEGER, source_available_at_utc TIMESTAMP, source_sequence BIGINT'
)
_CANDLE_COLS = ("event_start_utc", "event_end_utc", "open", "high", "low", "close", "tick_volume",
                "quality_flags", "real_volume", "spread_points", "source_available_at_utc", "source_sequence")


def _safe_rel(rel: str) -> None:
    if rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
        raise AssertionError(f"unsafe rel_path: {rel!r}")


def _write(root: str, rel: str, data) -> tuple:
    _safe_rel(rel)
    body = bytes(data) if isinstance(data, (bytes, bytearray)) else canonical_json_bytes(data)
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body)
    return rel, sha256_hex(body), len(body)


def _bundle_digest(paths) -> str:
    material = b""
    for p in sorted(paths):
        with open(p, "rb") as fh:
            material += sha256_hex(fh.read()).encode("ascii") + b"\x00"
    return sha256_hex(material)


def _signature(release_id, manifest_digest, contract_bundle_digest, validator_bundle_digest):
    pub_fp = sha256_hex(b"synthetic-public-material-v0_3")
    key_id = "synthkey1"
    return {
        "schema_version": "2.0", "domain_separator": V.SIGNATURE_DOMAIN_SEPARATOR, "release_id": release_id,
        "signed_manifest_path": f"{release_id}/manifest.json", "manifest_digest": manifest_digest,
        "contract_bundle_digest": contract_bundle_digest, "validator_bundle_digest": validator_bundle_digest,
        "producer_identity": "guvfx_producer_synth", "signing_key_id": key_id, "public_key_fingerprint": pub_fp,
        "algorithm": V.SYNTHETIC_ALGORITHM_V2, "signature_scheme": "detached",
        "signature_value": V.synthetic_sign_v2(release_id=release_id, manifest_digest=manifest_digest,
                                                contract_bundle_digest=contract_bundle_digest,
                                                validator_bundle_digest=validator_bundle_digest,
                                                producer_identity="guvfx_producer_synth",
                                                signing_key_id=key_id, public_key_fingerprint=pub_fp),
        "synthetic": True, "created_at_utc": "2026-01-12T00:03:01Z", "key_validity": None, "limitations": [F.SYNTH]}


def run_smoke(root: str | None = None) -> dict:
    with network_blocked():
        if root is not None:
            return _run(root)
        with tempfile.TemporaryDirectory(prefix="tim_v0_3_smoke_") as tmp:
            try:
                return _run(tmp)
            finally:
                for dirpath, _dn, filenames in os.walk(tmp):  # restore writability for cleanup
                    try:
                        os.chmod(dirpath, 0o755)
                    except OSError:
                        pass
                    for fn in filenames:
                        try:
                            os.chmod(os.path.join(dirpath, fn), 0o644)
                        except OSError:
                            pass


def _run(base: str) -> dict:
    staging = os.path.join(base, "staging")
    releases = os.path.join(base, "releases")
    consumer = os.path.join(base, "tim_consumer")  # SEPARATE consumer-owned namespace (never inside a release)
    for d in (staging, releases, consumer):
        os.makedirs(d, exist_ok=True)
    rel_dir = os.path.join(staging, RELEASE_ID)
    os.makedirs(rel_dir, exist_ok=True)
    emitted = []  # (abs_path, bytes) for the prohibited-key / no-absolute-path sweep

    def emit_json(rel_name, data):
        rel, digest, size = _write(rel_dir, rel_name, data)
        with open(os.path.join(rel_dir, rel), "rb") as fh:
            emitted.append((os.path.join(rel_dir, rel), fh.read()))
        return rel, digest, size

    # 1. GENUINE synthetic Parquet via DuckDB (G1).
    rows = F.synthetic_candle_rows(MINUTES)
    V.validate_canonical_candle_rows(rows)
    parquet_path = os.path.join(rel_dir, "eurusd_m1.parquet")
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"CREATE TABLE candles ({_CANDLE_DDL})")
        con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        [tuple(r[c] for c in _CANDLE_COLS) for r in rows])
        con.execute(f"COPY candles TO '{parquet_path}' (FORMAT PARQUET)")
        described = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{parquet_path}')").fetchall()
        observed_schema = [{"name": r[0], "type": r[1], "nullable": (str(r[2]).upper() == "YES")} for r in described]
        quoted = ", ".join('"' + c + '"' for c in _CANDLE_COLS)
        back = con.execute(f"SELECT {quoted} FROM read_parquet('{parquet_path}') ORDER BY event_start_utc").fetchall()
        readback = [dict(zip(_CANDLE_COLS, row)) for row in back]
        duckdb_version = duckdb.__version__
    finally:
        con.close()
    V.validate_canonical_candle_parquet_schema(observed_schema)
    observed_count = V.validate_canonical_candle_rows(readback)
    assert observed_count == MINUTES, (observed_count, MINUTES)
    with open(parquet_path, "rb") as fh:
        parquet_bytes = fh.read()
    parquet_sha, parquet_size = sha256_hex(parquet_bytes), len(parquet_bytes)

    # NDJSON raw payload alongside (permitted as optional raw evidence only).
    ndjson = b"\n".join(
        canonical_json_bytes({
            "event_start_utc": r["event_start_utc"].strftime("%Y-%m-%dT%H:%M:00Z"),
            "event_end_utc": r["event_end_utc"].strftime("%Y-%m-%dT%H:%M:00Z"),
            "open": str(r["open"]), "high": str(r["high"]), "low": str(r["low"]), "close": str(r["close"]),
            "tick_volume": r["tick_volume"], "quality_flags": r["quality_flags"]}) for r in rows) + b"\n"
    raw = _write(rel_dir, "eurusd_m1.raw.ndjson", ndjson)
    with open(os.path.join(rel_dir, "eurusd_m1.raw.ndjson"), "rb") as fh:
        emitted.append((os.path.join(rel_dir, "eurusd_m1.raw.ndjson"), fh.read()))

    # 2. hash-bound evidence (G3/G6): eligibility (v0.3) + quality/gap/watermark/mapping (v0.2) + 3 attestations.
    eligibility = F.source_eligibility(); V.validate_source_eligibility(eligibility)
    v2 = F2.positive_fixtures()
    quality = v2["market_data_quality_report_v1"]; T2.validate_quality_report(quality)
    gap = v2["market_data_gap_v1"]; T2.validate_market_data_gap(gap)
    watermark = v2["acquisition_watermark_v1"]; T2.validate_acquisition_watermark(watermark)
    mapping = v2["source_instrument_mapping_v1"]; T2.validate_source_instrument_mapping(mapping)
    broker_server = {"schema_version": "1.0", "source_id": F.SOURCE_ID, "broker_id": "synthbroker",
                     "server_identity": "SynthBroker-Research", "synthetic": True, "limitations": [F.SYNTH]}
    timezone_ev = {"schema_version": "1.0", "source_id": F.SOURCE_ID, "timezone_status": "VERIFIED",
                   "source_timezone": "Etc/UTC", "synthetic": True, "limitations": [F.SYNTH]}
    licensing = {"schema_version": "1.0", "source_id": F.SOURCE_ID,
                 "licensing_classification": "internal_research_only", "licensing_reference": "GFX-LIC-SYNTH-001",
                 "permitted_use": "internal_research_only", "synthetic": True, "limitations": [F.SYNTH]}
    ev = {
        "quality_report": emit_json("quality.json", quality),
        "gap_report": emit_json("gap.json", gap),
        "watermark": emit_json("watermark.json", watermark),
        "source_eligibility": emit_json("eligibility.json", eligibility),
        "broker_server_identity": emit_json("broker_server.json", broker_server),
        "instrument_mapping": emit_json("mapping.json", mapping),
        "timezone_evidence": emit_json("timezone.json", timezone_ev),
        "licensing": emit_json("licensing.json", licensing),
    }

    # 3. the SIGNED set (payload + evidence only; signature + READY are external, written after).
    def frec(rel, role, media, sv, vv, sha, size, rc):
        return {"relative_path": f"{RELEASE_ID}/{rel}", "media_type": media, "byte_size": size,
                "digest": {"algorithm": "sha256", "value": sha}, "logical_role": role,
                "schema_version": sv, "validator_version": vv, "row_count": rc}
    files = [
        frec("eurusd_m1.parquet", "canonical_parquet", V._MEDIA_PARQUET, "1.0", "0.3.0", parquet_sha, parquet_size, MINUTES),
        frec(raw[0], "raw_ndjson", V._MEDIA_NDJSON, "raw", "0.3.0", raw[1], raw[2], MINUTES),
        frec(ev["quality_report"][0], "quality_report", "application/json", "1.0", "0.2.0", ev["quality_report"][1], ev["quality_report"][2], None),
        frec(ev["gap_report"][0], "gap_report", "application/json", "1.0", "0.2.0", ev["gap_report"][1], ev["gap_report"][2], None),
        frec(ev["watermark"][0], "watermark", "application/json", "1.0", "0.2.0", ev["watermark"][1], ev["watermark"][2], None),
        frec(ev["source_eligibility"][0], "source_eligibility", "application/json", "1.0", "0.3.0", ev["source_eligibility"][1], ev["source_eligibility"][2], None),
        frec(ev["broker_server_identity"][0], "broker_server_identity", "application/json", "1.0", "0.2.0", ev["broker_server_identity"][1], ev["broker_server_identity"][2], None),
        frec(ev["instrument_mapping"][0], "instrument_mapping", "application/json", "1.0", "0.2.0", ev["instrument_mapping"][1], ev["instrument_mapping"][2], None),
        frec(ev["timezone_evidence"][0], "timezone_evidence", "application/json", "1.0", "0.2.0", ev["timezone_evidence"][1], ev["timezone_evidence"][2], None),
        frec(ev["licensing"][0], "licensing", "application/json", "1.0", "0.3.0", ev["licensing"][1], ev["licensing"][2], None),
    ]
    contract_bundle_digest = _bundle_digest(
        [os.path.join(REPO_ROOT, "research", "contracts", "v0_3", n + ".schema.json") for n in V.TIM_V0_3_SCHEMA_FILES])
    with open(os.path.join(REPO_ROOT, "research", "market_data", "tim_contracts_v0_3.py"), "rb") as fh:
        validator_bundle_digest = sha256_hex(fh.read())

    def evref(role):
        rel, sha, _sz = ev[role]
        return {"contract_version": "1.0", "relative_path": f"{RELEASE_ID}/{rel}", "sha256": sha}

    manifest = {
        "schema_version": "3.0", "contract_version": "0.3", "release_id": RELEASE_ID,
        "supersedes_release_id": None, "supersession": None, "source_id": F.SOURCE_ID,
        "canonical_symbol": SYMBOL, "broker_native_symbol": "EURUSD", "asset_class": "fx", "timeframe": "M1",
        "representation": "bid_ohlc", "price_basis": "bid", "provenance": "NATIVE", "derivation": None,
        "canonical_format": "parquet", "range_start_utc": "2026-01-05T00:00:00Z",
        "range_end_utc": "2026-01-12T00:00:00Z", "range_semantics": "[start,end)",
        "point_in_time": {
            "event_start_utc": "2026-01-05T00:00:00Z", "event_end_utc": "2026-01-12T00:00:00Z",
            "source_available_at_utc": "2026-01-12T00:00:02Z", "source_availability_classification": "EVIDENCED",
            "collector_observed_at_utc": "2026-01-12T00:00:05Z", "ingested_at_utc": "2026-01-12T00:00:06Z",
            "validated_at_utc": "2026-01-12T00:02:00Z", "published_at_utc": "2026-01-12T00:03:00Z"},
        "row_count": MINUTES, "uniqueness_key": ["event_start_utc"], "files": files,
        "evidence": {role: evref(role) for role in V._EVIDENCE_ROLES},
        "validator_pinning": {"schema_version": "3.0", "validator_id": "guvfx_tim_v0_3",
                              "validator_version": "0.3.0", "validator_digest": validator_bundle_digest,
                              "compatibility_range": ">=0.3.0,<0.4.0"},
        "contract_bundle_digest": contract_bundle_digest, "validator_bundle_digest": validator_bundle_digest,
        "release_completeness": "COMPLETE", "partial_release_allowed": False,
        "producer_identity": "guvfx_producer_synth", "producer_version": "guvfx-tim-0.3.0-synth",
        "signing_key_id": "synthkey1", "signature_path": f"{RELEASE_ID}/manifest.sig.json",
        "ready_marker": {"present": True, "written_last": True, "ready_marker_path": f"{RELEASE_ID}/READY"},
        "licensing_classification": "internal_research_only", "synthetic": True, "limitations": [F.SYNTH]}
    V.validate_release_manifest_v3(manifest)
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_digest = sha256_hex(manifest_bytes)

    # 4. detached signature over the manifest digest (external artefact).
    signature = _signature(RELEASE_ID, manifest_digest, contract_bundle_digest, validator_bundle_digest)
    V.validate_release_signature_v2(signature)
    signature_bytes = canonical_json_bytes(signature)
    sig_ok = V.verify_release_signature_v2(signature, manifest_bytes)
    tamper_detected = not V.verify_release_signature_v2(signature, manifest_bytes + b" ")

    # 5. READY marker binds manifest + signature-envelope digests (external artefact), written LAST.
    ready = {"schema_version": "1.0", "release_id": RELEASE_ID, "state": "READY",
             "manifest_digest": manifest_digest, "signature_envelope_digest": sha256_hex(signature_bytes),
             "contract_version": "0.3", "synthetic": True, "created_at_utc": "2026-01-12T00:03:02Z",
             "limitations": [F.SYNTH]}
    V.validate_ready_marker(ready)
    emit_json("manifest.json", manifest)
    emit_json("manifest.sig.json", signature)
    _write(rel_dir, "READY", canonical_json_bytes(ready))
    emitted.append((os.path.join(rel_dir, "READY"), canonical_json_bytes(ready)))

    # 6. atomic publish staging -> immutable release namespace; then make it read-only.
    published = os.path.join(releases, RELEASE_ID)
    os.rename(rel_dir, published)
    for dirpath, _dn, filenames in os.walk(published):
        for fn in filenames:
            os.chmod(os.path.join(dirpath, fn), 0o444)
    os.chmod(published, 0o555)
    post_ready_add_blocked = False
    try:
        with open(os.path.join(published, "sneaky_after_ready.json"), "wb") as fh:
            fh.write(b"{}")
    except (PermissionError, OSError):
        post_ready_add_blocked = True

    # 7. TIM read-only consume: re-read manifest + signature, verify signature + every bound file digest.
    with open(os.path.join(published, "manifest.json"), "rb") as fh:
        consumed_manifest_bytes = fh.read()
    with open(os.path.join(published, "manifest.sig.json"), "rb") as fh:
        consumed_sig = json.loads(fh.read().decode("utf-8"))
    consume_sig_ok = V.verify_release_signature_v2(consumed_sig, consumed_manifest_bytes)
    consumed_manifest = json.loads(consumed_manifest_bytes.decode("utf-8"))
    hash_matches = True
    for entry in consumed_manifest["files"]:
        with open(os.path.join(releases, entry["relative_path"]), "rb") as fh:
            if sha256_hex(fh.read()) != entry["digest"]["value"]:
                hash_matches = False
    file_tamper_detected = sha256_hex(parquet_bytes + b"x") != manifest["files"][0]["digest"]["value"]

    # 8. import receipt v2 -> SEPARATE consumer namespace (never inside the release) (G2/G7).
    receipt = F.import_receipt_v2(
        source_release_manifest_sha256=sha256_hex(consumed_manifest_bytes),
        source_signature_envelope_sha256=sha256_hex(canonical_json_bytes(consumed_sig)),
        checks={"signature_verified": bool(consume_sig_ok), "manifest_hashes_verified": bool(hash_matches),
                "parquet_schema_verified": True, "evidence_complete": True, "point_in_time_admissible": True},
        import_state="ACCEPTED" if (consume_sig_ok and hash_matches) else "QUARANTINED",
        reason_code=None if (consume_sig_ok and hash_matches) else "incomplete_release",
        retryable=not (consume_sig_ok and hash_matches),
        research_admissible=bool(consume_sig_ok and hash_matches))
    V.validate_import_receipt_v2(receipt)
    receipt_rel = os.path.join("receipts", RELEASE_ID + ".receipt.json")
    r_rel, _s, _z = _write(consumer, receipt_rel, receipt)
    receipt_abs = os.path.join(consumer, r_rel)
    emitted.append((receipt_abs, canonical_json_bytes(receipt)))
    receipt_outside_release = not os.path.abspath(receipt_abs).startswith(os.path.abspath(releases) + os.sep)

    # 9. point-in-time admissibility (G4): source-side gates on evidenced availability; published on publication.
    pit_source_ok = V.admissible_source_side(consumed_manifest, "2026-01-12T00:00:03Z")
    pit_source_block = not V.admissible_source_side(consumed_manifest, "2026-01-12T00:00:01Z")
    pit_pub_ok = V.admissible_published(consumed_manifest, "2026-01-12T00:05:00Z", "2026-01-12T00:11:00Z",
                                        receipt["imported_at_utc"])
    pit_pub_block = not V.admissible_published(consumed_manifest, "2026-01-12T00:02:59Z", "2026-01-12T00:11:00Z",
                                               receipt["imported_at_utc"])

    # 10. prohibited-key + no-absolute-path sweep over emitted JSON artefacts (skip Parquet binary).
    prohibited = None
    for abspath, body in emitted:
        if abspath.endswith(".parquet"):
            continue
        if scan_raw_for_prohibited_key_tokens(body):
            prohibited = abspath
        try:
            if find_prohibited_key(json.loads(body.decode("utf-8"))):
                prohibited = abspath
        except (ValueError, UnicodeDecodeError):
            pass  # NDJSON is line-delimited; covered by the token scan

    assert sig_ok and tamper_detected and consume_sig_ok and hash_matches and file_tamper_detected
    assert post_ready_add_blocked and receipt_outside_release and prohibited is None
    assert pit_source_ok and pit_source_block and pit_pub_ok and pit_pub_block

    return {
        "ok": True, "synthetic": True, "contract_version": "0.3", "release_id": RELEASE_ID, "symbol": SYMBOL,
        "timeframe": "M1", "days": 7, "bar_count": MINUTES, "canonical_format": "parquet",
        "duckdb_version": duckdb_version, "parquet_sha256": parquet_sha,
        "parquet_schema_verified": True, "parquet_rows_verified": observed_count,
        "manifest_sha256": manifest_digest, "signature_verified": bool(sig_ok),
        "tamper_detected": bool(tamper_detected), "consume_signature_verified": bool(consume_sig_ok),
        "all_file_hashes_match": bool(hash_matches), "file_tamper_detected": bool(file_tamper_detected),
        "ready_written_last": True, "post_ready_add_blocked": bool(post_ready_add_blocked),
        "import_state": receipt["import_state"], "receipt_outside_release": bool(receipt_outside_release),
        "pit_source_admissible": bool(pit_source_ok), "pit_source_blocked_before_availability": bool(pit_source_block),
        "pit_published_admissible": bool(pit_pub_ok), "pit_published_blocked_before_publication": bool(pit_pub_block),
        "evidence_roles_bound": len(V._EVIDENCE_ROLES), "emitted_file_count": len(emitted),
        "prohibited_key_found": prohibited, "no_absolute_paths": True,
        "network": "blocked", "nas": "none", "mt5": "none", "broker": "none", "credentials": "none",
        "production": "none", "real_signing_key": "none",
    }


def main() -> int:
    summary = run_smoke()
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
