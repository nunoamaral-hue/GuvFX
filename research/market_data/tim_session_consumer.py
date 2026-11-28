"""GuvFX-TIM v0.4 session-aware standalone CONSUMER acceptance (ADR-0130). duckdb-free.

Independently accepts a published v0.4 release: it re-verifies the READY binding, the operational signature
(against the PINNED trust store), every signed file hash, the v0.3 evidence identity for the shared roles,
AND — the session-aware part — it loads the pinned calendar (digest-checked against the signed manifest),
reads the signed raw NDJSON (the ACTUAL bars), and RECOMPUTES the expected count, the observed-in-session
count and the UNEXPLAINED-missing set from first principles, then checks them against the coverage and gap
evidence. A hidden missing in-session bar (dropped without a no-tick attestation) raises the unexplained
count and is rejected. Fail-closed, value-blind. Writes nothing.
"""
from __future__ import annotations

import json
import os

from . import tim_contracts_v0_3 as V
from . import tim_contracts_v0_4 as V4
from . import tim_release_signing_v3 as S
from . import tim_session_calendar as CAL
from .contracts import sha256_hex


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _observed_minutes_from_ndjson(ndjson_bytes):
    out = set()
    for line in ndjson_bytes.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        ts = rec.get("event_start_utc")
        if not isinstance(ts, str):
            raise ValueError("ndjson row missing event_start_utc")
        out.add(CAL._epoch_min(ts))
    return out


def _recompute(calendar, observed, no_tick, range_start_utc, range_end_utc):
    """Full first-principles recompute over the range. Returns counts + the exact unexplained minute set."""
    s, e = CAL._epoch_min(range_start_utc), CAL._epoch_min(range_end_utc)
    obs = set(int(x) for x in observed)
    nt = set(int(x) for x in no_tick)
    present = scheduled = verified = 0
    unexplained = set()
    for m in range(s, e):
        if not CAL.is_in_session(calendar, m):
            scheduled += 1
            continue
        if m in obs:
            present += 1
        elif m in nt:
            verified += 1
        else:
            unexplained.add(m)
    return {"expected": present + verified + len(unexplained), "present": present,
            "scheduled": scheduled, "verified": verified, "unexplained": unexplained}


