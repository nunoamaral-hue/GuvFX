"""Tests for the synthetic market-data foundation (GFX-PKT-006C).

Standard-library unittest + the pinned DuckDB (via the smoke). No network, no real
systems. Run with the research interpreter:

    .venv-research/bin/python -m unittest discover -s tests -p 'test_market_data_foundation.py' -v
"""

import copy
import glob
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from research.market_data import chunking, config, contracts, normalise, orchestrator, storage  # noqa: E402
from research.market_data import timezone as tzgate  # noqa: E402
from research.market_data.agent_client import (  # noqa: E402
    FixtureTransport, NetworkAgentClient, TransportError, network_blocked,
)
from research.market_data.contracts import ContractError, ProhibitedKeyError  # noqa: E402

CONTRACTS_DIR = os.path.join(REPO_ROOT, "research", "contracts")
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "market_data")
MD_DIR = os.path.join(REPO_ROOT, "research", "market_data")

SCHEMAS = [
    "agent_history_export_request_v1.schema.json",
    "agent_history_export_response_v1.schema.json",
    "raw_market_data_manifest_v1.schema.json",
    "broker_timezone_evidence_v1.schema.json",
]


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _fixture(name):
    return _load_json(os.path.join(FIXTURES, name))


def _fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


def _commit_args():
    return dict(code_commit="synthetic_test_only",
                acquired_at_utc="2026-01-01T00:00:00Z",
                received_at_utc="2026-01-01T00:00:00Z")


def _verified_evidence(request=None):
    """Deterministic VERIFIED evidence with an observation inside the coverage.

    The committed verified fixture intentionally violates the R2 observation
    coverage rule, so positive cases build evidence in-memory.
    """
    request = request or _fixture("synthetic_history_request.json")
    ev = {
        "schema_version": "1.0",
        "source_id": request["source_id"],
        "account_scope": request["account_scope"],
        "assessment_status": "VERIFIED",
        "evidence_method": "synthetic_clock_compare",
        "assessed_at_utc": "2025-01-01T00:00:00Z",
        "covered_start_utc": "2025-01-01T00:00:00Z",
        "covered_end_utc": "2025-02-01T00:00:00Z",
        "observations": [
            {"observed_at_utc": "2025-01-01T00:00:00Z", "server_clock_epoch_s": 1735689600,
             "utc_clock_epoch_s": 1735689600, "implied_offset_seconds": 0}
        ],
        "dst_behaviour": "synthetic_none",
        "limitations": ["synthetic_test_only"],
    }
    ev["evidence_fingerprint"] = tzgate.compute_evidence_fingerprint(ev)
    return ev


class TestSchemas(unittest.TestCase):
    def test_schemas_parse_and_are_strict(self):
        for name in SCHEMAS:
            schema = _load_json(os.path.join(CONTRACTS_DIR, name))
            self.assertEqual(schema.get("version"), "1.0")
            self.assertTrue(schema.get("$id"))
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft-07/schema#")
            self.assertFalse(schema.get("additionalProperties", True))

    def test_fixtures_parse(self):
        for name in (
            "synthetic_history_request.json", "synthetic_history_response.json",
            "synthetic_timezone_verified.json", "synthetic_timezone_inconclusive.json",
        ):
            self.assertIsInstance(_fixture(name), dict)


class TestRequestFingerprint(unittest.TestCase):
    def setUp(self):
        self.req = _fixture("synthetic_history_request.json")

    def test_deterministic(self):
        self.assertEqual(contracts.compute_request_id(self.req),
                         contracts.compute_request_id(copy.deepcopy(self.req)))
        self.assertEqual(self.req["request_id"], contracts.compute_request_id(self.req))

    def test_sensitive_to_each_field(self):
        base = contracts.compute_request_id(self.req)
        mutations = {
            "operation": "copy_rates_range_x",
            "source_id": "synthetic-other",
            "account_scope": "synthetic-other",
            "symbol": "GBPUSD",
            "timeframe": "M5",
            "representation": "ask_ohlc",
            "range_start_utc": "2025-01-02T00:00:00Z",
            "range_end_utc": "2025-03-01T00:00:00Z",
            "range_semantics": "(start,end)",
            "schema_version": "1.1",
        }
        for field, value in mutations.items():
            m = copy.deepcopy(self.req)
            m[field] = value
            self.assertNotEqual(contracts.compute_request_id(m), base, field)

    def test_validate_request_accepts_fixture(self):
        contracts.validate_request(self.req)

    def test_validate_request_rejects_all_digit_scope(self):
        m = copy.deepcopy(self.req)
        m["account_scope"] = "12345"
        m["request_id"] = contracts.compute_request_id(m)
        with self.assertRaises(ContractError):
            contracts.validate_request(m)


class TestDataRoot(unittest.TestCase):
    def test_unset_real_root_fails(self):
        with self.assertRaises(config.DataRootError):
            config.resolve_real_data_root(environ={})

    def test_blank_real_root_fails(self):
        with self.assertRaises(config.DataRootError):
            config.resolve_real_data_root(environ={"GUVFX_DATA_ROOT": "   "})

    def test_no_repository_fallback(self):
        with self.assertRaises(config.DataRootError):
            config.resolve_real_data_root(environ={"GUVFX_DATA_ROOT": str(config.REPO_ROOT)})
        sub = os.path.join(str(config.REPO_ROOT), "research")
        with self.assertRaises(config.DataRootError):
            config.resolve_real_data_root(environ={"GUVFX_DATA_ROOT": sub})

    def test_synthetic_root_rejects_repo_and_relative(self):
        with self.assertRaises(config.DataRootError):
            config.synthetic_data_root(str(config.REPO_ROOT))
        with self.assertRaises(config.DataRootError):
            config.synthetic_data_root("relative/path")

    def test_synthetic_root_accepts_tempdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolved = config.synthetic_data_root(tmp)
            self.assertEqual(resolved.mode, "synthetic")


class TestPathSafety(unittest.TestCase):
    def test_unsafe_components_rejected(self):
        for bad in ("", ".", "..", "a/b", "a\\b", "a b"):
            with self.assertRaises(storage.PathSafetyError):
                storage._safe_component(bad, "x", storage.SLUG_RE)

    def test_store_rejects_repo_root(self):
        with self.assertRaises(storage.PathSafetyError):
            storage.RawStore(config.REPO_ROOT)

    def test_resolve_under_root_blocks_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            with self.assertRaises(storage.PathSafetyError):
                store._resolve_under_root("..", "escape")


