"""WV1 — seed_wv1_board_demo guard + output.

Asserts the prod-safety guard (refuses a non-demo DB / missing --confirm-demo-db) and that, when allowed, it seeds
the labelled SYNTHETIC dataset: 10 accounts incl. 2 at the same broker, and the owner-attributed account-UNRESOLVED
reconstruction projected by the real correlation engine (REQUESTED→COMPLETED, 33m01s). Idempotent on re-run.
"""
from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from broker_intelligence.models import Provenance, Withdrawal
from trading.models import TradingAccount

User = get_user_model()


class SeedBoardDemoGuardTests(TestCase):
    def test_refuses_non_demo_db_without_debug_or_flag(self):
        # Test DB name is "test_<...>" (no "demo"), DEBUG off, demo flag off -> must refuse (prod-safety).
        with self.assertRaises(CommandError) as ctx:
            call_command("seed_wv1_board_demo", "--confirm-demo-db", stdout=StringIO())
        self.assertIn("does not look like a demo database", str(ctx.exception))

    @override_settings(DEBUG=True)
    def test_refuses_without_confirm_flag(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("seed_wv1_board_demo", stdout=StringIO())
        self.assertIn("--confirm-demo-db", str(ctx.exception))

    @override_settings(DEBUG=True)
    def test_seeds_labelled_dataset_and_is_idempotent(self):
        call_command("seed_wv1_board_demo", "--confirm-demo-db", stdout=StringIO())
        member = User.objects.get(username="demo-board")
        accts = TradingAccount.objects.filter(user=member)
        self.assertEqual(accts.count(), 10)
        self.assertEqual(accts.filter(broker_name="Pepperstone").count(), 2)   # two at the SAME broker
        wds = Withdrawal.objects.filter(owner=member)
        self.assertEqual(wds.count(), 8)
        self.assertTrue(all(w.provenance == Provenance.SYNTHETIC for w in wds))  # labelled synthetic — never REAL
        # the owner-attributed, account-UNRESOLVED reconstruction, COMPLETED via the advance-only path, 33m01s
        g = wds.get(trading_account__isnull=True)
        self.assertEqual(g.status, Withdrawal.Status.COMPLETED)
        self.assertEqual(g.owner_id, member.id)
        self.assertEqual(g.duration_seconds(), 33 * 60 + 1)
        # idempotent
        call_command("seed_wv1_board_demo", "--confirm-demo-db", stdout=StringIO())
        self.assertEqual(TradingAccount.objects.filter(user=member).count(), 10)
        self.assertEqual(Withdrawal.objects.filter(owner=member).count(), 8)
