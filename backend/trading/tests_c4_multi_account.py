"""Phase C4 — customer-visible multi-account model (DARK): backend API matrix.

Covers: accounts list serializer (masked #, active_strategy_count, myfxbook, readiness_provider, owner-scoped),
the entitlement-summary action (Active N / limit, STANDARD vs CONCURRENT), the account-explicit View-MT5
resolver (explicit + owner-scoped + cross-user 404 + malformed + single-account fallback), the STANDARD/
CONCURRENT set-active semantics (DARK plain-flip byte-identical; armed one-per-user + concurrent limit), and
Myfxbook metadata (no credentials). Telegram broker attribution is covered in customer_notifications tests.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from admin_ops.models import EntitlementOverride
from billing.models import UserSubscriptionState
from strategies.models import Strategy, StrategyAssignment
from trading.models import BrokerServer, TradingAccount
from trading.views import TradingAccountViewSet
from mt5.views import _resolve_launch_account

U = get_user_model()


def _user(name, *, plan=UserSubscriptionState.Plan.BETA):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.create(user=u, current_plan=plan,
                                         plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False)
    return u


def _acct(user, number, *, is_active=False, is_demo=True, broker="DemoBroker", server=None, disconnected=False):
    srv = None
    if server:
        srv, _ = BrokerServer.objects.get_or_create(server_name=server)
    return TradingAccount.objects.create(
        user=user, name="A", account_number=number, broker_name=broker, broker_server=srv, is_demo=is_demo,
        is_active=is_active, disconnected_at=(timezone.now() if disconnected else None))


def _override(user, capability, value):
    EntitlementOverride.objects.create(user=user, capability=capability, override_value=value, reason="t",
                                       is_active=True, expires_at=timezone.now() + timedelta(days=1))


def _list_accounts(user):
    req = APIRequestFactory().get("/api/trading/accounts/")
    force_authenticate(req, user=user)
    return TradingAccountViewSet.as_view({"get": "list"})(req)


def _entitlement_summary(user):
    req = APIRequestFactory().get("/api/trading/accounts/entitlement-summary/")
    force_authenticate(req, user=user)
    return TradingAccountViewSet.as_view({"get": "entitlement_summary"})(req)


def _set_active(user, acct_id, is_active=True):
    req = APIRequestFactory().post(f"/api/trading/accounts/{acct_id}/set-active/", {"is_active": is_active},
                                   format="json")
    force_authenticate(req, user=user)
    return TradingAccountViewSet.as_view({"post": "set_active"})(req, pk=acct_id)


class AccountsListSerializer(TestCase):
    def test_masked_number_and_fields_present_owner_scoped(self):
        u = _user("l1")
        a = _acct(u, "1302587", server="IS6-Demo", is_demo=True)
        other = _user("l2")
        _acct(other, "9999999")                                   # another user's account — must not appear
        r = _list_accounts(u)
        self.assertEqual(r.status_code, 200)
        rows = r.data if isinstance(r.data, list) else r.data.get("results", r.data)
        self.assertEqual(len(rows), 1)                            # owner-scoped
        row = rows[0]
        self.assertEqual(row["masked_account_number"], "••••2587")
        self.assertEqual(row["account_id"] if "account_id" in row else row["id"], a.id)
        self.assertIn("active_strategy_count", row)
        self.assertIn("myfxbook_url", row)
        self.assertIn("readiness_provider", row)
        self.assertEqual(row["is_demo"], True)

    def test_active_strategy_count_reflects_assignments(self):
        u = _user("l3")
        a = _acct(u, "111")
        s1 = Strategy.objects.create(owner=u, name="S1")
        s2 = Strategy.objects.create(owner=u, name="S2")
        StrategyAssignment.objects.create(account=a, strategy=s1, is_active=True)
        StrategyAssignment.objects.create(account=a, strategy=s2, is_active=True)
        StrategyAssignment.objects.create(account=a, strategy=Strategy.objects.create(owner=u, name="S3"),
                                          is_active=False)
        rows = _list_accounts(u).data
        rows = rows if isinstance(rows, list) else rows.get("results", rows)
        self.assertEqual(rows[0]["active_strategy_count"], 2)     # only the 2 active

    def test_same_strategy_across_multiple_accounts(self):
        u = _user("l4")
        a1, a2 = _acct(u, "111"), _acct(u, "222")
        s = Strategy.objects.create(owner=u, name="Shared")
        StrategyAssignment.objects.create(account=a1, strategy=s, is_active=True)
        StrategyAssignment.objects.create(account=a2, strategy=s, is_active=True)   # same strategy, 2 accounts
        rows = _list_accounts(u).data
        rows = rows if isinstance(rows, list) else rows.get("results", rows)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["active_strategy_count"] == 1 for r in rows))         # each account counts its own


class EntitlementSummaryAction(TestCase):
    def test_standard_default_limit_one(self):
        u = _user("e1")
        _acct(u, "111", is_active=True)
        r = _entitlement_summary(u)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["account_mode"], "standard")
        self.assertEqual(r.data["concurrent_limit"], 1)          # STANDARD ⇒ 1 active at a time
        self.assertEqual(r.data["active_count"], 1)

    def test_concurrent_reports_configured_limit(self):
        u = _user("e2")
        _override(u, "account_mode", {"value": "concurrent"})
        for i in range(3):
            _acct(u, str(100 + i), is_active=True)
        r = _entitlement_summary(u)
        self.assertEqual(r.data["account_mode"], "concurrent")
        self.assertEqual(r.data["concurrent_limit"], 5)          # beta plan concurrent limit
        self.assertEqual(r.data["active_count"], 3)              # Active 3 / 5


class Mt5LaunchAccountExplicit(TestCase):
    def _req(self, user, **body):
        from rest_framework.parsers import JSONParser
        from rest_framework.request import Request
        drf = Request(APIRequestFactory().post("/api/mt5/launch/", body, format="json"),
                      parsers=[JSONParser()])
        drf.user = user
        return drf

    def test_explicit_account_id_resolves_owner_scoped(self):
        u = _user("m1")
        a1 = _acct(u, "111", is_active=True)
        a2 = _acct(u, "222", is_active=False)
        acct, given = _resolve_launch_account(self._req(u, account_id=a2.id))
        self.assertTrue(given)
        self.assertEqual(acct.id, a2.id)                         # the EXPLICIT account, not the active .first()

    def test_cross_user_account_id_is_none(self):
        u, v = _user("m2"), _user("m2v")
        victim = _acct(v, "111", is_active=True)
        acct, given = _resolve_launch_account(self._req(u, account_id=victim.id))
        self.assertTrue(given)
        self.assertIsNone(acct)                                  # owner-scoped miss → None → caller 404s

    def test_malformed_account_id_is_none(self):
        u = _user("m3")
        _acct(u, "111", is_active=True)
        acct, given = _resolve_launch_account(self._req(u, account_id="not-an-int"))
        self.assertTrue(given)
        self.assertIsNone(acct)

    def test_no_selector_falls_back_to_active(self):
        u = _user("m4")
        a = _acct(u, "111", is_active=True)
        _acct(u, "222", is_active=False)
        acct, given = _resolve_launch_account(self._req(u))
        self.assertFalse(given)
        self.assertEqual(acct.id, a.id)                          # single-account fallback preserved


class SetActiveStandardConcurrent(TestCase):
    """set-active on beta/hosted accounts (mt5_instance=None). DARK=plain flip; armed=entitlement semantics."""

    def test_dark_flag_off_is_plain_flip(self):
        # enforcement OFF (default) → activating B does NOT deactivate A (byte-identical legacy plain flip).
        u = _user("s1")
        a = _acct(u, "111", is_active=True)
        b = _acct(u, "222", is_active=False)
        with mock.patch("trading.views._account_runtime_ready", return_value=True):
            r = _set_active(u, b.id, True)
        self.assertEqual(r.status_code, 200)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertTrue(a.is_active and b.is_active)             # BOTH active — legacy plain flip

    @override_settings(CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=True)
    def test_armed_standard_deactivates_the_other(self):
        from trading.account_entitlement import grant_concurrent_enforcement
        u = _user("s2")   # STANDARD default ⇒ one active per user
        grant_concurrent_enforcement(u)   # per-user activation (master ON via override_settings)
        a = _acct(u, "111", is_active=True)
        b = _acct(u, "222", is_active=False)
        with mock.patch("trading.views._account_runtime_ready", return_value=True):
            r = _set_active(u, b.id, True)
        self.assertEqual(r.status_code, 200)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertFalse(a.is_active)                            # A auto-deactivated
        self.assertTrue(b.is_active)                             # B now the single active

    @override_settings(CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=True)
    def test_armed_concurrent_allows_up_to_limit_then_refuses(self):
        from trading.account_entitlement import grant_concurrent_enforcement
        u = _user("s3")
        grant_concurrent_enforcement(u)   # per-user activation (master ON via override_settings)
        _override(u, "account_mode", {"value": "concurrent"})
        _override(u, "concurrent_broker_account_limit", {"value": 2})
        a = _acct(u, "111", is_active=True)
        b = _acct(u, "222", is_active=False)
        c = _acct(u, "333", is_active=False)
        with mock.patch("trading.views._account_runtime_ready", return_value=True):
            self.assertEqual(_set_active(u, b.id, True).status_code, 200)   # 2 active ≤ limit 2
            a.refresh_from_db(); b.refresh_from_db()
            self.assertTrue(a.is_active and b.is_active)         # CONCURRENT keeps both
            r3 = _set_active(u, c.id, True)                      # 3rd would exceed limit 2
        self.assertEqual(r3.status_code, 409)
        c.refresh_from_db()
        self.assertFalse(c.is_active)


class TelegramBrokerAttribution(TestCase):
    """Priority E — a multi-account customer's notification names Broker + MASKED account + strategy (FK)."""

    def test_render_includes_broker_masked_account_and_strategy(self):
        from customer_notifications.messages import render_customer_message
        from customer_notifications.models import CustomerNotification
        n = CustomerNotification(
            event_type=CustomerNotification.EventType.TRADE_CLOSED, language="en",
            payload={"broker": "Pepperstone", "account_kind": "demo", "account_number": "62139344",
                     "strategy": "T1 / Wayond", "symbol": "XAUUSD", "result": "40",
                     "currency": "USD", "outcome": "win"})
        text = render_customer_message(n)
        self.assertIn("Broker: Pepperstone", text)          # broker line
        self.assertIn("••••9344", text)                     # masked last-4
        self.assertNotIn("62139344", text)                  # full number NEVER rendered
        self.assertIn("T1 / Wayond", text)                  # strategy (from the StrategyAssignment FK)

    def test_safe_payload_derives_broker_server_side_from_owner_account(self):
        # Broker is re-derived from the OWNER-scoped durable account, not trusted from the caller payload.
        from customer_notifications.services import _safe_payload
        from customer_notifications.models import CustomerNotification
        u = _user("tg")
        a = _acct(u, "62139344", server="PepperstoneUK-Demo", broker="Pepperstone")
        out = _safe_payload(CustomerNotification.EventType.TRADE_CLOSED,
                            {"broker": "SPOOFED", "account_number": "SPOOF"}, account=a)
        self.assertEqual(out.get("broker"), "PepperstoneUK-Demo")  # server-derived (server_name), not "SPOOFED"
        self.assertEqual(out.get("account_number"), "62139344")    # re-derived from the durable account

    def test_safe_payload_drops_caller_broker_when_no_account(self):
        # Structural guard: broker is NEVER caller-authored. With no owner account it is dropped entirely
        # (no broker line), not passed through from the caller payload.
        from customer_notifications.services import _safe_payload
        from customer_notifications.models import CustomerNotification
        out = _safe_payload(CustomerNotification.EventType.TRADE_CLOSED,
                            {"broker": "SPOOFED", "result": "1"}, account=None)
        self.assertNotIn("broker", out)


class MyfxbookMetadata(TestCase):
    def test_defaults_and_no_credential_field(self):
        u = _user("mfx")
        a = _acct(u, "111")
        self.assertIsNone(a.myfxbook_url)
        self.assertFalse(a.myfxbook_enabled)
        # the serializer exposes url/system_id/enabled but NEVER a password/credential field
        from trading.serializers import TradingAccountSerializer
        fields = set(TradingAccountSerializer().fields)
        self.assertIn("myfxbook_url", fields)
        self.assertIn("myfxbook_system_id", fields)
        self.assertIn("myfxbook_enabled", fields)
        self.assertFalse(any("myfxbook" in f and ("pass" in f or "cred" in f or "secret" in f) for f in fields))
