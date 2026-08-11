"""Beta Readiness Stream 5 — the signed host provisioning protocol (host_protocol).

Proves authentication, integrity, replay resistance, time-binding, the operation allow-list, params/payload
binding, and response authentication — the transport-security core of the signed host executor. Pure, no host.
"""
from django.test import SimpleTestCase

from hosted_workspace import host_protocol as P

KR = {"k1": "s3cr3t-one", "k2": "s3cr3t-two"}
T = 1_000_000


def _burner():
    seen = set()

    def burn(nonce, expiry):
        first = nonce not in seen
        seen.add(nonce)
        return first
    return burn


def _signed(op="VERIFY_SLOT", account_id=14, params=None, payload=None, now=T, ttl=30, key_id="k1"):
    return P.sign_hosted_request(account_id=account_id, operation=op, correlation_id="c1", keyring=KR,
                                 key_id=key_id, now=now, params=params, ttl_seconds=ttl, payload=payload)


class SignVerifyTests(SimpleTestCase):
    def test_round_trip(self):
        req = _signed()
        out = P.verify_hosted_request(req, keyring=KR, now=T, nonce_burn=_burner())
        self.assertEqual(out["account_id"], 14)
        self.assertEqual(out["operation"], "VERIFY_SLOT")

    def test_unknown_operation_refused(self):
        with self.assertRaises(P.HostProtocolError):
            P.sign_hosted_request(account_id=1, operation="RUN_SHELL", correlation_id="c", keyring=KR,
                                  key_id="k1", now=T)

    def test_unknown_key_id_refused(self):
        with self.assertRaises(P.HostProtocolError):
            _signed(key_id="nope")

    def test_bad_account_id_refused(self):
        for bad in (0, -1, "x", None):
            with self.assertRaises(P.HostProtocolError):
                _signed(account_id=bad)

    def test_tamper_operation_fails_signature(self):
        req = _signed(op="VERIFY_SLOT")
        req["operation"] = "PROVISION_IDENTITY"
        with self.assertRaises(P.HostProtocolError) as cm:
            P.verify_hosted_request(req, keyring=KR, now=T, nonce_burn=_burner())
        self.assertEqual(cm.exception.reason_code, "bad_signature")

    def test_tamper_account_id_fails_signature(self):
        req = _signed(account_id=14)
        req["account_id"] = 15
        with self.assertRaises(P.HostProtocolError):
            P.verify_hosted_request(req, keyring=KR, now=T, nonce_burn=_burner())

    def test_replay_rejected(self):
        req = _signed()
        burn = _burner()
        P.verify_hosted_request(req, keyring=KR, now=T, nonce_burn=burn)
        with self.assertRaises(P.HostProtocolError) as cm:
            P.verify_hosted_request(req, keyring=KR, now=T, nonce_burn=burn)
        self.assertEqual(cm.exception.reason_code, "nonce_replayed")

    def test_timestamp_skew_rejected(self):
        req = _signed(now=T)
        with self.assertRaises(P.HostProtocolError) as cm:
            P.verify_hosted_request(req, keyring=KR, now=T + 120, nonce_burn=_burner())
        self.assertEqual(cm.exception.reason_code, "timestamp_skew")

    def test_expired_rejected(self):
        req = _signed(now=T, ttl=10)
        with self.assertRaises(P.HostProtocolError) as cm:
            P.verify_hosted_request(req, keyring=KR, now=T + 11, nonce_burn=_burner())
        # skew is checked first; push now within skew but past expiry
        req2 = _signed(now=T, ttl=5)
        with self.assertRaises(P.HostProtocolError):
            P.verify_hosted_request(req2, keyring=KR, now=T + 6, nonce_burn=_burner())
        _ = cm

    def test_params_binding(self):
        req = _signed(op="APPLY_APPLOCKER_AUDIT", params={})
        req["params"] = {"mode": "Enforce"}          # smuggle a param after signing
        with self.assertRaises(P.HostProtocolError) as cm:
            P.verify_hosted_request(req, keyring=KR, now=T, nonce_burn=_burner())
        self.assertEqual(cm.exception.reason_code, "params_digest_mismatch")

    def test_credentialed_op_requires_payload(self):
        with self.assertRaises(P.HostProtocolError) as cm:
            _signed(op="PROVISION_IDENTITY", payload=None)
        self.assertEqual(cm.exception.reason_code, "payload_required")

    def test_payload_binding(self):
        req = _signed(op="PROVISION_IDENTITY", payload={"ct": "abc", "epk": "x"})
        req["payload"] = {"ct": "TAMPERED", "epk": "x"}
        with self.assertRaises(P.HostProtocolError) as cm:
            P.verify_hosted_request(req, keyring=KR, now=T, nonce_burn=_burner())
        self.assertEqual(cm.exception.reason_code, "payload_digest_mismatch")

    def test_missing_field_rejected(self):
        req = _signed()
        del req["nonce"]
        with self.assertRaises(P.HostProtocolError):
            P.verify_hosted_request(req, keyring=KR, now=T, nonce_burn=_burner())

    def test_verify_accepts_rotated_key(self):
        req = _signed(key_id="k2")
        out = P.verify_hosted_request(req, keyring=KR, now=T, nonce_burn=_burner())
        self.assertEqual(out["key_id"], "k2")


class ResponseAuthTests(SimpleTestCase):
    def test_response_round_trip(self):
        resp = P.sign_hosted_response(result={"ok": True, "rows": [1, 2]}, correlation_id="c1", nonce="n1",
                                      keyring=KR, key_id="k1")
        out = P.verify_hosted_response(resp, correlation_id="c1", nonce="n1", keyring=KR)
        self.assertTrue(out["ok"])

    def test_response_context_mismatch_rejected(self):
        resp = P.sign_hosted_response(result={"ok": True}, correlation_id="c1", nonce="n1", keyring=KR, key_id="k1")
        with self.assertRaises(P.HostProtocolError):
            P.verify_hosted_response(resp, correlation_id="cX", nonce="n1", keyring=KR)

    def test_forged_response_rejected(self):
        resp = P.sign_hosted_response(result={"ok": True, "protected": True}, correlation_id="c1", nonce="n1",
                                      keyring=KR, key_id="k1")
        resp["result"]["protected"] = False          # MITM flips a security-relevant field
        with self.assertRaises(P.HostProtocolError) as cm:
            P.verify_hosted_response(resp, correlation_id="c1", nonce="n1", keyring=KR)
        self.assertEqual(cm.exception.reason_code, "bad_response_signature")
