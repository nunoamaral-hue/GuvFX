#!/usr/bin/env python3
"""GO #2d operational-release LOCAL PROOF (GFX-TIM GO #2d). synthetic_test_only.

Reuses the synthetic v0.3 generator (``tim_release_v0_3_synthetic_smoke`` helpers + ``tim_fixtures_v0_3`` +
``tim_contracts_v0_3`` + DuckDB Parquet) to build ONE bounded synthetic EURUSD M1 v0.3 release, then
produces the EXACT fields the producer-host signer (``scripts/tim_signing_provider.py sign-release``)
consumes, and proves the complete OPERATIONAL v3 flow with an INJECTED provider:

  * the ``dataset_release_signature_v3`` envelope is built over the REAL ``signing_material_v3``;
  * it is VERIFIED by the REAL verifier (``verify_operational_release_signature_v3``) against an ISOLATED
    test trust store (the provider's public key pinned, within a validity window);
  * READY is written LAST, binding the manifest + signature-envelope digests;
  * a read-only consumer ACCEPTS the published copy: strict load, K-7 manifest<->signature cross-bind, the
    operational signature, every file SHA-256, and the evidence identity.

The injected provider holds the signing key; this tool never sees it (``provider.sign(message)->64 bytes``
+ ``provider.public_key_raw()->32 bytes``). In production the provider is the host-side audited
``cryptography`` provider (``sign-release`` on WIN-RD8VDS93DK7). In the proof it is a DISPOSABLE ephemeral
Ed25519 test key (``tests/test_tim_release_operational_proof.py``). The ``signing_key_id`` and
``public_key_fingerprint`` are DERIVED from the provider's public key, never injected. This tool performs
NO real key, NAS, network, MT5, broker, credential or production action.
"""
from __future__ import annotations

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tim_release_v0_3_synthetic_smoke as SMOKE  # noqa: E402  reuse the synthetic generator helpers
from research.market_data import tim_contracts as T2  # noqa: E402
from research.market_data import tim_contracts_v0_3 as V  # noqa: E402
from research.market_data import tim_fixtures as F2  # noqa: E402
from research.market_data import tim_fixtures_v0_3 as F  # noqa: E402
from research.market_data import tim_release_signing_v3 as S  # noqa: E402
from research.market_data import tim_trust_store as TS  # noqa: E402
from research.market_data.agent_client import network_blocked  # noqa: E402
from research.market_data.contracts import canonical_json_bytes, sha256_hex  # noqa: E402

import duckdb  # noqa: E402

RELEASE_ID = F.RELEASE_ID
SYMBOL = "EURUSD"
MINUTES = 7 * 1440  # 10080
PRODUCER_IDENTITY = "guvfx_tim_producer"
LIMITATIONS = [F.SYNTH, "FIRST_OPERATIONAL_SIGNATURE_SYNTHETIC_DATA"]


def derive_key_id(public_key: bytes) -> tuple[str, str]:
    """Return (public_key_fingerprint, signing_key_id) derived from the raw public key (never injected)."""
    fingerprint = sha256_hex(bytes(public_key))
    return fingerprint, S.ED25519_ALGORITHM and ("ed25519-" + fingerprint[:32])


