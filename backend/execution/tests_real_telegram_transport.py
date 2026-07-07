"""GFX-PKT-REAL-TELEGRAM-TRANSPORT — the real Telegram transport + selector.

Proves: dry-run stays the DEFAULT; the real transport is used ONLY when explicitly selected;
missing token/chat id fail closed with NO network call; API success marks SENT (transmitted),
API failure marks FAILED (not transmitted); no duplicate send; no network in dry-run; the token
is NEVER printed/returned/logged; nothing publishes to WIMS or places an order. Disabled by
default (dispatch flag OFF), so NO real message is sent by this packet.
"""
import json
import os
import urllib.error
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution.models import NotificationCandidate, NotificationDelivery, TradeOutcomeRecord
from execution.notifications.dispatcher import dispatch_pending
from execution.notifications.real_transport import RealTelegramTransport, select_transport
from execution.notifications.transport import TelegramDryRunTransport
from trading.models import Trade, TradingAccount

User = get_user_model()
_URLOPEN = "urllib.request.urlopen"
TOKEN = "SECRET123:BOTTOKENvalueABC"   # a fake token; used to prove it is never leaked
CHAT = "@wims07072026"


def _ok_urlopen(payload=None):
    payload = payload if payload is not None else {"ok": True, "result": {"message_id": 1}}
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    cm = mock.MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


class RealTransportBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True,
        )
        self.trade = Trade.objects.create(
            account=self.acct, ticket="T1", symbol="EURUSD", side="BUY", volume=Decimal("0.01"),
            open_time=timezone.now(), open_price=Decimal("1.0850"), close_time=timezone.now(),
            close_price=Decimal("1.0900"), profit=Decimal("21"), correlation_id="c-1",
        )
        self.outcome = TradeOutcomeRecord.objects.create(
            trade=self.trade, outcome=TradeOutcomeRecord.Outcome.WIN, net_pnl=Decimal("21"),
            is_delivery_candidate=True, correlation_id="c-1", signal_source="wayond",
        )
        self.candidate = NotificationCandidate.objects.create(
            outcome_record=self.outcome, correlation_id="c-1", signal_source="wayond",
            net_pnl=Decimal("21"),
        )

    def _real(self, **kw):
        return RealTelegramTransport(token=kw.get("token", TOKEN), chat_id=kw.get("chat_id", CHAT))


class SelectorTests(RealTransportBase):
    def test_dry_run_is_the_default(self):
        with mock.patch.dict(os.environ, {"NOTIFICATION_DISPATCH_TRANSPORT": ""}):
            self.assertIsInstance(select_transport(), TelegramDryRunTransport)

    def test_real_only_when_explicitly_selected(self):
        for value, is_real in [("real", True), ("telegram-real", True), ("telegram", True),
                               ("dryrun", False), ("", False), ("nonsense", False)]:
            with mock.patch.dict(os.environ, {"NOTIFICATION_DISPATCH_TRANSPORT": value}):
                t = select_transport()
                self.assertEqual(isinstance(t, RealTelegramTransport), is_real, value)


class FailClosedTests(RealTransportBase):
    def test_missing_token_fails_closed_no_network(self):
        with mock.patch(_URLOPEN) as up:
            r = self._real(token="").deliver(self.candidate)
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "FAILED")
        self.assertFalse(r.transmitted)
        up.assert_not_called()

    def test_missing_chat_id_fails_closed_no_network(self):
        with mock.patch(_URLOPEN) as up:
            r = self._real(chat_id="").deliver(self.candidate)
        self.assertFalse(r.ok)
        self.assertFalse(r.transmitted)
        up.assert_not_called()

    def test_non_win_refused(self):
        TradeOutcomeRecord.objects.filter(pk=self.outcome.pk).update(
            outcome=TradeOutcomeRecord.Outcome.LOSS)
        with mock.patch(_URLOPEN) as up:
            r = self._real().deliver(NotificationCandidate.objects.get(pk=self.candidate.pk))
        self.assertFalse(r.ok)
        self.assertFalse(r.transmitted)
        up.assert_not_called()


class SendTests(RealTransportBase):
    def test_api_success_marks_sent_transmitted(self):
        with mock.patch(_URLOPEN, return_value=_ok_urlopen()) as up:
            r = self._real().deliver(self.candidate)
        self.assertTrue(r.ok)
        self.assertEqual(r.status, "SENT")
        self.assertTrue(r.transmitted)
        up.assert_called_once()
        req = up.call_args.args[0]
        self.assertIn("api.telegram.org", req.full_url)
        body = json.loads(req.data.decode())
        self.assertEqual(body["chat_id"], CHAT)
        self.assertIn("EURUSD", body["text"])

    def test_api_http_error_marks_failed(self):
        err = urllib.error.HTTPError("https://api.telegram.org/botX/sendMessage", 401, "no", {}, None)
        with mock.patch(_URLOPEN, side_effect=err):
            r = self._real().deliver(self.candidate)
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "FAILED")
        self.assertFalse(r.transmitted)
        self.assertIn("401", r.detail)

    def test_api_ok_false_marks_failed(self):
        with mock.patch(_URLOPEN, return_value=_ok_urlopen({"ok": False, "error_code": 400})):
            r = self._real().deliver(self.candidate)
        self.assertFalse(r.ok)
        self.assertFalse(r.transmitted)

    def test_idempotent_skip_when_already_transmitted(self):
        NotificationDelivery.objects.create(
            candidate=self.candidate, transport="telegram-real",
            result=NotificationDelivery.Result.SENT, transmitted=True, attempt=1,
            correlation_id="c-1", rendered_message="prev", detail="prev",
        )
        with mock.patch(_URLOPEN) as up:
            r = self._real().deliver(self.candidate)
        self.assertTrue(r.ok)
        self.assertFalse(r.transmitted)          # not re-sent
        up.assert_not_called()


