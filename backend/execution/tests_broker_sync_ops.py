"""GFX-PKT-BROKER-SYMBOL-DEPLOY-AND-SYNC — sync auth-header fix + cache staleness visibility.

Proves (1) the broker-symbol sync fetch sends the correct bridge auth header
(``X-GuvFX-Agent-Token``, not ``X-Worker-Token`` — the old header 401s against the bridge), and
(2) broker_instrument_status reports cache freshness fail-safe (never-synced / fresh / stale).
"""
import datetime as dt
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution.management.commands.broker_instrument_status import broker_instrument_staleness
from execution.models import BrokerInstrument
from trading.models import TradingAccount

User = get_user_model()


class SyncAuthHeaderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True,
        )

    def test_fetch_symbols_uses_agent_token_header(self):
        from execution.management.commands import sync_broker_instruments as cmd

        captured = {}

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"symbols": [{"name": "BTCUSD", "visible": true, "trade_mode": 4}]}'

        def _fake_urlopen(req, timeout=None):
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            captured["url"] = req.full_url
            return _Resp()

        env = {"GUVFX_WINDOWS_AGENT_BASE_URL": "http://bridge:8788", "GUVFX_WINDOWS_AGENT_TOKEN": "AGENTTOK"}
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch.object(cmd.urllib.request, "urlopen", _fake_urlopen):
            syms = cmd._fetch_symbols(self.acct)

        self.assertEqual([s["name"] for s in syms], ["BTCUSD"])
        # The bridge GET endpoints authenticate via X-GuvFX-Agent-Token — NOT X-Worker-Token.
        self.assertIn("x-guvfx-agent-token", captured["headers"])
        self.assertEqual(captured["headers"]["x-guvfx-agent-token"], "AGENTTOK")
        self.assertNotIn("x-worker-token", captured["headers"])
        self.assertTrue(captured["url"].endswith("/mt5/symbols"))


class StalenessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op2", email="op2@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D2", is_demo=True,
        )

    def _instrument(self):
        return BrokerInstrument.objects.create(
            account=self.acct, broker_symbol="BTCUSD", base_symbol="BTCUSD", enabled=True, metadata={},
        )

    def test_never_synced_is_stale(self):
        s = broker_instrument_staleness(self.acct, stale_hours=48)
        self.assertIsNone(s["last_synced"])
        self.assertTrue(s["stale"])
        self.assertEqual(s["total"], 0)

    def test_fresh_cache_not_stale(self):
        self._instrument()  # synced_at auto_now = now
        s = broker_instrument_staleness(self.acct, stale_hours=48)
        self.assertFalse(s["stale"])
        self.assertEqual(s["enabled"], 1)
        self.assertLess(s["age_hours"], 1.0)

    def test_old_cache_is_stale(self):
        inst = self._instrument()
        old = timezone.now() - dt.timedelta(hours=72)
        # auto_now overrides on save() — set synced_at directly via queryset update.
        BrokerInstrument.objects.filter(pk=inst.pk).update(synced_at=old)
        s = broker_instrument_staleness(self.acct, stale_hours=48)
        self.assertTrue(s["stale"])
        self.assertGreater(s["age_hours"], 48)
