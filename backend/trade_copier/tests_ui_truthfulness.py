"""Copy Trading UI truthfulness (ADR-0064) — two regression guards so the monitoring surfaces can never
read as more ready than they are:

  1. The execution status + banner are DERIVED LIVE from the execute flags, NEVER a hardcoded constant — so
     the instant a Sponsor arms the DEMO producer the UI STOPS asserting "EXECUTION OFF" (adversarial). The
     default-OFF output is asserted byte-identical to the historical constants so nothing changes today.
  2. The read-only monitoring OVERVIEW carries the SAME fail-closed per-follower sizing readiness as the
     founder config page, so a follower whose configured lot its broker cannot execute (0.01 below a 1.0
     volume_min — the Pepperstone case) is shown "not execution-ready" there too, not as a bare sizing line.

Execution stays OFF throughout: these are read-only reads that create no ExecutionJob.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from execution.models import ExecutionJob
from trade_copier import flags
from trade_copier.models import CopierFollower, CopierMaster, CopierSymbolMap
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
COHORT = dict(COPIER_UI_COHORT_ALLOWLIST="founder@guvfx.invalid")


class ExecutionStatusFlagTests(SimpleTestCase):
    """Gap 1 — the status/banner reflect the LIVE execute flags, not a constant."""

    def test_off_by_default_is_byte_identical_to_the_historical_constant(self):
        st = flags.execution_status()
        self.assertEqual(st["mode"], "observation")
        self.assertEqual(st["execution"], "off")
        self.assertEqual(st["label"], "Observation mode")
        self.assertEqual(st["detail"], "Copy execution off")
        self.assertEqual(st["note"], "Saving configuration does not activate trading.")
        self.assertEqual(st["live_follower_execution"], "blocked")
        self.assertFalse(st["execute_enabled"])
        self.assertFalse(st["demo_armed"])
        self.assertEqual(flags.execution_banner(), "SHADOW — EXECUTION OFF")

    def test_note_is_surface_specific(self):
        self.assertEqual(flags.execution_status(note="catalogue note")["note"], "catalogue note")

    @override_settings(COPIER_ENABLED="1", COPIER_EXECUTE_ENABLED="1")
    def test_execute_enabled_without_arm_is_shadow_jobs_not_off(self):
        st = flags.execution_status()
        self.assertEqual(st["execution"], "shadow_jobs")
        self.assertTrue(st["execute_enabled"])
        self.assertFalse(st["demo_armed"])
        self.assertEqual(flags.execution_banner(), "SHADOW JOBS — NO ORDER SENT")
        self.assertNotIn("EXECUTION OFF", flags.execution_banner())

    @override_settings(COPIER_ENABLED="1", COPIER_EXECUTE_ENABLED="1", COPIER_EXECUTE_DEMO_ARMED="1")
    def test_demo_armed_stops_saying_execution_off(self):
        st = flags.execution_status()
        self.assertEqual(st["execution"], "demo_armed")
        self.assertTrue(st["demo_armed"])
        banner = flags.execution_banner()
        self.assertNotIn("EXECUTION OFF", banner)   # the whole point: the constant would still lie here
        self.assertIn("DEMO", banner)
        # LIVE follower execution is NEVER reachable from the copier, whatever the flags say.
        self.assertEqual(st["live_follower_execution"], "blocked")

    @override_settings(COPIER_EXECUTE_ENABLED="1")   # master gate OFF ⇒ ANDed off
    def test_execute_without_master_gate_stays_off(self):
        self.assertEqual(flags.execution_status()["execution"], "off")
        self.assertEqual(flags.execution_banner(), "SHADOW — EXECUTION OFF")


def _acct(user, name, num, *, broker, server):
    bs, _ = BrokerServer.objects.get_or_create(
        server_name=server, defaults={"broker_display_name": broker, "environment": "live"})
    return TradingAccount.objects.create(user=user, name=name, broker_name=broker, account_number=num,
                                         is_demo=False, is_active=False, broker_server=bs)


class OverviewFollowerReadinessTests(APITestCase):
    """Gap 2 — the monitoring overview tells the same per-follower sizing truth as the config page."""

    def setUp(self):
        self.user = User.objects.create_user(username="founder", email="founder@guvfx.invalid", password="x")
        self.master_acct = _acct(self.user, "Master", "80001", broker="TradersWay", server="Tradersway-Live")
        self.master = CopierMaster.objects.create(trading_account=self.master_acct, observe_enabled=True)
        # A verified cross-broker map for both followers (same master symbol, different follower broker).
        for fb in ("IS6Technologies-Live", "PepperstoneUK-LiveC"):
            fsym = "XAUUSD.std" if "IS6" in fb else "XAUUSD_SB"
            CopierSymbolMap.objects.create(master_broker="Tradersway-Live", master_symbol="XAUUSD",
                                           follower_broker=fb, follower_symbol=fsym,
                                           verified=True, verified_at=timezone.now())
        # Healthy follower: 0.01 is valid on XAUUSD.std (vmin 0.01).
        self.ok = _acct(self.user, "FollowerOK", "80002", broker="IS6FX", server="IS6Technologies-Live")
        self._instrument(self.ok, "XAUUSD.std", vmin=0.01, vstep=0.01, vmax=30.0, csize=100.0)
        self.ok_follower = CopierFollower.objects.create(
            master=self.master, follower_account=self.ok, enabled=True,
            sizing_mode="FIXED_LOT", fixed_lot=Decimal("0.01"), max_volume=Decimal("0.10"))
        # Unexecutable follower: 0.01 is BELOW the 1.0 volume_min (the Pepperstone XAUUSD_SB case).
        self.bad = _acct(self.user, "FollowerBad", "80003", broker="Pepperstone", server="PepperstoneUK-LiveC")
        self._instrument(self.bad, "XAUUSD_SB", vmin=1.0, vstep=0.1, vmax=10000.0, csize=1.0)
        self.bad_follower = CopierFollower.objects.create(
            master=self.master, follower_account=self.bad, enabled=True,
            sizing_mode="FIXED_LOT", fixed_lot=Decimal("0.01"), max_volume=Decimal("0.10"))
        self.client.force_authenticate(self.user)
        self.url = f"/api/copy-trading/accounts/{self.master_acct.id}/overview/"

    def _instrument(self, account, symbol, *, vmin, vstep, vmax, csize):
        from execution.models import BrokerInstrument
        BrokerInstrument.objects.create(
            account=account, broker_symbol=symbol, base_symbol="XAUUSD",
            metadata={"volume_min": vmin, "volume_step": vstep, "volume_max": vmax,
                      "contract_size": csize, "currency_profit": "USD", "trade_mode": 4})

    def _follower(self, data, account_id):
        return next(f for f in data["followers"] if f["follower_account_id"] == account_id)

    @override_settings(**COHORT)
    def test_overview_marks_below_min_follower_not_execution_ready(self):
        before = ExecutionJob.objects.count()
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200, r.data)
        bad = self._follower(r.data, self.bad.id)
        self.assertEqual(bad["config_status"], "blocked")
        self.assertTrue((bad["config_blocked_reason"] or "").startswith("sizing_invalid:"),
                        bad["config_blocked_reason"])
        self.assertIn("below", bad["config_blocked_reason"])   # the precise broker reason is preserved
        # The healthy follower is shown ready (0.01 is valid on XAUUSD.std).
        ok = self._follower(r.data, self.ok.id)
        self.assertEqual(ok["config_status"], "ready")
        self.assertIsNone(ok["config_blocked_reason"])
        # Read-only: the overview created no execution job and the banner/status are flag-driven + OFF.
        self.assertEqual(ExecutionJob.objects.count(), before)
        self.assertEqual(r.data["banner"], "SHADOW — EXECUTION OFF")
        self.assertEqual(r.data["execution_status"]["execution"], "off")

    @override_settings(**COHORT)
    def test_overview_reports_follower_current_flag(self):
        r = self.client.get(self.url)
        self.assertTrue(self._follower(r.data, self.ok.id)["follower_current"])
