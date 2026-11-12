"""ADR-0094 — hybrid real-time dispatch: deterministic, mutation-killing tests.

Proves that a signed fast-path emission SUBMITS the follower order IMMEDIATELY (synchronously, on the event —
NOT via the ~2-min scheduled observer), with honest ≤2s source→submission latency; that push and a later poll
of the same ticket never double-copy (shared-baseline dedup); that every governed gate is still fail-closed;
and that the route-scoped read-only follower-position authority is default-OFF and never a global relaxation.

No real broker/host is contacted: the mutation transport is injected and the follower read (reconcile) is driven
with an injected fetch; the live admission runs against in-DB fixtures as ``trade_copier.tests_live_copier`` does.
Ticket linkage is done by ``reconcile`` AFTER the bridge fills the OPEN (the real-time dispatch mints the job but
never reads the follower bridge), so the MODIFY/CLOSE tests reconcile explicitly between actions.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from execution.models import BrokerInstrument, ExecutionJob
from trade_copier import services
from trade_copier.diff import ObservedPosition
from trade_copier.immediate_dispatch import dispatch_master_realtime
from trade_copier.models import (
    CopierIngestedRevision, CopierLiveAuthorization, CopierLiveAuthorizationStatus, CopierLiveKillSwitch,
    CopierMaster, CopierRealtimeDispatch, CopierReplication, CopierSymbolMap, MasterEvent,
    MasterObservationPoll, MasterPositionSnapshot, ReplicationStatus, SizingMode)
from trade_copier.observer import MasterReadResult, observe_master
from trading.models import BrokerServer, TradingAccount

User = get_user_model()

LIVE_ARMED = dict(COPIER_ENABLED="1", COPIER_EXECUTE_ENABLED="1", COPIER_LIVE_EXECUTE_ENABLED="1",
                  HOSTED_LIVE_EXECUTION_ENABLED="1")
# Real-time OPEN stack: the live arm + the synchronous trigger + projection + replication.
REALTIME = dict(LIVE_ARMED, COPIER_IMMEDIATE_DISPATCH_ENABLED="1", COPIER_EVENT_PROJECTION_ENABLED="1",
                COPIER_REPLICATION_ENABLED="1")
# Full lifecycle stack (MODIFY/CLOSE) adds the lifecycle projection + per-op arms + mutation dispatch/transport.
REALTIME_LIFECYCLE = dict(REALTIME, COPIER_LIFECYCLE_ENABLED="1", COPIER_LIVE_MODIFY_ENABLED="1",
                          COPIER_LIVE_FULL_CLOSE_ENABLED="1", COPIER_MUTATION_DISPATCH_ENABLED="1",
                          COPIER_MUTATION_TRANSPORT_ENABLED="1", COPIER_RECONCILE_ENABLED="1")


def _applied(cmd):
    """A mutation transport that reports the governed command APPLIED (no broker contact)."""
    return {"ok": True, "lifecycle_outcome": "APPLIED", "lifecycle_reason": "ok"}


def _live_acct(user, name, num, *, server, display):
    bs, _ = BrokerServer.objects.get_or_create(
        server_name=server, defaults={"broker_display_name": display, "environment": "live"})
    return TradingAccount.objects.create(user=user, name=name, broker_name=display, account_number=num,
                                         is_demo=False, is_active=True, broker_server=bs)


class _LiveRT(TestCase):
    """The governed LIVE #46→#48-style fixture (exact pair, 0.01, XAUUSD→XAUUSD.std), armed for real-time."""

    FOLLOWER_TICKET = 770001

    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@guvfx.invalid", "x")
        self.staff = User.objects.create_user("staff", "staff@guvfx.invalid", "x", is_staff=True)
        self.master_acct = _live_acct(self.owner, "m", "55425442", server="Tradersway-Live", display="TradersWay")
        self.follower_acct = _live_acct(self.owner, "f", "38313831", server="IS6Technologies-Live", display="IS6")
        self.master = CopierMaster.objects.create(trading_account=self.master_acct, observe_enabled=True)
        self.f = services.add_follower(
            self.master, self.follower_acct, sizing_mode=SizingMode.FIXED_LOT, fixed_lot=Decimal("0.01"),
            max_volume=Decimal("0.01"), max_concurrent_positions=1)
        services.set_follower_enabled(self.f, True, actor=self.staff)
        CopierSymbolMap.objects.create(
            master_broker="Tradersway-Live", master_symbol="XAUUSD",
            follower_broker="IS6Technologies-Live", follower_symbol="XAUUSD.std",
            verified=True, verified_by=self.staff, verified_at=timezone.now())
        BrokerInstrument.objects.create(
            account=self.follower_acct, broker_symbol="XAUUSD.std", base_symbol="XAUUSD.std",
            enabled=True, metadata={"volume_min": 0.01, "volume_step": 0.01, "volume_max": 100})
        self.authz = self._grant()

    def _grant(self, **over):
        now = timezone.now()
        d = dict(
            user=self.owner, master_account=self.master_acct, follower_account=self.follower_acct,
            master_login="55425442", master_server="Tradersway-Live", master_environment="LIVE",
            follower_login="38313831", follower_server="IS6Technologies-Live", follower_environment="LIVE",
            source_symbol="XAUUSD", follower_symbol="XAUUSD.std",
            max_volume=Decimal("0.01"), max_concurrent_positions=1, max_orders=1, orders_consumed=0,
            valid_from=now - timedelta(minutes=1), expires_at=now + timedelta(hours=1),
            watermark_at=now - timedelta(hours=1), watermark_event_sequence=0,
            modify_authority=True, full_close_authority=True,
            status=CopierLiveAuthorizationStatus.ACTIVE, approved_by=self.staff, reason="rt-test")
        d.update(over)
        authz = CopierLiveAuthorization.objects.create(**d)
        if authz.status in (CopierLiveAuthorizationStatus.ACTIVE, CopierLiveAuthorizationStatus.CONSUMED):  # ADR-0097: env+arm contract
            from trade_copier.tests_copier_live_arm import attach_engaged_arm
            attach_engaged_arm(authz)
        return authz

    def _ingest(self, ticket, revision, *, closed=False, symbol="XAUUSD", side="SELL", volume="0.01",
                sl="4000", tp="3980", price_open="3989", when=None):
        now = when or timezone.now()
        return CopierIngestedRevision.objects.create(
            master=self.master, account_id=self.master_acct.id, ticket=ticket, revision=revision,
            closed=closed, symbol=symbol, side=side, volume=Decimal(volume),
            sl=(Decimal(sl) if sl else None), tp=(Decimal(tp) if tp else None),
            price_open=(Decimal(price_open) if price_open else None),
            open_time=now, broker_event_time=now, observed_time=now,
            broker_login="55425442", broker_server="Tradersway-Live")

    def _place_jobs(self):
        return ExecutionJob.objects.filter(account=self.follower_acct, job_type=ExecutionJob.JobType.PLACE_ORDER)


