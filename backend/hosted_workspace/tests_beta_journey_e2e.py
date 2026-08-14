"""Beta Provider-B AUTONOMOUS journey — end-to-end acceptance proof + blocker-discovery engine.

Walks a BRAND-NEW beta user through the ENTIRE hosted Provider-B autonomous journey and proves it needs
ZERO engineer intervention, by driving the REAL production function at every one of the ten stages. The
ONLY things faked are the physical host / broker boundary:

  * the Windows slot-prep HOST executor is kept OUT of the loop (``HOSTED_SLOT_PREP_ENABLED`` OFF, stage 3),
    so node allocation advances PROVISIONING -> WAITING_FOR_LOGIN with no host contact (the documented
    "keep the host boundary out" path — see ``tests_slot_preparation.AllocateGateTests``);
  * the per-account session OBSERVER (which runs AS the tenant on the host — precisely the boundary the
    RemoteApp isolation cert governs) is replaced by feeding REAL ``WorkspaceObservation`` objects through
    the certified single writer ``consumer.ingest_observation`` (stage 5). No ``canonical_state`` / ``proj_*``
    field is EVER written directly — the state machine DERIVES ``EXECUTION_READY`` itself;
  * ``MetaTrader5`` (the broker terminal) is faked at the ``order_send`` boundary (stage 10), reusing the
    bridge test harness ``_fake_mt5`` / ``_load_bridge`` from ``execution.tests_bridge_symbols``.

Posture (stage 5/7/8/9 gate): run under ``SUPERVISED_SINGLE_TENANT_BETA_ENABLED`` with the full behavioural
isolation cert ``HOSTED_REMOTEAPP_ISOLATION_CERTIFIED`` deliberately WITHHELD — the REAL beta go-live posture
(ADR-0044): one non-Customer-Zero DEMO tenant, ALONE on a dedicated ACTIVE non-CZ node. This exercises the
REAL ``supervised_beta.supervised_single_tenant_beta_active`` gate rather than trivially short-circuiting the
posture with the (correctly un-set) cert flag. ``customer_zero_account_ids`` is pinned to ``frozenset()`` for
the arm/readiness legs (the canonical CZ set is a hardcoded ``{1}`` and Postgres does not reset the id
sequence between tests, so a test account can otherwise coincidentally land on pk=1 and be — correctly —
CZ-excluded; the same idiom used by ``strategies.tests_arm_containment``).

This is BOTH the acceptance proof AND a blocker-discovery engine: any stage the REAL code cannot advance
without an operator/manual step is a repository Beta Blocker and would fail here loudly rather than be
papered over.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from billing.models import BetaTester
from execution import readiness as R
from execution.hosted_provisioning import ARM_OK
from execution.models import ExecutionControl, ExecutionJob, SignalSourceConfig
from execution.readiness import PERSISTENT_WORKSPACE, evaluate_readiness
from signal_intake import acquisition
from signal_intake.models import AcquiredMessage, ParserProfile, SignalProvider
from strategies.models import StrategyAssignment
from trading.models import TradingAccount

from hosted_workspace import provisioning as P
from hosted_workspace.auto_arm_runner import run_hosted_auto_arm
from hosted_workspace.consumer import ingest_observation
from hosted_workspace.manager import WorkspaceObservation
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

# Import the REAL broker-boundary fakes rather than reinventing them (the same harness the bridge symbol
# tests use). ``_fake_mt5`` is a MetaTrader5 stand-in; ``_load_bridge`` loads the standalone bridge module.
from execution.tests_bridge_symbols import _fake_mt5, _load_bridge
# The WAYOND_TEXT signal body + provision helper are the proven inputs the e3 demo path already exercises.
from execution.tests_e3_demo_promotion import WAYOND_TEXT

User = get_user_model()

ARM_URL = "/api/strategies/strategies/signal-copy/arm/"
MP_010 = "mp-010"                 # marketplace signal-copy template -> signal_source "ti_signals"
SIGNAL_SOURCE = "ti_signals"      # the source mp-010 binds; the stage-10 provider slug must match it
EXPECTED_LOGIN = "770077"         # broker login IDENTIFIER the customer already knows (never a password)
EXPECTED_SERVER = "GuvfxBeta-Demo"

# The full go-live flag set. HOSTED_SLOT_PREP_ENABLED is deliberately OFF (keep the host executor out of the
# loop — stage 3). HOSTED_REMOTEAPP_ISOLATION_CERTIFIED is deliberately OFF (cert WITHHELD): the posture is
# carried by SUPERVISED_SINGLE_TENANT_BETA_ENABLED (ADR-0044), which is the honest beta config and exercises
# the REAL single-tenant gate. HOSTED_MT5_OBSERVATION_ENABLED / HOSTED_OBSERVATION_SCHEDULER_ENABLED are set
# for go-live fidelity but are NOT on this test's path — we feed observations directly through the certified
# writer instead of the live_observe transport, which is the legitimate observer/host-boundary fake.
FLAGS = dict(
    HOSTED_PERSISTENT_MT5_ENABLED=True,          # master subsystem gate (admission, ingest, readiness, arm, pin)
    HOSTED_WORKSPACE_ONBOARDING_ENABLED=True,    # request / confirm admission
    HOSTED_MT5_EXECUTION_ENABLED=True,           # Provider-B execution subsystem (readiness cond 2, arm, auto-arm)
    HOSTED_MT5_OBSERVATION_ENABLED=True,         # go-live set (not on the direct-ingest path)
    HOSTED_OBSERVATION_SCHEDULER_ENABLED=True,   # go-live set (not on the direct-ingest path)
    HOSTED_MT5_REMOTEAPP_ENABLED=True,           # delivery subsystem (stage 4 — see comment, delivery is off-spine)
    SUPERVISED_SINGLE_TENANT_BETA_ENABLED=True,  # ADR-0044 posture (cert WITHHELD) — the real beta go-live gate
    BETA_SELF_SERVE_ARM_ENABLED=True,            # stage 9 self-serve "Enable Trading"
    BETA_ADMISSION_ARM_ENABLED=True,             # stage 9 admission-derived arm authorization (ADR-0045)
    BETA_RUNTIMES_ENABLED=True,                  # harmless; mirrors tests_arm_containment BASE
    BETA_MAX_TESTERS=1000,
    # NOT set: HOSTED_SLOT_PREP_ENABLED, HOSTED_REMOTEAPP_ISOLATION_CERTIFIED,
    #          HOSTED_TENANT_NODE_ISOLATION_ENABLED, MULTI_ACCOUNT_ROUTING_ENABLED (single-tenant path).
)

# Patch target for pinning "this tenant is NOT Customer Zero" (see module docstring).
_CZ_PATCH = "hosted_workspace.tenant_isolation.customer_zero_account_ids"


@override_settings(**FLAGS)
class BetaProviderBAutonomousJourneyTests(TestCase):
    """One coherent, ordered walk of the whole autonomous journey. Helpers below feed observations and
    assert states, but every STAGE calls the REAL production function named in its comment."""

    # ---- helpers -------------------------------------------------------------------------------------

    def _feed_observation(self, ws, *, version, **health):
        """Push ONE real ``WorkspaceObservation`` through the REAL certified single writer
        ``consumer.ingest_observation``. ``previous_state``/``previous_reason`` are IGNORED by the consumer
        (it overrides them with the stored canonical premise), so we pass placeholders. Returns the fresh
        ``PersistResult``. This is the ONLY way canonical state moves — the state machine derives it."""
        obs = WorkspaceObservation(
            process_running=health["process_running"],
            ipc_available=health["ipc_available"],
            connected=health["connected"],
            account_match=health["account_match"],
            trade_allowed=health["trade_allowed"],
            fresh=health["fresh"],
            previous_state=str(S.PROVISIONING),   # placeholder — overridden by the consumer
            observed_at=None,
        )
        return ingest_observation(ws, obs, observation_version=version,
                                  correlation_id=f"beta-e2e-v{version}")

    def _fresh(self, ws):
        ws.refresh_from_db()
        return ws

    def _matching_terminal_mt5(self, *, login, server):
        """A ``MetaTrader5`` stand-in representing the CUSTOMER'S terminal correctly logged into THEIR OWN
        demo broker account — the connected + matched + trade-allowed state stages 5-6 established. Built on
        the shared ``_fake_mt5`` harness (broker boundary) with ``account_info``/``terminal_info`` set so the
        bridge's REAL per-job identity-pin binding gate (``verify_execution_binding``) is genuinely enforced
        AND satisfied (login/server/demo/connected/trade_allowed all agree with the server-injected pin)."""
        mt5 = _fake_mt5()  # EURUSD available+visible, order_check ok, order_send returns DONE
        acct = mock.MagicMock()
        acct.trade_mode = 0                    # 0 = DEMO
        acct.login = str(login)                # matches the server-derived expected_login (identifier)
        acct.server = str(server)              # matches the server-derived expected_server (identifier)
        mt5.account_info.return_value = acct
        term = mock.MagicMock()
        term.connected = True
        term.trade_allowed = True
        mt5.terminal_info.return_value = term
        return mt5

    # ---- the journey ---------------------------------------------------------------------------------

    def test_full_autonomous_hosted_journey(self):
        # ================================================================================================
        # STAGE 1 — Register + beta admission.
        # REAL: a User row + billing.models.BetaTester (the real admission allowlist). No commercial
        # subscription is created: hosted capability is decoupled from the plan and granted purely by ACTIVE
        # BetaTester membership (ADR-0034 amendment) — proving the beta allowlist is the load-bearing source.
        # ================================================================================================
        user = User.objects.create_user(
            username="beta-e2e", email="beta.e2e@example.invalid", password="x")
        BetaTester.objects.create(email=user.email, is_active=True)

        with mock.patch(_CZ_PATCH, return_value=frozenset()):
            self._run_journey(user)

    def _run_journey(self, user):
        # ================================================================================================
        # STAGE 2 — Hosted workspace onboarding.
        # REAL: provisioning.request_hosted_workspace — creates the INTENT-ONLY TradingAccount
        # (is_active=False, readiness_provider=persistent_workspace, NO password) + the HostedMt5Workspace
        # at PROVISIONING. We hand-create NOTHING.
        # ================================================================================================
        req = P.request_hosted_workspace(
            user, expected_login=EXPECTED_LOGIN, expected_server=EXPECTED_SERVER,
            broker_name="GuvFX Beta", is_demo=True)
        self.assertTrue(req.ok, req.reason)
        self.assertEqual(req.reason, P.REQ_CREATED)
        ws = req.workspace
        account = ws.trading_account
        self.assertFalse(account.is_active)                            # intent only
        self.assertEqual(account.readiness_provider, PERSISTENT_WORKSPACE)
        self.assertEqual((account.password_enc or ""), "")             # product invariant: no broker password
        self.assertEqual(str(self._fresh(ws).canonical_state), S.PROVISIONING)

        # ================================================================================================
        # STAGE 3 — Node allocation.
        # REAL: provisioning.allocate_workspace_node — binds an ACTIVE, deliverable (durable rdp_host),
        # non-forbidden TerminalNode and advances PROVISIONING -> WAITING_FOR_LOGIN through the certified
        # single writer. HOSTED_SLOT_PREP_ENABLED is OFF, so allocation advances straight to
        # WAITING_FOR_LOGIN with NO host contact (documented choice — keeps the host executor boundary out).
        # ================================================================================================
        from execution.models import TerminalNode
        node = TerminalNode.objects.create(
            hostname="beta-node-1", status=TerminalNode.Status.ACTIVE, rdp_host="10.60.0.9",
            max_accounts=1)
        alloc = P.allocate_workspace_node(ws)
        self.assertTrue(alloc.ok, alloc.reason)
        self.assertEqual(alloc.reason, P.ALLOC_OK)
        ws = self._fresh(ws)
        self.assertEqual(ws.execution_node_id, node.pk)
        self.assertEqual(ws.workspace_node_id, node.pk)                # delivery host = same node
        self.assertEqual(ws.trading_account.terminal_node_id, node.pk)  # route-agreement invariant
        self.assertEqual(str(ws.canonical_state), S.WAITING_FOR_LOGIN)  # driven via the single writer
        self.assertEqual(int(ws.observation_version), 1)

        # ================================================================================================
        # STAGE 4 — RemoteApp delivery (SKIPPED, off the execution spine).
        # delivery.authorize_workspace_delivery requires a MATERIALISED Windows identity (AccountProvisioning
        # with a credential) + runtime + Guacamole config — all produced ONLY by the host slot-prep engine
        # (the host boundary we deliberately keep out at stage 3). It is not on the execution spine, so we do
        # not assert it here (it would fail closed with IDENTITY_MISSING absent host prep, which is expected).
        # ================================================================================================

        # ================================================================================================
        # STAGE 5 — THE CRUX: drive WAITING_FOR_LOGIN -> CONNECTED -> EXECUTION_READY purely by feeding REAL
        # observations through the REAL certified writer. The §3 graph forbids WAITING_FOR_LOGIN ->
        # EXECUTION_READY directly, so the first observation must NOT satisfy the full execution conjunction
        # (trade_allowed=False) — it lands CONNECTED; the second (fully healthy) lands EXECUTION_READY. We set
        # NO canonical_state / proj_* by hand; the manager derives everything.
        # ================================================================================================
        # obs v2: attached + connected + matched + fresh, but trading halted -> CONNECTED (legal from
        # WAITING_FOR_LOGIN). observation_version must exceed the 1 that allocation stamped.
        r2 = self._feed_observation(
            ws, version=2, process_running=True, ipc_available=True, connected=True,
            account_match=True, trade_allowed=False, fresh=True)
        self.assertIsNotNone(r2)                                       # not DARK — master flag is on
        ws = self._fresh(ws)
        self.assertEqual(str(ws.canonical_state), S.CONNECTED)         # DERIVED, not set
        self.assertIs(ws.proj_account_match, True)
        self.assertIs(ws.proj_connected, True)
        self.assertFalse(ws.canonical_execution_ready)

        # obs v3: the FULL execution conjunction -> EXECUTION_READY (legal from CONNECTED).
        self._feed_observation(
            ws, version=3, process_running=True, ipc_available=True, connected=True,
            account_match=True, trade_allowed=True, fresh=True)
        ws = self._fresh(ws)
        self.assertEqual(str(ws.canonical_state), S.EXECUTION_READY)   # DERIVED by the state machine
        self.assertTrue(ws.canonical_execution_ready)
        self.assertIs(ws.proj_trade_allowed, True)

        # ================================================================================================
        # STAGE 6 — Customer confirmation (the human ACK).
        # REAL: provisioning.confirm_broker_account — owner-scoped, gated on an OBSERVED active-account match
        # on a CONNECTED/EXECUTION_READY workspace. Stamps workspace_confirmed_at AND activates the account
        # (is_active=True) — the customer-specific activation the autonomous journey performs.
        # ================================================================================================
        conf = P.confirm_broker_account(user, ws)
        self.assertTrue(conf.ok, conf.reason)
        self.assertEqual(conf.reason, P.CONFIRM_OK)
        account.refresh_from_db()
        self.assertIsNotNone(account.workspace_confirmed_at)
        self.assertTrue(account.is_active)                            # activated by the confirm ACK

        # ================================================================================================
        # STAGE 7 — Autonomous arming.
        # REAL: auto_arm_runner.run_hosted_auto_arm — arms execution_enabled on the now-EXECUTION_READY
        # workspace by calling the SAME certified arm action the operator CLI called (re-proving EVERY
        # precondition). Removes the last per-customer operator step (ADR-0044 Decision 2).
        # ================================================================================================
        summary = run_hosted_auto_arm()
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["armed"], 1, summary)                # our workspace armed autonomously
        self.assertEqual(summary["refused"], 0, summary)
        ws = self._fresh(ws)
        self.assertTrue(ws.execution_enabled)                        # the durable arm boolean flipped

        # ================================================================================================
        # STAGE 8 — Provider-B readiness.
        # REAL: execution.readiness.evaluate_readiness — must now be ELIGIBLE organically (proving stages
        # 5-7 connected: connected + matched + confirmed + armed + trade_allowed + EXECUTION_READY + fresh +
        # supervised posture single-tenant). NOT synthesized — every field was set by the real writers above.
        # ================================================================================================
        decision = evaluate_readiness(account)
        self.assertTrue(decision.eligible, decision.reason_code)
        self.assertEqual(decision.provider, PERSISTENT_WORKSPACE)

        # ================================================================================================
        # STAGE 9 — Self-serve arm (the REAL authenticated API).
        # REAL: POST /api/strategies/strategies/signal-copy/arm/ as the beta user. BETA_SELF_SERVE_ARM_ENABLED
        # + BETA_ADMISSION_ARM_ENABLED authorize the admitted (non-CZ) BetaTester; the Provider-B branch of
        # _account_execution_ready delegates to evaluate_readiness (which passed at stage 8). Creates the
        # AUTO_DEMO / stage=LIVE / active signal-copy assignment bound to ti_signals.
        # ================================================================================================
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(
            ARM_URL, {"marketplace_strategy_id": MP_010, "account_id": account.id}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "armed")
        assignment = StrategyAssignment.objects.get(
            account=account, execution_mode="AUTO_DEMO", stage="LIVE", is_active=True)
        self.assertEqual(assignment.signal_source, SIGNAL_SOURCE)

        # ================================================================================================
        # STAGE 10 — Signal -> order.
        # REAL (in-process pipeline, the furthest the e3 path reaches): a live signal on the ti_signals
        # source is acquired -> auto-routed -> planned -> promoted to PLACE_ORDER ExecutionJob(s) FOR THE
        # BETA ACCOUNT. Then the REAL bridge order function execute_demo_order is driven to mt5.order_send.
        #
        # FAKED (host/broker boundary ONLY): (a) MetaTrader5 — the customer's terminal, faked so the REAL
        # per-job identity-pin binding gate is enforced AND satisfied; (b) the worker->agent HTTP hop — we
        # call the REAL /mt5/order handler (execute_demo_order) in-process, mapping the job payload exactly as
        # mt5_trade_ingest_worker.main() builds its agent payload. The parser is wayond_v1 over WAYOND_TEXT
        # (the proven e3 body); the provider SLUG is "ti_signals" so approval.source routes to the stage-9
        # assignment (source and parser are independent — slug drives routing, parser_profile drives parsing).
        # ================================================================================================
        call_command("provision_auto_shadow")                        # the guvfx-auto-system reviewer (real)
        ctrl = ExecutionControl.get_solo()
        ctrl.auto_execution_enabled = True
        ctrl.signal_execution_mode = ExecutionControl.SignalExecutionMode.DEMO
        ctrl.kill_switch_engaged = False
        ctrl.save()
        parser = ParserProfile.objects.create(
            slug="wayond_v1", certification_level=ParserProfile.CertificationLevel.MEDIUM)
        provider = SignalProvider.objects.create(
            slug=SIGNAL_SOURCE, name="TI Signals", telegram_chat_id="-100999",
            parser_profile=parser, status=SignalProvider.Status.ARMED)
        SignalSourceConfig.objects.create(
            source=SIGNAL_SOURCE, auto_demo_execution_enabled=True, total_lot_target=Decimal("0.03"))

        acq = acquisition.acquire_message(provider, {
            "message_id": "beta-sig-1", "chat_id": "-100999",
            "date": timezone.now(), "text": WAYOND_TEXT,
        })
        self.assertEqual(acq.outcome, AcquiredMessage.Outcome.INTAKEN, acq.reason)

        # The auto-router fired synchronously on acquisition -> real PLACE_ORDER jobs for the beta account.
        jobs = list(ExecutionJob.objects.filter(
            account=account, job_type=ExecutionJob.JobType.PLACE_ORDER))
        self.assertTrue(jobs, "the real pipeline must create PLACE_ORDER job(s) for the beta account")
        job = jobs[0]
        payload = job.payload
        self.assertEqual(payload.get("execution_mode"), "DEMO")
        # The backend injected the Provider-B server-derived per-job identity pin at ExecutionJob.save()
        # (ADR-0034 Execution Engine G3) — the distinguishing hosted-execution control.
        self.assertIs(payload.get("require_identity_pin"), True)
        self.assertEqual(payload.get("expected_login"), EXPECTED_LOGIN)
        self.assertEqual(payload.get("expected_server"), EXPECTED_SERVER)

        # Build the agent order payload and forward the identity pin through the REAL worker helper
        # apply_identity_pin (the same function the dispatcher calls) — NOT a hand-rolled copy. NOTE: this
        # exercises the helper + asserts the transport carries the pin; that main()'s dispatch loop actually
        # CALLS the helper on every hosted path is guarded structurally by
        # execution.tests_worker_identity_pin.WorkerIdentityPinWiringTests (main() is a monolithic loop with
        # no callable per-job unit, so it cannot be driven end-to-end here).
        from mt5_trade_ingest_worker import apply_identity_pin
        agent_payload = {
            "symbol": payload["symbol"], "side": payload["side"], "lots": payload["lots"],
            "magic": payload.get("magic", 0), "comment": payload["comment"],
        }
        if payload.get("sl_price") is not None:
            agent_payload["sl"] = float(payload["sl_price"])
        if payload.get("tp_price") is not None:
            agent_payload["tp"] = float(payload["tp_price"])
        if payload.get("provider_symbol"):
            agent_payload["provider_symbol"] = payload["provider_symbol"]
        apply_identity_pin(agent_payload, payload)   # the REAL worker transport forwards the pin
        # GUARD the transport actually carries the pin — a hosted bridge (MT5_REQUIRE_IDENTITY_PIN=1) fails
        # the order closed without these, so this assertion is what makes the fix load-bearing here (if
        # apply_identity_pin regressed to a no-op the journey would break at the first live hosted trade).
        self.assertIs(agent_payload.get("require_identity_pin"), True)
        self.assertEqual(agent_payload.get("expected_login"), EXPECTED_LOGIN)
        self.assertEqual(agent_payload.get("expected_server"), EXPECTED_SERVER)

        bridge = _load_bridge()
        mt5 = self._matching_terminal_mt5(login=EXPECTED_LOGIN, server=EXPECTED_SERVER)
        sys.modules["MetaTrader5"] = mt5
        self.addCleanup(lambda: sys.modules.pop("MetaTrader5", None))
        result = bridge.execute_demo_order(agent_payload)

        # The REAL bridge reached order_send through the enforced (and satisfied) identity-pin binding gate.
        self.assertTrue(result.get("ok"), result)
        mt5.order_send.assert_called_once()
        self.assertEqual(mt5.order_send.call_args.args[0]["symbol"], payload["symbol"])