def consumer_validate_v0_4(release_dir, store) -> dict:
    base = {"receipt_type": "consumer", "stage": "validate", "contract_version": "0.4",
            "release_dir_name": os.path.basename(release_dir)}

    def reject(reason):
        return {**base, "accepted": False, "reason": reason}

    if not os.path.isfile(os.path.join(release_dir, "READY")):
        return reject("missing_ready")
    try:
        manifest_bytes = _read(os.path.join(release_dir, "manifest.json"))
        sig_bytes = _read(os.path.join(release_dir, "manifest.sig.json"))
        ready = V.strict_json_loads_v0_3(_read(os.path.join(release_dir, "READY")))
        manifest = V.strict_json_loads_v0_3(manifest_bytes)
        sig = V.strict_json_loads_v0_3(sig_bytes)
    except Exception:
        return reject("unreadable_release")

    # READY binding + v0.4 marker.
    if not (ready.get("state") == "READY" and ready.get("manifest_digest") == sha256_hex(manifest_bytes)
            and ready.get("signature_envelope_digest") == sha256_hex(sig_bytes)):
        return reject("ready_binding_mismatch")
    try:
        V4.validate_ready_marker(ready)
    except Exception:
        return reject("ready_marker_invalid")

    # Operational signature against the PINNED store (unchanged, exact).
    result = S.verify_operational_release_signature_v3(sig, manifest_bytes, trust_store=store, manifest=manifest)
    if not result.ok:
        return reject(result.reason)

    # Manifest contract (v0.4) + evidence roles present + every signed file hash.
    try:
        if V4.validate_release_manifest(manifest) != V4.CONTRACT_VERSION_V0_4:
            return reject("not_a_v0_4_release")
    except Exception:
        return reject("manifest_invalid")
    releases_parent = os.path.dirname(release_dir)
    evidence = manifest.get("evidence", {})
    if set(evidence) != set(V._EVIDENCE_ROLES):
        return reject("evidence_roles_incomplete")
    for role in V._EVIDENCE_ROLES:
        ref = evidence[role]
        fp = os.path.join(releases_parent, ref["relative_path"])
        if not os.path.isfile(fp) or sha256_hex(_read(fp)) != ref["sha256"]:
            return reject("evidence_reference_mismatch")
    for entry in manifest["files"]:
        fp = os.path.join(releases_parent, entry["relative_path"])
        if not os.path.isfile(fp) or sha256_hex(_read(fp)) != entry["digest"]["value"]:
            return reject("file_hash_mismatch")

    # Shared v0.3 evidence identity (eligibility / broker / timezone / licensing).
    try:
        c_ev = {role: V.strict_json_loads_v0_3(_read(os.path.join(release_dir, fname)))
                for role, fname in (("timezone_evidence", "timezone.json"),
                                    ("broker_server_identity", "broker_server.json"),
                                    ("licensing", "licensing.json"), ("source_eligibility", "eligibility.json"))}
        V.validate_release_evidence_identity(manifest, c_ev)
    except (V.ContractError, V.QuarantineRequired):
        return reject("evidence_identity_mismatch")
    except Exception:
        return reject("evidence_unreadable")

    # ---- session-aware verification (the new, data-grounded checks) ----
    try:
        calendar = json.loads(_read(os.path.join(release_dir, "calendar.json")))
        coverage = V.strict_json_loads_v0_3(_read(os.path.join(release_dir, "quality_coverage.json")))
        gap_set = V.strict_json_loads_v0_3(_read(os.path.join(release_dir, "gap.json")))
        ndjson = _read(os.path.join(release_dir, "eurusd_m1.raw.ndjson"))
    except Exception:
        return reject("session_artifacts_unreadable")

    # Pinned calendar digest MUST match the signed manifest binding.
    if sha256_hex(CAL.canonical_bytes(calendar)) != manifest["session_calendar"]["calendar_digest"]:
        return reject("calendar_digest_mismatch")
    try:
        CAL.validate_calendar(calendar)
        V4.validate_session_quality_coverage_v2(coverage)
        V4.validate_gap_set_v1(gap_set)
        V4.validate_session_release_consistency(manifest, coverage, gap_set, calendar)
    except Exception:
        return reject("session_contract_invalid")

    # Independent recompute from the ACTUAL signed bars + the pinned calendar + the attested no-tick set.
    try:
        observed = _observed_minutes_from_ndjson(ndjson)
    except Exception:
        return reject("ndjson_unreadable")
    no_tick = set(CAL._epoch_min(g["gap_start_utc"]) for g in gap_set["gaps"] if g["gap_class"] == "VERIFIED_NO_TICK")
    rc = _recompute(calendar, observed, no_tick, coverage["range_start_utc"], coverage["range_end_utc"])
    if not (rc["expected"] == coverage["expected_bar_count"]
            and rc["present"] == coverage["observed_in_session"]
            and rc["scheduled"] == coverage["scheduled_closure_minutes"]
            and rc["verified"] == coverage["verified_no_tick_minutes"]
            and len(rc["unexplained"]) == coverage["unexplained_missing_minutes"]):
        return reject("coverage_recompute_mismatch")
    # The declared UNEXPLAINED gaps MUST be exactly the recomputed unexplained minutes (no hidden gap).
    declared_unexplained = set(CAL._epoch_min(g["gap_start_utc"]) for g in gap_set["gaps"]
                               if g["gap_class"] == "UNEXPLAINED")
    if declared_unexplained != rc["unexplained"]:
        return reject("unexplained_gap_set_mismatch")
    # Verdict must stay honest: any unexplained miss => not PASS/COMPLETE.
    if len(rc["unexplained"]) > 0 and (coverage["quality_verdict"] == "PASS"
                                       or coverage["completeness_status"] == "COMPLETE"):
        return reject("verdict_hides_unexplained")

    return {**base, "accepted": True, "reason": None, "release_id": manifest["release_id"],
            "signing_key_id": sig["signing_key_id"], "quality_verdict": coverage["quality_verdict"],
            "expected_bar_count": rc["expected"], "observed_in_session": rc["present"],
            "scheduled_closure_minutes": rc["scheduled"], "verified_no_tick_minutes": rc["verified"],
            "unexplained_missing_minutes": len(rc["unexplained"]), "calendar_id": calendar["calendar_id"],
            "evidence_references_verified": len(V._EVIDENCE_ROLES), "files_verified": len(manifest["files"]),
            "recompute_matches_coverage": True, "synthetic_data": bool(manifest.get("synthetic", True))}
