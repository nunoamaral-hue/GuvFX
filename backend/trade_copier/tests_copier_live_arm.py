"""ADR-0097 — one-action, authorization-bound ARM/DISARM: deterministic, fail-closed tests.

Covers the arm runtime evaluator (``trade_copier.arm``), the ``copier_live_arm`` ARM/DISARM command, and the
env-AND-arm gate contract. No broker, Windows host, terminal, production endpoint, or genuine credential — the
``copier_live_demo_preflight`` sub-call is patched to a known verdict so these isolate the arm logic.
"""
from decimal import Decimal
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from trade_copier import services
from trade_copier.arm import engaged_arm_for_authorization, require_engaged_arm, arm_summary
from trade_copier.live_execution import _current_identity
from trade_copier.models import (CopierLiveArm, CopierLiveArmState, CopierLiveAuthorization,
                                 CopierLiveAuthorizationStatus, CopierLiveKillSwitch, CopierMaster,
                                 MasterObservationPoll)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()

_PF_OK = '{"verdict": "READY_TO_ARM", "gates": [], "blockers": []}'


def attach_engaged_arm(authz, *, expires_at=None):
    """Shared fixture used by the LIVE test suites: an engaged, route-exact arm for an ACTIVE authorization.
    Disengages any prior engaged arm for the route first (one engaged arm per route)."""
    CopierLiveArm.objects.filter(
        master_account=authz.master_account, follower_account=authz.follower_account,
        state=CopierLiveArmState.ENGAGED).update(state=CopierLiveArmState.DISENGAGED)
    return CopierLiveArm.objects.create(
        authorization=authz, owner=authz.user, master_account=authz.master_account,
        follower_account=authz.follower_account, source_symbol=authz.source_symbol,
        follower_symbol=authz.follower_symbol, state=CopierLiveArmState.ENGAGED,
        expires_at=expires_at or authz.expires_at)


class _Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("own", "own@x.invalid", "x")
        self.staff = User.objects.create_user("staff", "staff@x.invalid", "x", is_staff=True)
        self.bs = BrokerServer.objects.create(broker_display_name="B", server_name="B-Live", environment="live")
        self.macct = TradingAccount.objects.create(user=self.owner, name="m", broker_name="B",
                                                   account_number="55442", is_demo=False, is_active=True,
                                                   broker_server=self.bs)
        self.facct = TradingAccount.objects.create(user=self.owner, name="f", broker_name="B",
                                                   account_number="51763831", is_demo=False, is_active=True,
                                                   broker_server=self.bs)
        # Derive the authorization identity snapshot FROM the accounts so _identities_match passes by construction.
        self.ml, self.ms, self.me = _current_identity(self.macct)
        self.fl, self.fs, self.fe = _current_identity(self.facct)
        self.master = CopierMaster.objects.create(trading_account=self.macct, observe_enabled=True)
        # Flat master baseline for the arm command's flatness gate.
        MasterObservationPoll.objects.create(master=self.master, observed_at=timezone.now(),
                                             read_at=timezone.now(), position_count=0)

    def _authz(self, **over):
        now = timezone.now()
        d = dict(user=self.owner, master_account=self.macct, follower_account=self.facct,
                 master_login=self.ml, master_server=self.ms, master_environment=self.me,
                 follower_login=self.fl, follower_server=self.fs, follower_environment=self.fe,
                 source_symbol="XAUUSD", follower_symbol="XAUUSD.std",
                 max_volume=Decimal("0.01"), max_concurrent_positions=1, max_orders=1, orders_consumed=0,
                 valid_from=now - timedelta(minutes=1), expires_at=now + timedelta(hours=1),
                 watermark_at=now - timedelta(hours=1), watermark_event_sequence=5,
                 status=CopierLiveAuthorizationStatus.ACTIVE, approved_by=self.staff, reason="t",
                 modify_authority=True, full_close_authority=True)
        d.update(over)
        return CopierLiveAuthorization.objects.create(**d)

    def _arm(self, **extra):
        from io import StringIO
        buf = StringIO()
        with mock.patch("trade_copier.management.commands.copier_live_arm.call_command") as cc:
            cc.return_value = None
            # The command parses preflight JSON from the buffer it passes; emulate a clean backend preflight.
            def _fake(name, *a, **k):
                k.get("stdout").write(_PF_OK)
            cc.side_effect = _fake
            call_command("copier_live_arm", stdout=buf, **extra)
        return buf.getvalue()


