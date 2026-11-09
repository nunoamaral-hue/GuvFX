"""Adversarial synthetic tests for the LIVE lifecycle completion (ADR-0083): LIVE follower MODIFY_PROTECTION /
PARTIAL_CLOSE / FULL_CLOSE on an already-bound follower position.

Every gate in ``trade_copier.live_execution.admit_{modify,partial_close,full_close}_live`` has a distinct
killing case: remove exactly one precondition and admission must deny with its reason, and NO ExecutionJob may
be created. The job-creation functions are proven to (a) emit the correct typed command, (b) never consume the
one-shot ENTRY allowance, and (c) NEVER produce a PLACE_ORDER (no path here can open/increase exposure). The
close-only containment policy (after expiry / under kill switch) is asserted both ways. Nothing contacts a
broker: the follower read is a synthetic fake ``fetch``; identity/binding use the real DB-backed predicates.

These are DARK: the whole LIVE stack + the per-op arm must be set for any admission to pass.
"""
from decimal import Decimal
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution.models import ExecutionJob
from trade_copier import services
from trade_copier.diff import ObservedPosition
from trade_copier.observer import MasterReadResult
from trade_copier.execution_producer import (
    admit_modify, admit_close, admit_partial_close,
    produce_modify_one, produce_close_one, produce_partial_close_one)
from trade_copier.live_execution import (
    admit_modify_live, admit_full_close_live, admit_partial_close_live,
    create_live_modify_job, create_live_full_close_job, create_live_partial_close_job,
    lifecycle_authorization_for)
from trade_copier.models import (
    CopierFollower, CopierLiveAuthorization, CopierLiveAuthorizationStatus, CopierLiveKillSwitch,
    CopierMaster, CopierReplication, CopierSymbolMap, MasterEvent, MasterObservationPoll,
    MasterPositionSnapshot, ReplicationStatus, SizingMode)
from trade_copier.reconcile import MATCHED
from trading.models import BrokerServer, TradingAccount

User = get_user_model()

# The whole LIVE stack armed, incl. the explicit LIVE arm, the DARK hosted-live gate, and ALL three per-op arms.
LIVE = dict(COPIER_ENABLED="1", COPIER_EXECUTE_ENABLED="1", COPIER_LIVE_EXECUTE_ENABLED="1",
            COPIER_LIVE_MODIFY_ENABLED="1", COPIER_LIVE_PARTIAL_CLOSE_ENABLED="1",
            COPIER_LIVE_FULL_CLOSE_ENABLED="1", HOSTED_LIVE_EXECUTION_ENABLED="1")

MASTER_TICKET = 500
FOLLOWER_TICKET = 777


def _live_acct(user, name, num, *, server, display):
    bs, _ = BrokerServer.objects.get_or_create(
        server_name=server, defaults={"broker_display_name": display, "environment": "live"})
    return TradingAccount.objects.create(
        user=user, name=name, broker_name=display, account_number=num,
        is_demo=False, is_active=True, broker_server=bs)


def _fetch_follower(ticket, volume, symbol="XAUUSD.std"):
    """Synthetic fake bridge: the follower account currently holds one position (ticket, volume, symbol)."""
    def fetch(_account):
        return MasterReadResult(True, "ok", positions=(
            ObservedPosition(ticket=ticket, symbol=symbol, side="SELL", volume=volume),))
    return fetch


def _fetch_empty():
    def fetch(_account):
        return MasterReadResult(True, "ok", positions=())
    return fetch


