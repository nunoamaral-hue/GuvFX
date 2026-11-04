"""GUVFX-TIM Contract v0.3 repository tests (GFX-PKT-TIM-C2). synthetic_test_only.

Proves offline, with no network/MT5/NAS/broker/credential/production dependency:
  * the 7 additive v0.3 schemas (research/contracts/v0_3/) are well-formed and byte-pinned (mutation-killing);
  * the pre-existing 006C v1 schema bytes are UNCHANGED (pinned SHA-256) — v0.3 is strictly additive;
  * every positive fixture validates and every adversarial fixture is rejected/quarantined as specified;
  * the committed on-disk v0.3 fixtures validate and are clearly synthetic;
  * the canonical Parquet candle schema + row invariants, the signature envelope verify/tamper, and the
    point-in-time admissibility gates behave correctly;
  * the offline synthetic v0.3 release smoke passes (genuine Parquet, READY-last, immutability, receipt outside
    the release, tamper + PIT checks) network-blocked;
  * the new v0.3 modules import no network/MT5/execution/order-path symbol.
"""
import datetime as _dt
import hashlib
import json
import os
import re
import socket
import sys
import unittest
from decimal import Decimal

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from research.market_data import tim_contracts_v0_3 as V  # noqa: E402
from research.market_data import tim_fixtures_v0_3 as F  # noqa: E402
from research.market_data.agent_client import network_blocked  # noqa: E402
from research.market_data.contracts import ContractError, ProhibitedKeyError  # noqa: E402

CONTRACTS_DIR = os.path.join(REPO_ROOT, "research", "contracts")
V03_DIR = os.path.join(CONTRACTS_DIR, "v0_3")

# Pinned SHA-256 of every v0.3 schema. Editing any schema changes its hash and fails this test (mutation-killing).
V03_SCHEMA_SHA256 = {
    "canonical_candles_parquet_v1": "e68c873be692c0bb20cc2c1ce3f7606079cd6d7709109c0dfc6b56781d44b28a",
    "dataset_import_receipt_v2": "683a0dacaa4805211aa4ab325de2700c7bf78effe2f6e2353f029b2ac3167d2b",
    "dataset_release_manifest_v3": "883e54e1c10580e830c097e7931ccac682350fdffb66d0066143a21caf09065d",
    "dataset_release_signature_v2": "d4eef7674ac21783d8f537f1022ea009f7b5b8d71466a48ef8bf7f6b22eca758",
    "release_ready_marker_v1": "5b8996e6347fa13df67bdf359a6e1e956f2733a41f5c1d37fa03e08acd4760c3",
    "release_supersession_v1": "65a99892c63fc5161825925674b9d1092332d0bdf720101bfc9e97b6016d4a5f",
    "source_eligibility_v1": "9dc95164362c685f9a6904ad6351b23bae3f5ab3a200dd7d6f61a013d45e153a",
}
# The 006C v1 schemas that MUST remain byte-identical (GFX-PKT-TIM-C2 hard requirement).
V1_006C_SHA256 = {
    "agent_history_export_request_v1": "bbafa2fe1b3170167c1778b13dde369a406721f3a6fb2244daee0ffe82796a23",
    "agent_history_export_response_v1": "2e952cba01057536604bad720b8546c31e6e872a3d62f34a37cd5a8ac178fe24",
    "broker_cost_v1": "868b4b37d0a78cc5f66d9755a2279c805db60dff5473fe055025539da379d08b",
    "broker_timezone_evidence_v1": "074fbc79344b4b549fbd674df36119eed6598b8258444cd11f15973e011477ab",
    "dataset_manifest_v1": "7a43418503067bf0f7c555d1afe8e80e6de5f404081d32a95e9af3e602249da8",
    "market_observation_v1": "936d4065f03ca9deb91e9e71fb26ee604f59d241222dd6c8877e9de6074fb9e5",
    "raw_market_data_manifest_v1": "44c1b8b74eb9a4eefb6c65b4b9815fff75f316021f202d42565e85a9de375675",
}


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class TestV03SchemaHashes(unittest.TestCase):
    def test_v0_3_schema_set_and_hashes_pinned(self):
        on_disk = {f[:-len(".schema.json")] for f in os.listdir(V03_DIR) if f.endswith(".schema.json")}
        self.assertEqual(on_disk, set(V03_SCHEMA_SHA256), "v0.3 schema set drifted from the pinned map")
        for name, pinned in V03_SCHEMA_SHA256.items():
            self.assertEqual(_sha(os.path.join(V03_DIR, name + ".schema.json")), pinned,
                             f"{name}.schema.json byte-hash changed")

    def test_seven_additive_schemas(self):
        self.assertEqual(len(V.TIM_V0_3_SCHEMA_FILES), 7)
        self.assertEqual(set(V.TIM_V0_3_SCHEMA_FILES), set(V03_SCHEMA_SHA256))

    def test_006c_v1_byte_identical(self):
        # Hard GO requirement: the pre-existing 006C v1 contracts are byte-identical (v0.3 is strictly additive).
        for name, pinned in V1_006C_SHA256.items():
            self.assertEqual(_sha(os.path.join(CONTRACTS_DIR, name + ".schema.json")), pinned,
                             f"006C v1 schema {name} must be byte-identical")


