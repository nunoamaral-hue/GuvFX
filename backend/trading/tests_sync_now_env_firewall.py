"""LIVE READ-ONLY MONITORING — SyncNowView invariant-5 environment firewall (review follow-up).

The user-facing POST /api/trading/sync-now/ is the THIRD deals->persist path (alongside the async ingest
worker and the analytics reads). After the bridge demo-gate removal it serves LIVE accounts, so it must apply
the SAME DEMO/LIVE environment verification: a terminal whose observed environment disagrees with the account's
expected environment must be REFUSED (409) and persist ZERO rows. A matching environment proceeds normally.
"""
from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from execution import snapshot_transport as ST
from trading.models import Trade, TradingAccount

User = get_user_model()

_ENV = {"WINDOWS_AGENT_BASE": "http://agent.invalid:8787", "WINDOWS_AGENT_TOKEN": "t"}


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _deals_payload(login, server, trade_mode, deals=None):
    return {"ok": True, "deals": deals or [], "count": len(deals or []),
            "account_login": str(login), "account_server": server, "trade_mode": trade_mode}


class SyncNowEnvironmentFirewallTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.c = APIClient()
        self.c.force_authenticate(self.u)
        self.demo = TradingAccount.objects.create(
            user=self.u, name="Demo", account_number="500123", is_demo=True, broker_name="DemoBroker")
        self.live = TradingAccount.objects.create(
            user=self.u, name="Live", account_number="55442", is_demo=False, broker_name="TradersWay")

    def _sync(self, account, payload):
        ok_base = ST.SnapshotTransport(True, ST.ST_PER_TENANT_OK, "http://tenant.invalid:8800", per_tenant=True)
        with mock.patch.dict("os.environ", _ENV, clear=False), \
             mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=ok_base), \
             mock.patch("urllib.request.urlopen", return_value=_FakeResp(payload)):
            return self.c.post("/api/trading/sync-now/",
                               {"account_id": account.id, "windows_username": f"guvfx_u_{account.account_number}"},
                               format="json")

    def test_demo_account_live_terminal_refused_no_persist(self):
        # DEMO-classified account, terminal observed LIVE (trade_mode=2) -> environment mismatch -> 409, 0 rows.
        r = self._sync(self.demo, _deals_payload("500123", "DemoBroker-Demo", 2,
                                                 deals=[{"ticket": "1", "position_id": 9, "type": 0, "entry": 0,
                                                         "symbol": "XAUUSD", "volume": 0.1, "price": 2000.0,
                                                         "profit": 10.0, "time": 1}]))
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.json().get("reason_code"), ST.ID_ENVIRONMENT_MISMATCH)
        self.assertEqual(Trade.objects.filter(account=self.demo).count(), 0)   # persisted ZERO

    def test_live_account_live_terminal_proceeds(self):
        # Positive control: LIVE account on a LIVE terminal passes the firewall (empty deals -> ok, 0 inserted).
        r = self._sync(self.live, _deals_payload("55442", "TradersWay-Live", 2, deals=[]))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json().get("ok"))

    def test_pre_redeploy_bridge_without_trade_mode_still_syncs(self):
        # Backward-compat: a bridge that did not report trade_mode (None) skips the env check (login still binds).
        payload = {"ok": True, "deals": [], "count": 0, "account_login": "55442",
                   "account_server": "TradersWay-Live"}   # NO trade_mode key
        r = self._sync(self.live, payload)
        self.assertEqual(r.status_code, 200, r.content)
