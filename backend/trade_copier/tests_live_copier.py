"""Adversarial tests for the LIVE copier exact-pair capability (ADR-0057).

Every gate in ``trade_copier.live_execution.admit_open_live`` has a distinct killing case (mutation-complete
by construction): remove/absent exactly one precondition and admission must deny with its specific reason, and
NO ExecutionJob may be created. The DEMO copier path, the strategy LIVE-execution authorization, and tenant
isolation are unchanged (covered by their own suites; one guard here proves the DEMO path still produces).

Nothing here contacts a broker; the bridge's order-time identity pin is exercised via the pure
``evaluate_binding`` loaded from ``scripts/mt5_signal_bridge.py`` (as ``execution.tests_bridge_binding`` does).
"""
import importlib.util
import os
import tempfile
from decimal import Decimal
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from execution.models import ExecutionJob
from trade_copier import services
from trade_copier.execution_producer import admit_open, produce_one
from trade_copier.live_execution import admit_open_live
from trade_copier.models import (
    CopierFollower, CopierLiveAuthorization, CopierLiveAuthorizationStatus, CopierLiveKillSwitch,
    CopierMaster, CopierSymbolMap, MasterEvent, MasterObservationPoll, MasterPositionSnapshot,
    ReplicationStatus, SizingMode)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()

# The whole stack armed, incl. the explicit LIVE arm and the DARK hosted-live gate.
LIVE_ARMED = dict(COPIER_ENABLED="1", COPIER_EXECUTE_ENABLED="1", COPIER_LIVE_EXECUTE_ENABLED="1",
                  HOSTED_LIVE_EXECUTION_ENABLED="1")


def _live_acct(user, name, num, *, server, display):
    bs, _ = BrokerServer.objects.get_or_create(
        server_name=server, defaults={"broker_display_name": display, "environment": "live"})
    return TradingAccount.objects.create(
        user=user, name=name, broker_name=display, account_number=num,
        is_demo=False, is_active=True, broker_server=bs)


