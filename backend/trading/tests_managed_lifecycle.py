"""Phase 8 — managed Start/Stop trading lifecycle + truthful trading-state read-model + Stop-kill.

Covers the adversarial matrix: readiness-gated Start (fail-closed on every missing precondition), truthful
state projection (never "Trading" while trade_allowed=False), account-scoped capability recovery reuse,
product-driven promotion, idempotence, and the Stop-Trading creation kill (open-position management preserved).
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution.hosted_provisioning import ArmResult, ARM_OK
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount
from trading.trading_state import (resolve_trading_state, TRADING, PREPARING, TRADING_STOPPED,
                                    BROKER_CONNECTED, BROKER_LOGIN_REQUIRED, ATTENTION)

U = get_user_model()
_RP = TradingAccount.ReadinessProvider


def _acct(user, n, **kw):
    kw.setdefault("account_number", f"7{n:04d}")
    kw.setdefault("is_demo", True)
    kw.setdefault("readiness_provider", _RP.PERSISTENT_WORKSPACE)
    return TradingAccount.objects.create(user=user, name="a", broker_name="B", **kw)


def _ws(acct, **kw):
    base = dict(canonical_state=S.EXECUTION_READY, proj_connected=True, proj_trade_allowed=True,
                proj_account_match=True, proj_execution_ready=True, proj_margin_mode=2,
                last_decision_at=timezone.now(), execution_enabled=True,
                execution_authorized_at=timezone.now())
    base.update(kw)
    ws = HostedMt5Workspace.objects.create(trading_account=acct, **base)
    return ws


def _confirm(acct):
    acct.workspace_confirmed_at = timezone.now()
    acct.save(update_fields=["workspace_confirmed_at"])


def _strategy(user, *, marketplace=False, slug="wayond-wim", source="ti_signals", name="Wayond WIM Strategy"):
    return Strategy.objects.create(
        owner=user, name=name, is_marketplace=marketplace,
        filters={"template_slug": slug, "signal_source": source})


def _assign(acct, strat, **kw):
    kw.setdefault("is_active", True)
    a = StrategyAssignment.objects.create(account=acct, strategy=strat, **kw)
    return a


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class TradingStateReadModelTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="t", email="t@x.invalid", password="x")
        self._n = 0

    def _mk(self, **acct_kw):
        self._n += 1
        return _acct(self.user, self._n, **acct_kw)

    def test_active_and_ready_is_trading(self):
        a = self._mk(is_active=True)
        _ws(a); _confirm(a)
        self.assertEqual(resolve_trading_state(a)["state"], TRADING)

    def test_active_but_trade_allowed_false_is_preparing_not_trading(self):
        # CASE 24 — the Pepperstone bug: is_active=True must NEVER read "Trading" when MT5 can't auto-trade.
        a = self._mk(is_active=True)
        _ws(a, canonical_state=S.CONNECTED, proj_trade_allowed=False, proj_execution_ready=False,
            execution_enabled=False, execution_authorized_at=None)
        _confirm(a)
        st = resolve_trading_state(a)
        self.assertEqual(st["state"], PREPARING)
        self.assertNotEqual(st["state"], TRADING)

    def test_active_recovery_in_progress_is_preparing(self):
        a = self._mk(is_active=True)
        _ws(a, canonical_state=S.CONNECTED, proj_trade_allowed=False, proj_execution_ready=False,
            execution_enabled=False, execution_authorized_at=None, capability_recovery_count=1)
        _confirm(a)
        st = resolve_trading_state(a)
        self.assertEqual(st["state"], PREPARING)
        self.assertIn("Restoring", st["detail"])

    def test_active_not_connected_is_login_required(self):
        a = self._mk(is_active=True)
        _ws(a, canonical_state=S.CONNECTED, proj_connected=False, proj_trade_allowed=False,
            execution_enabled=False, execution_authorized_at=None)
        self.assertEqual(resolve_trading_state(a)["state"], BROKER_LOGIN_REQUIRED)

    def test_active_account_mismatch_is_attention(self):
        a = self._mk(is_active=True)
        _ws(a, canonical_state=S.CONNECTED, proj_account_match=False, proj_trade_allowed=False,
            execution_enabled=False, execution_authorized_at=None)
        self.assertEqual(resolve_trading_state(a)["state"], ATTENTION)

    def test_active_real_account_is_attention(self):
        a = self._mk(is_active=True, is_demo=False)
        _ws(a); _confirm(a)
        self.assertEqual(resolve_trading_state(a)["state"], ATTENTION)

    def test_stopped_with_active_strategy_is_trading_stopped(self):
        a = self._mk(is_active=False)
        _ws(a, canonical_state=S.CONNECTED, proj_trade_allowed=False, execution_enabled=False,
            execution_authorized_at=None)
        _assign(a, _strategy(self.user))
        self.assertEqual(resolve_trading_state(a)["state"], TRADING_STOPPED)

    def test_stopped_no_strategy_connected_is_broker_connected(self):
        a = self._mk(is_active=False)
        _ws(a, canonical_state=S.CONNECTED, proj_trade_allowed=False, execution_enabled=False,
            execution_authorized_at=None)
        self.assertEqual(resolve_trading_state(a)["state"], BROKER_CONNECTED)

    def test_stopped_not_connected_is_login_required(self):
        a = self._mk(is_active=False)
        _ws(a, canonical_state=S.CONNECTED, proj_connected=False, proj_account_match=False,
            proj_trade_allowed=False, execution_enabled=False, execution_authorized_at=None)
        self.assertEqual(resolve_trading_state(a)["state"], BROKER_LOGIN_REQUIRED)

    def test_legacy_account_uses_is_active_mapping(self):
        # A non-persistent (legacy) account keeps the simple is_active display, unchanged.
        a = self._mk(is_active=True, readiness_provider=_RP.TEMPORARY_VALIDATION)
        self.assertEqual(resolve_trading_state(a)["state"], TRADING)
        a.is_active = False
        self.assertEqual(resolve_trading_state(a)["state"], TRADING_STOPPED)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1",
                   MANAGED_START_TRADING_ENABLED="1", MANAGED_START_AUTHORIZES_EXECUTION="")
class ManagedStartTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="m", email="m@x.invalid", password="x")
        self._n = 0

    def _mk(self, **kw):
        self._n += 1
        return _acct(self.user, self._n, **kw)

    def _runtime_ready(self, val=False):
        # Default FALSE on purpose: the Provider-B preflight must NOT depend on AccountRuntime (Accounts 25/35/36
        # have none). Patching _account_runtime_ready=False proves the workspace-projection path stands alone —
        # and if the fix were reverted to gate on AccountRuntime, these tests would fail (no longer masked).
        return mock.patch("trading.views._account_runtime_ready", return_value=val)

    def _no_arm(self):
        return mock.patch("execution.hosted_provisioning.arm_hosted_workspace_execution",
                          return_value=ArmResult(True, ARM_OK))

    def _start(self, acct):
        from trading.managed_start import managed_start_trading
        return managed_start_trading(acct, self.user, actor="test")

    @override_settings(MANAGED_START_TRADING_ENABLED="")
    def test_flag_off_does_not_apply(self):
        a = self._mk(is_active=False); _ws(a); _confirm(a)
        self.assertFalse(self._start(a).applies)

    def test_provider_b_no_accountruntime_passes_preflight_and_enters_recovery(self):
        # THE PRODUCTION DEFECT (Account 36): a Provider-B hosted account has NO AccountRuntime, but its
        # workspace is connected+matched+fresh. Start must PASS the runtime preflight (not gate on AccountRuntime)
        # and ENTER capability recovery because trade_allowed=False (expected before recovery).
        a = self._mk(is_active=False)
        _ws(a, canonical_state=S.CONNECTED, proj_trade_allowed=False, proj_execution_ready=False)  # connected, fresh
        _confirm(a)
        _assign(a, _strategy(self.user, marketplace=True))
        self.assertFalse(hasattr(a, "runtime") and a.runtime)   # no AccountRuntime (Provider-B)
        rec = mock.Mock(return_value={"enabled": True, "outcome": "relaunched"})
        # _account_runtime_ready deliberately False (AccountRuntime absent) — must NOT block the Provider-B path.
        with mock.patch("trading.views._account_runtime_ready", return_value=False), self._no_arm(), \
             mock.patch("hosted_workspace.capability_recovery.recover_capability_for_account", rec):
            r = self._start(a)
        self.assertTrue(r.applies and r.ok, r.reason)           # passed preflight despite no AccountRuntime
        rec.assert_called_once()                                 # entered capability recovery
        self.assertEqual(r.trading_state["state"], PREPARING)   # trade_allowed still False → truthful PREPARING

    def test_stale_observation_blocked(self):
        # Runtime preflight fails CLOSED when the observation is stale (broker connectivity unproven).
        from django.utils import timezone as _tz
        a = self._mk(is_active=False)
        _ws(a, last_decision_at=_tz.now() - _tz.timedelta(seconds=3600))  # stale
        _confirm(a)
        with self._runtime_ready(False):
            r = self._start(a)
        self.assertFalse(r.ok); self.assertEqual(r.reason, "observation_stale")
        a.refresh_from_db(); self.assertFalse(a.is_active)

    def test_provider_b_no_workspace_blocked(self):
        a = self._mk(is_active=False)          # Provider-B, NO workspace created
        with self._runtime_ready(False):
            r = self._start(a)
        self.assertFalse(r.ok); self.assertEqual(r.reason, "workspace_missing")
        a.refresh_from_db(); self.assertFalse(a.is_active)

    def test_disconnected_account_blocked(self):
        from django.utils import timezone as _tz
        a = self._mk(is_active=False); _ws(a); _confirm(a)
        a.disconnected_at = _tz.now(); a.save(update_fields=["disconnected_at"])
        with self._runtime_ready(False):
            r = self._start(a)
        self.assertFalse(r.ok); self.assertEqual(r.reason, "broker_not_connected")
        a.refresh_from_db(); self.assertFalse(a.is_active)

    def test_terminal_not_ready_blocked_legacy_provider(self):
        # A non-Provider-B (legacy) account with no AccountRuntime falls through to _account_runtime_ready and
        # is blocked terminal_not_ready — covers the legacy branch + reason (was previously uncovered).
        a = self._mk(is_active=False, readiness_provider="temporary_validation")
        with self._runtime_ready(False):
            r = self._start(a)
        self.assertFalse(r.ok); self.assertEqual(r.reason, "terminal_not_ready")
        a.refresh_from_db(); self.assertFalse(a.is_active)

    def test_unobserved_connected_blocked(self):
        # RULE 11 negative control: an UNOBSERVED (None) proj_connected must fail closed (is not True catches None).
        a = self._mk(is_active=False); _ws(a, proj_connected=None); _confirm(a)
        with self._runtime_ready(False):
            r = self._start(a)
        self.assertFalse(r.ok); self.assertEqual(r.reason, "broker_not_connected")

    def test_unobserved_match_blocked(self):
        a = self._mk(is_active=False); _ws(a, proj_account_match=None); _confirm(a)
        with self._runtime_ready(False):
            r = self._start(a)
        self.assertFalse(r.ok); self.assertEqual(r.reason, "account_mismatch")

    def test_unobserved_margin_blocked(self):
        a = self._mk(is_active=False); _ws(a, proj_margin_mode=None); _confirm(a)
        _assign(a, _strategy(self.user, marketplace=True))
        with self._runtime_ready(False):
            r = self._start(a)
        self.assertFalse(r.ok); self.assertEqual(r.reason, "margin_mode")

    def test_not_demo_blocked(self):
        a = self._mk(is_active=False, is_demo=False); _ws(a); _confirm(a)
        with self._runtime_ready():
            r = self._start(a)
        self.assertEqual(r.reason, "demo_only"); self.assertFalse(r.ok)

    def test_broker_not_connected_blocked(self):
        a = self._mk(is_active=False); _ws(a, proj_connected=False); _confirm(a)
        with self._runtime_ready():
            r = self._start(a)
        self.assertEqual(r.reason, "broker_not_connected")

    def test_account_mismatch_blocked(self):
        a = self._mk(is_active=False); _ws(a, proj_account_match=False); _confirm(a)
        with self._runtime_ready():
            r = self._start(a)
        self.assertEqual(r.reason, "account_mismatch")

    def test_netting_margin_blocked(self):
        a = self._mk(is_active=False); _ws(a, proj_margin_mode=0); _confirm(a)
        _assign(a, _strategy(self.user, marketplace=True))
        with self._runtime_ready():
            r = self._start(a)
        self.assertEqual(r.reason, "margin_mode")

    def test_no_strategy_blocked(self):
        a = self._mk(is_active=False); _ws(a); _confirm(a)
        with self._runtime_ready():
            r = self._start(a)
        self.assertEqual(r.reason, "no_strategy")

    def test_invalid_sizing_blocked(self):
        a = self._mk(is_active=False); _ws(a); _confirm(a)
        asn = _assign(a, _strategy(self.user, marketplace=True))
        with self._runtime_ready(), \
             mock.patch("strategies.models.effective_lot_per_leg", return_value=Decimal("0")):
            r = self._start(a)
        self.assertEqual(r.reason, "invalid_sizing")

    def test_not_confirmed_blocked_when_authorize_gate_off(self):
        a = self._mk(is_active=False); _ws(a)  # not confirmed
        _assign(a, _strategy(self.user, marketplace=True))
        with self._runtime_ready():
            r = self._start(a)
        self.assertEqual(r.reason, "not_confirmed"); self.assertFalse(r.ok)
        a.refresh_from_db(); self.assertFalse(a.is_active)

    def test_not_authorized_blocked_when_authorize_gate_off(self):
        a = self._mk(is_active=False); _ws(a, execution_authorized_at=None); _confirm(a)
        _assign(a, _strategy(self.user, marketplace=True))
        with self._runtime_ready():
            r = self._start(a)
        self.assertEqual(r.reason, "not_authorized")

    def test_happy_path_ready_commits_and_trading(self):
        a = self._mk(is_active=False); _ws(a); _confirm(a)
        _assign(a, _strategy(self.user, marketplace=True))
        with self._runtime_ready(), self._no_arm():
            r = self._start(a)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.trading_state["state"], TRADING)
        a.refresh_from_db(); self.assertTrue(a.is_active)

    def test_capability_recovery_invoked_when_trade_allowed_false(self):
        a = self._mk(is_active=False)
        _ws(a, canonical_state=S.CONNECTED, proj_trade_allowed=False, proj_execution_ready=False)
        _confirm(a)
        _assign(a, _strategy(self.user, marketplace=True))
        rec = mock.Mock(return_value={"enabled": True, "outcome": "relaunched"})
        with self._runtime_ready(), self._no_arm(), \
             mock.patch("hosted_workspace.capability_recovery.recover_capability_for_account", rec):
            r = self._start(a)
        rec.assert_called_once()
        self.assertEqual(rec.call_args.args[0], a.id)
        self.assertTrue(rec.call_args.kwargs.get("bypass_onboarding_gate"))
        # trade_allowed still False (recovery is async) → committed intent but truthful PREPARING, never TRADING.
        self.assertTrue(r.ok)
        self.assertEqual(r.trading_state["state"], PREPARING)

    def test_product_driven_promotion_sets_live_auto_demo(self):
        a = self._mk(is_active=False); _ws(a); _confirm(a)
        asn = _assign(a, _strategy(self.user, marketplace=True), stage="TEST", execution_mode="MANUAL")
        with self._runtime_ready(), self._no_arm():
            self._start(a)
        asn.refresh_from_db()
        self.assertEqual(asn.stage, "LIVE")
        self.assertEqual(asn.execution_mode, "AUTO_DEMO")
        self.assertEqual(asn.signal_source, "ti_signals")

    def test_non_marketplace_strategy_not_promoted(self):
        a = self._mk(is_active=False); _ws(a); _confirm(a)
        asn = _assign(a, _strategy(self.user, marketplace=False), stage="TEST", execution_mode="MANUAL")
        with self._runtime_ready(), self._no_arm():
            self._start(a)
        asn.refresh_from_db()
        self.assertEqual(asn.stage, "TEST")           # a private/own strategy is not force-promoted
        self.assertEqual(asn.execution_mode, "MANUAL")

    def test_idempotent_repeat_start(self):
        a = self._mk(is_active=False); _ws(a); _confirm(a)
        _assign(a, _strategy(self.user, marketplace=True))
        with self._runtime_ready(), self._no_arm():
            r1 = self._start(a)
            r2 = self._start(a)
        self.assertTrue(r1.ok and r2.ok)
        a.refresh_from_db(); self.assertTrue(a.is_active)

    @override_settings(MANAGED_START_AUTHORIZES_EXECUTION="1")
    def test_authorize_gate_on_sets_confirm_and_authorization(self):
        a = self._mk(is_active=False)
        _ws(a, execution_authorized_at=None)          # unconfirmed + unauthorized
        _assign(a, _strategy(self.user, marketplace=True))
        authz = mock.Mock()

        def _do_authz(user, ws, **kw):
            ws.execution_authorized_at = timezone.now()
            ws.save(update_fields=["execution_authorized_at"])
        authz.side_effect = _do_authz
        with self._runtime_ready(), self._no_arm(), \
             mock.patch("hosted_workspace.provisioning.authorize_workspace_execution", authz):
            r = self._start(a)
        a.refresh_from_db()
        self.assertIsNotNone(a.workspace_confirmed_at)   # Start performed the confirm
        authz.assert_called_once()                        # Start performed the ADR-0047 authorization
        self.assertTrue(r.ok)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1",
                   MANAGED_START_TRADING_ENABLED="1", MANAGED_START_AUTHORIZES_EXECUTION="")
class RealArmReorderTests(TestCase):
    """Fix #5 — commit is_active BEFORE arm, so a genuinely-ready fresh Start arms synchronously (arm's
    precondition requires is_active=True) and reads TRADING, exercising the REAL arm (no mock)."""

    def setUp(self):
        self.user = U.objects.create_user(username="ra", email="ra@x.invalid", password="x")
        from execution.models import TerminalNode
        self.node = TerminalNode.objects.create(hostname="node-test-1")

    def test_ready_start_arms_synchronously_and_trades(self):
        a = _acct(self.user, 1, is_active=False, terminal_node=self.node)
        _ws(a, execution_enabled=False, execution_node=self.node)   # ready but NOT yet armed
        _confirm(a)
        _assign(a, _strategy(self.user, marketplace=True))
        from trading.managed_start import managed_start_trading
        with mock.patch("trading.views._account_runtime_ready", return_value=True):
            r = managed_start_trading(a, self.user, actor="test")   # REAL arm (not mocked)
        self.assertTrue(r.ok, r.reason)
        self.assertTrue(r.armed)                                     # arm succeeded because commit ran first
        a.refresh_from_db()
        self.assertTrue(a.is_active)
        self.assertTrue(a.hosted_workspace.execution_enabled)       # execution_enabled flipped by the real arm
        self.assertEqual(r.trading_state["state"], TRADING)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_CAPABILITY_RECOVERY_ENABLED="1",
                   HOSTED_BOUNDED_OBSERVATION_ENABLED="1")
class AccountScopedRecoveryTests(TestCase):
    """Fix #3 — direct coverage of recover_capability_for_account, including the reserved-id type-total guard."""

    def setUp(self):
        self.user = U.objects.create_user(username="s", email="s@x.invalid", password="x")
        self._n = 0

    def _nonreserved_acct(self, **kw):
        # DB auto-ids can land on 1 or 18 (the production RESERVED ids); a normal test account on a reserved id
        # would be (correctly) refused as "reserved". Skip those so these tests exercise the intended paths.
        a = _acct(self.user, self._n, **kw)
        while a.id in (1, 18):
            self._n += 1
            a = _acct(self.user, self._n, **kw)
        return a

    def _stuck(self, confirmed=True):
        self._n += 1
        a = self._nonreserved_acct(is_active=True)
        _ws(a, canonical_state=S.CONNECTED, proj_trade_allowed=False, proj_execution_ready=False,
            execution_enabled=False, execution_authorized_at=None)
        if confirmed:
            _confirm(a)
        return a

    def _rec(self, account_id, **kw):
        from hosted_workspace.capability_recovery import recover_capability_for_account
        return recover_capability_for_account(account_id, **kw)

    @override_settings(HOSTED_CAPABILITY_RECOVERY_ENABLED="")
    def test_flags_off_disabled(self):
        self.assertEqual(self._rec(999)["outcome"], "disabled")

    def test_reserved_int_refused(self):
        self.assertEqual(self._rec(1)["outcome"], "reserved")
        self.assertEqual(self._rec(18)["outcome"], "reserved")

    def test_reserved_string_refused(self):
        # The bug this fixes: a string account_id must NOT bypass the reserved-id guard.
        self.assertEqual(self._rec("1")["outcome"], "reserved")
        self.assertEqual(self._rec("18")["outcome"], "reserved")

    def test_invalid_account_id(self):
        self.assertEqual(self._rec("not-a-number")["outcome"], "invalid_account_id")

    def test_no_workspace(self):
        self._n = 90
        a = self._nonreserved_acct(is_active=True)   # no workspace created
        self.assertEqual(self._rec(a.id)["outcome"], "no_workspace")

    def test_not_a_candidate_when_trade_allowed(self):
        a = self._stuck()
        a.hosted_workspace.proj_trade_allowed = True
        a.hosted_workspace.canonical_state = S.EXECUTION_READY
        a.hosted_workspace.save(update_fields=["proj_trade_allowed", "canonical_state"])
        self.assertEqual(self._rec(a.id)["outcome"], "not_a_candidate")

    def test_onboarding_gate_holds_unconfirmed_without_bypass(self):
        a = self._stuck(confirmed=False)
        self.assertEqual(self._rec(a.id, executor_resolver=lambda *_: None)["outcome"], "skipped_onboarding")

    def test_bypass_onboarding_gets_past_onboarding_only(self):
        # Without bypass an unconfirmed stuck account is held at onboarding; WITH bypass it advances past
        # onboarding to the next real gate (here: no node/executor). Proves bypass skips ONLY the onboarding hold.
        a = self._stuck(confirmed=False)
        held = self._rec(a.id, executor_resolver=lambda *_: None)["outcome"]
        self.assertEqual(held, "skipped_onboarding")
        advanced = self._rec(a.id, bypass_onboarding_gate=True, executor_resolver=lambda *_: None)["outcome"]
        self.assertNotEqual(advanced, "skipped_onboarding")   # advanced past onboarding
        self.assertIn(advanced, ("skipped_not_ready", "skipped_no_executor", "skipped_cooldown"))


