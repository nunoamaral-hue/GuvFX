"""CVM-Inc-3 B1 — beta ProvisioningJob worker + versioned-contract negotiation tests."""
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model

from billing.models import BetaTester
from trading.models import TradingAccount
from trading.crypto import encrypt_password
from terminal_provisioning import beta_capacity as cap
from terminal_provisioning import beta_worker
from terminal_provisioning.mgmt_client import AgentWindowsProvisioner, ManagementChannelTimeout
from terminal_provisioning.models import AccountRuntime, ProvisioningJob, RuntimeState
from terminal_provisioning.provisioner import MAX_ATTEMPTS
from terminal_provisioning.tests_mgmt_channel import KEYRING, _agent

U = get_user_model()


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

    def test_ambiguous_timeout_quarantines_after_bounded_attempts(self):
        acct = _admitted_account(4)
        rt = cap.reserve_beta_slot(acct)
        ProvisioningJob.objects.create(runtime=rt, op=ProvisioningJob.Op.PROVISION)
        agent = _good_agent()

        def transport(base, req):
            if req.get("operation") == "NEGOTIATE":
                return agent.handle(req)            # negotiation OK
            raise ManagementChannelTimeout()        # every provisioning op times out (ambiguous)

        factory = _factory(transport)
        for _ in range(MAX_ATTEMPTS):
            beta_worker.process_one(factory)
        rt.refresh_from_db()
        job = ProvisioningJob.objects.get(runtime=rt)
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)
        self.assertTrue(rt.quarantined)                          # quarantined, not re-launched
        self.assertEqual(rt.quarantine_reason, "ambiguous_timeout")
        self.assertNotEqual(rt.state, RuntimeState.FAILED)       # state left as-is (a terminal may be up)
