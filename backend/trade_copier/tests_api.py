"""Read-only Copy Trading API certification: owner scoping, DARK gating, honest states (PR C)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from trade_copier import services
from trade_copier.models import (
    CopierMaster,
    CopierReplication,
    MasterEvent,
    MasterObservationPoll,
    ReplicationStatus,
)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()


class CopyTradingApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner", email="o@x.invalid", password="x")
        self.other = User.objects.create_user(username="other", email="ot@x.invalid", password="x")
        bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-Demo",
                                         environment="demo")
        self.master_acct = TradingAccount.objects.create(
            user=self.user, name="m", broker_name="IS6", account_number="1000001",
            is_demo=True, is_active=True, broker_server=bs)
        self.master = CopierMaster.objects.create(trading_account=self.master_acct, observe_enabled=True)
        fbs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-DemoF",
                                          environment="demo")
        self.follower_acct = TradingAccount.objects.create(
            user=self.user, name="f", broker_name="IS6", account_number="2000001",
            is_demo=True, is_active=True, broker_server=fbs)
        self.follower = services.add_follower(self.master, self.follower_acct)
        poll = MasterObservationPoll.objects.create(master=self.master, observed_at=timezone.now(),
                                                    read_at=timezone.now(), position_count=1)
        self.event = MasterEvent.objects.create(
            master=self.master, sequence=1, from_poll=poll, event_type=MasterEvent.EventType.OPEN,
            position_ticket=1, symbol="XAUUSD", side="BUY", volume_before=Decimal("0"),
            volume_after=Decimal("0.10"), volume_delta=Decimal("0.10"), observed_at=timezone.now())
        CopierReplication.objects.create(
            master_event=self.event, follower=self.follower, requested_op="OPEN",
            status=ReplicationStatus.SHADOW_RECORDED, target_volume=Decimal("0.10"),
            follower_symbol="XAUUSD", constraints_authoritative=False, sltp_translation_ok=True)
        self._overview = f"/api/copy-trading/accounts/{self.master_acct.id}/overview/"

    def test_status_default_disabled(self):
        self.client.force_authenticate(self.user)
        r = self.client.get("/api/copy-trading/status/")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["enabled"])

    @override_settings(COPIER_UI_ENABLED=True)
    def test_status_enabled_with_flag(self):
        self.client.force_authenticate(self.user)
        r = self.client.get("/api/copy-trading/status/")
        self.assertTrue(r.data["enabled"])
        self.assertEqual(r.data["limitations"]["live_follower_execution"], "blocked")

    def test_overview_flag_off_reports_disabled_no_rows(self):
        self.client.force_authenticate(self.user)
        r = self.client.get(self._overview)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["enabled"])
        self.assertIsNone(r.data["master"])
        self.assertEqual(r.data["followers"], [])

    @override_settings(COPIER_UI_ENABLED=True)
    def test_overview_owner_sees_full_dashboard(self):
        self.client.force_authenticate(self.user)
        r = self.client.get(self._overview)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["enabled"])
        self.assertEqual(r.data["master"]["account_id"], self.master_acct.id)
        self.assertEqual(len(r.data["followers"]), 1)
        self.assertEqual(len(r.data["recent_events"]), 1)
        self.assertEqual(len(r.data["recent_replications"]), 1)
        self.assertEqual(r.data["recent_replications"][0]["status"], "SHADOW_RECORDED")
        self.assertFalse(r.data["followers"][0]["live_execution_blocked"])  # demo follower
        self.assertEqual(r.data["limitations"]["live_follower_execution"], "blocked")

    @override_settings(COPIER_UI_ENABLED=True)
    def test_overview_other_user_gets_404_no_leak(self):
        self.client.force_authenticate(self.other)
        r = self.client.get(self._overview)
        self.assertEqual(r.status_code, 404)

    @override_settings(COPIER_UI_ENABLED=True)
    def test_overview_owned_non_master_account_master_null(self):
        plain = TradingAccount.objects.create(
            user=self.user, name="p", broker_name="IS6", account_number="3000001",
            is_demo=True, is_active=True,
            broker_server=BrokerServer.objects.create(broker_display_name="IS6",
                                                      server_name="IS6Technologies-DemoP", environment="demo"))
        self.client.force_authenticate(self.user)
        r = self.client.get(f"/api/copy-trading/accounts/{plain.id}/overview/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["enabled"])
        self.assertIsNone(r.data["master"])

    @override_settings(COPIER_UI_ENABLED=True)
    def test_masters_list_owner_scoped(self):
        self.client.force_authenticate(self.other)
        r = self.client.get("/api/copy-trading/masters/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["masters"], [])   # other user owns no masters

    def test_unauthenticated_rejected(self):
        r = self.client.get("/api/copy-trading/status/")
        self.assertIn(r.status_code, (401, 403))