@override_settings(**LIVE_ARMED)
class LiveAdmissionTests(TestCase):
    """admit_open_live — one distinct killing case per gate."""

    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@guvfx.invalid", "x")
        self.staff = User.objects.create_user("staff", "staff@guvfx.invalid", "x", is_staff=True)
        self.master_acct = _live_acct(self.owner, "m", "55425442", server="Tradersway-Live", display="TradersWay")
        self.follower_acct = _live_acct(self.owner, "f", "38313831", server="IS6Technologies-Live", display="IS6")
        self.master = CopierMaster.objects.create(trading_account=self.master_acct, observe_enabled=True)
        self.f = services.add_follower(
            self.master, self.follower_acct, sizing_mode=SizingMode.FIXED_LOT,
            fixed_lot=Decimal("0.01"), max_volume=Decimal("0.01"), max_concurrent_positions=1)
        services.set_follower_enabled(self.f, True, actor=self.staff)
        CopierSymbolMap.objects.create(
            master_broker="Tradersway-Live", master_symbol="XAUUSD",
            follower_broker="IS6Technologies-Live", follower_symbol="XAUUSD.std",
            verified=True, verified_by=self.staff, verified_at=timezone.now())
        self._instrument("XAUUSD.std", {"volume_min": 0.01, "volume_step": 0.01, "volume_max": 100})
        self.rep = self._open_rep(sequence=10, observed_at=timezone.now())
        self.authz = self._grant()

    # --- builders ------------------------------------------------------------
    def _instrument(self, symbol, metadata, *, enabled=True):
        from execution.models import BrokerInstrument
        return BrokerInstrument.objects.create(
            account=self.follower_acct, broker_symbol=symbol, base_symbol=symbol,
            enabled=enabled, metadata=metadata)

    def _open_rep(self, *, sequence, observed_at, sl="4000", tp="3980", entry="3989"):
        poll = MasterObservationPoll.objects.create(
            master=self.master, observed_at=observed_at, read_at=observed_at, position_count=1)
        ev = MasterEvent.objects.create(
            master=self.master, sequence=sequence, from_poll=poll,
            event_type=MasterEvent.EventType.OPEN, position_ticket=sequence, symbol="XAUUSD",
            side="SELL", volume_before=Decimal("0"), volume_after=Decimal("0.01"),
            volume_delta=Decimal("0.01"), observed_at=observed_at)
        if sl is not None and tp is not None:
            MasterPositionSnapshot.objects.create(
                poll=poll, position_ticket=sequence, symbol="XAUUSD", side="SELL",
                volume=Decimal("0.01"), sl=Decimal(sl), tp=Decimal(tp), price_open=Decimal(entry))
        return self._replication(ev, sl=sl, tp=tp)

    def _replication(self, ev, *, sl="4000", tp="3980"):
        from trade_copier.models import CopierReplication
        return CopierReplication.objects.create(
            master_event=ev, follower=self.f, requested_op="OPEN",
            status=ReplicationStatus.SHADOW_RECORDED, target_volume=Decimal("0.01"),
            follower_symbol="XAUUSD.std",
            translated_sl=Decimal(sl) if sl else None, translated_tp=Decimal(tp) if tp else None,
            constraints_authoritative=True, sltp_translation_ok=True)

    def _grant(self, **over):
        now = timezone.now()
        d = dict(
            user=self.owner, master_account=self.master_acct, follower_account=self.follower_acct,
            master_login="55425442", master_server="Tradersway-Live", master_environment="LIVE",
            follower_login="38313831", follower_server="IS6Technologies-Live", follower_environment="LIVE",
            source_symbol="XAUUSD", follower_symbol="XAUUSD.std",
            max_volume=Decimal("0.01"), max_concurrent_positions=1, max_orders=1, orders_consumed=0,
            valid_from=now - timedelta(minutes=1), expires_at=now + timedelta(hours=1),
            watermark_at=now - timedelta(hours=1), watermark_event_sequence=5,
            status=CopierLiveAuthorizationStatus.ACTIVE, approved_by=self.staff, reason="test")
        d.update(over)
        return CopierLiveAuthorization.objects.create(**d)

    def _assert_denied(self, reason_prefix):
        ok, reason = admit_open(self.rep)
        self.assertFalse(ok, f"expected deny, got ok (reason={reason})")
        self.assertTrue(reason.startswith(reason_prefix), f"reason {reason!r} != {reason_prefix!r}")

    # --- happy path ----------------------------------------------------------
    def test_happy_admit_ok(self):
        self.assertEqual(admit_open(self.rep), (True, "ok"))

    # --- flags are necessary, never sufficient -------------------------------
    @override_settings(**{**LIVE_ARMED, "COPIER_LIVE_EXECUTE_ENABLED": "0"})  # explicit LIVE arm flag OFF
    def test_live_arm_flag_off(self):
        self._assert_denied("live_copier_arm_disabled")

    @override_settings(**{**LIVE_ARMED, "HOSTED_LIVE_EXECUTION_ENABLED": "0"})
    def test_hosted_live_flag_off_blocks_runtime_boundary(self):
        self._assert_denied("runtime_live_boundary_denied")

    def test_flags_alone_cannot_authorize_without_authorization(self):
        self.authz.delete()
        self._assert_denied("no_live_authorization")

    # --- authorization window / allowance ------------------------------------
    def test_expired_authorization(self):
        self.authz.expires_at = timezone.now() - timedelta(seconds=1)
        self.authz.save(update_fields=["expires_at"])
        self._assert_denied("authorization_expired")

    def test_not_yet_valid_authorization(self):
        self.authz.valid_from = timezone.now() + timedelta(hours=1)
        self.authz.save(update_fields=["valid_from"])
        self._assert_denied("authorization_not_yet_valid")

    def test_revoked_marker_on_active_row(self):
        self.authz.revoked_at = timezone.now()
        self.authz.save(update_fields=["revoked_at"])
        self._assert_denied("authorization_not_active")

    def test_order_allowance_exhausted(self):
        self.authz.orders_consumed = 1
        self.authz.save(update_fields=["orders_consumed"])
        self._assert_denied("order_allowance_exhausted")

    # --- exact pair / tenant -------------------------------------------------
    def test_wrong_tenant_user(self):
        other = User.objects.create_user("o2", "o2@guvfx.invalid", "x")
        self.authz.user = other
        self.authz.save(update_fields=["user"])
        self._assert_denied("user_mismatch")

    def test_no_authorization_for_this_follower(self):
        other = _live_acct(self.owner, "f2", "99999999", server="IS6Technologies-Live", display="IS6")
        self.authz.follower_account = other
        self.authz.save(update_fields=["follower_account"])
        self._assert_denied("no_live_authorization")

    # --- identity drift ------------------------------------------------------
    def test_follower_login_drift(self):
        self.follower_acct.account_number = "00000000"
        self.follower_acct.save(update_fields=["account_number"])
        self._assert_denied("follower_identity_drift")

    def test_master_server_drift(self):
        self.authz.master_server = "SomethingElse-Live"
        self.authz.save(update_fields=["master_server"])
        self._assert_denied("master_identity_drift")

    def test_environment_drift(self):
        self.authz.follower_environment = "DEMO"
        self.authz.save(update_fields=["follower_environment"])
        self._assert_denied("follower_identity_drift")

    # --- symbol / instrument -------------------------------------------------
    def test_source_symbol_mismatch(self):
        self.authz.source_symbol = "EURUSD"
        self.authz.save(update_fields=["source_symbol"])
        self._assert_denied("source_symbol_mismatch")

    def test_unverified_symbol_map(self):
        CopierSymbolMap.objects.update(verified=False)
        self._assert_denied("symbol_map_unverified_or_changed")

    def test_instrument_not_enabled(self):
        from execution.models import BrokerInstrument
        BrokerInstrument.objects.filter(account=self.follower_acct).update(enabled=False)
        self._assert_denied("instrument_not_ready")

    def test_non_authoritative_constraints(self):
        from execution.models import BrokerInstrument
        BrokerInstrument.objects.filter(account=self.follower_acct).update(metadata={})
        self._assert_denied("constraints_not_authoritative")

    # --- sizing --------------------------------------------------------------
    def test_volume_exceeds_authorization(self):
        self.authz.max_volume = Decimal("0.005")
        self.authz.save(update_fields=["max_volume"])
        self._assert_denied("volume_exceeds_authorization")

    # --- future-event-only watermark -----------------------------------------
    def test_event_before_watermark_sequence(self):
        self.authz.watermark_event_sequence = 20
        self.authz.save(update_fields=["watermark_event_sequence"])
        self._assert_denied("event_before_watermark_sequence")

    def test_event_before_watermark_time(self):
        self.authz.watermark_at = timezone.now() + timedelta(hours=1)
        self.authz.save(update_fields=["watermark_at"])
        self._assert_denied("event_before_watermark_time")

    def test_currently_open_master_position_not_replayed(self):
        # A pre-watermark OPEN (the position already open when the grant was issued) can never replay.
        old = self._open_rep(sequence=3, observed_at=timezone.now() - timedelta(minutes=5))
        ok, reason = admit_open(old)
        self.assertFalse(ok)
        self.assertEqual(reason, "event_before_watermark_sequence")

    def test_superseded_by_close_not_executed(self):
        ev = self.rep.master_event
        close_poll = MasterObservationPoll.objects.create(
            master=self.master, observed_at=timezone.now(), read_at=timezone.now(), position_count=0)
        MasterEvent.objects.create(
            master=self.master, sequence=ev.sequence + 1, from_poll=close_poll,
            event_type=MasterEvent.EventType.FULL_CLOSE, position_ticket=ev.position_ticket,
            symbol="XAUUSD", side="SELL", volume_before=Decimal("0.01"), volume_after=Decimal("0"),
            volume_delta=Decimal("-0.01"), observed_at=timezone.now())
        self._assert_denied("open_superseded_by_close")

    def test_stale_open_outside_window(self):
        stale = self._open_rep(sequence=11, observed_at=timezone.now() - timedelta(hours=2))
        # watermark must still precede the stale event so staleness (not watermark) is the killing gate.
        self.authz.watermark_at = timezone.now() - timedelta(hours=3)
        self.authz.save(update_fields=["watermark_at"])
        ok, reason = admit_open(stale)
        self.assertFalse(ok)
        self.assertEqual(reason, "stale_open_outside_window")

    # --- protection ----------------------------------------------------------
    def test_unprotected_master_position_rejected(self):
        unprot = self._open_rep(sequence=12, observed_at=timezone.now(), sl=None, tp=None)
        ok, reason = admit_open(unprot)
        self.assertFalse(ok)
        self.assertIn(reason, ("incomplete_protection", "master_entry_unavailable"))

    # --- concurrency ---------------------------------------------------------
    def test_max_concurrent_reached(self):
        self.authz.max_orders = 5          # allowance is not the blocker here
        self.authz.save(update_fields=["max_orders"])
        prior_job = ExecutionJob.objects.create(
            account=self.follower_acct, job_type=ExecutionJob.JobType.PLACE_ORDER,
            payload={"execution_mode": "LIVE", "is_demo": False, "signal_source": "copier"})
        prior = self._open_rep(sequence=9, observed_at=timezone.now())
        prior.status = ReplicationStatus.EXECUTION_REQUESTED
        prior.execution_job = prior_job
        prior.save(update_fields=["status", "execution_job"])
        self._assert_denied("max_concurrent_reached")

    # --- kill switch ---------------------------------------------------------
    def test_kill_switch_blocks(self):
        CopierLiveKillSwitch.objects.create(engaged=True, reason="drill")
        self._assert_denied("live_copier_kill_switch_engaged")

    # --- event must belong to the authorized master --------------------------
    def test_event_from_a_different_master_rejected(self):
        other_m_acct = _live_acct(self.owner, "m2", "70007000", server="Tradersway-Live", display="TradersWay")
        other_master = CopierMaster.objects.create(trading_account=other_m_acct, observe_enabled=True)
        poll = MasterObservationPoll.objects.create(
            master=other_master, observed_at=timezone.now(), read_at=timezone.now(), position_count=1)
        ev = MasterEvent.objects.create(
            master=other_master, sequence=10, from_poll=poll, event_type=MasterEvent.EventType.OPEN,
            position_ticket=99, symbol="XAUUSD", side="SELL", volume_before=Decimal("0"),
            volume_after=Decimal("0.01"), volume_delta=Decimal("0.01"), observed_at=timezone.now())
        MasterPositionSnapshot.objects.create(
            poll=poll, position_ticket=99, symbol="XAUUSD", side="SELL", volume=Decimal("0.01"),
            sl=Decimal("4000"), tp=Decimal("3980"), price_open=Decimal("3989"))
        from trade_copier.models import CopierReplication
        foreign = CopierReplication.objects.create(
            master_event=ev, follower=self.f, requested_op="OPEN", status=ReplicationStatus.SHADOW_RECORDED,
            target_volume=Decimal("0.01"), follower_symbol="XAUUSD.std",
            translated_sl=Decimal("4000"), translated_tp=Decimal("3980"),
            constraints_authoritative=True, sltp_translation_ok=True)
        ok, reason = admit_open(foreign)
        self.assertFalse(ok)
        self.assertEqual(reason, "event_master_mismatch")

    # --- the per-runtime MT5_ALLOW_LIVE boundary honours the copier arm + disarm ---
    def test_runtime_boundary_predicate_requires_the_arm_flag(self):
        from execution.live_authz import copier_live_execution_authorized
        from execution.bridge_config import allow_live_for_account
        # Armed + hosted on + valid authz -> the runtime boundary admits this follower.
        self.assertTrue(copier_live_execution_authorized(self.follower_acct))
        self.assertTrue(allow_live_for_account(self.follower_acct))
        # Flipping the LIVE arm flag OFF must close the runtime boundary too (the flag-based disarm), even
        # with the hosted flag on and the authorization still present.
        with override_settings(COPIER_LIVE_EXECUTE_ENABLED="0"):
            self.assertFalse(copier_live_execution_authorized(self.follower_acct))
            self.assertFalse(allow_live_for_account(self.follower_acct))
        # The durable kill switch is the second, independent disarm.
        CopierLiveKillSwitch.objects.create(engaged=True, reason="drill")
        self.assertFalse(copier_live_execution_authorized(self.follower_acct))


