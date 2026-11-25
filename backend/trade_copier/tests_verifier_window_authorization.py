"""Synthetic-key tests for the host-private / backend-public verifier-window-open authorization contract
(GFX-172, ADR-0129). An Ed25519 keypair is generated in-test; the host-side (private) signs a bounded
attestation and the backend-side (pinned public) verifies it. No real key, no host, no network.
"""
from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from trade_copier import verifier_window_authorization as vwa

NOW = 1_800_000_000
OWNER, MASTER, FOLLOWER = 29, 46, 48


def _raw_pub(priv) -> bytes:
    return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _att(**over) -> vwa.WindowOpenAttestation:
    kw = dict(key_id="ct-win-1", owner_id=OWNER, master_account_id=MASTER, follower_account_id=FOLLOWER,
              purpose=vwa.PURPOSE_INGEST, issued_at_epoch=NOW, window_not_after_epoch=NOW + 1000, nonce="nonce-1")
    kw.update(over)
    return vwa.WindowOpenAttestation(**kw)


def _signed(priv, att):
    return priv.sign(att.canonical_bytes())


def _verify(att, sig, *, provider, nonce_store=None, authz_not_after=NOW + 500, now=NOW):
    return vwa.verify_window_open_authorization(
        att, sig, expected_owner_id=OWNER, expected_master_id=MASTER, expected_follower_id=FOLLOWER,
        authz_not_after_epoch=authz_not_after, now_epoch=now,
        public_key_provider=provider, nonce_store=(nonce_store or vwa.NonceStore()))


class VerifierWindowAuthorizationTests(SimpleTestCase):
    def setUp(self):
        self.priv = Ed25519PrivateKey.generate()
        self.pub = _raw_pub(self.priv)
        self.provider = lambda kid: self.pub if kid == "ct-win-1" else None

    def test_valid_attestation_verifies_and_binds(self):
        att = _att()
        res = _verify(att, _signed(self.priv, att), provider=self.provider)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["reason"], vwa.R_VERIFIED)
        self.assertEqual(res["bound"], {"owner_id": OWNER, "master_account_id": MASTER,
                                        "follower_account_id": FOLLOWER, "purpose": vwa.PURPOSE_INGEST})

    def test_unpinned_or_revoked_key_id_fails_invalid_identity(self):
        att = _att(key_id="not-pinned")   # revocation = the provider no longer maps this id ⇒ None
        res = _verify(att, _signed(self.priv, att), provider=self.provider)
        self.assertEqual(res["reason"], vwa.R_INVALID_IDENTITY)

    def test_wrong_key_signature_fails_bad_signature(self):
        other = Ed25519PrivateKey.generate()
        att = _att()
        res = _verify(att, _signed(other, att), provider=self.provider)   # signed by a different private key
        self.assertEqual(res["reason"], vwa.R_BAD_SIGNATURE)

    def test_tampered_field_fails_bad_signature(self):
        att = _att()
        sig = _signed(self.priv, att)
        tampered = _att(window_not_after_epoch=NOW + 99999)   # change a bound field after signing
        res = _verify(tampered, sig, provider=self.provider)
        self.assertEqual(res["reason"], vwa.R_BAD_SIGNATURE)

    def test_purpose_mismatch_fails(self):
        att = _att(purpose="EXECUTE")
        res = _verify(att, _signed(self.priv, att), provider=self.provider)
        self.assertEqual(res["reason"], vwa.R_PURPOSE_MISMATCH)

    def test_route_mismatch_fails(self):
        att = _att(follower_account_id=49)
        res = _verify(att, _signed(self.priv, att), provider=self.provider)
        self.assertEqual(res["reason"], vwa.R_ROUTE_MISMATCH)

    def test_tenant_mismatch_fails(self):
        att = _att(owner_id=30)
        res = _verify(att, _signed(self.priv, att), provider=self.provider)
        self.assertEqual(res["reason"], vwa.R_TENANT_MISMATCH)

    def test_stale_attestation_fails_expired(self):
        att = _att(issued_at_epoch=NOW - 10_000)   # older than max_issue_age
        res = _verify(att, _signed(self.priv, att), provider=self.provider)
        self.assertEqual(res["reason"], vwa.R_ATTESTATION_EXPIRED)

    def test_future_attestation_fails_expired(self):
        att = _att(issued_at_epoch=NOW + 10_000)   # implausibly future
        res = _verify(att, _signed(self.priv, att), provider=self.provider)
        self.assertEqual(res["reason"], vwa.R_ATTESTATION_EXPIRED)

    def test_window_not_outliving_authorization_fails(self):
        att = _att(window_not_after_epoch=NOW + 100)   # < authz_not_after (NOW+500)
        res = _verify(att, _signed(self.priv, att), provider=self.provider, authz_not_after=NOW + 500)
        self.assertEqual(res["reason"], vwa.R_WINDOW_EXPIRY_INSUFFICIENT)

    def test_already_expired_window_fails(self):
        att = _att(window_not_after_epoch=NOW - 1)
        res = _verify(att, _signed(self.priv, att), provider=self.provider, authz_not_after=NOW - 100)
        self.assertEqual(res["reason"], vwa.R_WINDOW_EXPIRY_INSUFFICIENT)

    def test_replay_is_single_use(self):
        ns = vwa.NonceStore()
        att = _att()
        sig = _signed(self.priv, att)
        first = _verify(att, sig, provider=self.provider, nonce_store=ns)
        second = _verify(att, sig, provider=self.provider, nonce_store=ns)   # same nonce again
        self.assertTrue(first["ok"])
        self.assertEqual(second["reason"], vwa.R_REPLAYED)

    def test_malformed_signature_fails_closed(self):
        att = _att()
        res = _verify(att, b"", provider=self.provider)
        self.assertEqual(res["reason"], vwa.R_MALFORMED)

    @override_settings(COPIER_WINDOW_OPEN_TRUST={})
    def test_default_registry_empty_is_dark(self):
        # The production registry resolver (default provider) is EMPTY by default ⇒ INVALID_IDENTITY ⇒ opener DARK.
        att = _att()
        res = vwa.verify_window_open_authorization(
            att, _signed(self.priv, att), expected_owner_id=OWNER, expected_master_id=MASTER,
            expected_follower_id=FOLLOWER, authz_not_after_epoch=NOW + 500, now_epoch=NOW)
        self.assertEqual(res["reason"], vwa.R_INVALID_IDENTITY)

    def test_rotation_new_key_id_accepts_new_key(self):
        new_priv = Ed25519PrivateKey.generate()
        new_pub = _raw_pub(new_priv)
        provider = lambda kid: new_pub if kid == "ct-win-2" else None   # old id dropped (revoked), new id pinned
        att = _att(key_id="ct-win-2")
        res = _verify(att, _signed(new_priv, att), provider=provider)
        self.assertTrue(res["ok"], res)
        # The old key_id now fails closed under the rotated registry.
        old = _att(key_id="ct-win-1")
        self.assertEqual(_verify(old, _signed(self.priv, old), provider=provider)["reason"], vwa.R_INVALID_IDENTITY)