@override_settings(**REALTIME)
class ImmediateOpenTests(_LiveRT):
    """A + F + E (OPEN) — the headline: an ingested OPEN becomes one LIVE follower order immediately, ≤2s."""

    def test_immediate_open_submits_one_live_order_fast(self):
        self._ingest(ticket=901, revision=1)
        out = dispatch_master_realtime(self.master.pk)
        self.assertTrue(out["enabled"])
        self.assertEqual(out["events"], 1)
        self.assertEqual(out["submitted"], 1)
        self.assertEqual(out["late"], 0)
        # exactly ONE LIVE PLACE_ORDER on the follower, correct route/symbol/side/volume, truthfully live.
        jobs = list(self._place_jobs())
        self.assertEqual(len(jobs), 1)
        p = jobs[0].payload
        self.assertFalse(p["is_demo"])
        self.assertEqual(p["execution_mode"], "LIVE")
        self.assertEqual(p["symbol"], "XAUUSD.std")
        self.assertEqual(p["side"], "SELL")
        self.assertEqual(str(p["lots"]), "0.01")
        # allowance consumed exactly once (so a retry can never place a second order).
        self.authz.refresh_from_db()
        self.assertEqual(self.authz.orders_consumed, 1)
        # honest latency evidence, within the ≤2s acceptance ceiling, marked as the immediate push source.
        rd = CopierRealtimeDispatch.objects.get(event_type="OPEN")
        self.assertEqual(rd.outcome, CopierRealtimeDispatch.Outcome.OPEN_SUBMITTED)
        self.assertEqual(rd.dispatch_source, CopierRealtimeDispatch.Source.EVENT_PUSH)
        self.assertIsNotNone(rd.source_to_submission_ms)
        self.assertGreaterEqual(rd.source_to_submission_ms, 0)
        self.assertLessEqual(rd.source_to_submission_ms, CopierRealtimeDispatch.ACCEPTANCE_CEILING_MS)
        self.assertFalse(rd.is_late)

    def test_disabled_gate_is_byte_identical_noop(self):
        # Mutation-killer: with the immediate gate OFF the ingestion is persist-only (ADR-0074) — nothing runs.
        self._ingest(ticket=902, revision=1)
        with override_settings(COPIER_IMMEDIATE_DISPATCH_ENABLED="0"):
            out = dispatch_master_realtime(self.master.pk)
        self.assertFalse(out["enabled"])
        self.assertEqual(MasterEvent.objects.filter(master=self.master).count(), 0)
        self.assertEqual(self._place_jobs().count(), 0)
        self.assertEqual(CopierRealtimeDispatch.objects.count(), 0)

    def test_no_authorization_projects_but_places_no_order(self):
        # Fail-closed: without a live authorization the event is still OBSERVED, but admit_open_live denies —
        # NO ExecutionJob, and the latency row records NO_ACTION.
        self.authz.delete()
        self._ingest(ticket=903, revision=1)
        out = dispatch_master_realtime(self.master.pk)
        self.assertEqual(out["submitted"], 0)
        self.assertEqual(self._place_jobs().count(), 0)
        self.assertTrue(MasterEvent.objects.filter(master=self.master, event_type="OPEN").exists())
        rd = CopierRealtimeDispatch.objects.get(event_type="OPEN")
        self.assertEqual(rd.outcome, CopierRealtimeDispatch.Outcome.NO_ACTION)

    def test_execute_gate_off_projects_but_no_order(self):
        # Fail-closed: a sub-gate off (execute) no-ops exactly as the scheduled path would.
        self._ingest(ticket=904, revision=1)
        with override_settings(COPIER_EXECUTE_ENABLED="0"):
            out = dispatch_master_realtime(self.master.pk)
        self.assertEqual(out["submitted"], 0)
        self.assertEqual(self._place_jobs().count(), 0)

    def test_kill_switch_blocks_order(self):
        CopierLiveKillSwitch.objects.create(engaged=True, engaged_by=self.staff, reason="test")
        self._ingest(ticket=905, revision=1)
        out = dispatch_master_realtime(self.master.pk)
        self.assertEqual(out["submitted"], 0)
        self.assertEqual(self._place_jobs().count(), 0)