class StopTradingKillTests(TestCase):
    """Phase 6 — the account-level Stop is the authoritative creation kill for NEW automated entries, while
    open-position management (CLOSE/MODIFY) is never blocked."""

    def setUp(self):
        self.user = U.objects.create_user(username="k", email="k@x.invalid", password="x")

    def _acct(self, active):
        return TradingAccount.objects.create(user=self.user, name="a", broker_name="B",
                                             account_number="8001", is_demo=True, is_active=active)

    def _job(self, acct, jt):
        from execution.models import ExecutionJob
        return ExecutionJob(account=acct, job_type=jt, payload={})

    def test_place_order_on_stopped_account_refused(self):
        from execution.models import ExecutionJob, ExecutionAccountStopped
        a = self._acct(active=False)
        with self.assertRaises(ExecutionAccountStopped):
            self._job(a, ExecutionJob.JobType.PLACE_ORDER).save()

    def test_open_trade_on_stopped_account_refused(self):
        from execution.models import ExecutionJob, ExecutionAccountStopped
        a = self._acct(active=False)
        with self.assertRaises(ExecutionAccountStopped):
            self._job(a, ExecutionJob.JobType.OPEN_TRADE).save()

    def test_place_order_on_active_account_allowed(self):
        from execution.models import ExecutionJob
        a = self._acct(active=True)
        self._job(a, ExecutionJob.JobType.PLACE_ORDER).save()   # must not raise

    def test_close_trade_on_stopped_account_allowed(self):
        # Open-position management must NOT be orphaned by Stop.
        from execution.models import ExecutionJob
        a = self._acct(active=False)
        self._job(a, ExecutionJob.JobType.CLOSE_TRADE).save()   # must not raise

    def test_modify_position_on_stopped_account_allowed(self):
        from execution.models import ExecutionJob
        a = self._acct(active=False)
        self._job(a, ExecutionJob.JobType.MODIFY_POSITION).save()  # must not raise

    @override_settings(STOP_TRADING_ACCOUNT_GATE_ENABLED="0")
    def test_gate_off_rolls_back_to_legacy(self):
        from execution.models import ExecutionJob
        a = self._acct(active=False)
        self._job(a, ExecutionJob.JobType.PLACE_ORDER).save()   # gate off → not blocked
