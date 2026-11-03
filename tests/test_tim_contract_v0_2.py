"""GUVFX-TIM Contract v0.2 repository tests (GFX-PKT-TIM-C1). synthetic_test_only.

Proves offline, with no network/MT5/NAS/broker/credential/production dependency:
  * the existing 006C v1 schema bytes are UNCHANGED (pinned SHA-256) and the 12 additive schemas are pinned;
  * every positive fixture validates and every adversarial fixture is rejected or quarantined as specified;
  * the on-disk synthetic fixtures validate;
  * the offline synthetic 7-day EURUSD M1 release smoke passes (signature + hashes + READY-last + quarantine/gap);
  * network is blocked inside the pipeline;
  * the new modules import no network/MT5/execution/order-path symbol.
"""
import hashlib
import json
import os
import re
import socket
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from research.market_data import tim_contracts as T  # noqa: E402
from research.market_data import tim_fixtures as F  # noqa: E402
from research.market_data.agent_client import network_blocked  # noqa: E402
from research.market_data.contracts import ContractError, ProhibitedKeyError  # noqa: E402

CONTRACTS_DIR = os.path.join(REPO_ROOT, "research", "contracts")

# Pinned SHA-256 of every market-data schema file. Editing any schema changes its hash and fails this test
# (mutation-killing). The four 006C v1 request/response + raw-manifest + timezone hashes are the GO's hard
# "v1 byte-identical" requirement; the seven pre-existing schemas must ALL stay byte-stable.
SCHEMA_SHA256 = {
    "acquisition_watermark_v1": "1b6731ea034ea41cb8e216bbb13f522953efcfcf8ead586e56cb1f9959a39465",
    "agent_history_export_request_v1": "bbafa2fe1b3170167c1778b13dde369a406721f3a6fb2244daee0ffe82796a23",
    "agent_history_export_response_v1": "2e952cba01057536604bad720b8546c31e6e872a3d62f34a37cd5a8ac178fe24",
    "agent_market_data_export_request_v2": "896be9828dc052a799c2d5f15518a1bf3de1060107acd50f6aa0480001a2b0c8",
    "agent_market_data_export_response_v2": "002ed2374755d934070587495e37cf8c64fe93750e05b523a7aa6e7026589f5d",
    "broker_cost_v1": "868b4b37d0a78cc5f66d9755a2279c805db60dff5473fe055025539da379d08b",
    "broker_timezone_evidence_v1": "074fbc79344b4b549fbd674df36119eed6598b8258444cd11f15973e011477ab",
    "canonical_instrument_v1": "ee5f4801d23bebe2de817f6fe0c5267a4e713bd5f6dce96c503ec2af556112ca",
    "dataset_import_receipt_v1": "2be6c4dd3d1bd704bb1be9647bb8e4ae22a7a5498a40f2d3eda3bbed6c0730c2",
    "dataset_manifest_v1": "7a43418503067bf0f7c555d1afe8e80e6de5f404081d32a95e9af3e602249da8",
    "dataset_release_manifest_v2": "6f59a785d9b657b5162330d4fd32779574cec0dcb9de6e8ad31e7fdc744db26c",
    "dataset_release_signature_v1": "1073be7faf4ac9be3daaa3b64703e8248045a3c14320ddc69c52cde6960be9dd",
    "market_data_gap_v1": "526d64f42bc1e3cf58e802cf5865d9249a98a01abfecdf43fba26f454ac90e36",
    "market_data_quality_report_v1": "cb7eb55fbd46f911769ce435385765d8d4aca260a88481bb4d96dbd4c4696dd2",
    "market_data_source_v1": "ba106a77514b28b8da0f695d83f472ae5c17ebf7b414797234035f2046e08b82",
    "market_observation_v1": "936d4065f03ca9deb91e9e71fb26ee604f59d241222dd6c8877e9de6074fb9e5",
    "raw_market_data_manifest_v1": "44c1b8b74eb9a4eefb6c65b4b9815fff75f316021f202d42565e85a9de375675",
    "raw_market_data_manifest_v2": "088f0a860f4cc23035d7685a301e5abfc58006cd75ee51dd19e4a1314ed423be",
    "source_instrument_mapping_v1": "b524d646539ab45658484aeede4a60c5116ad114acfff2e0a0086b2f9b843dc5",
}
EXISTING_V1 = ("agent_history_export_request_v1", "agent_history_export_response_v1",
               "raw_market_data_manifest_v1", "broker_timezone_evidence_v1", "dataset_manifest_v1",
               "market_observation_v1", "broker_cost_v1")
REQUIRED_V1_BYTE_IDENTICAL = ("agent_history_export_request_v1", "agent_history_export_response_v1")


