"""Founder SHADOW configuration API: cohort gate, owner scoping, overlap/dup/tenant guards, idempotency, audit,
and the hard invariant that it can never enable execution."""
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from trade_copier import flags as cf
from trade_copier.models import CopierFollower, CopierFollowerAudit, CopierMaster
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
COHORT = dict(COPIER_UI_COHORT_ALLOWLIST="founder@guvfx.invalid")


def _acct(user, name, num, *, demo=False, broker="TradersWay", server="Tradersway-Live"):
    bs, _ = BrokerServer.objects.get_or_create(server_name=server, defaults={"broker_display_name": broker,
                                                                             "environment": "live"})
    return TradingAccount.objects.create(user=user, name=name, broker_name=broker, account_number=num,
                                         is_demo=demo, is_active=True, broker_server=bs)


class ConfigApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="founder", email="founder@guvfx.invalid", password="x")
        self.other = User.objects.create_user(username="other", email="other@x.invalid", password="x")
        self.master = _acct(self.user, "Master", "50001", broker="TradersWay", server="Tradersway-Live")
        self.foll = _acct(self.user, "Follower", "50002", broker="IS6FX", server="IS6Technologies-Live")
        self.foll2 = _acct(self.user, "Follower2", "50003", broker="IS6FX", server="IS6Technologies-LiveB")
        self.foreign = _acct(self.other, "Foreign", "59999", broker="IS6FX", server="IS6Technologies-LiveC")
        self.client.force_authenticate(self.user)
        self.url = "/api/copy-trading/config/"

    def _post(self, body):
        return self.client.post(self.url, body, format="json")

    # --- cohort gate ---
    def test_get_non_cohort_denied(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["enabled"])
        self.assertEqual(r.data["banner"], "SHADOW — EXECUTION OFF")

    @override_settings(**COHORT)
    def test_post_non_cohort_denied(self):
        self.client.force_authenticate(self.other)   # not in cohort
        r = self._post({"master_account_id": self.master.id, "followers": [{"account_id": self.foll.id}]})
        self.assertEqual(r.status_code, 403)

    @override_settings(**COHORT)
    def test_get_cohort_lists_eligible_live_accounts_and_banner(self):
        r = self.client.get(self.url)
        self.assertTrue(r.data["enabled"])
        self.assertEqual(r.data["banner"], "SHADOW — EXECUTION OFF")
        self.assertEqual(r.data["execution"], "off")
        ids = {a["account_id"] for a in r.data["eligible_accounts"]}
        self.assertEqual(ids, {self.master.id, self.foll.id, self.foll2.id})   # own LIVE accounts only, no foreign

    # --- validation / guards ---
    @override_settings(**COHORT)
    def test_master_equals_follower_rejected(self):
        r = self._post({"master_account_id": self.master.id, "followers": [{"account_id": self.master.id}]})
        self.assertEqual(r.status_code, 400)

    @override_settings(**COHORT)
    def test_duplicate_follower_rejected(self):
        r = self._post({"master_account_id": self.master.id,
                        "followers": [{"account_id": self.foll.id}, {"account_id": self.foll.id}]})
        self.assertEqual(r.status_code, 400)

    @override_settings(**COHORT)
    def test_cross_tenant_follower_not_found_no_leak(self):
        r = self._post({"master_account_id": self.master.id, "followers": [{"account_id": self.foreign.id}]})
        self.assertEqual(r.status_code, 404)

    @override_settings(**COHORT)
    def test_demo_account_rejected(self):
        demo = _acct(self.user, "Demo", "50009", demo=True, server="Demo-x")
        r = self._post({"master_account_id": demo.id, "followers": [{"account_id": self.foll.id}]})
        self.assertEqual(r.status_code, 404)   # not a LIVE account

    @override_settings(**COHORT)
    def test_fixed_lot_required_and_max_volume_bound(self):
        r = self._post({"master_account_id": self.master.id,
                        "followers": [{"account_id": self.foll.id, "sizing_mode": "FIXED_LOT",
                                       "fixed_lot": "0.10", "max_volume": "0.01"}]})
        self.assertEqual(r.status_code, 400)   # max_volume < fixed_lot

    # --- happy path + idempotency + audit ---
    @override_settings(**COHORT)
    def test_save_creates_master_and_enabled_shadow_follower_with_audit(self):
        r = self._post({"master_account_id": self.master.id,
                        "followers": [{"account_id": self.foll.id, "sizing_mode": "FIXED_LOT",
                                       "fixed_lot": "0.01", "max_volume": "0.10", "max_concurrent_positions": 1}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["audit"]["created"], [self.foll.id])
        self.assertEqual(r.data["audit"]["enabled"], [self.foll.id])
        m = CopierMaster.objects.get(trading_account=self.master)
        self.assertTrue(m.observe_enabled)
        f = CopierFollower.objects.get(master=m, follower_account=self.foll)
        self.assertTrue(f.enabled)
        self.assertEqual(f.sizing_mode, "FIXED_LOT")
        self.assertEqual(str(f.fixed_lot), "0.010")
        self.assertTrue(CopierFollowerAudit.objects.filter(follower=f, action="created").exists())

    @override_settings(**COHORT)
    def test_idempotent_repost_makes_no_change_no_new_audit(self):
        body = {"master_account_id": self.master.id,
                "followers": [{"account_id": self.foll.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]}
        self._post(body)
        n_audit = CopierFollowerAudit.objects.count()
        r2 = self._post(body)   # replay
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.data["audit"], {"created": [], "updated": [], "enabled": [], "disabled": []})
        self.assertEqual(CopierFollowerAudit.objects.count(), n_audit)   # no new audit rows on replay

    @override_settings(**COHORT)
    def test_removing_follower_disables_it_declaratively(self):
        self._post({"master_account_id": self.master.id,
                    "followers": [{"account_id": self.foll.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"},
                                  {"account_id": self.foll2.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]})
        r = self._post({"master_account_id": self.master.id,
                        "followers": [{"account_id": self.foll.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]})
        self.assertIn(self.foll2.id, r.data["audit"]["disabled"])
        self.assertFalse(CopierFollower.objects.get(follower_account=self.foll2).enabled)

    @override_settings(**COHORT)
    def test_save_never_enables_execution(self):
        self._post({"master_account_id": self.master.id,
                    "followers": [{"account_id": self.foll.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]})
        # The config write touches no execution seam: all execute flags remain OFF.
        self.assertFalse(cf.copier_execute_enabled())
        self.assertFalse(cf.copier_execute_demo_armed())
        self.assertFalse(cf.copier_enabled())
