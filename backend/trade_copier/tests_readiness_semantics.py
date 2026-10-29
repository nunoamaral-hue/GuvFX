"""Readiness-semantics repair: the eligible-account picker exposes FRESHNESS (never copy-readiness), and each
configured pair exposes SEPARATED readiness dimensions + a fail-closed copy-readiness roll-up. Terminal/snapshot
freshness alone must never imply copy-readiness; a pair is copy-ready only when every copy dimension holds."""
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from trade_copier.models import CopierSymbolMap
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
COHORT = dict(COPIER_UI_COHORT_ALLOWLIST="founder@guvfx.invalid")


def _acct(user, name, num, *, broker, server):
    bs, _ = BrokerServer.objects.get_or_create(
        server_name=server, defaults={"broker_display_name": broker, "environment": "live"})
    return TradingAccount.objects.create(user=user, name=name, broker_name=broker, account_number=num,
                                         is_demo=False, is_active=False, broker_server=bs)


class ReadinessSemanticsTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="founder", email="founder@guvfx.invalid", password="x")
        self.master = _acct(self.user, "M", "90001", broker="TradersWay", server="Tradersway-Live")
        # Ready follower: verified map + synced constraints (0.01 valid).
        self.ok = _acct(self.user, "OK", "90002", broker="IS6FX", server="IS6Technologies-Live")
        CopierSymbolMap.objects.create(master_broker="Tradersway-Live", master_symbol="XAUUSD",
                                       follower_broker="IS6Technologies-Live", follower_symbol="XAUUSD.std",
                                       verified=True, verified_at=timezone.now())
        from execution.models import BrokerInstrument
        BrokerInstrument.objects.create(account=self.ok, broker_symbol="XAUUSD.std", base_symbol="XAUUSD",
                                        metadata={"volume_min": 0.01, "volume_step": 0.01, "volume_max": 30.0,
                                                  "contract_size": 100.0, "currency_profit": "USD"})
        # Blocked follower: no verified map for its broker pair.
        self.nomap = _acct(self.user, "NM", "90003", broker="Pepperstone", server="PepperstoneUK-LiveZ")
        self.client.force_authenticate(self.user)
        self.url = "/api/copy-trading/config/"

    def _pair(self, fid):
        cfg = self.client.get(self.url).data["config"]
        return next(p for p in cfg["followers"] if p["follower_account_id"] == fid)

    @override_settings(**COHORT)
    def test_eligible_picker_exposes_freshness_not_copy_readiness(self):
        r = self.client.get(self.url)
        accts = r.data["eligible_accounts"]
        self.assertTrue(accts)
        for a in accts:
            self.assertIn("data_fresh", a)
            self.assertEqual(a.get("readiness_scope"), "monitoring_freshness_only")

    @override_settings(**COHORT)
    def test_ready_pair_dimensions_but_not_shadow_without_live_data(self):
        r = self.client.post(self.url, {"master_account_id": self.master.id, "followers": [
            {"account_id": self.ok.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01", "max_volume": "0.10"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        p = self._pair(self.ok.id)
        self.assertEqual(p["config_status"], "ready")
        rd = p["readiness"]
        self.assertTrue(rd["mapping_verified"]); self.assertTrue(rd["constraints_available"])
        self.assertTrue(rd["sizing_valid"]); self.assertTrue(rd["direction_mapped"])
        self.assertTrue(rd["live_blocked"])
        # No equity snapshot in test => snapshot not fresh => NOT shadow-ready (freshness alone ≠ readiness, and
        # readiness requires fresh live data). This is the "config valid — awaiting live data" state.
        self.assertFalse(rd["snapshot_fresh"]); self.assertFalse(rd["shadow_ready"])

    @override_settings(**COHORT)
    def test_blocked_pair_not_shadow_ready_with_precise_reason(self):
        r = self.client.post(self.url, {"master_account_id": self.master.id, "followers": [
            {"account_id": self.nomap.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)   # saved as SHADOW config; not a hard error
        p = self._pair(self.nomap.id)
        self.assertEqual(p["config_status"], "blocked")
        self.assertEqual(p["config_blocked_reason"], "no_symbol_map")
        rd = p["readiness"]
        self.assertFalse(rd["mapping_verified"]); self.assertFalse(rd["sizing_valid"])
        self.assertFalse(rd["shadow_ready"]); self.assertTrue(rd["live_blocked"])