class TestChunking(unittest.TestCase):
    def test_full_year_2025_has_12_contiguous_chunks(self):
        chunks = chunking.monthly_chunks("2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        self.assertEqual(len(chunks), 12)
        self.assertEqual(chunks[0].start_utc, "2025-01-01T00:00:00Z")
        self.assertEqual(chunks[-1].end_utc, "2026-01-01T00:00:00Z")
        for a, b in zip(chunks, chunks[1:]):
            self.assertEqual(a.end_utc, b.start_utc)  # contiguous, no gap/overlap

    def test_january_single_chunk(self):
        chunks = chunking.monthly_chunks("2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z")
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0].start_utc, chunks[0].end_utc),
                         ("2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z"))

    def test_partial_months_preserved(self):
        chunks = chunking.monthly_chunks("2025-01-15T00:00:00Z", "2025-03-10T00:00:00Z")
        self.assertEqual([c.start_utc for c in chunks],
                         ["2025-01-15T00:00:00Z", "2025-02-01T00:00:00Z", "2025-03-01T00:00:00Z"])
        self.assertEqual(chunks[-1].end_utc, "2025-03-10T00:00:00Z")

    def test_reversed_range_fails(self):
        with self.assertRaises(ContractError):
            chunking.monthly_chunks("2025-02-01T00:00:00Z", "2025-01-01T00:00:00Z")


class TestNetworkBoundary(unittest.TestCase):
    def test_synthetic_acquire_makes_zero_network_calls(self):
        request = _fixture("synthetic_history_request.json")
        response_bytes = _fixture_bytes("synthetic_history_response.json")
        with network_blocked():
            # Prove the guard is active.
            with self.assertRaises(RuntimeError):
                socket.create_connection(("example.com", 80))
            with tempfile.TemporaryDirectory() as tmp:
                store = storage.RawStore(tmp)
                result = orchestrator.acquire(request, FixtureTransport(response_bytes),
                                              store, **_commit_args())
        self.assertEqual(result["status"], "ACCEPTED")

    def test_network_client_inert_without_permission(self):
        client = NetworkAgentClient("http://example.invalid:8787", "TOPSECRETVALUE")
        with self.assertRaises(TransportError):
            client.export_rates(b"{}")

    def test_token_redacted_in_repr(self):
        client = NetworkAgentClient("http://example.invalid:8787", "TOPSECRETVALUE")
        self.assertNotIn("TOPSECRETVALUE", repr(client))
        self.assertIn("<redacted>", repr(client))


