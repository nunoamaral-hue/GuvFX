"""DB tests for the durable single-use nonce store (GFX-173, ADR-0130).

Proves the replay guard is single-use ACROSS a simulated restart (a fresh store instance reading the same
``CopierIngestNonce`` rows) and ACROSS concurrent requests (real threads racing one UNIQUE insert on the test
database), that a persistence fault FAILS CLOSED (treated as a replay, never a fresh nonce), and that the
ADR-0129 window-open authorization is now DURABLY single-use when given this store. Synthetic keys only; no host,
no network.
"""
from __future__ import annotations

import threading
from unittest.mock import patch

from django.db import connections
from django.test import TestCase, TransactionTestCase

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trade_copier import durable_nonce as dn
from trade_copier import verifier_window_authorization as vwa
from trade_copier.models import CopierIngestNonce

NOW = 1_800_000_000


class DurableNonceStoreTests(TestCase):
    def test_consume_is_single_use(self):
        store = dn.durable_window_open_nonce_store()
        self.assertTrue(store.consume(scope="s", nonce="n1", now_epoch=NOW))
        self.assertFalse(store.consume(scope="s", nonce="n1", now_epoch=NOW))   # replay

    def test_single_use_survives_restart(self):
        self.assertTrue(dn.durable_window_open_nonce_store().consume(scope="s", nonce="n1", now_epoch=NOW))
        # A brand-new store instance = a fresh process after restart. The burned nonce is still durable.
        fresh = dn.durable_window_open_nonce_store()
        self.assertFalse(fresh.consume(scope="s", nonce="n1", now_epoch=NOW + 5))

    def test_distinct_nonces_both_fresh(self):
        store = dn.durable_window_open_nonce_store()
        self.assertTrue(store.consume(scope="s", nonce="a", now_epoch=NOW))
        self.assertTrue(store.consume(scope="s", nonce="b", now_epoch=NOW))

    def test_distinct_scopes_do_not_collide(self):
        store = dn.durable_window_open_nonce_store()
        self.assertTrue(store.consume(scope="s1", nonce="n", now_epoch=NOW))
        self.assertTrue(store.consume(scope="s2", nonce="n", now_epoch=NOW))

    def test_distinct_domains_do_not_collide(self):
        wo = dn.DurableNonceStore(domain="d-wo")
        ev = dn.DurableNonceStore(domain="d-ev")
        self.assertTrue(wo.consume(scope="s", nonce="n", now_epoch=NOW))
        self.assertTrue(ev.consume(scope="s", nonce="n", now_epoch=NOW))   # same (scope,nonce), other domain

    def test_stored_token_is_value_blind_and_bounded(self):
        dn.durable_window_open_nonce_store().consume(scope="route:46->48", nonce="secret-nonce-xyz", now_epoch=NOW)
        row = CopierIngestNonce.objects.get()
        self.assertNotIn("secret-nonce-xyz", row.nonce)        # the raw nonce is never stored
        self.assertLessEqual(len(row.nonce), 80)               # fits the varchar(80) column

    def test_persistence_failure_fails_closed(self):
        store = dn.durable_window_open_nonce_store()
        with patch.object(CopierIngestNonce.objects, "create", side_effect=RuntimeError("db down")):
            self.assertFalse(store.consume(scope="s", nonce="n", now_epoch=NOW))   # fault ⇒ treated as replay

    def test_event_burn_single_use_and_restart(self):
        burn = dn.durable_ingest_event_nonce_burn()
        self.assertTrue(burn("evn-1", float(NOW + 120)))
        self.assertFalse(burn("evn-1", float(NOW + 120)))                 # same instance replay
        self.assertFalse(dn.durable_ingest_event_nonce_burn()("evn-1", float(NOW + 120)))  # restart replay

    def test_event_burn_persistence_failure_fails_closed(self):
        burn = dn.durable_ingest_event_nonce_burn()
        with patch.object(CopierIngestNonce.objects, "create", side_effect=RuntimeError("db down")):
            self.assertFalse(burn("evn-x", float(NOW + 120)))


def _keypair():
    sk = Ed25519PrivateKey.generate()
    raw_pub = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return sk, raw_pub


class WindowOpenAuthorizationDurableTests(TestCase):
    """ADR-0129 gap #2 closed: the window-open authorization is single-use across a restart when backed by the
    durable store (its default in-memory guard could not survive a process restart)."""

    def _att(self, nonce="won-1"):
        return vwa.WindowOpenAttestation(
            key_id="wo-syn-1", owner_id=29, master_account_id=46, follower_account_id=48, purpose="INGEST",
            issued_at_epoch=NOW, window_not_after_epoch=NOW + 600, nonce=nonce)

    def _verify(self, att, sk, pub, store):
        sig = sk.sign(att.canonical_bytes())
        return vwa.verify_window_open_authorization(
            att, sig, expected_owner_id=29, expected_master_id=46, expected_follower_id=48,
            authz_not_after_epoch=NOW + 300, now_epoch=NOW,
            public_key_provider=lambda kid: pub if kid == "wo-syn-1" else None, nonce_store=store)

    def test_durable_single_use_across_restart(self):
        sk, pub = _keypair()
        att = self._att()
        first = self._verify(att, sk, pub, dn.durable_window_open_nonce_store())
        self.assertTrue(first["ok"], first)
        # Restart: a fresh durable store, same durable rows — the same attestation can never re-open a window.
        second = self._verify(att, sk, pub, dn.durable_window_open_nonce_store())
        self.assertEqual(second["reason"], vwa.R_REPLAYED)


class DurableNonceConcurrencyTests(TransactionTestCase):
    """Real concurrency: two threads race the SAME single-use burn; the UNIQUE insert admits exactly one."""
    reset_sequences = True

    def _race(self, fn):
        results = [None, None]
        barrier = threading.Barrier(2)

        def worker(i):
            try:
                barrier.wait(timeout=5)
                results[i] = fn()
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        return results

    def test_concurrent_replay_admits_exactly_one(self):
        def fn():
            return dn.durable_window_open_nonce_store().consume(scope="race", nonce="n", now_epoch=NOW)
        results = self._race(fn)
        self.assertEqual(sorted(bool(r) for r in results), [False, True])   # exactly one winner
        self.assertEqual(CopierIngestNonce.objects.count(), 1)

    def test_concurrent_event_burn_admits_exactly_one(self):
        def fn():
            return dn.durable_ingest_event_nonce_burn()("evn-race", float(NOW + 120))
        results = self._race(fn)
        self.assertEqual(sorted(bool(r) for r in results), [False, True])
        self.assertEqual(CopierIngestNonce.objects.count(), 1)