class ArmEvaluatorTests(_Base):
    def test_no_arm_is_not_armed(self):
        authz = self._authz()
        self.assertIsNone(engaged_arm_for_authorization(authz))
        ok, reason = require_engaged_arm(authz)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_live_arm")

    def test_engaged_arm_is_armed(self):
        authz = self._authz()
        attach_engaged_arm(authz)
        ok, reason = require_engaged_arm(authz)
        self.assertTrue(ok)
        self.assertEqual(reason, "armed")
        self.assertTrue(arm_summary(engaged_arm_for_authorization(authz))["armed"])

    def test_expired_arm_is_not_armed(self):
        authz = self._authz()
        now = timezone.now()
        attach_engaged_arm(authz, expires_at=now - timedelta(seconds=1))
        self.assertIsNone(engaged_arm_for_authorization(authz, now=now))

    def test_disengaged_arm_is_not_armed(self):
        authz = self._authz()
        arm = attach_engaged_arm(authz)
        arm.state = CopierLiveArmState.DISENGAGED
        arm.save(update_fields=["state"])
        self.assertIsNone(engaged_arm_for_authorization(authz))

    def test_route_symbol_drift_not_armed(self):
        authz = self._authz()
        arm = attach_engaged_arm(authz)
        arm.follower_symbol = "OTHER.std"
        arm.save(update_fields=["follower_symbol"])
        self.assertIsNone(engaged_arm_for_authorization(authz))

    def test_owner_drift_not_armed(self):
        authz = self._authz()
        arm = attach_engaged_arm(authz)
        arm.owner = self.staff
        arm.save(update_fields=["owner"])
        self.assertIsNone(engaged_arm_for_authorization(authz))

    def test_duplicate_engaged_fails_closed(self):
        authz = self._authz()
        attach_engaged_arm(authz)
        # Force a second engaged arm bypassing the helper's disengage (raw create). The partial-unique constraint
        # forbids two engaged for the SAME route; use a different route pair to simulate a corrupted duplicate
        # bound to the same authorization, which the evaluator must fail closed on.
        other = TradingAccount.objects.create(user=self.owner, name="f2", broker_name="B",
                                              account_number="999", is_demo=False, is_active=True,
                                              broker_server=self.bs)
        CopierLiveArm.objects.create(
            authorization=authz, owner=self.owner, master_account=self.macct, follower_account=other,
            source_symbol="XAUUSD", follower_symbol="XAUUSD.std", state=CopierLiveArmState.ENGAGED,
            expires_at=authz.expires_at)
        self.assertIsNone(engaged_arm_for_authorization(authz))  # 2 engaged for authz → ambiguous → fail closed