class TestRawLandingAndIdempotency(unittest.TestCase):
    def setUp(self):
        self.request = _fixture("synthetic_history_request.json")
        self.response_bytes = _fixture_bytes("synthetic_history_response.json")

    def _acquire(self, store):
        return orchestrator.acquire(self.request, FixtureTransport(self.response_bytes),
                                    store, **_commit_args())

    def test_accepted_manifest_and_checksums(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            res = self._acquire(store)
            self.assertEqual(res["status"], "ACCEPTED")
            self.assertEqual(res["response_sha256"], contracts.sha256_hex(self.response_bytes))
            self.assertFalse(res["manifest_path"].startswith("/"))
            manifest = _load_json(os.path.join(tmp, res["manifest_path"]))
            self.assertEqual(manifest["response_sha256"], res["response_sha256"])
            self.assertEqual(manifest["object_state"], "ACCEPTED")
            self.assertEqual(manifest["timezone_status"], "NOT_EVALUATED")
            self.assertTrue(os.path.isfile(os.path.join(tmp, res["response_path"])))

    def test_identical_rerun_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            r1 = self._acquire(store)
            resp_path = os.path.join(tmp, r1["response_path"])
            before = Path(resp_path).read_bytes()
            r2 = self._acquire(store)
            self.assertEqual(r2["status"], "ALREADY_PRESENT")
            self.assertEqual(Path(resp_path).read_bytes(), before)

    def test_conflicting_bytes_quarantined_accepted_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            r1 = self._acquire(store)
            accepted = os.path.join(tmp, r1["response_path"])
            original = Path(accepted).read_bytes()
            # Same request_id, different response bytes.
            other = json.loads(self.response_bytes)
            other["bars"][0]["close"] = 1.99999
            other_bytes = json.dumps(other).encode("utf-8")
            request_bytes = contracts.canonical_json_bytes(self.request)
            res = store.land(self.request, other, request_bytes, other_bytes, **_commit_args())
            self.assertEqual(res["status"], "QUARANTINED_CONFLICT")
            self.assertEqual(Path(accepted).read_bytes(), original)  # unchanged

    def test_malformed_response_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            res = orchestrator.acquire(self.request, FixtureTransport(b"not json"),
                                       store, **_commit_args())
            self.assertEqual(res["object_state"], "QUARANTINED")

    def test_prohibited_key_stops_without_persisting_body(self):
        bad = json.loads(self.response_bytes)
        bad["source"]["token"] = "SUPERSECRET-TOKEN"
        bad_bytes = json.dumps(bad).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            with self.assertRaises(ProhibitedKeyError):
                orchestrator.acquire(self.request, FixtureTransport(bad_bytes),
                                     store, **_commit_args())
            # No accepted object exists.
            self.assertEqual(glob.glob(os.path.join(tmp, "raw", "**", "manifest.json"),
                                       recursive=True), [])
            # Security record exists but contains no body / no token value.
            sec = glob.glob(os.path.join(tmp, "quarantine", "security", "**", "security.json"),
                            recursive=True)
            self.assertTrue(sec)
            text = Path(sec[0]).read_text(encoding="utf-8")
            self.assertNotIn("SUPERSECRET-TOKEN", text)

    def test_incomplete_write_leaves_no_accepted_object(self):
        from unittest import mock
        original = storage.RawStore.__dict__["_write_exclusive"].__func__

        def failing(path, data):
            if str(path).endswith("manifest.json"):
                raise OSError("simulated mid-write failure")
            return original(path, data)

        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            with mock.patch.object(storage.RawStore, "_write_exclusive",
                                   staticmethod(failing)):
                with self.assertRaises(OSError):
                    self._acquire(store)
            self.assertEqual(glob.glob(os.path.join(tmp, "raw", "**", "manifest.json"),
                                       recursive=True), [])
            self.assertEqual(glob.glob(os.path.join(tmp, ".staging", "*")), [])


class TestTimezoneGate(unittest.TestCase):
    def setUp(self):
        self.request = _fixture("synthetic_history_request.json")
        self.response = _fixture("synthetic_history_response.json")
        self.verified = _verified_evidence(self.request)
        self.inconclusive = _fixture("synthetic_timezone_inconclusive.json")
        self.epochs = [b["time_epoch_s"] for b in self.response["bars"]]

    def _gate(self, evidence):
        tzgate.gate_for_normalisation(
            evidence, source_id=self.request["source_id"],
            account_scope=self.request["account_scope"], bar_epochs_s=self.epochs)

    def test_landing_allowed_with_inconclusive(self):
        # Raw landing does not depend on timezone status.
        with network_blocked():
            with tempfile.TemporaryDirectory() as tmp:
                store = storage.RawStore(tmp)
                res = orchestrator.acquire(
                    self.request, FixtureTransport(_fixture_bytes("synthetic_history_response.json")),
                    store, **_commit_args())
        self.assertEqual(res["status"], "ACCEPTED")

    def test_verified_permits_normalisation(self):
        self._gate(self.verified)  # no raise

    def test_committed_verified_fixture_violates_coverage(self):
        # The frozen fixture's observation is outside its covered interval; the R2
        # observation-coverage rule must reject it.
        with self.assertRaises(tzgate.TimezoneError):
            self._gate(_fixture("synthetic_timezone_verified.json"))

    def test_inconclusive_blocks(self):
        with self.assertRaises(tzgate.TimezoneError):
            self._gate(self.inconclusive)

    def test_conflict_status_blocks(self):
        ev = copy.deepcopy(self.verified)
        ev["assessment_status"] = "CONFLICT"
        ev["evidence_fingerprint"] = tzgate.compute_evidence_fingerprint(ev)
        with self.assertRaises(tzgate.TimezoneError):
            self._gate(ev)

    def test_source_mismatch_blocks(self):
        ev = copy.deepcopy(self.verified)
        ev["source_id"] = "synthetic-other"
        ev["evidence_fingerprint"] = tzgate.compute_evidence_fingerprint(ev)
        with self.assertRaises(tzgate.TimezoneError):
            self._gate(ev)

    def test_uncovered_interval_blocks(self):
        ev = copy.deepcopy(self.verified)
        ev["covered_end_utc"] = "2025-01-01T00:01:00Z"  # excludes later bars
        ev["evidence_fingerprint"] = tzgate.compute_evidence_fingerprint(ev)
        with self.assertRaises(tzgate.TimezoneError):
            self._gate(ev)

    def test_tampered_fingerprint_blocks(self):
        ev = copy.deepcopy(self.verified)
        ev["dst_behaviour"] = "tampered"  # fingerprint no longer matches
        with self.assertRaises(tzgate.TimezoneError):
            self._gate(ev)


class TestNoHardcodedOffset(unittest.TestCase):
    def test_no_utc_plus_two_or_offset_constant(self):
        for path in glob.glob(os.path.join(MD_DIR, "*.py")):
            text = Path(path).read_text(encoding="utf-8")
            self.assertNotIn("UTC+2", text)
            self.assertNotIn("+02:00", text)
            self.assertNotIn("7200", text)  # 2h in seconds


class TestGatedPublication(unittest.TestCase):
    def setUp(self):
        self.request = _fixture("synthetic_history_request.json")
        self.response_bytes = _fixture_bytes("synthetic_history_response.json")
        self.response = json.loads(self.response_bytes)
        self.verified = _verified_evidence(self.request)
        self.raw_id = self.request["request_id"]
        self.sha = contracts.sha256_hex(self.response_bytes)

    def _publish(self, evidence, **over):
        kw = dict(response_bytes=self.response_bytes, raw_object_id=self.raw_id,
                  response_sha256=self.sha, received_time_utc="2026-01-01T00:00:00Z",
                  ingestion_time_utc="2026-01-01T00:00:05Z", synthetic=True)
        kw.update(over)
        return normalise.publish_observations(self.request, self.response, evidence, **kw)

    def test_verified_publishes_m1_bid_ohlc_with_lineage(self):
        records = self._publish(self.verified)
        self.assertEqual(len(records), len(self.response["bars"]))
        for rec in records:
            self.assertEqual(rec["record_type"], "bar")
            self.assertEqual(rec["frequency"], "M1")
            self.assertIsNone(rec["volume"])
            self.assertIsNone(rec["volume_unit"])
            self.assertNotIn("bid", rec)
            self.assertNotIn("ask", rec)
            self.assertEqual(rec["raw_object_sha256"], self.sha)
            self.assertEqual(rec["raw_object_id"], self.raw_id)
            self.assertIn("bid_ohlc", rec["quality_flags"])
            self.assertIn("historical_backfill", rec["quality_flags"])

    def test_missing_evidence_raises(self):
        with self.assertRaises(normalise.PublicationError):
            self._publish(None)

    def test_inconclusive_evidence_raises(self):
        with self.assertRaises(tzgate.TimezoneError):
            self._publish(_fixture("synthetic_timezone_inconclusive.json"))

    def test_source_mismatch_raises(self):
        ev = copy.deepcopy(self.verified)
        ev["source_id"] = "synthetic-other"
        ev["evidence_fingerprint"] = tzgate.compute_evidence_fingerprint(ev)
        with self.assertRaises(tzgate.TimezoneError):
            self._publish(ev)

    def test_uncovered_bars_raise(self):
        ev = copy.deepcopy(self.verified)
        ev["covered_end_utc"] = "2025-01-01T00:01:00Z"  # excludes later bars
        ev["observations"] = []  # keep evidence otherwise valid
        ev["evidence_fingerprint"] = tzgate.compute_evidence_fingerprint(ev)
        with self.assertRaises(tzgate.TimezoneError):
            self._publish(ev)

    def test_invalid_response_raises_before_records(self):
        bad = copy.deepcopy(self.response)
        bad["bars"][1]["time_epoch_s"] = bad["bars"][0]["time_epoch_s"]  # duplicate
        bad_bytes = contracts.canonical_json_bytes(bad)
        with self.assertRaises(ContractError):
            normalise.publish_observations(
                self.request, bad, self.verified, response_bytes=bad_bytes,
                raw_object_id=self.raw_id, response_sha256=contracts.sha256_hex(bad_bytes),
                received_time_utc="2026-01-01T00:00:00Z",
                ingestion_time_utc="2026-01-01T00:00:05Z", synthetic=True)

    def test_no_output_path_created_on_any_gate_failure(self):
        cases = [None, _fixture("synthetic_timezone_inconclusive.json")]
        conflict = copy.deepcopy(self.verified)
        conflict["assessment_status"] = "CONFLICT"
        conflict["evidence_fingerprint"] = tzgate.compute_evidence_fingerprint(conflict)
        cases.append(conflict)
        for ev in cases:
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, "publish_out")
                with self.assertRaises((normalise.PublicationError, tzgate.TimezoneError)):
                    self._publish(ev)
                self.assertFalse(os.path.exists(out))

    # -- R3: exact raw-lineage binding --
    def test_wrong_raw_object_id_raises(self):
        with self.assertRaises(normalise.PublicationError):
            self._publish(self.verified, raw_object_id="f" * 64)

    def test_wrong_digest_raises(self):
        with self.assertRaises(normalise.PublicationError):
            self._publish(self.verified, response_sha256="0" * 64)

    def test_altered_bytes_raise(self):
        with self.assertRaises(normalise.PublicationError):
            self._publish(self.verified, response_bytes=self.response_bytes + b" ")

    def test_parsed_response_byte_mismatch_raises(self):
        other = copy.deepcopy(self.response)
        other["bars"][0]["close"] = 1.42
        with self.assertRaises(normalise.PublicationError):
            normalise.publish_observations(
                self.request, other, self.verified, response_bytes=self.response_bytes,
                raw_object_id=self.raw_id, response_sha256=self.sha,
                received_time_utc="2026-01-01T00:00:00Z",
                ingestion_time_utc="2026-01-01T00:00:05Z", synthetic=True)

    def test_non_bytes_response_raises(self):
        with self.assertRaises(normalise.PublicationError):
            self._publish(self.verified, response_bytes="not-bytes")

    def test_private_mapper_not_reexported(self):
        self.assertFalse(hasattr(normalise, "normalise_bid_ohlc"))
        self.assertTrue(hasattr(normalise, "_map_bid_ohlc"))  # private, present