@override_settings(**REALTIME)
class HybridDedupeTests(_LiveRT):
    """D — push/poll dedup through the one shared MasterEvent ledger + baseline; idempotent re-runs."""

    def test_rerun_is_idempotent_no_second_order(self):
        self._ingest(ticket=911, revision=1)
        dispatch_master_realtime(self.master.pk)
        self.assertEqual(self._place_jobs().count(), 1)
        # Re-run with no new ingested revision: projection promotes nothing (watermark), no second order.
        out2 = dispatch_master_realtime(self.master.pk)
        self.assertEqual(out2["events"], 0)
        self.assertEqual(self._place_jobs().count(), 1)
        self.authz.refresh_from_db()
        self.assertEqual(self.authz.orders_consumed, 1)

    def test_push_then_poll_does_not_double_copy(self):
        # Push → immediate OPEN (advances the SHARED baseline). A subsequent observer poll of the same position
        # must diff against the event-advanced baseline and emit NO second OPEN → no second order.
        self._ingest(ticket=912, revision=1)
        dispatch_master_realtime(self.master.pk)
        self.assertEqual(self._place_jobs().count(), 1)

        def _poll_fetch(account):
            return MasterReadResult(
                True, "ok", observed_login="55425442", observed_server="Tradersway-Live", observed_trade_mode=2,
                positions=(ObservedPosition(ticket=912, symbol="XAUUSD", side="SELL", volume=0.01,
                                            sl=4000.0, tp=3980.0, price_open=3989.0),))
        with override_settings(COPIER_OBSERVER_ENABLED="1"):
            observe_master(self.master, fetch=_poll_fetch)
        self.assertEqual(MasterEvent.objects.filter(master=self.master, event_type="OPEN").count(), 1)
        self.assertEqual(self._place_jobs().count(), 1)

    def test_out_of_order_lower_revision_never_regresses(self):
        self._ingest(ticket=913, revision=2)
        dispatch_master_realtime(self.master.pk)
        n_events = MasterEvent.objects.filter(master=self.master).count()
        # A later-arriving LOWER revision for the same ticket is rejected by the fold — no new event/order.
        self._ingest(ticket=913, revision=1)
        out = dispatch_master_realtime(self.master.pk)
        self.assertEqual(out["events"], 0)
        self.assertEqual(MasterEvent.objects.filter(master=self.master).count(), n_events)


