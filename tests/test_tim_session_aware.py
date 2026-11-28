"""Session-aware market-data release contract (v0.4, ADR-0130) — regression evidence.

Proves, with fixtures, the full session-aware path: a pinned trading-session calendar (weekend closure,
holiday / partial session, DST transition), session-aware coverage (zero defects and multiple in-session
gaps kept distinct and visible), tampered coverage / calendar evidence rejection, ONE fixture release that
builds -> signs with a disposable TEST key -> assembles -> publishes -> validates through the real CLI, the
detection of a removed in-session bar (beyond the file-hash check, via independent recompute), and that an
existing v0.3 release still verifies under its original rules. No real key, NAS, network, MT5, broker or
production action occurs; the TEST-only signer lives only in this test (ADR-0070).
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from research.market_data import tim_contracts_v0_4 as V4  # noqa: E402
from research.market_data import tim_session_calendar as CAL  # noqa: E402
from research.market_data import tim_session_consumer as CON  # noqa: E402
from research.market_data import tim_ed25519 as E  # noqa: E402
from research.market_data import tim_fixtures_v0_3 as F  # noqa: E402
from research.market_data.contracts import canonical_json_bytes, sha256_hex  # noqa: E402
from research.market_data.tim_contracts_v0_3 import ContractError  # noqa: E402

_KV = {"not_before_utc": "2026-01-01T00:00:00Z", "not_after_utc": "2027-01-01T00:00:00Z"}
_CREATED = "2026-06-01T00:00:00Z"
_WEEKDAYS = {str(i): [["00:00", "24:00"]] for i in range(1, 6)}  # Mon-Fri fully open


def _cal(**over):
    base = {"schema_version": "1.0", "calendar_id": "fix_fx_v1", "source_id": F.SOURCE_ID,
            "iana_timezone": "Europe/Athens", "timeframe_seconds": 60,
            "utc_offset_rules": [{"effective_from_utc": "2026-01-01T00:00:00Z", "offset_seconds": 7200,
                                  "label": "winter+2"}],
            "weekly_sessions_local": dict(_WEEKDAYS), "holidays": [], "exceptional_sessions_local": {}}
    base.update(over)
    return base


class _Prov:
    """TEST ONLY disposable Ed25519 (pure-python RFC-8032; reuses tim_ed25519). Never persisted."""

    def __init__(self, seed=b"\x22" * 32):
        h = hashlib.sha512(seed).digest()
        a = bytearray(h[:32]); a[0] &= 248; a[31] &= 127; a[31] |= 64
        self._a = int.from_bytes(a, "little"); self._prefix = h[32:]
        self._A = E._encodepoint(E._scalarmult(E._B, self._a))

    def public_key_raw(self):
        return self._A

    def sign(self, message):
        m = bytes(message)
        r = int.from_bytes(hashlib.sha512(self._prefix + m).digest(), "little") % E._L
        rpt = E._encodepoint(E._scalarmult(E._B, r))
        k = int.from_bytes(hashlib.sha512(rpt + self._A + m).digest(), "little") % E._L
        return rpt + ((r + k * self._a) % E._L).to_bytes(32, "little")


class TestSessionCalendar(unittest.TestCase):
    def test_weekend_closure_excluded(self):
        cal = _cal()
        # Mon 2026-02-09 00:00 local(+2) = 2026-02-08T22:00Z. One week -> only Mon-Fri = 5*1440 in session.
        self.assertEqual(CAL.expected_bar_count(cal, "2026-02-08T22:00:00Z", "2026-02-15T22:00:00Z"), 5 * 1440)
        # A Saturday minute is a scheduled closure, not in session.
        self.assertFalse(CAL.is_in_session(cal, CAL._epoch_min("2026-02-14T12:00:00Z")))

    def test_holiday_and_partial_session(self):
        cal = _cal(holidays=["2026-02-16"], exceptional_sessions_local={"2026-02-17": [["00:00", "12:00"]]})
        # Mon 02-16 holiday (0) + Tue 02-17 partial 00:00-12:00 local = 720. Range Mon..Wed 00:00 local.
        self.assertEqual(CAL.expected_bar_count(cal, "2026-02-15T22:00:00Z", "2026-02-17T22:00:00Z"), 720)

    def test_dst_transition_shifts_utc_by_one_hour(self):
        cal = _cal(weekly_sessions_local={str(i): [["10:00", "11:00"]] for i in range(1, 6)},
                   utc_offset_rules=[{"effective_from_utc": "2026-01-01T00:00:00Z", "offset_seconds": 7200,
                                      "label": "winter+2"},
                                     {"effective_from_utc": "2026-03-29T01:00:00Z", "offset_seconds": 10800,
                                      "label": "summer+3"}])
        # Pre-DST Fri 2026-03-27 (+2): local 10:00 == 08:00Z in session; 07:00Z not.
        self.assertTrue(CAL.is_in_session(cal, CAL._epoch_min("2026-03-27T08:00:00Z")))
        self.assertFalse(CAL.is_in_session(cal, CAL._epoch_min("2026-03-27T07:00:00Z")))
        # Post-DST Mon 2026-03-30 (+3): local 10:00 == 07:00Z in session; 08:00Z not. The 1h shift is explicit.
        self.assertTrue(CAL.is_in_session(cal, CAL._epoch_min("2026-03-30T07:00:00Z")))
        self.assertFalse(CAL.is_in_session(cal, CAL._epoch_min("2026-03-30T08:00:00Z")))

    def test_calendar_digest_pins(self):
        cal = _cal()
        d0 = CAL.calendar_digest(cal)
        tampered = _cal(holidays=["2026-02-16"])
        self.assertNotEqual(d0, CAL.calendar_digest(tampered))


class TestSessionContracts(unittest.TestCase):
    def _classify(self, drops, no_tick=()):
        cal = _cal()
        s = CAL._epoch_min("2026-02-09T00:00:00Z")
        observed = set(range(s, s + 60)) - set(s + d for d in drops)
        return CAL.classify_coverage(cal, observed, "2026-02-09T00:00:00Z", "2026-02-09T01:00:00Z",
                                     verified_no_tick_minutes=[s + d for d in no_tick])

    def test_zero_defect_is_pass(self):
        r = self._classify(())
        self.assertEqual(r["unexplained_missing_minutes"], 0)
        self.assertEqual(r["quality_verdict"], "PASS")

    def test_multiple_in_session_gaps_stay_visible(self):
        r = self._classify((5, 20, 41))
        self.assertEqual(r["unexplained_missing_minutes"], 3)
        self.assertEqual(r["quality_verdict"], "DEGRADED")

    def test_verified_no_tick_distinct_from_unexplained(self):
        r = self._classify((5, 20), no_tick=(5,))
        self.assertEqual(r["verified_no_tick_minutes"], 1)
        self.assertEqual(r["unexplained_missing_minutes"], 1)
        self.assertEqual(r["quality_verdict"], "DEGRADED")

    def test_tampered_coverage_verdict_rejected(self):
        D = "a" * 64
        cov = {"schema_version": "1.0", "release_id": "rel_x", "source_id": "src_x", "canonical_symbol": "EURUSD",
               "timeframe": "M1", "calendar_id": "c1", "calendar_digest": D, "range_start_utc": "2026-02-09T00:00:00Z",
               "range_end_utc": "2026-02-09T01:00:00Z", "range_semantics": "[start,end)",
               "first_event_start_utc": "2026-02-09T00:00:00Z", "last_event_start_utc": "2026-02-09T00:58:00Z",
               "expected_bar_count": 60, "observed_in_session": 58, "scheduled_closure_minutes": 0,
               "verified_no_tick_minutes": 0, "unexplained_missing_minutes": 2,
               "canonical_file": {"relative_path": "rel_x/e.parquet", "sha256": D},
               "gap_set_ref": {"relative_path": "rel_x/gap.json", "sha256": D},
               "completeness_status": "COMPLETE", "quality_verdict": "PASS", "synthetic": True, "limitations": []}
        with self.assertRaises(ContractError):
            V4.validate_session_quality_coverage_v2(cov)  # 2 unexplained cannot be PASS/COMPLETE

    def test_tampered_calendar_digest_rejected(self):
        cal = _cal()
        dg = CAL.calendar_digest(cal)
        manifest = {"release_id": "rel_x", "source_id": F.SOURCE_ID,
                    "session_calendar": {"calendar_id": cal["calendar_id"], "calendar_digest": "b" * 64,
                                         "source_id": F.SOURCE_ID}}
        coverage = {"release_id": "rel_x", "source_id": F.SOURCE_ID, "calendar_id": cal["calendar_id"],
                    "calendar_digest": dg, "range_start_utc": "2026-02-09T00:00:00Z",
                    "range_end_utc": "2026-02-09T01:00:00Z", "expected_bar_count": 60,
                    "unexplained_missing_minutes": 0}
        gap_set = {"calendar_id": cal["calendar_id"], "calendar_digest": dg, "source_id": F.SOURCE_ID,
                   "unexplained_count": 0}
        with self.assertRaises(ContractError):
            V4.validate_session_release_consistency(manifest, coverage, gap_set, cal)  # digest mismatch

    def test_verified_no_tick_gap_requires_evidence_ref(self):
        D = "a" * 64

        def gs(ref):
            return {"schema_version": "1.0", "source_id": "src_x", "canonical_symbol": "EURUSD", "timeframe": "M1",
                    "calendar_id": "c1", "calendar_digest": D,
                    "gaps": [{"gap_start_utc": "2026-02-09T00:05:00Z", "gap_end_utc": "2026-02-09T00:06:00Z",
                              "gap_class": "VERIFIED_NO_TICK", "reason": "x", "repair": None,
                              "no_tick_evidence_ref": ref}],
                    "unexplained_count": 0, "synthetic": True, "limitations": []}
        V4.validate_gap_set_v1(gs({"relative_path": "r/no_tick.json", "sha256": D}))  # with evidence ref: ok
        with self.assertRaises(ContractError):
            V4.validate_gap_set_v1(gs(None))  # no_tick without an evidence ref is rejected


try:
    import duckdb  # noqa: F401
    import tim_session_release_builder as B  # noqa: E402
    import tim_release_v3_operational_proof as P  # noqa: E402
    import tim_release_cli as CLI  # noqa: E402
    _HAVE_DUCKDB = True
except Exception:
    _HAVE_DUCKDB = False


def _write_store(path, provider):
    pub = bytes(provider.public_key_raw()); fp = sha256_hex(pub)
    store = {"schema_version": "1.0", "keys": [{"signing_key_id": "ed25519-" + fp[:32], "algorithm": "ed25519-v1",
             "public_key": pub.hex(), "public_key_fingerprint": fp, "not_before_utc": _KV["not_before_utc"],
             "not_after_utc": _KV["not_after_utc"], "status": "active"}]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(store, fh)


def _producer_sign(fields_path, out_path, provider):
    with open(fields_path) as fh:
        fields = json.load(fh)
    env, _ = P.build_v3_envelope(provider, fields)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (json.dumps(env, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)


def _run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = CLI.main(argv)
    return rc, json.loads(buf.getvalue().strip().splitlines()[-1])


def _rb(path):
    with open(path, "rb") as fh:
        return fh.read()


def _write_calendar_store(path, calendar, *, status="approved"):
    store = {"schema_version": "1.0", "calendars": [
        {"calendar_id": calendar["calendar_id"], "calendar_digest": CAL.calendar_digest(calendar),
         "status": status, "calendar": calendar}]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(store, fh)


@unittest.skipUnless(_HAVE_DUCKDB, "duckdb required for the session-aware build path")
class TestSessionFullPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tim-sess-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.prov = _Prov()
        self.kid = "ed25519-" + sha256_hex(bytes(self.prov.public_key_raw()))[:32]
        self.store = os.path.join(self.tmp, "store.json"); _write_store(self.store, self.prov)
        self.cal_store = os.path.join(self.tmp, "calstore.json"); _write_calendar_store(self.cal_store, _cal())

    def _build(self, release_id, *, count=600, drop_minutes=(), verified_no_tick=()):
        return B.build_session_release(
            self.tmp, calendar=_cal(), release_id=release_id, source_id=F.SOURCE_ID, signing_key_id=self.kid,
            producer_identity="guvfx_tim_producer", created_at_utc=_CREATED, key_validity=_KV,
            start_utc="2026-02-09T00:00:00Z", count=count, drop_minutes=drop_minutes,
            verified_no_tick=verified_no_tick)

    def _seal_publish_validate(self, candidate, release_id):
        sigout = os.path.join(self.tmp, release_id + ".sig.json")
        _producer_sign(os.path.join(candidate, "request.fields.json"), sigout, self.prov)
        rc, rec = _run(["assemble", "--candidate", candidate, "--signature", sigout, "--trust-store", self.store])
        self.assertEqual((rc, rec["state"]), (0, "SEALED"), rec)
        mount = os.path.join(self.tmp, release_id + "_mount"); os.makedirs(mount)
        with open(os.path.join(mount, CLI.SCOPE_MARKER), "w") as fh:
            json.dump({"producer_identity": "guvfx-tim-producer", "scope": "exchange/releases"}, fh)
        rc, rec = _run(["publish", "--candidate", candidate, "--mount-root", mount])
        self.assertEqual((rc, rec["result"]), (0, "published"), rec)
        published = os.path.join(mount, "exchange", "releases", release_id)
        rc, rec = _run(["validate", "--release-dir", published, "--trust-store", self.store,
                        "--calendar-store", self.cal_store])
        return rc, rec, published

    def _resign(self, published, built, mutate):
        """Apply `mutate(published)` to the published files, then re-establish full internal consistency
        (every files[] digest + evidence sha recomputed, manifest + READY re-signed with the disposable key)
        so only the deeper semantic checks — not the hash/signature checks — can catch the tamper."""
        mutate(published)
        parent = os.path.dirname(published)
        man = json.loads(_rb(os.path.join(published, "manifest.json")))
        for f in man["files"]:
            data = _rb(os.path.join(parent, f["relative_path"]))
            f["digest"]["value"] = sha256_hex(data); f["byte_size"] = len(data)
        for _role, ref in man["evidence"].items():
            ref["sha256"] = sha256_hex(_rb(os.path.join(parent, ref["relative_path"])))
        mb = canonical_json_bytes(man)
        with open(os.path.join(published, "manifest.json"), "wb") as fh:
            fh.write(mb)
        fields = dict(built["fields"]); fields["manifest_digest"] = sha256_hex(mb)
        env, _ = P.build_v3_envelope(self.prov, fields); sigb = canonical_json_bytes(env)
        with open(os.path.join(published, "manifest.sig.json"), "wb") as fh:
            fh.write(sigb)
        ready = json.loads(_rb(os.path.join(published, "READY")))
        ready["manifest_digest"] = sha256_hex(mb); ready["signature_envelope_digest"] = sha256_hex(sigb)
        with open(os.path.join(published, "READY"), "wb") as fh:
            fh.write(canonical_json_bytes(ready))
        return _run(["validate", "--release-dir", published, "--trust-store", self.store,
                     "--calendar-store", self.cal_store])

    def test_zero_defect_full_path(self):
        built = self._build("rel_sess_clean")
        self.assertEqual(built["quality_verdict"], "PASS")
        rc, rec, _ = self._seal_publish_validate(built["rel_dir"], "rel_sess_clean")
        self.assertEqual((rc, rec["accepted"]), (0, True), rec)
        self.assertEqual(rec["quality_verdict"], "PASS")
        self.assertEqual(rec["contract_version"], "0.4")
        self.assertEqual(rec["unexplained_missing_minutes"], 0)

    def test_degraded_multi_gap_full_path(self):
        built = self._build("rel_sess_degraded", drop_minutes=(100, 220, 333))
        self.assertEqual(built["quality_verdict"], "DEGRADED")
        rc, rec, _ = self._seal_publish_validate(built["rel_dir"], "rel_sess_degraded")
        self.assertEqual((rc, rec["accepted"]), (0, True), rec)  # honest DEGRADED release is accepted
        self.assertEqual(rec["quality_verdict"], "DEGRADED")
        self.assertEqual(rec["unexplained_missing_minutes"], 3)

    def test_verified_no_tick_accepted_as_pass(self):
        built = self._build("rel_sess_notick", drop_minutes=(50, 51), verified_no_tick=(50, 51))
        self.assertEqual(built["quality_verdict"], "PASS")  # both misses attested no-tick
        rc, rec, _ = self._seal_publish_validate(built["rel_dir"], "rel_sess_notick")
        self.assertEqual((rc, rec["accepted"]), (0, True), rec)
        self.assertEqual(rec["verified_no_tick_minutes"], 2)
        self.assertEqual(rec["unexplained_missing_minutes"], 0)

    def test_ndjson_divergence_detected(self):
        built = self._build("rel_div_nd")
        rc, rec, published = self._seal_publish_validate(built["rel_dir"], "rel_div_nd")
        self.assertEqual((rc, rec["accepted"]), (0, True), rec)

        def mutate(pub):  # drop one NDJSON row; the Parquet is left intact
            nd = os.path.join(pub, "eurusd_m1.raw.ndjson")
            kept = [ln for ln in _rb(nd).split(b"\n") if ln.strip()][:-1]
            with open(nd, "wb") as fh:
                fh.write(b"\n".join(kept) + b"\n")
        rc, rec = self._resign(published, built, mutate)
        self.assertEqual((rc, rec["accepted"]), (4, False), rec)
        self.assertEqual(rec["reason"], "parquet_ndjson_divergence")

    def test_parquet_drops_bar_detected_even_when_raw_valid(self):
        # The research artifact is the Parquet. A Parquet that drops a bar while the raw NDJSON stays valid —
        # with hashes + signature all re-made consistent — must still fail (coverage is checked against the
        # Parquet, proven to match the NDJSON).
        built = self._build("rel_div_pq")
        rc, rec, published = self._seal_publish_validate(built["rel_dir"], "rel_div_pq")
        self.assertEqual((rc, rec["accepted"]), (0, True), rec)

        def mutate(pub):
            import duckdb
            pq = os.path.join(pub, "eurusd_m1.parquet")
            con = duckdb.connect(":memory:")
            try:
                con.execute("CREATE TABLE t AS SELECT * FROM read_parquet('%s')" % pq)
                con.execute("DELETE FROM t WHERE event_start_utc = (SELECT MAX(event_start_utc) FROM t)")
                os.remove(pq)
                con.execute("COPY t TO '%s' (FORMAT PARQUET)" % pq)
            finally:
                con.close()
        rc, rec = self._resign(published, built, mutate)
        self.assertEqual((rc, rec["accepted"]), (4, False), rec)
        self.assertEqual(rec["reason"], "parquet_ndjson_divergence")

    def test_unapproved_calendar_rejected(self):
        built = self._build("rel_unapproved")
        rc, rec, published = self._seal_publish_validate(built["rel_dir"], "rel_unapproved")
        self.assertEqual((rc, rec["accepted"]), (0, True), rec)
        empty = os.path.join(self.tmp, "empty_calstore.json")
        with open(empty, "w") as fh:
            json.dump({"schema_version": "1.0", "calendars": []}, fh)
        rc, rec = _run(["validate", "--release-dir", published, "--trust-store", self.store,
                        "--calendar-store", empty])
        self.assertEqual((rc, rec["accepted"]), (4, False), rec)
        self.assertEqual(rec["reason"], "unapproved_calendar")

    def test_calendar_not_matching_approved_pin_rejected(self):
        # The release's bound calendar must match the APPROVED pin by digest — a producer cannot substitute
        # its own calendar (e.g. to relabel an unexplained gap a scheduled closure).
        built = self._build("rel_wrongcal")
        rc, rec, published = self._seal_publish_validate(built["rel_dir"], "rel_wrongcal")
        self.assertEqual((rc, rec["accepted"]), (0, True), rec)
        other_store = os.path.join(self.tmp, "other_calstore.json")
        _write_calendar_store(other_store, _cal(holidays=["2026-02-09"]))  # same id, different digest
        rc, rec = _run(["validate", "--release-dir", published, "--trust-store", self.store,
                        "--calendar-store", other_store])
        self.assertEqual((rc, rec["accepted"]), (4, False), rec)
        self.assertEqual(rec["reason"], "calendar_binding_not_approved")

    def test_no_tick_without_covering_evidence_rejected(self):
        # A DEGRADED release with one unexplained in-session miss cannot be laundered to PASS by relabelling
        # the gap VERIFIED_NO_TICK with no-tick evidence that does NOT cover that minute.
        built = self._build("rel_forged_notick", drop_minutes=(100,))
        self.assertEqual(built["quality_verdict"], "DEGRADED")
        rc, rec, published = self._seal_publish_validate(built["rel_dir"], "rel_forged_notick")
        self.assertEqual((rc, rec["accepted"]), (0, True), rec)

        def mutate(pub):
            rid = os.path.basename(pub)
            nte = {"schema_version": "1.0", "source_id": F.SOURCE_ID, "calendar_id": _cal()["calendar_id"],
                   "timeframe": "M1", "attested_minutes": ["2026-02-09T00:00:00Z"],  # NOT the missing minute
                   "provenance": "forged", "synthetic": True, "limitations": ["SYNTH"]}
            nteb = canonical_json_bytes(nte)
            with open(os.path.join(pub, "no_tick_evidence.json"), "wb") as fh:
                fh.write(nteb)
            gap = json.loads(_rb(os.path.join(pub, "gap.json")))
            g = gap["gaps"][0]
            g["gap_class"] = "VERIFIED_NO_TICK"; g["reason"] = "relabelled"
            g["no_tick_evidence_ref"] = {"relative_path": rid + "/no_tick_evidence.json", "sha256": sha256_hex(nteb)}
            gap["unexplained_count"] = 0
            with open(os.path.join(pub, "gap.json"), "wb") as fh:
                fh.write(canonical_json_bytes(gap))
            cov = json.loads(_rb(os.path.join(pub, "quality_coverage.json")))
            cov["unexplained_missing_minutes"] = 0; cov["verified_no_tick_minutes"] = 1
            cov["quality_verdict"] = "PASS"; cov["completeness_status"] = "COMPLETE"
            with open(os.path.join(pub, "quality_coverage.json"), "wb") as fh:
                fh.write(canonical_json_bytes(cov))
        rc, rec = self._resign(published, built, mutate)
        self.assertEqual((rc, rec["accepted"]), (4, False), rec)
        self.assertEqual(rec["reason"], "no_tick_evidence_does_not_cover_minute")


@unittest.skipUnless(_HAVE_DUCKDB, "duckdb required")
class TestBackCompat(unittest.TestCase):
    def test_v0_3_release_still_validates_under_original_rules(self):
        # The dispatch routes a v0.3 manifest to the FROZEN v3 validator and a v0.3 release through the
        # original CLI consumer unchanged.
        tmp = tempfile.mkdtemp(prefix="tim-sess-bc-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        prov = _Prov(seed=b"\x33" * 32)
        kid = "ed25519-" + sha256_hex(bytes(prov.public_key_raw()))[:32]
        store = os.path.join(tmp, "store.json"); _write_store(store, prov)
        rc, rec = _run(["build", "--root", tmp, "--signing-key-id", kid])
        self.assertEqual((rc, rec["state"]), (0, "UNSEALED"), rec)
        candidate = os.path.join(tmp, "staging", P.RELEASE_ID)
        self.assertEqual(V4.validate_release_manifest(
            json.loads(_rb(os.path.join(candidate, "manifest.json")))), "0.3")
        sigout = os.path.join(tmp, "sig.json")
        _producer_sign(os.path.join(candidate, "request.fields.json"), sigout, prov)
        rc, rec = _run(["assemble", "--candidate", candidate, "--signature", sigout, "--trust-store", store])
        self.assertEqual((rc, rec["state"]), (0, "SEALED"), rec)
        self.assertEqual(json.loads(_rb(os.path.join(candidate, "READY")))["contract_version"], "0.3")


if __name__ == "__main__":
    unittest.main()