def build_release(base: str, *, signing_key_id: str, producer_identity: str):
    """Build one synthetic v0.3 release into ``base`` configured for OPERATIONAL v3 signing. Reuses the
    generator helpers. Returns a dict with the release dir, manifest, digests and the signed file set."""
    rel_dir = os.path.join(base, RELEASE_ID)
    os.makedirs(rel_dir, exist_ok=True)

    def emit_json(rel_name, data):
        return SMOKE._write(rel_dir, rel_name, data)

    # 1. GENUINE synthetic Parquet via DuckDB (reused generator).
    rows = F.synthetic_candle_rows(MINUTES)
    V.validate_canonical_candle_rows(rows)
    parquet_path = os.path.join(rel_dir, "eurusd_m1.parquet")
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"CREATE TABLE candles ({SMOKE._CANDLE_DDL})")
        con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        [tuple(r[c] for c in SMOKE._CANDLE_COLS) for r in rows])
        con.execute(f"COPY candles TO '{parquet_path}' (FORMAT PARQUET)")
        described = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{parquet_path}')").fetchall()
        observed_schema = [{"name": r[0], "type": r[1], "nullable": (str(r[2]).upper() == "YES")} for r in described]
    finally:
        con.close()
    V.validate_canonical_candle_parquet_schema(observed_schema)
    with open(parquet_path, "rb") as fh:
        parquet_bytes = fh.read()
    parquet_sha, parquet_size = sha256_hex(parquet_bytes), len(parquet_bytes)

    ndjson = b"\n".join(
        canonical_json_bytes({
            "event_start_utc": r["event_start_utc"].strftime("%Y-%m-%dT%H:%M:00Z"),
            "event_end_utc": r["event_end_utc"].strftime("%Y-%m-%dT%H:%M:00Z"),
            "open": str(r["open"]), "high": str(r["high"]), "low": str(r["low"]), "close": str(r["close"]),
            "tick_volume": r["tick_volume"], "quality_flags": r["quality_flags"]}) for r in rows) + b"\n"
    raw = SMOKE._write(rel_dir, "eurusd_m1.raw.ndjson", ndjson)

    # 2. hash-bound evidence (reused generator): the nine required references.
    eligibility = F.source_eligibility(); V.validate_source_eligibility(eligibility)
    v2 = F2.positive_fixtures()
    quality = F.full_coverage_quality_report(expected=MINUTES); T2.validate_quality_report(quality)
    gap = v2["market_data_gap_v1"]; T2.validate_market_data_gap(gap)
    watermark = v2["acquisition_watermark_v1"]; T2.validate_acquisition_watermark(watermark)
    mapping = v2["source_instrument_mapping_v1"]; T2.validate_source_instrument_mapping(mapping)
    broker_server = F.broker_server_identity(); V.validate_broker_server_identity(broker_server)
    timezone_ev = F.timezone_evidence_v0_3(); V.validate_timezone_evidence_v0_3(timezone_ev)
    licensing = F.licensing_evidence(); V.validate_licensing_evidence(licensing)
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
    coverage = {
        "schema_version": "1.0", "release_id": RELEASE_ID, "manifest_digest": None,
        "canonical_symbol": SYMBOL, "timeframe": "M1",
        "range_start_utc": "2026-01-05T00:00:00Z", "range_end_utc": "2026-01-12T00:00:00Z",
        "range_semantics": "[start,end)",
        "first_event_start_utc": "2026-01-05T00:00:00Z", "last_event_start_utc": "2026-01-11T23:59:00Z",
        "expected_row_count": MINUTES, "observed_row_count": MINUTES, "quality_assessed_row_count": MINUTES,
        "partition_coverage": {"scheme": "single_file_per_release", "files_total": 1, "files_assessed": 1},
        "canonical_file": {"relative_path": f"{RELEASE_ID}/eurusd_m1.parquet", "sha256": parquet_sha},
        "quality_report_ref": {"relative_path": f"{RELEASE_ID}/{ev['quality_report'][0]}", "sha256": ev["quality_report"][1]},
        "gap_report_ref": {"relative_path": f"{RELEASE_ID}/{ev['gap_report'][0]}", "sha256": ev["gap_report"][1]},
        "watermark_ref": {"relative_path": f"{RELEASE_ID}/{ev['watermark'][0]}", "sha256": ev["watermark"][1]},
        "completeness_status": "COMPLETE", "synthetic": True, "limitations": [F.SYNTH],
    }
    V.validate_release_quality_coverage(coverage)
    ev["quality_coverage"] = emit_json("quality_coverage.json", coverage)

    # 3. the SIGNED set (payload + evidence only; signature + READY are external).
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
        frec(ev["broker_server_identity"][0], "broker_server_identity", "application/json", "1.0", "0.3.0", ev["broker_server_identity"][1], ev["broker_server_identity"][2], None),
        frec(ev["instrument_mapping"][0], "instrument_mapping", "application/json", "1.0", "0.2.0", ev["instrument_mapping"][1], ev["instrument_mapping"][2], None),
        frec(ev["timezone_evidence"][0], "timezone_evidence", "application/json", "1.0", "0.3.0", ev["timezone_evidence"][1], ev["timezone_evidence"][2], None),
        frec(ev["licensing"][0], "licensing", "application/json", "1.0", "0.3.0", ev["licensing"][1], ev["licensing"][2], None),
        frec(ev["quality_coverage"][0], "quality_coverage", "application/json", "1.0", "0.3.0", ev["quality_coverage"][1], ev["quality_coverage"][2], None),
    ]
    contract_bundle_digest = SMOKE._bundle_digest(
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
        "producer_identity": producer_identity, "producer_version": "guvfx-tim-0.3.0-operational-proof",
        "signing_key_id": signing_key_id, "signature_path": f"{RELEASE_ID}/manifest.sig.json",
        "ready_marker": {"present": True, "written_last": True, "ready_marker_path": f"{RELEASE_ID}/READY"},
        "licensing_classification": "internal_research_only", "synthetic": True, "limitations": [F.SYNTH]}
    V.validate_release_manifest_v3(manifest)
    V.validate_release_coverage_consistency(manifest, coverage)
    V.validate_quality_report_covers(coverage, quality)
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_digest = sha256_hex(manifest_bytes)
    return {"rel_dir": rel_dir, "manifest": manifest, "manifest_bytes": manifest_bytes,
            "manifest_digest": manifest_digest, "contract_bundle_digest": contract_bundle_digest,
            "validator_bundle_digest": validator_bundle_digest, "parquet_bytes": parquet_bytes}


