"""Observer recording + identity firewall + ledger certification (DB-backed, injected I/O)."""
import os
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from trade_copier import diff, observer
from trade_copier.diff import ObservedPosition as P
from trade_copier.models import CopierMaster, MasterEvent, MasterObservationPoll, MasterPositionSnapshot
from trade_copier.observer import MasterReadResult, observe_master, record_observation, run_observer_pass
from trading.models import BrokerServer, TradingAccount

User = get_user_model()


def _result(positions, *, login="1302575", server="IS6Technologies-Demo", trade_mode=0, read_at=None):
    return MasterReadResult(True, "ok", observed_login=login, observed_server=server,
                            observed_trade_mode=trade_mode, transport_reason="snapshot_transport_legacy_global",
                            positions=tuple(positions), read_at=read_at)


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

    def test_stale_out_of_order_read_discarded_no_false_events(self):
        # BLOCKER-2: an older read arriving AFTER a newer one has committed must be discarded —
        # never diffed against the newer baseline (which would fabricate closes + regress the baseline).
        m = self._master()
        t0 = timezone.now()
        observe_master(m, fetch=_queued_fetch([_result([P(1, "XAUUSD", "BUY", 0.10)], read_at=t0)]))
        observe_master(m, fetch=_queued_fetch([_result(
            [P(1, "XAUUSD", "BUY", 0.10), P(2, "EURUSD", "SELL", 0.5)],
            read_at=t0 + timedelta(seconds=2))]))                 # newer: OPEN pos2, baseline -> t0+2
        m.refresh_from_db()
        newer_poll = m.last_poll_id
        events_before = MasterEvent.objects.count()
        # Older read (t0+1s) arriving LATE, empty set (would be 2 false FULL_CLOSEs if applied).
        out = observe_master(m, fetch=lambda _a: _result([], read_at=t0 + timedelta(seconds=1)))
        self.assertEqual(out["reason"], "stale_read_discarded")
        m.refresh_from_db()
        self.assertEqual(m.last_poll_id, newer_poll)              # baseline NOT regressed
        self.assertEqual(MasterEvent.objects.count(), events_before)        # no fabricated events
        self.assertEqual(MasterEvent.objects.filter(event_type=diff.FULL_CLOSE).count(), 0)

    def test_single_flight_rejects_concurrent_observation(self):
        # BLOCKER-2 (bounded single-flight): a pass must not even read while another holds the lease.
        m = self._master()
        m.observation_lease_token = "other-pass"
        m.observation_lease_until = timezone.now() + timedelta(seconds=30)   # active lease
        m.save(update_fields=["observation_lease_token", "observation_lease_until"])
        called = {"n": 0}

        def _fetch(_a):
            called["n"] += 1
            return _result([P(1, "XAUUSD", "BUY", 0.10)])
        out = observe_master(m, fetch=_fetch)
        self.assertEqual(out["reason"], "observation_in_flight")
        self.assertEqual(called["n"], 0)                          # did NOT perform the remote read
        self.assertEqual(MasterObservationPoll.objects.count(), 0)

    def test_late_older_response_after_lease_takeover_discarded(self):
        # Adversarial INVERTED completion order (BLOCKER-2): pass A captured an OLDER snapshot and was
        # slow; its lease expired; pass B captured a NEWER snapshot and committed; A's response then
        # arrives LAST. A must be discarded (lease lost) — never regress the baseline or fabricate closes,
        # regardless of A's (later) response-arrival timestamp.
        m = self._master()
        t0 = timezone.now()
        observe_master(m, fetch=_queued_fetch([_result([P(1, "XAUUSD", "BUY", 0.10)], read_at=t0)]))  # baseline P1
        # Pass A claimed earlier; its lease has EXPIRED by now.
        m.refresh_from_db()
        m.observation_lease_token = "A"
        m.observation_lease_until = t0 - timedelta(seconds=1)
        m.save(update_fields=["observation_lease_token", "observation_lease_until"])
        # Pass B takes over (A's lease expired) and commits a NEWER snapshot adding P2.
        observe_master(m, fetch=_queued_fetch([_result(
            [P(1, "XAUUSD", "BUY", 0.10), P(2, "EURUSD", "SELL", 0.5)], read_at=t0 + timedelta(seconds=5))]))
        m.refresh_from_db()
        b_poll = m.last_poll_id
        events_before = MasterEvent.objects.count()
        # Pass A's OLDER (empty) response arrives LAST with a HIGHER arrival time but a stale lease token.
        out = record_observation(m, _result([], read_at=t0 + timedelta(seconds=9)), lease_token="A")
        self.assertTrue(out.get("lease_lost"))
        m.refresh_from_db()
        self.assertEqual(m.last_poll_id, b_poll)                  # baseline NOT regressed
        self.assertEqual(MasterEvent.objects.count(), events_before)       # no fabricated events
        self.assertEqual(MasterEvent.objects.filter(event_type=diff.FULL_CLOSE).count(), 0)

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

    def _ok_payload(self, positions):
        return {"ok": True, "account_login": "1302575", "account_server": "IS6Technologies-Demo",
                "trade_mode": 0, "positions": positions}

    def test_null_positions_with_ok_true_is_refused_not_flat(self):
        # BLOCKER-1: ok:true + positions:null is an INVALID read, not a confirmed-flat account.
        acct = self._legacy_account(number="1302575")
        with mock.patch.dict(os.environ, self._ENV, clear=False), \
                mock.patch.object(observer, "_http_get_json", return_value=self._ok_payload(None)):
            res = observer.fetch_master_positions(acct)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "positions_not_a_list")

    def test_missing_positions_field_is_refused(self):
        acct = self._legacy_account(number="1302575")
        payload = {"ok": True, "account_login": "1302575", "account_server": "IS6Technologies-Demo",
                   "trade_mode": 0}  # no "positions" key
        with mock.patch.dict(os.environ, self._ENV, clear=False), \
                mock.patch.object(observer, "_http_get_json", return_value=payload):
            res = observer.fetch_master_positions(acct)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "positions_not_a_list")

    def test_empty_list_is_a_valid_flat_account(self):
        # Confirmed flat (ok:true + empty LIST) is authoritative and accepted.
        acct = self._legacy_account(number="1302575")
        with mock.patch.dict(os.environ, self._ENV, clear=False), \
                mock.patch.object(observer, "_http_get_json", return_value=self._ok_payload([])):
            res = observer.fetch_master_positions(acct)
        self.assertTrue(res.ok)
        self.assertEqual(len(res.positions), 0)

    def test_duplicate_tickets_refused(self):
        acct = self._legacy_account(number="1302575")
        dup = [{"ticket": 1, "symbol": "XAUUSD", "type": 0, "volume": 0.1},
               {"ticket": 1, "symbol": "XAUUSD", "type": 0, "volume": 0.2}]
        with mock.patch.dict(os.environ, self._ENV, clear=False), \
                mock.patch.object(observer, "_http_get_json", return_value=self._ok_payload(dup)):
            res = observer.fetch_master_positions(acct)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "duplicate_position_ticket")