class TestSmokeEndToEnd(unittest.TestCase):
    def test_smoke_pass_and_manifest_conforms(self):
        from tools import market_data_synthetic_smoke as smoke
        result = smoke.run_smoke()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["acquire_status"], "ACCEPTED")
        self.assertEqual(result["rerun_status"], "ALREADY_PRESENT")
        m = result["dataset_manifest"]
        required = set(_load_json(os.path.join(CONTRACTS_DIR, "dataset_manifest_v1.schema.json"))["required"])
        self.assertTrue(required.issubset(set(m.keys())))
        self.assertEqual((m["record_type"], m["interval"]), ("bar", "M1"))
        self.assertEqual(len(m["source_objects"]), 1)

    def test_smoke_deterministic_and_no_absolute_path(self):
        from tools import market_data_synthetic_smoke as smoke
        out1 = io.StringIO()
        with redirect_stdout(out1):
            smoke.main()
        out2 = io.StringIO()
        with redirect_stdout(out2):
            smoke.main()
        a, b = out1.getvalue(), out2.getvalue()
        self.assertEqual(a, b)  # byte-for-byte deterministic
        json.loads(a)
        for needle in (REPO_ROOT, os.path.expanduser("~"), "/Users/", "/home/",
                       tempfile.gettempdir()):
            self.assertNotIn(needle, a)

    def test_no_persistent_smoke_artefact(self):
        from tools import market_data_synthetic_smoke as smoke
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "guvfx_md_smoke_*")))
        smoke.run_smoke()
        after = set(glob.glob(os.path.join(tempfile.gettempdir(), "guvfx_md_smoke_*")))
        self.assertEqual(before, after)


class TestResearchFoundationStillGreen(unittest.TestCase):
    def test_research_smoke_still_passes(self):
        from tools import research_smoke
        self.assertEqual(research_smoke.run_smoke()["status"], "PASS")


class TestBackendSettingWiring(unittest.TestCase):
    def test_setting_line_has_no_default(self):
        # Authoritative runtime check is backend/core/tests.py (backend CI job);
        # here (research venv, no Django) we verify the wiring textually.
        src = Path(os.path.join(REPO_ROOT, "backend", "guvfx_backend", "settings.py")).read_text(encoding="utf-8")
        self.assertIn('GUVFX_DATA_ROOT = env("GUVFX_DATA_ROOT")', src)
        self.assertNotIn('GUVFX_DATA_ROOT = env("GUVFX_DATA_ROOT",', src)  # no default arg


class _FakeResp:
    def __init__(self, status=200, body=b'{"ok": true}', *, has_status=True,
                 getcode_exc=None, read_exc=None):
        if has_status:
            self.status = status
        self._status = status
        self._body = body
        self._getcode_exc = getcode_exc
        self._read_exc = read_exc
        self.closed = False

    def read(self, n=-1):
        if self._read_exc is not None:
            raise self._read_exc
        return self._body if (n is None or n < 0) else self._body[:n]

    def close(self):
        self.closed = True

    def getcode(self):
        if self._getcode_exc is not None:
            raise self._getcode_exc
        return self._status


class _FakeOpener:
    def __init__(self, resp=None, raises=None):
        self.resp = resp
        self.raises = raises
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append((request, timeout))
        if self.raises is not None:
            raise self.raises
        return self.resp


