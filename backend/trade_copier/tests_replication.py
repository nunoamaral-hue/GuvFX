"""Replication intent engine + follower service certification (DB-backed). No execution is performed."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from trade_copier import diff, services
from trade_copier.models import (
    CopierFollower,
    CopierFollowerAudit,
    CopierMaster,
    CopierReplication,
    CopierSymbolMap,
    DisablePolicy,
    MasterEvent,
    MasterObservationPoll,
    MasterPositionSnapshot,
    ReplicationStatus,
    SizingMode,
    SltpTranslation,
)
from trade_copier.replication import replicate_master_event
from trading.models import BrokerServer, TradingAccount

User = get_user_model()


class _Fixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner", email="o@x.invalid", password="x")
        self.bs_master = BrokerServer.objects.create(broker_display_name="IS6",
                                                     server_name="IS6Technologies-Demo", environment="demo")
        self.master_acct = TradingAccount.objects.create(
            user=self.user, name="master", broker_name="IS6", account_number="1000001",
            is_demo=True, is_active=True, broker_server=self.bs_master)
        self.master = CopierMaster.objects.create(trading_account=self.master_acct, observe_enabled=True)
        self.poll = MasterObservationPoll.objects.create(master=self.master, observed_at=timezone.now(),
                                                         position_count=0)
        self._seq = 0

    def _follower_account(self, number, *, user=None, broker=None, server=None, environment="demo"):
        # Reuse an existing BrokerServer by its globally-unique server_name (same-broker followers
        # share the master's broker server row); create a new one otherwise (cross-broker).
        bs, _ = BrokerServer.objects.get_or_create(
            server_name=server or f"srv-{number}",
            defaults={"broker_display_name": broker or "IS6", "environment": environment})
        return TradingAccount.objects.create(
            user=user or self.user, name=f"f{number}", broker_name=broker or "IS6",
            account_number=number, is_demo=(environment == "demo"), is_active=True, broker_server=bs)

    def _event(self, event_type, *, ticket=1, symbol="XAUUSD", side="BUY",
               vol_before=0.0, vol_after=0.10, new_sl=None, new_tp=None):
        self._seq += 1
        return MasterEvent.objects.create(
            master=self.master, sequence=self._seq, from_poll=self.poll, event_type=event_type,
            position_ticket=ticket, symbol=symbol, side=side,
            volume_before=Decimal(str(vol_before)), volume_after=Decimal(str(vol_after)),
            volume_delta=Decimal(str(vol_after - vol_before)),
            new_sl=(Decimal(str(new_sl)) if new_sl is not None else None),
            new_tp=(Decimal(str(new_tp)) if new_tp is not None else None),
            observed_at=timezone.now())


class ReplicationIntentTests(_Fixture):
    def test_open_records_shadow_intent_same_broker(self):
        facct = self._follower_account("2000001", server="IS6Technologies-Demo")  # same broker as master
        services.add_follower(self.master, facct, sizing_mode=SizingMode.MULTIPLIER, multiplier=Decimal("0.5"))
        f = CopierFollower.objects.get(follower_account=facct)
        services.set_follower_enabled(f, True)
        ev = self._event(diff.OPEN, vol_after=0.10)
        replicate_master_event(ev)
        rep = CopierReplication.objects.get(master_event=ev, follower=f)
        self.assertEqual(rep.status, ReplicationStatus.SHADOW_RECORDED)
        self.assertEqual(rep.requested_op, "OPEN")
        self.assertEqual(rep.target_volume, Decimal("0.050"))
        self.assertEqual(rep.follower_symbol, "XAUUSD")
        self.assertIsNone(rep.execution_job)   # PR B creates NO execution job

    def test_idempotent_no_double_replication(self):
        facct = self._follower_account("2000002", server="IS6Technologies-Demo")
        f = services.add_follower(self.master, facct, multiplier=Decimal("1"))
        services.set_follower_enabled(f, True)
        ev = self._event(diff.OPEN)
        replicate_master_event(ev)
        replicate_master_event(ev)  # re-run
        self.assertEqual(CopierReplication.objects.filter(master_event=ev, follower=f).count(), 1)

    def test_disabled_follower_skips_new_open(self):
        facct = self._follower_account("2000003", server="IS6Technologies-Demo")
        f = services.add_follower(self.master, facct)  # enabled=False
        ev = self._event(diff.OPEN)
        replicate_master_event(ev)
        rep = CopierReplication.objects.get(master_event=ev, follower=f)
        self.assertEqual(rep.status, ReplicationStatus.SKIPPED)
        self.assertEqual(rep.reason, "disabled_no_new_entries")

    def test_disabled_policy_A_keeps_managing_modify(self):
        facct = self._follower_account("2000004", server="IS6Technologies-Demo")
        f = services.add_follower(self.master, facct, disable_policy=DisablePolicy.A_KEEP_MANAGING)
        self._snap(1, sl=1990.0, tp=2100.0)   # authoritative full state (BLOCKER-3 requires it for a modify)
        ev = self._event(diff.SL_CHANGE, ticket=1, new_sl=1990.0)
        replicate_master_event(ev)
        rep = CopierReplication.objects.get(master_event=ev, follower=f)
        self.assertEqual(rep.status, ReplicationStatus.SHADOW_RECORDED)
        self.assertEqual(rep.requested_op, "MODIFY_SLTP")

    def test_disabled_policy_B_detach_skips_modify(self):
        facct = self._follower_account("2000005", server="IS6Technologies-Demo")
        f = services.add_follower(self.master, facct, disable_policy=DisablePolicy.B_DETACH)
        ev = self._event(diff.SL_CHANGE, new_sl=1990.0)
        replicate_master_event(ev)
        rep = CopierReplication.objects.get(master_event=ev, follower=f)
        self.assertEqual(rep.status, ReplicationStatus.SKIPPED)
        self.assertEqual(rep.reason, "detached_manual")

    def test_cross_broker_without_verified_map_rejected(self):
        facct = self._follower_account("2000006", broker="Pepperstone", server="Pepperstone-Demo")
        f = services.add_follower(self.master, facct)
        services.set_follower_enabled(f, True)
        ev = self._event(diff.OPEN)
        replicate_master_event(ev)
        rep = CopierReplication.objects.get(master_event=ev, follower=f)
        self.assertEqual(rep.status, ReplicationStatus.REJECTED)
        self.assertEqual(rep.reason, "symbol_map_unverified")

    def test_cross_broker_with_verified_map_resolves_symbol(self):
        facct = self._follower_account("2000007", broker="Pepperstone", server="Pepperstone-Demo")
        f = services.add_follower(self.master, facct, multiplier=Decimal("1"))
        services.set_follower_enabled(f, True)
        CopierSymbolMap.objects.create(master_broker="IS6Technologies-Demo", master_symbol="XAUUSD",
                                       follower_broker="Pepperstone-Demo", follower_symbol="GOLD",
                                       verified=True, verified_at=timezone.now())
        ev = self._event(diff.OPEN)
        replicate_master_event(ev)
        rep = CopierReplication.objects.get(master_event=ev, follower=f)
        self.assertEqual(rep.status, ReplicationStatus.SHADOW_RECORDED)
        self.assertEqual(rep.follower_symbol, "GOLD")

    def test_engine_rejects_cross_user_follower(self):
        # Direct create bypassing the service, to exercise the engine's defense-in-depth guard.
        other = User.objects.create_user(username="intruder", email="i@x.invalid", password="x")
        facct = self._follower_account("2000008", user=other, server="IS6Technologies-Demo")
        f = CopierFollower.objects.create(master=self.master, follower_account=facct, enabled=True)
        ev = self._event(diff.OPEN)
        replicate_master_event(ev)
        rep = CopierReplication.objects.get(master_event=ev, follower=f)
        self.assertEqual(rep.status, ReplicationStatus.REJECTED)
        self.assertEqual(rep.reason, "cross_user")

    def test_one_follower_rejection_does_not_block_another(self):
        good = self._follower_account("2000009", server="IS6Technologies-Demo")
        fg = services.add_follower(self.master, good, multiplier=Decimal("1"))
        services.set_follower_enabled(fg, True)
        # a cross-user follower (direct create) that will be rejected
        other = User.objects.create_user(username="x2", email="x2@x.invalid", password="x")
        bad = self._follower_account("2000010", user=other, server="IS6Technologies-Demo")
        fb = CopierFollower.objects.create(master=self.master, follower_account=bad, enabled=True)
        ev = self._event(diff.OPEN)
        replicate_master_event(ev)
        self.assertEqual(CopierReplication.objects.get(master_event=ev, follower=fg).status,
                         ReplicationStatus.SHADOW_RECORDED)
        self.assertEqual(CopierReplication.objects.get(master_event=ev, follower=fb).status,
                         ReplicationStatus.REJECTED)

    # ── BLOCKER-3: complete SL/TP desired state from the authoritative same-poll snapshot ──
    def _snap(self, ticket, *, sl=None, tp=None, entry=2000.0, symbol="XAUUSD", side="BUY", vol=0.10):
        return MasterPositionSnapshot.objects.create(
            poll=self.poll, position_ticket=ticket, symbol=symbol, side=side, volume=Decimal(str(vol)),
            sl=(Decimal(str(sl)) if sl is not None else None),
            tp=(Decimal(str(tp)) if tp is not None else None), price_open=Decimal(str(entry)))

    def _enabled_follower(self, number, **cfg):
        facct = self._follower_account(number, server="IS6Technologies-Demo")
        f = services.add_follower(self.master, facct, **cfg)
        services.set_follower_enabled(f, True)
        return f

    def test_modify_sl_only_preserves_existing_tp(self):
        f = self._enabled_follower("2000011")
        self._snap(1, sl=1995.0, tp=2100.0)                        # authoritative full state: BOTH set
        ev = self._event(diff.SL_CHANGE, ticket=1, new_sl=1995.0, new_tp=None)  # event carries ONLY new_sl
        replicate_master_event(ev)
        rep = CopierReplication.objects.get(master_event=ev, follower=f)
        self.assertEqual(rep.status, ReplicationStatus.SHADOW_RECORDED)
        self.assertEqual(float(rep.translated_sl), 1995.0)
        self.assertEqual(float(rep.translated_tp), 2100.0)         # TP preserved (not erased)

    def test_modify_tp_only_preserves_existing_sl(self):
        f = self._enabled_follower("2000012")
        self._snap(2, sl=1990.0, tp=2200.0)
        ev = self._event(diff.TP_CHANGE, ticket=2, new_sl=None, new_tp=2200.0)
        replicate_master_event(ev)
        rep = CopierReplication.objects.get(master_event=ev, follower=f)
        self.assertEqual(float(rep.translated_sl), 1990.0)         # SL preserved
        self.assertEqual(float(rep.translated_tp), 2200.0)

    def test_modify_without_authoritative_snapshot_rejected(self):
        f = self._enabled_follower("2000013")
        ev = self._event(diff.SL_CHANGE, ticket=99, new_sl=1995.0)  # no snapshot for ticket 99
        replicate_master_event(ev)
        rep = CopierReplication.objects.get(master_event=ev, follower=f)
        self.assertEqual(rep.status, ReplicationStatus.REJECTED)
        self.assertEqual(rep.reason, "master_state_unavailable")

    # ── BLOCKER-4: unknown opcode fail-closed; non-authoritative broker constraints flagged ──
    def test_unknown_event_type_rejected_not_defaulted_to_mutation(self):
        f = self._enabled_follower("2000014")
        self._seq += 1
        ev = MasterEvent.objects.create(
            master=self.master, sequence=self._seq, from_poll=self.poll, event_type="BOGUS_EVENT",
            position_ticket=1, symbol="XAUUSD", side="BUY", volume_before=Decimal("0"),
            volume_after=Decimal("0.1"), volume_delta=Decimal("0.1"), observed_at=timezone.now())
        replicate_master_event(ev)
        rep = CopierReplication.objects.get(master_event=ev, follower=f)
        self.assertEqual(rep.status, ReplicationStatus.REJECTED)
        self.assertEqual(rep.reason, "unknown_event_type")

    def test_open_marks_broker_constraints_unverified_when_no_metadata(self):
        f = self._enabled_follower("2000015", multiplier=Decimal("1"))
        ev = self._event(diff.OPEN, ticket=5, vol_after=0.10)
        replicate_master_event(ev)
        rep = CopierReplication.objects.get(master_event=ev, follower=f)
        self.assertEqual(rep.status, ReplicationStatus.SHADOW_RECORDED)
        self.assertIn("unverified", rep.sizing_detail)             # guessed constraints flagged for B-exec


class FollowerServiceTests(_Fixture):
    def test_add_follower_rejects_self_follow(self):
        with self.assertRaises(services.FollowerConfigError):
            services.add_follower(self.master, self.master_acct)

    def test_add_follower_rejects_cross_user(self):
        other = User.objects.create_user(username="other", email="ot@x.invalid", password="x")
        facct = self._follower_account("3000001", user=other, server="IS6Technologies-Demo")
        with self.assertRaises(services.FollowerConfigError):
            services.add_follower(self.master, facct)

    def test_add_follower_writes_audit_and_disabled(self):
        facct = self._follower_account("3000002", server="IS6Technologies-Demo")
        f = services.add_follower(self.master, facct)
        self.assertFalse(f.enabled)   # a follower cannot activate itself
        self.assertTrue(CopierFollowerAudit.objects.filter(follower=f, action="created").exists())

    def test_set_enabled_writes_audit(self):
        facct = self._follower_account("3000003", server="IS6Technologies-Demo")
        f = services.add_follower(self.master, facct)
        services.set_follower_enabled(f, True)
        self.assertTrue(CopierFollowerAudit.objects.filter(follower=f, action="enabled").exists())
        f.refresh_from_db()
        self.assertTrue(f.enabled)

    def test_unprotected_close_authorization_is_versioned_and_revocable(self):
        facct = self._follower_account("3000004", server="IS6Technologies-Demo")
        f = services.add_follower(self.master, facct)
        services.authorize_unprotected_close(f)
        f.refresh_from_db()
        self.assertTrue(f.unprotected_close_enabled)
        self.assertEqual(f.unprotected_close_authorization_version, 1)
        services.revoke_unprotected_close(f)
        f.refresh_from_db()
        self.assertFalse(f.unprotected_close_enabled)
        self.assertTrue(CopierFollowerAudit.objects.filter(
            follower=f, action="unprotected_close_revoked").exists())
