"""Per-follower sizing validation + catalogue freshness/tradability (packet §5).

Proves sizing is validated against the follower's OWN broker constraints (never a hard-coded value),
economic-exposure differences (XAUUSD contract 100 vs XAUUSD_SB contract 1) are preserved, and that a
follower with no synced constraints fails closed (readiness block, not a silent pass)."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution.models import BrokerInstrument
from trade_copier.catalogue import (
    catalogue_state, is_catalogue_stale, is_tradable, validate_follower_sizing,
)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()


def _acct(user, name, num, *, broker="TradersWay", server="Tradersway-Live"):
    bs, _ = BrokerServer.objects.get_or_create(
        server_name=server, defaults={"broker_display_name": broker, "environment": "live"})
    return TradingAccount.objects.create(user=user, name=name, broker_name=broker, account_number=num,
                                         is_demo=False, is_active=True, broker_server=bs)


def _bi(account, symbol, base, meta, *, enabled=True):
    return BrokerInstrument.objects.create(account=account, broker_symbol=symbol, base_symbol=base,
                                           enabled=enabled, metadata=meta)


class SizingValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="f", email="f@guvfx.invalid", password="x")
        self.is6 = _acct(self.user, "IS6", "60001", broker="IS6FX", server="IS6Technologies-Live")
        self.pep = _acct(self.user, "Pep", "60002", broker="Pepperstone", server="PepperstoneUK-Live")
        # IS6 XAUUSD.std: min 0.01 / step 0.01 / max 30, contract 100.
        _bi(self.is6, "XAUUSD.std", "XAUUSD", {"volume_min": 0.01, "volume_step": 0.01, "volume_max": 30.0,
                                               "contract_size": 100.0, "currency_profit": "USD", "trade_mode": 4})
        # Pepperstone XAUUSD_SB: min 1.0 / step 0.1 / max 10000, contract 1 (the real #49 spec).
        _bi(self.pep, "XAUUSD_SB", "XAUUSD", {"volume_min": 1.0, "volume_step": 0.1, "volume_max": 10000.0,
                                              "contract_size": 1.0, "currency_profit": "GBP", "trade_mode": 4})

    def test_valid_is6_lot_on_step(self):
        r = validate_follower_sizing(self.is6, "XAUUSD.std", fixed_lot="0.05")
        self.assertTrue(r.ok)
        self.assertEqual(r.reason, "valid")

    def test_below_min_rejected(self):
        r = validate_follower_sizing(self.pep, "XAUUSD_SB", fixed_lot="0.01")
        self.assertFalse(r.ok)
        self.assertTrue(any("below_min" in f for f in r.failures))

    def test_off_step_rejected(self):
        r = validate_follower_sizing(self.pep, "XAUUSD_SB", fixed_lot="1.05")   # min 1.0, step 0.1
        self.assertFalse(r.ok)
        self.assertTrue(any("off_step" in f for f in r.failures))

    def test_on_step_accepted(self):
        r = validate_follower_sizing(self.pep, "XAUUSD_SB", fixed_lot="1.1")
        self.assertTrue(r.ok, r.failures)

    def test_above_max_rejected(self):
        r = validate_follower_sizing(self.is6, "XAUUSD.std", fixed_lot="31")   # max 30
        self.assertFalse(r.ok)
        self.assertTrue(any("above_max" in f for f in r.failures))

    def test_max_volume_above_broker_max_rejected(self):
        r = validate_follower_sizing(self.is6, "XAUUSD.std", fixed_lot="1", max_volume="40")
        self.assertFalse(r.ok)
        self.assertTrue(any("max_volume_above_broker_max" in f for f in r.failures))

    def test_float_repr_safe_step_check(self):
        # 0.01 + 0.02 style float artefacts must not trigger a false off-step rejection.
        r = validate_follower_sizing(self.is6, "XAUUSD.std", fixed_lot="0.03")
        self.assertTrue(r.ok, r.failures)

    def test_constraints_unavailable_fails_closed(self):
        r = validate_follower_sizing(self.pep, "XAUUSD_SB_MISSING", fixed_lot="1")
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "constraints_unavailable")

    def test_economic_exposure_preserved_contract_100_vs_1(self):
        is6 = validate_follower_sizing(self.is6, "XAUUSD.std", fixed_lot="1")   # contract 100
        pep = validate_follower_sizing(self.pep, "XAUUSD_SB", fixed_lot="1")    # contract 1
        self.assertEqual(is6.fixed_lot_exposure, Decimal("100"))
        self.assertEqual(pep.fixed_lot_exposure, Decimal("1"))
        self.assertNotEqual(is6.fixed_lot_exposure, pep.fixed_lot_exposure)     # NOT folded together

    def test_per_follower_independent(self):
        # 0.01 is valid on IS6 (min 0.01) but invalid on Pepperstone (min 1.0) — same number, different verdict.
        self.assertTrue(validate_follower_sizing(self.is6, "XAUUSD.std", fixed_lot="0.01").ok)
        self.assertFalse(validate_follower_sizing(self.pep, "XAUUSD_SB", fixed_lot="0.01").ok)


class CatalogueStateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="c", email="c@guvfx.invalid", password="x")
        self.acct = _acct(self.user, "A", "61001")

    def test_empty_catalogue(self):
        st = catalogue_state(self.acct)
        self.assertEqual(st["state"], "empty")
        self.assertEqual(st["symbol_count"], 0)

    def test_synced_catalogue_counts(self):
        _bi(self.acct, "EURUSD", "EURUSD", {"trade_mode": 4})
        _bi(self.acct, "GBPUSD", "GBPUSD", {"trade_mode": 4}, enabled=False)
        st = catalogue_state(self.acct)
        self.assertEqual(st["state"], "synced")
        self.assertEqual((st["symbol_count"], st["enabled_count"]), (2, 1))

    def test_stale_catalogue_flagged(self):
        _bi(self.acct, "EURUSD", "EURUSD", {"trade_mode": 4})
        # Force synced_at far in the past (auto_now field → update via queryset).
        old = timezone.now() - timedelta(days=3650)
        BrokerInstrument.objects.filter(account=self.acct).update(synced_at=old)
        with self.settings():
            import trade_copier.catalogue as cat
            orig = cat._STALE_SECONDS
            cat._STALE_SECONDS = 3600
            try:
                self.assertTrue(is_catalogue_stale(self.acct))
            finally:
                cat._STALE_SECONDS = orig

    def test_is_tradable_rejects_disabled_and_closeonly(self):
        self.assertTrue(is_tradable({"trade_mode": 4}))
        self.assertFalse(is_tradable({"trade_mode": 0}))   # disabled
        self.assertFalse(is_tradable({"trade_mode": 3}))   # close-only
        self.assertFalse(is_tradable({"trade_mode": 4, "visible": False}))