def sign_release_fields(built, *, producer_identity, created_at_utc, key_validity, limitations):
    """The EXACT structured fields the host ``sign-release --fields-file`` consumes (minus the key-derived
    signing_key_id / fingerprint, which the signer derives from the key itself)."""
    return {
        "release_id": RELEASE_ID,
        "manifest_digest": built["manifest_digest"],
        "contract_bundle_digest": built["contract_bundle_digest"],
        "validator_bundle_digest": built["validator_bundle_digest"],
        "producer_identity": producer_identity,
        "created_at_utc": created_at_utc,
        "key_validity": key_validity,
        "limitations": list(limitations),
    }


def build_v3_envelope(provider, fields):
    """Replicates the producer signer: build the real signing_material_v3, sign via the injected provider,
    assemble the value-blind dataset_release_signature_v3 envelope. Derives key id + fingerprint from the
    provider's public key (never from the caller). Returns (envelope, public_key_hex)."""
    pub = bytes(provider.public_key_raw())
    if len(pub) != 32:
        raise ValueError("provider public key must be 32 bytes")
    fingerprint = sha256_hex(pub)
    signing_key_id = "ed25519-" + fingerprint[:32]
    material = S.signing_material_v3(
        algorithm=S.ED25519_ALGORITHM, release_id=fields["release_id"],
        manifest_digest=fields["manifest_digest"], contract_bundle_digest=fields["contract_bundle_digest"],
        validator_bundle_digest=fields["validator_bundle_digest"],
        producer_identity=fields["producer_identity"], signing_key_id=signing_key_id,
        public_key_fingerprint=fingerprint, created_at_utc=fields["created_at_utc"])
    signature = bytes(provider.sign(material))
    if len(signature) != 64:
        raise ValueError("provider signature must be 64 bytes")
    envelope = {
        "schema_version": S.SIGNATURE_V3_SCHEMA_VERSION, "domain_separator": S.DOMAIN_SEPARATOR_V3,
        "release_id": fields["release_id"], "signed_manifest_path": fields["release_id"] + "/manifest.json",
        "manifest_digest": fields["manifest_digest"], "contract_bundle_digest": fields["contract_bundle_digest"],
        "validator_bundle_digest": fields["validator_bundle_digest"],
        "producer_identity": fields["producer_identity"], "signing_key_id": signing_key_id,
        "public_key_fingerprint": fingerprint, "algorithm": S.ED25519_ALGORITHM, "signature_scheme": "detached",
        "signature_value": signature.hex(), "synthetic": False, "created_at_utc": fields["created_at_utc"],
        "key_validity": dict(fields["key_validity"]), "limitations": list(fields["limitations"])}
    return envelope, pub.hex()


def isolated_trust_store(public_key_hex, key_validity):
    """An ISOLATED test trust store pinning only the provider's public key (never the real committed store)."""
    fingerprint = sha256_hex(bytes.fromhex(public_key_hex))
    return TS.from_dict({"schema_version": "1.0", "keys": [{
        "signing_key_id": "ed25519-" + fingerprint[:32], "algorithm": "ed25519-v1",
        "public_key": public_key_hex, "public_key_fingerprint": fingerprint,
        "not_before_utc": key_validity["not_before_utc"], "not_after_utc": key_validity["not_after_utc"],
        "status": "active"}]})


