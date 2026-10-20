"""Observer recording + identity firewall + ledger certification (DB-backed, injected I/O)."""
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from trade_copier import diff, observer
from trade_copier.diff import ObservedPosition as P
from trade_copier.models import CopierMaster, MasterEvent, MasterObservationPoll, MasterPositionSnapshot
from trade_copier.observer import MasterReadResult, observe_master, run_observer_pass
from trading.models import BrokerServer, TradingAccount

User = get_user_model()


def _result(positions, *, login="1302575", server="IS6Technologies-Demo", trade_mode=0):
    return MasterReadResult(True, "ok", observed_login=login, observed_server=server,
                            observed_trade_mode=trade_mode, transport_reason="snapshot_transport_legacy_global",
                            positions=tuple(positions))


def _queued_fetch(results):
    """A fetch() that returns queued MasterReadResults in order (one per observe call)."""
    seq = iter(results)

    def _fetch(_account):
        return next(seq)
    return _fetch


class ObserverRecordTests(TestCase):
    def _master(self, number="1302575", *, is_demo=True, server="IS6Technologies-Demo",
                environment="demo", enabled=True):
        user = User.objects.create_user(username=f"u{number}", email=f"u{number}@x.invalid", password="x")
        bs = BrokerServer.objects.create(broker_display_name="IS6", server_name=server,
                                         environment=environment)
        acct = TradingAccount.objects.create(user=user, name="m", broker_name="B",
                                             account_number=number, is_demo=is_demo, is_active=True,
                                             broker_server=bs)
        return CopierMaster.objects.create(trading_account=acct, observe_enabled=enabled)

    def test_first_poll_is_baseline_no_events(self):
        m = self._master()
        out = observe_master(m, fetch=_queued_fetch([_result([P(1, "XAUUSD", "BUY", 0.10, sl=1990.0)])]))
        self.assertTrue(out["ok"])
        self.assertTrue(out["first_poll"])
        self.assertEqual(out["events"], 0)
        self.assertEqual(MasterObservationPoll.objects.count(), 1)
        self.assertEqual(MasterPositionSnapshot.objects.count(), 1)
        self.assertEqual(MasterEvent.objects.count(), 0)
        m.refresh_from_db()
        self.assertIsNotNone(m.last_poll)
        self.assertIsNotNone(m.last_observed_at)

    def test_second_poll_emits_open_and_sl_events(self):
        m = self._master()
        fetch = _queued_fetch([
            _result([P(1, "XAUUSD", "BUY", 0.10, sl=0.0)]),                       # baseline
            _result([P(1, "XAUUSD", "BUY", 0.10, sl=1990.0), P(2, "EURUSD", "SELL", 0.5)]),  # +SL, +open
        ])
        observe_master(m, fetch=fetch)
        out2 = observe_master(m, fetch=fetch)
        self.assertEqual(out2["events"], 2)
        types = sorted(MasterEvent.objects.values_list("event_type", flat=True))
        self.assertEqual(types, sorted([diff.SL_SET, diff.OPEN]))
        # Sequences are monotone 1,2 for this master.
        self.assertEqual(sorted(MasterEvent.objects.values_list("sequence", flat=True)), [1, 2])

    def test_no_change_second_poll_zero_events(self):
        m = self._master()
        pos = [P(1, "XAUUSD", "BUY", 0.10, sl=1990.0, tp=2100.0)]
        fetch = _queued_fetch([_result(pos), _result(pos)])
        observe_master(m, fetch=fetch)
        out2 = observe_master(m, fetch=fetch)
        self.assertEqual(out2["events"], 0)
        self.assertEqual(MasterObservationPoll.objects.count(), 2)   # poll still recorded (immutable evidence)
        self.assertEqual(MasterEvent.objects.count(), 0)

    def test_full_close_event(self):
        m = self._master()
        fetch = _queued_fetch([_result([P(1, "XAUUSD", "BUY", 0.10)]), _result([])])
        observe_master(m, fetch=fetch)
        observe_master(m, fetch=fetch)
        ev = MasterEvent.objects.get()
        self.assertEqual(ev.event_type, diff.FULL_CLOSE)
        self.assertEqual(ev.position_ticket, 1)

    def test_refused_read_records_nothing_and_keeps_baseline(self):
        m = self._master()
        observe_master(m, fetch=_queued_fetch([_result([P(1, "X", "BUY", 0.1)])]))  # baseline
        m.refresh_from_db()
        baseline_poll = m.last_poll_id
        out = observe_master(m, fetch=lambda _a: MasterReadResult(False, "snapshot_identity_login_mismatch"))
        self.assertFalse(out["ok"])
        self.assertIsNone(out["poll_id"])
        m.refresh_from_db()
        self.assertEqual(m.last_poll_id, baseline_poll)          # baseline NOT advanced
        self.assertEqual(MasterObservationPoll.objects.count(), 1)  # no new poll invented

    def test_master_event_is_append_only(self):
        m = self._master()
        fetch = _queued_fetch([_result([P(1, "X", "BUY", 0.1)]), _result([])])
        observe_master(m, fetch=fetch)
        observe_master(m, fetch=fetch)
        ev = MasterEvent.objects.get()
        ev.reason = "tampered"
        with self.assertRaises(ValueError):
            ev.save()

    def test_snapshot_row_is_append_only(self):
        m = self._master()
        observe_master(m, fetch=_queued_fetch([_result([P(1, "X", "BUY", 0.1)])]))
        row = MasterPositionSnapshot.objects.first()
        row.volume = 9
        with self.assertRaises(ValueError):
            row.save()

    def test_run_pass_skips_disabled_master(self):
        self._master(number="111", server="IS6Technologies-DemoA", enabled=False)
        self._master(number="222", server="IS6Technologies-DemoB", enabled=True)
        summary = run_observer_pass(fetch=lambda _a: _result([], login="222"))
        self.assertEqual(summary["masters"], 1)   # only the enabled one
        self.assertEqual(summary["ok"], 1)


