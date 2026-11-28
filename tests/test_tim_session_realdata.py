"""Real-data v0.4 builder + session quality_report successor (ADR-0130) — regression evidence.

Proves the full build (from a delivered Parquet with BOTH in-session and out-of-session bars) -> sign with a
disposable TEST key -> assemble -> publish to an isolated test destination -> consumer validate path, that
out-of-session quotes are reported separately (neither inflating coverage nor vanishing), that a removed
in-session bar is detected, and that conflicting quality evidence is rejected. No real snapshot or operational
key is used. v0.3 release compatibility is covered by tests/test_tim_session_aware.py (TestBackCompat).
"""
import contextlib
import datetime
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from research.market_data import tim_ed25519 as E  # noqa: E402
from research.market_data import tim_session_calendar as CAL  # noqa: E402
from research.market_data.contracts import canonical_json_bytes, sha256_hex  # noqa: E402

try:
    import duckdb  # noqa: F401
    import tim_session_release_builder as B  # noqa: E402
    import tim_release_v3_operational_proof as P  # noqa: E402
    import tim_release_cli as CLI  # noqa: E402
    _HAVE = True
except Exception:
    _HAVE = False

_KV = {"not_before_utc": "2026-01-01T00:00:00Z", "not_after_utc": "2027-01-01T00:00:00Z"}
_CREATED = "2026-06-01T00:00:00Z"
_SRC = "mt5_fixture_src"


def _cal():
    # Mon-Fri session 00:00-12:00 (UTC, offset 0); 12:00-24:00 and weekends closed.
    return {"schema_version": "1.0", "calendar_id": "fix_partial_v1", "source_id": _SRC,
            "iana_timezone": "Etc/UTC", "timeframe_seconds": 60,
            "utc_offset_rules": [{"effective_from_utc": "2026-01-01T00:00:00Z", "offset_seconds": 0, "label": "utc"}],
            "weekly_sessions_local": {str(i): [["00:00", "12:00"]] for i in range(1, 6)},
            "holidays": [], "exceptional_sessions_local": {}}


class _Prov:
    def __init__(self, seed=b"\x44" * 32):
        h = hashlib.sha512(seed).digest(); a = bytearray(h[:32]); a[0] &= 248; a[31] &= 127; a[31] |= 64
        self._a = int.from_bytes(a, "little"); self._prefix = h[32:]; self._A = E._encodepoint(E._scalarmult(E._B, self._a))

    def public_key_raw(self): return self._A

    def sign(self, m):
        m = bytes(m); r = int.from_bytes(hashlib.sha512(self._prefix + m).digest(), "little") % E._L
        rpt = E._encodepoint(E._scalarmult(E._B, r)); k = int.from_bytes(hashlib.sha512(rpt + self._A + m).digest(), "little") % E._L
        return rpt + ((r + k * self._a) % E._L).to_bytes(32, "little")


def _make_parquet(path, minutes):
    con = duckdb.connect(":memory:")
    rows = []
    for i, m in enumerate(sorted(minutes)):
        base = Decimal("1.10000") + Decimal(i % 10) * Decimal("0.00001")
        s = datetime.datetime.fromtimestamp(m * 60, datetime.timezone.utc).replace(tzinfo=None)
        e = datetime.datetime.fromtimestamp((m + 1) * 60, datetime.timezone.utc).replace(tzinfo=None)
        rows.append((s, e, base, base + Decimal("0.00008"), base - Decimal("0.00006"), base + Decimal("0.00002"), 10 + i % 7, 0))
    con.execute('CREATE TABLE c (event_start_utc TIMESTAMP NOT NULL, event_end_utc TIMESTAMP NOT NULL, '
                '"open" DECIMAL(38,18) NOT NULL, high DECIMAL(38,18) NOT NULL, low DECIMAL(38,18) NOT NULL, '
                '"close" DECIMAL(38,18) NOT NULL, tick_volume BIGINT NOT NULL, quality_flags UINTEGER NOT NULL)')
    con.executemany("INSERT INTO c VALUES (?,?,?,?,?,?,?,?)", rows)
    con.execute("COPY (SELECT * FROM c ORDER BY event_start_utc) TO '%s' (FORMAT PARQUET)" % path)
    con.close()


def _write_store(path, prov):
    pub = bytes(prov.public_key_raw()); fp = sha256_hex(pub)
    json.dump({"schema_version": "1.0", "keys": [{"signing_key_id": "ed25519-" + fp[:32], "algorithm": "ed25519-v1",
              "public_key": pub.hex(), "public_key_fingerprint": fp, "not_before_utc": _KV["not_before_utc"],
              "not_after_utc": _KV["not_after_utc"], "status": "active"}]}, open(path, "w"))


def _write_calstore(path, cal):
    json.dump({"schema_version": "1.0", "calendars": [{"calendar_id": cal["calendar_id"],
              "calendar_digest": CAL.calendar_digest(cal), "status": "approved", "calendar": cal}]}, open(path, "w"))


def _run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = CLI.main(argv)
    return rc, json.loads(buf.getvalue().strip().splitlines()[-1])


def _rb(p):
    with open(p, "rb") as fh:
        return fh.read()


