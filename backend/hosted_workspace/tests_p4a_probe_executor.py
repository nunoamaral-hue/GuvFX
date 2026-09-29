"""P4-a: SignedHostExecutor.probe_session() — the Django-side client for the DARK read-only PROBE_SESSION host-op.

Proves the executor method maps to the PROBE_SESSION op, surfaces the canonical session_status from the signed
response, refuses Customer Zero without a host call, and FAILS CLOSED (ok:false, NO session_status) on any
transport failure — so the P4 reconciler treats a missing status / ok:false as UNKNOWN, never ABSENT. No host.
"""
from unittest import mock

from django.test import SimpleTestCase

from hosted_workspace.host_executor import SignedHostExecutor
from hosted_workspace import host_protocol as P

KR = {"k1": "probe-exec-secret"}


def _executor(account_id=35, *, responder=None, reserved_ids=""):
    """Build a SignedHostExecutor whose transport is a fake that (by default) SIGNS a response echoing the
    request's correlation_id + nonce, so verify_hosted_response accepts it. ``responder(request) -> result``
    supplies the result dict; a responder that raises simulates a transport failure."""
    seen = {}

    def transport(base_url, request):
        seen["request"] = request
        result = (responder or (lambda req: {"ok": True, "session_status": "ABSENT"}))(request)
        return P.sign_hosted_response(result=result, correlation_id=request["correlation_id"],
                                      nonce=request["nonce"], keyring=KR, key_id="k1")

    ex = SignedHostExecutor(
        account_id=account_id, rdp_host="10.50.0.9", transport=transport, keyring=KR, key_id="k1",
        base_url="http://host.invalid", seal_password=lambda *a, **k: {}, reserved_ids=reserved_ids,
        correlation_id="corr-probe", clock=lambda: 1_000_000)
    return ex, seen


class ProbeSessionExecutorTests(SimpleTestCase):
    def test_maps_to_probe_session_op_and_surfaces_status(self):
        ex, seen = _executor(responder=lambda req: {"ok": True, "session_status": "DISCONNECTED",
                                                    "session_found": True, "session_id": 3})
        r = ex.probe_session()
        self.assertEqual(seen["request"]["operation"], "PROBE_SESSION")
        self.assertEqual(seen["request"]["account_id"], 35)
        self.assertEqual(seen["request"].get("params") or {}, {})   # no confinement/caller params
        self.assertTrue(r["ok"])
        self.assertEqual(r["session_status"], "DISCONNECTED")

    def test_each_status_flows_through(self):
        for status in ("ACTIVE", "DISCONNECTED", "ABSENT", "UNKNOWN"):
            ex, _ = _executor(responder=lambda req, s=status: {"ok": True, "session_status": s})
            self.assertEqual(ex.probe_session()["session_status"], status)

    def test_transport_failure_normalised_to_unknown(self):
        # A raising transport => host_unavailable, ok:false, and session_status GUARANTEED UNKNOWN (never ABSENT).
        def boom(base_url, request):
            raise OSError("connection refused")
        ex = SignedHostExecutor(
            account_id=35, rdp_host="10.50.0.9", transport=boom, keyring=KR, key_id="k1",
            base_url="http://host.invalid", seal_password=lambda *a, **k: {}, reserved_ids="",
            correlation_id="corr-probe", clock=lambda: 1_000_000)
        r = ex.probe_session()
        self.assertFalse(r["ok"])
        self.assertEqual(r.get("reason"), "host_unavailable")
        self.assertEqual(r["session_status"], "UNKNOWN")

    def test_ok_true_missing_status_normalised_to_unknown(self):
        # The dangerous case for a reconciler: a healthy-looking ok:true with NO status must become UNKNOWN, not
        # be read as ABSENT (which would let the reconciler create/tear down a session over a live one).
        ex, _ = _executor(responder=lambda req: {"ok": True})            # no session_status
        self.assertEqual(ex.probe_session()["session_status"], "UNKNOWN")

    def test_ok_true_out_of_enum_status_normalised_to_unknown(self):
        ex, _ = _executor(responder=lambda req: {"ok": True, "session_status": "WeirdState"})
        self.assertEqual(ex.probe_session()["session_status"], "UNKNOWN")

    def test_valid_status_passed_through_verbatim(self):
        # An ok:true result carrying an in-enum status is trusted unchanged (normalisation must not clobber it).
        ex, _ = _executor(responder=lambda req: {"ok": True, "session_status": "ACTIVE"})
        self.assertEqual(ex.probe_session()["session_status"], "ACTIVE")

    def test_customer_zero_refused_without_host_call(self):
        ex, seen = _executor(account_id=1, reserved_ids=None)   # None -> default reserved {1}
        r = ex.probe_session()
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "reserved_identity")
        self.assertEqual(r["session_status"], "UNKNOWN")   # refusal is fail-closed UNKNOWN too
        self.assertNotIn("request", seen)        # never reached the transport
