"""Synthetic-key tests for the Ed25519 INGEST-event signing contract (GFX-173, ADR-0130).

PURE: synthetic Ed25519 keypairs + injected providers (trust / window lookup / in-memory nonce burn); no host,
no network, no DB, no real key or window. Producer and verifier are exercised against SHARED synthetic vectors,
and every fail-closed path is asserted. An Ed25519 event is value-blind and never falls back to HMAC.
"""
from __future__ import annotations

import base64

from django.test import SimpleTestCase

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trade_copier import ingest_event_signing as ies

NOW = 1_800_000_000
OWNER, MASTER, FOLLOWER = 29, 46, 48
KEY_ID = "copier-ingest-event-syn-1"
WINDOW_NOT_AFTER = NOW + 300


def _keypair():
    sk = Ed25519PrivateKey.generate()
    raw_pub = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return sk, raw_pub


def _provider(mapping):
    """An injected pinned-key provider: {key_id: raw_pub_bytes}. Returns None for an absent/revoked key_id."""
    def p(key_id):
        return mapping.get(key_id)
    return p


class _MemBurn:
    """In-memory single-use nonce burn for the pure verifier tests."""

    def __init__(self):
        self.seen = set()

    def __call__(self, nonce, expiry):
        if nonce in self.seen:
            return False
        self.seen.add(nonce)
        return True