@override_settings(**LIVE)
class LiveLifecycleFixture(TestCase):
    """A LINKED live pair: master #->follower, a MATCHED OPEN bound to FOLLOWER_TICKET, full per-op authority."""

    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@guvfx.invalid", "x")
        self.staff = User.objects.create_user("staff", "staff@guvfx.invalid", "x", is_staff=True)
        self.master_acct = _live_acct(self.owner, "m", "55425442", server="Tradersway-Live", display="TradersWay")
        self.follower_acct = _live_acct(self.owner, "f", "38313831", server="IS6Technologies-Live", display="IS6")
        self.master = CopierMaster.objects.create(trading_account=self.master_acct, observe_enabled=True)
        self.f = services.add_follower(
            self.master, self.follower_acct, sizing_mode=SizingMode.FIXED_LOT,
            fixed_lot=Decimal("0.01"), max_volume=Decimal("0.10"), max_concurrent_positions=1)
        services.set_follower_enabled(self.f, True, actor=self.staff)
        CopierSymbolMap.objects.create(
            master_broker="Tradersway-Live", master_symbol="XAUUSD",
            follower_broker="IS6Technologies-Live", follower_symbol="XAUUSD.std",
            verified=True, verified_by=self.staff, verified_at=timezone.now())
        self._instrument("XAUUSD.std", {"volume_min": 0.01, "volume_step": 0.01, "volume_max": 100})
        self.authz = self._grant()
        self.open_rep = self._matched_open(self.authz)

    # --- builders ------------------------------------------------------------
    def _instrument(self, symbol, metadata, *, enabled=True):
        from execution.models import BrokerInstrument
        return BrokerInstrument.objects.create(
            account=self.follower_acct, broker_symbol=symbol, base_symbol=symbol,
            enabled=enabled, metadata=metadata)

    def _poll(self, observed_at=None):
        observed_at = observed_at or timezone.now()
        return MasterObservationPoll.objects.create(
            master=self.master, observed_at=observed_at, read_at=observed_at, position_count=1)

    def _matched_open(self, authz):
        """The reconciled OPEN whose follower position (FOLLOWER_TICKET) the lifecycle ops act on. Its LIVE job
        records the GOVERNING authorization id — ADR-0083 lifecycle ops bind to the OPEN's grant, not the newest."""
        ev = MasterEvent.objects.create(
            master=self.master, sequence=10, from_poll=self._poll(),
            event_type=MasterEvent.EventType.OPEN, position_ticket=MASTER_TICKET, symbol="XAUUSD",
            side="SELL", volume_before=Decimal("0"), volume_after=Decimal("0.02"),
            volume_delta=Decimal("0.02"), observed_at=timezone.now())
        job = ExecutionJob.objects.create(
            account=self.follower_acct, job_type=ExecutionJob.JobType.PLACE_ORDER,
            payload={"copier_live_authorization_id": authz.id, "execution_mode": "LIVE", "ticket": FOLLOWER_TICKET},
            hosted_idempotency_key="copier-live:fixture-open")
        return CopierReplication.objects.create(
            master_event=ev, follower=self.f, requested_op="OPEN", execution_job=job,
            status=ReplicationStatus.EXECUTION_VERIFIED, reconciliation_status=MATCHED,
            follower_ticket=FOLLOWER_TICKET, follower_symbol="XAUUSD.std", target_volume=Decimal("0.02"),
            constraints_authoritative=True, sltp_translation_ok=True)

    def _event(self, event_type, *, sequence, sl=None, tp=None, prev_sl=None, prev_tp=None,
               vol_before=None, vol_after=None):
        ev = MasterEvent.objects.create(
            master=self.master, sequence=sequence, from_poll=self._poll(),
            event_type=event_type, position_ticket=MASTER_TICKET, symbol="XAUUSD", side="SELL",
            volume_before=Decimal(str(vol_before)) if vol_before is not None else Decimal("0.02"),
            volume_after=Decimal(str(vol_after)) if vol_after is not None else Decimal("0.02"),
            volume_delta=Decimal("0"), observed_at=timezone.now(),
            new_sl=Decimal(str(sl)) if sl is not None else None,
            new_tp=Decimal(str(tp)) if tp is not None else None,
            prev_sl=Decimal(str(prev_sl)) if prev_sl is not None else None,
            prev_tp=Decimal(str(prev_tp)) if prev_tp is not None else None)
        return ev

    def _modify_rep(self, *, sequence=20, sl="3990", tp="3970", translated=True):
        ev = self._event(MasterEvent.EventType.TP_CHANGE, sequence=sequence, tp=tp, prev_tp="3980")
        return CopierReplication.objects.create(
            master_event=ev, follower=self.f, requested_op="MODIFY_SLTP",
            status=ReplicationStatus.SHADOW_RECORDED, follower_symbol="XAUUSD.std",
            translated_sl=Decimal(sl) if sl else None, translated_tp=Decimal(tp) if tp else None,
            constraints_authoritative=True, sltp_translation_ok=translated)

    def _close_rep(self, *, sequence=30):
        ev = self._event(MasterEvent.EventType.FULL_CLOSE, sequence=sequence, vol_before="0.02", vol_after="0")
        return CopierReplication.objects.create(
            master_event=ev, follower=self.f, requested_op="CLOSE",
            status=ReplicationStatus.SHADOW_RECORDED, follower_symbol="XAUUSD.std",
            constraints_authoritative=True, sltp_translation_ok=True)

    def _partial_rep(self, *, sequence=40, vol_before="0.04", vol_after="0.02"):
        ev = self._event(MasterEvent.EventType.PARTIAL_CLOSE, sequence=sequence,
                         vol_before=vol_before, vol_after=vol_after)
        return CopierReplication.objects.create(
            master_event=ev, follower=self.f, requested_op="PARTIAL_CLOSE",
            status=ReplicationStatus.SHADOW_RECORDED, follower_symbol="XAUUSD.std",
            constraints_authoritative=True, sltp_translation_ok=True)

    def _grant(self, **over):
        now = timezone.now()
        d = dict(
            user=self.owner, master_account=self.master_acct, follower_account=self.follower_acct,
            master_login="55425442", master_server="Tradersway-Live", master_environment="LIVE",
            follower_login="38313831", follower_server="IS6Technologies-Live", follower_environment="LIVE",
            source_symbol="XAUUSD", follower_symbol="XAUUSD.std",
            max_volume=Decimal("0.10"), max_concurrent_positions=1, max_orders=1, orders_consumed=1,
            valid_from=now - timedelta(minutes=1), expires_at=now + timedelta(hours=1),
            watermark_at=now - timedelta(hours=1), watermark_event_sequence=5,
            status=CopierLiveAuthorizationStatus.CONSUMED, approved_by=self.staff, reason="test",
            correlation_id="corr-test-1",
            modify_authority=True, partial_close_authority=True, full_close_authority=True,
            close_only_after_expiry=False)
        d.update(over)
        return CopierLiveAuthorization.objects.create(**d)

    def assertNoNewJobs(self):
        # Only the fixture's OPEN (entry) job may exist — the lifecycle op created nothing.
        self.assertEqual(ExecutionJob.objects.exclude(id=self.open_rep.execution_job_id).count(), 0,
                         "no new ExecutionJob may be created")

    def assertNoPlaceOrder(self):
        # A lifecycle op must NEVER create a PLACE_ORDER; the ONLY PLACE_ORDER is the fixture's entry OPEN.
        self.assertEqual(
            ExecutionJob.objects.filter(
                job_type__in=(ExecutionJob.JobType.PLACE_ORDER, ExecutionJob.JobType.PLACE_ORDER_SHADOW))
            .exclude(id=self.open_rep.execution_job_id).count(), 0,
            "a lifecycle op must NEVER create a PLACE_ORDER (no open/increase exposure)")


