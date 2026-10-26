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

    @override_settings(COPIER_UI_COHORT_ALLOWLIST="f2@guvfx.invalid")
    def test_all_current_no_operator_attention(self):
        # Negative control: when master + enabled follower are both current, the fail-safe flag is False.
        self.client.post(self.url, {"master_account_id": self.cur_active.id,
                                    "followers": [{"account_id": self.cur_stopped.id, "sizing_mode": "FIXED_LOT",
                                                   "fixed_lot": "0.01"}]}, format="json")
        cfg = self.client.get(self.url).data["config"]
        self.assertFalse(cfg["needs_operator_attention"])
        self.assertTrue(cfg["master_current"])

    @override_settings(COPIER_UI_COHORT_ALLOWLIST="f2@guvfx.invalid")
    def test_master_disconnected_flags_attention(self):
        from django.utils import timezone
        self.client.post(self.url, {"master_account_id": self.cur_active.id,
                                    "followers": [{"account_id": self.cur_stopped.id, "sizing_mode": "FIXED_LOT",
                                                   "fixed_lot": "0.01"}]}, format="json")
        self.cur_active.disconnected_at = timezone.now(); self.cur_active.save(update_fields=["disconnected_at"])
        cfg = self.client.get(self.url).data["config"]
        self.assertTrue(cfg["needs_operator_attention"])
        self.assertFalse(cfg["master_current"])

    @override_settings(COPIER_UI_COHORT_ALLOWLIST="f2@guvfx.invalid")
    def test_disabled_disconnected_follower_does_not_flag(self):
        from django.utils import timezone
        # Configure two followers, then de-select foll2 (disabled), then disconnect the DISABLED one.
        self.foll2 = _acct(self.user, "F2", "60005", active=True, server="S-g")
        self.client.post(self.url, {"master_account_id": self.cur_active.id, "followers": [
            {"account_id": self.cur_stopped.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"},
            {"account_id": self.foll2.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]}, format="json")
        self.client.post(self.url, {"master_account_id": self.cur_active.id, "followers": [
            {"account_id": self.cur_stopped.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]}, format="json")
        self.foll2.disconnected_at = timezone.now(); self.foll2.save(update_fields=["disconnected_at"])
        cfg = self.client.get(self.url).data["config"]
        self.assertFalse(cfg["needs_operator_attention"])   # a DISABLED disconnected follower is not participating


class PerFollowerSizingConstraintTests(APITestCase):
    """Workstream A/B: each follower's sizing is validated against THAT follower's OWN broker-observed symbol
    constraints; invalid volume (below-min/above-max/off-step) is rejected; a missing map or missing symbol spec
    fails CLOSED (config 'blocked', never a silent accept and never a hard save error); sizing is per-follower and
    never cross-applied; and execution stays OFF throughout."""

    def setUp(self):
        from django.utils import timezone
        from trade_copier.models import CopierSymbolMap
        from execution.models import BrokerInstrument
        self.user = User.objects.create_user(username="founder", email="founder@guvfx.invalid", password="x")
        self.master = _acct(self.user, "Master", "70001", broker="TradersWay", server="Tradersway-Live")
        # Follower A — cross-broker, verified map + synced constraints (min 0.01 / step 0.01 / max 30), like #48.
        self.fa = _acct(self.user, "FollowerA", "70002", broker="IS6FX", server="IS6Technologies-Live")
        CopierSymbolMap.objects.create(master_broker="Tradersway-Live", master_symbol="XAUUSD",
                                       follower_broker="IS6Technologies-Live", follower_symbol="XAUUSD.std",
                                       verified=True, verified_at=timezone.now())
        BrokerInstrument.objects.create(account=self.fa, broker_symbol="XAUUSD.std", base_symbol="XAUUSD",
                                        metadata={"volume_min": 0.01, "volume_step": 0.01, "volume_max": 30.0,
                                                  "contract_size": 100.0, "currency_profit": "USD"})
        # Follower C — cross-broker, verified map + constraints with min 1.00 / step 0.1 (mirrors Pepperstone
        # XAUUSD_SB once it WOULD be synced): proves below-min / off-step rejection with real broker numbers.
        self.fc = _acct(self.user, "FollowerC", "70004", broker="Pepperstone", server="PepperstoneUK-LiveC")
        CopierSymbolMap.objects.create(master_broker="Tradersway-Live", master_symbol="XAUUSD",
                                       follower_broker="PepperstoneUK-LiveC", follower_symbol="XAUUSD_SB",
                                       verified=True, verified_at=timezone.now())
        BrokerInstrument.objects.create(account=self.fc, broker_symbol="XAUUSD_SB", base_symbol="XAUUSD",
                                        metadata={"volume_min": 1.0, "volume_step": 0.1, "volume_max": 10000.0,
                                                  "contract_size": 1.0, "currency_profit": "GBP"})
        # Follower B — cross-broker, NO map and NO constraints at all (the actual production #49 today).
        self.fb = _acct(self.user, "FollowerB", "70003", broker="Pepperstone", server="PepperstoneUK-Live")
        # Follower D — verified map but NO synced BrokerInstrument (map present, spec missing).
        self.fd = _acct(self.user, "FollowerD", "70005", broker="IS6FX", server="IS6Technologies-LiveD")
        CopierSymbolMap.objects.create(master_broker="Tradersway-Live", master_symbol="XAUUSD",
                                       follower_broker="IS6Technologies-LiveD", follower_symbol="XAUUSD.std",
                                       verified=True, verified_at=timezone.now())
        self.client.force_authenticate(self.user)
        self.url = "/api/copy-trading/config/"

    def _post(self, body):
        return self.client.post(self.url, body, format="json")

    def _pair(self, follower_id):
        cfg = self.client.get(self.url).data["config"]
        return next(p for p in cfg["followers"] if p["follower_account_id"] == follower_id)

    # --- valid sizing against observed constraints ---
    @override_settings(**COHORT)
    def test_valid_lot_against_constraints_is_config_ready(self):
        r = self._post({"master_account_id": self.master.id,
                        "followers": [{"account_id": self.fa.id, "sizing_mode": "FIXED_LOT",
                                       "fixed_lot": "0.01", "max_volume": "0.10"}]})
        self.assertEqual(r.status_code, 200, r.data)
        p = self._pair(self.fa.id)
        self.assertEqual(p["config_status"], "ready")
        self.assertIsNone(p["config_blocked_reason"])
        self.assertEqual(p["instrument_map"]["state"], "verified")
        self.assertEqual(p["instrument_map"]["pairs"][0]["follower_symbol"], "XAUUSD.std")
        self.assertTrue(p["instrument_map"]["last_verified_at"])
        self.assertTrue(p["observed_constraints"]["available"])
        self.assertEqual(p["observed_constraints"]["symbols"]["XAUUSD.std"]["volume_min"], 0.01)

    # --- invalid volume rejected (below-min / off-step / above-max) ---
    @override_settings(**COHORT)
    def test_below_min_volume_rejected(self):
        for bad in ("0.01", "0.5"):   # both below the 1.00 minimum on fc
            r = self._post({"master_account_id": self.master.id,
                            "followers": [{"account_id": self.fc.id, "sizing_mode": "FIXED_LOT", "fixed_lot": bad}]})
            self.assertEqual(r.status_code, 400, (bad, r.data))
            self.assertIn("below", r.data["detail"])

    @override_settings(**COHORT)
    def test_off_step_volume_rejected(self):
        r = self._post({"master_account_id": self.master.id,
                        "followers": [{"account_id": self.fc.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "1.05"}]})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("step", r.data["detail"])

    @override_settings(**COHORT)
    def test_above_max_volume_rejected(self):
        r = self._post({"master_account_id": self.master.id,
                        "followers": [{"account_id": self.fa.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "50"}]})
        self.assertEqual(r.status_code, 400, r.data)   # 50 <= parse ceiling 1000, but exceeds XAUUSD.std max 30
        self.assertIn("maximum", r.data["detail"])

    @override_settings(**COHORT)
    def test_max_volume_above_broker_max_rejected(self):
        r = self._post({"master_account_id": self.master.id,
                        "followers": [{"account_id": self.fa.id, "sizing_mode": "FIXED_LOT",
                                       "fixed_lot": "0.01", "max_volume": "500"}]})
        self.assertEqual(r.status_code, 400, r.data)   # 500 exceeds XAUUSD.std max 30
        self.assertIn("max_volume", r.data["detail"])

    @override_settings(**COHORT)
    def test_on_step_minimum_accepted_on_pepperstone_like(self):
        for good in ("1.0", "1.2", "2.0"):
            r = self._post({"master_account_id": self.master.id,
                            "followers": [{"account_id": self.fc.id, "sizing_mode": "FIXED_LOT", "fixed_lot": good}]})
            self.assertEqual(r.status_code, 200, (good, r.data))
        self.assertEqual(self._pair(self.fc.id)["config_status"], "ready")

    # --- fail-closed: missing map / missing spec / unverified map ---
    @override_settings(**COHORT)
    def test_missing_map_fails_closed_not_rejected(self):
        r = self._post({"master_account_id": self.master.id,
                        "followers": [{"account_id": self.fb.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]})
        self.assertEqual(r.status_code, 200, r.data)   # saved as SHADOW; not a hard error
        p = self._pair(self.fb.id)
        self.assertEqual(p["config_status"], "blocked")
        self.assertEqual(p["config_blocked_reason"], "no_symbol_map")
        self.assertEqual(p["instrument_map"]["state"], "missing")
        self.assertFalse(p["observed_constraints"]["available"])

    @override_settings(**COHORT)
    def test_missing_symbol_spec_fails_closed(self):
        r = self._post({"master_account_id": self.master.id,
                        "followers": [{"account_id": self.fd.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]})
        self.assertEqual(r.status_code, 200, r.data)
        p = self._pair(self.fd.id)
        self.assertEqual(p["config_status"], "blocked")
        self.assertEqual(p["config_blocked_reason"], "follower_instruments_unsynced")
        self.assertEqual(p["instrument_map"]["state"], "verified")   # map IS verified; the SPEC is what is missing
        self.assertFalse(p["observed_constraints"]["available"])

    @override_settings(**COHORT)
    def test_unverified_map_fails_closed(self):
        from django.utils import timezone
        from trade_copier.models import CopierSymbolMap
        fe = _acct(self.user, "FollowerE", "70006", broker="IS6FX", server="IS6Technologies-LiveE")
        CopierSymbolMap.objects.create(master_broker="Tradersway-Live", master_symbol="XAUUSD",
                                       follower_broker="IS6Technologies-LiveE", follower_symbol="XAUUSD.std",
                                       verified=False)
        r = self._post({"master_account_id": self.master.id,
                        "followers": [{"account_id": fe.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]})
        self.assertEqual(r.status_code, 200, r.data)
        p = self._pair(fe.id)
        self.assertEqual(p["config_status"], "blocked")
        self.assertEqual(p["config_blocked_reason"], "map_present_but_unverified")
        self.assertEqual(p["instrument_map"]["state"], "unverified")

    # --- per-follower isolation: one follower's sizing is never applied to another ---
    @override_settings(**COHORT)
    def test_per_follower_sizing_is_isolated(self):
        self._post({"master_account_id": self.master.id, "followers": [
            {"account_id": self.fa.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01", "max_volume": "0.10"},
            {"account_id": self.fc.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "1.0", "max_volume": "5.0"}]})
        self.assertEqual(self._pair(self.fa.id)["fixed_lot"], "0.010")
        self.assertEqual(self._pair(self.fc.id)["fixed_lot"], "1.000")
        # Edit ONLY fc; fa must be untouched (no silent cross-application).
        self._post({"master_account_id": self.master.id, "followers": [
            {"account_id": self.fa.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01", "max_volume": "0.10"},
            {"account_id": self.fc.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "1.2", "max_volume": "5.0"}]})
        self.assertEqual(self._pair(self.fa.id)["fixed_lot"], "0.010")   # unchanged
        self.assertEqual(self._pair(self.fc.id)["fixed_lot"], "1.200")   # only fc changed

    # --- execution stays OFF throughout (compact status, no flag flipped, no copier job) ---
    @override_settings(**COHORT)
    def test_execution_status_is_compact_and_off(self):
        d = self.client.get(self.url).data
        self.assertEqual(d["execution_status"]["execution"], "off")
        self.assertEqual(d["execution_status"]["label"], "Observation mode")
        self.assertEqual(d["execution_status"]["detail"], "Copy execution off")
        self._post({"master_account_id": self.master.id,
                    "followers": [{"account_id": self.fa.id, "sizing_mode": "FIXED_LOT", "fixed_lot": "0.01"}]})
        self.assertFalse(any([cf.copier_enabled(), cf.copier_observer_enabled(), cf.copier_replication_enabled(),
                              cf.copier_execute_enabled(), cf.copier_execute_demo_armed()]))
        self.assertEqual(ExecutionJob.objects.filter(job_type__icontains="copier").count(), 0)
