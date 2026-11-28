"""GuvFX-TIM v0.4 session-aware release contract (ADR-0130). ADDITIVE over v0.3.

v0.3 releases (``contract_version == "0.3"``) keep validating under the ORIGINAL rules (tim_contracts_v0_3).
v0.4 releases (``contract_version == "0.4"``) bind a PINNED session calendar into the signed manifest and:
  * derive the expected bar count from that calendar (not raw calendar minutes);
  * keep scheduled closures, verified no-tick intervals, and unexplained in-session misses DISTINCT;
  * keep an unexplained gap VISIBLE and let it drive the quality verdict (PASS iff zero unexplained);
  * carry a gap SET that supports zero or many gaps without forcing a synthetic "defect" record.

This module only VERIFIES. It never signs, publishes, or touches a host. Fail-closed, value-blind.
"""
from __future__ import annotations

from . import tim_contracts_v0_3 as V3
from . import tim_session_calendar as CAL
from .tim_contracts import _bool, _const, _exact_keys, _instant, _minute_epoch, _str, _str_array
from .tim_contracts_v0_3 import ContractError
from .contracts import SHA256_RE, SLUG_RE

CONTRACT_VERSION_V0_4 = "0.4"
MANIFEST_SCHEMA_V0_4 = "3.1"
GAP_CLASSES = ("VERIFIED_NO_TICK", "UNEXPLAINED", "DECLARED", "REPAIRED", "UNREPAIRABLE")
QUALITY_VERDICTS = ("PASS", "DEGRADED", "FAIL")

GAP_SET_FIELDS = ("schema_version", "source_id", "canonical_symbol", "timeframe", "calendar_id",
                  "calendar_digest", "gaps", "unexplained_count", "synthetic", "limitations")
GAP_FIELDS = ("gap_start_utc", "gap_end_utc", "gap_class", "reason", "repair", "no_tick_evidence_ref")
REPAIR_FIELDS = ("repaired_at_utc", "repair_raw_object_sha256", "repair_evidence_path", "supersedes_gap_id")
SESSION_COVERAGE_FIELDS = ("schema_version", "release_id", "source_id", "canonical_symbol", "timeframe",
                           "calendar_id", "calendar_digest", "range_start_utc", "range_end_utc",
                           "range_semantics", "first_event_start_utc", "last_event_start_utc",
                           "expected_bar_count", "observed_in_session", "scheduled_closure_minutes",
                           "verified_no_tick_minutes", "unexplained_missing_minutes", "canonical_file",
                           "gap_set_ref", "completeness_status", "quality_verdict", "synthetic", "limitations")
SESSION_CALENDAR_BINDING_FIELDS = ("calendar_id", "calendar_digest", "source_id")
RELEASE_V0_4_EXTRA = ("session_calendar",)


def _ref(obj, where):
    if not isinstance(obj, dict) or set(obj) != {"relative_path", "sha256"}:
        raise ContractError("%s: must be {relative_path, sha256}" % where)
    _str(obj, "relative_path", where, max_len=512)
    _str(obj, "sha256", where, regex=SHA256_RE, min_len=64, max_len=64)