@unittest.skipUnless(_HAVE, "duckdb required")
class TestRealDataBuilder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tim-rd-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.cal = _cal()
        self.prov = _Prov()
        self.kid = "ed25519-" + sha256_hex(bytes(self.prov.public_key_raw()))[:32]
        self.store = os.path.join(self.tmp, "ks.json"); _write_store(self.store, self.prov)
        self.calstore = os.path.join(self.tmp, "cs.json"); _write_calstore(self.calstore, self.cal)
        # Monday 2026-02-09 00:00Z; 720 in-session (00:00-11:59) + 60 OUT-of-session (12:00-12:59).
        base = CAL._epoch_min("2026-02-09T00:00:00Z")
        self.parquet = os.path.join(self.tmp, "src.parquet")
        _make_parquet(self.parquet, list(range(base, base + 780)))

    def _build(self, rid):
        return B.build_session_release_from_parquet(
            self.tmp, parquet_path=self.parquet, calendar=self.cal, release_id=rid, source_id=_SRC,
            signing_key_id=self.kid, producer_identity="guvfx_tim_producer", created_at_utc=_CREATED,
            key_validity=_KV, synthetic=True)

    def _publish_validate(self, built, rid, resign=None):
        cand = built["rel_dir"]
        sigout = os.path.join(self.tmp, rid + ".sig")
        env, _ = P.build_v3_envelope(self.prov, json.load(open(os.path.join(cand, "request.fields.json"))))
        fd = os.open(sigout, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, (json.dumps(env, sort_keys=True) + "\n").encode()); os.close(fd)
        rc, rec = _run(["assemble", "--candidate", cand, "--signature", sigout, "--trust-store", self.store])
        self.assertEqual((rc, rec["state"]), (0, "SEALED"), rec)
        mount = os.path.join(self.tmp, rid + "_m"); os.makedirs(mount)
        json.dump({"producer_identity": "guvfx-tim-producer", "scope": "exchange/releases"}, open(os.path.join(mount, CLI.SCOPE_MARKER), "w"))
        rc, rec = _run(["publish", "--candidate", cand, "--mount-root", mount])
        self.assertEqual((rc, rec["result"]), (0, "published"), rec)
        pub = os.path.join(mount, "exchange", "releases", rid)
        if resign:
            resign(pub)
            man = json.loads(_rb(os.path.join(pub, "manifest.json")))
            parent = os.path.dirname(pub)
            for f in man["files"]:
                data = _rb(os.path.join(parent, f["relative_path"])); f["digest"]["value"] = sha256_hex(data); f["byte_size"] = len(data)
            for _role, ref in man["evidence"].items():
                ref["sha256"] = sha256_hex(_rb(os.path.join(parent, ref["relative_path"])))
            mb = canonical_json_bytes(man); open(os.path.join(pub, "manifest.json"), "wb").write(mb)
            flds = dict(built["fields"]); flds["manifest_digest"] = sha256_hex(mb)
            env2, _ = P.build_v3_envelope(self.prov, flds); sigb = canonical_json_bytes(env2); open(os.path.join(pub, "manifest.sig.json"), "wb").write(sigb)
            ready = json.loads(_rb(os.path.join(pub, "READY"))); ready["manifest_digest"] = sha256_hex(mb); ready["signature_envelope_digest"] = sha256_hex(sigb)
            open(os.path.join(pub, "READY"), "wb").write(canonical_json_bytes(ready))
        return _run(["validate", "--release-dir", pub, "--trust-store", self.store, "--calendar-store", self.calstore])

    def test_out_of_session_quotes_reported_and_accepted(self):
        built = self._build("rel_rd_oos")
        self.assertEqual(built["out_of_session_bars"], 60)
        self.assertEqual(built["quality_verdict"], "PASS")
        rc, rec = self._publish_validate(built, "rel_rd_oos")
        self.assertEqual((rc, rec["accepted"]), (0, True), rec)
        self.assertEqual(rec["out_of_session_bars"], 60)        # out-of-session reported separately
        self.assertEqual(rec["observed_in_session"], 720)       # not inflated by the 60 out-of-session bars
        self.assertEqual(rec["unexplained_missing_minutes"], 0)
        self.assertEqual(rec["quality_status"], "PASS")

    def test_quality_report_understating_out_of_session_rejected(self):
        built = self._build("rel_rd_bad_oos")

        def resign(pub):
            q = json.loads(_rb(os.path.join(pub, "quality.json")))
            q["out_of_session_bars"] = 0  # hide the out-of-session quotes
            open(os.path.join(pub, "quality.json"), "wb").write(canonical_json_bytes(q))
        rc, rec = self._publish_validate(built, "rel_rd_bad_oos", resign=resign)
        self.assertEqual((rc, rec["accepted"]), (4, False), rec)
        self.assertEqual(rec["reason"], "quality_report_recompute_mismatch")

    def test_removed_in_session_bar_detected(self):
        built = self._build("rel_rd_drop")

        def resign(pub):
            # drop one IN-session bar from the Parquet + NDJSON consistently; leave coverage/quality claiming full.
            base = CAL._epoch_min("2026-02-09T00:00:00Z")
            keep = [m for m in range(base, base + 780) if m != base + 100]
            _make_parquet(os.path.join(pub, "eurusd_m1.parquet"), keep)
            nd = [ln for ln in _rb(os.path.join(pub, "eurusd_m1.raw.ndjson")).split(b"\n") if ln.strip()]
            drop_key = b'"event_start_utc":"' + B._min_iso(base + 100).encode() + b'"'
            nd = [ln for ln in nd if drop_key not in ln]
            open(os.path.join(pub, "eurusd_m1.raw.ndjson"), "wb").write(b"\n".join(nd) + b"\n")
        rc, rec = self._publish_validate(built, "rel_rd_drop", resign=resign)
        self.assertEqual((rc, rec["accepted"]), (4, False), rec)
        self.assertIn(rec["reason"], ("coverage_recompute_mismatch", "quality_report_recompute_mismatch"))


if __name__ == "__main__":
    unittest.main()
