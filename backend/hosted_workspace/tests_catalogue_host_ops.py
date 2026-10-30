"""Broker Catalogue V3 — the ACCOUNT-INDEPENDENT host operations (STAGE/ATTEST_CATALOGUE_ARTEFACT).

Proves the protocol + dispatch contract for the two new shared-resource operations: the account_id==0 sentinel,
payload size/SHA binding for staging, slug/label/traversal validation, that they skip slot-derivation and the
reserved-identity guard, and that replay/expiry/signature protections carry over. Pure, no host, no DB.
"""
import base64
import hashlib

from django.test import SimpleTestCase

from hosted_workspace import host_protocol as P
from hosted_workspace.host_agent_dispatch import dispatch

KR = {"k1": "catalogue-hmac-secret-value-01"}
T = 2_000_000


def _burner():
    seen = set()

    def burn(nonce, expiry):
        first = nonce not in seen
        seen.add(nonce)
        return first
    return burn


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stage_req(*, data=b"SERVERSDAT-BYTES", broker="tradersway", label="v3", size=None, sha=None,
               account_id=0, nonce="n-stage-1", now=T):
    size = len(data) if size is None else size
    sha = _sha(data) if sha is None else sha
    return P.sign_hosted_request(
        account_id=account_id, operation="STAGE_CATALOGUE_ARTEFACT", correlation_id="c1", keyring=KR,
        key_id="k1", now=now, nonce=nonce,
        params={"broker_id": broker, "label": label, "expected_sha256": sha, "size_bytes": size},
        payload={"artefact_b64": _b64(data)})


def _attest_req(*, broker="tradersway", label="v3", sha="", account_id=0, nonce="n-att-1", now=T):
    return P.sign_hosted_request(
        account_id=account_id, operation="ATTEST_CATALOGUE_ARTEFACT", correlation_id="c1", keyring=KR,
        key_id="k1", now=now, nonce=nonce,
        params={"broker_id": broker, "label": label, "expected_sha256": sha})


class _Capture:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"ok": True}

    def __call__(self, primitive, args):
        self.calls.append((primitive, dict(args)))
        return dict(self.result)


def _dispatch(req, cap, now=T, burn=None):
    return dispatch(req, keyring=KR, now=now, nonce_burn=burn or _burner(), run_primitive=cap)


class StageOpTests(SimpleTestCase):
    def test_stage_round_trip_derives_primitive_and_args(self):
        data = b"TRADERSWAY-servers-dat"
        cap = _Capture({"ok": True, "staged_sha256": _sha(data)})
        resp = _dispatch(_stage_req(data=data), cap)
        self.assertEqual(len(cap.calls), 1)
        prim, args = cap.calls[0]
        self.assertEqual(prim, "stage_catalogue_artefact")
        self.assertEqual(args["broker_id"], "tradersway")
        self.assertEqual(args["label"], "v3")
        self.assertEqual(args["expected_sha256"], _sha(data))
        self.assertEqual(args["size_bytes"], len(data))
        self.assertEqual(base64.b64decode(args["artefact_b64"]), data)   # bytes carried, decoded host-side
        out = P.verify_hosted_response(resp, correlation_id="c1", nonce="n-stage-1", keyring=KR)
        self.assertTrue(out["ok"])

    def test_stage_requires_account_id_zero_sign_side(self):
        with self.assertRaises(P.HostProtocolError):
            _stage_req(account_id=14)

    def test_stage_payload_sha_mismatch_refused(self):
        req = _stage_req(data=b"AAAA", sha=_sha(b"BBBB"))     # declared sha != payload bytes
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(req, _Capture())
        self.assertEqual(cm.exception.reason_code, "payload_sha_mismatch")

    def test_stage_payload_size_mismatch_refused(self):
        req = _stage_req(data=b"AAAA", size=9999)
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(req, _Capture())
        self.assertEqual(cm.exception.reason_code, "payload_size_mismatch")

    def test_stage_bad_broker_slug_refused(self):
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(_stage_req(broker="Traders-Way"), _Capture())    # uppercase + dash not in slug charset
        self.assertEqual(cm.exception.reason_code, "params_malformed")

    def test_stage_traversal_label_refused(self):
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(_stage_req(label="../evil"), _Capture())
        self.assertEqual(cm.exception.reason_code, "params_malformed")

    def test_stage_dotdot_label_refused(self):
        # ".." matches the charset regex but must be rejected at the dispatcher (defence in depth over the .ps1).
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(_stage_req(label=".."), _Capture())
        self.assertEqual(cm.exception.reason_code, "params_malformed")

    def test_stage_dot_edge_labels_refused(self):
        for bad in (".", "v3.", ".v3", "-v3"):
            with self.assertRaises(P.HostProtocolError) as cm:
                _dispatch(_stage_req(label=bad), _Capture())
            self.assertEqual(cm.exception.reason_code, "params_malformed", bad)

    def test_stage_oversize_declared_refused(self):
        big = 2_000_001
        # Build bytes matching the oversize so size/sha pass their own checks; the size ceiling must still refuse.
        data = b"x" * 16
        req = P.sign_hosted_request(
            account_id=0, operation="STAGE_CATALOGUE_ARTEFACT", correlation_id="c1", keyring=KR, key_id="k1",
            now=T, nonce="n-big", params={"broker_id": "tradersway", "label": "v3",
                                          "expected_sha256": _sha(data), "size_bytes": big},
            payload={"artefact_b64": _b64(data)})
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(req, _Capture())
        self.assertEqual(cm.exception.reason_code, "params_malformed")

    def test_stage_replay_refused(self):
        burn = _burner()
        _dispatch(_stage_req(nonce="dup"), _Capture(), burn=burn)
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(_stage_req(nonce="dup"), _Capture(), burn=burn)
        self.assertEqual(cm.exception.reason_code, "nonce_replayed")

    def test_stage_expired_refused(self):
        req = _stage_req(now=T - 10_000)
        with self.assertRaises(P.HostProtocolError):
            _dispatch(req, _Capture(), now=T)

    def test_stage_payload_tamper_fails_digest(self):
        req = _stage_req(data=b"ORIGINAL")
        req["payload"] = {"artefact_b64": _b64(b"SWAPPED-PAYLOAD")}   # swap the opaque payload after signing
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(req, _Capture())
        self.assertEqual(cm.exception.reason_code, "payload_digest_mismatch")