def validate_gap_set_v1(obj) -> None:
    """A gap SET bound to a pinned calendar. Zero or many gaps; each explicitly classified. No forced defect."""
    _exact_keys(obj, GAP_SET_FIELDS, "gap_set")
    _const(obj, "schema_version", "gap_set", "1.0")
    _str(obj, "source_id", "gap_set", regex=SLUG_RE, max_len=64)
    _str(obj, "canonical_symbol", "gap_set", max_len=64)
    _str(obj, "timeframe", "gap_set", enum=("M1",))
    _str(obj, "calendar_id", "gap_set", max_len=128)
    _str(obj, "calendar_digest", "gap_set", regex=SHA256_RE, min_len=64, max_len=64)
    gaps = obj["gaps"]
    if not isinstance(gaps, list):
        raise ContractError("gap_set: gaps must be a list (may be empty)")
    unexplained = 0
    prev_end = None
    for g in gaps:
        _exact_keys(g, GAP_FIELDS, "gap_set.gap")
        gs = _minute_epoch(g["gap_start_utc"], "gap_start_utc", "gap_set.gap")
        ge = _minute_epoch(g["gap_end_utc"], "gap_end_utc", "gap_set.gap")
        if ge <= gs:
            raise ContractError("gap_set.gap: end must be after start (half-open)")
        if prev_end is not None and gs < prev_end:
            raise ContractError("gap_set.gap: gaps must be ordered and non-overlapping")
        prev_end = ge
        cls = _str(g, "gap_class", "gap_set.gap", enum=GAP_CLASSES)
        _str(g, "reason", "gap_set.gap", max_len=512)
        if cls == "UNEXPLAINED":
            unexplained += 1
        # A VERIFIED_NO_TICK classification cannot stand on the producer's word: it REQUIRES a reference to a
        # signed no-tick evidence artifact (checked against the actual data by the consumer). Any other class
        # must NOT carry one. This is what stops a release improving its verdict by relabelling a gap no-tick.
        ntr = g["no_tick_evidence_ref"]
        if cls == "VERIFIED_NO_TICK":
            _ref(ntr, "gap_set.gap.no_tick_evidence_ref")
        elif ntr is not None:
            raise ContractError("gap_set.gap: only VERIFIED_NO_TICK may carry a no_tick_evidence_ref")
        if cls == "REPAIRED":
            rep = g["repair"]
            if not isinstance(rep, dict):
                raise ContractError("gap_set.gap: REPAIRED requires a repair object")
            _exact_keys(rep, REPAIR_FIELDS, "gap_set.gap.repair")
            _instant(rep["repaired_at_utc"], "repaired_at_utc", "gap_set.gap.repair")
            _str(rep, "repair_raw_object_sha256", "gap_set.gap.repair", regex=SHA256_RE, min_len=64, max_len=64)
            _str(rep, "repair_evidence_path", "gap_set.gap.repair", max_len=512)
            if rep["supersedes_gap_id"] is not None:
                _str(rep, "supersedes_gap_id", "gap_set.gap.repair", regex=SHA256_RE, min_len=64, max_len=64)
        elif g["repair"] is not None:
            raise ContractError("gap_set.gap: only REPAIRED may carry a repair object")
    if obj["unexplained_count"] != unexplained:
        raise ContractError("gap_set: unexplained_count must equal the number of UNEXPLAINED gaps")
    _bool(obj, "synthetic", "gap_set")
    _str_array(obj, "limitations", "gap_set")


NO_TICK_EVIDENCE_FIELDS = ("schema_version", "source_id", "calendar_id", "timeframe", "attested_minutes",
                           "provenance", "synthetic", "limitations")


def validate_no_tick_evidence_v1(obj) -> None:
    """Evidence that specific in-session minutes genuinely had no ticks (so they are not unexplained gaps).
    ``attested_minutes`` is a sorted, de-duplicated list of canonical minute instants."""
    _exact_keys(obj, NO_TICK_EVIDENCE_FIELDS, "no_tick_evidence")
    _const(obj, "schema_version", "no_tick_evidence", "1.0")
    _str(obj, "source_id", "no_tick_evidence", regex=SLUG_RE, max_len=64)
    _str(obj, "calendar_id", "no_tick_evidence", max_len=128)
    _str(obj, "timeframe", "no_tick_evidence", enum=("M1",))
    mins = obj["attested_minutes"]
    if not isinstance(mins, list):
        raise ContractError("no_tick_evidence: attested_minutes must be a list")
    prev = None
    for m in mins:
        e = _minute_epoch(m, "attested_minutes", "no_tick_evidence")
        if prev is not None and e <= prev:
            raise ContractError("no_tick_evidence: attested_minutes must be sorted and de-duplicated")
        prev = e
    _str(obj, "provenance", "no_tick_evidence", max_len=256)
    _bool(obj, "synthetic", "no_tick_evidence")
    _str_array(obj, "limitations", "no_tick_evidence")