class ModifyAdmissionTests(LiveLifecycleFixture):
    def test_happy_modify_admits(self):
        rep = self._modify_rep()
        self.assertEqual(admit_modify_live(rep), (True, FOLLOWER_TICKET, "ok"))
        # And the demo-producer entrypoint delegates here for a live follower.
        self.assertEqual(admit_modify(rep), (True, FOLLOWER_TICKET, "ok"))

    @override_settings(**{**LIVE, "COPIER_LIVE_MODIFY_ENABLED": "0"})
    def test_modify_arm_off(self):
        ok, _t, reason = admit_modify_live(self._modify_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "live_modify_arm_disabled")

    @override_settings(**{**LIVE, "COPIER_LIVE_EXECUTE_ENABLED": "0"})
    def test_modify_live_stack_off(self):
        ok, _t, reason = admit_modify_live(self._modify_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "live_copier_arm_disabled")

    def test_modify_requires_modify_authority(self):
        self.authz.modify_authority = False
        self.authz.save(update_fields=["modify_authority"])
        ok, _t, reason = admit_modify_live(self._modify_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "no_modify_authority")

    def test_modify_denied_after_expiry(self):
        self.authz.expires_at = timezone.now() - timedelta(seconds=1)
        self.authz.save(update_fields=["expires_at"])
        ok, _t, reason = admit_modify_live(self._modify_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "authorization_expired")

    def test_modify_denied_under_kill_switch(self):
        CopierLiveKillSwitch.objects.create(engaged=True, engaged_by=self.staff)
        ok, _t, reason = admit_modify_live(self._modify_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "live_copier_kill_switch_engaged")

    def test_modify_denied_when_revoked(self):
        self.authz.status = CopierLiveAuthorizationStatus.REVOKED
        self.authz.revoked_at = timezone.now()
        self.authz.save(update_fields=["status", "revoked_at"])
        ok, _t, reason = admit_modify_live(self._modify_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "governing_authorization_revoked_or_absent")

    def test_modify_requires_complete_sltp(self):
        rep = self._modify_rep(translated=False)
        ok, _t, reason = admit_modify_live(rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "sltp_incomplete")

    def test_modify_sl_removal_fails_closed(self):
        ev = self._event(MasterEvent.EventType.SL_REMOVE, sequence=21, prev_sl="4000")
        rep = CopierReplication.objects.create(
            master_event=ev, follower=self.f, requested_op="MODIFY_SLTP",
            status=ReplicationStatus.SHADOW_RECORDED, follower_symbol="XAUUSD.std",
            translated_sl=None, translated_tp=Decimal("3970"), constraints_authoritative=True,
            sltp_translation_ok=True)
        ok, _t, reason = admit_modify_live(rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "sl_removal_not_auto_replicated")

    def test_modify_requires_linked_ticket(self):
        self.open_rep.reconciliation_status = "UNMATCHED"
        self.open_rep.save(update_fields=["reconciliation_status"])
        ok, _t, reason = admit_modify_live(self._modify_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "no_verified_follower_ticket")