class AttestOpTests(SimpleTestCase):
    def test_attest_artefact_round_trip(self):
        cap = _Capture({"ok": True, "verified_sha256": "a" * 64})
        resp = _dispatch(_attest_req(sha="a" * 64), cap)
        prim, args = cap.calls[0]
        self.assertEqual(prim, "attest_catalogue_artefact")
        self.assertEqual(args["broker_id"], "tradersway")
        self.assertEqual(args["label"], "v3")
        out = P.verify_hosted_response(resp, correlation_id="c1", nonce="n-att-1", keyring=KR)
        self.assertEqual(out["verified_sha256"], "a" * 64)

    def test_attest_inventory_mode_empty_broker(self):
        cap = _Capture({"ok": True, "brokers": []})
        _dispatch(_attest_req(broker="", sha=""), cap)
        _, args = cap.calls[0]
        self.assertEqual(args["broker_id"], "")        # inventory mode

    def test_attest_requires_account_id_zero(self):
        with self.assertRaises(P.HostProtocolError):
            _attest_req(account_id=7)

    def test_attest_bad_label_refused(self):
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(_attest_req(label="v3/../etc"), _Capture())
        self.assertEqual(cm.exception.reason_code, "params_malformed")


class AccountIndependenceTests(SimpleTestCase):
    def test_account_ops_still_require_positive_account_id(self):
        for bad in (0, -1):
            with self.assertRaises(P.HostProtocolError):
                P.sign_hosted_request(account_id=bad, operation="VERIFY_SLOT", correlation_id="c", keyring=KR,
                                      key_id="k1", now=T)

    def test_catalogue_op_skips_reserved_identity_guard(self):
        # account_id==0 is not a real account; the reserved-id (Customer-Zero) guard must NOT apply to it, and the
        # op must dispatch to its catalogue primitive rather than deriving a slot.
        cap = _Capture({"ok": True})
        _dispatch(_attest_req(), cap, burn=_burner())
        self.assertEqual(cap.calls[0][0], "attest_catalogue_artefact")

    def test_op_primitives_cover_operations(self):
        from hosted_workspace.host_agent_dispatch import OP_PRIMITIVES
        self.assertEqual(set(OP_PRIMITIVES), set(P.HOSTED_OPERATIONS))
        for op in P.ACCOUNT_INDEPENDENT_OPERATIONS:
            self.assertIn(op, OP_PRIMITIVES)