def validate_session_quality_coverage_v2(obj) -> None:
    """Session-aware coverage: expected from the pinned calendar; closures / no-tick / unexplained kept
    distinct; verdict driven by unexplained misses (fail-closed)."""
    _exact_keys(obj, SESSION_COVERAGE_FIELDS, "session_coverage")
    _const(obj, "schema_version", "session_coverage", "1.0")
    _str(obj, "release_id", "session_coverage", regex=SLUG_RE, max_len=64)
    _str(obj, "source_id", "session_coverage", regex=SLUG_RE, max_len=64)
    _str(obj, "canonical_symbol", "session_coverage", max_len=64)
    _str(obj, "timeframe", "session_coverage", enum=("M1",))
    _str(obj, "calendar_id", "session_coverage", max_len=128)
    _str(obj, "calendar_digest", "session_coverage", regex=SHA256_RE, min_len=64, max_len=64)
    rs = _minute_epoch(obj["range_start_utc"], "range_start_utc", "session_coverage")
    re_ = _minute_epoch(obj["range_end_utc"], "range_end_utc", "session_coverage")
    if re_ <= rs:
        raise ContractError("session_coverage: range end must be after start")
    _const(obj, "range_semantics", "session_coverage", "[start,end)")
    _minute_epoch(obj["first_event_start_utc"], "first_event_start_utc", "session_coverage")
    _minute_epoch(obj["last_event_start_utc"], "last_event_start_utc", "session_coverage")
    for k in ("expected_bar_count", "observed_in_session", "scheduled_closure_minutes",
              "verified_no_tick_minutes", "unexplained_missing_minutes"):
        v = obj[k]
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ContractError("session_coverage: %s must be a non-negative int" % k)
    if obj["expected_bar_count"] != (obj["observed_in_session"] + obj["verified_no_tick_minutes"]
                                     + obj["unexplained_missing_minutes"]):
        raise ContractError("session_coverage: expected = observed_in_session + verified_no_tick + unexplained")
    _ref(obj["canonical_file"], "session_coverage.canonical_file")
    _ref(obj["gap_set_ref"], "session_coverage.gap_set_ref")
    verdict = _str(obj, "quality_verdict", "session_coverage", enum=QUALITY_VERDICTS)
    comp = _str(obj, "completeness_status", "session_coverage", enum=("COMPLETE", "DEGRADED"))
    # An unexplained gap MUST remain visible and drive the verdict: PASS/COMPLETE requires zero unexplained.
    if obj["unexplained_missing_minutes"] == 0:
        if verdict != "PASS":
            raise ContractError("session_coverage: zero unexplained must be PASS")
    else:
        if verdict == "PASS" or comp == "COMPLETE":
            raise ContractError("session_coverage: unexplained misses cannot be PASS/COMPLETE (must stay visible)")
    _bool(obj, "synthetic", "session_coverage")
    _str_array(obj, "limitations", "session_coverage")


SESSION_QUALITY_REPORT_FIELDS = (
    "schema_version", "source_id", "canonical_symbol", "timeframe", "calendar_id", "calendar_digest",
    "range_start_utc", "range_end_utc", "range_semantics", "expected_in_session_bars",
    "observed_in_session_bars", "out_of_session_bars", "verified_no_tick_bars", "unexplained_in_session_missing",
    "duplicate_count", "out_of_order_count", "overlap_count", "ohlc_violation_count", "non_finite_count",
    "quality_status", "quarantine_reasons", "evaluated_at_utc", "synthetic", "limitations")
_SQR_VIOLATIONS = ("duplicate_count", "out_of_order_count", "overlap_count", "ohlc_violation_count",
                   "non_finite_count")