class FullCloseAdmissionTests(LiveLifecycleFixture):
    def test_happy_full_close_admits(self):
        rep = self._close_rep()
        self.assertEqual(admit_full_close_live(rep), (True, FOLLOWER_TICKET, "ok"))
        self.assertEqual(admit_close(rep), (True, FOLLOWER_TICKET, "ok"))

    @override_settings(**{**LIVE, "COPIER_LIVE_FULL_CLOSE_ENABLED": "0"})
    def test_full_close_arm_off(self):
        ok, _t, reason = admit_full_close_live(self._close_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "live_full_close_arm_disabled")

    def test_full_close_requires_authority(self):
        self.authz.full_close_authority = False
        self.authz.save(update_fields=["full_close_authority"])
        ok, _t, reason = admit_full_close_live(self._close_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "no_full_close_authority")

    def test_full_close_requires_linked_ticket(self):
        self.open_rep.follower_ticket = None
        self.open_rep.save(update_fields=["follower_ticket"])
        ok, _t, reason = admit_full_close_live(self._close_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "no_verified_follower_ticket")

    # --- close-only containment AFTER EXPIRY ---------------------------------
    def test_full_close_after_expiry_denied_without_policy(self):
        self.authz.expires_at = timezone.now() - timedelta(seconds=1)
        self.authz.save(update_fields=["expires_at"])
        ok, _t, reason = admit_full_close_live(self._close_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "entry_window_expired_no_close_only")

    @override_settings(**{**LIVE, "COPIER_LIVE_CLOSE_ONLY_AFTER_EXPIRY_ENABLED": "1"})
    def test_full_close_after_expiry_allowed_with_policy(self):
        self.authz.expires_at = timezone.now() - timedelta(seconds=1)
        self.authz.close_only_after_expiry = True
        self.authz.save(update_fields=["expires_at", "close_only_after_expiry"])
        self.assertEqual(admit_full_close_live(self._close_rep()), (True, FOLLOWER_TICKET, "ok"))

    @override_settings(**{**LIVE, "COPIER_LIVE_CLOSE_ONLY_AFTER_EXPIRY_ENABLED": "1"})
    def test_full_close_after_expiry_needs_authz_policy_too(self):
        # Global arm on but the authorization's own policy flag is False -> still denied (both required).
        self.authz.expires_at = timezone.now() - timedelta(seconds=1)
        self.authz.close_only_after_expiry = False
        self.authz.save(update_fields=["expires_at", "close_only_after_expiry"])
        ok, _t, reason = admit_full_close_live(self._close_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "entry_window_expired_no_close_only")

    # --- close-only containment UNDER KILL SWITCH ----------------------------
    def test_full_close_under_kill_switch_denied_without_policy(self):
        CopierLiveKillSwitch.objects.create(engaged=True, engaged_by=self.staff)
        ok, _t, reason = admit_full_close_live(self._close_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "kill_switch_no_close_containment")

    @override_settings(**{**LIVE, "COPIER_LIVE_CONTAIN_ON_KILL_SWITCH_ENABLED": "1"})
    def test_full_close_under_kill_switch_allowed_with_policy(self):
        CopierLiveKillSwitch.objects.create(engaged=True, engaged_by=self.staff)
        self.assertEqual(admit_full_close_live(self._close_rep()), (True, FOLLOWER_TICKET, "ok"))