class TestHttpClient(unittest.TestCase):
    TOKEN = "TOPSECRETVALUE-do-not-log"
    URL = "http://agent.invalid:8787"

    def test_disabled_by_default_fails_before_open(self):
        opener = _FakeOpener(_FakeResp())
        client = NetworkAgentClient(self.URL, self.TOKEN, opener=opener)  # allow_network False
        with network_blocked():
            with self.assertRaises(TransportError):
                client.export_rates(b'{"x":1}')
        self.assertEqual(opener.calls, [])  # never opened

    def test_constructor_rejects_bad_inputs(self):
        for base, tok in [("", "t"), ("http://a", ""), ("ftp://a", "t"),
                          ("http://a?q=1", "t"), ("http://a#f", "t"), ("notaurl", "t")]:
            with self.assertRaises(TransportError):
                NetworkAgentClient(base, tok, allow_network=True)

    def test_post_contract_under_mock(self):
        resp = _FakeResp(status=200, body=b'{"ok": true}')
        opener = _FakeOpener(resp)
        client = NetworkAgentClient(self.URL, self.TOKEN, allow_network=True,
                                    timeout_s=12, opener=opener)
        body = b'{"request_id":"x"}'
        with network_blocked():
            out = client.export_rates(body)
        self.assertEqual(out, b'{"ok": true}')
        self.assertEqual(len(opener.calls), 1)  # one attempt, no retry
        req, timeout = opener.calls[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, self.URL + "/mt5/history/rates/export")
        self.assertEqual(req.data, body)  # exact bytes
        self.assertEqual(timeout, 12)
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(headers["X-guvfx-agent-token".lower()], self.TOKEN)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["accept"], "application/json")
        self.assertTrue(resp.closed)  # response closed

    def test_non_success_status_raises_without_body(self):
        resp = _FakeResp(status=500, body=b'{"secret-body":1}')
        opener = _FakeOpener(resp)
        client = NetworkAgentClient(self.URL, self.TOKEN, allow_network=True, opener=opener)
        with network_blocked():
            with self.assertRaises(TransportError) as ctx:
                client.export_rates(b'{}')
        self.assertNotIn("secret-body", str(ctx.exception))

    def test_byte_limit_exceeded_raises(self):
        resp = _FakeResp(status=200, body=b"x" * 50)
        opener = _FakeOpener(resp)
        client = NetworkAgentClient(self.URL, self.TOKEN, allow_network=True,
                                    max_response_bytes=4, opener=opener)
        with network_blocked():
            with self.assertRaises(TransportError):
                client.export_rates(b'{}')

    def test_http_error_redacted(self):
        import urllib.error
        opener = _FakeOpener(raises=urllib.error.HTTPError(self.URL, 503, "boom", {}, io.BytesIO(b"")))
        client = NetworkAgentClient(self.URL, self.TOKEN, allow_network=True, opener=opener)
        with network_blocked():
            with self.assertRaises(TransportError) as ctx:
                client.export_rates(b'{}')
        self.assertNotIn(self.TOKEN, str(ctx.exception))

    def test_url_error_redacted(self):
        import urllib.error
        opener = _FakeOpener(raises=urllib.error.URLError("dns boom"))
        client = NetworkAgentClient(self.URL, self.TOKEN, allow_network=True, opener=opener)
        with network_blocked():
            with self.assertRaises(TransportError) as ctx:
                client.export_rates(b'{}')
        self.assertNotIn(self.TOKEN, str(ctx.exception))

    def test_repr_redacts_token(self):
        client = NetworkAgentClient(self.URL, self.TOKEN, allow_network=True)
        self.assertNotIn(self.TOKEN, repr(client))
        self.assertIn("<redacted>", repr(client))


class TestStrictJsonAndBounds(unittest.TestCase):
    def test_strict_json_rejects_non_finite(self):
        for bad in (b'{"x": NaN}', b'{"x": Infinity}', b'{"x": -Infinity}'):
            with self.assertRaises(ContractError):
                contracts.strict_json_loads(bad)

    def test_strict_json_rejects_invalid(self):
        with self.assertRaises(ContractError):
            contracts.strict_json_loads(b'{not json')
        with self.assertRaises(ContractError):
            contracts.strict_json_loads(b'\xff\xfe not utf8')

    def test_non_finite_ohlc_rejected(self):
        resp = _fixture("synthetic_history_response.json")
        resp["bars"][0]["high"] = float("inf")
        with self.assertRaises(ContractError):
            contracts.validate_response(resp)

    def test_length_bounds(self):
        req = _fixture("synthetic_history_request.json")
        long_req = copy.deepcopy(req)
        long_req["source_id"] = "s" + "x" * 70
        long_req["request_id"] = contracts.compute_request_id(long_req)
        with self.assertRaises(ContractError):
            contracts.validate_request(long_req)

        resp = _fixture("synthetic_history_response.json")
        r2 = copy.deepcopy(resp)
        r2["source"]["broker_reported"] = "B" * 129
        with self.assertRaises(ContractError):
            contracts.validate_response(r2)

    def test_regex_type_guard_no_typeerror(self):
        req = _fixture("synthetic_history_request.json")
        req["source_id"] = 12345  # not a string
        with self.assertRaises(ContractError):
            contracts.validate_request(req)


class TestMalformedProhibitedKey(unittest.TestCase):
    def test_scan_distinguishes_key_from_value(self):
        self.assertTrue(contracts.scan_raw_for_prohibited_key_tokens(b'{"token": "x"}'))
        self.assertTrue(contracts.scan_raw_for_prohibited_key_tokens(b'{"token"  : "x"}'))
        self.assertTrue(contracts.scan_raw_for_prohibited_key_tokens(b'{"TOKEN": "x"}'))
        self.assertFalse(contracts.scan_raw_for_prohibited_key_tokens(b'{"note": "my token: y"}'))

    def test_malformed_with_secret_key_not_persisted(self):
        request = _fixture("synthetic_history_request.json")
        malformed = b'{"token": "SUPERSECRET-XYZ", broken'
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            with self.assertRaises(ProhibitedKeyError):
                orchestrator.acquire(request, FixtureTransport(malformed), store,
                                     **_commit_args())
            # No accepted/quarantine body persisted; security record exists; no value.
            self.assertEqual(glob.glob(os.path.join(tmp, "raw", "**", "*.json"),
                                       recursive=True), [])
            for path in glob.glob(os.path.join(tmp, "**", "*"), recursive=True):
                if os.path.isfile(path):
                    self.assertNotIn("SUPERSECRET-XYZ", Path(path).read_text(encoding="utf-8"))

    def test_malformed_without_secret_quarantines(self):
        request = _fixture("synthetic_history_request.json")
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            res = orchestrator.acquire(request, FixtureTransport(b"not json at all"),
                                       store, **_commit_args())
            self.assertEqual(res["object_state"], "QUARANTINED")


