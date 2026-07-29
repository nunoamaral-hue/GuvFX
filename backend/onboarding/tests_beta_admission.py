"""CVM-Inc-1 / ADR-0021 — controlled beta admission model (retained) is NO LONGER an eligibility gate.

The ``BetaTester`` allowlist and ``is_admitted_beta_tester`` are RETAINED as an operator concept, but
ADR-0021 removed admission as an eligibility gate: it no longer replaces email verification, no longer
auto-grants entitlement on onboarding-state creation, and no longer gates provisioning/arming. Customers
verify email via the genuine email flow. These tests pin that retained-but-inert behaviour.
"""
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model

from billing.beta import is_admitted_beta_tester
from billing.models import BetaTester
from onboarding.services import get_or_create_onboarding_state

U = get_user_model()


def _user(email):
    return U.objects.create_user(username=email, email=email, password="x")


class BetaAdmissionAllowlistTests(TestCase):
    def test_empty_allowlist_admits_nobody_public_stays_closed(self):
        u = _user("public@example.invalid")
        self.assertFalse(is_admitted_beta_tester(u))
        state = get_or_create_onboarding_state(u)
        # Non-allowlisted user: email verification is STILL required (not auto-satisfied) → public closed.
        self.assertFalse(state.email_verified)

    def test_allowlist_no_longer_replaces_email_verification(self):
        # ADR-0021: admission no longer bypasses email verification. The allowlist row still exists and
        # is_admitted_beta_tester is still True, but onboarding-state creation does NOT auto-verify email.
        BetaTester.objects.create(email="tester@example.invalid")
        u = _user("tester@example.invalid")
        self.assertTrue(is_admitted_beta_tester(u))     # the function is retained…
        state = get_or_create_onboarding_state(u)
        self.assertFalse(state.email_verified)          # …but it no longer bypasses email verification

    def test_admission_is_case_insensitive(self):
        BetaTester.objects.create(email="Mixed@Example.invalid")
        u = _user("mixed@example.invalid")
        self.assertTrue(is_admitted_beta_tester(u))

    def test_inactive_allowlist_entry_does_not_admit(self):
        BetaTester.objects.create(email="tester@example.invalid", is_active=False)
        u = _user("tester@example.invalid")
        self.assertFalse(is_admitted_beta_tester(u))
        self.assertFalse(get_or_create_onboarding_state(u).email_verified)

    def test_repeated_onboarding_state_is_stable_noop(self):
        # ADR-0021: repeated onboarding-state creation is a stable no-op that never force-flips email
        # verification (admission does not touch it in either direction).
        BetaTester.objects.create(email="tester@example.invalid")
        u = _user("tester@example.invalid")
        s1 = get_or_create_onboarding_state(u)
        self.assertFalse(s1.email_verified)
        s2 = get_or_create_onboarding_state(u)
        self.assertFalse(s2.email_verified)

    def test_cap_enforced_default_one_active(self):
        BetaTester.objects.create(email="a@example.invalid")
        with self.assertRaises(ValidationError):
            BetaTester.objects.create(email="b@example.invalid")
        # deactivating the first frees the slot
        first = BetaTester.objects.get(email="a@example.invalid")
        first.is_active = False
        first.save()
        BetaTester.objects.create(email="b@example.invalid")  # now allowed
        self.assertEqual(BetaTester.objects.filter(is_active=True).count(), 1)

    @override_settings(BETA_MAX_TESTERS=2)
    def test_cap_is_configurable(self):
        BetaTester.objects.create(email="a@example.invalid")
        BetaTester.objects.create(email="b@example.invalid")  # allowed at cap 2
        with self.assertRaises(ValidationError):
            BetaTester.objects.create(email="c@example.invalid")

    def test_management_command_admits_and_lists(self):
        call_command("admit_beta_tester", "cli@example.invalid", "--note", "cvm")
        self.assertTrue(BetaTester.objects.filter(email="cli@example.invalid", is_active=True).exists())
        call_command("admit_beta_tester", "cli@example.invalid", "--deactivate")
        self.assertFalse(BetaTester.objects.filter(email="cli@example.invalid", is_active=True).exists())

    def test_email_normalised_lowercase_on_save(self):
        bt = BetaTester.objects.create(email="  UPPER@Example.invalid  ")
        self.assertEqual(bt.email, "upper@example.invalid")

    def test_staff_superuser_never_admitted_even_if_allowlisted(self):
        # Estate-safety: a staff/superuser (Nuno) is never admitted, even if their email is on the list.
        BetaTester.objects.create(email="boss@example.invalid")
        boss = _user("boss@example.invalid")
        boss.is_staff = True
        boss.save(update_fields=["is_staff"])
        state = get_or_create_onboarding_state(boss)
        self.assertFalse(state.email_verified)  # admission skipped for staff

    def test_command_refuses_staff_email(self):
        from django.core.management.base import CommandError
        boss = _user("boss2@example.invalid")
        boss.is_superuser = True
        boss.save(update_fields=["is_superuser"])
        with self.assertRaises(CommandError):
            call_command("admit_beta_tester", "boss2@example.invalid")
        self.assertFalse(BetaTester.objects.filter(email="boss2@example.invalid").exists())
