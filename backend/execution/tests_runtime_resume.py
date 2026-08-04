"""WP1B/WP2 (ADR-0029) — CONTROLLED RESUME (Workstream D) tests.

The single explicit-caller service that clears a broker-health pause. Covers: success, every refusal
(account + health + credential + validation + stale/newer version), idempotency + concurrency
outcomes, flag-OFF inertness, non-destructive invariants, and the proof that NO automatic path invokes
it.
"""
import os
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models import AuditEvent
from execution import broker_gate as bg
from execution import runtime_pause as rp
from execution.models import BrokerRuntimePause, ExecutionJob
from reliability.models import BrokerAccountHealth
from trading.models import TradingAccount

U = get_user_model()
_VS = TradingAccount.ValidationStatus
_ON_BOTH = {"BROKER_CONNECTIVITY_EXECUTION_GATE": "1", "BROKER_CONNECTIVITY_HEALTH_ENABLED": "true"}


def _acct(user, *, number="1302575", active=True, validated=_VS.VALIDATED, cred="cipher", disconnected=None):
    a = TradingAccount.objects.create(
        user=user, name="A", account_number=number, broker_name="IS6Technologies",
        is_demo=True, is_active=active, validation_status=validated, password_enc=cred)
    if disconnected is not None:
        a.disconnected_at = disconnected
        a.save(update_fields=["disconnected_at"])
    return a


def _health(account, state, version):
    return BrokerAccountHealth.objects.create(account=account, state=state, state_version=version)


def _paused(account, *, source_version, paused=True):
    return BrokerRuntimePause.objects.create(
        account=account, paused=paused, reason_code=bg.SR_HEALTH_DEGRADED,
        source_state_version=source_version, last_processed_version=source_version,
        paused_at=timezone.now())


class ResumeSuccessTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="r1", email="r1@x.invalid", password="x")
        self.acct = _acct(self.user)

    def test_healthy_recovery_resumes(self):
        _health(self.acct, "HEALTHY", 11)
        _paused(self.acct, source_version=10)
        with mock.patch.dict(os.environ, _ON_BOTH):
            res = rp.request_broker_runtime_resume(self.acct)
        self.assertTrue(res.resumed)
        self.assertFalse(res.refused)
        self.assertEqual(res.processed_state_version, 11)
        rec = BrokerRuntimePause.objects.get(account=self.acct)
        self.assertFalse(rec.paused)
        self.assertIsNotNone(rec.resumed_at)
        self.assertEqual(rec.resumed_state_version, 11)
        self.assertFalse(rec.resume_eligible)
        self.assertEqual(ExecutionJob.objects.count(), 0)  # no order/job created
        self.assertTrue(AuditEvent.objects.filter(event_type="BROKER_RUNTIME_RESUMED").exists())

    def test_resume_does_not_mutate_account(self):
        _health(self.acct, "HEALTHY", 6)
        _paused(self.acct, source_version=5)
        before = TradingAccount.objects.get(pk=self.acct.pk)
        with mock.patch.dict(os.environ, _ON_BOTH):
            rp.request_broker_runtime_resume(self.acct)
        after = TradingAccount.objects.get(pk=self.acct.pk)
        self.assertEqual(after.password_enc, before.password_enc)
        self.assertEqual(after.validation_status, before.validation_status)
        self.assertEqual(after.is_active, before.is_active)


class ResumeRefusalTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="r2", email="r2@x.invalid", password="x")

    def _resume(self, acct):
        with mock.patch.dict(os.environ, _ON_BOTH):
            return rp.request_broker_runtime_resume(acct)

    def test_no_pause_row_refused(self):
        acct = _acct(self.user, number="200")
        _health(acct, "HEALTHY", 3)
        res = self._resume(acct)
        self.assertTrue(res.refused)
        self.assertEqual(res.reason_code, bg.SR_RESUME_NOT_ELIGIBLE)

    def test_already_cleared_is_idempotent(self):
        acct = _acct(self.user, number="201")
        _health(acct, "HEALTHY", 5)
        _paused(acct, source_version=4, paused=False)
        res = self._resume(acct)
        self.assertTrue(res.idempotent)
        self.assertFalse(res.resumed)

    def test_health_refusals(self):
        cases = [("DEGRADED", bg.SR_HEALTH_DEGRADED), ("STALE", bg.SR_HEALTH_STALE),
                 ("DISCONNECTED", bg.SR_HEALTH_DISCONNECTED), ("TOMBSTONED", bg.SR_ACCOUNT_TOMBSTONED)]
        for i, (state, reason) in enumerate(cases):
            acct = _acct(self.user, number=f"30{i}")
            _health(acct, state, 6)
            _paused(acct, source_version=5)
            res = self._resume(acct)
            self.assertTrue(res.refused, state)
            self.assertEqual(res.reason_code, reason, state)
            self.assertTrue(BrokerRuntimePause.objects.get(account=acct).paused)  # still paused

    def test_account_refusals(self):
        cases = [
            (dict(active=False), bg.SR_ACCOUNT_INACTIVE),
            (dict(disconnected=timezone.now()), bg.SR_ACCOUNT_DISCONNECTED),
            (dict(cred=""), bg.SR_CREDENTIAL_MISSING),
            (dict(validated=_VS.NEVER), bg.SR_VALIDATION_REQUIRED),
            (dict(validated=_VS.CONNECTION_FAILED), bg.SR_VALIDATION_FAILED),
            (dict(validated=_VS.TECHNICAL_ERROR), bg.SR_VALIDATION_UNAVAILABLE),
        ]
        for i, (kw, reason) in enumerate(cases):
            acct = _acct(self.user, number=f"40{i}", **kw)
            _health(acct, "HEALTHY", 6)
            _paused(acct, source_version=5)
            res = self._resume(acct)
            self.assertTrue(res.refused, kw)
            self.assertEqual(res.reason_code, reason, kw)
            self.assertTrue(BrokerRuntimePause.objects.get(account=acct).paused)

    def test_missing_health_contract_refused(self):
        acct = _acct(self.user, number="500")  # no health row
        _paused(acct, source_version=5)
        res = self._resume(acct)
        self.assertTrue(res.refused)
        self.assertEqual(res.reason_code, bg.SR_RESUME_NOT_ELIGIBLE)

    def test_stale_resume_version_refused(self):
        # Pause at v10; the observed contract is an older v9 → stale, must fail closed (never clears).
        acct = _acct(self.user, number="600")
        _health(acct, "HEALTHY", 9)
        _paused(acct, source_version=10)
        res = self._resume(acct)
        self.assertTrue(res.refused)
        self.assertEqual(res.reason_code, bg.SR_HEALTH_STATE_CHANGED)
        self.assertTrue(BrokerRuntimePause.objects.get(account=acct).paused)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="BROKER_HEALTH_STALE_RESUME_VERSION_IGNORED").exists())

    def test_newer_degradation_refuses_resume(self):
        # Paused v10, but a newer degradation (contract DEGRADED v12) has superseded the recovery.
        acct = _acct(self.user, number="601")
        _health(acct, "DEGRADED", 12)
        _paused(acct, source_version=10)
        res = self._resume(acct)
        self.assertTrue(res.refused)
        self.assertEqual(res.reason_code, bg.SR_HEALTH_DEGRADED)


class ResumeIdempotencyTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="r3", email="r3@x.invalid", password="x")
        self.acct = _acct(self.user)
        _health(self.acct, "HEALTHY", 11)
        _paused(self.acct, source_version=10)

    def test_duplicate_resume_one_success_then_idempotent(self):
        with mock.patch.dict(os.environ, _ON_BOTH):
            r1 = rp.request_broker_runtime_resume(self.acct)
            r2 = rp.request_broker_runtime_resume(self.acct)  # replay / duplicate
        self.assertTrue(r1.resumed)
        self.assertTrue(r2.idempotent)
        self.assertFalse(r2.resumed)
        # exactly one RESUMED audit
        self.assertEqual(AuditEvent.objects.filter(event_type="BROKER_RUNTIME_RESUMED").count(), 1)

    def test_pause_after_resume_persists_as_newer(self):
        with mock.patch.dict(os.environ, _ON_BOTH):
            rp.request_broker_runtime_resume(self.acct)  # resumed at v11
            # A newer degradation arrives at v13 and is processed → re-pauses (no older resume reverses it).
            h = BrokerAccountHealth.objects.get(account=self.acct)
            h.state = "DISCONNECTED"
            h.state_version = 13
            h.save(update_fields=["state", "state_version"])
            rp.process_broker_health_pause(self.acct)
        rec = BrokerRuntimePause.objects.get(account=self.acct)
        self.assertTrue(rec.paused)
        self.assertEqual(rec.source_state_version, 13)


class ResumeInertnessTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="r4", email="r4@x.invalid", password="x")
        self.acct = _acct(self.user)
        _health(self.acct, "HEALTHY", 11)
        _paused(self.acct, source_version=10)

    def _assert_inert(self, env):
        before_audits = AuditEvent.objects.count()
        with mock.patch.dict(os.environ, env, clear=True):
            res = rp.request_broker_runtime_resume(self.acct)
        self.assertTrue(res.refused)
        self.assertEqual(res.reason_code, bg.SR_RESUME_NOT_ELIGIBLE)
        self.assertTrue(BrokerRuntimePause.objects.get(account=self.acct).paused)  # not cleared
        self.assertEqual(AuditEvent.objects.count(), before_audits)  # no audit emitted

    def test_inert_both_flags_off(self):
        self._assert_inert({})

    def test_inert_exec_flag_off(self):
        self._assert_inert({"BROKER_CONNECTIVITY_HEALTH_ENABLED": "true"})

    def test_inert_health_flag_off(self):
        self._assert_inert({"BROKER_CONNECTIVITY_EXECUTION_GATE": "1"})