class TestV03SchemaStructure(unittest.TestCase):
    def test_schemas_wellformed(self):
        for name in V.TIM_V0_3_SCHEMA_FILES:
            with open(os.path.join(V03_DIR, name + ".schema.json")) as fh:
                d = json.load(fh)
            self.assertEqual(d["$id"], f"https://guvfx.local/schema/{name}.schema.json")
            self.assertEqual(d["$schema"], "https://json-schema.org/draft-07/schema#")
            self.assertIs(d["additionalProperties"], False)
            self.assertIn("required", d)
            self.assertIn("title", d)


class TestV03ValidatorMatrix(unittest.TestCase):
    def test_positive_fixtures_accept(self):
        for name, payload in F.positive_fixtures().items():
            with self.subTest(fixture=name):
                V.VALIDATORS_V0_3[F.contract_of(name)](payload)  # must not raise

    def test_adversarial_cases_rejected_or_quarantined(self):
        for c in F.adversarial_cases():
            with self.subTest(case=c["name"], expect=c["expect"]):
                fn = V.VALIDATORS_V0_3[c["contract"]]
                if c["expect"] == "quarantine":
                    with self.assertRaises(V.QuarantineRequired):
                        fn(c["payload"])
                else:
                    with self.assertRaises((ContractError, ProhibitedKeyError)) as ctx:
                        fn(c["payload"])
                    self.assertNotIsInstance(ctx.exception, V.QuarantineRequired,
                                             f"{c['name']} should be a hard reject, not a quarantine")

    def test_matrix_is_nontrivial(self):
        self.assertGreaterEqual(len(F.positive_fixtures()), 7)
        self.assertGreaterEqual(len(F.adversarial_cases()), 22)


class TestV03OnDiskFixtures(unittest.TestCase):
    def test_committed_fixtures_validate(self):
        fdir = os.path.join(REPO_ROOT, "tests", "fixtures", "market_data", "tim", "v0_3")
        files = [f for f in os.listdir(fdir) if f.endswith(".json")]
        self.assertEqual(len(files), 7, "expected 7 committed v0.3 fixtures")
        for f in files:
            with self.subTest(fixture=f):
                with open(os.path.join(fdir, f)) as fh:
                    payload = json.load(fh)
                contract = f[:-len(".json")]
                self.assertIn(contract, V.VALIDATORS_V0_3)
                V.VALIDATORS_V0_3[contract](payload)  # must not raise
                self.assertIn("synth", json.dumps(payload).lower(), "fixture must be clearly synthetic")