class IngestEventSigningTests(SimpleTestCase):
    def setUp(self):
        self.sk, self.pub = _keypair()
        self.trust = {KEY_ID: self.pub}

    def _payload(self):
        return {"revisions": [{"account_id": MASTER, "ticket": 101, "revision": 3,
                               "side": "BUY", "volume": 0.01, "symbol": "EURUSD"}]}

    def _env(self, **over):
        kw = dict(signing_key=self.sk, owner_id=OWNER, master_account_id=MASTER, follower_account_id=FOLLOWER,
                  nonce="evn-1", issued_at_epoch=NOW, expiry_epoch=NOW + 120,
                  window_not_after_epoch=WINDOW_NOT_AFTER, payload=self._payload(), key_id=KEY_ID)
        kw.update(over)
        return ies.sign_ingest_event(**kw)

    def _verify(self, env, *, provider=None, window=WINDOW_NOT_AFTER, now=NOW, burn=None,
                owner=OWNER, master=MASTER, follower=FOLLOWER, purpose=ies.INGEST_EVENT_PURPOSE):
        return ies.verify_ingest_event(
            env, expected_owner_id=owner, expected_master_id=master, expected_follower_id=follower,
            now_epoch=now, nonce_burn=burn or _MemBurn(),
            public_key_provider=provider or _provider(self.trust),
            window_not_after_provider=lambda *, now_epoch: window, expected_purpose=purpose)

    # ── shared synthetic vectors (producer ⇄ verifier) ──────────────────────────────────────────────
    def test_roundtrip_ok(self):
        out = self._verify(self._env())
        self.assertEqual(out["bound"], {"owner_id": OWNER, "master_account_id": MASTER,
                                        "follower_account_id": FOLLOWER, "purpose": "INGEST",
                                        "window_not_after_epoch": WINDOW_NOT_AFTER, "key_id": KEY_ID})

    def test_shared_vector_signing_input_is_domain_separated_and_independently_verifiable(self):
        env = self._env()
        # Independently reconstruct the signed bytes and verify the signature with the bare public key — proves
        # producer and verifier agree byte-for-byte AND that the signing input carries the event domain prefix.
        msg = ies.signing_input(env["signed"])
        self.assertTrue(msg.startswith(ies.INGEST_EVENT_DOMAIN + b"\n"))
        self.sk.public_key().verify(bytes.fromhex(env["signature"]), msg)   # raises if the vector disagrees
        self.assertEqual(env["alg"], "ed25519")

    def test_payload_digest_binds_body(self):
        env = self._env()
        self.assertEqual(env["signed"]["payload_digest"], ies.payload_digest(env["payload"]))

    # ── identity / signature ───────────────────────────────────────────────────────────────────────
    def test_empty_trust_is_dark_unknown_key(self):
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(), provider=_provider({}))        # DARK default: nothing pinned
        self.assertEqual(c.exception.reason, ies.R_UNKNOWN_KEY)

    def test_revoked_key_fails_unknown(self):
        env = self._env()
        # Revocation = un-pin the key_id (remove from the registry) ⇒ provider returns None.
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(env, provider=_provider({"other": self.pub}))
        self.assertEqual(c.exception.reason, ies.R_UNKNOWN_KEY)

    def test_wrong_key_fails_bad_signature(self):
        _, other_pub = _keypair()
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(), provider=_provider({KEY_ID: other_pub}))
        self.assertEqual(c.exception.reason, ies.R_BAD_SIGNATURE)

    def test_tampered_signature_fails(self):
        env = self._env()
        env["signature"] = "00" * 64                 # well-formed hex, wrong 64-byte signature
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, ies.R_BAD_SIGNATURE)

    def test_non_hex_signature_is_malformed(self):
        env = self._env()
        env["signature"] = "zz-not-hex"
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, ies.R_MALFORMED)

    def test_tampered_signed_field_breaks_signature(self):
        env = self._env()
        env["signed"]["owner_id"] = OWNER + 1         # every signed field is bound ⇒ signature no longer matches
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, ies.R_BAD_SIGNATURE)

    def test_tampered_payload_fails_digest(self):
        env = self._env()
        env["payload"] = {"revisions": [{"account_id": MASTER, "ticket": 999, "revision": 9}]}
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, ies.R_PAYLOAD_DIGEST_MISMATCH)

    # ── binding: purpose / route / tenant ──────────────────────────────────────────────────────────
    def test_purpose_mismatch(self):
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(purpose="ATTEST"))
        self.assertEqual(c.exception.reason, ies.R_PURPOSE_MISMATCH)

    def test_route_mismatch_master(self):
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(), master=47)
        self.assertEqual(c.exception.reason, ies.R_ROUTE_MISMATCH)

    def test_route_mismatch_follower(self):
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(), follower=49)
        self.assertEqual(c.exception.reason, ies.R_ROUTE_MISMATCH)

    def test_tenant_mismatch(self):
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(), owner=30)
        self.assertEqual(c.exception.reason, ies.R_TENANT_MISMATCH)

    # ── authorized window ──────────────────────────────────────────────────────────────────────────
    def test_no_authorized_window(self):
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(), window=None)
        self.assertEqual(c.exception.reason, ies.R_NO_WINDOW)

    def test_window_mismatch(self):
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(), window=WINDOW_NOT_AFTER + 1)     # a DIFFERENT open window
        self.assertEqual(c.exception.reason, ies.R_WINDOW_MISMATCH)

    def test_window_expired(self):
        env = self._env(window_not_after_epoch=NOW + 10, expiry_epoch=NOW + 5)
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(env, window=NOW + 10, now=NOW + 20)
        self.assertEqual(c.exception.reason, ies.R_WINDOW_EXPIRED)

    # ── event expiry ───────────────────────────────────────────────────────────────────────────────
    def test_stale_issue(self):
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(issued_at_epoch=NOW - 200, expiry_epoch=NOW - 80))
        self.assertEqual(c.exception.reason, ies.R_EVENT_EXPIRED)

    def test_future_issue(self):
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(issued_at_epoch=NOW + 120, expiry_epoch=NOW + 200))
        self.assertEqual(c.exception.reason, ies.R_EVENT_EXPIRED)

    def test_past_expiry(self):
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(issued_at_epoch=NOW, expiry_epoch=NOW + 5), now=NOW + 10)
        self.assertEqual(c.exception.reason, ies.R_EVENT_EXPIRED)

    def test_ttl_too_long(self):
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(issued_at_epoch=NOW, expiry_epoch=NOW + 700,
                                   window_not_after_epoch=NOW + 800), window=NOW + 800)
        self.assertEqual(c.exception.reason, ies.R_EVENT_EXPIRED)

    def test_event_may_not_outlive_window(self):
        env = self._env(issued_at_epoch=NOW, expiry_epoch=NOW + 200, window_not_after_epoch=NOW + 100)
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(env, window=NOW + 100, now=NOW)
        self.assertEqual(c.exception.reason, ies.R_EVENT_OUTLIVES_WINDOW)

    # ── replay / structure ─────────────────────────────────────────────────────────────────────────
    def test_replay_same_nonce(self):
        burn = _MemBurn()
        env = self._env()
        self._verify(env, burn=burn)                             # first use OK
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(), burn=burn)                 # same nonce again ⇒ replay
        self.assertEqual(c.exception.reason, ies.R_NONCE_REPLAYED)

    def test_bad_alg(self):
        env = self._env()
        env["alg"] = "hmac"
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, ies.R_BAD_ALG)

    def test_bad_protocol(self):
        env = self._env()
        env["signed"]["protocol_version"] = "copier-ingest-1"   # the HMAC protocol id
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, ies.R_BAD_PROTOCOL)

    def test_missing_signed_field(self):
        env = self._env()
        del env["signed"]["nonce"]
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, ies.R_MALFORMED)

    def test_nonce_too_long(self):
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(self._env(nonce="x" * (ies.MAX_NONCE_LEN + 1)))
        self.assertEqual(c.exception.reason, ies.R_NONCE_TOO_LONG)

    # ── domain separation (vs window-open authorization) ───────────────────────────────────────────
    def test_non_domain_signature_is_rejected(self):
        """A signature produced over the signed field set WITHOUT the event domain prefix (the shape a
        window-open-style signer would use) must NOT verify as an event — proving the domain binds."""
        att = ies.IngestEventAttestation(
            key_id=KEY_ID, owner_id=OWNER, master_account_id=MASTER, follower_account_id=FOLLOWER,
            purpose="INGEST", window_not_after_epoch=WINDOW_NOT_AFTER, issued_at_epoch=NOW,
            expiry_epoch=NOW + 120, nonce="evn-x", payload_digest=ies.payload_digest(self._payload()))
        signed = att.signed_dict()
        no_domain_sig = self.sk.sign(ies.canonical_bytes({k: signed[k] for k in ies._SIGNED_FIELDS})).hex()
        env = {"alg": "ed25519", "signed": signed, "signature": no_domain_sig, "payload": self._payload()}
        with self.assertRaises(ies.IngestEventError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, ies.R_BAD_SIGNATURE)

    # ── value-blind ─────────────────────────────────────────────────────────────────────────────────
    def test_reason_is_value_blind(self):
        try:
            self._verify(self._env(), provider=_provider({}))
        except ies.IngestEventError as e:
            blob = (str(e) + e.reason).lower()
            for needle in ("evn-", base64.b64encode(self.pub).decode().lower()[:12], "begin"):
                self.assertNotIn(needle, blob)