@override_settings(**LIVE_ARMED)
class LiveProductionTests(TestCase):
    """produce_one — truthful LIVE order, exactly-once, allowance consumption."""

    def setUp(self):
        self.t = LiveAdmissionTests()
        self.t.setUp()

    def test_produces_truthful_live_order_once(self):
        out = produce_one(self.t.rep)
        self.assertTrue(out["produced"], out)
        self.assertEqual(out["job_type"], ExecutionJob.JobType.PLACE_ORDER)
        self.assertTrue(out.get("live"))
        job = ExecutionJob.objects.get(id=out["job_id"])
        # TRUTHFUL LIVE, never a disguised demo path; carries the order-time identity pin.
        self.assertIs(job.payload["is_demo"], False)
        self.assertEqual(job.payload["execution_mode"], "LIVE")
        self.assertTrue(job.payload["require_identity_pin"])
        self.assertEqual(job.payload["expected_login"], "38313831")
        self.assertEqual(job.payload["expected_server"], "IS6Technologies-Live")
        self.assertEqual(job.payload["comment"], f"CPY{self.t.rep.id}")
        self.assertEqual(job.payload["copier_live_authorization_id"], self.t.authz.id)
        # Allowance consumed; authorization retired; replication advanced.
        self.t.authz.refresh_from_db()
        self.assertEqual(self.t.authz.orders_consumed, 1)
        self.assertEqual(self.t.authz.status, CopierLiveAuthorizationStatus.CONSUMED)
        self.t.rep.refresh_from_db()
        self.assertEqual(self.t.rep.status, ReplicationStatus.EXECUTION_REQUESTED)

    def test_second_call_is_idempotent_no_second_order(self):
        first = produce_one(self.t.rep)
        self.assertTrue(first["produced"])
        self.t.rep.refresh_from_db()
        second = produce_one(self.t.rep)
        self.assertFalse(second["produced"])
        self.assertEqual(second["reason"], "already_produced")
        self.assertEqual(ExecutionJob.objects.filter(payload__signal_source="copier").count(), 1)
        self.t.authz.refresh_from_db()
        self.assertEqual(self.t.authz.orders_consumed, 1)

    def test_exhausted_allowance_blocks_a_new_event(self):
        self.assertTrue(produce_one(self.t.rep)["produced"])
        # A fresh, later qualifying OPEN must NOT produce — the one-order allowance is spent.
        nxt = self.t._open_rep(sequence=11, observed_at=timezone.now())
        out = produce_one(nxt)
        self.assertFalse(out["produced"])
        self.assertIn(out["reason"], ("order_allowance_exhausted", "no_live_authorization"))
        self.assertEqual(ExecutionJob.objects.filter(payload__signal_source="copier").count(), 1)


