"""ADR-0034 Execution Engine — Hosted Workspace routing + arming (Decisions C & D) + structural no-bypass.

Proves owner-bound single-workspace routing (Decision C), the layered arming gate (Decision D), demo-only
enforcement, secret-free reason codes, and that the per-job identity pin covers every mutation job type so a
new mutation site cannot be added without the execution guard.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from trading.models import BrokerServer, TradingAccount

from execution import hosted_routing as HR
from execution.hosted_routing import hosted_execution_armed, resolve_hosted_route
from execution.models import ExecutionJob, IDENTITY_PIN_JOB_TYPES
from execution.readiness import PERSISTENT_WORKSPACE, TEMPORARY_VALIDATION
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()


def _armed_account(*, provider=PERSISTENT_WORKSPACE, login="700900", server="IS6-Demo", is_demo=True,
                   with_ws=True, execution_enabled=True):
    user = U.objects.create_user(username=f"u{login}{provider}{is_demo}", email=f"{login}{provider}@x.invalid",
                                 password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name=server)
    acct = TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number=login,
                                         is_demo=is_demo, broker_server=srv, readiness_provider=provider)
    if with_ws:
        HostedMt5Workspace.objects.create(
            trading_account=acct, canonical_state=S.EXECUTION_READY, proj_connected=True,
            proj_trade_allowed=True, proj_account_match=True, proj_execution_ready=True,
            last_decision_at=timezone.now(), execution_enabled=execution_enabled)
    return acct


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class RouteResolutionTests(TestCase):
    def test_armed_route_resolves_owner_bound_identity(self):
        acct = _armed_account(login="700900", server="IS6-Demo")
        r = resolve_hosted_route(acct)
        self.assertTrue(r.ok, r.reason_code)
        self.assertEqual(r.reason_code, HR.ER_ROUTE_OK)
        self.assertEqual(r.expected_login, "700900")     # server-derived, never client-supplied
        self.assertEqual(r.expected_server, "IS6-Demo")

    def test_missing_workspace_fails_closed(self):
        acct = _armed_account(with_ws=False)
        self.assertEqual(resolve_hosted_route(acct).reason_code, HR.ER_WORKSPACE_NOT_FOUND)

    def test_unarmed_workspace_not_routed(self):
        acct = _armed_account(execution_enabled=False)
        r = resolve_hosted_route(acct)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, HR.ER_NOT_ARMED)
        self.assertEqual(r.expected_login, "")  # no identity leaked from an unarmed route

    def test_real_account_not_routed(self):
        acct = _armed_account(is_demo=False)
        self.assertEqual(resolve_hosted_route(acct).reason_code, HR.ER_NOT_ARMED)

    def test_provider_a_not_routed(self):
        acct = _armed_account(provider=TEMPORARY_VALIDATION)
        self.assertEqual(resolve_hosted_route(acct).reason_code, HR.ER_NOT_ARMED)

    def test_none_account_fails_closed(self):
        self.assertEqual(resolve_hosted_route(None).reason_code, HR.ER_ACCOUNT_MISSING)

    def test_cross_owner_workspace_hard_rejected(self):
        # Adversarial (Decision C): a workspace whose bound account is NOT this account must fail closed,
        # even though the OneToOne normally makes that impossible — defence-in-depth against a mis-set FK.
        class _Ws:
            trading_account_id = 999
            workspace_uuid = "u"
        class _Acct:
            pk = 1
            user_id = 10
            hosted_workspace = _Ws()
            readiness_provider = PERSISTENT_WORKSPACE
            is_demo = True
            account_number = "1"
            broker_server_id = None
        self.assertEqual(resolve_hosted_route(_Acct()).reason_code, HR.ER_WORKSPACE_OWNER_MISMATCH)

    def test_route_output_is_secret_free(self):
        acct = _armed_account(login="SECRETLOGIN", server="SRV")
        blob = str(resolve_hosted_route(acct).as_dict())
        for forbidden in ("password", "token", "secret", "accounts_dat", "keyring"):
            self.assertNotIn(forbidden, blob)


class ArmingTests(TestCase):
    def test_dark_never_armed(self):
        acct = _armed_account()  # flags OFF (no override) → dark
        self.assertFalse(hosted_execution_armed(acct))

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
    def test_fully_armed_true(self):
        self.assertTrue(hosted_execution_armed(_armed_account()))

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1")  # execution flag OFF
    def test_execution_flag_off_not_armed(self):
        self.assertFalse(hosted_execution_armed(_armed_account()))

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
    def test_provider_a_not_armed(self):
        self.assertFalse(hosted_execution_armed(_armed_account(provider=TEMPORARY_VALIDATION)))


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class ClaimEntitlementTests(TestCase):
    """G4 claim-seam entitlement (Decision C) — a hosted mutation job may be claimed only via a non-NULL
    owner-bound route by a node-aware (non-legacy/non-shared) worker."""

    class _Job:
        def __init__(self, account, terminal_node_id, job_type):
            self.account = account
            self.terminal_node_id = terminal_node_id
            self.job_type = job_type

    def _job(self, account, node_id=7):
        return self._Job(account, node_id, ExecutionJob.JobType.PLACE_ORDER)

    def test_armed_hosted_job_node_aware_worker_ok(self):
        acct = _armed_account()
        self.assertTrue(HR.authorize_hosted_claim(self._job(acct), worker_is_node_aware=True).ok)

    def test_null_route_hosted_job_rejected(self):
        acct = _armed_account()
        d = HR.authorize_hosted_claim(self._job(acct, node_id=None), worker_is_node_aware=True)
        self.assertFalse(d.ok)
        self.assertEqual(d.reason_code, HR.ER_ROUTE_MISSING)  # no shared/NULL route for a hosted job

    def test_legacy_worker_cannot_claim_hosted_job(self):
        acct = _armed_account()
        d = HR.authorize_hosted_claim(self._job(acct), worker_is_node_aware=False)
        self.assertFalse(d.ok)
        self.assertEqual(d.reason_code, HR.ER_WORKER_NOT_ENTITLED)  # no shared/legacy-worker entitlement

    def test_unarmed_hosted_job_rejected_at_claim(self):
        acct = _armed_account(execution_enabled=False)
        self.assertEqual(HR.authorize_hosted_claim(self._job(acct), worker_is_node_aware=True).reason_code,
                         HR.ER_NOT_ARMED)

    def test_non_hosted_job_passes_through(self):
        acct = _armed_account(provider=TEMPORARY_VALIDATION)
        # Provider A / non-hosted → unchanged behaviour (ER_ROUTE_OK), even from a legacy worker.
        self.assertTrue(HR.authorize_hosted_claim(self._job(acct), worker_is_node_aware=False).ok)


class ClaimDarkOverheadTests(TestCase):
    """The claim authorizer must add ZERO queries while the subsystem is dark (flag checked before any
    account access) — the regression the subsystem review flagged."""

    def test_dark_claim_issues_no_query(self):
        acct = _armed_account(provider=PERSISTENT_WORKSPACE)  # a Provider-B-shaped account, but dark (no flag)
        job = ExecutionJob.objects.create(job_type=ExecutionJob.JobType.CLOSE_TRADE, account=acct,
                                          payload={"ticket": 1})
        job = ExecutionJob.objects.get(pk=job.pk)  # fresh ⇒ job.account is a LAZY FK (uncached)
        with self.assertNumQueries(0):              # dark short-circuit must never dereference job.account
            d = HR.authorize_hosted_claim(job, worker_is_node_aware=True)
        self.assertTrue(d.ok)  # pass-through while dark


class MutationSurfaceStructuralTests(TestCase):
    """PART 6/19 — a machine-checkable guard so a new MT5 mutation job type cannot be added without also
    being pinned. If someone adds a new order/close/modify JobType, this test forces them to decide whether
    it carries the per-job identity pin (or to consciously exempt it here)."""

    def test_identity_pin_covers_every_mutation_job_type(self):
        JT = ExecutionJob.JobType
        # Every job type that reaches a real MT5 mutation MUST be in IDENTITY_PIN_JOB_TYPES.
        expected_mutation_types = {JT.OPEN_TRADE, JT.PLACE_ORDER, JT.PLACE_TEST_ORDER, JT.CLOSE_TRADE,
                                   JT.MODIFY_POSITION}
        self.assertEqual(set(IDENTITY_PIN_JOB_TYPES), expected_mutation_types)
        # Deliberately-exempt (non-mutation / no-broker) types — documented so the exemption is conscious.
        exempt = {JT.TEST_CONNECTION, JT.SYNC_POSITIONS, JT.PLACE_ORDER_SHADOW}
        all_types = {jt for jt in JT}
        uncategorised = all_types - set(IDENTITY_PIN_JOB_TYPES) - exempt
        self.assertEqual(uncategorised, set(),
                         f"new JobType(s) {uncategorised} must be classified as pinned-mutation or exempt")
