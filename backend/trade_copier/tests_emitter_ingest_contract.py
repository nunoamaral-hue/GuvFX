"""ADR-0077/ADR-0096 — emitter↔ingestion contract for the #46 real-time source.

The emitter signs with ``trade_copier.event_ingestion.sign_ingest_request`` — the SAME module the deployed
ingestion endpoint verifies with — so this pins the exact #46 / 55442 / Tradersway-Live lifecycle contract at
the signed-verifier boundary: a known-good full-lifecycle envelope verifies, and any post-signing mutation of
the tenant, payload, nonce or signature fails closed. Pure verifier (no DB, no endpoint, no secret of record).
"""
import time

from django.test import SimpleTestCase
from django.utils import timezone

from trade_copier import event_ingestion as ei

SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"  # 40 hex, test-only, no placeholder marker


class _Burn:
    def __init__(self):
        self.seen = set()

    def __call__(self, nonce, expiry):
        if nonce in self.seen:
            return False
        self.seen.add(nonce)
        return True


class EmitterIngestContractTests(SimpleTestCase):
    LOGIN = "55442"
    SERVER = "Tradersway-Live"

    def _rev(self, *, ticket=5001, revision=1, closed=False, side="SELL", vol=0.01):
        r = {"account_id": 46, "ticket": ticket, "revision": revision,
             "observed_time": timezone.now().isoformat(), "closed": closed,
             "broker_login": self.LOGIN, "broker_server": self.SERVER}
        if not closed:
            r.update({"symbol": "XAUUSD", "side": side, "volume": vol,
                      "sl": 4000.0, "tp": 3980.0, "price_open": 3989.0})
        return r

    def _env(self, revisions, *, account_id=46, nonce="n1", now=None):
        return ei.sign_ingest_request(account_id=account_id, secret=SECRET, nonce=nonce,
                                      now=now if now is not None else time.time(),
                                      payload={"revisions": revisions})

    def _verify(self, env, *, keyring=None, now=None, burn=None):
        return ei.verify_ingest_request(env, keyring=keyring or {ei.DEFAULT_KEY_ID: SECRET},
                                        now=now if now is not None else time.time(),
                                        nonce_burn=burn or _Burn())

    def test_full_lifecycle_envelope_verifies_for_46(self):
        revs = [self._rev(revision=1), self._rev(revision=2), self._rev(revision=3, closed=True)]
        revs[1]["sl"] = 3995.0   # tightened SL = MODIFY
        out = self._verify(self._env(revs))
        self.assertEqual(int(out["signed"]["account_id"]), 46)
        self.assertEqual(len(out["payload"]["revisions"]), 3)

    def test_retarget_signed_account_to_follower_fails_closed(self):
        env = self._env([self._rev()])
        env["signed"]["account_id"] = 48   # retarget to the follower → HMAC/digest breaks
        with self.assertRaises(ei.IngestError) as c:
            self._verify(env)
        self.assertIn(c.exception.reason, ("bad_signature", "payload_digest_mismatch"))

    def test_tampered_revision_volume_fails_digest(self):
        env = self._env([self._rev(vol=0.01)])
        env["payload"]["revisions"][0]["volume"] = 5.0   # oversize after signing
        with self.assertRaises(ei.IngestError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "payload_digest_mismatch")

    def test_bad_signature_fails_closed(self):
        env = self._env([self._rev()])
        env["signature"] = "0" * 64
        with self.assertRaises(ei.IngestError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "bad_signature")

    def test_unknown_key_id_fails_closed(self):
        with self.assertRaises(ei.IngestError) as c:
            self._verify(self._env([self._rev()]), keyring={"other-key": SECRET})
        self.assertEqual(c.exception.reason, "unknown_key_id")

    def test_replayed_nonce_rejected(self):
        burn = _Burn()
        self._verify(self._env([self._rev()], nonce="same"), burn=burn)
        with self.assertRaises(ei.IngestError) as c:
            self._verify(self._env([self._rev(ticket=5002)], nonce="same"), burn=burn)
        self.assertEqual(c.exception.reason, "nonce_replayed")
