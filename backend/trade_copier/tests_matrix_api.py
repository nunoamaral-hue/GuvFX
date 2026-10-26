"""Read-only instrument-matrix API: cohort gate, owner scoping, counts/coverage, drill-down, execution-off."""
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from execution.models import BrokerInstrument, ExecutionJob
from trade_copier.models import CopierSymbolMap
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
COHORT = dict(COPIER_UI_COHORT_ALLOWLIST="founder@guvfx.invalid")
URL = "/api/copy-trading/instrument-matrix/"

_XAU_USD = {"contract_size": 100.0, "currency_base": "XAU", "currency_profit": "USD",
            "volume_min": 0.01, "volume_step": 0.01, "volume_max": 100.0, "trade_mode": 4}
_XAU_STD = {"contract_size": 100.0, "currency_base": "USD", "currency_profit": "USD",
            "volume_min": 0.01, "volume_step": 0.01, "volume_max": 30.0, "trade_mode": 4}
_XAU_SB = {"contract_size": 1.0, "currency_base": "GBP", "currency_profit": "GBP",
           "volume_min": 1.0, "volume_step": 0.1, "volume_max": 10000.0, "trade_mode": 4, "path": "SB\\XAUUSD_SB"}


def _acct(user, name, num, broker, server):
    bs, _ = BrokerServer.objects.get_or_create(
        server_name=server, defaults={"broker_display_name": broker, "environment": "live"})
    return TradingAccount.objects.create(user=user, name=name, broker_name=broker, account_number=num,
                                         is_demo=False, is_active=True, broker_server=bs)


def _bi(account, symbol, base, meta):
    return BrokerInstrument.objects.create(account=account, broker_symbol=symbol, base_symbol=base,
                                           enabled=True, metadata=meta)


class MatrixApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="founder", email="founder@guvfx.invalid", password="x")
        self.other = User.objects.create_user(username="other", email="other@x.invalid", password="x")
        self.m46 = _acct(self.user, "TradersWay", "46", "TradersWay", "Tradersway-Live")
        self.f48 = _acct(self.user, "IS6FX", "48", "IS6FX", "IS6Technologies-Live")
        self.f49 = _acct(self.user, "Pepperstone", "49", "Pepperstone", "PepperstoneUK-Live")
        self.foreign = _acct(self.other, "Foreign", "99", "IS6FX", "IS6Technologies-Foreign")
        _bi(self.m46, "XAUUSD", "XAUUSD", _XAU_USD)
        _bi(self.f48, "XAUUSD.std", "XAUUSD", _XAU_STD)
        _bi(self.f49, "XAUUSD_SB", "XAUUSD", _XAU_SB)
        _bi(self.foreign, "XAUUSD.std", "XAUUSD", _XAU_STD)
        CopierSymbolMap.objects.create(
            master_broker="Tradersway-Live", master_symbol="XAUUSD",
            follower_broker="IS6Technologies-Live", follower_symbol="XAUUSD.std",
            verified=True, verified_by=self.user, verified_at=timezone.now())
        self.client.force_authenticate(self.user)

    def test_non_cohort_denied_quietly(self):
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["enabled"])
        self.assertEqual(r.data["directions"], [])

    @override_settings(**COHORT)
    def test_cohort_sees_six_directions_and_quiet_status(self):
        r = self.client.get(URL)
        self.assertTrue(r.data["enabled"])
        self.assertEqual(len(r.data["directions"]), 6)   # 3 own accounts, ordered pairs
        es = r.data["execution_status"]
        self.assertEqual((es["mode"], es["execution"]), ("observation", "off"))
        self.assertIn("not execution readiness", es["note"])

    @override_settings(**COHORT)
    def test_counts_and_coverage_for_verified_pair(self):
        r = self.client.get(URL + f"?master={self.m46.id}&follower={self.f48.id}")
        self.assertEqual(len(r.data["directions"]), 1)
        d = r.data["directions"][0]
        self.assertEqual(d["counts"]["VERIFIED"], 1)
        self.assertEqual(d["coverage_pct"], 100.0)   # 1 verified of 1 eligible source
        syms = {c["master_symbol"]: c for c in d["candidates"]}
        self.assertEqual(syms["XAUUSD"]["classification"], "VERIFIED")
        self.assertIsNotNone(syms["XAUUSD"]["verified_at"])

    @override_settings(**COHORT)
    def test_spread_bet_direction_ambiguous(self):
        r = self.client.get(URL + f"?master={self.m46.id}&follower={self.f49.id}")
        d = r.data["directions"][0]
        c = next(x for x in d["candidates"] if x["master_symbol"] == "XAUUSD")
        self.assertEqual(c["classification"], "AMBIGUOUS")
        self.assertTrue(c["material_differences"]["settlement_currency"]["differs"])

    @override_settings(**COHORT)
    def test_owner_scoped_foreign_account_absent(self):
        r = self.client.get(URL)
        acct_ids = {a["account_id"] for a in r.data["accounts"]}
        self.assertEqual(acct_ids, {self.m46.id, self.f48.id, self.f49.id})
        self.assertNotIn(self.foreign.id, acct_ids)
        for d in r.data["directions"]:
            self.assertNotIn(self.foreign.id, (d["master_account_id"], d["follower_account_id"]))

    @override_settings(**COHORT)
    def test_read_only_creates_no_execution_job(self):
        self.client.get(URL)
        self.client.get(URL + f"?master={self.m46.id}&follower={self.f49.id}")
        self.assertEqual(ExecutionJob.objects.count(), 0)
