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
        self.verified = _fixture("synthetic_timezone_verified.json")
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


class TestNormalisation(unittest.TestCase):
    def setUp(self):
        self.response = _fixture("synthetic_history_response.json")

    def _records(self):
        return normalise.normalise_bid_ohlc(
            self.response, raw_object_id="a" * 64,
            response_sha256="b" * 64, received_time_utc="2026-01-01T00:00:00Z",
            ingestion_time_utc="2026-01-01T00:00:05Z", synthetic=True)

    def test_records_are_m1_bid_ohlc_only_with_lineage(self):
        records = self._records()
        self.assertEqual(len(records), len(self.response["bars"]))
        for rec in records:
            self.assertEqual(rec["record_type"], "bar")
            self.assertEqual(rec["frequency"], "M1")
            self.assertIsNone(rec["volume"])
            self.assertIsNone(rec["volume_unit"])
            self.assertNotIn("bid", rec)
            self.assertNotIn("ask", rec)
            self.assertEqual(rec["raw_object_sha256"], "b" * 64)
            self.assertIn("bid_ohlc", rec["quality_flags"])
            self.assertIn("historical_backfill", rec["quality_flags"])

    def test_duplicate_timestamp_fails(self):
        bad = copy.deepcopy(self.response)
        bad["bars"][1]["time_epoch_s"] = bad["bars"][0]["time_epoch_s"]
        with self.assertRaises(normalise.NormalisationError):
            normalise.normalise_bid_ohlc(bad, raw_object_id="a" * 64, response_sha256="b" * 64,
                                         received_time_utc="2026-01-01T00:00:00Z",
                                         ingestion_time_utc="2026-01-01T00:00:05Z", synthetic=True)

    def test_out_of_order_timestamp_fails(self):
        bad = copy.deepcopy(self.response)
        bad["bars"] = list(reversed(bad["bars"]))
        with self.assertRaises(normalise.NormalisationError):
            normalise.normalise_bid_ohlc(bad, raw_object_id="a" * 64, response_sha256="b" * 64,
                                         received_time_utc="2026-01-01T00:00:00Z",
                                         ingestion_time_utc="2026-01-01T00:00:05Z", synthetic=True)


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
    def __init__(self, status=200, body=b'{"ok": true}'):
        self.status = status
        self._body = body
        self.closed = False

    def read(self, n=-1):
        return self._body if (n is None or n < 0) else self._body[:n]

    def close(self):
        self.closed = True

    def getcode(self):
        return self.status


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


if __name__ == "__main__":
    unittest.main()