class ArmCommandTests(_Base):
    def test_arm_success_creates_bound_arm(self):
        authz = self._authz()
        out = self._arm(authorization_id=authz.id, by="staff@x.invalid", reason="demo")
        self.assertIn('"changed": true', out)
        arm = CopierLiveArm.objects.get(authorization=authz, state=CopierLiveArmState.ENGAGED)
        self.assertEqual(arm.master_account_id, self.macct.id)
        self.assertEqual(arm.follower_account_id, self.facct.id)
        self.assertEqual(arm.armed_by_id, self.staff.id)
        self.assertLessEqual(arm.expires_at, authz.expires_at)

    def test_arm_caps_expiry_at_authorization(self):
        authz = self._authz(expires_at=timezone.now() + timedelta(minutes=10))
        self._arm(authorization_id=authz.id, by="staff@x.invalid", reason="d", valid_minutes=600)
        arm = CopierLiveArm.objects.get(authorization=authz)
        self.assertLessEqual(arm.expires_at, authz.expires_at)

    def test_arm_idempotent(self):
        authz = self._authz()
        self._arm(authorization_id=authz.id, by="staff@x.invalid", reason="d")
        out2 = self._arm(authorization_id=authz.id, by="staff@x.invalid", reason="d")
        self.assertIn('"idempotent": true', out2)
        self.assertEqual(CopierLiveArm.objects.filter(authorization=authz,
                                                      state=CopierLiveArmState.ENGAGED).count(), 1)

    def test_non_staff_rejected(self):
        authz = self._authz()
        with self.assertRaises(CommandError):
            self._arm(authorization_id=authz.id, by="own@x.invalid", reason="d")
        self.assertFalse(CopierLiveArm.objects.exists())

    def test_expired_authorization_rejected(self):
        authz = self._authz(expires_at=timezone.now() - timedelta(minutes=1))
        with self.assertRaises(CommandError):
            self._arm(authorization_id=authz.id, by="staff@x.invalid", reason="d")

    def test_revoked_authorization_rejected(self):
        authz = self._authz(status=CopierLiveAuthorizationStatus.REVOKED, revoked_at=timezone.now())
        with self.assertRaises(CommandError):
            self._arm(authorization_id=authz.id, by="staff@x.invalid", reason="d")

    def test_insufficient_window_rejected(self):
        authz = self._authz(expires_at=timezone.now() + timedelta(minutes=1))
        with self.assertRaises(CommandError):
            self._arm(authorization_id=authz.id, by="staff@x.invalid", reason="d", min_window_minutes=5)

    def test_allowance_consumed_rejected(self):
        authz = self._authz(orders_consumed=1, max_orders=1)
        with self.assertRaises(CommandError):
            self._arm(authorization_id=authz.id, by="staff@x.invalid", reason="d")

    def test_kill_switch_rejected(self):
        authz = self._authz()
        CopierLiveKillSwitch.objects.create(engaged=True)
        with self.assertRaises(CommandError):
            self._arm(authorization_id=authz.id, by="staff@x.invalid", reason="d")

    def test_conflicting_arm_rejected(self):
        authz1 = self._authz()
        attach_engaged_arm(authz1)   # engaged arm for the route, bound to authz1
        # A second authorization for the same route cannot be ACTIVE (unique), so force the conflict by consuming
        # authz1 then creating authz2 ACTIVE; the engaged arm (authz1) now conflicts for authz2's route.
        authz1.status = CopierLiveAuthorizationStatus.CONSUMED
        authz1.save(update_fields=["status"])
        authz2 = self._authz()
        with self.assertRaises(CommandError):
            self._arm(authorization_id=authz2.id, by="staff@x.invalid", reason="d")

    def test_master_not_flat_rejected(self):
        MasterObservationPoll.objects.filter(master=self.master).update(position_count=1)
        authz = self._authz()
        with self.assertRaises(CommandError):
            self._arm(authorization_id=authz.id, by="staff@x.invalid", reason="d")

    def test_master_not_flat_waivable(self):
        MasterObservationPoll.objects.filter(master=self.master).update(position_count=1)
        authz = self._authz()
        self._arm(authorization_id=authz.id, by="staff@x.invalid", reason="d", no_require_master_flat=True)
        self.assertTrue(CopierLiveArm.objects.filter(authorization=authz,
                                                     state=CopierLiveArmState.ENGAGED).exists())

    def test_command_creates_no_job(self):
        from execution.models import ExecutionJob
        authz = self._authz()
        self._arm(authorization_id=authz.id, by="staff@x.invalid", reason="d")
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_disengage_returns_off(self):
        authz = self._authz()
        self._arm(authorization_id=authz.id, by="staff@x.invalid", reason="d")
        call_command("copier_live_arm", authorization_id=authz.id, by="staff@x.invalid",
                     reason="demo complete", disengage=True)
        self.assertFalse(CopierLiveArm.objects.filter(authorization=authz,
                                                      state=CopierLiveArmState.ENGAGED).exists())
        arm = CopierLiveArm.objects.get(authorization=authz)
        self.assertEqual(arm.state, CopierLiveArmState.DISENGAGED)
        self.assertIsNotNone(arm.disengaged_at)

    def test_disengage_idempotent_no_arm(self):
        authz = self._authz()
        # no engaged arm → disengage is a no-op, not an error
        call_command("copier_live_arm", authorization_id=authz.id, by="staff@x.invalid",
                     reason="x", disengage=True)

    def test_kill_switch_disengages_arm(self):
        authz = self._authz()
        attach_engaged_arm(authz)
        call_command("copier_live_disarm", by="staff@x.invalid", reason="emergency")
        self.assertFalse(CopierLiveArm.objects.filter(state=CopierLiveArmState.ENGAGED).exists())

    def test_reconcile_disengages_expired_arm(self):
        authz = self._authz()
        arm = attach_engaged_arm(authz, expires_at=timezone.now() - timedelta(seconds=1))
        call_command("reconcile_expired_copier_authorizations", "--commit")
        arm.refresh_from_db()
        self.assertEqual(arm.state, CopierLiveArmState.DISENGAGED)
