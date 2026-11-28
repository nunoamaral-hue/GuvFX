#!/usr/bin/env python3
"""GuvFX-TIM v0.4 SESSION-AWARE release builder (ADR-0130).

Builds one UNSEALED v0.4 release candidate from candle rows and a PINNED session calendar. Two entry points
share one assembly core:
  * ``build_session_release`` — generates in-session fixture rows (synthetic CI path).
  * ``build_session_release_from_parquet`` — ingests a delivered NORMALIZED Parquet of REAL bars; it does NOT
    assume every bar is in-session. ALL observed bars are preserved in the release (the exact Parquet bytes are
    bound to the signed manifest); in-session completeness, out-of-session quotes, defects and gaps are all
    derived from the pinned calendar and the data.

Evidence is the nine roles: the seven reused v0.3 roles, the session ``quality_coverage`` (v2) and the
session ``quality_report`` SUCCESSOR (v2 — expected/observed over the trading session, out-of-session quotes
reported separately, bar defects counted) and the ``gap_report`` gap-set. The signer contract is unchanged.
Run with the research venv (duckdb). Fixture data only in CI; real-data use is gated separately.
"""
from __future__ import annotations

import datetime as _dt
import math
import os
import shutil
import sys
from decimal import Decimal

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import duckdb  # noqa: E402
import tim_release_v0_3_synthetic_smoke as SMOKE  # noqa: E402
from research.market_data import tim_contracts_v0_3 as V  # noqa: E402
from research.market_data import tim_contracts_v0_4 as V4  # noqa: E402
from research.market_data import tim_contracts as T2  # noqa: E402
from research.market_data import tim_fixtures_v0_3 as F  # noqa: E402
from research.market_data import tim_fixtures as F2  # noqa: E402
from research.market_data import tim_session_calendar as CAL  # noqa: E402
from research.market_data.contracts import canonical_json_bytes, sha256_hex  # noqa: E402

SYMBOL = "EURUSD"
_CANDLE_DDL = ('event_start_utc TIMESTAMP NOT NULL, event_end_utc TIMESTAMP NOT NULL, '
               '"open" DECIMAL(38,18) NOT NULL, high DECIMAL(38,18) NOT NULL, low DECIMAL(38,18) NOT NULL, '
               '"close" DECIMAL(38,18) NOT NULL, tick_volume BIGINT NOT NULL, quality_flags UINTEGER NOT NULL')
_CANDLE_COLS = ("event_start_utc", "event_end_utc", "open", "high", "low", "close", "tick_volume", "quality_flags")