class FetchFirewallTests(TestCase):
    """fetch_master_positions must bind the batch to the observed identity, fail-closed."""

    def _legacy_account(self, number="1302575", *, is_demo=True, server="IS6Technologies-Demo",
                        environment="demo"):
        user = User.objects.create_user(username=f"f{number}", email=f"f{number}@x.invalid", password="x")
        bs = BrokerServer.objects.create(broker_display_name="IS6", server_name=server,
                                         environment=environment)
        return TradingAccount.objects.create(user=user, name="m", broker_name="B",
                                             account_number=number, is_demo=is_demo, is_active=True,
                                             broker_server=bs)

    _ENV = {"WINDOWS_AGENT_BASE": "http://agent.invalid:8788", "WINDOWS_AGENT_TOKEN": "t"}

    def test_matching_identity_builds_positions(self):
        acct = self._legacy_account()
        payload = {"ok": True, "account_login": "1302575", "account_server": "IS6Technologies-Demo",
                   "trade_mode": 0,
                   "positions": [{"ticket": 1, "symbol": "XAUUSD", "type": 0, "volume": 0.1,
                                  "sl": 1990.0, "tp": 0.0, "price_open": 2000.0, "magic": 0, "comment": ""}]}
        with mock.patch.dict(os.environ, self._ENV, clear=False), \
                mock.patch.object(observer, "_http_get_json", return_value=payload):
            res = observer.fetch_master_positions(acct)
        self.assertTrue(res.ok)
        self.assertEqual(len(res.positions), 1)
        self.assertEqual(res.positions[0].ticket, 1)

    def test_wrong_login_refused(self):
        acct = self._legacy_account(number="1302575")
        payload = {"ok": True, "account_login": "9999999", "account_server": "IS6Technologies-Demo",
                   "trade_mode": 0, "positions": []}
        with mock.patch.dict(os.environ, self._ENV, clear=False), \
                mock.patch.object(observer, "_http_get_json", return_value=payload):
            res = observer.fetch_master_positions(acct)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "snapshot_identity_login_mismatch")

    def test_environment_mismatch_refused(self):
        # DEMO-classified account, but the terminal reports REAL (trade_mode=2) -> refused.
        acct = self._legacy_account(number="1302575", is_demo=True, environment="demo")
        payload = {"ok": True, "account_login": "1302575", "account_server": "IS6Technologies-Demo",
                   "trade_mode": 2, "positions": []}
        with mock.patch.dict(os.environ, self._ENV, clear=False), \
                mock.patch.object(observer, "_http_get_json", return_value=payload):
            res = observer.fetch_master_positions(acct)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "snapshot_identity_environment_mismatch")

    def test_live_master_read_only_observation_allowed(self):
        # A clean LIVE account observed read-only: trade_mode=2 matches a LIVE classification -> ok.
        acct = self._legacy_account(number="51763830", is_demo=False, server="TradersWay-Live",
                                    environment="live")
        payload = {"ok": True, "account_login": "51763830", "account_server": "TradersWay-Live",
                   "trade_mode": 2, "positions": []}
        with mock.patch.dict(os.environ, self._ENV, clear=False), \
                mock.patch.object(observer, "_http_get_json", return_value=payload):
            res = observer.fetch_master_positions(acct)
        self.assertTrue(res.ok, res.reason)

    def test_no_token_fails_closed(self):
        acct = self._legacy_account()
        with mock.patch.dict(os.environ, {"WINDOWS_AGENT_BASE": "", "WINDOWS_AGENT_TOKEN": "",
                                          "GUVFX_AGENT_URL": "", "GUVFX_AGENT_TOKEN": ""}, clear=False):
            res = observer.fetch_master_positions(acct)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "no_agent_token")
