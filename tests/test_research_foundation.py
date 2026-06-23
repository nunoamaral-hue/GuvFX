"""Tests for the GFX-PKT-005B research foundation (R1: contract integrity).

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

        manifest = _load_schema("dataset_manifest")
        self.assertEqual(manifest["properties"]["schema_version"]["const"], "1.0")
        for field in (
            "dataset_id", "dataset_version", "created_at_utc", "record_type",
            "instrument_universe", "interval", "source_objects",
            "schema_versions", "code_commit", "config_hash",
            "point_in_time_policy", "row_count", "partition_count",
            "content_checksums", "quality_result", "artefact_location",
        ):
            self.assertIn(field, manifest["required"])

    def test_quote_and_bar_variants_are_distinct(self):
        obs = _load_schema("market_observation")
        branches = {b["if"]["properties"]["record_type"]["const"]: b["then"] for b in obs["allOf"]}
        self.assertEqual(set(branches["quote"]["required"]), {"bid", "ask"})
        self.assertEqual(set(branches["bar"]["required"]),
                         {"frequency", "open", "high", "low", "close"})

    def test_schema_rejects_cross_variant_fields(self):
        obs = _load_schema("market_observation")
        branches = {b["if"]["properties"]["record_type"]["const"]: b["then"] for b in obs["allOf"]}
        # Quote branch forbids every bar-only field (schema value `false`).
        for field in ("frequency", "open", "high", "low", "close", "volume", "volume_unit"):
            self.assertEqual(branches["quote"]["properties"].get(field), False)
        # Bar branch forbids every quote-only field.
        for field in ("bid", "ask", "bid_size", "ask_size"):
            self.assertEqual(branches["bar"]["properties"].get(field), False)
        # Root still rejects unknown fields.
        self.assertFalse(obs["additionalProperties"])

    def test_dataset_manifest_constraints_tightened(self):
        manifest = _load_schema("dataset_manifest")
        props = manifest["properties"]
        self.assertEqual(props["record_type"]["enum"], ["quote", "bar"])
        self.assertEqual(props["source_objects"]["minItems"], 1)
        self.assertEqual(props["content_checksums"]["minProperties"], 1)
        # config_hash constrained to the lowercase SHA-256 pattern via $ref.
        self.assertEqual(props["config_hash"].get("$ref"), "#/definitions/sha256_hex")
        self.assertEqual(manifest["definitions"]["sha256_hex"]["pattern"], "^[0-9a-f]{64}$")


class TestLocalValidator(unittest.TestCase):
    def _valid_quote(self):
        return copy.deepcopy(smoke.build_synthetic_quotes()[0])

    def _valid_bar(self):
        return copy.deepcopy(smoke.build_synthetic_bars()[0])

    def test_valid_records_pass(self):
        smoke.validate_observation(self._valid_quote())
        smoke.validate_observation(self._valid_bar())

    def test_unknown_field_rejected(self):
        q = self._valid_quote()
        q["mystery"] = "nope"
        with self.assertRaises(ValueError):
            smoke.validate_observation(q)

    def test_bar_only_field_on_quote_rejected(self):
        q = self._valid_quote()
        q["frequency"] = "M1"
        with self.assertRaises(ValueError):
            smoke.validate_observation(q)

    def test_quote_only_field_on_bar_rejected(self):
        b = self._valid_bar()
        b["bid"] = 1.1
        with self.assertRaises(ValueError):
            smoke.validate_observation(b)

    def test_non_string_quality_flag_rejected(self):
        q = self._valid_quote()
        q["quality_flags"] = ["ok", 123]
        with self.assertRaises(ValueError):
            smoke.validate_observation(q)

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

    def test_required_common_field_remains_mandatory(self):
        q = self._valid_quote()
        del q["instrument_id"]
        with self.assertRaises(ValueError):
            smoke.validate_observation(q)


class TestUtcSemantics(unittest.TestCase):
    """Point-in-time validation compares real UTC instants, not strings."""

    def _quote_with_times(self, obs, avail):
        q = copy.deepcopy(smoke.build_synthetic_quotes()[0])
        q["observation_time_utc"] = obs
        q["availability_time_utc"] = avail
        # Keep nullable source/received valid by mirroring observation.
        q["source_time_utc"] = obs
        q["received_time_utc"] = obs
        return q

    def test_parse_utc_returns_aware_datetime(self):
        dt = smoke._parse_utc("2026-01-02T00:00:00Z")
        self.assertIsNotNone(dt.tzinfo)
        self.assertEqual(dt.utcoffset().total_seconds(), 0)

    def test_invalid_calendar_date_rejected(self):
        with self.assertRaises(ValueError):
            smoke._parse_utc("2026-02-30T00:00:00Z")
        with self.assertRaises(ValueError):
            smoke.validate_observation(self._quote_with_times(
                "2026-02-30T00:00:00Z", "2026-02-30T00:00:00Z"))

    def test_invalid_time_rejected(self):
        with self.assertRaises(ValueError):
            smoke._parse_utc("2026-01-02T25:00:00Z")
        with self.assertRaises(ValueError):
            smoke.validate_observation(self._quote_with_times(
                "2026-01-02T25:00:00Z", "2026-01-02T25:00:00Z"))

    def test_later_fractional_availability_accepted(self):
        # observation whole-second, availability fractionally later -> valid.
        smoke.validate_observation(self._quote_with_times(
            "2026-01-02T00:00:00Z", "2026-01-02T00:00:00.1Z"))

    def test_earlier_whole_second_availability_rejected(self):
        # availability earlier than fractional observation -> invalid.
        with self.assertRaises(ValueError):
            smoke.validate_observation(self._quote_with_times(
                "2026-01-02T00:00:00.1Z", "2026-01-02T00:00:00Z"))

    def test_equal_instants_accepted(self):
        smoke.validate_observation(self._quote_with_times(
            "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z"))


class TestNullableRoundTrip(unittest.TestCase):
    """Nullable/omitted optional fields survive Parquet as null."""

    def _readback(self):
        import duckdb
        with tempfile.TemporaryDirectory(prefix="guvfx_test_null_") as tmp:
            smoke.run_smoke(output_dir=tmp)
            con = duckdb.connect(database=":memory:")
            try:
                qback = smoke.read_parquet_records(
                    con, os.path.join(tmp, smoke.QUOTE_PARQUET), smoke.QUOTE_COLUMNS)
                bback = smoke.read_parquet_records(
                    con, os.path.join(tmp, smoke.BAR_PARQUET), smoke.BAR_COLUMNS)
            finally:
                con.close()
        return qback, bback

    def test_null_timestamps_and_sizes_round_trip(self):
        qback, _ = self._readback()
        nq = {q["raw_object_id"]: q for q in qback}["synthetic_raw_quote_4"]
        self.assertIsNone(nq["source_time_utc"])
        self.assertIsNone(nq["received_time_utc"])
        self.assertIsNone(nq["bid_size"])
        self.assertIsNone(nq["ask_size"])
        # Required common fields are still populated.
        self.assertEqual(nq["instrument_id"], "EURUSD")
        self.assertIsNotNone(nq["observation_time_utc"])
        smoke.validate_observation(nq)

    def test_null_bar_volume_round_trip(self):
        _, bback = self._readback()
        nb = {b["raw_object_id"]: b for b in bback}["synthetic_raw_bar_4"]
        self.assertIsNone(nb["volume"])
        self.assertIsNone(nb["volume_unit"])
        self.assertIsNotNone(nb["close"])
        smoke.validate_observation(nb)

    def test_omitted_optional_inputs_do_not_raise(self):
        # The synthetic builders omit optional fields entirely; building + the
        # full round trip must materialise them as null without a KeyError.
        result = smoke.run_smoke()
        self.assertEqual(result["status"], "PASS")


class TestSmokeRoundTrip(unittest.TestCase):
    def test_full_field_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="guvfx_test_smoke_") as tmp:
            result = smoke.run_smoke(output_dir=tmp)
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(result["full_field_roundtrip_verified"])
            self.assertEqual(result["counts"]["quote_written"], result["counts"]["quote_read"])
            self.assertEqual(result["counts"]["bar_written"], result["counts"]["bar_read"])
            # Parquet preserves the entire contract surface, not a subset.
            self.assertEqual(set(result["columns"]["quote"]), set(smoke.QUOTE_COLUMNS))
            self.assertEqual(set(result["columns"]["bar"]), set(smoke.BAR_COLUMNS))
            for field in (
                "schema_version", "record_type", "broker_id", "account_type",
                "source_time_utc", "received_time_utc", "ingestion_time_utc",
                "quality_flags", "raw_object_id", "raw_object_sha256",
                "bid_size", "ask_size",
            ):
                self.assertIn(field, result["columns"]["quote"])
            for field in ("volume", "volume_unit", "frequency"):
                self.assertIn(field, result["columns"]["bar"])
            self.assertTrue(os.path.isfile(os.path.join(tmp, smoke.QUOTE_PARQUET)))
            self.assertTrue(os.path.isfile(os.path.join(tmp, smoke.BAR_PARQUET)))

    def test_readback_records_validate_and_match_source(self):
        import duckdb
        quotes = smoke.build_synthetic_quotes()
        bars = smoke.build_synthetic_bars()
        with tempfile.TemporaryDirectory(prefix="guvfx_test_rb_") as tmp:
            smoke.run_smoke(output_dir=tmp)
            con = duckdb.connect(database=":memory:")
            try:
                qback = smoke.read_parquet_records(
                    con, os.path.join(tmp, smoke.QUOTE_PARQUET), smoke.QUOTE_COLUMNS)
                bback = smoke.read_parquet_records(
                    con, os.path.join(tmp, smoke.BAR_PARQUET), smoke.BAR_COLUMNS)
            finally:
                con.close()
        # Every reconstructed record re-validates and round-trips losslessly.
        for rec in (*qback, *bback):
            smoke.validate_observation(rec)
        smoke._assert_records_match(quotes, qback, smoke.QUOTE_COLUMNS)
        smoke._assert_records_match(bars, bback, smoke.BAR_COLUMNS)
        # Nullable fields round-trip as null.
        self.assertIsNone(qback[0]["broker_id"])
        self.assertIsNone(qback[0]["account_type"])
        # quality_flags survives as a list/array.
        self.assertEqual(qback[0]["quality_flags"], ["synthetic"])

    def test_separate_manifests_are_correct(self):
        result = smoke.run_smoke()
        qm, bm = result["quote_manifest"], result["bar_manifest"]
        # Distinct deterministic dataset IDs.
        self.assertNotEqual(qm["dataset_id"], bm["dataset_id"])
        # Record type + interval semantics.
        self.assertEqual((qm["record_type"], qm["interval"]), ("quote", "event"))
        self.assertEqual((bm["record_type"], bm["interval"]), ("bar", "M1"))
        # Each manifest references only its own raw objects.
        q_raws = {s["source_object_id"] for s in qm["source_objects"]}
        b_raws = {s["source_object_id"] for s in bm["source_objects"]}
        self.assertTrue(all("quote" in r for r in q_raws))
        self.assertTrue(all("bar" in r for r in b_raws))
        self.assertEqual(q_raws & b_raws, set())
        # Each manifest's content checksum belongs to that dataset only.
        self.assertEqual(set(qm["content_checksums"]), {smoke.QUOTE_PARQUET})
        self.assertEqual(set(bm["content_checksums"]), {smoke.BAR_PARQUET})
        # Counts reflect only that dataset.
        self.assertEqual(qm["row_count"], result["counts"]["quote_read"])
        self.assertEqual(bm["row_count"], result["counts"]["bar_read"])
        self.assertEqual(qm["partition_count"], 1)
        self.assertEqual(bm["partition_count"], 1)
        # Both conform to the dataset_manifest required set, with hex config_hash.
        required = set(_load_schema("dataset_manifest")["required"])
        for m in (qm, bm):
            self.assertTrue(required.issubset(set(m.keys())))
            self.assertRegex(m["config_hash"], r"^[0-9a-f]{64}$")
            self.assertNotIn("/", m["artefact_location"].split("://")[0])

    def test_result_is_deterministic_excluding_checksums(self):
        def core(res):
            res = copy.deepcopy(res)
            res.pop("checksums", None)
            for key in ("quote_manifest", "bar_manifest"):
                res[key].pop("content_checksums", None)
            return res

        self.assertEqual(core(smoke.run_smoke()), core(smoke.run_smoke()))

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