class PartialCloseAdmissionTests(LiveLifecycleFixture):
    def test_happy_partial_admits_reduces_only(self):
        rep = self._partial_rep(vol_before="0.04", vol_after="0.02")  # master halved
        ok, ticket, reduction, remaining, reason = admit_partial_close_live(
            rep, fetch=_fetch_follower(FOLLOWER_TICKET, 0.02))
        self.assertTrue(ok, reason)
        self.assertEqual(ticket, FOLLOWER_TICKET)
        # 50% of 0.02 current = 0.01 reduced, 0.01 remaining (strict reduction, never a full close).
        self.assertAlmostEqual(reduction, 0.01, places=8)
        self.assertAlmostEqual(remaining, 0.01, places=8)
        self.assertTrue(0 < reduction < 0.02)

    @override_settings(**{**LIVE, "COPIER_LIVE_PARTIAL_CLOSE_ENABLED": "0"})
    def test_partial_arm_off(self):
        ok, *_rest, reason = admit_partial_close_live(self._partial_rep(), fetch=_fetch_follower(FOLLOWER_TICKET, 0.02))
        self.assertFalse(ok)
        self.assertEqual(reason, "live_partial_close_arm_disabled")

    def test_partial_requires_authority(self):
        self.authz.partial_close_authority = False
        self.authz.save(update_fields=["partial_close_authority"])
        ok, *_rest, reason = admit_partial_close_live(self._partial_rep(), fetch=_fetch_follower(FOLLOWER_TICKET, 0.02))
        self.assertFalse(ok)
        self.assertEqual(reason, "no_partial_close_authority")

    def test_partial_follower_ticket_not_open(self):
        ok, *_rest, reason = admit_partial_close_live(self._partial_rep(), fetch=_fetch_empty())
        self.assertFalse(ok)
        self.assertEqual(reason, "follower_ticket_not_open")

    def test_partial_would_be_full_close_fails_closed(self):
        # master fully halves but follower only holds the minimum lot -> reduction would equal/exceed current.
        rep = self._partial_rep(vol_before="0.04", vol_after="0.02")
        ok, *_rest, reason = admit_partial_close_live(rep, fetch=_fetch_follower(FOLLOWER_TICKET, 0.01))
        self.assertFalse(ok)
        self.assertIn(reason, ("below_min_reduction", "would_be_full_close", "remainder_below_min", "rounds_to_zero"))

    def test_partial_master_ratio_invalid(self):
        rep = self._partial_rep(vol_before="0.02", vol_after="0.02")  # no reduction
        ok, *_rest, reason = admit_partial_close_live(rep, fetch=_fetch_follower(FOLLOWER_TICKET, 0.02))
        self.assertFalse(ok)
        self.assertEqual(reason, "master_ratio_invalid")

    def test_partial_after_expiry_denied_without_policy(self):
        self.authz.expires_at = timezone.now() - timedelta(seconds=1)
        self.authz.save(update_fields=["expires_at"])
        ok, *_rest, reason = admit_partial_close_live(self._partial_rep(), fetch=_fetch_follower(FOLLOWER_TICKET, 0.02))
        self.assertFalse(ok)
        self.assertEqual(reason, "entry_window_expired_no_close_only")


class SecurityIdentityTests(LiveLifecycleFixture):
    def test_wrong_tenant_user(self):
        other = User.objects.create_user("o2", "o2@guvfx.invalid", "x")
        self.authz.user = other
        self.authz.save(update_fields=["user"])
        ok, _t, reason = admit_full_close_live(self._close_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "user_mismatch")

    def test_master_identity_drift(self):
        self.authz.master_login = "99999999"
        self.authz.save(update_fields=["master_login"])
        ok, _t, reason = admit_full_close_live(self._close_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "master_identity_drift")

    def test_source_symbol_mismatch(self):
        self.authz.source_symbol = "EURUSD"
        self.authz.save(update_fields=["source_symbol"])
        ok, _t, reason = admit_full_close_live(self._close_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "source_symbol_mismatch")

    def test_no_authorization_for_pair(self):
        self.authz.delete()
        ok, _t, reason = admit_full_close_live(self._close_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "governing_authorization_revoked_or_absent")

    def test_lifecycle_authz_finds_consumed_not_revoked(self):
        # The governing authz for a bound position is ACTIVE-or-CONSUMED; a REVOKED one governs nothing.
        self.assertIsNotNone(lifecycle_authorization_for(self.master_acct.id, self.follower_acct.id))
        self.authz.status = CopierLiveAuthorizationStatus.REVOKED
        self.authz.revoked_at = timezone.now()
        self.authz.save(update_fields=["status", "revoked_at"])
        self.assertIsNone(lifecycle_authorization_for(self.master_acct.id, self.follower_acct.id))


