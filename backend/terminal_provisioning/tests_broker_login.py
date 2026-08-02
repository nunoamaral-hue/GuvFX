"""ADR-0021 PR B — genuine broker-login validation during dedicated-runtime provisioning.

The permanent rule: a customer runtime may reach RUNNING only after the assigned MT5 terminal has
established a genuine session with the SUBMITTED broker account and the returned account identity has been
verified (login + server + demo/live classification). Covers: the happy path, the full durable failure
taxonomy (9 states), retry safety, credential safety, state transitions, the NO-ORDER guarantee, and
Customer-Zero-style resubmit continuity — all under ``PROVISIONING_REQUIRE_BROKER_LOGIN=1`` (PR-B mode).

No real customer credentials appear here — ``SECRET`` is a synthetic test password.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from billing.models import BetaTester
from trading.crypto import encrypt_password
from trading.models import BrokerServer, TradingAccount

from terminal_provisioning import beta_capacity as cap
from terminal_provisioning.models import (
    AccountRuntime, ProvisioningJob, ProvisioningVerificationReport, RuntimeEvent, RuntimeState)
from terminal_provisioning.provisioner import (
    MAX_ATTEMPTS, FakeProvisioner, ProvisionStepError, advance_provisioning_job, enqueue_op)

U = get_user_model()
REQUIRE_LOGIN = override_settings(BETA_RUNTIMES_ENABLED=True, PROVISIONING_REQUIRE_BROKER_LOGIN=True,
                                  BETA_MAX_TESTERS=1000)

SECRET = "s3cret-broker-pw!never-real"   # synthetic — NEVER a real customer credential


def _acct(n=1, *, is_demo=True, with_server=True, password=SECRET):
    email = f"bl{n}@x.invalid"
    u = U.objects.create_user(username=f"bl{n}", email=email, password="x")
    BetaTester.objects.create(email=email)
    server = None
    if with_server:
        server = BrokerServer.objects.create(broker_display_name=f"Demo Broker {n}",
                                             server_name=f"Broker-Demo-{n}")
    return TradingAccount.objects.create(
        user=u, name=f"A{n}", account_number=str(88000 + n),
        broker_server=server, broker_name="" if with_server else "DemoBroker",
        is_demo=is_demo, password_enc=encrypt_password(password))


def _advance(acct, verify_result=None, fail_on=None, times=1):
    rt = cap.reserve_beta_slot(acct)
    p = FakeProvisioner(verify_result=verify_result, fail_on=fail_on)
    job = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
    for _ in range(times):
        job = advance_provisioning_job(job, p)
    rt.refresh_from_db()
    return rt, job, p


@REQUIRE_LOGIN
class BrokerLoginHappyPathTests(TestCase):
    def test_running_only_after_genuine_login_verified(self):
        acct = _acct(1, is_demo=True)
        rt, job, p = _advance(acct)
        self.assertEqual(rt.state, RuntimeState.RUNNING)
        self.assertEqual(job.status, ProvisioningJob.Status.DONE)
        # ordered lifecycle actually ran, ending at verify
        self.assertEqual([c[0] for c in p.calls], ["materialise", "configure", "start", "verify"])
        # the durable Verification Report records that the broker login WAS platform-verified
        report = ProvisioningVerificationReport.objects.get(runtime=rt)
        self.assertTrue(report.broker_login_verified)

    def test_configure_receives_login_server_and_password(self):
        acct = _acct(2, is_demo=True)
        _rt, _job, p = _advance(acct)
        cfg = next(c for c in p.calls if c[0] == "configure")
        # configure(login, server, bool(password)) — submitted account number, the MT5 server_name, and a
        # NON-EMPTY password (the sanctioned decrypted credential), passed via the channel not a cmdline.
        self.assertEqual(cfg[1], "88002")            # login = account number
        self.assertEqual(cfg[2], "Broker-Demo-2")    # server = broker_server.server_name
        self.assertTrue(cfg[3])                       # password present (bool True), plaintext not exposed


@REQUIRE_LOGIN
class BrokerLoginFailureTaxonomyTests(TestCase):
    """Each of the 9 failure modes leaves the runtime NON-ready with a truthful, distinct durable code."""

    def _fail(self, acct, verify_result=None, fail_on=None):
        rt, job, _p = _advance(acct, verify_result=verify_result, fail_on=fail_on)
        self.assertNotEqual(rt.state, RuntimeState.RUNNING)   # never RUNNING on a failed validation
        self.assertFalse(ProvisioningVerificationReport.objects.filter(runtime=rt).exists())
        return rt, job

    def test_invalid_credentials(self):
        rt, job = self._fail(_acct(1), {"running": True, "logged_in": False,
                                        "login_error": "invalid_credentials"})
        self.assertEqual(job.last_error, "broker_login_failed")
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)   # non-retryable → immediate FAIL
        self.assertEqual(rt.state, RuntimeState.FAILED)

    def test_broker_server_unavailable_is_retryable(self):
        rt, job = self._fail(_acct(2), {"running": True, "logged_in": False,
                                        "login_error": "server_unavailable"})
        self.assertEqual(job.last_error, "broker_server_unavailable")
        self.assertEqual(job.status, ProvisioningJob.Status.QUEUED)   # retryable → re-queued
        self.assertNotEqual(rt.state, RuntimeState.FAILED)

    def test_mt5_initialisation_failure(self):
        rt, job = self._fail(_acct(3), {"running": False, "init_error": "terminal did not initialise"})
        self.assertEqual(job.last_error, "mt5_init_failed")
        self.assertEqual(job.status, ProvisioningJob.Status.QUEUED)   # retryable

    def test_login_timeout_is_retryable(self):
        rt, job = self._fail(_acct(4), {"running": True, "logged_in": False, "login_error": "timeout"})
        self.assertEqual(job.last_error, "broker_login_timeout")
        self.assertEqual(job.status, ProvisioningJob.Status.QUEUED)

    def test_terminal_crash_not_running(self):
        rt, job = self._fail(_acct(5), {"running": False})
        self.assertEqual(job.last_error, "terminal_not_running")
        self.assertEqual(job.status, ProvisioningJob.Status.QUEUED)   # retryable (may still be starting)

    def test_account_identity_mismatch(self):
        # authenticated to the WRONG login — fail closed, do NOT run it
        rt, job = self._fail(_acct(6), {"running": True, "logged_in": True, "login": "99999",
                                        "server": "", "is_demo": True})
        self.assertEqual(job.last_error, "broker_identity_mismatch")
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)   # non-retryable
        self.assertEqual(rt.state, RuntimeState.FAILED)

    def test_server_identity_mismatch(self):
        # login matches (88007) but the CONNECTED server differs from the submitted broker_server → mismatch
        rt, job = self._fail(_acct(7), {"running": True, "logged_in": True, "login": "88007",
                                        "server": "Some-Other-Server", "is_demo": True})
        self.assertEqual(job.last_error, "broker_identity_mismatch")
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)

    def test_demo_live_mismatch(self):
        # account DECLARED demo, but the connected account is LIVE → fail closed
        rt, job = self._fail(_acct(8, is_demo=True), {"running": True, "logged_in": True, "login": None,
                                                      "is_demo": False})
        self.assertEqual(job.last_error, "demo_live_mismatch")
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)   # non-retryable
        self.assertEqual(rt.state, RuntimeState.FAILED)

    def test_free_text_broker_name_resolves_as_server(self):
        # ADR-0025: the customer's free-text broker_name (the frontend "Broker server name" field) IS the
        # MT5 server for beta accounts. With NO normalised broker_server, provisioning now RESOLVES
        # broker_name as the server and reaches RUNNING with a genuinely verified login (the exact-server
        # identity check still runs) — the automated journey needs no operator/customer re-entry.
        acct = _acct(11, with_server=False)   # broker_name="DemoBroker", broker_server=None
        rt, job, p = _advance(acct)
        self.assertEqual(rt.state, RuntimeState.RUNNING)
        self.assertEqual(job.status, ProvisioningJob.Status.DONE)
        cfg = next(c for c in p.calls if c[0] == "configure")   # (configure, login, server, bool(pw))
        self.assertEqual(cfg[2], "DemoBroker")                  # configure received the resolved server
        self.assertTrue(ProvisioningVerificationReport.objects.get(runtime=rt).broker_login_verified)

    # NOTE: a "no server anywhere" account cannot be persisted (the model strips broker_name and the
    # ``brokeridentity_present`` DB constraint + create serializer both require a broker_server FK OR a
    # non-empty broker_name), so the provisioning ``broker_server_missing`` gate is unreachable for a real
    # account. The resolver's missing-path is covered by tests_broker_server_resolution (in-memory); the
    # end-to-end fail-closed path is covered by the CONFLICT case there.

    def test_null_classification_fails_closed(self):
        # a PRESENT-but-null classification (agent could not determine) must NOT pass via bool() coercion
        # (bool(None)==bool(False) would wrongly pass a live account) — strict boolean required.
        rt, job = self._fail(_acct(12, is_demo=False), {"running": True, "logged_in": True, "login": None,
                                                        "server": None, "is_demo": None})
        self.assertEqual(job.last_error, "demo_live_mismatch")

    def test_non_boolean_classification_fails_closed(self):
        # a non-boolean truthy value (e.g. the string "false") must NOT pass for a demo-declared account
        rt, job = self._fail(_acct(13, is_demo=True), {"running": True, "logged_in": True, "login": None,
                                                       "server": None, "is_demo": "false"})
        self.assertEqual(job.last_error, "demo_live_mismatch")

    def test_missing_classification_fails_closed(self):
        # the agent did not report a demo/live classification → treated as unverified (fail closed)
        acct = _acct(9, is_demo=True)
        rt = cap.reserve_beta_slot(acct)
        p = FakeProvisioner()
        p.verify = lambda runtime: {"running": True, "logged_in": True,
                                    "login": "88009", "server": "Broker-Demo-9"}   # NO is_demo key
        job = advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), p)
        rt.refresh_from_db()
        self.assertNotEqual(rt.state, RuntimeState.RUNNING)
        self.assertEqual(job.last_error, "demo_live_mismatch")

    def test_unexpected_technical_error_is_sanitised(self):
        # a raw exception from verify is converted to a sanitised, retryable step error (never leaked)
        acct = _acct(10)
        rt = cap.reserve_beta_slot(acct)
        p = FakeProvisioner()

        def boom(runtime):
            raise RuntimeError("INTERNAL boom C:\\secret\\path")
        p.verify = boom
        job = advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), p)
        rt.refresh_from_db()
        self.assertNotEqual(rt.state, RuntimeState.RUNNING)
        self.assertEqual(job.last_error, "verify_failed")           # sanitised code, not the raw string
        self.assertNotIn("secret", job.last_error)


@REQUIRE_LOGIN
class BrokerLoginRetrySafetyTests(TestCase):
    def test_bad_credentials_no_infinite_loop(self):
        acct = _acct(1)
        rt, job, _p = _advance(acct, {"running": True, "logged_in": False,
                                      "login_error": "invalid_credentials"})
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)   # ONE attempt, then terminal
        self.assertEqual(job.attempt, 1)
        # a further advance is a no-op on a terminal job (never re-launches / never loops)
        job2 = advance_provisioning_job(job, FakeProvisioner())
        self.assertEqual(job2.status, ProvisioningJob.Status.FAILED)
        self.assertEqual(job2.attempt, 1)

    def test_transient_failure_bounded_by_max_attempts(self):
        acct = _acct(2)
        rt = cap.reserve_beta_slot(acct)
        p = FakeProvisioner(verify_result={"running": True, "logged_in": False,
                                           "login_error": "server_unavailable"})
        job = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
        for _ in range(MAX_ATTEMPTS):
            job = advance_provisioning_job(job, p)
        rt.refresh_from_db()
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)   # bounded — not infinite
        self.assertEqual(rt.state, RuntimeState.FAILED)

    def test_retry_after_transient_reaches_running_same_runtime_and_job(self):
        acct = _acct(3)
        rt = cap.reserve_beta_slot(acct)
        job = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
        # 1st attempt: transient server-unavailable → re-queued
        job = advance_provisioning_job(job, FakeProvisioner(
            verify_result={"running": True, "logged_in": False, "login_error": "server_unavailable"}))
        self.assertEqual(job.status, ProvisioningJob.Status.QUEUED)
        # 2nd attempt: healthy → RUNNING, and STILL exactly one runtime + one job
        job = advance_provisioning_job(job, FakeProvisioner())
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.RUNNING)
        self.assertEqual(AccountRuntime.objects.filter(trading_account=acct).count(), 1)
        self.assertEqual(ProvisioningJob.objects.filter(runtime=rt).count(), 1)

    def test_repeated_provision_reuses_runtime_no_second_active_job(self):
        acct = _acct(4)
        rt = cap.reserve_beta_slot(acct)
        j1 = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
        # a duplicate enqueue while one is active returns the SAME job (uniq_active_job_per_runtime_op)
        j2 = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
        self.assertEqual(j1.id, j2.id)
        self.assertEqual(cap.reserve_beta_slot(acct).id, rt.id)   # reservation idempotent — same runtime
        self.assertEqual(
            ProvisioningJob.objects.filter(runtime=rt, op=ProvisioningJob.Op.PROVISION,
                                           status__in=[ProvisioningJob.Status.QUEUED,
                                                       ProvisioningJob.Status.RUNNING]).count(), 1)


@REQUIRE_LOGIN
class BrokerLoginCredentialSafetyTests(TestCase):
    def test_password_never_appears_in_any_durable_evidence(self):
        acct = _acct(1, password=SECRET)
        # run a FAILED path too, so failure evidence is also checked for the secret
        rt, job, p = _advance(acct, {"running": True, "logged_in": False,
                                     "login_error": "invalid_credentials"})
        durable_blobs = []
        for ev in RuntimeEvent.objects.filter(runtime=rt):
            durable_blobs += [ev.reason_code or "", ev.detail or "", getattr(ev, "to_state", "") or ""]
        durable_blobs += [job.last_error or "", rt.last_failure_reason or ""]
        for r in ProvisioningVerificationReport.objects.filter(runtime=rt):
            durable_blobs += [r.broker_login or "", r.broker_server or "", r.bridge_identity or ""]
        blob = "\n".join(str(b) for b in durable_blobs)
        self.assertNotIn(SECRET, blob)              # plaintext credential is NOWHERE durable
        # configure DID receive a non-empty password (proving it was decrypted + passed, not skipped)
        self.assertTrue(any(c[0] == "configure" and c[3] for c in p.calls))

    def test_credential_access_is_audited(self):
        from core.models import AuditEvent
        acct = _acct(2)
        _advance(acct)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="CREDENTIAL_ACCESSED", entity_id=str(acct.id)).exists())


@REQUIRE_LOGIN
class BrokerLoginNoOrderProofTests(TestCase):
    def test_validation_places_no_order(self):
        from execution.models import ExecutionJob
        from trading.models import Trade
        acct = _acct(1)
        rt, job, p = _advance(acct)
        self.assertEqual(rt.state, RuntimeState.RUNNING)   # a full, successful validation…
        # …placed NO trading action: no trades, no positions, no execution jobs.
        self.assertEqual(Trade.objects.count(), 0)
        self.assertEqual(ExecutionJob.objects.count(), 0)
        # the provisioner interface exposes no order capability, and none was invoked.
        self.assertFalse(any("order" in str(c[0]).lower() for c in p.calls))
        self.assertFalse(hasattr(p, "order_send"))
        self.assertFalse(hasattr(p, "place_order"))


@REQUIRE_LOGIN
class BrokerLoginContinuityTests(TestCase):
    def test_resubmit_reuses_runtime_and_job_no_duplicates(self):
        # Customer-Zero-style continuity at the provisioning layer: an existing account re-driven returns
        # the SAME runtime + SAME active job (no duplicate runtime, no second active job). The account-row
        # no-duplicate guarantee is PR-A (trading.tests_beta_provisioning_wiring); here we prove the
        # runtime/job continuity holds under PR-B (broker-login) mode.
        acct = _acct(1)
        rt1 = cap.reserve_beta_slot(acct)
        j1 = enqueue_op(rt1, ProvisioningJob.Op.PROVISION)
        # resubmit / resume
        rt2 = cap.get_or_create_beta_runtime(acct)
        rt3 = cap.reserve_beta_slot(acct)
        j2 = enqueue_op(rt1, ProvisioningJob.Op.PROVISION)
        self.assertEqual({rt1.id, rt2.id, rt3.id}, {rt1.id})     # one runtime
        self.assertEqual(j1.id, j2.id)                            # one active job
        self.assertEqual(AccountRuntime.objects.filter(trading_account=acct).count(), 1)
        self.assertEqual(TradingAccount.objects.filter(id=acct.id).count(), 1)  # no duplicate account
