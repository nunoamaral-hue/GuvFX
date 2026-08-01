"""Customer Zero remediation — per-operation transport timeout selection + lease/timeout coupling.

Objective A: the single 20s transport timeout that killed the CZ MATERIALISE (a ~380MB golden copy) is
replaced by a per-operation, centrally-governed, CLAMPED read budget carried on a (connect, read) tuple so a
long MATERIALISE read never blocks a fast-fail on an unreachable agent. These are pure-function tests (no DB,
no host, no live agent)."""
import json
import os
from unittest import mock

from django.test import SimpleTestCase, override_settings

from terminal_provisioning import beta_worker, provisioner
from terminal_provisioning.mgmt_protocol import DEFAULT_TTL_SECONDS


class OpReadTimeoutTests(SimpleTestCase):
    def test_per_operation_defaults(self):
        self.assertEqual(beta_worker._op_read_timeout("NEGOTIATE"), 10)
        self.assertEqual(beta_worker._op_read_timeout("VERIFY"), 15)
        self.assertEqual(beta_worker._op_read_timeout("START"), 60)
        self.assertEqual(beta_worker._op_read_timeout("STOP"), 90)
        self.assertEqual(beta_worker._op_read_timeout("TOMBSTONE"), 120)
        self.assertEqual(beta_worker._op_read_timeout("RELEASE"), 30)
        self.assertEqual(beta_worker._op_read_timeout("MATERIALISE"), 300)

    def test_materialise_is_far_longer_than_negotiate(self):
        # The core regression: MATERIALISE must get a much longer read budget than the handshake, else a
        # legitimate long copy reads as a false timeout (the incident).
        self.assertGreater(beta_worker._op_read_timeout("MATERIALISE"),
                           10 * beta_worker._op_read_timeout("NEGOTIATE"))

    def test_unmapped_operation_uses_scalar_default(self):
        self.assertEqual(beta_worker._op_read_timeout("SOMETHING_NEW"), beta_worker.DEFAULT_TRANSPORT_TIMEOUT)
        self.assertEqual(beta_worker._op_read_timeout("", default=7), 7)

    @override_settings(BETA_AGENT_OP_TIMEOUTS={"MATERIALISE": 123, "NEGOTIATE": 3})
    def test_settings_override_is_honoured(self):
        self.assertEqual(beta_worker._op_read_timeout("MATERIALISE"), 123)
        self.assertEqual(beta_worker._op_read_timeout("NEGOTIATE"), 3)
        # an op absent from the override falls back to the per-op default
        self.assertEqual(beta_worker._op_read_timeout("VERIFY"), 15)

    @override_settings(BETA_AGENT_OP_TIMEOUTS={"MATERIALISE": 99999})
    def test_override_is_clamped_to_the_ceiling(self):
        self.assertEqual(beta_worker._op_read_timeout("MATERIALISE"), beta_worker.MAX_TRANSPORT_READ_TIMEOUT)

    def test_env_override_when_settings_absent(self):
        # settings has no BETA_AGENT_OP_TIMEOUTS by default in the test project; the env JSON is honoured.
        with mock.patch.dict(os.environ, {"BETA_AGENT_OP_TIMEOUTS": json.dumps({"MATERIALISE": 250})}):
            self.assertEqual(beta_worker._op_read_timeout("MATERIALISE"), 250)

    def test_malformed_override_fails_safe_to_the_PER_OP_default(self):
        # Both a malformed env JSON AND a malformed per-op VALUE must fall back to the PER-OP default (300 for
        # MATERIALISE) — never the 20s scalar, which would silently reinstate the incident's short timeout.
        with mock.patch.dict(os.environ, {"BETA_AGENT_OP_TIMEOUTS": "{not json"}):
            self.assertEqual(beta_worker._op_read_timeout("MATERIALISE"), 300)
        with override_settings(BETA_AGENT_OP_TIMEOUTS={"MATERIALISE": "oops"}):
            self.assertEqual(beta_worker._op_read_timeout("MATERIALISE"), 300)   # per-op default, NOT 20
        # an unmapped op with a malformed override still uses the scalar default (nothing better to fall to)
        with override_settings(BETA_AGENT_OP_TIMEOUTS={"WEIRD": "oops"}):
            self.assertEqual(beta_worker._op_read_timeout("WEIRD"), beta_worker.DEFAULT_TRANSPORT_TIMEOUT)


