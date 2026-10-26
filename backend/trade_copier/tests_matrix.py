"""Directional instrument-mapping matrix (packet §3/§4/§6/§7).

Fixtures model the three founder brokers: #46 TradersWay (XAUUSD contract 100 / USD), #48 IS6FX
(XAUUSD.std contract 100 / USD) and #49 Pepperstone (XAUUSD_SB contract 1 / GBP). The verified
``46→48 XAUUSD→XAUUSD.std`` map pre-exists. Proves: all six ordered directions; directionality (reverse
not auto-verified); canonical-in-identity uniqueness (a native pair cannot attach to two canonicals) and
cross-pair isolation; coverage reconciliation + raw-vs-eligible; stale/disabled/expired/ambiguous
fail-closed; preservation of the existing map; command safety (dry-run default, same-owner, self-pair); and
the execution-off invariant."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from execution.models import BrokerInstrument, ExecutionJob
from trade_copier import matrix as M
from trade_copier.matrix import compute_matrix, generate_candidates
from trade_copier.models import CanonicalInstrument, CopierSymbolMap, InstrumentMapping
from trading.models import BrokerServer, TradingAccount

User = get_user_model()

_XAU_USD = {"contract_size": 100.0, "currency_base": "XAU", "currency_profit": "USD", "currency_margin": "XAU",
            "volume_min": 0.01, "volume_step": 0.01, "volume_max": 100.0, "trade_mode": 4, "path": "Metals\\XAUUSD"}
_XAU_STD = {"contract_size": 100.0, "currency_base": "USD", "currency_profit": "USD", "currency_margin": "USD",
            "volume_min": 0.01, "volume_step": 0.01, "volume_max": 30.0, "trade_mode": 4, "path": "Spot Metals\\XAUUSD.std"}
_XAU_SB = {"contract_size": 1.0, "currency_base": "GBP", "currency_profit": "GBP", "currency_margin": "GBP",
           "volume_min": 1.0, "volume_step": 0.1, "volume_max": 10000.0, "trade_mode": 4, "path": "SB Metals\\XAUUSD_SB"}
_EUR = {"contract_size": 100000.0, "currency_base": "EUR", "currency_profit": "USD", "currency_margin": "EUR",
        "volume_min": 0.01, "volume_step": 0.01, "volume_max": 500.0, "trade_mode": 4, "path": "Forex\\EURUSD"}


def _acct(user, name, num, broker, server):
    bs, _ = BrokerServer.objects.get_or_create(
        server_name=server, defaults={"broker_display_name": broker, "environment": "live"})
    return TradingAccount.objects.create(user=user, name=name, broker_name=broker, account_number=num,
                                         is_demo=False, is_active=True, broker_server=bs)


def _bi(account, symbol, base, meta, *, enabled=True):
    return BrokerInstrument.objects.create(account=account, broker_symbol=symbol, base_symbol=base,
                                           enabled=enabled, metadata=meta)


class MatrixFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="f", email="f@guvfx.invalid", password="x")
        self.m46 = _acct(self.user, "TradersWay", "46", "TradersWay", "Tradersway-Live")
        self.f48 = _acct(self.user, "IS6FX", "48", "IS6FX", "IS6Technologies-Live")
        self.f49 = _acct(self.user, "Pepperstone", "49", "Pepperstone", "PepperstoneUK-Live")
        _bi(self.m46, "XAUUSD", "XAUUSD", _XAU_USD)
        _bi(self.m46, "EURUSD", "EURUSD", _EUR)
        _bi(self.f48, "XAUUSD.std", "XAUUSD", _XAU_STD)
        _bi(self.f48, "EURUSD.std", "EURUSD", _EUR)
        _bi(self.f49, "XAUUSD_SB", "XAUUSD", _XAU_SB)
        _bi(self.f49, "EURUSD", "EURUSD", dict(_EUR))
        self.vmap = CopierSymbolMap.objects.create(
            master_broker="Tradersway-Live", master_symbol="XAUUSD",
            follower_broker="IS6Technologies-Live", follower_symbol="XAUUSD.std",
            verified=True, verified_by=self.user, verified_at=timezone.now())

    def _cand(self, direction, master_symbol):
        return next(c for c in direction.candidates if c.master_symbol == master_symbol)


class ClassificationTests(MatrixFixture):
    def test_forward_46_to_48_verified_and_coverage(self):
        d = generate_candidates(self.m46, self.f48)
        self.assertEqual(self._cand(d, "XAUUSD").classification, M.VERIFIED)
        self.assertEqual(self._cand(d, "EURUSD").classification, M.UNVERIFIED)
        self.assertEqual(d.eligible_source, 2)
        self.assertEqual(d.counts[M.VERIFIED], 1)
        self.assertEqual(d.coverage_pct, 50.0)

    def test_reverse_48_to_46_not_auto_verified(self):
        d = generate_candidates(self.f48, self.m46)
        self.assertEqual(self._cand(d, "XAUUSD.std").classification, M.UNVERIFIED)
        self.assertEqual(d.counts[M.VERIFIED], 0)
        self.assertEqual(d.coverage_pct, 0.0)

    def test_46_to_49_identity_matches_but_settlement_is_a_compatibility_warning(self):
        """Canonical identity matches (same key, both HIGH confidence); the pairing is AMBIGUOUS because
        of a settlement-currency + contract-type compatibility WARNING — not a wrong identity."""
        d = generate_candidates(self.m46, self.f49)
        c = self._cand(d, "XAUUSD")
        self.assertEqual(c.classification, M.AMBIGUOUS)
        self.assertEqual(c.reason, "material_compatibility_warning")
        self.assertEqual(c.identity_confidence, "high")                 # identity is NOT impugned
        self.assertEqual(c.canonical_slug, "METAL:XAU:USD")             # same canonical as the master
        self.assertTrue(c.material_differences["settlement_currency"]["differs"])  # USD vs GBP
        self.assertTrue(c.material_differences["contract_type"]["differs"])        # SPOT vs SPREADBET
        self.assertTrue(c.material_differences["contract_size"]["differs"])        # 100 vs 1
        self.assertIn("settlement_currency", c.compatibility_warnings)
        self.assertEqual(self._cand(d, "EURUSD").classification, M.UNVERIFIED)

    def test_49_to_46_also_ambiguous(self):
        d = generate_candidates(self.f49, self.m46)
        self.assertEqual(self._cand(d, "XAUUSD_SB").classification, M.AMBIGUOUS)

    def test_all_six_ordered_directions_present(self):
        view = compute_matrix([self.m46, self.f48, self.f49])
        pairs = {(d.master_account_id, d.follower_account_id) for d in view.directions}
        expected = {(a.id, b.id) for a in (self.m46, self.f48, self.f49)
                    for b in (self.m46, self.f48, self.f49) if a.id != b.id}
        self.assertEqual(pairs, expected)
        self.assertEqual(len(view.directions), 6)

    def test_absent_when_follower_lacks_canonical(self):
        _bi(self.m46, "USDJPY", "USDJPY", {"currency_base": "USD", "currency_profit": "JPY", "trade_mode": 4})
        d = generate_candidates(self.m46, self.f48)
        self.assertEqual(self._cand(d, "USDJPY").classification, M.ABSENT)

    def test_ineligible_when_follower_symbol_close_only(self):
        _bi(self.m46, "XAGUSD", "XAGUSD", {"currency_base": "XAG", "currency_profit": "USD",
                                           "contract_size": 5000.0, "trade_mode": 4})
        _bi(self.f48, "XAGUSD.std", "XAGUSD", {"currency_base": "USD", "currency_profit": "USD",
                                               "contract_size": 5000.0, "trade_mode": 3})  # close-only
        d = generate_candidates(self.m46, self.f48)
        self.assertEqual(self._cand(d, "XAGUSD").classification, M.INELIGIBLE)

    def test_ambiguous_when_multiple_follower_matches(self):
        _bi(self.m46, "USDJPY", "USDJPY", {"currency_base": "USD", "currency_profit": "JPY", "trade_mode": 4})
        _bi(self.f48, "USDJPY", "USDJPY", {"currency_base": "USD", "currency_profit": "JPY", "trade_mode": 4})
        _bi(self.f48, "USDJPY.raw", "USDJPY", {"currency_base": "USD", "currency_profit": "JPY", "trade_mode": 4})
        d = generate_candidates(self.m46, self.f48)
        c = self._cand(d, "USDJPY")
        self.assertEqual(c.classification, M.AMBIGUOUS)
        self.assertEqual(c.reason, "multiple_follower_matches")

    def test_stale_catalogue_fails_closed(self):
        BrokerInstrument.objects.filter(account=self.f48).update(synced_at=timezone.now() - timedelta(days=3650))
        import trade_copier.catalogue as cat
        orig = cat._STALE_SECONDS
        cat._STALE_SECONDS = 3600
        try:
            d = generate_candidates(self.m46, self.f48)
            self.assertEqual(self._cand(d, "EURUSD").classification, M.STALE)
        finally:
            cat._STALE_SECONDS = orig

    def test_expired_follower_symbol_ineligible(self):
        _bi(self.m46, "USDJPY", "USDJPY", {"currency_base": "USD", "currency_profit": "JPY", "trade_mode": 4})
        _bi(self.f48, "USDJPY-F", "USDJPY", {"currency_base": "USD", "currency_profit": "JPY",
                                             "trade_mode": 4, "expiry": "2000-01-01"})
        d = generate_candidates(self.m46, self.f48)
        self.assertEqual(self._cand(d, "USDJPY").classification, M.INELIGIBLE)


class CoverageContractTests(MatrixFixture):
    def test_counts_reconcile_to_eligible_source(self):
        for d in compute_matrix([self.m46, self.f48, self.f49]).directions:
            self.assertEqual(sum(d.counts.values()), d.eligible_source)

    def test_raw_catalogue_count_distinct_from_eligible(self):
        # A disabled + a close-only master symbol are in the raw catalogue but NOT eligible sources.
        _bi(self.m46, "GBPUSD", "GBPUSD", {"currency_base": "GBP", "currency_profit": "USD", "trade_mode": 4}, enabled=False)
        _bi(self.m46, "AUDUSD", "AUDUSD", {"currency_base": "AUD", "currency_profit": "USD", "trade_mode": 3})
        d = generate_candidates(self.m46, self.f48)
        self.assertEqual(d.raw_catalogue_count, 4)   # XAUUSD, EURUSD, GBPUSD(disabled), AUDUSD(close-only)
        self.assertEqual(d.eligible_source, 2)       # only the two tradable enabled ones
        self.assertEqual(sum(d.counts.values()), 2)  # still reconciles to eligible

    def test_coverage_is_verified_over_eligible(self):
        d = generate_candidates(self.m46, self.f48)
        self.assertEqual(d.coverage_pct, round(100.0 * d.counts["VERIFIED"] / d.eligible_source, 1))


class IdentityUniquenessTests(MatrixFixture):
    def test_native_pair_cannot_attach_two_canonicals(self):
        c1 = CanonicalInstrument.objects.create(key="METAL:XAU:USD", asset_class="METAL", underlying="XAU", quote_currency="USD")
        c2 = CanonicalInstrument.objects.create(key="FX:EUR:USD", asset_class="FX", underlying="EUR", quote_currency="USD")
        InstrumentMapping.objects.create(
            master_account=self.m46, master_broker="Tradersway-Live", master_symbol="XAUUSD",
            follower_account=self.f48, follower_broker="IS6Technologies-Live", follower_symbol="XAUUSD.std",
            canonical_instrument=c1, classification="UNVERIFIED", observed_at=timezone.now())
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InstrumentMapping.objects.create(
                    master_account=self.m46, master_broker="Tradersway-Live", master_symbol="XAUUSD",
                    follower_account=self.f48, follower_broker="IS6Technologies-Live", follower_symbol="XAUUSD.std",
                    canonical_instrument=c2, classification="UNVERIFIED", observed_at=timezone.now())

    def test_distinct_directions_and_pairs_coexist(self):
        c = CanonicalInstrument.objects.create(key="METAL:XAU:USD", asset_class="METAL", underlying="XAU", quote_currency="USD")
        base = dict(master_broker="x", follower_broker="y", master_symbol="XAUUSD", follower_symbol="XAUUSD.std",
                    canonical_instrument=c, classification="UNVERIFIED", observed_at=timezone.now())
        InstrumentMapping.objects.create(master_account=self.m46, follower_account=self.f48, **base)
        InstrumentMapping.objects.create(master_account=self.f48, follower_account=self.m46, **base)  # reverse
        InstrumentMapping.objects.create(master_account=self.m46, follower_account=self.f49, **base)  # other follower
        self.assertEqual(InstrumentMapping.objects.count(), 3)


class PersistenceAndIsolationTests(MatrixFixture):
    def _refresh(self, m, f):
        call_command("refresh_instrument_mappings", "--master", str(m.id), "--follower", str(f.id), "--commit")

    def test_dry_run_is_default_and_writes_nothing(self):
        call_command("refresh_instrument_mappings", "--master", str(self.m46.id), "--follower", str(self.f48.id))
        self.assertEqual(InstrumentMapping.objects.count(), 0)   # no --commit => inert
        self.assertEqual(CanonicalInstrument.objects.count(), 0)

    def test_refresh_persists_and_is_idempotent(self):
        self._refresh(self.m46, self.f48)
        n1 = InstrumentMapping.objects.filter(master_account=self.m46, follower_account=self.f48).count()
        self.assertEqual(n1, 2)
        self._refresh(self.m46, self.f48)
        self.assertEqual(InstrumentMapping.objects.filter(master_account=self.m46, follower_account=self.f48).count(), n1)

    def test_one_direction_refresh_never_touches_another(self):
        self._refresh(self.m46, self.f48)
        before = {(m.master_symbol, m.follower_symbol, m.classification)
                  for m in InstrumentMapping.objects.filter(master_account=self.m46, follower_account=self.f48)}
        self._refresh(self.f48, self.m46)
        self._refresh(self.m46, self.f49)
        after = {(m.master_symbol, m.follower_symbol, m.classification)
                 for m in InstrumentMapping.objects.filter(master_account=self.m46, follower_account=self.f48)}
        self.assertEqual(before, after)
        rev = InstrumentMapping.objects.get(master_account=self.f48, follower_account=self.m46, master_symbol="XAUUSD.std")
        self.assertEqual(rev.classification, M.UNVERIFIED)

    def test_existing_symbol_map_preserved(self):
        before = (self.vmap.verified, self.vmap.verified_by_id, self.vmap.verified_at)
        self._refresh(self.m46, self.f48)
        self.vmap.refresh_from_db()
        self.assertEqual((self.vmap.verified, self.vmap.verified_by_id, self.vmap.verified_at), before)
        self.assertEqual(CopierSymbolMap.objects.count(), 1)

    def test_self_pair_refused(self):
        with self.assertRaises(CommandError):
            call_command("refresh_instrument_mappings", "--master", str(self.m46.id), "--follower", str(self.m46.id), "--commit")

    def test_cross_owner_pair_refused(self):
        other = User.objects.create_user(username="o", email="o@x.invalid", password="x")
        foreign = _acct(other, "Foreign", "77", "IS6FX", "IS6Technologies-Foreign")
        with self.assertRaises(CommandError):
            call_command("refresh_instrument_mappings", "--master", str(self.m46.id), "--follower", str(foreign.id), "--commit")
        self.assertEqual(InstrumentMapping.objects.count(), 0)

    def test_refresh_creates_no_execution_job(self):
        self.assertEqual(ExecutionJob.objects.count(), 0)
        for m, f in [(self.m46, self.f48), (self.f48, self.m46), (self.m46, self.f49),
                     (self.f49, self.m46), (self.f48, self.f49), (self.f49, self.f48)]:
            self._refresh(m, f)
        self.assertEqual(ExecutionJob.objects.count(), 0)
        self.assertTrue(CanonicalInstrument.objects.filter(key="METAL:XAU:USD").exists())