class JobCreationTests(LiveLifecycleFixture):
    """The produce_* entrypoints route a live follower to a truthful LIVE command and never a PLACE_ORDER."""

    def test_modify_produces_live_modify_position(self):
        rep = self._modify_rep()
        out = produce_modify_one(rep)
        self.assertTrue(out["produced"], out)
        job = ExecutionJob.objects.get(id=out["job_id"])
        self.assertEqual(job.job_type, ExecutionJob.JobType.MODIFY_POSITION)
        self.assertEqual(job.payload["execution_mode"], "LIVE")
        self.assertFalse(job.payload["is_demo"])
        self.assertEqual(job.payload["copier_operation"], "MODIFY_PROTECTION")
        self.assertEqual(job.payload["ticket"], FOLLOWER_TICKET)
        self.assertEqual(job.payload["correlation_id"], "corr-test-1")
        self.assertTrue(job.payload["require_identity_pin"])
        self.assertIn("expected_pre_state", job.payload)
        # A MODIFY carries no volume/side -> cannot increase exposure.
        self.assertNotIn("lots", job.payload)
        self.assertNotIn("volume", job.payload)
        self.assertNoPlaceOrder()

    def test_full_close_produces_live_close_trade(self):
        out = produce_close_one(self._close_rep())
        self.assertTrue(out["produced"], out)
        job = ExecutionJob.objects.get(id=out["job_id"])
        self.assertEqual(job.job_type, ExecutionJob.JobType.CLOSE_TRADE)
        self.assertEqual(job.payload["execution_mode"], "LIVE")
        self.assertEqual(job.payload["copier_operation"], "FULL_CLOSE")
        self.assertEqual(job.payload["ticket"], FOLLOWER_TICKET)
        self.assertNoPlaceOrder()

    def test_partial_produces_live_partial_close_trade(self):
        rep = self._partial_rep(vol_before="0.04", vol_after="0.02")
        out = produce_partial_close_one(rep, fetch=_fetch_follower(FOLLOWER_TICKET, 0.02))
        self.assertTrue(out["produced"], out)
        job = ExecutionJob.objects.get(id=out["job_id"])
        self.assertEqual(job.job_type, ExecutionJob.JobType.PARTIAL_CLOSE_TRADE)
        self.assertEqual(job.payload["copier_operation"], "PARTIAL_CLOSE")
        self.assertAlmostEqual(float(job.payload["volume"]), 0.01, places=8)
        self.assertLess(float(job.payload["volume"]), 0.02)   # strict reduction
        self.assertNoPlaceOrder()

    def test_lifecycle_ops_never_consume_entry_allowance(self):
        # The authorization's entry allowance is already consumed (orders_consumed=1); lifecycle ops must not
        # touch it (they are not entries).
        before = self.authz.orders_consumed
        produce_modify_one(self._modify_rep())
        produce_close_one(self._close_rep())
        self.authz.refresh_from_db()
        self.assertEqual(self.authz.orders_consumed, before)

    def test_modify_idempotent_no_duplicate(self):
        produce_modify_one(self._modify_rep())
        n = ExecutionJob.objects.filter(job_type=ExecutionJob.JobType.MODIFY_POSITION).count()
        # A second modify intent for the same bound ticket while one is in flight does not duplicate.
        ok, _t, reason = admit_modify_live(self._modify_rep(sequence=22))
        self.assertFalse(ok)
        self.assertEqual(reason, "modify_already_in_flight")
        self.assertEqual(ExecutionJob.objects.filter(job_type=ExecutionJob.JobType.MODIFY_POSITION).count(), n)

    def test_mid_flight_revoke_fails_closed(self):
        rep = self._close_rep()
        self.assertEqual(admit_full_close_live(rep), (True, FOLLOWER_TICKET, "ok"))
        # Revoke committed before the under-lock job creation -> create_live_* must fail closed.
        self.authz.status = CopierLiveAuthorizationStatus.REVOKED
        self.authz.revoked_at = timezone.now()
        self.authz.save(update_fields=["status", "revoked_at"])
        out = create_live_full_close_job(rep, FOLLOWER_TICKET)
        self.assertFalse(out["produced"])
        self.assertEqual(out["reason"], "governing_authorization_revoked_or_absent")
        self.assertNoNewJobs()


