"""ADR-0074 Phase-4 — the authoritative event→MasterEvent projection.

Proves the single-authoritative-object design: an ingested fast-path revision deterministically advances the
ADR-0071 lifecycle by being promoted into the ONE authoritative ``MasterEvent`` ledger (shared sequence +
baseline), via an OVERLAY onto the poll baseline (never a bare partial-snapshot diff). Mutation-killers:
no spurious close of an untouched baseline ticket; poll and event dedup through the shared baseline; a stale
(old broker_event_time) OPEN is ruled ENTRY_FAILED_FINAL by the UNCHANGED state machine — no late entry;
idempotent across re-runs; out-of-order/duplicate emissions never regress the ledger.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from trade_copier import lifecycle as lc, services
from trade_copier.diff import ObservedPosition
from trade_copier.event_projection import run_event_projection_pass
from trade_copier.lifecycle import LifecyclePolicy, LifecycleState
from trade_copier.models import (CopierIngestedRevision, CopierMaster, CopierPositionLifecycle, MasterEvent,
                                 MasterObservationPoll, MasterPositionSnapshot)
from trade_copier.observer import record_observation, MasterReadResult
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
PROJ = dict(COPIER_ENABLED=True, COPIER_EVENT_PROJECTION_ENABLED=True)
POLICY = LifecyclePolicy()


@override_settings(**PROJ)
class EventProjectionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-Demo",
                                              environment="demo")
        self.macct = TradingAccount.objects.create(user=self.user, name="m", broker_name="IS6",
                                                   account_number="1000001", is_demo=True, is_active=True,
                                                   broker_server=self.bs)
        self.master = CopierMaster.objects.create(trading_account=self.macct, observe_enabled=True)
        facct = TradingAccount.objects.create(user=self.user, name="f", broker_name="IS6",
                                              account_number="2000001", is_demo=True, is_active=True,
                                              broker_server=self.bs)
        services.set_follower_enabled(services.add_follower(self.master, facct), True)

    def _baseline(self, positions):
        """Set the master's diff baseline (last_poll) to the given ObservedPosition list."""
        poll = MasterObservationPoll.objects.create(master=self.master, observed_at=timezone.now(),
                                                    read_at=timezone.now(), position_count=len(positions))
        for p in positions:
            MasterPositionSnapshot.objects.create(
                poll=poll, position_ticket=p.ticket, symbol=p.symbol, side=p.side,
                volume=Decimal(str(p.volume)),
                sl=(Decimal(str(p.sl)) if p.sl is not None else None),
                tp=(Decimal(str(p.tp)) if p.tp is not None else None))
        self.master.last_poll = poll
        self.master.save(update_fields=["last_poll"])
        return poll

    def _ingest(self, ticket, revision, *, closed=False, symbol="XAUUSD", side="BUY", volume=0.1,
                sl=None, tp=None, bet=None, open_time=None):
        return CopierIngestedRevision.objects.create(
            master=self.master, account_id=self.macct.id, ticket=ticket, revision=revision, source="EVENT",
            closed=closed, symbol=symbol, side=side, volume=Decimal(str(volume)),
            sl=(Decimal(str(sl)) if sl is not None else None),
            tp=(Decimal(str(tp)) if tp is not None else None), open_time=open_time,
            broker_event_time=(bet or timezone.now()), observed_time=timezone.now())

    def test_open_revision_promoted_to_authoritative_open_event(self):
        self._ingest(ticket=10, revision=1, volume=0.1)
        out = run_event_projection_pass()
        self.assertEqual(out["promoted_events"], 1)
        ev = MasterEvent.objects.get(master=self.master, position_ticket=10)
        self.assertEqual(ev.event_type, MasterEvent.EventType.OPEN)
        self.assertEqual(ev.source, "EVENT")
        self.assertIsNotNone(ev.broker_event_time)
        self.master.refresh_from_db()
        self.assertEqual(self.master.last_projected_ingest_id, CopierIngestedRevision.objects.latest("id").id)

    def test_overlay_does_not_close_untouched_baseline_ticket(self):
        # Baseline has ticket A(1) and B(2); an event touches ONLY B (SL set). A must NOT be closed.
        self._baseline([ObservedPosition(ticket=1, symbol="XAUUSD", side="BUY", volume=1.0),
                        ObservedPosition(ticket=2, symbol="XAUUSD", side="BUY", volume=1.0)])
        self._ingest(ticket=2, revision=1, volume=1.0, sl=1990.0)
        run_event_projection_pass()
        types = sorted(MasterEvent.objects.filter(master=self.master).values_list("event_type", flat=True))
        self.assertEqual(types, [MasterEvent.EventType.SL_SET])        # ONLY the SL set — A untouched
        self.assertFalse(MasterEvent.objects.filter(position_ticket=1).exists())

    def test_poll_and_event_dedup_through_shared_baseline(self):
        # Event opens ticket 5 → promoted + advances last_poll. A later real poll seeing the SAME position
        # must emit NOTHING (the baseline already reflects it) — one ledger, no double.
        self._ingest(ticket=5, revision=1, volume=0.2)
        run_event_projection_pass()
        self.assertEqual(MasterEvent.objects.filter(position_ticket=5).count(), 1)
        self.master.refresh_from_db()
        result = MasterReadResult(True, "ok", observed_login="1000001", observed_server="IS6Technologies-Demo",
                                  positions=(ObservedPosition(ticket=5, symbol="XAUUSD", side="BUY", volume=0.2),))
        record_observation(self.master, result)
        self.assertEqual(MasterEvent.objects.filter(position_ticket=5).count(), 1)   # poll added NO duplicate

    def test_partial_and_full_close_promoted(self):
        self._baseline([ObservedPosition(ticket=7, symbol="XAUUSD", side="BUY", volume=1.0)])
        self._ingest(ticket=7, revision=1, volume=0.6)     # reduction → PARTIAL_CLOSE
        run_event_projection_pass()
        self.assertTrue(MasterEvent.objects.filter(position_ticket=7,
                                                   event_type=MasterEvent.EventType.PARTIAL_CLOSE).exists())
        self._ingest(ticket=7, revision=2, closed=True, volume=0)   # gone → FULL_CLOSE
        run_event_projection_pass()
        self.assertTrue(MasterEvent.objects.filter(position_ticket=7,
                                                   event_type=MasterEvent.EventType.FULL_CLOSE).exists())

    def test_idempotent_rerun_promotes_nothing_new(self):
        self._ingest(ticket=9, revision=1, volume=0.1)
        run_event_projection_pass()
        n1 = MasterEvent.objects.count()
        run_event_projection_pass()          # no new ingested rows
        self.assertEqual(MasterEvent.objects.count(), n1)

    def test_out_of_order_lower_revision_never_regresses(self):
        # Promote revision 2 (SL=2000) first; a late revision 1 (SL removed) must be rejected by the fold.
        self._baseline([ObservedPosition(ticket=3, symbol="XAUUSD", side="BUY", volume=1.0)])
        self._ingest(ticket=3, revision=2, volume=1.0, sl=2000.0)
        run_event_projection_pass()
        self.assertTrue(MasterEvent.objects.filter(position_ticket=3,
                                                   event_type=MasterEvent.EventType.SL_SET).exists())
        before = MasterEvent.objects.count()
        self._ingest(ticket=3, revision=1, volume=1.0, sl=None)   # stale (lower revision)
        run_event_projection_pass()
        self.assertEqual(MasterEvent.objects.count(), before)     # the stale emission produced no event

    def test_stale_poll_discarded_against_event_baseline(self):
        # H2 regression: an event projection stamps the event poll read_at=now, so a poll whose network read
        # PREDATES the projection is discarded by the observer's stale-read backstop — never diffed against the
        # event-advanced baseline (which would emit a spurious VOLUME_INCREASE / revert the baseline).
        self._ingest(ticket=20, revision=1, volume=1.0)
        run_event_projection_pass()
        self.master.refresh_from_db()
        ev_read_at = self.master.last_poll.read_at
        self.assertIsNotNone(ev_read_at)
        stale = MasterReadResult(True, "ok", observed_login="1000001", observed_server="IS6Technologies-Demo",
                                 positions=(ObservedPosition(ticket=20, symbol="XAUUSD", side="BUY", volume=0.5),),
                                 read_at=ev_read_at - timedelta(seconds=1))
        out = record_observation(self.master, stale)
        self.assertTrue(out.get("stale_discarded"))
        self.assertFalse(MasterEvent.objects.filter(
            position_ticket=20, event_type=MasterEvent.EventType.VOLUME_INCREASE).exists())

    def test_stale_open_is_entry_failed_final_no_late_entry(self):
        # A buffered OPEN whose true broker OPEN time is far past the admissibility window must NOT create a live
        # entry: the projection uses the position's open_time as the OPEN admissibility reference (the honest,
        # positions-feed-available age), and the unchanged lifecycle rules it ENTRY_FAILED_FINAL. This is the
        # REALISTIC path — the bridge supplies open_time, not a per-change broker_event_time.
        old = timezone.now() - timedelta(seconds=10_000)
        self._ingest(ticket=11, revision=1, volume=0.1, open_time=old, bet=None)
        run_event_projection_pass()
        ev = MasterEvent.objects.get(position_ticket=11)
        self.assertEqual(ev.broker_event_time, old)   # OPEN admissibility reference = the true open_time
        with override_settings(COPIER_ENABLED=True, COPIER_LIFECYCLE_ENABLED=True):
            lc.run_lifecycle_pass(policy=POLICY)
        row = CopierPositionLifecycle.objects.get(master=self.master, master_position_ticket=11)
        self.assertEqual(row.state, LifecycleState.ENTRY_FAILED_FINAL.value)
        self.assertTrue(row.is_terminal)
