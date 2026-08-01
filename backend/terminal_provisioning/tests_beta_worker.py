"""CVM-Inc-3 B1 — beta ProvisioningJob worker + versioned-contract negotiation tests."""
from contextlib import contextmanager

from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model

from billing.models import BetaTester
from trading.models import TradingAccount
from trading.crypto import encrypt_password
from terminal_provisioning import beta_capacity as cap
from terminal_provisioning import beta_worker
from terminal_provisioning import provisioner as prov_mod
from terminal_provisioning.mgmt_client import (
    AgentWindowsProvisioner, ManagementChannelError, ManagementChannelTimeout)
from terminal_provisioning.models import AccountRuntime, ProvisioningJob, RuntimeState
from terminal_provisioning.provisioner import MAX_ATTEMPTS
from terminal_provisioning.tests_mgmt_channel import KEYRING, _agent

U = get_user_model()


@contextmanager
def _fake_reconcile_clock():
    """Deterministic in-attempt reconcile time: ``sleep`` advances a fake monotonic clock the loop reads, so
    the wall-clock budget is consumed with NO real sleeping and NO dependence on the machine clock."""
    state = {"t": 0.0}
    saved_now, saved_sleep = prov_mod._reconcile_now, prov_mod._reconcile_sleep
    prov_mod._reconcile_now = lambda: state["t"]
    prov_mod._reconcile_sleep = lambda s: state.__setitem__("t", state["t"] + s)
    try:
        yield
    finally:
        prov_mod._reconcile_now, prov_mod._reconcile_sleep = saved_now, saved_sleep


def _denied(reason_code):
    return {"outcome": "denied", "reason_code": reason_code}


def _admitted_account(n=1):
    email = f"w{n}@example.invalid"
    BetaTester.objects.create(email=email)
    u = U.objects.create_user(username=f"w{n}", email=email, password="x")
    return TradingAccount.objects.create(
        user=u, name=f"A{n}", account_number=str(900000 + n), broker_name="DemoBroker",
        is_demo=True, password_enc=encrypt_password("pw"))


def _good_agent():
    a = _agent({
        "MATERIALISE": lambda **k: {"duration_ms": 3},
        "START": lambda **k: {"pid": 13020, "session_id": 1},
        "VERIFY": lambda **k: {"running": True, "logged_in": False, "pid": 13020, "session_id": 1},
        "STOP": lambda **k: {}, "TOMBSTONE": lambda **k: {},
    })
    a.manifest_version = "manifest-1"
    a.now_fn = lambda: int(__import__("time").time())
    return a


def _factory(transport):
    return lambda job: AgentWindowsProvisioner(job_id=job.id, transport=transport,
                                               keyring=KEYRING, key_id="k1")