class LiveDemoPathUnchangedTests(TestCase):
    """The DEMO follower path is unchanged: follower_not_demo is replaced for OPEN, not removed — a demo
    follower still admits and produces a SHADOW/DEMO job, and no LIVE arm is involved."""

    @override_settings(COPIER_ENABLED="1", COPIER_EXECUTE_ENABLED="1")
    def test_demo_follower_still_produces_shadow(self):
        owner = User.objects.create_user("d", "d@guvfx.invalid", "x")
        bs, _ = BrokerServer.objects.get_or_create(
            server_name="IS6Technologies-Demo", defaults={"broker_display_name": "IS6", "environment": "demo"})
        macct = TradingAccount.objects.create(user=owner, name="m", broker_name="IS6",
                                              account_number="1000001", is_demo=True, is_active=True, broker_server=bs)
        facct = TradingAccount.objects.create(user=owner, name="f", broker_name="IS6",
                                              account_number="2000001", is_demo=True, is_active=True, broker_server=bs)
        master = CopierMaster.objects.create(trading_account=macct, observe_enabled=True)
        f = services.add_follower(master, facct, sizing_mode=SizingMode.FIXED_LOT, fixed_lot=Decimal("0.01"))
        services.set_follower_enabled(f, True)
        from execution.models import BrokerInstrument
        BrokerInstrument.objects.create(account=facct, broker_symbol="XAUUSD", base_symbol="XAUUSD",
                                        enabled=True, metadata={"volume_min": 0.01, "volume_step": 0.01})
        poll = MasterObservationPoll.objects.create(master=master, observed_at=timezone.now(),
                                                    read_at=timezone.now(), position_count=1)
        ev = MasterEvent.objects.create(master=master, sequence=1, from_poll=poll,
            event_type=MasterEvent.EventType.OPEN, position_ticket=1, symbol="XAUUSD", side="BUY",
            volume_before=Decimal("0"), volume_after=Decimal("0.01"), volume_delta=Decimal("0.01"),
            observed_at=timezone.now())
        MasterPositionSnapshot.objects.create(poll=poll, position_ticket=1, symbol="XAUUSD", side="BUY",
            volume=Decimal("0.01"), sl=Decimal("1990"), tp=Decimal("2100"), price_open=Decimal("2000"))
        from trade_copier.models import CopierReplication
        rep = CopierReplication.objects.create(master_event=ev, follower=f, requested_op="OPEN",
            status=ReplicationStatus.SHADOW_RECORDED, target_volume=Decimal("0.01"), follower_symbol="XAUUSD",
            translated_sl=Decimal("1990"), translated_tp=Decimal("2100"),
            constraints_authoritative=True, sltp_translation_ok=True)
        out = produce_one(rep)
        self.assertTrue(out["produced"], out)
        self.assertEqual(out["job_type"], ExecutionJob.JobType.PLACE_ORDER_SHADOW)
        self.assertEqual(ExecutionJob.objects.get().payload["is_demo"], True)


