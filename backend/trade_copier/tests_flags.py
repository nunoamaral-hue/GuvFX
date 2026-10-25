"""Copier DARK-flag gating — default OFF, master ANDs subsystem, settings override wins."""
from django.test import SimpleTestCase, override_settings

from trade_copier import flags


class CopierFlagTests(SimpleTestCase):
    def test_default_off(self):
        self.assertFalse(flags.copier_enabled())
        self.assertFalse(flags.copier_observer_enabled())

    @override_settings(COPIER_ENABLED=True)
    def test_observer_requires_both_flags(self):
        # Master on, observer sub-flag off -> still OFF (cannot half-activate).
        self.assertTrue(flags.copier_enabled())
        self.assertFalse(flags.copier_observer_enabled())

    @override_settings(COPIER_ENABLED=True, COPIER_OBSERVER_ENABLED=True)
    def test_observer_on_only_when_both(self):
        self.assertTrue(flags.copier_observer_enabled())

    @override_settings(COPIER_ENABLED=False, COPIER_OBSERVER_ENABLED=True)
    def test_master_off_keeps_observer_off(self):
        self.assertFalse(flags.copier_observer_enabled())

    @override_settings(COPIER_ENABLED="yes")
    def test_tolerant_truthy_tokens(self):
        self.assertTrue(flags.copier_enabled())

    def test_replication_default_off(self):
        self.assertFalse(flags.copier_replication_enabled())

    @override_settings(COPIER_ENABLED=True, COPIER_REPLICATION_ENABLED=True)
    def test_replication_requires_both(self):
        self.assertTrue(flags.copier_replication_enabled())

    @override_settings(COPIER_ENABLED=False, COPIER_REPLICATION_ENABLED=True)
    def test_replication_master_off_keeps_off(self):
        self.assertFalse(flags.copier_replication_enabled())


class _FakeUser:
    def __init__(self, email, authenticated=True):
        self.email = email
        self.is_authenticated = authenticated


class CopierUiCohortTests(SimpleTestCase):
    """Founder/operator UI cohort gate — default-DENY allow-list, no global-flag footgun."""

    def test_allowlist_empty_by_default_denies_everyone(self):
        self.assertEqual(flags.copier_ui_cohort_allowlist(), frozenset())
        self.assertFalse(flags.copier_ui_enabled_for(_FakeUser("support@guvfx.com")))

    @override_settings(COPIER_UI_COHORT_ALLOWLIST="support@guvfx.com")
    def test_listed_email_allowed_others_denied(self):
        self.assertTrue(flags.copier_ui_enabled_for(_FakeUser("support@guvfx.com")))
        self.assertFalse(flags.copier_ui_enabled_for(_FakeUser("intruder@x.invalid")))

    @override_settings(COPIER_UI_COHORT_ALLOWLIST="  Support@GuvFX.com , other@x.invalid ")
    def test_match_case_insensitive_and_trimmed(self):
        self.assertTrue(flags.copier_ui_enabled_for(_FakeUser("support@guvfx.com")))
        self.assertTrue(flags.copier_ui_enabled_for(_FakeUser("OTHER@X.INVALID")))

    @override_settings(COPIER_UI_COHORT_ALLOWLIST="support@guvfx.com")
    def test_anonymous_or_missing_user_denied(self):
        self.assertFalse(flags.copier_ui_enabled_for(None))
        self.assertFalse(flags.copier_ui_enabled_for(_FakeUser("support@guvfx.com", authenticated=False)))
        self.assertFalse(flags.copier_ui_enabled_for(_FakeUser("", authenticated=True)))

    @override_settings(COPIER_UI_ENABLED=True, COPIER_UI_COHORT_ALLOWLIST="")
    def test_no_global_flag_footgun(self):
        # The legacy global COPIER_UI_ENABLED must NOT open the per-user gate to a non-listed user.
        self.assertTrue(flags.copier_ui_enabled())              # global is on
        self.assertFalse(flags.copier_ui_enabled_for(_FakeUser("anyone@x.invalid")))  # still denied