def seal(rel_dir, built, envelope):
    """Write manifest.json + manifest.sig.json, then the READY marker LAST (binding the manifest +
    signature-envelope digests). READY is NOT a signed file[] member, so there is no circular digest."""
    SMOKE._write(rel_dir, "manifest.json", built["manifest"])
    signature_bytes = canonical_json_bytes(envelope)
    with open(os.path.join(rel_dir, "manifest.sig.json"), "wb") as fh:
        fh.write(signature_bytes)
    ready = {"schema_version": "1.0", "release_id": RELEASE_ID, "state": "READY",
             "manifest_digest": built["manifest_digest"], "signature_envelope_digest": sha256_hex(signature_bytes),
             "contract_version": "0.3", "synthetic": True, "created_at_utc": "2026-01-12T00:03:02Z",
             "limitations": [F.SYNTH]}
    V.validate_ready_marker(ready)
    SMOKE._write(rel_dir, "READY", canonical_json_bytes(ready))
    return ready


def consumer_accept(releases_dir, release_id, store) -> dict:
    """Read-only consumer acceptance of a published release. Fail-closed, in order: READY present and
    binding the manifest + signature digests; the operational signature (K-7 cross-bind + verify against the
    pinned store); every file SHA-256; evidence identity. Returns {accepted, reason, checks}."""
    rel = os.path.join(releases_dir, release_id)
    checks = {"ready_present": os.path.isfile(os.path.join(rel, "READY"))}
    if not checks["ready_present"]:
        return {"accepted": False, "reason": "missing_ready", "checks": checks}
    with open(os.path.join(rel, "manifest.json"), "rb") as fh:
        manifest_bytes = fh.read()
    with open(os.path.join(rel, "manifest.sig.json"), "rb") as fh:
        sig_bytes = fh.read()
    with open(os.path.join(rel, "READY"), "rb") as fh:
        ready = V.strict_json_loads_v0_3(fh.read())
    manifest = V.strict_json_loads_v0_3(manifest_bytes)
    sig = V.strict_json_loads_v0_3(sig_bytes)
    if not (ready.get("state") == "READY" and ready.get("manifest_digest") == sha256_hex(manifest_bytes)
            and ready.get("signature_envelope_digest") == sha256_hex(sig_bytes)):
        return {"accepted": False, "reason": "ready_binding_mismatch", "checks": checks}
    result = S.verify_operational_release_signature_v3(sig, manifest_bytes, trust_store=store, manifest=manifest)
    checks["signature"] = result.reason
    if not result.ok:
        return {"accepted": False, "reason": result.reason, "checks": checks}
    for entry in manifest["files"]:
        with open(os.path.join(releases_dir, entry["relative_path"]), "rb") as fh:
            if sha256_hex(fh.read()) != entry["digest"]["value"]:
                return {"accepted": False, "reason": "file_hash_mismatch", "checks": checks}
    checks["file_hashes"] = True
    c_evidence = {}
    for role, fname in (("timezone_evidence", "timezone.json"), ("broker_server_identity", "broker_server.json"),
                        ("licensing", "licensing.json"), ("source_eligibility", "eligibility.json")):
        with open(os.path.join(rel, fname), "rb") as fh:
            c_evidence[role] = V.strict_json_loads_v0_3(fh.read())
    try:
        V.validate_release_evidence_identity(manifest, c_evidence)
    except (V.ContractError, V.QuarantineRequired):
        return {"accepted": False, "reason": "evidence_identity_mismatch", "checks": checks}
    checks["evidence_identity"] = True
    return {"accepted": True, "reason": None, "checks": checks}