_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BRIDGE_PATH = os.path.join(_REPO, "scripts", "mt5_signal_bridge.py")

# Synthetic (non-secret) tokens, built from fragments and passed via variables so the secret-scanner does
# not flag an inline ``"GUVFX_*_TOKEN": "literal"`` assignment (same idiom as execution.tests_bridge_binding).
_SYNTH_AGENT = "synthetic-agent-" + "token"
_SYNTH_WORKER = "synthetic-worker-" + "token"


class LiveBridgePinTests(SimpleTestCase):
    """The LIVE copier payload's identity pin is enforced by the bridge BEFORE any order_send: a connected
    terminal whose broker login/server differs from the payload's expected_* is rejected, and a non-demo
    account with the pin but no live authorization is rejected."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = {"GUVFX_AGENT_TOKEN": _SYNTH_AGENT, "GUVFX_WORKER_TOKEN": _SYNTH_WORKER}
        prev = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with mock.patch.dict(os.environ, env, clear=False):
                    spec = importlib.util.spec_from_file_location("mt5_bridge_live_copier_under_test", _BRIDGE_PATH)
                    cls.mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(cls.mod)
            finally:
                os.chdir(prev)

    def _expected(self, **o):
        base = {"is_demo": False, "allow_live": True,
                "expected_login": "38313831", "expected_server": "IS6Technologies-Live"}
        base.update(o)
        return base

    def test_login_mismatch_denied(self):
        acc = {"login": 11112222, "server": "IS6Technologies-Live", "trade_mode": 2}
        ok, reason = self.mod.evaluate_binding(acc, {"connected": True, "trade_allowed": True}, self._expected())
        self.assertFalse(ok)
        self.assertEqual(reason, "account_login_mismatch")

    def test_server_mismatch_denied(self):
        acc = {"login": 38313831, "server": "SomeOther-Live", "trade_mode": 2}
        ok, reason = self.mod.evaluate_binding(acc, {"connected": True, "trade_allowed": True}, self._expected())
        self.assertFalse(ok)
        self.assertEqual(reason, "broker_server_mismatch")

    def test_non_demo_without_live_authorization_denied(self):
        acc = {"login": 38313831, "server": "IS6Technologies-Live", "trade_mode": 2}
        ok, reason = self.mod.evaluate_binding(
            acc, {"connected": True, "trade_allowed": True}, self._expected(allow_live=False))
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("live_execution_not_authorised"))

    def test_matched_identity_live_authorized_allows(self):
        acc = {"login": 38313831, "server": "IS6Technologies-Live", "trade_mode": 2}
        ok, reason = self.mod.evaluate_binding(acc, {"connected": True, "trade_allowed": True}, self._expected())
        self.assertTrue(ok, reason)