class TestV03CanonicalCandle(unittest.TestCase):
    def _schema(self):
        return ([{"name": n, "type": t, "nullable": False} for n, t, _ in V.CANON_CANDLE_MANDATORY]
                + [{"name": n, "type": t, "nullable": True} for n, t, _ in V.CANON_CANDLE_OPTIONAL])

    def test_parquet_schema_positive(self):
        V.validate_canonical_candle_parquet_schema(self._schema())

    def test_parquet_schema_unknown_column(self):
        s = self._schema() + [{"name": "surprise", "type": "BIGINT", "nullable": True}]
        with self.assertRaises(ContractError):
            V.validate_canonical_candle_parquet_schema(s)

    def test_parquet_schema_missing_mandatory(self):
        s = [c for c in self._schema() if c["name"] != "close"]
        with self.assertRaises(ContractError):
            V.validate_canonical_candle_parquet_schema(s)

    def test_parquet_schema_wrong_type(self):
        s = self._schema()
        s[2] = {**s[2], "type": "DOUBLE"}  # open as DOUBLE instead of DECIMAL(38,18)
        with self.assertRaises(ContractError):
            V.validate_canonical_candle_parquet_schema(s)

    def test_parquet_schema_mandatory_not_first(self):
        s = self._schema()
        s = [s[1], s[0]] + s[2:]  # swap event_start/event_end order
        with self.assertRaises(ContractError):
            V.validate_canonical_candle_parquet_schema(s)

    def test_candle_rows_positive(self):
        self.assertEqual(V.validate_canonical_candle_rows(F.synthetic_candle_rows(5)), 5)

    def test_candle_rows_duplicate_key(self):
        rows = F.synthetic_candle_rows(3)
        rows[2]["event_start_utc"] = rows[1]["event_start_utc"]
        rows[2]["event_end_utc"] = rows[1]["event_end_utc"]
        with self.assertRaises(ContractError):
            V.validate_canonical_candle_rows(rows)

    def test_candle_rows_bad_interval(self):
        rows = F.synthetic_candle_rows(2)
        rows[0]["event_end_utc"] = rows[0]["event_start_utc"] + _dt.timedelta(minutes=2)
        with self.assertRaises(ContractError):
            V.validate_canonical_candle_rows(rows)

    def test_candle_rows_ohlc_violation(self):
        rows = F.synthetic_candle_rows(1)
        rows[0]["high"] = Decimal("0.5")  # high < open/low
        with self.assertRaises(ContractError):
            V.validate_canonical_candle_rows(rows)

    def test_candle_rows_uint32_overflow(self):
        rows = F.synthetic_candle_rows(1)
        rows[0]["quality_flags"] = 2 ** 32
        with self.assertRaises(ContractError):
            V.validate_canonical_candle_rows(rows)

    def test_candle_rows_missing_mandatory(self):
        rows = F.synthetic_candle_rows(1)
        del rows[0]["close"]
        with self.assertRaises(ContractError):
            V.validate_canonical_candle_rows(rows)

    def test_candle_rows_non_finite_rejected_as_contract_error(self):
        # D1 regression: Infinity/NaN must fail CLOSED with a ContractError (never slip through, never raise
        # the uncaught decimal.InvalidOperation a fail-closed consumer would not catch).
        for bad in (Decimal("Infinity"), Decimal("-Infinity"), "Infinity", Decimal("NaN"), "NaN"):
            rows = F.synthetic_candle_rows(1)
            rows[0]["high"] = bad if "Inf" in str(bad) else rows[0]["high"]
            rows[0]["open"] = bad
            with self.subTest(bad=str(bad)):
                with self.assertRaises(ContractError):
                    V.validate_canonical_candle_rows(rows)

    def test_candle_rows_real_volume_non_finite_rejected(self):
        rows = F.synthetic_candle_rows(1)
        rows[0]["real_volume"] = Decimal("Infinity")
        with self.assertRaises(ContractError):
            V.validate_canonical_candle_rows(rows)

    def test_candle_rows_not_minute_aligned(self):
        rows = F.synthetic_candle_rows(2, start=_dt.datetime(2026, 1, 5, 0, 0, 30, tzinfo=_dt.timezone.utc))
        with self.assertRaises(ContractError):
            V.validate_canonical_candle_rows(rows)


class TestV03Signature(unittest.TestCase):
    def test_verify_and_tamper(self):
        mb = b"synthetic-manifest-bytes"
        from research.market_data.contracts import sha256_hex
        md = sha256_hex(mb)
        sig = F.signature_v2_for(mb, "rel_x")
        self.assertEqual(sig["manifest_digest"], md)
        self.assertTrue(V.verify_release_signature_v2(sig, mb))
        self.assertFalse(V.verify_release_signature_v2(sig, mb + b" "))  # tamper
        bad = {**sig, "signature_value": "0" * 64}
        self.assertFalse(V.verify_release_signature_v2(bad, mb))

    def test_producer_identity_is_bound(self):
        # G9 binds producer identity: altering it (without re-signing) must fail verification.
        mb = b"synthetic-manifest-bytes"
        sig = F.signature_v2_for(mb, "rel_x")
        self.assertTrue(V.verify_release_signature_v2(sig, mb))
        self.assertFalse(V.verify_release_signature_v2({**sig, "producer_identity": "someone_else"}, mb))