def validate_session_quality_report_v2(obj) -> None:
    """Session-aware successor to the legacy v0.2 quality_report (which required expected = full calendar
    minutes and so could not represent a weekend-gapped release). Completeness is measured over the pinned
    calendar's TRADING session (expected/observed-in-session), bar-level DEFECTS are counted over the observed
    bars, and OUT-OF-SESSION quotes are reported as a SEPARATE count so they neither inflate in-session
    coverage nor disappear from the deliverable. PASS iff zero defects AND zero unexplained in-session misses.
    The v0.2 validator is untouched (v0.3 releases keep validating under it)."""
    _exact_keys(obj, SESSION_QUALITY_REPORT_FIELDS, "session_quality")
    _const(obj, "schema_version", "session_quality", "1.0")
    _str(obj, "source_id", "session_quality", regex=SLUG_RE, max_len=64)
    _str(obj, "canonical_symbol", "session_quality", max_len=64)
    _str(obj, "timeframe", "session_quality", enum=("M1",))
    _str(obj, "calendar_id", "session_quality", max_len=128)
    _str(obj, "calendar_digest", "session_quality", regex=SHA256_RE, min_len=64, max_len=64)
    rs = _minute_epoch(obj["range_start_utc"], "range_start_utc", "session_quality")
    re_ = _minute_epoch(obj["range_end_utc"], "range_end_utc", "session_quality")
    if re_ <= rs:
        raise ContractError("session_quality: range end must be after start")
    _const(obj, "range_semantics", "session_quality", "[start,end)")
    for k in ("expected_in_session_bars", "observed_in_session_bars", "out_of_session_bars",
              "verified_no_tick_bars", "unexplained_in_session_missing") + _SQR_VIOLATIONS:
        v = obj[k]
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ContractError("session_quality: %s must be a non-negative int" % k)
    if obj["expected_in_session_bars"] != (obj["observed_in_session_bars"] + obj["verified_no_tick_bars"]
                                           + obj["unexplained_in_session_missing"]):
        raise ContractError("session_quality: expected_in_session = observed_in_session + verified_no_tick "
                            "+ unexplained_in_session_missing")
    status = _str(obj, "quality_status", "session_quality", enum=("PASS", "QUARANTINE", "FAIL"))
    reasons = _str_array(obj, "quarantine_reasons", "session_quality")
    _instant(obj["evaluated_at_utc"], "evaluated_at_utc", "session_quality")
    _bool(obj, "synthetic", "session_quality")
    _str_array(obj, "limitations", "session_quality")
    clean = all(obj[f] == 0 for f in _SQR_VIOLATIONS) and obj["unexplained_in_session_missing"] == 0
    if status == "PASS":
        if not clean:
            raise ContractError("session_quality: PASS requires zero defects AND zero unexplained in-session misses")
        if reasons:
            raise ContractError("session_quality: PASS must not carry quarantine_reasons")
    else:
        if clean:
            raise ContractError("session_quality: %s requires a defect or an unexplained in-session miss" % status)
        if not reasons:
            raise ContractError("session_quality: %s requires at least one quarantine_reason" % status)


def validate_release_manifest_v0_4(obj) -> None:
    """v0.4 manifest = the full v3 manifest + a bound ``session_calendar`` + versions 3.1/0.4. The shared
    fields are validated by the FROZEN v3 validator via a version-normalised view (so v0.3 rules are reused
    verbatim and never forked)."""
    if not isinstance(obj, dict):
        raise ContractError("release: manifest must be an object")
    if obj.get("schema_version") != MANIFEST_SCHEMA_V0_4:
        raise ContractError("release: v0.4 manifest schema_version must be %r" % MANIFEST_SCHEMA_V0_4)
    if obj.get("contract_version") != CONTRACT_VERSION_V0_4:
        raise ContractError("release: v0.4 manifest contract_version must be %r" % CONTRACT_VERSION_V0_4)
    if "session_calendar" not in obj:
        raise ContractError("release: v0.4 manifest must bind a session_calendar")
    sc = obj["session_calendar"]
    _exact_keys(sc, SESSION_CALENDAR_BINDING_FIELDS, "release.session_calendar")
    _str(sc, "calendar_id", "release.session_calendar", max_len=128)
    _str(sc, "calendar_digest", "release.session_calendar", regex=SHA256_RE, min_len=64, max_len=64)
    _str(sc, "source_id", "release.session_calendar", regex=SLUG_RE, max_len=64)
    # Version-normalised v3 view: strip the v0.4-only field and restore v3 versions, then reuse the FROZEN
    # v3 structural validation unchanged.
    view = {k: v for k, v in obj.items() if k not in RELEASE_V0_4_EXTRA}
    view["schema_version"] = "3.0"
    view["contract_version"] = V3.CONTRACT_VERSION
    V3.validate_release_manifest_v3(view)
    if sc["source_id"] != obj.get("source_id"):
        raise ContractError("release.session_calendar: source_id must match the manifest source_id")