class DemoPathUnchangedTests(TestCase):
    """A DEMO follower still uses the demo close/modify/partial path (the LIVE delegation did not weaken it)."""

    @override_settings(COPIER_ENABLED="1", COPIER_EXECUTE_ENABLED="1")
    def test_demo_follower_modify_not_live(self):
        owner = User.objects.create_user("d", "d@guvfx.invalid", "x")
        staff = User.objects.create_user("ds", "ds@guvfx.invalid", "x", is_staff=True)
        bs, _ = BrokerServer.objects.get_or_create(
            server_name="Demo-Server", defaults={"broker_display_name": "Demo", "environment": "demo"})
        macct = TradingAccount.objects.create(user=owner, name="m", broker_name="Demo", account_number="1",
                                              is_demo=True, is_active=True, broker_server=bs)
        facct = TradingAccount.objects.create(user=owner, name="f", broker_name="Demo", account_number="2",
                                              is_demo=True, is_active=True, broker_server=bs)
        master = CopierMaster.objects.create(trading_account=macct, observe_enabled=True)
        f = services.add_follower(master, facct, sizing_mode=SizingMode.FIXED_LOT, fixed_lot=Decimal("0.01"))
        services.set_follower_enabled(f, True, actor=staff)
        ev = MasterEvent.objects.create(
            master=master, sequence=1, from_poll=MasterObservationPoll.objects.create(
                master=master, observed_at=timezone.now(), read_at=timezone.now(), position_count=1),
            event_type=MasterEvent.EventType.TP_CHANGE, position_ticket=1, symbol="XAUUSD", side="SELL",
            volume_before=Decimal("0.01"), volume_after=Decimal("0.01"), volume_delta=Decimal("0"),
            observed_at=timezone.now(), new_tp=Decimal("3970"), prev_tp=Decimal("3980"))
        rep = CopierReplication.objects.create(
            master_event=ev, follower=f, requested_op="MODIFY_SLTP", status=ReplicationStatus.SHADOW_RECORDED,
            follower_symbol="XAUUSD", translated_sl=Decimal("4000"), translated_tp=Decimal("3970"),
            constraints_authoritative=True, sltp_translation_ok=True)
        # No matched OPEN -> demo path denies with its own reason (NOT a live-arm reason) and creates no job.
        ok, _t, reason = admit_modify(rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_verified_follower_ticket")
        self.assertEqual(ExecutionJob.objects.count(), 0)


class GrantCommandTests(LiveLifecycleFixture):
    """The human-gated grant ceremony sets the new lifecycle authority, defaulting every grant OFF."""

    def _grant_via_command(self, **extra):
        from django.core.management import call_command
        from io import StringIO
        self.authz.delete()   # remove the fixture's CONSUMED authz so the ceremony writes a fresh ACTIVE one
        call_command(
            "grant_copier_live_authorization", user_email="owner@guvfx.invalid",
            master_account_id=self.master_acct.id, follower_account_id=self.follower_acct.id,
            source_symbol="XAUUSD", follower_symbol="XAUUSD.std", max_volume="0.01",
            approved_by="staff@guvfx.invalid", reason="t", stdout=StringIO(), **extra)
        return (CopierLiveAuthorization.objects
                .filter(master_account=self.master_acct, follower_account=self.follower_acct,
                        status=CopierLiveAuthorizationStatus.ACTIVE).latest("created_at"))

    def test_grant_sets_requested_authority_only(self):
        authz = self._grant_via_command(modify_authority=True, full_close_authority=True)
        self.assertTrue(authz.modify_authority)
        self.assertTrue(authz.full_close_authority)
        self.assertFalse(authz.partial_close_authority)       # not requested -> OFF
        self.assertFalse(authz.close_only_after_expiry)
        self.assertTrue(authz.correlation_id)                 # auto-generated

    def test_grant_defaults_all_authority_off(self):
        authz = self._grant_via_command()
        self.assertFalse(authz.modify_authority)
        self.assertFalse(authz.partial_close_authority)
        self.assertFalse(authz.full_close_authority)
        self.assertFalse(authz.close_only_after_expiry)


class AdversarialFixTests(LiveLifecycleFixture):
    """Regression tests for the confirmed adversarial findings (ADR-0083 review)."""

    def test_regrant_does_not_widen_authority(self):
        # F1 (HIGH): the position was opened under self.authz (modify WITHHELD). A LATER re-grant for the same
        # pair with modify authority must NOT retroactively authorize a MODIFY of the already-open position —
        # the lifecycle op binds to the OPEN's recorded grant, not the newest grant.
        self.authz.modify_authority = False
        self.authz.save(update_fields=["modify_authority"])
        newer = self._grant(status=CopierLiveAuthorizationStatus.ACTIVE, orders_consumed=0,
                            modify_authority=True, correlation_id="corr-regrant")
        self.assertGreater(newer.id, self.authz.id)   # a strictly newer ACTIVE grant exists for the pair
        ok, _t, reason = admit_modify_live(self._modify_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "no_modify_authority")

    def test_modify_superseded_by_later_close(self):
        # F2 (MEDIUM): a later FULL_CLOSE for the position makes a pending MODIFY stale (never applied).
        self._event(MasterEvent.EventType.FULL_CLOSE, sequence=99, vol_before="0.02", vol_after="0")
        ok, _t, reason = admit_modify_live(self._modify_rep(sequence=20))
        self.assertFalse(ok)
        self.assertEqual(reason, "superseded_by_later_close")

    def test_lifecycle_event_must_be_after_open(self):
        # F2 (MEDIUM): a lifecycle event at/below the OPEN's sequence (10) is stale/out-of-order.
        ok, _t, reason = admit_modify_live(self._modify_rep(sequence=5))
        self.assertFalse(ok)
        self.assertEqual(reason, "event_not_after_open")

    def test_blank_follower_symbol_denied(self):
        # F3 (LOW): a blank intent follower_symbol must deny (no silent master-symbol fallback).
        rep = self._modify_rep()
        rep.follower_symbol = ""
        rep.save(update_fields=["follower_symbol"])
        ok, _t, reason = admit_modify_live(rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "intent_follower_symbol_mismatch")

    def test_full_close_blocked_while_partial_in_flight(self):
        # F4 (LOW): cross-op idempotency — a full close must not fire while a partial close is in flight.
        out = produce_partial_close_one(self._partial_rep(vol_before="0.04", vol_after="0.02"),
                                        fetch=_fetch_follower(FOLLOWER_TICKET, 0.02))
        self.assertTrue(out["produced"], out)
        ok, _t, reason = admit_full_close_live(self._close_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "close_already_in_flight")


class RuntimeBoundaryTests(LiveLifecycleFixture):
    """Positive + negative controls for copier_live_lifecycle_authorized (the MT5_ALLOW_LIVE lifecycle boundary)."""

    def _authorized(self, account=None):
        from execution.live_authz import copier_live_lifecycle_authorized
        return copier_live_lifecycle_authorized(account or self.follower_acct)

    @override_settings(**{**LIVE, "COPIER_LIVE_MODIFY_ENABLED": "0", "COPIER_LIVE_PARTIAL_CLOSE_ENABLED": "0",
                          "COPIER_LIVE_FULL_CLOSE_ENABLED": "0"})
    def test_dark_when_no_op_armed(self):
        # The most-important DARK claim: with every per-op arm OFF the boundary is unchanged (False) even though
        # the LIVE stack + a valid CONSUMED authorization with full authority are present.
        self.assertFalse(self._authorized())

    def test_authorized_when_op_armed(self):
        # LIVE (class decorator) arms all three ops + the fixture authz carries full authority within window.
        self.assertTrue(self._authorized())

    def test_denied_for_wrong_account(self):
        other = _live_acct(self.owner, "f2", "99999999", server="IS6Technologies-Live", display="IS6")
        self.assertFalse(self._authorized(other))

    def test_denied_when_revoked(self):
        self.authz.status = CopierLiveAuthorizationStatus.REVOKED
        self.authz.revoked_at = timezone.now()
        self.authz.save(update_fields=["status", "revoked_at"])
        self.assertFalse(self._authorized())

    def test_denied_under_kill_switch_without_contain(self):
        CopierLiveKillSwitch.objects.create(engaged=True, engaged_by=self.staff)
        self.assertFalse(self._authorized())

    @override_settings(**{**LIVE, "COPIER_LIVE_CONTAIN_ON_KILL_SWITCH_ENABLED": "1"})
    def test_authorized_under_kill_switch_with_contain(self):
        # Opt-in close-only containment permits the runtime to stay LIVE to CLOSE a bound position.
        CopierLiveKillSwitch.objects.create(engaged=True, engaged_by=self.staff)
        self.assertTrue(self._authorized())
