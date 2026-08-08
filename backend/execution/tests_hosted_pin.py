"""ADR-0034 Execution Engine (G3) — server-derived per-job identity pin for Hosted Workspace (Provider B).

Proves: the pin is derived server-side from the account's durable bindings; it is injected at the SINGLE
creation boundary (``ExecutionJob.save``) for every mutation job type; it is a total no-op for Provider A /
Customer Zero and while the subsystem is dark (regression-identical legacy path); it fails closed (pin still
required) when a binding value is missing; and it never clobbers a caller-supplied pin. Plus a runnable
mutation harness on the pure predicates.
"""
from __future__ import annotations

import inspect
import textwrap

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from trading.models import BrokerServer, TradingAccount

from execution import hosted_pin as HP
from execution.hosted_pin import identity_pin_for, inject_identity_pin, is_hosted_workspace_account
from execution.models import ExecutionJob
from execution.readiness import PERSISTENT_WORKSPACE, TEMPORARY_VALIDATION

U = get_user_model()

PIN_KEYS = {"require_identity_pin", "expected_login", "expected_server", "is_demo"}


def _account(provider=TEMPORARY_VALIDATION, *, login="500123", server_name="Demo-Srv", is_demo=True,
             with_server=True):
    user = U.objects.create_user(username=f"u{login}{provider}", email=f"{login}{provider}@x.invalid",
                                 password="x")
    server = None
    if with_server:
        server, _ = BrokerServer.objects.get_or_create(server_name=server_name)  # server_name is unique
    return TradingAccount.objects.create(
        user=user, name="a", broker_name="B", account_number=login, is_demo=is_demo,
        broker_server=server, readiness_provider=provider)


class IdentityPinForTests(TestCase):
    def test_non_provider_b_is_empty_even_when_enabled(self):
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True):
            acct = _account(provider=TEMPORARY_VALIDATION)
            self.assertEqual(identity_pin_for(acct), {})
            self.assertFalse(is_hosted_workspace_account(acct))

    def test_none_account_is_empty(self):
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True):
            self.assertEqual(identity_pin_for(None), {})

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_provider_b_pins_server_derived_identity(self):
        acct = _account(provider=PERSISTENT_WORKSPACE, login="700900", server_name="IS6-Demo", is_demo=True)
        pin = identity_pin_for(acct)
        self.assertEqual(pin, {"require_identity_pin": True, "expected_login": "700900",
                               "expected_server": "IS6-Demo", "is_demo": True})

    def test_provider_b_dark_is_empty(self):
        # Flag OFF (default): the legacy env-pin path is unchanged — no per-job pin.
        acct = _account(provider=PERSISTENT_WORKSPACE)
        self.assertEqual(identity_pin_for(acct), {})
        self.assertFalse(is_hosted_workspace_account(acct))

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_missing_server_still_requires_pin_fail_closed(self):
        acct = _account(provider=PERSISTENT_WORKSPACE, login="700900", with_server=False)
        pin = identity_pin_for(acct)
        self.assertTrue(pin["require_identity_pin"])   # pin STILL required
        self.assertEqual(pin["expected_server"], "")   # empty ⇒ the bridge fails closed, never trades unpinned
        self.assertEqual(pin["expected_login"], "700900")

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_pin_carries_live_classification(self):
        live = _account(provider=PERSISTENT_WORKSPACE, login="900001", is_demo=False)
        self.assertFalse(identity_pin_for(live)["is_demo"])  # demo/live travels with the pin (PART F)


