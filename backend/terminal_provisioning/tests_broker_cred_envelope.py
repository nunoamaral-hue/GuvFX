"""ADR-0027 — adversarial tests for broker-credential envelope encryption.

Proves: seal/open roundtrip; the backend cannot decrypt what it sealed; tamper, wrong-key, and
aad-rebind/replay all fail closed; key-id rotation; malformed input; env keyring wiring; and that no
plaintext/ciphertext/key ever reaches a log. Uses synthetic keys/passwords only.
"""
import base64
import io
import json
import logging

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from django.test import TestCase, override_settings

from terminal_provisioning import broker_cred_envelope as env

SECRET = b"s3cret-broker-pw!never-real"


def _keypair():
    priv = X25519PrivateKey.generate()
    return priv, priv.public_key()


def _aad(op="VALIDATE_LOGIN", uuid="66972e0e", corr="corr-1", nonce="n-1"):
    return env.bind_aad(operation=op, runtime_uuid=uuid, correlation_id=corr, nonce=nonce)


class EnvelopeCryptoTests(TestCase):
    def test_roundtrip(self):
        priv, pub = _keypair()
        aad = _aad()
        e = env.seal(SECRET, aad=aad, key_id="k1", recipient_public_key=pub)
        self.assertEqual(e["key_id"], "k1")
        self.assertEqual(e["v"], env.ENVELOPE_VERSION)
        self.assertEqual(env.open_envelope(e, aad=aad, recipient_private_key=priv), SECRET)

    def test_backend_cannot_decrypt_what_it_sealed(self):
        # ephemeral-static ECIES with a discarded ephemeral key: holding only the recipient PUBLIC key, the
        # sealer cannot re-derive the shared secret. There is no seal-side decrypt at all.
        priv, pub = _keypair()
        e = env.seal(SECRET, aad=_aad(), key_id="k1", recipient_public_key=pub)
        self.assertFalse(hasattr(env, "unseal_as_sender"))          # no such capability exists
        # even given the recipient PUBLIC key, open with a public key is impossible (needs the private key)
        with self.assertRaises(Exception):
            env.open_envelope(e, aad=_aad(), recipient_private_key=pub)  # pub is not a private key

    def test_wrong_recipient_key_fails_closed(self):
        _p1, pub1 = _keypair()
        priv2, _p2 = _keypair()
        e = env.seal(SECRET, aad=_aad(), key_id="k1", recipient_public_key=pub1)
        with self.assertRaises(env.EnvelopeError) as c:
            env.open_envelope(e, aad=_aad(), recipient_private_key=priv2)
        self.assertEqual(c.exception.reason_code, "envelope_auth_failed")

    def test_aad_rebind_replay_fails_closed(self):
        # a ciphertext lifted onto a DIFFERENT request (different runtime/op/correlation/nonce) fails AEAD auth
        priv, pub = _keypair()
        e = env.seal(SECRET, aad=_aad(nonce="n-1"), key_id="k1", recipient_public_key=pub)
        with self.assertRaises(env.EnvelopeError) as c:
            env.open_envelope(e, aad=_aad(nonce="n-2"), recipient_private_key=priv)   # replayed under new nonce
        self.assertEqual(c.exception.reason_code, "envelope_auth_failed")
        for changed in (_aad(op="STOP"), _aad(uuid="deadbeef"), _aad(corr="corr-2")):
            with self.assertRaises(env.EnvelopeError):
                env.open_envelope(e, aad=changed, recipient_private_key=priv)

    def test_ciphertext_tamper_fails_closed(self):
        priv, pub = _keypair()
        aad = _aad()
        e = env.seal(SECRET, aad=aad, key_id="k1", recipient_public_key=pub)
        ct = bytearray(base64.b64decode(e["ct"]))
        ct[0] ^= 0x01
        e2 = dict(e, ct=base64.b64encode(bytes(ct)).decode())
        with self.assertRaises(env.EnvelopeError) as c:
            env.open_envelope(e2, aad=aad, recipient_private_key=priv)
        self.assertEqual(c.exception.reason_code, "envelope_auth_failed")

    def test_malformed_envelope_fails_closed(self):
        priv, _pub = _keypair()
        for bad in ({}, {"v": 999}, {"v": env.ENVELOPE_VERSION, "key_id": ""},
                    {"v": env.ENVELOPE_VERSION, "key_id": "k1", "epk": "!!", "nonce": "x", "ct": "y"}):
            with self.assertRaises(env.EnvelopeError):
                env.open_envelope(bad, aad=_aad(), recipient_private_key=priv)

    def test_key_rotation_by_key_id(self):
        priv1, pub1 = _keypair(); priv2, pub2 = _keypair()
        aad = _aad()
        e1 = env.seal(SECRET, aad=aad, key_id="k1", recipient_public_key=pub1)
        e2 = env.seal(SECRET, aad=aad, key_id="k2", recipient_public_key=pub2)
        self.assertEqual(e1["key_id"], "k1"); self.assertEqual(e2["key_id"], "k2")
        self.assertEqual(env.open_envelope(e1, aad=aad, recipient_private_key=priv1), SECRET)
        self.assertEqual(env.open_envelope(e2, aad=aad, recipient_private_key=priv2), SECRET)
        # cross-key open fails closed
        with self.assertRaises(env.EnvelopeError):
            env.open_envelope(e1, aad=aad, recipient_private_key=priv2)

    def test_env_keyring_wiring_and_fail_closed(self):
        priv, pub = _keypair()
        pub_b64 = base64.b64encode(pub.public_bytes_raw()).decode()
        priv_b64 = base64.b64encode(priv.private_bytes_raw()).decode()
        with override_settings(BROKER_CRED_ENC_KEY_ID="k1",
                               BROKER_CRED_ENC_PUBKEYS=json.dumps({"k1": pub_b64}),
                               BROKER_CRED_ENC_PRIVKEYS=json.dumps({"k1": priv_b64})):
            self.assertTrue(env.backend_enc_configured())
            e = env.seal(SECRET, aad=_aad())                          # resolves pubkey from settings
            self.assertEqual(env.open_envelope(e, aad=_aad()), SECRET)  # resolves privkey from settings
        # unknown active key id -> not configured + seal fails closed
        with override_settings(BROKER_CRED_ENC_KEY_ID="missing", BROKER_CRED_ENC_PUBKEYS=json.dumps({"k1": pub_b64})):
            self.assertFalse(env.backend_enc_configured())
            with self.assertRaises(env.EnvelopeError) as c:
                env.seal(SECRET, aad=_aad())
            self.assertEqual(c.exception.reason_code, "envelope_unknown_key_id")
        # no key configured at all
        with override_settings(BROKER_CRED_ENC_KEY_ID="", BROKER_CRED_ENC_PUBKEYS=""):
            self.assertFalse(env.backend_enc_configured())
            with self.assertRaises(env.EnvelopeError):
                env.seal(SECRET, aad=_aad())

    def test_no_secret_or_key_material_is_logged(self):
        priv, pub = _keypair()
        buf = io.StringIO(); h = logging.StreamHandler(buf); root = logging.getLogger(); root.addHandler(h)
        try:
            e = env.seal(SECRET, aad=_aad(), key_id="k1", recipient_public_key=pub)
            env.open_envelope(e, aad=_aad(), recipient_private_key=priv)
            try:
                env.open_envelope(dict(e, ct="AAAA"), aad=_aad(), recipient_private_key=priv)
            except env.EnvelopeError:
                pass
        finally:
            root.removeHandler(h)
        logged = buf.getvalue()
        self.assertNotIn(SECRET.decode(), logged)
        self.assertNotIn(base64.b64encode(priv.private_bytes_raw()).decode(), logged)
