"""ADR-0074 Phase-6 — bounded, safe nonce retention.

Dry-run by default; prunes ONLY fully-expired nonces; NEVER weakens in-window replay protection; a positive
margin is strictly more conservative; counts are secret-free. Mutation-killers: a dry-run deletes nothing; an
in-window nonce is always retained; the margin keeps a recently-expired nonce.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from trade_copier.event_ingestion import prune_ingest_nonces
from trade_copier.models import CopierIngestNonce


class NoncePruneTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.expired = CopierIngestNonce.objects.create(nonce="old", expires_at=now - timedelta(seconds=120))
        self.recent = CopierIngestNonce.objects.create(nonce="recent", expires_at=now - timedelta(seconds=10))
        self.live = CopierIngestNonce.objects.create(nonce="live", expires_at=now + timedelta(seconds=120))

    def test_dry_run_deletes_nothing(self):
        out = prune_ingest_nonces(dry_run=True)
        self.assertEqual(out["eligible"], 2)          # both expired are eligible
        self.assertEqual(out["pruned"], 0)
        self.assertEqual(CopierIngestNonce.objects.count(), 3)

    def test_apply_prunes_only_expired(self):
        out = prune_ingest_nonces(dry_run=False)
        self.assertEqual(out["pruned"], 2)
        self.assertTrue(CopierIngestNonce.objects.filter(nonce="live").exists())   # in-window retained
        self.assertFalse(CopierIngestNonce.objects.filter(nonce="old").exists())

    def test_margin_retains_recently_expired(self):
        out = prune_ingest_nonces(dry_run=False, margin_seconds=60)
        # Only "old" (expired 120s ago) is beyond a 60s margin; "recent" (10s) and "live" are retained.
        self.assertEqual(out["pruned"], 1)
        self.assertTrue(CopierIngestNonce.objects.filter(nonce="recent").exists())
        self.assertTrue(CopierIngestNonce.objects.filter(nonce="live").exists())

    def test_in_window_nonce_never_pruned(self):
        prune_ingest_nonces(dry_run=False, margin_seconds=0)
        self.assertTrue(CopierIngestNonce.objects.filter(nonce="live").exists())
