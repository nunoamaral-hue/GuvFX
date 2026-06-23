"""Tests for the GFX-PKT-005B research foundation.

Standard-library ``unittest`` plus the research venv's DuckDB package. No pandas,
PyArrow or Polars. Run with the research interpreter:

    .venv-research/bin/python -m unittest discover -s tests -p 'test_research_foundation.py' -v
"""

import copy
import glob
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

# Ensure the repository root (parent of tests/) is importable for ``tools``.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools import research_smoke as smoke  # noqa: E402

CONTRACTS_DIR = os.path.join(REPO_ROOT, "research", "contracts")
SCHEMA_FILES = {
    "market_observation": "market_observation_v1.schema.json",
    "broker_cost": "broker_cost_v1.schema.json",
    "dataset_manifest": "dataset_manifest_v1.schema.json",
}


def _load_schema(key):
    with open(os.path.join(CONTRACTS_DIR, SCHEMA_FILES[key]), encoding="utf-8") as fh:
        return json.load(fh)


class TestSchemasParse(unittest.TestCase):
    def test_all_three_schemas_parse(self):
        for key in SCHEMA_FILES:
            schema = _load_schema(key)
            self.assertIsInstance(schema, dict)
            self.assertIn("$id", schema)
            self.assertIn("title", schema)
            self.assertEqual(schema.get("version"), "1.0")

    def test_required_field_sets_and_schema_versions(self):
        obs = _load_schema("market_observation")
        self.assertEqual(obs["properties"]["schema_version"]["const"], "1.0")
        for field in (
            "schema_version", "record_type", "instrument_id", "source_id",
            "broker_id", "account_type", "observation_time_utc",
            "ingestion_time_utc", "availability_time_utc", "quality_flags",
            "raw_object_id", "raw_object_sha256",
        ):
            self.assertIn(field, obs["required"])

        cost = _load_schema("broker_cost")
        self.assertEqual(cost["properties"]["schema_version"]["const"], "1.0")
        for field in (
            "instrument_id", "broker_id", "account_type", "valid_from_utc",
            "commission_model", "commission_value", "commission_currency",
            "contract_size", "tick_size", "tick_value", "minimum_lot",
            "lot_step", "source_object_id", "source_object_sha256",
        ):
            self.assertIn(field, cost["required"])

        manifest = _load_schema("dataset_manifest")
        self.assertEqual(manifest["properties"]["schema_version"]["const"], "1.0")
        for field in (
            "dataset_id", "dataset_version", "created_at_utc",
            "instrument_universe", "interval", "source_objects",
            "schema_versions", "code_commit", "config_hash",
            "point_in_time_policy", "row_count", "partition_count",
            "content_checksums", "quality_result", "artefact_location",
        ):
            self.assertIn(field, manifest["required"])

    def test_quote_and_bar_variants_are_distinct(self):
        obs = _load_schema("market_observation")
        branches = obs["allOf"]
        required_sets = []
        for branch in branches:
            const = branch["if"]["properties"]["record_type"]["const"]
            required_sets.append((const, set(branch["then"]["required"])))
        as_dict = dict(required_sets)
        self.assertEqual(as_dict["quote"], {"bid", "ask"})
        self.assertEqual(as_dict["bar"], {"frequency", "open", "high", "low", "close"})
        self.assertNotEqual(as_dict["quote"], as_dict["bar"])


class TestLocalValidator(unittest.TestCase):
    def _valid_quote(self):
        return copy.deepcopy(smoke.build_synthetic_quotes()[0])

    def _valid_bar(self):
        return copy.deepcopy(smoke.build_synthetic_bars()[0])

    def test_valid_records_pass(self):
        smoke.validate_observation(self._valid_quote())
        smoke.validate_observation(self._valid_bar())

    def test_missing_raw_lineage_rejected(self):
        q = self._valid_quote()
        del q["raw_object_sha256"]
        with self.assertRaises(ValueError):
            smoke.validate_observation(q)

    def test_missing_timestamp_rejected(self):
        q = self._valid_quote()
        del q["observation_time_utc"]
        with self.assertRaises(ValueError):
            smoke.validate_observation(q)

    def test_non_z_timestamp_rejected(self):
        q = self._valid_quote()
        q["observation_time_utc"] = "2026-01-02 00:00:00"  # no T / no Z
        with self.assertRaises(ValueError):
            smoke.validate_observation(q)

    def test_bid_gt_ask_rejected(self):
        q = self._valid_quote()
        q["bid"], q["ask"] = q["ask"] + 1.0, q["ask"]
        with self.assertRaises(ValueError):
            smoke.validate_observation(q)

    def test_bar_high_low_inconsistency_rejected(self):
        b = self._valid_bar()
        b["high"], b["low"] = b["low"], b["high"]  # high < low
        with self.assertRaises(ValueError):
            smoke.validate_observation(b)

    def test_availability_before_observation_rejected(self):
        q = self._valid_quote()
        q["availability_time_utc"] = "2026-01-01T00:00:00Z"  # before observation
        with self.assertRaises(ValueError):
            smoke.validate_observation(q)


class TestSmokeRoundTrip(unittest.TestCase):
    def test_round_trip_writes_and_reads_parquet(self):
        with tempfile.TemporaryDirectory(prefix="guvfx_test_smoke_") as tmp:
            result = smoke.run_smoke(output_dir=tmp)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["counts"]["quote_written"], result["counts"]["quote_read"])
            self.assertEqual(result["counts"]["bar_written"], result["counts"]["bar_read"])
            self.assertGreater(result["counts"]["quote_read"], 0)
            self.assertGreater(result["counts"]["bar_read"], 0)
            # Parquet files were actually written.
            self.assertTrue(os.path.isfile(os.path.join(tmp, smoke.QUOTE_PARQUET)))
            self.assertTrue(os.path.isfile(os.path.join(tmp, smoke.BAR_PARQUET)))

    def test_result_is_deterministic_excluding_checksums(self):
        def core(res):
            res = copy.deepcopy(res)
            res.pop("checksums", None)
            res["manifest"].pop("content_checksums", None)
            return res

        r1 = smoke.run_smoke()
        r2 = smoke.run_smoke()
        self.assertEqual(core(r1), core(r2))

    def test_manifest_conforms_to_dataset_manifest_required_fields(self):
        manifest = smoke.run_smoke()["manifest"]
        required = set(_load_schema("dataset_manifest")["required"])
        self.assertTrue(required.issubset(set(manifest.keys())))
        self.assertEqual(manifest["schema_version"], "1.0")
        self.assertNotIn("/", manifest["artefact_location"].split("://")[0])

    def test_output_contains_no_personal_absolute_path(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            smoke.main()
        out = buf.getvalue()
        json.loads(out)  # valid JSON
        for needle in (REPO_ROOT, os.path.expanduser("~"), "/Users/", "/home/",
                       tempfile.gettempdir()):
            self.assertNotIn(needle, out)

    def test_no_heavy_dataframe_libs_required(self):
        # Importing the module and running the smoke must not require these.
        smoke.run_smoke()
        for mod in ("pandas", "pyarrow", "polars"):
            self.assertNotIn(mod, sys.modules)

    def test_no_persistent_file_after_temporary_run(self):
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "guvfx_research_smoke_*")))
        smoke.run_smoke()
        after = set(glob.glob(os.path.join(tempfile.gettempdir(), "guvfx_research_smoke_*")))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