class TransportTimeoutTupleTests(SimpleTestCase):
    def _capture_post(self, operation):
        captured = {}

        class _Resp:
            @staticmethod
            def json():
                return {"outcome": "ok"}

        def fake_post(url, json=None, timeout=None):
            captured["url"], captured["json"], captured["timeout"] = url, json, timeout
            return _Resp()

        with mock.patch("requests.post", fake_post):
            transport = beta_worker.make_http_transport()
            req = {"operation": operation, "provisioning_job_id": 1, "signature": "sig", "nonce": "n"}
            transport("http://agent.invalid:8791", req)
        return captured, req

    def test_materialise_passes_connect_and_long_read_tuple(self):
        cap, req = self._capture_post("MATERIALISE")
        self.assertEqual(cap["timeout"], (beta_worker.CONNECT_TIMEOUT, 300))
        # the transport only READS operation; the signed body is passed through byte-for-byte
        self.assertIs(cap["json"], req)
        self.assertEqual(cap["json"]["signature"], "sig")
        self.assertEqual(cap["json"]["nonce"], "n")

    def test_negotiate_passes_short_read_even_though_materialise_is_long(self):
        cap, _ = self._capture_post("NEGOTIATE")
        self.assertEqual(cap["timeout"], (beta_worker.CONNECT_TIMEOUT, 10))

    def test_connect_timeout_is_bounded_and_short(self):
        self.assertLessEqual(beta_worker.CONNECT_TIMEOUT, 15)


class LeaseCouplingTests(SimpleTestCase):
    def test_lease_covers_materialise_read_plus_reconcile_budget(self):
        # Must not raise with the shipped defaults.
        provisioner.assert_lease_covers_op_timeouts()
        self.assertGreater(provisioner.LEASE_TTL_SECONDS,
                           beta_worker._op_read_timeout("MATERIALISE")
                           + provisioner.PROVISIONING_MATERIALISE_MAX_WAIT_SECONDS)

    @override_settings(BETA_AGENT_OP_TIMEOUTS={"MATERIALISE": 600},
                       PROVISIONING_MATERIALISE_MAX_WAIT_SECONDS=600)
    def test_broken_coupling_is_caught(self):
        # A future bump that makes read + reconcile budget meet/exceed the lease MUST fail the guard, so CI
        # catches it rather than shipping a job that can be re-claimed mid-flight.
        with self.assertRaises(AssertionError):
            provisioner.assert_lease_covers_op_timeouts()


class ReasonSetCouplingTests(SimpleTestCase):
    def test_only_runtime_busy_triggers_reconcile(self):
        # agent_busy (global semaphore, OTHER runtimes) and agent_stopping (drain) are raised BEFORE this op
        # runs, so they must NOT trigger the reconcile path (which on exhaustion quarantines) — only
        # runtime_busy is positive evidence THIS runtime's op is in flight.
        self.assertEqual(provisioner.RECONCILE_BUSY_REASONS, frozenset({"runtime_busy"}))
        self.assertNotIn("agent_busy", provisioner.RECONCILE_BUSY_REASONS)
        self.assertNotIn("agent_stopping", provisioner.RECONCILE_BUSY_REASONS)

    def test_partial_reasons_cover_the_live_pool_agent_integrity_codes(self):
        # The codes the deployed SLOT_POOL agent actually emits on MATERIALISE must be fail-closed+quarantined,
        # else a real containment/integrity refusal is silently retried and ends FAILED-without-quarantine.
        for code in ("reparse_escape", "slot_integrity_mismatch", "path_escape", "impl_integrity_mismatch",
                     "stage_copy_precheck_failed", "stage_copy_incomplete", "image_outside_slot",
                     "audit_chain_corrupt"):
            self.assertIn(code, provisioner.PARTIAL_REASONS)


class ProtocolTtlUntouchedTests(SimpleTestCase):
    def test_signing_ttl_is_not_a_transport_timeout(self):
        # Guard: the transport-timeout work must NOT have touched the signed-request expiry (replay window).
        self.assertEqual(DEFAULT_TTL_SECONDS, 30)