class TestV03PointInTime(unittest.TestCase):
    def _m(self):
        return F._native_manifest_v3()

    def test_source_side_admissibility(self):
        m = self._m()
        self.assertTrue(V.admissible_source_side(m, "2026-01-12T00:00:03Z"))
        self.assertFalse(V.admissible_source_side(m, "2026-01-12T00:00:01Z"))  # before availability

    def test_source_side_unknown_never_admissible(self):
        m = self._m()
        m["point_in_time"] = {**m["point_in_time"], "source_availability_classification": "UNKNOWN_RETROSPECTIVE",
                              "source_available_at_utc": None}
        self.assertFalse(V.admissible_source_side(m, "2027-01-01T00:00:00Z"))

    def test_published_admissibility(self):
        m = self._m()
        self.assertTrue(V.admissible_published(m, "2026-01-12T00:05:00Z", "2026-01-12T01:00:00Z", "2026-01-12T00:10:00Z"))
        self.assertFalse(V.admissible_published(m, "2026-01-12T00:02:59Z", "2026-01-12T01:00:00Z", "2026-01-12T00:10:00Z"))
        self.assertFalse(V.admissible_published(m, "2026-01-12T00:05:00Z", "2026-01-12T00:09:00Z", "2026-01-12T00:10:00Z"))

    def test_incomplete_or_empty_release_never_admissible(self):
        # D3 regression: an INCOMPLETE / empty release must never be reported research-admissible, regardless of
        # otherwise-valid timestamps.
        for mutate in ({"release_completeness": "INCOMPLETE"}, {"row_count": 0}):
            m = {**self._m(), **mutate}
            with self.subTest(mutate=mutate):
                self.assertFalse(V.admissible_source_side(m, "2027-01-01T00:00:00Z"))
                self.assertFalse(V.admissible_published(m, "2027-01-01T00:00:00Z", "2027-01-01T00:00:00Z",
                                                        "2026-01-12T00:10:00Z"))


class TestV03OfflineSmoke(unittest.TestCase):
    def test_v0_3_release_smoke(self):
        from tools import tim_release_v0_3_synthetic_smoke as smoke
        s = smoke.run_smoke()
        self.assertTrue(s["ok"] and s["synthetic"])
        self.assertEqual(s["bar_count"], 10080)
        self.assertEqual(s["canonical_format"], "parquet")
        for k in ("parquet_schema_verified", "signature_verified", "tamper_detected", "consume_signature_verified",
                  "all_file_hashes_match", "file_tamper_detected", "ready_written_last", "post_ready_add_blocked",
                  "receipt_outside_release", "pit_source_admissible", "pit_source_blocked_before_availability",
                  "pit_published_admissible", "pit_published_blocked_before_publication", "no_absolute_paths"):
            self.assertTrue(s[k], k)
        self.assertEqual(s["parquet_rows_verified"], 10080)
        self.assertEqual(s["import_state"], "ACCEPTED")
        self.assertEqual(s["evidence_roles_bound"], 8)
        self.assertIsNone(s["prohibited_key_found"])
        self.assertEqual(s["network"], "blocked")
        for k in ("nas", "mt5", "broker", "credentials", "production", "real_signing_key"):
            self.assertEqual(s[k], "none")


class TestV03NoNetwork(unittest.TestCase):
    def test_network_blocked_denies_sockets(self):
        with network_blocked():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with self.assertRaises(Exception):
                    s.connect(("127.0.0.1", 9))
            finally:
                s.close()


class TestV03Isolation(unittest.TestCase):
    _FORBIDDEN = re.compile(
        r"^\s*(?:import|from)\s+(socket|urllib|http|https|requests|httpx|ftplib|smtplib|"
        r"MetaTrader5|mt5|subprocess|paramiko|boto3)\b", re.MULTILINE)
    _MODULES = (
        "research/market_data/tim_contracts_v0_3.py",
        "research/market_data/tim_fixtures_v0_3.py",
        "tools/tim_release_v0_3_synthetic_smoke.py",
    )

    def test_no_network_or_broker_imports(self):
        for rel in self._MODULES:
            with open(os.path.join(REPO_ROOT, rel)) as fh:
                src = fh.read()
            m = self._FORBIDDEN.search(src)
            self.assertIsNone(m, f"{rel} imports a forbidden module: {m.group(1) if m else ''}")

    def test_no_reachable_order_or_trading_path(self):
        for rel in self._MODULES:
            with open(os.path.join(REPO_ROOT, rel)) as fh:
                src = fh.read().lower()
            for token in ("place_order", "open_trade", "execution_job", "order_transport", "allow_live"):
                self.assertNotIn(token, src, f"{rel} references a trading/order token: {token}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