@override_settings(BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000)
class BetaWorkerTests(TestCase):
    def test_claim_ignores_production_jobs(self):
        pacct = _admitted_account(1)
        prod = AccountRuntime.objects.create(trading_account=pacct,
                                             cohort=AccountRuntime.Cohort.PRODUCTION)
        ProvisioningJob.objects.create(runtime=prod, op=ProvisioningJob.Op.PROVISION)
        self.assertIsNone(beta_worker.claim_next_beta_job())   # production job never claimed

    def test_process_one_negotiates_then_advances_to_running(self):
        acct = _admitted_account(2)
        rt = cap.reserve_beta_slot(acct)
        ProvisioningJob.objects.create(runtime=rt, op=ProvisioningJob.Op.PROVISION)
        agent = _good_agent()
        status = beta_worker.process_one(_factory(lambda b, r: agent.handle(r)))
        self.assertEqual(status, "advanced")
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.RUNNING)

    def test_process_one_disabled_is_noop(self):
        with override_settings(BETA_RUNTIMES_ENABLED=False):
            self.assertEqual(beta_worker.process_one(), "disabled")

    def test_negotiation_protocol_mismatch_blocks_advance(self):
        acct = _admitted_account(3)
        rt = cap.reserve_beta_slot(acct)
        ProvisioningJob.objects.create(runtime=rt, op=ProvisioningJob.Op.PROVISION)
        agent = _good_agent()

        def transport(base, req):
            # forge a NEGOTIATE reply advertising a DIFFERENT protocol version → must block
            if req.get("operation") == "NEGOTIATE":
                return {"outcome": "ok", "operation": "NEGOTIATE", "protocol_version": 999,
                        "agent_version": "a", "manifest_version": "m",
                        "supported_operations": ["MATERIALISE", "START", "VERIFY", "STOP", "TOMBSTONE"]}
            return agent.handle(req)

        status = beta_worker.process_one(_factory(transport))
        self.assertEqual(status, "negotiation_failed")
        rt.refresh_from_db()
        self.assertNotEqual(rt.state, RuntimeState.RUNNING)   # never launched on an unnegotiated contract
        self.assertEqual(ProvisioningJob.objects.get(runtime=rt).status, ProvisioningJob.Status.QUEUED)

    def test_runtime_busy_during_copy_is_reconciled_then_reaches_running(self):
        # THE Customer Zero regression. Previously: MATERIALISE times out at 20s while the copy runs, the
        # driver blind-re-POSTs, the agent (lock held) replies runtime_busy, that was mis-classified as
        # materialise_failed and burned MAX_ATTEMPTS in ~0.3s -> false FAILED though the copy completed.
        # Now: the timeout + runtime_busy are reconciled IN-attempt (poll-not-repost) and the runtime
        # reaches RUNNING on the agent's eventual stored result.
        acct = _admitted_account(5)
        rt = cap.reserve_beta_slot(acct)
        ProvisioningJob.objects.create(runtime=rt, op=ProvisioningJob.Op.PROVISION)
        agent = _good_agent()
        calls = {"n": 0}

        def transport(base, req):
            if req.get("operation") == "MATERIALISE":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ManagementChannelTimeout()          # first POST times out (copy > read budget)
                if calls["n"] in (2, 3):
                    return _denied("runtime_busy")            # agent still copying under the per-runtime lock
                return agent.handle(req)                       # copy completed -> stored idempotent ok
            return agent.handle(req)                           # NEGOTIATE/START/VERIFY

        with _fake_reconcile_clock():
            status = beta_worker.process_one(_factory(transport))
        self.assertEqual(status, "advanced")
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.RUNNING)       # RECOVERED — not the incident's false FAILED
        self.assertFalse(rt.quarantined)
        job = ProvisioningJob.objects.get(runtime=rt)
        self.assertEqual(job.status, ProvisioningJob.Status.DONE)
        self.assertEqual(job.attempt, 1)                        # reconcile burned NO extra attempts
        self.assertGreaterEqual(calls["n"], 4)                  # timeout + 2x busy + 1 ok

    def test_reconcile_budget_exhaustion_quarantines_without_relaunch(self):
        # Genuine unresolvable ambiguity: MATERIALISE never confirms within the wall-clock budget. Bounded
        # reconcile -> quarantine (never a "safe to re-launch" FAILED, never a 0.3s three-attempt burn).
        acct = _admitted_account(4)
        rt = cap.reserve_beta_slot(acct)
        ProvisioningJob.objects.create(runtime=rt, op=ProvisioningJob.Op.PROVISION)
        agent = _good_agent()

        def transport(base, req):
            if req.get("operation") == "MATERIALISE":
                raise ManagementChannelTimeout()               # never confirms
            return agent.handle(req)

        with _fake_reconcile_clock():
            beta_worker.process_one(_factory(transport))
        rt.refresh_from_db()
        job = ProvisioningJob.objects.get(runtime=rt)
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)
        self.assertTrue(rt.quarantined)                          # quarantined, never re-launched
        self.assertEqual(rt.quarantine_reason, "ambiguous_timeout")
        self.assertNotEqual(rt.state, RuntimeState.FAILED)       # state left as-is (a terminal MAY be up)
        self.assertEqual(job.attempt, 1)                         # ONE attempt reconciled — not a 3-attempt burn

    def test_proven_partial_materialise_is_fail_closed_and_quarantined(self):
        # A proven-partial / integrity refusal from the agent must fail CLOSED and quarantine — a partial
        # slot is never silently re-driven as success.
        acct = _admitted_account(6)
        rt = cap.reserve_beta_slot(acct)
        ProvisioningJob.objects.create(runtime=rt, op=ProvisioningJob.Op.PROVISION)
        agent = _good_agent()

        def transport(base, req):
            if req.get("operation") == "MATERIALISE":
                return _denied("stage_copy_precheck_failed")   # proven partial/integrity refusal
            return agent.handle(req)

        beta_worker.process_one(_factory(transport))
        rt.refresh_from_db()
        job = ProvisioningJob.objects.get(runtime=rt)
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)
        self.assertTrue(rt.quarantined)
        self.assertEqual(rt.quarantine_reason, "partial_materialise")
        self.assertEqual(rt.state, RuntimeState.FAILED)