class TestIdempotencyExactness(unittest.TestCase):
    def setUp(self):
        self.request = _fixture("synthetic_history_request.json")
        self.response_bytes = _fixture_bytes("synthetic_history_response.json")

    def _acquire(self, store):
        return orchestrator.acquire(self.request, FixtureTransport(self.response_bytes),
                                    store, **_commit_args())

    def test_already_present_returns_stored_checksums(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            self._acquire(store)
            res = self._acquire(store)
            self.assertEqual(res["status"], "ALREADY_PRESENT")
            # Returned checksums match the files at the returned paths.
            self.assertEqual(
                res["response_sha256"],
                contracts.sha256_hex(Path(os.path.join(tmp, res["response_path"])).read_bytes()))
            self.assertEqual(
                res["request_sha256"],
                contracts.sha256_hex(Path(os.path.join(tmp, res["request_path"])).read_bytes()))

    def test_changed_request_bytes_quarantines_accepted_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            r1 = self._acquire(store)
            accepted = os.path.join(tmp, r1["response_path"])
            original = Path(accepted).read_bytes()
            canonical = contracts.canonical_json_bytes(self.request)
            noncanonical = canonical + b" "  # same object, different bytes
            res = store.land(self.request, json.loads(self.response_bytes), noncanonical,
                             self.response_bytes, **_commit_args())
            self.assertEqual(res["status"], "QUARANTINED_CONFLICT")
            self.assertEqual(Path(accepted).read_bytes(), original)

    def test_changed_response_quarantines_accepted_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            r1 = self._acquire(store)
            accepted = os.path.join(tmp, r1["response_path"])
            original = Path(accepted).read_bytes()
            other = json.loads(self.response_bytes)
            other["bars"][0]["close"] = 1.23456
            other_bytes = contracts.canonical_json_bytes(other)
            canonical = contracts.canonical_json_bytes(self.request)
            res = store.land(self.request, other, canonical, other_bytes, **_commit_args())
            self.assertEqual(res["status"], "QUARANTINED_CONFLICT")
            self.assertEqual(Path(accepted).read_bytes(), original)

    def test_distinct_request_bytes_distinct_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            self._acquire(store)
            canonical = contracts.canonical_json_bytes(self.request)
            r_a = store.land(self.request, json.loads(self.response_bytes), canonical + b" ",
                             self.response_bytes, **_commit_args())
            r_b = store.land(self.request, json.loads(self.response_bytes), canonical + b"  ",
                             self.response_bytes, **_commit_args())
            self.assertNotEqual(r_a["manifest_path"], r_b["manifest_path"])  # no aliasing

    def test_corrupt_accepted_manifest_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            r1 = self._acquire(store)
            manifest_file = os.path.join(tmp, r1["manifest_path"])
            with open(manifest_file, "w", encoding="utf-8") as fh:
                fh.write("{ corrupt")
            with self.assertRaises(storage.StorageError):
                self._acquire(store)


class TestTimezoneRuntimeBounds(unittest.TestCase):
    def _ev(self):
        return _verified_evidence()

    def test_source_type_error_no_typeerror(self):
        ev = self._ev()
        ev["source_id"] = 123
        ev["evidence_fingerprint"] = tzgate.compute_evidence_fingerprint(ev)
        with self.assertRaises(ContractError):
            tzgate.validate_timezone_evidence(ev)

    def test_overlength_method_rejected(self):
        ev = self._ev()
        ev["evidence_method"] = "m" * 257
        ev["evidence_fingerprint"] = tzgate.compute_evidence_fingerprint(ev)
        with self.assertRaises(ContractError):
            tzgate.validate_timezone_evidence(ev)

    def test_offset_arithmetic_mismatch_rejected(self):
        ev = self._ev()
        ev["observations"][0]["implied_offset_seconds"] = 3600  # but server==utc -> 0
        ev["evidence_fingerprint"] = tzgate.compute_evidence_fingerprint(ev)
        with self.assertRaises(ContractError):
            tzgate.validate_timezone_evidence(ev)

    def test_observation_outside_coverage_rejected(self):
        ev = self._ev()
        ev["observations"][0]["observed_at_utc"] = "2025-03-01T00:00:00Z"  # outside coverage
        ev["evidence_fingerprint"] = tzgate.compute_evidence_fingerprint(ev)
        with self.assertRaises(ContractError):
            tzgate.validate_timezone_evidence(ev)


class TestSchemaRelPath(unittest.TestCase):
    def test_rel_path_pattern_rejects_unsafe(self):
        import re
        schema = _load_json(os.path.join(CONTRACTS_DIR, "raw_market_data_manifest_v1.schema.json"))
        pattern = re.compile(schema["definitions"]["rel_path"]["pattern"])
        good = "raw/mt5/synthetic-mt5-agent/synthetic-demo/EURUSD/M1/2025/01/" + "a" * 64 + "/request.json"
        self.assertTrue(pattern.match(good))
        for bad in ("/abs/path", "a\\b", "a//b", "../escape", "a/../b", "./a",
                    ".hidden/x", "a/.hidden", ""):
            self.assertFalse(pattern.match(bad), bad)


class TestManifestVerification(unittest.TestCase):
    def _land(self, tmp):
        store = storage.RawStore(tmp)
        req = _fixture("synthetic_history_request.json")
        rb = _fixture_bytes("synthetic_history_response.json")
        res = orchestrator.acquire(req, FixtureTransport(rb), store, **_commit_args())
        return store, res, req

    def test_tampered_response_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, res, req = self._land(tmp)
            Path(os.path.join(tmp, res["response_path"])).write_bytes(b'{"tampered": true}')
            with self.assertRaises(storage.StorageError):
                orchestrator.acquire(req, FixtureTransport(_fixture_bytes("synthetic_history_response.json")),
                                     store, **_commit_args())

    def test_crafted_manifests_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, res, _ = self._land(tmp)
            acc_dir = Path(os.path.join(tmp, os.path.dirname(res["manifest_path"])))
            rel_dir = os.path.dirname(res["manifest_path"])
            rid = res["raw_object_id"]
            base = json.loads((acc_dir / "manifest.json").read_text("utf-8"))
            # Valid baseline passes.
            store._validate_manifest(base, files_dir=acc_dir, expected_rel_dir=rel_dir,
                                     expected_request_id=rid)

            def bad(mutate):
                m = copy.deepcopy(base)
                mutate(m)
                with self.assertRaises(storage.StorageError):
                    store._validate_manifest(m, files_dir=acc_dir, expected_rel_dir=rel_dir,
                                             expected_request_id=rid)

            bad(lambda m: m.update(extra="x"))                                   # extra field
            bad(lambda m: m.update(quarantine_reason="x"))                       # ACCEPTED + reason
            bad(lambda m: m.update(request_path="../escape/request.json"))       # traversal
            bad(lambda m: m.update(request_path="a\\b/request.json"))            # backslash
            bad(lambda m: m.update(request_path=rel_dir + "/../sibling/request.json"))  # sibling
            bad(lambda m: m.update(response_sha256="0" * 64))                    # checksum mismatch
            # identity mismatch (expected request id differs)
            with self.assertRaises(storage.StorageError):
                store._validate_manifest(base, files_dir=acc_dir, expected_rel_dir=rel_dir,
                                         expected_request_id="f" * 64)


class TestConcurrencyStaging(unittest.TestCase):
    def setUp(self):
        self.req = _fixture("synthetic_history_request.json")
        self.rb = _fixture_bytes("synthetic_history_response.json")

    def test_foreign_staging_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            staging_parent = Path(tmp) / ".staging"
            staging_parent.mkdir(parents=True, exist_ok=True)
            foreign = staging_parent / "foreign-attempt"
            foreign.mkdir()
            (foreign / "keep.txt").write_bytes(b"do-not-touch")
            orchestrator.acquire(self.req, FixtureTransport(self.rb), store, **_commit_args())
            self.assertTrue(foreign.exists())
            self.assertEqual((foreign / "keep.txt").read_bytes(), b"do-not-touch")

    def test_unique_staging_no_alias(self):
        # Two distinct objects both land without clobbering each other.
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            r1 = orchestrator.acquire(self.req, FixtureTransport(self.rb), store, **_commit_args())
            other_req = copy.deepcopy(self.req)
            other_req["range_start_utc"] = "2025-02-01T00:00:00Z"
            other_req["range_end_utc"] = "2025-03-01T00:00:00Z"
            other_req["request_id"] = contracts.compute_request_id(other_req)
            other_resp = json.loads(self.rb)
            other_resp["request_id"] = other_req["request_id"]
            other_resp["range_start_utc"] = "2025-02-01T00:00:00Z"
            other_resp["range_end_utc"] = "2025-03-01T00:00:00Z"
            base = 1738368000  # 2025-02-01T00:00:00Z
            for i, b in enumerate(other_resp["bars"]):
                b["time_epoch_s"] = base + i * 60
            other_bytes = contracts.canonical_json_bytes(other_resp)
            r2 = orchestrator.acquire(other_req, FixtureTransport(other_bytes), store, **_commit_args())
            self.assertEqual(r1["status"], "ACCEPTED")
            self.assertEqual(r2["status"], "ACCEPTED")
            self.assertNotEqual(r1["manifest_path"], r2["manifest_path"])

    def test_late_identical_race_resolves_already_present(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            real_replace = storage.os.replace
            state = {"raced": False}

            def racing_replace(src, dst):
                if not state["raced"]:
                    state["raced"] = True
                    real_replace(src, dst)            # a "winner" lands identical bytes
                    raise OSError("simulated late race")
                return real_replace(src, dst)

            with mock.patch.object(storage.os, "replace", racing_replace):
                res = orchestrator.acquire(self.req, FixtureTransport(self.rb), store, **_commit_args())
            self.assertEqual(res["status"], "ALREADY_PRESENT")

    def test_late_conflicting_winner_quarantines(self):
        # A pre-existing winner with different response bytes -> conflict, unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            store = storage.RawStore(tmp)
            r1 = orchestrator.acquire(self.req, FixtureTransport(self.rb), store, **_commit_args())
            accepted = Path(os.path.join(tmp, r1["response_path"]))
            original = accepted.read_bytes()
            other = json.loads(self.rb)
            other["bars"][0]["close"] = 1.42424
            res = store.land(self.req, other, contracts.canonical_json_bytes(self.req),
                             contracts.canonical_json_bytes(other), **_commit_args())
            self.assertEqual(res["status"], "QUARANTINED_CONFLICT")
            self.assertEqual(accepted.read_bytes(), original)


class TestHttpHardening(unittest.TestCase):
    URL = "http://agent.invalid:8787"
    TOKEN = "TOPSECRETVALUE-do-not-log"

    def test_invalid_max_bytes_rejected(self):
        for bad in (0, -1, True, "100"):
            with self.assertRaises(TransportError):
                NetworkAgentClient(self.URL, self.TOKEN, allow_network=True, max_response_bytes=bad)

    def test_userinfo_url_rejected(self):
        with self.assertRaises(TransportError):
            NetworkAgentClient("http://user:pass@agent.invalid:8787", self.TOKEN, allow_network=True)

    def test_getcode_failure_redacted_and_closed(self):
        resp = _FakeResp(has_status=False, getcode_exc=OSError("boom"))
        opener = _FakeOpener(resp)
        client = NetworkAgentClient(self.URL, self.TOKEN, allow_network=True, opener=opener)
        with network_blocked():
            with self.assertRaises(TransportError) as ctx:
                client.export_rates(b'{}')
        self.assertNotIn(self.TOKEN, str(ctx.exception))
        self.assertTrue(resp.closed)

    def test_read_failure_redacted_and_closed(self):
        resp = _FakeResp(status=200, read_exc=TimeoutError("slow"))
        opener = _FakeOpener(resp)
        client = NetworkAgentClient(self.URL, self.TOKEN, allow_network=True, opener=opener)
        with network_blocked():
            with self.assertRaises(TransportError) as ctx:
                client.export_rates(b'{}')
        self.assertNotIn(self.TOKEN, str(ctx.exception))
        self.assertTrue(resp.closed)


class TestTimezoneFractional(unittest.TestCase):
    REQ = {"source_id": "synthetic-mt5-agent", "account_scope": "synthetic-demo"}

    def _ev(self, covered_start, covered_end, obs_at, *, status="VERIFIED"):
        ev = {
            "schema_version": "1.0",
            "source_id": self.REQ["source_id"],
            "account_scope": self.REQ["account_scope"],
            "assessment_status": status,
            "evidence_method": "synthetic_clock_compare",
            "assessed_at_utc": "2025-01-01T00:00:00Z",
            "covered_start_utc": covered_start,
            "covered_end_utc": covered_end,
            "observations": [
                {"observed_at_utc": obs_at, "server_clock_epoch_s": 1735689600,
                 "utc_clock_epoch_s": 1735689600, "implied_offset_seconds": 0}
            ] if obs_at else [],
            "dst_behaviour": "synthetic_none",
            "limitations": ["synthetic_test_only"],
        }
        ev["evidence_fingerprint"] = tzgate.compute_evidence_fingerprint(ev)
        return ev

    def test_fractional_start_rejects_earlier_observation(self):
        ev = self._ev("2025-01-01T00:00:00.900Z", "2025-02-01T00:00:00Z",
                      "2025-01-01T00:00:00.100Z")
        with self.assertRaises(ContractError):
            tzgate.validate_timezone_evidence(ev)

    def test_fractional_start_rejects_integer_epoch_bar(self):
        # Valid evidence (observation at .900Z), but a bar at integer 00:00:00Z is
        # earlier than the fractional covered start and must be rejected.
        ev = self._ev("2025-01-01T00:00:00.900Z", "2025-02-01T00:00:00Z",
                      "2025-01-01T00:00:00.900Z")
        with self.assertRaises(tzgate.TimezoneError):
            tzgate.gate_for_normalisation(
                ev, source_id=self.REQ["source_id"],
                account_scope=self.REQ["account_scope"], bar_epochs_s=[1735689600])

    def test_fractional_instant_inside_coverage_passes(self):
        ev = self._ev("2025-01-01T00:00:00.900Z", "2025-02-01T00:00:00Z",
                      "2025-01-01T00:00:00.900Z")
        # A bar one second later is within coverage.
        tzgate.gate_for_normalisation(
            ev, source_id=self.REQ["source_id"],
            account_scope=self.REQ["account_scope"], bar_epochs_s=[1735689601])

    def test_equal_start_observation_passes(self):
        ev = self._ev("2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z",
                      "2025-01-01T00:00:00Z")
        tzgate.validate_timezone_evidence(ev)  # observation == start is allowed

    def test_just_before_end_passes_and_end_fails(self):
        ev = self._ev("2025-01-01T00:00:00Z", "2025-01-01T00:02:00Z",
                      "2025-01-01T00:00:00Z")
        # 00:01:00Z within; 00:02:00Z == end is excluded.
        tzgate.gate_for_normalisation(ev, source_id=self.REQ["source_id"],
                                      account_scope=self.REQ["account_scope"],
                                      bar_epochs_s=[1735689660])
        with self.assertRaises(tzgate.TimezoneError):
            tzgate.gate_for_normalisation(ev, source_id=self.REQ["source_id"],
                                          account_scope=self.REQ["account_scope"],
                                          bar_epochs_s=[1735689720])

    def test_impossible_instant_governed_error(self):
        ev = self._ev("2025-13-01T00:00:00Z", "2025-02-01T00:00:00Z", None)
        with self.assertRaises(ContractError):
            tzgate.validate_timezone_evidence(ev)


class TestManifestProvenance(unittest.TestCase):
    def _land(self, tmp):
        store = storage.RawStore(tmp)
        req = _fixture("synthetic_history_request.json")
        rb = _fixture_bytes("synthetic_history_response.json")
        res = orchestrator.acquire(req, FixtureTransport(rb), store, **_commit_args())
        acc_dir = Path(os.path.join(tmp, os.path.dirname(res["manifest_path"])))
        rel_dir = os.path.dirname(res["manifest_path"])
        base = json.loads((acc_dir / "manifest.json").read_text("utf-8"))
        return store, acc_dir, rel_dir, res["raw_object_id"], base

    def _bad(self, store, acc_dir, rel_dir, rid, mutate):
        m = copy.deepcopy(self._base)
        mutate(m)
        with self.assertRaises(storage.StorageError):
            store._validate_manifest(m, files_dir=acc_dir, expected_rel_dir=rel_dir,
                                     expected_request_id=rid)

    def test_field_and_date_mismatches_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, acc_dir, rel_dir, rid, base = self._land(tmp)
            self._base = base
            # Baseline valid.
            store._validate_manifest(base, files_dir=acc_dir, expected_rel_dir=rel_dir,
                                     expected_request_id=rid)
            b = lambda mut: self._bad(store, acc_dir, rel_dir, rid, mut)
            b(lambda m: m.update(source_id="synthetic-other"))      # source mismatch
            b(lambda m: m.update(symbol="GBPUSD"))                  # symbol mismatch
            b(lambda m: m.update(range_start_utc="2025-02-01T00:00:00Z"))  # range mismatch
            b(lambda m: m.update(range_start_utc="2025-13-01T00:00:00Z"))  # impossible date
            b(lambda m: m.update(range_start_utc="2025-02-01T00:00:00Z",
                                 range_end_utc="2025-01-01T00:00:00Z"))    # reversed
            b(lambda m: m.update(request_schema_id="https://guvfx.local/schema/wrong.json"))
            b(lambda m: m.update(timeframe="M5"))                   # not M1

    def test_wrong_expected_request_id_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, acc_dir, rel_dir, rid, base = self._land(tmp)
            with self.assertRaises(storage.StorageError):
                store._validate_manifest(base, files_dir=acc_dir, expected_rel_dir=rel_dir,
                                         expected_request_id="f" * 64)

    def test_noncanonical_stored_request_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, acc_dir, rel_dir, rid, base = self._land(tmp)
            # Replace the stored request bytes with a non-canonical (but valid-JSON)
            # encoding and realign the manifest checksum so only the canonical check
            # can catch it.
            req = _fixture("synthetic_history_request.json")
            noncanon = contracts.canonical_json_bytes(req) + b" "
            (acc_dir / "request.json").write_bytes(noncanon)
            m = copy.deepcopy(base)
            m["request_sha256"] = contracts.sha256_hex(noncanon)
            with self.assertRaises(storage.StorageError):
                store._validate_manifest(m, files_dir=acc_dir, expected_rel_dir=rel_dir,
                                         expected_request_id=rid)


if __name__ == "__main__":
    unittest.main()
