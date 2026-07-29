"""Phase 3 (P3-E) — demo/live classification consistency at intake (threat T7).

When a BrokerServer is configured, the account's is_demo flag must match the server's environment.
The check is fail-closed, shared across BOTH customer-facing create paths (the serializer and the
add-with-mt5-login endpoint), and scoped so it never retroactively blocks an unrelated edit on a
legacy-inconsistent row.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from billing.beta import grant_beta_entitlement
from trading.models import BrokerServer, TradingAccount
from trading.serializers import TradingAccountSerializer
from trading.views_account_add import AddAccountWithMt5LoginView

U = get_user_model()


def _server(env, name):
    return BrokerServer.objects.create(
        broker_display_name="Broker", server_name=name, environment=env)


class ClassificationCrossCheckTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="cc", email="cc@x.invalid", password="x")
        grant_beta_entitlement(self.user)   # capacity to create accounts (the canonical cap now applies)
        self.demo_srv = _server(BrokerServer.DEMO, "srv-demo")
        self.live_srv = _server(BrokerServer.LIVE, "srv-live")

    def _ser(self, data, instance=None):
        return TradingAccountSerializer(instance, data=data, partial=instance is not None)

    # ── serializer path ──
    def test_demo_server_with_demo_flag_ok(self):
        self.assertTrue(self._ser({"name": "A", "account_number": "1",
                                   "broker_server": self.demo_srv.id, "is_demo": True}).is_valid())

    def test_live_server_with_live_flag_ok(self):
        self.assertTrue(self._ser({"name": "A", "account_number": "2",
                                   "broker_server": self.live_srv.id, "is_demo": False}).is_valid())

    def test_demo_server_with_live_flag_rejected(self):
        ser = self._ser({"name": "A", "account_number": "3", "broker_server": self.demo_srv.id,
                         "is_demo": False})
        self.assertFalse(ser.is_valid())
        self.assertIn("is_demo", ser.errors)

    def test_live_server_with_demo_flag_rejected(self):
        ser = self._ser({"name": "A", "account_number": "4", "broker_server": self.live_srv.id,
                         "is_demo": True})
        self.assertFalse(ser.is_valid())
        self.assertIn("is_demo", ser.errors)

    def test_create_on_demo_server_omitting_is_demo_is_rejected(self):
        # Fail-closed: with the model default (False), an omitted is_demo on a DEMO server is a
        # mismatch — is_demo must be sent explicitly. (Pins the M1 behaviour.)
        ser = self._ser({"name": "A", "account_number": "5", "broker_server": self.demo_srv.id})
        self.assertFalse(ser.is_valid())
        self.assertIn("is_demo", ser.errors)

    def test_broker_name_only_skips_crosscheck(self):
        self.assertTrue(self._ser({"name": "A", "account_number": "6", "broker_name": "SomeBroker",
                                   "is_demo": False}).is_valid())

    def test_patch_flag_into_mismatch_rejected(self):
        acct = TradingAccount.objects.create(
            user=self.user, name="A", account_number="7", broker_server=self.live_srv, is_demo=False)
        ser = self._ser({"is_demo": True}, instance=acct)   # live server + demo flag
        self.assertFalse(ser.is_valid())
        self.assertIn("is_demo", ser.errors)

    def test_patch_server_only_into_mismatch_rejected(self):
        # Swapping the server without resending is_demo must still be caught (effective is_demo comes
        # from the stored row).
        acct = TradingAccount.objects.create(
            user=self.user, name="A", account_number="8", broker_server=self.demo_srv, is_demo=True)
        ser = self._ser({"broker_server": self.live_srv.id}, instance=acct)   # now live srv + demo flag
        self.assertFalse(ser.is_valid())
        self.assertIn("is_demo", ser.errors)

    def test_unrelated_update_on_legacy_inconsistent_row_not_blocked(self):
        acct = TradingAccount.objects.create(
            user=self.user, name="A", account_number="9", broker_server=self.live_srv,
            is_demo=True)   # deliberately inconsistent legacy row
        self.assertTrue(self._ser({"name": "Renamed"}, instance=acct).is_valid())

    def test_unknown_environment_is_rejected(self):
        weird = _server("staging", "srv-weird")   # not demo/live
        ser = self._ser({"name": "A", "account_number": "10", "broker_server": weird.id,
                         "is_demo": True})
        self.assertFalse(ser.is_valid())
        self.assertIn("is_demo", ser.errors)

    # ── add-with-mt5-login endpoint path (the create path the UI uses) ──
    def test_add_with_mt5_login_endpoint_rejects_mismatch(self):
        factory = APIRequestFactory()
        req = factory.post("/api/accounts/add-with-mt5-login/", {
            "name": "A", "account_number": "11", "password": "pw",
            "broker_server": self.live_srv.id, "is_demo": True}, format="json")
        force_authenticate(req, user=self.user)
        resp = AddAccountWithMt5LoginView.as_view()(req)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Classification mismatch", str(resp.data))

    def test_add_with_mt5_login_endpoint_passes_classification_on_match(self):
        # A matching demo/demo pair passes the classification gate and the canonical contract CREATES the
        # account (records intent; mt5_instance=None; broker-login deferred to provisioning) — proving the
        # gate did not falsely reject a consistent account.
        factory = APIRequestFactory()
        req = factory.post("/api/accounts/add-with-mt5-login/", {
            "name": "A", "account_number": "12", "password": "pw",
            "broker_server": self.demo_srv.id, "is_demo": True}, format="json")
        force_authenticate(req, user=self.user)
        resp = AddAccountWithMt5LoginView.as_view()(req)
        self.assertEqual(resp.status_code, 201, resp.data)   # past classification → account created
        self.assertTrue(resp.data["created"])
        acct = TradingAccount.objects.get(user=self.user, account_number="12")
        self.assertEqual(acct.broker_server_id, self.demo_srv.id)
        self.assertIsNone(acct.mt5_instance)   # canonical contract: never the legacy shared instance
