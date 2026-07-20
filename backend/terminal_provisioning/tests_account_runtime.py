"""GFX-BETA-PHASE0 Increment 2 — AccountRuntime (1:1) + immutable RuntimeEvent + durable state service,
and the onboarding path recording provisioning failures instead of swallowing them."""
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.contrib.auth import get_user_model

from trading.models import TradingAccount
from terminal_provisioning.models import AccountRuntime, RuntimeEvent, RuntimeState
from terminal_provisioning.runtime_state import (
    get_or_create_runtime, record_transition, user_facing_state)

U = get_user_model()


class AccountRuntimeModelTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="u", email="u@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="A", account_number="A1", is_demo=True)

    def test_runtime_is_one_to_one_with_account(self):
        AccountRuntime.objects.create(trading_account=self.acct)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AccountRuntime.objects.create(trading_account=self.acct)

    def test_default_state_not_provisioned(self):
        rt = get_or_create_runtime(self.acct)
        self.assertEqual(rt.state, RuntimeState.NOT_PROVISIONED)
        self.assertEqual(user_facing_state(rt), "NOT_CONFIGURED")

    def test_get_or_create_is_idempotent(self):
        a = get_or_create_runtime(self.acct)
        b = get_or_create_runtime(self.acct)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(AccountRuntime.objects.filter(trading_account=self.acct).count(), 1)

    def test_runtime_event_is_immutable(self):
        rt = get_or_create_runtime(self.acct)
        ev = RuntimeEvent.objects.create(runtime=rt, from_state="A", to_state="B")
        ev.to_state = "C"
        with self.assertRaises(ValueError):
            ev.save()
        with self.assertRaises(ValueError):
            ev.delete()

    def test_runtime_event_db_level_update_blocked(self):
        # Even a queryset .update() (which bypasses the model save() override) is rejected by the DB trigger.
        from django.db import Error
        rt = get_or_create_runtime(self.acct)
        ev = RuntimeEvent.objects.create(runtime=rt, from_state="A", to_state="B")
        with self.assertRaises(Error):
            with transaction.atomic():
                RuntimeEvent.objects.filter(pk=ev.pk).update(to_state="C")
        ev.refresh_from_db()
        self.assertEqual(ev.to_state, "B")  # unchanged

    def test_cascade_delete_with_account_still_works(self):
        # Removing the account (lifecycle) cascades away its runtime + events despite immutability.
        rt = get_or_create_runtime(self.acct)
        RuntimeEvent.objects.create(runtime=rt, from_state="A", to_state="B")
        self.acct.delete()
        self.assertFalse(AccountRuntime.objects.filter(pk=rt.pk).exists())
        self.assertFalse(RuntimeEvent.objects.filter(runtime_id=rt.pk).exists())

    def test_transition_records_event_and_updates_state(self):
        rt = get_or_create_runtime(self.acct)
        record_transition(rt, RuntimeState.QUEUED, event_type="TRANSITION")
        record_transition(rt, RuntimeState.FAILED, event_type="FAILURE",
                          reason_code="provision_terminal_error", detail="raw agent blah")
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.FAILED)
        self.assertEqual(rt.last_error, "provision_terminal_error")  # sanitised reason, not raw detail
        evs = list(RuntimeEvent.objects.filter(runtime=rt).order_by("id"))
        self.assertEqual([(e.from_state, e.to_state) for e in evs],
                         [("NOT_PROVISIONED", "QUEUED"), ("QUEUED", "FAILED")])

    def test_last_error_cleared_on_healthy_state(self):
        rt = get_or_create_runtime(self.acct)
        record_transition(rt, RuntimeState.FAILED, reason_code="x")
        record_transition(rt, RuntimeState.RUNNING)
        rt.refresh_from_db()
        self.assertEqual(rt.last_error, "")

    def test_user_facing_state_from_durable_record_only(self):
        rt = get_or_create_runtime(self.acct)
        # user-facing state comes from the durable state, not any live process probe
        rt = record_transition(rt, RuntimeState.RUNNING)
        self.assertEqual(user_facing_state(rt), "RUNNING")
        rt = record_transition(rt, RuntimeState.DEGRADED)
        self.assertEqual(user_facing_state(rt), "DEGRADED")


class OnboardingProvisionRecordsFailureTests(TestCase):
    """mark_account_connected must RECORD a provisioning failure on the runtime, not swallow it."""

    def setUp(self):
        self.user = U.objects.create_user(username="ob", email="ob@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="A", account_number="OB1", is_demo=True, is_active=True)

    def test_provision_failure_is_recorded_not_swallowed(self):
        from onboarding import services
        state = services.get_or_create_onboarding_state(self.user)
        # satisfy the prerequisites so account_connected can proceed
        state.plan_selected = True
        state.email_verified = True
        state.risk_accepted = True
        state.save(update_fields=["plan_selected", "email_verified", "risk_accepted"])
        with mock.patch(
                "mt5.services.terminal_provisioning_service.provision_terminal_for_account",
                side_effect=RuntimeError("agent exploded")):
            services.mark_account_connected(self.user)  # must NOT raise (non-blocking)
        rt = AccountRuntime.objects.get(trading_account=self.acct)
        self.assertEqual(rt.state, RuntimeState.FAILED)
        self.assertEqual(rt.last_error, "provision_terminal_error")
        self.assertTrue(RuntimeEvent.objects.filter(
            runtime=rt, event_type="FAILURE", to_state=RuntimeState.FAILED).exists())
