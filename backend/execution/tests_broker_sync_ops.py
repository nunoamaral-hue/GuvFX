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
            user=self.user, name="Demo", account_number="D1", is_demo=True, broker_name="DemoBroker",
        )

    def test_fetch_symbols_identity_verified_uses_agent_token_and_per_tenant_base(self):
        from execution.management.commands import sync_broker_instruments as cmd
        from execution.snapshot_transport import SnapshotTransport, IdentityCheck

        captured = {"urls": [], "headers": []}

        def _resp(body):
            class _R:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self): return body
            return _R()

        def _fake_urlopen(req, timeout=None):
            captured["urls"].append(req.full_url)
            captured["headers"].append({k.lower(): v for k, v in req.headers.items()})
            if "/mt5/snapshots/account" in req.full_url:
                return _resp(b'{"ok": true, "account_login": "5442", "account_server": "TradersWay-Live", "trade_mode": 2}')
            return _resp(b'{"symbols": [{"name": "XAUUSD", "visible": true, "trade_mode": 4}]}')

        env = {"GUVFX_WINDOWS_AGENT_BASE_URL": "http://global:8788", "GUVFX_WINDOWS_AGENT_TOKEN": "AGENTTOK"}
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch.object(cmd, "resolve_account_snapshot_base",
                               return_value=SnapshotTransport(True, "ok", "http://bridge-8806:8806", per_tenant=True)), \
             mock.patch.object(cmd, "account_windows_username", return_value="wu"), \
             mock.patch.object(cmd, "verify_snapshot_identity", return_value=IdentityCheck(True, "ok", "5442", "5442")), \
             mock.patch.object(cmd.urllib.request, "urlopen", _fake_urlopen):
            syms, prov = cmd._fetch_symbols(self.acct)

        self.assertEqual([s["name"] for s in syms], ["XAUUSD"])
        # Reads the account's OWN per-tenant base (never the global agent) for BOTH the identity check and the
        # symbols fetch; identity is checked FIRST; the bridge auth header is X-GuvFX-Agent-Token (not X-Worker-Token).
        self.assertTrue(all(u.startswith("http://bridge-8806:8806/") for u in captured["urls"]))
        self.assertIn("/mt5/snapshots/account", captured["urls"][0])
        self.assertTrue(captured["urls"][-1].endswith("/mt5/symbols"))
        for h in captured["headers"]:
            self.assertEqual(h.get("x-guvfx-agent-token"), "AGENTTOK")
            self.assertNotIn("x-worker-token", h)
        self.assertTrue(prov["per_tenant"])


class StalenessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op2", email="op2@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D2", is_demo=True, broker_name="DemoBroker",
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