def _min_iso(epoch_min: int) -> str:
    return _dt.datetime.fromtimestamp(epoch_min * 60, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")


def _row_min(r) -> int:
    return CAL._epoch_min(r["event_start_utc"].strftime("%Y-%m-%dT%H:%M:00Z"))


def generate_in_session_rows(calendar: dict, start_utc: str, count: int, *, drop_minutes=()):
    """`count` contiguous M1 bars from ``start_utc`` (every minute MUST be in-session). ``drop_minutes`` is a
    set of RELATIVE offsets (0..count-1) omitted, simulating missing in-session bars. Deterministic OHLC."""
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
        s = _dt.datetime.fromtimestamp(m * 60, _dt.timezone.utc).replace(tzinfo=None)
        e = _dt.datetime.fromtimestamp((m + 1) * 60, _dt.timezone.utc).replace(tzinfo=None)
        rows.append({"event_start_utc": s, "event_end_utc": e, "open": base, "high": base + Decimal("0.00008"),
                     "low": base - Decimal("0.00006"), "close": base + Decimal("0.00002"),
                     "tick_volume": 10 + (i % 7), "quality_flags": 0})
    return rows, start, start + count


def read_parquet_rows(parquet_path: str):
    """Read a delivered canonical Parquet into rows + return the EXACT file bytes/sha (bound to the manifest)."""
    con = duckdb.connect(":memory:")
    try:
        desc = con.execute("DESCRIBE SELECT * FROM read_parquet('%s')" % parquet_path).fetchall()
        V.validate_canonical_candle_parquet_schema([{"name": d[0], "type": d[1], "nullable": d[2] == "YES"} for d in desc])
        recs = con.execute("SELECT event_start_utc, event_end_utc, \"open\", high, low, \"close\", tick_volume, "
                           "quality_flags FROM read_parquet('%s') ORDER BY event_start_utc" % parquet_path).fetchall()
    finally:
        con.close()
    rows = [{"event_start_utc": r[0], "event_end_utc": r[1], "open": r[2], "high": r[3], "low": r[4],
             "close": r[5], "tick_volume": int(r[6]), "quality_flags": int(r[7])} for r in recs]
    with open(parquet_path, "rb") as fh:
        pq = fh.read()
    return rows, pq, sha256_hex(pq), len(pq)


def _emit_parquet_from_rows(rel_dir, rows):
    pq_path = os.path.join(rel_dir, "eurusd_m1.parquet")
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE c (%s)" % _CANDLE_DDL)
        con.executemany("INSERT INTO c VALUES (?,?,?,?,?,?,?,?)", [tuple(r[c] for c in _CANDLE_COLS) for r in rows])
        con.execute("COPY c TO '%s' (FORMAT PARQUET)" % pq_path)
        desc = con.execute("DESCRIBE SELECT * FROM read_parquet('%s')" % pq_path).fetchall()
        V.validate_canonical_candle_parquet_schema([{"name": d[0], "type": d[1], "nullable": d[2] == "YES"} for d in desc])
    finally:
        con.close()
    with open(pq_path, "rb") as fh:
        pq = fh.read()
    return pq, sha256_hex(pq), len(pq)


def _ndjson(rows):
    return b"\n".join(canonical_json_bytes({
        "event_start_utc": r["event_start_utc"].strftime("%Y-%m-%dT%H:%M:00Z"),
        "event_end_utc": r["event_end_utc"].strftime("%Y-%m-%dT%H:%M:00Z"),
        "open": str(r["open"]), "high": str(r["high"]), "low": str(r["low"]), "close": str(r["close"]),
        "tick_volume": r["tick_volume"], "quality_flags": r["quality_flags"]}) for r in rows) + b"\n"


def _defects(rows):
    """Bar-level defects over the observed rows (sorted by event_start). Clean real data yields all zeros."""
    mins = [_row_min(r) for r in rows]
    dup = len(mins) - len(set(mins))
    out_of_order = sum(1 for i in range(1, len(mins)) if mins[i] < mins[i - 1])
    overlap = sum(1 for i in range(1, len(mins)) if mins[i] < mins[i - 1] + 1)  # next start before current end
    ohlc = nonfin = 0
    for r in rows:
        try:
            o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        except Exception:
            nonfin += 1
            continue
        if not all(math.isfinite(x) for x in (o, h, l, c)):
            nonfin += 1
        elif not (l <= min(o, c) and max(o, c) <= h and l <= h):
            ohlc += 1
    return {"duplicate_count": dup, "out_of_order_count": out_of_order, "overlap_count": overlap,
            "ohlc_violation_count": ohlc, "non_finite_count": nonfin}


def _assemble(base, *, calendar, rows, parquet_bytes, parquet_sha, parquet_size, range_start_m, range_end_m,
              release_id, source_id, signing_key_id, producer_identity, created_at_utc, key_validity,
              no_tick_abs, synthetic, eligibility=None, licensing=None):
    CAL.validate_calendar(calendar)
    rel_dir = os.path.join(base, "staging", release_id)
    os.makedirs(rel_dir, exist_ok=True)
    with open(os.path.join(rel_dir, "eurusd_m1.parquet"), "wb") as fh:
        fh.write(parquet_bytes)
    raw = SMOKE._write(rel_dir, "eurusd_m1.raw.ndjson", _ndjson(rows))
    cal_sha = CAL.calendar_digest(calendar)
    SMOKE._write(rel_dir, "calendar.json", CAL.canonical_bytes(calendar))
    range_start, range_end = _min_iso(range_start_m), _min_iso(range_end_m)
    lim = [F.SYNTH] if synthetic else ["internal_research_only"]

    observed = set(_row_min(r) for r in rows)
    in_session_present = set(m for m in observed if CAL.is_in_session(calendar, m))
    out_of_session_bars = sum(1 for r in rows if not CAL.is_in_session(calendar, _row_min(r)))
    unexplained_full, verified_actual = set(), set()
    scheduled = 0
    for m in range(range_start_m, range_end_m):
        if not CAL.is_in_session(calendar, m):
            scheduled += 1
            continue
        if m in in_session_present:
            continue
        (verified_actual if m in no_tick_abs else unexplained_full).add(m)
    unexplained = len(unexplained_full)
    verified = len(verified_actual)
    expected_in_session = len(in_session_present) + verified + unexplained
    verdict = "PASS" if unexplained == 0 else "DEGRADED"

    no_tick_ref = None
    if verified_actual:
        nte = {"schema_version": "1.0", "source_id": source_id, "calendar_id": calendar["calendar_id"],
               "timeframe": "M1", "attested_minutes": [_min_iso(m) for m in sorted(verified_actual)],
               "provenance": "synthetic_test_no_tick_scan" if synthetic else "producer_no_tick_scan",
               "synthetic": synthetic, "limitations": lim}
        V4.validate_no_tick_evidence_v1(nte)
        _r, nte_sha, _s = SMOKE._write(rel_dir, "no_tick_evidence.json", nte)
        no_tick_ref = {"relative_path": release_id + "/no_tick_evidence.json", "sha256": nte_sha}

    gaps = [{"gap_start_utc": _min_iso(m), "gap_end_utc": _min_iso(m + 1), "gap_class": "UNEXPLAINED",
             "reason": "in-session bar missing, no no-tick evidence", "repair": None, "no_tick_evidence_ref": None}
            for m in sorted(unexplained_full)]
    gaps += [{"gap_start_utc": _min_iso(m), "gap_end_utc": _min_iso(m + 1), "gap_class": "VERIFIED_NO_TICK",
              "reason": "attested no-tick interval", "repair": None, "no_tick_evidence_ref": no_tick_ref}
             for m in sorted(verified_actual)]
    gaps.sort(key=lambda g: g["gap_start_utc"])
    gap_set = {"schema_version": "1.0", "source_id": source_id, "canonical_symbol": SYMBOL, "timeframe": "M1",
               "calendar_id": calendar["calendar_id"], "calendar_digest": cal_sha, "gaps": gaps,
               "unexplained_count": sum(1 for g in gaps if g["gap_class"] == "UNEXPLAINED"),
               "synthetic": synthetic, "limitations": lim}
    V4.validate_gap_set_v1(gap_set)
    _r, gap_sha, gap_size = SMOKE._write(rel_dir, "gap.json", gap_set)

    coverage = {"schema_version": "1.0", "release_id": release_id, "source_id": source_id, "canonical_symbol": SYMBOL,
                "timeframe": "M1", "calendar_id": calendar["calendar_id"], "calendar_digest": cal_sha,
                "range_start_utc": range_start, "range_end_utc": range_end, "range_semantics": "[start,end)",
                "first_event_start_utc": _min_iso(min(observed)), "last_event_start_utc": _min_iso(max(observed)),
                "expected_bar_count": expected_in_session, "observed_in_session": len(in_session_present),
                "scheduled_closure_minutes": scheduled, "verified_no_tick_minutes": verified,
                "unexplained_missing_minutes": unexplained,
                "canonical_file": {"relative_path": release_id + "/eurusd_m1.parquet", "sha256": parquet_sha},
                "gap_set_ref": {"relative_path": release_id + "/gap.json", "sha256": gap_sha},
                "completeness_status": "COMPLETE" if unexplained == 0 else "DEGRADED",
                "quality_verdict": verdict, "synthetic": synthetic, "limitations": lim}
    V4.validate_session_quality_coverage_v2(coverage)
    _r, cov_sha, cov_size = SMOKE._write(rel_dir, "quality_coverage.json", coverage)

    defects = _defects(rows)
    clean = all(defects[k] == 0 for k in defects) and unexplained == 0
    reasons = []
    if unexplained > 0: reasons.append("unexplained_in_session_gap")
    if any(defects[k] for k in defects): reasons.append("bar_defect")
    quality = {"schema_version": "1.0", "source_id": source_id, "canonical_symbol": SYMBOL, "timeframe": "M1",
               "calendar_id": calendar["calendar_id"], "calendar_digest": cal_sha, "range_start_utc": range_start,
               "range_end_utc": range_end, "range_semantics": "[start,end)",
               "expected_in_session_bars": expected_in_session, "observed_in_session_bars": len(in_session_present),
               "out_of_session_bars": out_of_session_bars, "verified_no_tick_bars": verified,
               "unexplained_in_session_missing": unexplained, **defects,
               "quality_status": "PASS" if clean else "QUARANTINE", "quarantine_reasons": reasons,
               "evaluated_at_utc": created_at_utc, "synthetic": synthetic, "limitations": lim}
    V4.validate_session_quality_report_v2(quality)
    _r, q_sha, q_size = SMOKE._write(rel_dir, "quality.json", quality)

    # Reuse the EXISTING eligibility + licensing checks; a real release supplies the approved records, else a fixture.
    eligibility = eligibility if eligibility is not None else F.source_eligibility(source_id=source_id)
    V.validate_source_eligibility(eligibility)
    broker = F.broker_server_identity(source_id=source_id); V.validate_broker_server_identity(broker)
    timezone_ev = F.timezone_evidence_v0_3(source_id=source_id); V.validate_timezone_evidence_v0_3(timezone_ev)
    licensing = licensing if licensing is not None else F.licensing_evidence(source_id=source_id)
    V.validate_licensing_evidence(licensing)
    watermark = {**F2.positive_fixtures()["acquisition_watermark_v1"], "source_id": source_id,
                 "watermark_event_end_utc": range_end, "last_raw_object_sha256": parquet_sha}
    T2.validate_acquisition_watermark(watermark)
    mapping = {**F2.positive_fixtures()["source_instrument_mapping_v1"], "source_id": source_id}
    T2.validate_source_instrument_mapping(mapping)

    def ev(name, obj): return SMOKE._write(rel_dir, name, obj)
    emap = {"source_eligibility": ev("eligibility.json", eligibility),
            "broker_server_identity": ev("broker_server.json", broker),
            "timezone_evidence": ev("timezone.json", timezone_ev), "licensing": ev("licensing.json", licensing),
            "quality_report": (("quality.json", q_sha, q_size)), "watermark": ev("watermark.json", watermark),
            "instrument_mapping": ev("mapping.json", mapping), "quality_coverage": ("quality_coverage.json", cov_sha, cov_size),
            "gap_report": ("gap.json", gap_sha, gap_size)}

    def frec(rel, role, media, sv, vv, sha, size, rc):
        return {"relative_path": "%s/%s" % (release_id, rel), "media_type": media, "byte_size": size,
                "digest": {"algorithm": "sha256", "value": sha}, "logical_role": role,
                "schema_version": sv, "validator_version": vv, "row_count": rc}
    files = [
        frec("eurusd_m1.parquet", "canonical_parquet", V._MEDIA_PARQUET, "1.0", "0.3.0", parquet_sha, parquet_size, len(rows)),
        frec(raw[0], "raw_ndjson", V._MEDIA_NDJSON, "raw", "0.3.0", raw[1], raw[2], len(rows)),
        frec("quality.json", "quality_report", "application/json", "1.0", "0.4.0", q_sha, q_size, None),
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
        "release_completeness": "COMPLETE", "partial_release_allowed": False,
        "producer_identity": producer_identity, "producer_version": "guvfx-tim-0.4.0-session-aware",
        "signing_key_id": signing_key_id, "signature_path": "%s/manifest.sig.json" % release_id,
        "ready_marker": {"present": True, "written_last": True, "ready_marker_path": "%s/READY" % release_id},
        "session_calendar": {"calendar_id": calendar["calendar_id"], "calendar_digest": cal_sha, "source_id": source_id},
        "licensing_classification": "internal_research_only", "synthetic": synthetic, "limitations": lim}
    V4.validate_release_manifest_v0_4(manifest)
    V4.validate_session_release_consistency(manifest, coverage, gap_set, calendar)
    manifest_bytes = canonical_json_bytes(manifest)
    with open(os.path.join(rel_dir, "manifest.json"), "wb") as fh:
        fh.write(manifest_bytes)
    manifest_digest = sha256_hex(manifest_bytes)
    fields = {"release_id": release_id, "manifest_digest": manifest_digest,
              "contract_bundle_digest": contract_bundle_digest, "validator_bundle_digest": validator_bundle_digest,
              "producer_identity": producer_identity, "created_at_utc": created_at_utc,
              "key_validity": dict(key_validity), "limitations": lim}
    SMOKE._write(rel_dir, "request.fields.json", canonical_json_bytes(fields))
    return {"rel_dir": rel_dir, "manifest": manifest, "manifest_bytes": manifest_bytes,
            "manifest_digest": manifest_digest, "contract_bundle_digest": contract_bundle_digest,
            "validator_bundle_digest": validator_bundle_digest, "fields": fields, "calendar_digest": cal_sha,
            "row_count": len(rows), "quality_verdict": verdict, "out_of_session_bars": out_of_session_bars,
            "parquet_sha256": parquet_sha}


def build_session_release(base, *, calendar, release_id, source_id, signing_key_id, producer_identity,
                          created_at_utc, key_validity, start_utc, count, drop_minutes=(),
                          verified_no_tick=(), synthetic=True):
    """Fixture path: generate in-session rows, then assemble."""
    rows, start_m, end_m = generate_in_session_rows(calendar, start_utc, count, drop_minutes=drop_minutes)
    if not rows:
        raise ValueError("no rows produced")
    os.makedirs(os.path.join(base, "staging", release_id), exist_ok=True)
    pq, pq_sha, pq_size = _emit_parquet_from_rows(os.path.join(base, "staging", release_id), rows)
    no_tick_abs = set(start_m + int(r) for r in verified_no_tick)
    return _assemble(base, calendar=calendar, rows=rows, parquet_bytes=pq, parquet_sha=pq_sha, parquet_size=pq_size,
                     range_start_m=start_m, range_end_m=end_m, release_id=release_id, source_id=source_id,
                     signing_key_id=signing_key_id, producer_identity=producer_identity, created_at_utc=created_at_utc,
                     key_validity=key_validity, no_tick_abs=no_tick_abs, synthetic=synthetic)


def build_session_release_from_parquet(base, *, parquet_path, calendar, release_id, source_id, signing_key_id,
                                       producer_identity, created_at_utc, key_validity, verified_no_tick_utc=(),
                                       synthetic=False, eligibility=None, licensing=None):
    """Real-data path: ingest a delivered normalized Parquet (any bars; out-of-session preserved) and assemble.
    The exact Parquet bytes are bound to the signed manifest. ``eligibility``/``licensing`` supply the approved
    records for a real release (else fixtures)."""
    rows, pq, pq_sha, pq_size = read_parquet_rows(parquet_path)
    if not rows:
        raise ValueError("empty parquet")
    mins = [_row_min(r) for r in rows]
    start_m, end_m = min(mins), max(mins) + 1
    no_tick_abs = set(CAL._epoch_min(t) for t in verified_no_tick_utc)
    return _assemble(base, calendar=calendar, rows=rows, parquet_bytes=pq, parquet_sha=pq_sha, parquet_size=pq_size,
                     range_start_m=start_m, range_end_m=end_m, release_id=release_id, source_id=source_id,
                     signing_key_id=signing_key_id, producer_identity=producer_identity, created_at_utc=created_at_utc,
                     key_validity=key_validity, no_tick_abs=no_tick_abs, synthetic=synthetic,
                     eligibility=eligibility, licensing=licensing)
