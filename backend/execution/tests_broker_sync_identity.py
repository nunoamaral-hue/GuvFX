"""Phase 2 — identity-safe BrokerInstrument sync. Proves the data-isolation fix: each account syncs from ITS
OWN per-tenant, identity-verified bridge (never the module-global agent); a mismatch / unresolved / empty /
failed read writes ZERO rows and leaves any existing cache intact; broker suffixes are stored EXACTLY
(XAUUSD vs XAUUSD.std never collapse); the refresh is atomic and replay-idempotent. No order, no network
(bridge mocked)."""
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.contrib.auth import get_user_model
from django.test import TestCase

from execution.management.commands import sync_broker_instruments as cmd
from execution.management.commands.sync_broker_instruments import upsert_broker_instruments
from execution.models import BrokerInstrument
from execution.snapshot_transport import SnapshotTransport, IdentityCheck
from trading.models import TradingAccount

U = get_user_model()
_n = 0


def _acct(user, broker="TradersWay", number=None):
    global _n
    _n += 1
    return TradingAccount.objects.create(user=user, name=broker, broker_name=broker,
                                         account_number=number or f"S{_n:04d}", is_demo=False)


def _resp(body: bytes):
    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return body
    return _R()


def _bridge(account_base, *, acct_json, symbols_json):
    """A urlopen fake bound to ONE base: returns the account snapshot then the symbols for that base only."""
    def _fake(req, timeout=None):
        assert req.full_url.startswith(account_base), f"read hit {req.full_url}, expected base {account_base}"
        if "/mt5/snapshots/account" in req.full_url:
            return _resp(acct_json)
        return _resp(symbols_json)
    return _fake


_ENV = {"GUVFX_WINDOWS_AGENT_BASE_URL": "http://GLOBAL-AGENT:8788", "GUVFX_WINDOWS_AGENT_TOKEN": "TOK"}
_OK_IDENT = IdentityCheck(True, "ok", "x", "x")