class DispatchIntegrationTests(RealTransportBase):
    def _env(self, **extra):
        base = {"NOTIFICATION_DISPATCH_ENABLED": "true", "NOTIFICATION_DISPATCH_TRANSPORT": "real",
                "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}
        base.update(extra)
        return mock.patch.dict(os.environ, base)

    def test_dispatch_disabled_by_default_sends_nothing(self):
        # No NOTIFICATION_DISPATCH_ENABLED → dispatch is a no-op even with a real transport selected.
        with mock.patch.dict(os.environ, {"NOTIFICATION_DISPATCH_ENABLED": "",
                                          "NOTIFICATION_DISPATCH_TRANSPORT": "real",
                                          "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}):
            with mock.patch(_URLOPEN) as up:
                counts = dispatch_pending()
        self.assertFalse(counts["enabled"])
        self.assertEqual(counts["claimed"], 0)
        up.assert_not_called()
        self.assertEqual(NotificationDelivery.objects.count(), 0)

    def test_enabled_real_dispatch_sends_once_and_marks_sent(self):
        with self._env():
            with mock.patch(_URLOPEN, return_value=_ok_urlopen()) as up:
                counts = dispatch_pending()
                # A second run must NOT re-send (candidate now SENT, not re-claimed).
                dispatch_pending()
        self.assertEqual(counts["sent"], 1)
        up.assert_called_once()
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, NotificationCandidate.Status.SENT)
        d = NotificationDelivery.objects.get()
        self.assertTrue(d.transmitted)

    def test_transmission_durable_across_finalize_failure_no_resend(self):
        # THE idempotency fix: if the DB finalize fails AFTER a successful send, the transmission
        # is still recorded durably (its own committed txn) and the candidate is not re-sent.
        from execution.notifications import dispatcher as disp
        with self._env():
            with mock.patch(_URLOPEN, return_value=_ok_urlopen()) as up:
                with mock.patch.object(disp, "_finalize_result",
                                       side_effect=RuntimeError("db down")):
                    disp.dispatch_pending()
                # the finalize blew up, but a second run must NOT re-send.
                disp.dispatch_pending()
        up.assert_called_once()                                   # sent exactly once
        self.assertEqual(NotificationDelivery.objects.filter(transmitted=True).count(), 1)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, NotificationCandidate.Status.SENT)

    def test_belt_skips_resend_when_a_transmitted_delivery_exists(self):
        # Even if a candidate is somehow retryable (FAILED) with a transmitted delivery, the
        # transport's belt refuses to re-send.
        NotificationDelivery.objects.create(
            candidate=self.candidate, transport="telegram-real",
            result=NotificationDelivery.Result.SENT, transmitted=True, attempt=1,
            correlation_id="c-1", rendered_message="x", detail="x")
        NotificationCandidate.objects.filter(pk=self.candidate.pk).update(
            status=NotificationCandidate.Status.FAILED)
        with self._env():
            with mock.patch(_URLOPEN) as up:
                dispatch_pending()
        up.assert_not_called()
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, NotificationCandidate.Status.SENT)

    def test_dry_run_dispatch_makes_no_network_call(self):
        with mock.patch.dict(os.environ, {"NOTIFICATION_DISPATCH_ENABLED": "true"}):
            with mock.patch(_URLOPEN) as up:
                counts = dispatch_pending(transport=TelegramDryRunTransport())
        self.assertEqual(counts["sent"], 1)
        up.assert_not_called()
        self.assertFalse(NotificationDelivery.objects.get().transmitted)


class TokenSecrecyTests(RealTransportBase):
    def test_token_never_appears_in_result_or_logs(self):
        err = urllib.error.HTTPError(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage", 401, "Unauthorized", {}, None)
        import logging
        with self.assertLogs("guvfx.execution.notifications", level="DEBUG") as logs:
            with mock.patch(_URLOPEN, side_effect=err):
                r = self._real().deliver(self.candidate)
            # The transport deliberately logs nothing; emit one record so assertLogs has content.
            logging.getLogger("guvfx.execution.notifications").info("probe")
        self.assertNotIn(TOKEN, r.detail)
        self.assertNotIn(TOKEN, repr(r))
        self.assertFalse(any(TOKEN in line for line in logs.output))

    def test_token_not_in_success_result(self):
        with mock.patch(_URLOPEN, return_value=_ok_urlopen()):
            r = self._real().deliver(self.candidate)
        self.assertNotIn(TOKEN, repr(r))
        self.assertNotIn(TOKEN, r.detail)
        self.assertNotIn(TOKEN, r.rendered_message)