class NoAutomaticResumeTests(TestCase):
    """WSE (ADR-0029) — prove the controlled resume service (``request_broker_runtime_resume``) has NO
    automatic caller. SCOPE/BOUNDARY: this scans ``backend/**/*.py`` only. The standalone host services
    (the Windows bridge, ``mt5_validate_worker``, ``mt5_worker/``) live outside ``backend/`` and cannot
    import Django/execution, so they cannot call it; non-``.py`` invocation surfaces (cron / systemd /
    compose / periodic-task DB rows referencing a management command) are out of scope and are governed by
    the arming runbook, not this test."""

    # ALLOWLIST of files permitted to reference the resume service by name. Only its definition today; when
    # the single sanctioned operator entry point is wired, ADD it here — an allowlist, so the guard
    # survives arming without being weakened into a bare "offenders == []".
    _ALLOWED_RESUME_REFERENCES = {"execution/runtime_pause.py"}
    _RESUME_TOKEN = "broker_runtime_resume"

    @staticmethod
    def _is_test_or_migration(rel: str) -> bool:
        # Precise exclusion (NOT a broad `"tests" in rel` substring): a genuine test module is one whose
        # basename matches test_*/tests_*, or that lives under a directory component exactly named
        # "tests" — so a production file whose path merely contains the substring "tests" is still scanned.
        parts = rel.split("/")
        base = parts[-1]
        return (base in ("tests.py", "test.py", "conftest.py")
                or base.startswith(("test_", "tests_"))
                or "tests" in parts[:-1] or "migrations" in parts[:-1])

    def test_resume_service_has_no_automatic_caller(self):
        # No automatic path invokes the controlled resume. Scans for the distinctive function-name core
        # `broker_runtime_resume`, so a literal call OR a full-string getattr is caught.
        backend = Path(__file__).resolve().parent.parent
        offenders = []
        scanned = 0
        hits_in_definition = 0
        for path in backend.rglob("*.py"):
            rel = path.relative_to(backend).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            scanned += 1
            has_token = self._RESUME_TOKEN in text
            if rel == "execution/runtime_pause.py" and has_token:
                hits_in_definition = text.count(self._RESUME_TOKEN)
            if self._is_test_or_migration(rel) or rel in self._ALLOWED_RESUME_REFERENCES:
                continue
            if has_token:
                offenders.append(rel)
        # POSITIVE CONTROL (RULE 11) — a negative result is worthless unless the scanner is shown capable of
        # a positive: the walk must have found many files, and the definition file MUST contain the token.
        # A broken/empty walk, wrong root, or mistyped token would otherwise pass green proving nothing.
        self.assertGreater(scanned, 200, "resume-caller scan walked too few files — the walk is broken")
        self.assertGreaterEqual(
            hits_in_definition, 1,
            "positive control FAILED: the scanner did not find the token in its own definition file")
        self.assertEqual(
            offenders, [], f"unexpected non-allowlisted reference(s) to the resume service: {offenders}")

    def test_no_split_string_dispatch_of_resume(self):
        # Best-effort catch of the split / constructed-string dispatch the plain substring scan misses
        # (e.g. getattr(rp, "request_broker_runtime_" + "resume")). A residual dynamic-reflection gap
        # (dir()/suffix matching) remains and is documented in ADR-0029 §resume-caller-proof.
        backend = Path(__file__).resolve().parent.parent
        red_flags = ('"request_broker_runtime_"', "'request_broker_runtime_'",
                     '"broker_runtime_"', "'broker_runtime_'")
        offenders = []
        for path in backend.rglob("*.py"):
            rel = path.relative_to(backend).as_posix()
            if self._is_test_or_migration(rel) or rel in self._ALLOWED_RESUME_REFERENCES:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if any(f in text for f in red_flags):
                offenders.append(rel)
        self.assertEqual(offenders, [], f"possible split-string resume dispatch: {offenders}")

    def test_process_pause_never_clears_paused(self):
        # BEHAVIOURAL guard (replaces the brittle "paused = False" source-string assertion): drive a
        # recovery contract through process_broker_health_pause and prove it NEVER clears the pause — it
        # only records resume-eligibility. Only request_broker_runtime_resume may clear a pause.
        user = U.objects.create_user(username="rp-beh", email="rpb@x.invalid", password="x")
        acct = _acct(user)
        _health(acct, "HEALTHY", 11)          # recovered / eligible at a newer version
        rec = _paused(acct, source_version=10)
        with mock.patch.dict(os.environ, _ON_BOTH):
            rp.process_broker_health_pause(acct)
        rec.refresh_from_db()
        self.assertTrue(rec.paused, "process_broker_health_pause must NEVER clear a pause")
        self.assertTrue(rec.resume_eligible, "recovery must record resume-eligibility on the pause record")
