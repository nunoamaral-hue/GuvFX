#!/usr/bin/env python3
"""GuvFX-TIM v0.4 SESSION-AWARE release builder (ADR-0130).

Builds one bounded v0.4 release candidate: canonical Parquet + raw NDJSON + the nine evidence references
(the seven v0.3 roles reused verbatim, plus the session-aware ``quality_coverage`` and ``gap_report``) + a
PINNED session calendar bound by digest into the signed manifest + ``request.fields.json`` for the external
producer signer. It computes expected bars and gap classifications FROM the pinned calendar and the actual
rows — never inventing data. UNSEALED (no signature, no READY); the producer signs the request out of band,
exactly as the v0.3 CLI workflow. Fixture/synthetic data only here; real-data use is gated separately.

Run with the research venv (duckdb). Verification is duckdb-free (research.market_data.tim_session_consumer).
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
from decimal import Decimal

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import duckdb  # noqa: E402
import tim_release_v0_3_synthetic_smoke as SMOKE  # noqa: E402  reuse _write/_bundle_digest/_CANDLE_DDL
from research.market_data import tim_contracts_v0_3 as V  # noqa: E402
from research.market_data import tim_contracts_v0_4 as V4  # noqa: E402
from research.market_data import tim_fixtures_v0_3 as F  # noqa: E402
from research.market_data import tim_fixtures as F2  # noqa: E402
from research.market_data import tim_session_calendar as CAL  # noqa: E402
from research.market_data.contracts import canonical_json_bytes, sha256_hex  # noqa: E402

SYMBOL = "EURUSD"
# The canonical candle schema (mandatory 8 columns, in order) — validated by
# V.validate_canonical_candle_parquet_schema.
_CANDLE_DDL = ('event_start_utc TIMESTAMP NOT NULL, event_end_utc TIMESTAMP NOT NULL, '
               '"open" DECIMAL(38,18) NOT NULL, high DECIMAL(38,18) NOT NULL, low DECIMAL(38,18) NOT NULL, '
               '"close" DECIMAL(38,18) NOT NULL, tick_volume BIGINT NOT NULL, quality_flags UINTEGER NOT NULL')
_CANDLE_COLS = ("event_start_utc", "event_end_utc", "open", "high", "low", "close", "tick_volume",
                "quality_flags")


def _min_iso(epoch_min: int) -> str:
    return _dt.datetime.fromtimestamp(epoch_min * 60, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")


def generate_in_session_rows(calendar: dict, start_utc: str, count: int, *, drop_minutes=()):
    """`count` contiguous M1 bars from ``start_utc`` (every minute MUST be in-session). ``drop_minutes`` is a
    set of RELATIVE minute offsets (0..count-1) that are omitted, simulating missing in-session bars.
    Deterministic valid OHLC."""
    CAL.validate_calendar(calendar)
    start = CAL._epoch_min(start_utc)
    drop = set(int(x) for x in drop_minutes)
    rows = []
    for i in range(count):
        m = start + i
        if not CAL.is_in_session(calendar, m):
            raise ValueError("minute %d (%s) is not in-session for this calendar" % (m, _min_iso(m)))
        if i in drop:
            continue
        base = Decimal("1.10000") + (Decimal(i % 20) * Decimal("0.00001"))
        o = base
        h = base + Decimal("0.00008")
        lo = base - Decimal("0.00006")
        c = base + Decimal("0.00002")
        s = _dt.datetime.fromtimestamp(m * 60, _dt.timezone.utc).replace(tzinfo=None)
        e = _dt.datetime.fromtimestamp((m + 1) * 60, _dt.timezone.utc).replace(tzinfo=None)
        rows.append({"event_start_utc": s, "event_end_utc": e, "open": o, "high": h, "low": lo,
                     "close": c, "tick_volume": 10 + (i % 7), "quality_flags": 0})
    return rows, start, start + count


def _emit_parquet_and_ndjson(rel_dir, rows):
    parquet_path = os.path.join(rel_dir, "eurusd_m1.parquet")
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE candles (%s)" % _CANDLE_DDL)
        con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)",
                        [tuple(r[c] for c in _CANDLE_COLS) for r in rows])
        con.execute("COPY candles TO '%s' (FORMAT PARQUET)" % parquet_path)
        described = con.execute("DESCRIBE SELECT * FROM read_parquet('%s')" % parquet_path).fetchall()
        observed_schema = [{"name": r[0], "type": r[1], "nullable": (str(r[2]).upper() == "YES")} for r in described]
    finally:
        con.close()
    V.validate_canonical_candle_parquet_schema(observed_schema)
    with open(parquet_path, "rb") as fh:
        pq = fh.read()
    ndjson = b"\n".join(
        canonical_json_bytes({
            "event_start_utc": r["event_start_utc"].strftime("%Y-%m-%dT%H:%M:00Z"),
            "event_end_utc": r["event_end_utc"].strftime("%Y-%m-%dT%H:%M:00Z"),
            "open": str(r["open"]), "high": str(r["high"]), "low": str(r["low"]), "close": str(r["close"]),
            "tick_volume": r["tick_volume"], "quality_flags": r["quality_flags"]}) for r in rows) + b"\n"
    return pq, sha256_hex(pq), len(pq), ndjson


def build_session_release(base, *, calendar, release_id, source_id, signing_key_id, producer_identity,
                          created_at_utc, key_validity, start_utc, count, drop_minutes=(),
                          verified_no_tick=(), synthetic=True):
    """Build an UNSEALED v0.4 session-aware candidate under ``base/staging/<release_id>``. Returns the build
    dict (rel_dir, manifest, manifest_bytes, manifest_digest, request fields, calendar_digest)."""
    CAL.validate_calendar(calendar)
    staging = os.path.join(base, "staging")
    rel_dir = os.path.join(staging, release_id)
    os.makedirs(rel_dir, exist_ok=True)

    rows, start_m, end_m = generate_in_session_rows(calendar, start_utc, count, drop_minutes=drop_minutes)
    if not rows:
        raise ValueError("no rows produced")
    range_start, range_end = _min_iso(start_m), _min_iso(end_m)
    pq, pq_sha, pq_size, ndjson = _emit_parquet_and_ndjson(rel_dir, rows)
    raw = SMOKE._write(rel_dir, "eurusd_m1.raw.ndjson", ndjson)

    cal_bytes = CAL.canonical_bytes(calendar)
    cal_rel, cal_sha, _cal_size = SMOKE._write(rel_dir, "calendar.json", cal_bytes)
    assert cal_sha == CAL.calendar_digest(calendar)

    observed = set(CAL._epoch_min(r["event_start_utc"].strftime("%Y-%m-%dT%H:%M:00Z")) for r in rows)
    # verified_no_tick is given as RELATIVE offsets from the range start; resolve to absolute epoch minutes.
    no_tick_abs = set(start_m + int(r) for r in verified_no_tick)
    cov = CAL.classify_coverage(calendar, observed, range_start, range_end,
                                verified_no_tick_minutes=no_tick_abs)
    # FULL (not sampled) classification of in-session misses, for the gap set.
    unexplained_full, verified_actual = set(), set()
    for m in range(start_m, end_m):
        if not CAL.is_in_session(calendar, m) or m in observed:
            continue
        (verified_actual if m in no_tick_abs else unexplained_full).add(m)
    unexplained = len(unexplained_full)
    verdict = "PASS" if unexplained == 0 else "DEGRADED"
    completeness = "COMPLETE" if unexplained == 0 else "DEGRADED"

    # no-tick EVIDENCE artifact (only when minutes are attested no-tick); each VERIFIED_NO_TICK gap references it.
    no_tick_ref = None
    if verified_actual:
        nte = {"schema_version": "1.0", "source_id": source_id, "calendar_id": calendar["calendar_id"],
               "timeframe": "M1", "attested_minutes": [_min_iso(m) for m in sorted(verified_actual)],
               "provenance": "synthetic_test_no_tick_scan", "synthetic": synthetic,
               "limitations": [F.SYNTH] if synthetic else ["internal_research_only"]}
        V4.validate_no_tick_evidence_v1(nte)
        _nte_rel, nte_sha, _nte_size = SMOKE._write(rel_dir, "no_tick_evidence.json", nte)
        no_tick_ref = {"relative_path": release_id + "/no_tick_evidence.json", "sha256": nte_sha}

    # gap SET (zero or many; no forced defect record). UNEXPLAINED carries no evidence ref; VERIFIED_NO_TICK
    # must reference the signed no-tick evidence above.
    gap_list = []
    for m in sorted(unexplained_full):
        gap_list.append({"gap_start_utc": _min_iso(m), "gap_end_utc": _min_iso(m + 1), "gap_class": "UNEXPLAINED",
                         "reason": "in-session bar missing, no no-tick evidence", "repair": None,
                         "no_tick_evidence_ref": None})
    for m in sorted(verified_actual):
        gap_list.append({"gap_start_utc": _min_iso(m), "gap_end_utc": _min_iso(m + 1),
                         "gap_class": "VERIFIED_NO_TICK", "reason": "attested no-tick interval", "repair": None,
                         "no_tick_evidence_ref": no_tick_ref})
    gap_list.sort(key=lambda g: g["gap_start_utc"])
    gap_set = {"schema_version": "1.0", "source_id": source_id, "canonical_symbol": SYMBOL, "timeframe": "M1",
               "calendar_id": calendar["calendar_id"], "calendar_digest": cal_sha, "gaps": gap_list,
               "unexplained_count": sum(1 for g in gap_list if g["gap_class"] == "UNEXPLAINED"),
               "synthetic": synthetic, "limitations": [F.SYNTH] if synthetic else ["internal_research_only"]}
    V4.validate_gap_set_v1(gap_set)
    gap_rel, gap_sha, gap_size = SMOKE._write(rel_dir, "gap.json", gap_set)

    coverage = {"schema_version": "1.0", "release_id": release_id, "source_id": source_id,
                "canonical_symbol": SYMBOL, "timeframe": "M1", "calendar_id": calendar["calendar_id"],
                "calendar_digest": cal_sha, "range_start_utc": range_start, "range_end_utc": range_end,
                "range_semantics": "[start,end)", "first_event_start_utc": _min_iso(min(observed)),
                "last_event_start_utc": _min_iso(max(observed)),
                "expected_bar_count": cov["expected_bar_count"], "observed_in_session": cov["observed_in_session"],
                "scheduled_closure_minutes": cov["scheduled_closure_minutes"],
                "verified_no_tick_minutes": cov["verified_no_tick_minutes"],
                "unexplained_missing_minutes": unexplained, "canonical_file": {"relative_path": release_id + "/eurusd_m1.parquet", "sha256": pq_sha},
                "gap_set_ref": {"relative_path": release_id + "/gap.json", "sha256": gap_sha},
                "completeness_status": completeness, "quality_verdict": verdict, "synthetic": synthetic,
                "limitations": [F.SYNTH] if synthetic else ["internal_research_only"]}
    V4.validate_session_quality_coverage_v2(coverage)
    cov_rel, cov_sha, cov_size = SMOKE._write(rel_dir, "quality_coverage.json", coverage)

    # the seven reused v0.3 evidence roles (fixtures; source_id set).
    def ev(name, obj):
        rel, sha, size = SMOKE._write(rel_dir, name, obj)
        return rel, sha, size

    eligibility = F.source_eligibility(source_id=source_id); V.validate_source_eligibility(eligibility)
    broker = F.broker_server_identity(source_id=source_id); V.validate_broker_server_identity(broker)
    timezone_ev = F.timezone_evidence_v0_3(source_id=source_id); V.validate_timezone_evidence_v0_3(timezone_ev)
    licensing = F.licensing_evidence(source_id=source_id); V.validate_licensing_evidence(licensing)
    from research.market_data import tim_contracts as T2
    # Legacy per-range quality report over the CONTINUOUS release range (expected = full range minutes).
    # In-session misses surface as QUARANTINE here AND as unexplained minutes in the session coverage.
    expected_total = end_m - start_m
    missing_total = expected_total - len(rows)
    quality = {"schema_version": "1.0", "source_id": source_id, "canonical_symbol": SYMBOL, "timeframe": "M1",
               "representation": "bid_ohlc", "price_basis": "bid", "provenance": "NATIVE",
               "range_start_utc": range_start, "range_end_utc": range_end, "range_semantics": "[start,end)",
               "expected_bar_count": expected_total, "observed_bar_count": len(rows), "duplicate_count": 0,
               "out_of_order_count": 0, "overlap_count": 0, "ohlc_violation_count": 0, "non_finite_count": 0,
               "gap_count": missing_total, "quality_status": "PASS" if missing_total == 0 else "QUARANTINE",
               "quarantine_reasons": [] if missing_total == 0 else ["in_session_gap"],
               "evaluated_at_utc": created_at_utc,
               "limitations": [F.SYNTH] if synthetic else ["internal_research_only"]}
    T2.validate_quality_report(quality)
    watermark = {**F2.positive_fixtures()["acquisition_watermark_v1"], "source_id": source_id,
                 "watermark_event_end_utc": range_end, "last_raw_object_sha256": pq_sha}
    T2.validate_acquisition_watermark(watermark)
    mapping = {**F2.positive_fixtures()["source_instrument_mapping_v1"], "source_id": source_id}
    T2.validate_source_instrument_mapping(mapping)

    emap = {"source_eligibility": ev("eligibility.json", eligibility),
            "broker_server_identity": ev("broker_server.json", broker),
            "timezone_evidence": ev("timezone.json", timezone_ev),
            "licensing": ev("licensing.json", licensing),
            "quality_report": ev("quality.json", quality),
            "watermark": ev("watermark.json", watermark),
            "instrument_mapping": ev("mapping.json", mapping),
            "quality_coverage": (cov_rel, cov_sha, cov_size),
            "gap_report": (gap_rel, gap_sha, gap_size)}

    def frec(rel, role, media, sv, vv, sha, size, rc):
        return {"relative_path": "%s/%s" % (release_id, rel), "media_type": media, "byte_size": size,
                "digest": {"algorithm": "sha256", "value": sha}, "logical_role": role,
                "schema_version": sv, "validator_version": vv, "row_count": rc}
    files = [
        frec("eurusd_m1.parquet", "canonical_parquet", V._MEDIA_PARQUET, "1.0", "0.3.0", pq_sha, pq_size, len(rows)),
        frec(raw[0], "raw_ndjson", V._MEDIA_NDJSON, "raw", "0.3.0", raw[1], raw[2], len(rows)),
        frec("quality.json", "quality_report", "application/json", "1.0", "0.2.0", emap["quality_report"][1], emap["quality_report"][2], None),
        frec("gap.json", "gap_report", "application/json", "1.0", "0.4.0", gap_sha, gap_size, None),
        frec("watermark.json", "watermark", "application/json", "1.0", "0.2.0", emap["watermark"][1], emap["watermark"][2], None),
        frec("eligibility.json", "source_eligibility", "application/json", "1.0", "0.3.0", emap["source_eligibility"][1], emap["source_eligibility"][2], None),
        frec("broker_server.json", "broker_server_identity", "application/json", "1.0", "0.3.0", emap["broker_server_identity"][1], emap["broker_server_identity"][2], None),
        frec("mapping.json", "instrument_mapping", "application/json", "1.0", "0.2.0", emap["instrument_mapping"][1], emap["instrument_mapping"][2], None),
        frec("timezone.json", "timezone_evidence", "application/json", "1.0", "0.3.0", emap["timezone_evidence"][1], emap["timezone_evidence"][2], None),
        frec("licensing.json", "licensing", "application/json", "1.0", "0.3.0", emap["licensing"][1], emap["licensing"][2], None),
        frec("quality_coverage.json", "quality_coverage", "application/json", "1.0", "0.4.0", cov_sha, cov_size, None),
    ]
    contract_bundle_digest = SMOKE._bundle_digest(
        [os.path.join(REPO_ROOT, "research", "contracts", "v0_3", n + ".schema.json") for n in V.TIM_V0_3_SCHEMA_FILES])
    with open(os.path.join(REPO_ROOT, "research", "market_data", "tim_contracts_v0_4.py"), "rb") as fh:
        validator_bundle_digest = sha256_hex(fh.read())

    def evref(role):
        rel, sha, _sz = emap[role]
        return {"contract_version": "1.0", "relative_path": "%s/%s" % (release_id, rel), "sha256": sha}

    manifest = {
        "schema_version": V4.MANIFEST_SCHEMA_V0_4, "contract_version": V4.CONTRACT_VERSION_V0_4,
        "release_id": release_id, "supersedes_release_id": None, "supersession": None, "source_id": source_id,
        "canonical_symbol": SYMBOL, "broker_native_symbol": "EURUSD.std", "asset_class": "fx", "timeframe": "M1",
        "representation": "bid_ohlc", "price_basis": "bid", "provenance": "NATIVE", "derivation": None,
        "canonical_format": "parquet", "range_start_utc": range_start, "range_end_utc": range_end,
        "range_semantics": "[start,end)",
        "point_in_time": {"event_start_utc": range_start, "event_end_utc": range_end,
                          "source_available_at_utc": created_at_utc, "source_availability_classification": "EVIDENCED",
                          "collector_observed_at_utc": created_at_utc, "ingested_at_utc": created_at_utc,
                          "validated_at_utc": created_at_utc, "published_at_utc": created_at_utc},
        "row_count": len(rows), "uniqueness_key": ["event_start_utc"], "files": files,
        "evidence": {role: evref(role) for role in V._EVIDENCE_ROLES},
        "validator_pinning": {"schema_version": "3.0", "validator_id": "guvfx_tim_v0_4",
                              "validator_version": "0.4.0", "validator_digest": validator_bundle_digest,
                              "compatibility_range": ">=0.4.0,<0.5.0"},
        "contract_bundle_digest": contract_bundle_digest, "validator_bundle_digest": validator_bundle_digest,
        # release_completeness is the FILE-SET / range-coverage axis (the release spans the full intended
        # range with the full file set) — always COMPLETE here; the DATA-quality verdict (gaps) lives in the
        # session coverage evidence (quality_verdict / completeness_status), a distinct axis.
        "release_completeness": "COMPLETE", "partial_release_allowed": False,
        "producer_identity": producer_identity, "producer_version": "guvfx-tim-0.4.0-session-aware",
        "signing_key_id": signing_key_id, "signature_path": "%s/manifest.sig.json" % release_id,
        "ready_marker": {"present": True, "written_last": True, "ready_marker_path": "%s/READY" % release_id},
        "session_calendar": {"calendar_id": calendar["calendar_id"], "calendar_digest": cal_sha,
                             "source_id": source_id},
        "licensing_classification": "internal_research_only", "synthetic": synthetic,
        "limitations": [F.SYNTH] if synthetic else ["internal_research_only"]}
    V4.validate_release_manifest_v0_4(manifest)
    V4.validate_session_release_consistency(manifest, coverage, gap_set, calendar)
    with open(os.path.join(rel_dir, "manifest.json"), "wb") as fh:
        fh.write(canonical_json_bytes(manifest))
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_digest = sha256_hex(manifest_bytes)

    fields = {"release_id": release_id, "manifest_digest": manifest_digest,
              "contract_bundle_digest": contract_bundle_digest, "validator_bundle_digest": validator_bundle_digest,
              "producer_identity": producer_identity, "created_at_utc": created_at_utc,
              "key_validity": dict(key_validity), "limitations": list(key_validity.get("limitations", []))}
    fields["limitations"] = [F.SYNTH] if synthetic else ["internal_research_only"]
    SMOKE._write(rel_dir, "request.fields.json", canonical_json_bytes(fields))
    return {"rel_dir": rel_dir, "manifest": manifest, "manifest_bytes": manifest_bytes,
            "manifest_digest": manifest_digest, "contract_bundle_digest": contract_bundle_digest,
            "validator_bundle_digest": validator_bundle_digest, "fields": fields, "calendar_digest": cal_sha,
            "row_count": len(rows), "quality_verdict": verdict}