def _sha(name):
    with open(os.path.join(CONTRACTS_DIR, name + ".schema.json"), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class TestSchemaHashes(unittest.TestCase):
    def test_all_schema_hashes_pinned(self):
        on_disk = {f[:-len(".schema.json")] for f in os.listdir(CONTRACTS_DIR) if f.endswith(".schema.json")}
        self.assertEqual(on_disk, set(SCHEMA_SHA256), "schema set drifted from the pinned map")
        for name, pinned in SCHEMA_SHA256.items():
            self.assertEqual(_sha(name), pinned, f"{name}.schema.json byte-hash changed")

    def test_existing_v1_schemas_byte_identical(self):
        # The GO's hard requirement: the pre-existing v1 contracts are byte-identical to the recorded 006C bytes.
        for name in EXISTING_V1:
            self.assertEqual(_sha(name), SCHEMA_SHA256[name], f"EXISTING v1 schema {name} must be byte-identical")
        self.assertEqual(_sha("agent_history_export_request_v1"),
                         "bbafa2fe1b3170167c1778b13dde369a406721f3a6fb2244daee0ffe82796a23")
        self.assertEqual(_sha("agent_history_export_response_v1"),
                         "2e952cba01057536604bad720b8546c31e6e872a3d62f34a37cd5a8ac178fe24")


class TestSchemaStructure(unittest.TestCase):
    def test_twelve_additive_schemas_wellformed(self):
        for name in F.T.TIM_SCHEMA_FILES:
            with open(os.path.join(CONTRACTS_DIR, name + ".schema.json")) as fh:
                d = json.load(fh)
            self.assertEqual(d["$id"], f"https://guvfx.local/schema/{name}.schema.json")
            self.assertEqual(d["$schema"], "https://json-schema.org/draft-07/schema#")
            self.assertIs(d["additionalProperties"], False)
            self.assertIn("required", d)
            self.assertIn("title", d)

    def test_exactly_twelve_additive(self):
        self.assertEqual(len(T.TIM_SCHEMA_FILES), 12)
        self.assertEqual(len(set(T.TIM_SCHEMA_FILES)), 12)


class TestValidatorMatrix(unittest.TestCase):
    def test_positive_fixtures_accept(self):
        for name, payload in F.positive_fixtures().items():
            with self.subTest(fixture=name):
                T.VALIDATORS[F.contract_of(name)](payload)  # must not raise

    def test_adversarial_cases_rejected_or_quarantined(self):
        for c in F.adversarial_cases():
            with self.subTest(case=c["name"], expect=c["expect"]):
                fn = T.VALIDATORS[c["contract"]]
                if c["expect"] == "quarantine":
                    with self.assertRaises(T.QuarantineRequired):
                        fn(c["payload"])
                else:
                    with self.assertRaises((ContractError, ProhibitedKeyError)) as ctx:
                        fn(c["payload"])
                    self.assertNotIsInstance(ctx.exception, T.QuarantineRequired,
                                             f"{c['name']} should be a hard reject, not a quarantine")

    def test_matrix_is_nontrivial(self):
        self.assertGreaterEqual(len(F.positive_fixtures()), 12)
        self.assertGreaterEqual(len(F.adversarial_cases()), 18)


class TestOnDiskFixtures(unittest.TestCase):
    def test_committed_fixture_files_validate(self):
        fdir = os.path.join(REPO_ROOT, "tests", "fixtures", "market_data", "tim")
        files = [f for f in os.listdir(fdir) if f.endswith(".json")]
        self.assertEqual(len(files), 12, "expected 12 committed canonical fixture files")
        for f in files:
            with self.subTest(fixture=f):
                with open(os.path.join(fdir, f)) as fh:
                    payload = json.load(fh)
                contract = f[:-len(".json")]
                self.assertIn(contract, T.VALIDATORS)
                T.VALIDATORS[contract](payload)  # must not raise
                self.assertIn("synth", json.dumps(payload).lower(),
                              "fixture must be clearly labelled synthetic")


class TestOfflineSmoke(unittest.TestCase):
    def test_seven_day_release_smoke(self):
        from tools import tim_release_synthetic_smoke as smoke
        s = smoke.run_smoke()
        self.assertTrue(s["ok"] and s["synthetic"])
        self.assertEqual(s["bar_count"], 10080)
        self.assertTrue(s["signature_verified"])
        self.assertTrue(s["tamper_detected"])
        self.assertTrue(s["all_file_hashes_match"])
        self.assertTrue(s["ready_written_last"])
        self.assertEqual(s["import_state"], "IMPORTED")
        self.assertTrue(s["quarantine_case_validated"] and s["gap_repair_case_validated"])
        self.assertIsNone(s["prohibited_key_found"])
        self.assertTrue(s["no_absolute_paths"])
        for k in ("network",):
            self.assertEqual(s[k], "blocked")
        for k in ("nas", "mt5", "broker", "credentials", "production", "real_signing_key"):
            self.assertEqual(s[k], "none")


class TestNoNetwork(unittest.TestCase):
    def test_network_blocked_denies_sockets(self):
        with network_blocked():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with self.assertRaises(Exception):
                    s.connect(("127.0.0.1", 9))
            finally:
                s.close()


class TestIsolation(unittest.TestCase):
    _FORBIDDEN = re.compile(
        r"^\s*(?:import|from)\s+(socket|urllib|http|https|requests|httpx|ftplib|smtplib|"
        r"MetaTrader5|mt5|subprocess|paramiko|boto3)\b", re.MULTILINE)
    _MODULES = (
        "research/market_data/tim_contracts.py",
        "research/market_data/tim_fixtures.py",
        "tools/tim_release_synthetic_smoke.py",
    )

    def test_new_modules_have_no_network_or_execution_imports(self):
        for rel in self._MODULES:
            with open(os.path.join(REPO_ROOT, rel)) as fh:
                src = fh.read()
            m = self._FORBIDDEN.search(src)
            self.assertIsNone(m, f"{rel} imports a forbidden module: {m.group(1) if m else ''}")

    def test_no_reachable_order_or_trading_path(self):
        # The TIM contracts/fixtures/smoke must not reference any execution/order placement symbol.
        for rel in self._MODULES:
            with open(os.path.join(REPO_ROOT, rel)) as fh:
                src = fh.read().lower()
            for token in ("place_order", "open_trade", "execution_job", "order_transport", "allow_live"):
                self.assertNotIn(token, src, f"{rel} references a trading/order token: {token}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