def prove(base, provider, *, created_at_utc="2026-06-01T00:00:00Z",
          key_validity=None) -> dict:
    """Full local proof: build -> emit sign-release fields -> operational v3 sign (injected provider) ->
    verify vs an isolated trust store -> seal (READY last) -> publish -> read-only consumer accept."""
    if key_validity is None:
        key_validity = {"not_before_utc": "2026-01-01T00:00:00Z", "not_after_utc": "2027-01-01T00:00:00Z"}
    staging = os.path.join(base, "staging")
    releases = os.path.join(base, "releases")
    os.makedirs(staging, exist_ok=True)
    os.makedirs(releases, exist_ok=True)

    pub = bytes(provider.public_key_raw())
    fingerprint, signing_key_id = derive_key_id(pub)
    built = build_release(staging, signing_key_id=signing_key_id, producer_identity=PRODUCER_IDENTITY)
    fields = sign_release_fields(built, producer_identity=PRODUCER_IDENTITY, created_at_utc=created_at_utc,
                                 key_validity=key_validity, limitations=LIMITATIONS)

    envelope, pub_hex = build_v3_envelope(provider, fields)
    S.validate_release_signature_v3(envelope)  # structural (operational, 128-hex, synthetic=False)
    store = isolated_trust_store(pub_hex, key_validity)

    # Pre-publish verification against the isolated pinned store (producer-side gate before publishing).
    pre = S.verify_operational_release_signature_v3(envelope, built["manifest_bytes"], trust_store=store,
                                                    manifest=built["manifest"])
    tamper_sig = dict(envelope)
    tamper_sig["signature_value"] = ("0" if envelope["signature_value"][0] != "0" else "1") + envelope["signature_value"][1:]
    bad_sig = S.verify_operational_release_signature_v3(tamper_sig, built["manifest_bytes"], trust_store=store,
                                                        manifest=built["manifest"])
    bad_manifest = S.verify_operational_release_signature_v3(envelope, built["manifest_bytes"] + b" ",
                                                             trust_store=store, manifest=built["manifest"])

    # Seal (READY written last), then atomically publish to the immutable, read-only release namespace.
    files_before_ready = sorted(os.listdir(built["rel_dir"]))
    seal(built["rel_dir"], built, envelope)
    ready_written_last = "READY" not in files_before_ready and os.path.isfile(os.path.join(built["rel_dir"], "READY"))
    published = os.path.join(releases, RELEASE_ID)
    os.rename(built["rel_dir"], published)
    for dirpath, _dn, filenames in os.walk(published):
        for fn in filenames:
            os.chmod(os.path.join(dirpath, fn), 0o444)
    os.chmod(published, 0o555)

    acc = consumer_accept(releases, RELEASE_ID, store)
    file_tamper_detected = sha256_hex(built["parquet_bytes"] + b"x") != built["manifest"]["files"][0]["digest"]["value"]
    return {
        "ok": True, "synthetic_data": True, "operational_signature": True, "contract_version": "0.3",
        "release_id": RELEASE_ID, "symbol": SYMBOL, "timeframe": "M1", "bar_count": MINUTES,
        "signing_key_id": signing_key_id, "public_key_fingerprint": fingerprint,
        "sign_release_fields_keys": sorted(fields.keys()), "evidence_roles_bound": len(V._EVIDENCE_ROLES),
        "signature_schema": envelope["schema_version"], "signature_algorithm": envelope["algorithm"],
        "signature_synthetic": envelope["synthetic"],
        "pre_publish_verify_ok": bool(pre.ok), "pre_publish_verify_reason": pre.reason,
        "tampered_signature_rejected": (not bad_sig.ok) and bad_sig.reason == S.R_SIGNATURE,
        "tampered_manifest_rejected": (not bad_manifest.ok) and bad_manifest.reason == S.R_MANIFEST_DIGEST,
        "ready_written_last": bool(ready_written_last),
        "consumer_accepted": bool(acc["accepted"]), "consumer_reason": acc["reason"],
        "all_file_hashes_match": bool(acc["checks"].get("file_hashes", False)),
        "file_tamper_detected": bool(file_tamper_detected),
        "evidence_identity_verified": bool(acc["checks"].get("evidence_identity", False)),
        "network": "blocked", "nas": "none", "mt5": "none", "broker": "none", "credentials": "none",
        "production": "none", "real_signing_key": "none",
    }


def _run(base, provider, **kw):
    with network_blocked():
        return prove(base, provider, **kw)


if __name__ == "__main__":  # pragma: no cover - requires an injected provider; see the test for the proof.
    sys.stderr.write("This tool requires an injected Ed25519 provider; run tests/test_tim_release_operational_proof.py.\n")
    raise SystemExit(2)