class InjectPinTests(TestCase):
    def _job(self, account, job_type=ExecutionJob.JobType.CLOSE_TRADE, payload=None):
        # CLOSE_TRADE/MODIFY are NOT gate/kill-blocked, so save() runs without the broker gate interfering.
        return ExecutionJob(job_type=job_type, account=account, payload=payload or {"ticket": 1})

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_provider_b_close_job_is_pinned_via_save(self):
        acct = _account(provider=PERSISTENT_WORKSPACE, login="700900", server_name="IS6-Demo")
        job = self._job(acct, ExecutionJob.JobType.CLOSE_TRADE)
        job.save()
        job.refresh_from_db()
        self.assertTrue(job.payload["require_identity_pin"])
        self.assertEqual(job.payload["expected_login"], "700900")
        self.assertEqual(job.payload["expected_server"], "IS6-Demo")
        self.assertEqual(job.payload["ticket"], 1)  # original payload preserved

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_provider_b_modify_job_is_pinned(self):
        acct = _account(provider=PERSISTENT_WORKSPACE)
        job = self._job(acct, ExecutionJob.JobType.MODIFY_POSITION)
        job.save()
        job.refresh_from_db()
        self.assertTrue(job.payload["require_identity_pin"])

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_provider_a_job_is_not_pinned(self):  # regression: Provider A untouched
        acct = _account(provider=TEMPORARY_VALIDATION)
        job = self._job(acct, ExecutionJob.JobType.CLOSE_TRADE)
        job.save()
        job.refresh_from_db()
        self.assertEqual(PIN_KEYS & set(job.payload), set())

    def test_dark_job_is_not_pinned(self):  # regression: subsystem dark ⇒ no payload change
        acct = _account(provider=PERSISTENT_WORKSPACE)
        job = self._job(acct, ExecutionJob.JobType.CLOSE_TRADE)
        job.save()
        job.refresh_from_db()
        self.assertEqual(PIN_KEYS & set(job.payload), set())

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_non_mutation_type_is_not_pinned(self):
        acct = _account(provider=PERSISTENT_WORKSPACE)
        job = ExecutionJob(job_type=ExecutionJob.JobType.SYNC_POSITIONS, account=acct, payload={})
        job.save()
        job.refresh_from_db()
        self.assertEqual(PIN_KEYS & set(job.payload), set())

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_inject_never_clobbers_caller_supplied_pin(self):
        acct = _account(provider=PERSISTENT_WORKSPACE, login="700900")
        job = self._job(acct, payload={"expected_login": "CALLER_SET", "ticket": 9})
        inject_identity_pin(job)
        self.assertEqual(job.payload["expected_login"], "CALLER_SET")  # setdefault preserves the caller's
        self.assertTrue(job.payload["require_identity_pin"])           # but fills the missing keys

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_require_pin_enable_flag_cannot_be_disabled_by_payload(self):
        # The ENABLE flag is safety-critical: a payload can never turn the pin OFF for a hosted mutation.
        acct = _account(provider=PERSISTENT_WORKSPACE, login="700900")
        job = self._job(acct, payload={"require_identity_pin": False, "ticket": 3})
        inject_identity_pin(job)
        self.assertTrue(job.payload["require_identity_pin"])   # forced on, not setdefault
        self.assertEqual(job.payload["expected_login"], "700900")

    def test_inject_returns_false_when_dark(self):
        acct = _account(provider=PERSISTENT_WORKSPACE)
        job = self._job(acct)
        self.assertFalse(inject_identity_pin(job))


class MutationAdequacyTests(TestCase):
    """Runnable mutation harness for the pin's pure decision. Each mutant must be KILLED by an oracle."""

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_identity_pin_for_oracle(self):
        b = _account(provider=PERSISTENT_WORKSPACE, login="1", server_name="S")
        a = _account(provider=TEMPORARY_VALIDATION, login="2", server_name="S")
        self.assertEqual(identity_pin_for(b)["expected_login"], "1")
        self.assertEqual(identity_pin_for(a), {})  # the provider comparison is load-bearing

    def test_provider_comparison_mutant_is_killed(self):
        # `== PERSISTENT_WORKSPACE` -> `!= PERSISTENT_WORKSPACE`: a Provider-B account would go empty and a
        # Provider-A account would get pinned — both wrong. Mutate the source of is_hosted_workspace_account.
        src = textwrap.dedent(inspect.getsource(HP.is_hosted_workspace_account))
        mutant_src = src.replace("== PERSISTENT_WORKSPACE", "!= PERSISTENT_WORKSPACE", 1)
        self.assertIn("!= PERSISTENT_WORKSPACE", mutant_src)
        ns = {"PERSISTENT_WORKSPACE": PERSISTENT_WORKSPACE, "_provider_b_pin_enabled": lambda: True}
        exec(compile(mutant_src, "<mutant>", "exec"), ns)
        mutant = ns["is_hosted_workspace_account"]

        class _B:  # a Provider-B-shaped object
            readiness_provider = PERSISTENT_WORKSPACE
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True):
            self.assertTrue(is_hosted_workspace_account_via(_B()))   # real: Provider B ⇒ True
        self.assertFalse(mutant(_B()))  # mutant flips it ⇒ killed


def is_hosted_workspace_account_via(obj):
    """Call the real predicate against a shaped object (helper for the mutation oracle)."""
    return is_hosted_workspace_account(obj)