class IdentityRoutingTests(TestCase):
    def setUp(self):
        self.u = U.objects.create_user(username="r", email="r@x.invalid", password="x")
        self.a = _acct(self.u)

    def test_syncs_from_own_per_tenant_base_never_global_agent(self):
        base = "http://TENANT-8806:8806"
        syms = b'{"symbols": [{"name": "XAUUSD", "visible": true, "trade_mode": 4, "contract_size": 100.0}]}'
        acct = b'{"ok": true, "account_login": "5442", "account_server": "TradersWay-Live", "trade_mode": 2}'
        with mock.patch.dict("os.environ", _ENV, clear=False), \
             mock.patch.object(cmd, "resolve_account_snapshot_base",
                               return_value=SnapshotTransport(True, "ok", base, per_tenant=True)), \
             mock.patch.object(cmd, "account_windows_username", return_value="wu"), \
             mock.patch.object(cmd, "verify_snapshot_identity", return_value=_OK_IDENT), \
             mock.patch.object(cmd.urllib.request, "urlopen", _bridge(base, acct_json=acct, symbols_json=syms)):
            call_command("sync_broker_instruments", "--account", str(self.a.id))
        # the row came from the per-tenant base (the _bridge fake asserts no read hit the global agent)
        self.assertTrue(BrokerInstrument.objects.filter(account=self.a, broker_symbol="XAUUSD", enabled=True).exists())

    def test_each_account_routes_to_its_own_base(self):
        a2 = _acct(self.u, broker="IS6FX")
        bases = {self.a.id: "http://T46:8806", a2.id: "http://T48:8804"}
        seen = {}

        def _resolve(account, *, global_base_url):
            return SnapshotTransport(True, "ok", bases[account.id], per_tenant=True)

        def _fake(req, timeout=None):
            for aid, b in bases.items():
                if req.full_url.startswith(b):
                    seen.setdefault(aid, []).append(req.full_url)
            if "/mt5/snapshots/account" in req.full_url:
                return _resp(b'{"ok": true, "account_login": "1", "account_server": "S", "trade_mode": 2}')
            return _resp(b'{"symbols": [{"name": "XAUUSD", "visible": true, "trade_mode": 4}]}')

        with mock.patch.dict("os.environ", _ENV, clear=False), \
             mock.patch.object(cmd, "resolve_account_snapshot_base", side_effect=_resolve), \
             mock.patch.object(cmd, "account_windows_username", return_value="wu"), \
             mock.patch.object(cmd, "verify_snapshot_identity", return_value=_OK_IDENT), \
             mock.patch.object(cmd.urllib.request, "urlopen", _fake):
            call_command("sync_broker_instruments", "--account", str(self.a.id))
            call_command("sync_broker_instruments", "--account", str(a2.id))
        # each account's reads hit ONLY its own base
        self.assertTrue(all(u.startswith("http://T46:8806") for u in seen[self.a.id]))
        self.assertTrue(all(u.startswith("http://T48:8804") for u in seen[a2.id]))

    def test_identity_mismatch_writes_zero_rows(self):
        BrokerInstrument.objects.create(account=self.a, broker_symbol="OLD", base_symbol="OLD", enabled=True)
        with mock.patch.dict("os.environ", _ENV, clear=False), \
             mock.patch.object(cmd, "resolve_account_snapshot_base",
                               return_value=SnapshotTransport(True, "ok", "http://T:8806", per_tenant=True)), \
             mock.patch.object(cmd, "account_windows_username", return_value="wu"), \
             mock.patch.object(cmd, "verify_snapshot_identity",
                               return_value=IdentityCheck(False, "login_mismatch", "a", "b")), \
             mock.patch.object(cmd.urllib.request, "urlopen",
                               _bridge("http://T:8806",
                                       acct_json=b'{"account_login":"9","account_server":"Foreign","trade_mode":2}',
                                       symbols_json=b'{"symbols":[{"name":"FOREIGN"}]}')):
            with self.assertRaises(CommandError):
                call_command("sync_broker_instruments", "--account", str(self.a.id))
        # no foreign symbol written; pre-existing cache untouched
        self.assertFalse(BrokerInstrument.objects.filter(account=self.a, broker_symbol="FOREIGN").exists())
        self.assertTrue(BrokerInstrument.objects.filter(account=self.a, broker_symbol="OLD", enabled=True).exists())

    def test_unresolved_transport_writes_nothing(self):
        with mock.patch.dict("os.environ", _ENV, clear=False), \
             mock.patch.object(cmd, "resolve_account_snapshot_base",
                               return_value=SnapshotTransport(False, "endpoint_not_ready")):
            with self.assertRaises(CommandError):
                call_command("sync_broker_instruments", "--account", str(self.a.id))
        self.assertEqual(BrokerInstrument.objects.filter(account=self.a).count(), 0)

    def test_empty_response_does_not_erase_valid_cache(self):
        BrokerInstrument.objects.create(account=self.a, broker_symbol="XAUUSD", base_symbol="XAUUSD", enabled=True)
        with mock.patch.object(cmd, "_fetch_symbols", return_value=([], {"source": "t"})):
            with self.assertRaises(CommandError):
                call_command("sync_broker_instruments", "--account", str(self.a.id))
        self.assertTrue(BrokerInstrument.objects.filter(account=self.a, broker_symbol="XAUUSD", enabled=True).exists())

    def test_malformed_symbols_json_fails_closed_cleanly(self):
        # A 200 with unparseable JSON must raise CommandError (not a raw traceback) and leave the cache intact.
        BrokerInstrument.objects.create(account=self.a, broker_symbol="XAUUSD", base_symbol="XAUUSD", enabled=True)

        def _fake(req, timeout=None):
            if "/mt5/snapshots/account" in req.full_url:
                return _resp(b'{"ok": true, "account_login": "5442", "account_server": "TradersWay-Live", "trade_mode": 2}')
            return _resp(b'{ this is not valid json ')
        with mock.patch.dict("os.environ", _ENV, clear=False), \
             mock.patch.object(cmd, "resolve_account_snapshot_base",
                               return_value=SnapshotTransport(True, "ok", "http://T:8806", per_tenant=True)), \
             mock.patch.object(cmd, "account_windows_username", return_value="wu"), \
             mock.patch.object(cmd, "verify_snapshot_identity", return_value=_OK_IDENT), \
             mock.patch.object(cmd.urllib.request, "urlopen", _fake):
            with self.assertRaises(CommandError):
                call_command("sync_broker_instruments", "--account", str(self.a.id))
        self.assertTrue(BrokerInstrument.objects.filter(account=self.a, broker_symbol="XAUUSD", enabled=True).exists())


