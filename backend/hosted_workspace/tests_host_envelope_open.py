"""Stream 7C - tests for the hosted executor's host-side envelope opener (ADR-0027 sealed box, private half).

Seals a password exactly as the backend does (``broker_cred_envelope.seal`` with a key_id + the same AAD binding
as ``host_executor._default_seal_password``), then proves the host opener recovers it only with the matching
private key and matching request context - and fails closed otherwise.
"""
import base64
import json
import os
import sys
import unittest

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "hosted-executor")
_LIB = os.path.join(_BUNDLE, "lib")
for _p in (_BUNDLE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import envelope_open as eo  # noqa: E402
# The backend seal (production seal side); byte-compatible with the vendored open side.
from terminal_provisioning.broker_cred_envelope import bind_aad as backend_bind_aad, seal as backend_seal  # noqa: E402
from hosted_workspace.host_protocol import HostProtocolError  # noqa: E402

_ACCT = 14
_CORR = "corr-xyz"
_NONCE = "nonce-abc"


def _seal_password(pub, key_id, *, account_id=_ACCT, correlation_id=_CORR, nonce=_NONCE, pw=b"W1nPass!"):
    aad = backend_bind_aad(operation="PROVISION_IDENTITY", runtime_uuid=f"account:{account_id}",
                           correlation_id=correlation_id, nonce=nonce)
    return backend_seal(pw, aad=aad, key_id=key_id, recipient_public_key=pub)


class EnvelopeOpenTests(unittest.TestCase):
    def setUp(self):
        self.priv = X25519PrivateKey.generate()
        self.pub = self.priv.public_key()
        priv_b64 = base64.b64encode(self.priv.private_bytes_raw()).decode("ascii")
        self.privkeys_raw = json.dumps({"enc-1": priv_b64})

    def test_round_trip_recovers_password(self):
        env = _seal_password(self.pub, "enc-1")
        opener = eo.make_envelope_opener(self.privkeys_raw)
        pw = opener(env, account_id=_ACCT, correlation_id=_CORR, nonce=_NONCE)
        self.assertEqual(pw, b"W1nPass!")

    def test_wrong_account_id_fails_closed(self):
        env = _seal_password(self.pub, "enc-1")           # sealed for account 14
        opener = eo.make_envelope_opener(self.privkeys_raw)
        with self.assertRaises(HostProtocolError) as ctx:
            opener(env, account_id=999, correlation_id=_CORR, nonce=_NONCE)   # AAD mismatch
        self.assertEqual(ctx.exception.reason_code, "envelope_open_failed")

    def test_wrong_nonce_fails_closed(self):
        env = _seal_password(self.pub, "enc-1")
        opener = eo.make_envelope_opener(self.privkeys_raw)
        with self.assertRaises(HostProtocolError):
            opener(env, account_id=_ACCT, correlation_id=_CORR, nonce="other-nonce")

    def test_unknown_key_id_fails_closed(self):
        env = _seal_password(self.pub, "enc-2")           # sealed under a key id the host does not hold
        opener = eo.make_envelope_opener(self.privkeys_raw)
        with self.assertRaises(HostProtocolError) as ctx:
            opener(env, account_id=_ACCT, correlation_id=_CORR, nonce=_NONCE)
        self.assertEqual(ctx.exception.reason_code, "envelope_unknown_key_id")

    def test_wrong_private_key_fails_closed(self):
        env = _seal_password(self.pub, "enc-1")
        other = X25519PrivateKey.generate()
        other_raw = json.dumps({"enc-1": base64.b64encode(other.private_bytes_raw()).decode("ascii")})
        opener = eo.make_envelope_opener(other_raw)
        with self.assertRaises(HostProtocolError):
            opener(env, account_id=_ACCT, correlation_id=_CORR, nonce=_NONCE)

    def test_malformed_envelope_fails_closed(self):
        opener = eo.make_envelope_opener(self.privkeys_raw)
        with self.assertRaises(HostProtocolError):
            opener({"not": "an envelope"}, account_id=_ACCT, correlation_id=_CORR, nonce=_NONCE)

    def test_missing_key_id_in_envelope_fails_closed(self):
        env = _seal_password(self.pub, "enc-1")
        env["key_id"] = ""
        opener = eo.make_envelope_opener(self.privkeys_raw)
        with self.assertRaises(HostProtocolError) as ctx:
            opener(env, account_id=_ACCT, correlation_id=_CORR, nonce=_NONCE)
        self.assertEqual(ctx.exception.reason_code, "envelope_no_key_id")

    def test_malformed_privkeys_refused_at_construction(self):
        with self.assertRaises(ValueError):
            eo.make_envelope_opener("not json")
        with self.assertRaises(ValueError):
            eo.make_envelope_opener("{}")


if __name__ == "__main__":
    unittest.main()