def validate_session_release_consistency(manifest, coverage, gap_set, calendar) -> None:
    """Cross-bind manifest <-> session coverage <-> gap set <-> the PINNED calendar. The calendar's digest
    must match everywhere, and the coverage counts must agree with the calendar over the release range."""
    CAL.validate_calendar(calendar)
    cal_digest = CAL.calendar_digest(calendar)
    cal_id = calendar["calendar_id"]
    binding = manifest["session_calendar"]
    for label, got in (("manifest", binding["calendar_digest"]), ("coverage", coverage["calendar_digest"]),
                       ("gap_set", gap_set["calendar_digest"])):
        if got != cal_digest:
            raise ContractError("release: %s calendar_digest does not match the pinned calendar" % label)
    for label, got in (("manifest", binding["calendar_id"]), ("coverage", coverage["calendar_id"]),
                       ("gap_set", gap_set["calendar_id"])):
        if got != cal_id:
            raise ContractError("release: %s calendar_id does not match the pinned calendar" % label)
    if coverage["release_id"] != manifest["release_id"]:
        raise ContractError("release: coverage release_id mismatch")
    if not (manifest["source_id"] == coverage["source_id"] == gap_set["source_id"] == calendar["source_id"]):
        raise ContractError("release: source_id mismatch across manifest/coverage/gap_set/calendar")
    # Expected from the pinned calendar MUST equal the coverage's expected count (independent recomputation).
    recomputed = CAL.expected_bar_count(calendar, coverage["range_start_utc"], coverage["range_end_utc"])
    if recomputed != coverage["expected_bar_count"]:
        raise ContractError("release: coverage expected_bar_count disagrees with the pinned calendar")
    # The number of UNEXPLAINED gaps in the gap set must be consistent with unexplained minutes > 0.
    if (gap_set["unexplained_count"] > 0) != (coverage["unexplained_missing_minutes"] > 0):
        raise ContractError("release: gap_set unexplained_count inconsistent with coverage unexplained minutes")


def validate_ready_marker_v0_4(obj) -> None:
    """A v0.4 READY marker is the v3 marker with ``contract_version == "0.4"``. Reuse the FROZEN v3 marker
    validator via a version-normalised view, then assert the v0.4 version."""
    if not isinstance(obj, dict):
        raise ContractError("ready_marker: must be an object")
    if obj.get("contract_version") != CONTRACT_VERSION_V0_4:
        raise ContractError("ready_marker: v0.4 contract_version must be %r" % CONTRACT_VERSION_V0_4)
    view = dict(obj)
    view["contract_version"] = V3.CONTRACT_VERSION
    V3.validate_ready_marker(view)


def validate_ready_marker(obj) -> None:
    """Dispatch a READY marker by contract_version (v0.3 frozen)."""
    if isinstance(obj, dict) and obj.get("contract_version") == CONTRACT_VERSION_V0_4:
        validate_ready_marker_v0_4(obj)
    else:
        V3.validate_ready_marker(obj)


def validate_release_manifest(manifest) -> str:
    """Dispatch by contract_version. Returns the contract version used. v0.3 stays on the frozen validator."""
    cv = manifest.get("contract_version") if isinstance(manifest, dict) else None
    if cv == CONTRACT_VERSION_V0_4:
        validate_release_manifest_v0_4(manifest)
        return CONTRACT_VERSION_V0_4
    V3.validate_release_manifest_v3(manifest)
    return V3.CONTRACT_VERSION