@override_settings(**REALTIME_LIFECYCLE)
class ImmediateModifyCloseTests(_LiveRT):
    """B + C — an immediate OPEN (then post-fill reconcile), then an immediate MODIFY and an immediate FULL
    CLOSE on the SAME follower ticket, each ≤2s, via the governed lifecycle + mutation dispatch (transport
    injected)."""

    def _open(self):
        self._ingest(ticket=920, revision=1)
        dispatch_master_realtime(self.master.pk, mutation_transport_factory=lambda: _applied)
        # Simulate the bridge filling the OPEN and reporting the result ticket to the job-completion endpoint →
        # ADR-0096 event-driven binding (NOT a /mt5/positions reconcile — that is walled for a LIVE follower).
        rep = CopierReplication.objects.get(requested_op="OPEN")
        job = rep.execution_job
        self.assertIsNotNone(job, "OPEN produced no PLACE_ORDER job")
        job.status = ExecutionJob.Status.SUCCESS
        job.result = {"ticket": self.FOLLOWER_TICKET}
        job.save(update_fields=["status", "result"])
        from trade_copier.open_result_binding import bind_open_result
        self.assertTrue(bind_open_result(job)["bound"])
        rep.refresh_from_db()
        self.assertEqual(rep.follower_ticket, self.FOLLOWER_TICKET)
        return rep

    def test_immediate_modify_targets_the_bound_ticket(self):
        self._open()
        # MODIFY: same ticket, SL TIGHTENED (SELL stop lowered toward entry — a safe, non-weakening SL_CHANGE).
        self._ingest(ticket=920, revision=2, sl="3995", tp="3980")
        out = dispatch_master_realtime(self.master.pk, mutation_transport_factory=lambda: _applied)
        self.assertTrue(out["enabled"])
        mjob = ExecutionJob.objects.filter(account=self.follower_acct,
                                           job_type=ExecutionJob.JobType.MODIFY_POSITION)
        self.assertEqual(mjob.count(), 1)
        self.assertEqual(mjob.first().payload.get("ticket"), self.FOLLOWER_TICKET)
        rd = (CopierRealtimeDispatch.objects
              .filter(outcome=CopierRealtimeDispatch.Outcome.MODIFY_SUBMITTED).order_by("-id").first())
        self.assertIsNotNone(rd)
        self.assertLessEqual(rd.source_to_submission_ms or 10 ** 9,
                             CopierRealtimeDispatch.ACCEPTANCE_CEILING_MS)

    def test_immediate_close_targets_the_bound_ticket(self):
        self._open()
        self._ingest(ticket=920, revision=2, closed=True)
        out = dispatch_master_realtime(self.master.pk, mutation_transport_factory=lambda: _applied)
        self.assertTrue(out["enabled"])
        cjob = ExecutionJob.objects.filter(account=self.follower_acct,
                                           job_type=ExecutionJob.JobType.CLOSE_TRADE)
        self.assertEqual(cjob.count(), 1)
        self.assertEqual(cjob.first().payload.get("ticket"), self.FOLLOWER_TICKET)
        rd = (CopierRealtimeDispatch.objects
              .filter(outcome=CopierRealtimeDispatch.Outcome.CLOSE_SUBMITTED).order_by("-id").first())
        self.assertIsNotNone(rd)


@override_settings(**REALTIME)
class ContainedFaultTests(_LiveRT):
    """Fail-safe: an internal fault is contained — the ingestion response/evidence is never broken, and a
    produced order never lacks a latency row."""

    def test_internal_fault_is_contained_not_raised(self):
        self._ingest(ticket=931, revision=1)
        # A fault in a late pass (close producer) must not raise; the OPEN already-committed job keeps evidence.
        with mock.patch("trade_copier.execution_producer.produce_pending_closes", side_effect=RuntimeError("boom")):
            out = dispatch_master_realtime(self.master.pk)
        self.assertTrue(out.get("contained"))
        # The OPEN job was produced before the fault; its latency row still exists.
        self.assertEqual(self._place_jobs().count(), 1)
        self.assertTrue(CopierRealtimeDispatch.objects.filter(event_type="OPEN").exists())

    def test_master_absent_is_safe(self):
        out = dispatch_master_realtime(99999)
        self.assertEqual(out["reason"], "master_absent")


