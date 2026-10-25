"""Founder SHADOW configuration API: cohort gate, owner scoping, overlap/dup/tenant guards, idempotency, audit,
and the hard invariant that it can never enable execution."""
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from execution.models import ExecutionJob
from trade_copier import flags as cf
from trade_copier.models import CopierFollower, CopierFollowerAudit, CopierMaster
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
COHORT = dict(COPIER_UI_COHORT_ALLOWLIST="founder@guvfx.invalid")


def _acct(user, name, num, *, demo=False, broker="TradersWay", server="Tradersway-Live",
          active=True, disconnected=False):
    from django.utils import timezone
    bs, _ = BrokerServer.objects.get_or_create(server_name=server, defaults={"broker_display_name": broker,
                                                                             "environment": "live"})
    return TradingAccount.objects.create(user=user, name=name, broker_name=broker, account_number=num,
                                         is_demo=demo, is_active=active, broker_server=bs,
                                         disconnected_at=(timezone.now() if disconnected else None))


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
        self.assertEqual(r2.data["audit"],
                         {"created": [], "updated": [], "enabled": [], "disabled": [], "demoted_masters": []})
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
        jobs_before = ExecutionJob.objects.count()
        r = self._post({"master_account_id": self.master.id,
                        "followers": [{"account_id": self.foll.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]})
        self.assertEqual(r.status_code, 200)
        # No execution seam is touched: flags stay OFF AND — the discriminating part — NO ExecutionJob is created
        # by a config save (a follower is SHADOW-enabled only).
        self.assertFalse(cf.copier_execute_enabled())
        self.assertFalse(cf.copier_execute_demo_armed())
        self.assertFalse(cf.copier_enabled())
        self.assertEqual(ExecutionJob.objects.count(), jobs_before)   # zero jobs produced

    @override_settings(**COHORT)
    def test_foreign_master_not_found_no_leak(self):
        r = self._post({"master_account_id": self.foreign.id, "followers": [{"account_id": self.foll.id}]})
        self.assertEqual(r.status_code, 404)

    @override_settings(**COHORT)
    def test_nan_infinity_and_huge_sizing_rejected_400(self):
        for bad in ("NaN", "Infinity", "1e30"):
            r = self._post({"master_account_id": self.master.id,
                            "followers": [{"account_id": self.foll.id, "sizing_mode": "FIXED_LOT", "fixed_lot": bad}]})
            self.assertEqual(r.status_code, 400, f"{bad} should be a clean 400, not a 500")
        r2 = self._post({"master_account_id": self.master.id,
                         "followers": [{"account_id": self.foll.id, "sizing_mode": "FIXED_LOT",
                                        "fixed_lot": "0.01", "max_concurrent_positions": 2147483648}]})
        self.assertEqual(r2.status_code, 400)

    @override_settings(**COHORT)
    def test_single_master_enforced_on_switch_breaks_stale_and_circular(self):
        # First designate master A(=self.master) -> follower B(=foll2). Then switch to master B -> follower A.
        self._post({"master_account_id": self.master.id,
                    "followers": [{"account_id": self.foll2.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]})
        r = self._post({"master_account_id": self.foll2.id,
                        "followers": [{"account_id": self.master.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]})
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.master.id, r.data["audit"]["demoted_masters"])
        # Old master A is demoted (no longer observing) and its follower B is disabled — no stale/circular config.
        self.assertFalse(CopierMaster.objects.get(trading_account=self.master).observe_enabled)
        old_follow = CopierFollower.objects.get(master__trading_account=self.master, follower_account=self.foll2)
        self.assertFalse(old_follow.enabled)
        # Exactly one active master remains, and it is B.
        self.assertEqual(CopierMaster.objects.filter(observe_enabled=True).count(), 1)
        self.assertEqual(r.data["config"]["master_account_id"], self.foll2.id)


class EligibilityTests(APITestCase):
    """Current-account eligibility: tombstones (disconnected) excluded; current accounts (incl. stopped/is_active=
    False) offered; the predicate is disconnected_at-based, never is_active-based (prod current accounts are all
    stopped). Applied consistently to GET eligibility + POST master/follower validation."""
    def setUp(self):
        self.user = User.objects.create_user(username="f2", email="f2@guvfx.invalid", password="x")
        self.other = User.objects.create_user(username="o2", email="o2@x.invalid", password="x")
        self.cur_active = _acct(self.user, "CurActive", "60001", active=True, server="S-a")
        self.cur_stopped = _acct(self.user, "CurStopped", "60002", active=False, server="S-b")
        self.disc = _acct(self.user, "Tomb", "60003", active=True, disconnected=True, server="S-c")
        self.demo = _acct(self.user, "Demo", "60004", demo=True, server="S-d")
        self.foreign = _acct(self.other, "Foreign", "60009", active=True, server="S-f")
        self.client.force_authenticate(self.user)
        self.url = "/api/copy-trading/config/"

    @override_settings(COPIER_UI_COHORT_ALLOWLIST="f2@guvfx.invalid")
    def test_get_offers_only_owner_current_live_accounts(self):
        r = self.client.get(self.url)
        ids = sorted(a["account_id"] for a in r.data["eligible_accounts"])
        self.assertEqual(ids, sorted([self.cur_active.id, self.cur_stopped.id]))

    @override_settings(COPIER_UI_COHORT_ALLOWLIST="f2@guvfx.invalid")
    def test_current_stopped_account_is_eligible(self):
        ids = {a["account_id"] for a in self.client.get(self.url).data["eligible_accounts"]}
        self.assertIn(self.cur_stopped.id, ids)

    @override_settings(COPIER_UI_COHORT_ALLOWLIST="f2@guvfx.invalid")
    def test_disconnected_tombstone_excluded_even_if_active_true(self):
        ids = {a["account_id"] for a in self.client.get(self.url).data["eligible_accounts"]}
        self.assertNotIn(self.disc.id, ids)

    @override_settings(COPIER_UI_COHORT_ALLOWLIST="f2@guvfx.invalid")
    def test_post_rejects_disconnected_master(self):
        r = self.client.post(self.url, {"master_account_id": self.disc.id,
                                        "followers": [{"account_id": self.cur_active.id}]}, format="json")
        self.assertEqual(r.status_code, 404)

    @override_settings(COPIER_UI_COHORT_ALLOWLIST="f2@guvfx.invalid")
    def test_post_rejects_disconnected_follower(self):
        r = self.client.post(self.url, {"master_account_id": self.cur_active.id,
                                        "followers": [{"account_id": self.disc.id, "sizing_mode": "FIXED_LOT",
                                                       "fixed_lot": "0.01"}]}, format="json")
        self.assertEqual(r.status_code, 404)

    @override_settings(COPIER_UI_COHORT_ALLOWLIST="f2@guvfx.invalid")
    def test_post_rejects_foreign_and_demo(self):
        r1 = self.client.post(self.url, {"master_account_id": self.foreign.id,
                                         "followers": [{"account_id": self.cur_active.id}]}, format="json")
        self.assertEqual(r1.status_code, 404)
        r2 = self.client.post(self.url, {"master_account_id": self.cur_active.id,
                                         "followers": [{"account_id": self.demo.id}]}, format="json")
        self.assertEqual(r2.status_code, 404)

    @override_settings(COPIER_UI_COHORT_ALLOWLIST="f2@guvfx.invalid")
    def test_config_flags_operator_attention_when_configured_account_later_disconnected(self):
        from django.utils import timezone
        self.client.post(self.url, {"master_account_id": self.cur_active.id,
                                    "followers": [{"account_id": self.cur_stopped.id, "sizing_mode": "FIXED_LOT",
                                                   "fixed_lot": "0.01"}]}, format="json")
        self.cur_stopped.disconnected_at = timezone.now(); self.cur_stopped.save(update_fields=["disconnected_at"])
        cfg = self.client.get(self.url).data["config"]
        self.assertTrue(cfg["needs_operator_attention"])
        fv = next(f for f in cfg["followers"] if f["follower_account_id"] == self.cur_stopped.id)
        self.assertFalse(fv["follower_current"])
