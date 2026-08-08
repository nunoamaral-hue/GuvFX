"""ADR-0033 — tests for the execution-readiness provider abstraction (backend/execution/readiness.py)
and its integration into broker_gate.evaluate_execution_gate.

Proves: provider selection; Provider A regression-identical to the pre-ADR gate; Provider B fail-closed
with NO password_enc / VALIDATED requirement; the lifecycle checks (is_active/disconnected_at) are ANDed
(never dropped) for Provider B; a wrong active account reports the SPECIFIC active_account_mismatch code;
and the whole persistent path is triple-dark.
"""
from __future__ import annotations

import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution import broker_gate as g
from execution import readiness as R
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState
from trading.models import TradingAccount

U = get_user_model()
_RP = TradingAccount.ReadinessProvider


def _gate_on():
    return mock.patch.dict(os.environ, {"BROKER_CONNECTIVITY_EXECUTION_GATE": "1"}, clear=False)


class ProviderSelectionTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="r", email="r@x.invalid", password="x")
        self._n = 0

    def _acct(self, **kw):
        self._n += 1
        kw.setdefault("account_number", f"5{self._n:04d}")
        return TradingAccount.objects.create(
            user=self.user, name="a", broker_name="B", is_demo=True, **kw)

    def test_default_is_temporary(self):
        a = self._acct()
        self.assertEqual(a.readiness_provider, _RP.TEMPORARY_VALIDATION)
        self.assertIsInstance(R.provider_for(a), R.TemporaryValidationProvider)

    def test_persistent_selects_provider_b(self):
        a = self._acct(readiness_provider=_RP.PERSISTENT_WORKSPACE)
        self.assertIsInstance(R.provider_for(a), R.PersistentWorkspaceProvider)

    def test_unknown_falls_back_to_temporary(self):
        a = self._acct()
        a.readiness_provider = "garbage"
        self.assertIsInstance(R.provider_for(a), R.TemporaryValidationProvider)


class ProviderARegressionTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="ra", email="ra@x.invalid", password="x")
        self._n = 0

    def _acct(self, **kw):
        self._n += 1
        kw.setdefault("account_number", f"5{self._n:04d}")
        return TradingAccount.objects.create(
            user=self.user, name="a", broker_name="B", is_demo=True, **kw)

    def test_inactive_disconnected_credential_validation_ladder(self):
        d = R.TemporaryValidationProvider().evaluate
        a = self._acct(is_active=False)
        self.assertEqual(d(a).reason_code, g.R_ACCOUNT_INACTIVE)
        a = self._acct(disconnected_at=timezone.now())
        self.assertEqual(d(a).reason_code, g.R_ACCOUNT_DISCONNECTED)
        a = self._acct(password_enc="")
        self.assertEqual(d(a).reason_code, g.R_CREDENTIAL_MISSING)
        a = self._acct(password_enc="enc", validation_status=TradingAccount.ValidationStatus.NEVER)
        self.assertEqual(d(a).reason_code, g.R_NOT_VALIDATED_NEVER)
        a = self._acct(password_enc="enc", validation_status=TradingAccount.ValidationStatus.VALIDATED)
        dec = d(a)
        self.assertTrue(dec.eligible)
        self.assertEqual(dec.reason_code, g.GATE_OK)

    def test_gate_delegation_is_identical_for_temporary(self):
        with _gate_on():
            a = self._acct(password_enc="enc",
                           validation_status=TradingAccount.ValidationStatus.VALIDATED)
            self.assertTrue(g.evaluate_execution_gate(a).allowed)
            a2 = self._acct(password_enc="enc",
                            validation_status=TradingAccount.ValidationStatus.NEVER)
            dec = g.evaluate_execution_gate(a2)
            self.assertFalse(dec.allowed)
            self.assertEqual(dec.reason_code, g.R_NOT_VALIDATED_NEVER)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class ProviderBTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="rb", email="rb@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="a", broker_name="B", account_number="500", is_demo=True,
            readiness_provider=_RP.PERSISTENT_WORKSPACE)  # NOTE: no password_enc, NOT VALIDATED

    def _ready_ws(self, **kw):
        # Set the M3c CANONICAL projection — the fields the certified single writer maintains and the
        # fields Provider-B readiness now reads (ADR-0034 Execution Engine G1 / Decision A).
        base = dict(canonical_state=WorkspaceLifecycleState.EXECUTION_READY,
                    proj_connected=True, proj_trade_allowed=True, proj_account_match=True,
                    proj_execution_ready=True, last_decision_at=timezone.now(),
                    execution_enabled=True)  # ADR-0034 Execution Engine — the explicit per-workspace ARM
        base.update(kw)
        return HostedMt5Workspace.objects.create(trading_account=self.acct, **base)

    def test_ready_without_password_or_validated(self):
        self._ready_ws()
        dec = R.PersistentWorkspaceProvider().evaluate(self.acct)
        self.assertTrue(dec.eligible, dec.reason_code)
        self.assertEqual(self.acct.password_enc, "")
        self.assertEqual(self.acct.validation_status, TradingAccount.ValidationStatus.NEVER)

    def test_missing_workspace_fails_closed(self):
        self.assertEqual(R.PersistentWorkspaceProvider().evaluate(self.acct).reason_code,
                         R.RW_WORKSPACE_MISSING)

    def test_wrong_account_reports_specific_mismatch(self):
        # A connected, trade-allowed workspace whose active account != bound account reports the
        # SPECIFIC active_account_mismatch (not a generic not-ready).
        self._ready_ws(proj_account_match=False)
        self.assertEqual(R.PersistentWorkspaceProvider().evaluate(self.acct).reason_code,
                         R.RW_ACTIVE_ACCOUNT_MISMATCH)

    def test_not_connected_and_stale_fail_closed(self):
        ws = self._ready_ws(proj_connected=False)
        self.assertEqual(R.PersistentWorkspaceProvider().evaluate(self.acct).reason_code,
                         R.RW_WORKSPACE_NOT_CONNECTED)
        ws.proj_connected = True
        ws.last_decision_at = timezone.now() - timezone.timedelta(
            seconds=R.WORKSPACE_OBSERVATION_FRESH_SECONDS + 60)
        ws.save()
        self.assertEqual(R.PersistentWorkspaceProvider().evaluate(self.acct).reason_code,
                         R.RW_OBSERVATION_STALE)

    def test_not_ready_trade_disjunct_only(self):
        # Mutation adequacy for the compound RW_WORKSPACE_NOT_READY (readiness.py:150) — disjunct 1 ONLY:
        # broker trading halted, canonical stays EXECUTION_READY. Kills a mutant dropping the
        # `proj_trade_allowed is not True` sub-check. (The arm twin is split in tests_hosted_capstone; this is
        # the parallel — more safety-critical — dispatch-path copy.)
        self._ready_ws(proj_trade_allowed=False)
        self.assertEqual(R.PersistentWorkspaceProvider().evaluate(self.acct).reason_code,
                         R.RW_WORKSPACE_NOT_READY)

    def test_not_ready_canonical_disjunct_only(self):
        # disjunct 2 ONLY: canonical not EXECUTION_READY, trading stays allowed. Kills a mutant dropping the
        # `not canonical_execution_ready` sub-check — the exact "persisted cache is not authority" fail-open.
        self._ready_ws(canonical_state=WorkspaceLifecycleState.CONNECTED)
        self.assertEqual(R.PersistentWorkspaceProvider().evaluate(self.acct).reason_code,
                         R.RW_WORKSPACE_NOT_READY)

    def test_future_decision_timestamp_is_stale(self):
        # Freshness lower-bound guard (`0 <= age`): a FUTURE last_decision_at (clock skew / bad stamp) must
        # NOT be treated as fresh. Kills a mutant dropping the `0 <=` future guard.
        self._ready_ws(last_decision_at=timezone.now() + timezone.timedelta(seconds=120))
        self.assertEqual(R.PersistentWorkspaceProvider().evaluate(self.acct).reason_code,
                         R.RW_OBSERVATION_STALE)

    def test_none_decision_timestamp_is_stale(self):
        # No decision timestamp ⇒ not fresh (fail-closed). Kills a mutant dropping the `if ts is None` branch.
        self._ready_ws(last_decision_at=None)
        self.assertEqual(R.PersistentWorkspaceProvider().evaluate(self.acct).reason_code,
                         R.RW_OBSERVATION_STALE)

    def test_lifecycle_checks_are_anded(self):
        self._ready_ws()
        self.acct.is_active = False
        self.assertEqual(R.PersistentWorkspaceProvider().evaluate(self.acct).reason_code,
                         g.R_ACCOUNT_INACTIVE)
        self.acct.is_active = True
        self.acct.disconnected_at = timezone.now()
        self.assertEqual(R.PersistentWorkspaceProvider().evaluate(self.acct).reason_code,
                         g.R_ACCOUNT_DISCONNECTED)

    def test_execution_feature_flag_disabled_fails_closed(self):
        # ADR-0034 Decision D condition 2 — the subsystem execution flag must be ON (master may be on for
        # observation while execution stays dark).
        self._ready_ws()
        with override_settings(HOSTED_MT5_EXECUTION_ENABLED="0"):
            self.assertEqual(R.PersistentWorkspaceProvider().evaluate(self.acct).reason_code,
                             R.RW_EXECUTION_FEATURE_DISABLED)

    def test_unarmed_workspace_fails_closed(self):
        # ADR-0034 Decision D condition 4 — the explicit per-workspace arm must be True.
        self._ready_ws(execution_enabled=False)
        self.assertEqual(R.PersistentWorkspaceProvider().evaluate(self.acct).reason_code,
                         R.RW_EXECUTION_DISABLED)

    def test_real_account_hard_rejected_demo_only(self):
        # ADR-0034 Decision D condition 11 — this subsystem is DEMO ONLY; a real account never executes.
        self._ready_ws()
        self.acct.is_demo = False
        self.assertEqual(R.PersistentWorkspaceProvider().evaluate(self.acct).reason_code,
                         R.RW_REAL_ACCOUNT_NOT_ENABLED)

    def test_gate_substitution_end_to_end(self):
        self._ready_ws()
        with _gate_on():
            self.assertTrue(g.evaluate_execution_gate(self.acct).allowed)


class ProviderBDarknessTests(TestCase):
    def test_dark_when_hosted_flag_off(self):
        user = U.objects.create_user(username="rd", email="rd@x.invalid", password="x")
        acct = TradingAccount.objects.create(
            user=user, name="a", broker_name="B", account_number="500", is_demo=True,
            readiness_provider=_RP.PERSISTENT_WORKSPACE)
        HostedMt5Workspace.objects.create(
            trading_account=acct, canonical_state=WorkspaceLifecycleState.EXECUTION_READY,
            proj_connected=True, proj_trade_allowed=True, proj_account_match=True,
            proj_execution_ready=True, last_decision_at=timezone.now())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HOSTED_PERSISTENT_MT5_ENABLED", None)
            self.assertEqual(R.PersistentWorkspaceProvider().evaluate(acct).reason_code,
                             R.RW_SUBSYSTEM_DISABLED)