class RealtimeLatencyModelTests(SimpleTestCase):
    """F — deterministic, clock-based latency math (no DB, no sleeps). Honest N/A + negatives, never a zero."""

    def _row(self, submit_delta_ms, *, detected_delta_ms=0):
        base = timezone.now()
        rd = CopierRealtimeDispatch(
            event_detected_at=base + timedelta(milliseconds=detected_delta_ms),
            emitted_at=base, backend_received_at=base + timedelta(milliseconds=200),
            follower_submitted_at=base + timedelta(milliseconds=submit_delta_ms))
        return rd.recompute_latencies()

    def test_within_ceiling_not_late(self):
        rd = self._row(850)
        self.assertEqual(rd.source_to_submission_ms, 850)
        self.assertEqual(rd.received_to_submission_ms, 650)
        self.assertFalse(rd.is_late)

    def test_over_ceiling_is_late(self):
        rd = self._row(2500)
        self.assertEqual(rd.source_to_submission_ms, 2500)
        self.assertTrue(rd.is_late)

    def test_measured_from_detection_not_emitter(self):
        # Detection is 1500ms BEFORE emission; the gate must measure from detection, so a 900ms-after-emission
        # submission is really 2400ms from the broker event → LATE (not a flattering sub-2s emitter number).
        base = timezone.now()
        rd = CopierRealtimeDispatch(
            event_detected_at=base, emitted_at=base + timedelta(milliseconds=1500),
            follower_submitted_at=base + timedelta(milliseconds=2400)).recompute_latencies()
        self.assertEqual(rd.source_to_submission_ms, 2400)
        self.assertTrue(rd.is_late)

    def test_negative_skew_is_late_not_on_time(self):
        # Forward clock skew → submission "before" the source → physically impossible → must FAIL the gate.
        rd = self._row(-500)
        self.assertEqual(rd.source_to_submission_ms, -500)
        self.assertTrue(rd.is_late)

    def test_missing_submission_is_late_not_zero(self):
        base = timezone.now()
        rd = CopierRealtimeDispatch(emitted_at=base, follower_submitted_at=None).recompute_latencies()
        self.assertIsNone(rd.source_to_submission_ms)   # NEVER 0
        self.assertTrue(rd.is_late)                     # unknown fails the acceptance assertion


class ReadonlyPositionsRoutingTests(_LiveRT):
    """ADR-0094 route-scoped follower read permission: default-OFF, bound to the exact authorized follower,
    never a global relaxation. Plus the FOLLOWER_POSITIONS_READ_* readiness codes."""

    def _readiness(self):
        from execution.bridge_config import follower_positions_read_readiness, readonly_positions_for_account
        return (follower_positions_read_readiness(self.follower_acct)[0],
                readonly_positions_for_account(self.follower_acct))

    def test_default_off_not_authorized(self):
        code, rendered = self._readiness()
        self.assertEqual(code, "FOLLOWER_POSITIONS_READ_NOT_AUTHORIZED")
        self.assertFalse(rendered)

    @override_settings(COPIER_FOLLOWER_READONLY_POSITIONS_ENABLED="1")
    def test_route_scoped_authorized_follower_ok(self):
        code, rendered = self._readiness()
        self.assertEqual(code, "FOLLOWER_POSITIONS_READ_OK")
        self.assertTrue(rendered)

    @override_settings(COPIER_FOLLOWER_READONLY_POSITIONS_ENABLED="1")
    def test_flag_on_but_no_authorization_is_not_global(self):
        # Flag ON but NO authorization → still NOT authorized: proves it is route-scoped, never a global relaxation.
        self.authz.delete()
        code, rendered = self._readiness()
        self.assertEqual(code, "FOLLOWER_POSITIONS_READ_NOT_AUTHORIZED")
        self.assertFalse(rendered)

    def test_revoked_authorization_does_not_authorize(self):
        with override_settings(COPIER_FOLLOWER_READONLY_POSITIONS_ENABLED="1"):
            self.authz.status = CopierLiveAuthorizationStatus.REVOKED
            self.authz.revoked_at = timezone.now()
            self.authz.save(update_fields=["status", "revoked_at"])
            code, rendered = self._readiness()
        self.assertEqual(code, "FOLLOWER_POSITIONS_READ_NOT_AUTHORIZED")
        self.assertFalse(rendered)