class UpsertExactnessTests(TestCase):
    def setUp(self):
        self.u = U.objects.create_user(username="x", email="x@x.invalid", password="x")
        self.a = _acct(self.u)

    def test_broker_suffix_stored_exactly_not_collapsed(self):
        upsert_broker_instruments(self.a, [
            {"name": "XAUUSD", "visible": True, "trade_mode": 4, "currency_profit": "USD"},
            {"name": "XAUUSD.std", "visible": True, "trade_mode": 4, "currency_profit": "USD"},
            {"name": "XAUUSD_SB", "visible": True, "trade_mode": 4, "currency_profit": "GBP"},
        ])
        names = set(BrokerInstrument.objects.filter(account=self.a).values_list("broker_symbol", flat=True))
        self.assertEqual(names, {"XAUUSD", "XAUUSD.std", "XAUUSD_SB"})   # exact; .std NOT upper-cased, not merged
        std = BrokerInstrument.objects.get(account=self.a, broker_symbol="XAUUSD.std")
        self.assertEqual(std.base_symbol, "XAUUSD")                     # base strips the suffix for matching
        sb = BrokerInstrument.objects.get(account=self.a, broker_symbol="XAUUSD_SB")
        self.assertEqual(sb.base_symbol, "XAUUSD")                      # distinct symbol; never treated as XAUUSD

    def test_zero_valid_names_does_not_erase_cache(self):
        # Non-empty but nameless/garbage response: must NOT disable the whole valid cache (fail-closed-on-partial).
        BrokerInstrument.objects.create(account=self.a, broker_symbol="XAUUSD", base_symbol="XAUUSD", enabled=True)
        counts = upsert_broker_instruments(self.a, [{"symbol": "EURUSD"}, {"description": "Gold"}, {}])
        self.assertEqual(counts["seen"], 0)
        self.assertEqual(counts["disabled"], 0)
        self.assertTrue(BrokerInstrument.objects.filter(account=self.a, broker_symbol="XAUUSD", enabled=True).exists())

    def test_replay_is_idempotent(self):
        rows = [{"name": "XAUUSD", "visible": True, "trade_mode": 4}]
        c1 = upsert_broker_instruments(self.a, rows)
        c2 = upsert_broker_instruments(self.a, rows)
        self.assertEqual(c1["created"], 1)
        self.assertEqual(c2["created"], 0)
        self.assertEqual(c2["updated"], 1)
        self.assertEqual(BrokerInstrument.objects.filter(account=self.a, broker_symbol="XAUUSD").count(), 1)

    def test_provenance_recorded_on_metadata(self):
        upsert_broker_instruments(self.a, [{"name": "XAUUSD", "visible": True, "trade_mode": 4}],
                                  provenance={"source_server": "TradersWay-Live", "observed_at": "t0"})
        r = BrokerInstrument.objects.get(account=self.a, broker_symbol="XAUUSD")
        self.assertEqual(r.metadata.get("_provenance", {}).get("source_server"), "TradersWay-Live")

    def test_refresh_is_atomic_on_failure(self):
        BrokerInstrument.objects.create(account=self.a, broker_symbol="KEEP", base_symbol="KEEP", enabled=True)
        real = BrokerInstrument.objects.update_or_create
        calls = {"n": 0}

        def _flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom mid-refresh")
            return real(*args, **kwargs)

        with mock.patch.object(BrokerInstrument.objects, "update_or_create", side_effect=_flaky):
            with self.assertRaises(RuntimeError):
                upsert_broker_instruments(self.a, [{"name": "NEW1", "visible": True, "trade_mode": 4},
                                                   {"name": "NEW2", "visible": True, "trade_mode": 4}])
        # atomic rollback: neither new row persisted, and the pre-existing KEEP row is untouched (still enabled)
        self.assertFalse(BrokerInstrument.objects.filter(account=self.a, broker_symbol="NEW1").exists())
        self.assertTrue(BrokerInstrument.objects.filter(account=self.a, broker_symbol="KEEP", enabled=True).exists())
