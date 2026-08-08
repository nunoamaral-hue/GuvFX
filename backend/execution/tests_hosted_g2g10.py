"""ADR-0034 Execution Engine — G2 observation driver + G10 idempotency/ambiguous-result (+ mutation)."""
from __future__ import annotations

import inspect
import textwrap

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from trading.models import TradingAccount

from execution import hosted_idempotency as ID
from hosted_workspace import observation_runner as RUN
from hosted_workspace.manager import WorkspaceObservation
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S, WorkspaceReason

U = get_user_model()


def _healthy_obs():
    return WorkspaceObservation(process_running=True, ipc_available=True, connected=True, account_match=True,
                                trade_allowed=True, fresh=True, previous_state=str(S.CONNECTED),
                                previous_reason=str(WorkspaceReason.NONE), observed_at=None)


class ObservationRunnerTests(TestCase):
    def _ws(self):
        user = U.objects.create_user(username="g2", email="g2@x.invalid", password="x")
        acct = TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number="1",
                                             is_demo=True)
        ws = HostedMt5Workspace.objects.create(trading_account=acct)
        ws.canonical_state = str(S.CONNECTED)
        ws.save(update_fields=["canonical_state", "updated_at"])
        return ws

    def test_dark_is_noop(self):
        self._ws()
        summary = RUN.run_hosted_observations(observe_fn=lambda ws: _healthy_obs())
        self.assertEqual(summary["enabled"], False)
        self.assertEqual(summary["polled"], 0)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_enabled_applies_and_advances_state(self):
        ws = self._ws()
        summary = RUN.run_hosted_observations(observe_fn=lambda w: _healthy_obs())
        self.assertEqual(summary["polled"], 1)
        self.assertEqual(summary["applied"], 1)
        ws.refresh_from_db()
        self.assertEqual(ws.canonical_state, str(S.EXECUTION_READY))  # CONNECTED -> EXECUTION_READY persisted

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_unavailable_observation_is_failclosed_not_ingested(self):
        ws = self._ws()
        summary = RUN.run_hosted_observations(observe_fn=lambda w: None)  # host unavailable this cycle
        self.assertEqual(summary["unavailable"], 1)
        self.assertEqual(summary["applied"], 0)
        ws.refresh_from_db()
        self.assertEqual(ws.canonical_state, str(S.CONNECTED))  # unchanged; freshness lapses naturally

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_one_workspace_error_does_not_stop_the_cycle(self):
        self._ws()
        def _boom(w):
            raise RuntimeError("observe failed")
        summary = RUN.run_hosted_observations(observe_fn=_boom)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["polled"], 1)  # cycle completed despite the error


class IdempotencyKeyTests(TestCase):
    def _k(self, **over):
        base = dict(workspace_uuid="ws1", expected_login="700900", expected_server="Srv", job_id="42",
                    operation="PLACE_ORDER", strategy_id="s1")
        base.update(over)
        return ID.hosted_idempotency_key(**base)

    def test_deterministic(self):
        self.assertEqual(self._k(), self._k())

    def test_every_component_changes_the_key(self):
        base = self._k()
        for field, val in [("workspace_uuid", "ws2"), ("expected_login", "999"), ("expected_server", "S2"),
                           ("job_id", "43"), ("operation", "CLOSE_TRADE"), ("strategy_id", "s2")]:
            self.assertNotEqual(base, self._k(**{field: val}), field)  # no cross-context collision

    def test_secret_free_key(self):
        k = self._k(expected_login="SECRETLOGIN")
        self.assertNotIn("SECRETLOGIN", k)  # login is hashed, never in the key plaintext
        self.assertTrue(k.startswith("HWX-"))


class AmbiguousResultTests(TestCase):
    def test_evidence_means_executed(self):
        for ev in ("order_found", "position_found", "deal_found"):
            kw = dict(reconciliation_authoritative=False, order_found=False, position_found=False,
                      deal_found=False)
            kw[ev] = True
            self.assertEqual(ID.classify_ambiguous_result(**kw), ID.CONFIRMED_EXECUTED, ev)

    def test_authoritative_no_evidence_is_not_executed(self):
        self.assertEqual(ID.classify_ambiguous_result(reconciliation_authoritative=True, order_found=False,
                         position_found=False, deal_found=False), ID.CONFIRMED_NOT_EXECUTED)

    def test_non_authoritative_no_evidence_stays_ambiguous_failclosed(self):
        self.assertEqual(ID.classify_ambiguous_result(reconciliation_authoritative=False, order_found=False,
                         position_found=False, deal_found=False), ID.STILL_AMBIGUOUS)

    def test_only_not_executed_may_retry(self):
        self.assertTrue(ID.may_retry_after_ambiguous(ID.CONFIRMED_NOT_EXECUTED))
        self.assertFalse(ID.may_retry_after_ambiguous(ID.CONFIRMED_EXECUTED))
        self.assertFalse(ID.may_retry_after_ambiguous(ID.STILL_AMBIGUOUS))

    def test_classifier_mutants_are_killed(self):
        # Mutating the fail-closed default (STILL_AMBIGUOUS -> CONFIRMED_NOT_EXECUTED) must be caught by the
        # non-authoritative oracle: a mutant would wrongly permit a retry after an unproven send.
        src = textwrap.dedent(inspect.getsource(ID.classify_ambiguous_result))
        mutant_src = src.replace("return STILL_AMBIGUOUS", "return CONFIRMED_NOT_EXECUTED", 1)
        ns = {"CONFIRMED_EXECUTED": ID.CONFIRMED_EXECUTED, "CONFIRMED_NOT_EXECUTED": ID.CONFIRMED_NOT_EXECUTED,
              "STILL_AMBIGUOUS": ID.STILL_AMBIGUOUS}
        exec(compile(mutant_src, "<mutant>", "exec"), ns)
        mutant = ns["classify_ambiguous_result"]
        args = dict(reconciliation_authoritative=False, order_found=False, position_found=False,
                    deal_found=False)
        self.assertNotEqual(mutant(**args), ID.classify_ambiguous_result(**args))  # killed
