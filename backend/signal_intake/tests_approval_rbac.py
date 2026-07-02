"""
GFX-PKT-E3-APPROVAL-RBAC tests.

Approving/rejecting a signal requires the dedicated ``review_signals`` permission:
an ordinary staff/admin user is DENIED (fail-closed, with a persisted
APPROVAL_DENIED audit and no status change); a granted reviewer or superuser
succeeds (with the who-did-it audit); the admin actions are hidden from
unauthorised staff; and the grant command grants/revokes idempotently.
"""

from io import StringIO
from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import RequestFactory, TestCase

from signal_intake import services
from signal_intake.admin import PendingSignalApprovalAdmin
from signal_intake.models import PendingSignalApproval, SignalAuditEvent

User = get_user_model()
SRC = PendingSignalApproval.Source.WAYOND_TELEGRAM


def _perm():
    return Permission.objects.get(
        codename="review_signals", content_type__app_label="signal_intake"
    )


class ApprovalRbacTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="plain-staff", email="staff@x.invalid", password="x", is_staff=True
        )
        self.reviewer = User.objects.create_user(
            username="reviewer", email="rev@x.invalid", password="x", is_staff=True
        )
        self.reviewer.user_permissions.add(_perm())
        self.reviewer = User.objects.get(pk=self.reviewer.pk)  # refresh perm cache
        self.superuser = User.objects.create_superuser(
            username="root", email="root@x.invalid", password="x"
        )

    def _pending(self, mid="m1"):
        return PendingSignalApproval.objects.create(
            source=SRC, message_id=mid, symbol="EURUSD", direction="BUY",
            status=PendingSignalApproval.Status.PENDING_APPROVAL,
        )

    # --- service-layer gate -------------------------------------------------
    def test_plain_staff_cannot_approve(self):
        a = self._pending()
        with self.assertRaises(services.ReviewPermissionDenied):
            services.approve(a, reviewer=self.staff)
        a.refresh_from_db()
        self.assertEqual(a.status, PendingSignalApproval.Status.PENDING_APPROVAL)
        # the refused attempt is audited (and survived the raise)
        denied = SignalAuditEvent.objects.filter(
            approval=a, event=SignalAuditEvent.Event.APPROVAL_DENIED
        )
        self.assertTrue(denied.exists())
        self.assertEqual(denied.first().actor, self.staff)

    def test_plain_staff_cannot_reject(self):
        a = self._pending()
        with self.assertRaises(services.ReviewPermissionDenied):
            services.reject(a, reviewer=self.staff)
        a.refresh_from_db()
        self.assertEqual(a.status, PendingSignalApproval.Status.PENDING_APPROVAL)

    def test_none_reviewer_denied_fail_closed(self):
        a = self._pending()
        with self.assertRaises(services.ReviewPermissionDenied):
            services.approve(a, reviewer=None)

    def test_permission_error_denies_fail_closed(self):
        # has_perm blowing up (indeterminate state) must deny, never allow.
        broken = mock.Mock()
        broken.is_active = True
        broken.pk = None
        broken.has_perm.side_effect = RuntimeError("auth backend down")
        self.assertFalse(services.can_review(broken))

    def test_granted_reviewer_can_approve_with_audit(self):
        a = self._pending()
        services.approve(a, reviewer=self.reviewer, notes="ok")
        a.refresh_from_db()
        self.assertEqual(a.status, PendingSignalApproval.Status.APPROVED)
        self.assertEqual(a.reviewer, self.reviewer)  # who approved is recorded
        audit = SignalAuditEvent.objects.get(
            approval=a, event=SignalAuditEvent.Event.SIGNAL_APPROVED
        )
        self.assertEqual(audit.actor, self.reviewer)

    def test_superuser_can_approve(self):
        a = self._pending()
        services.approve(a, reviewer=self.superuser)
        a.refresh_from_db()
        self.assertEqual(a.status, PendingSignalApproval.Status.APPROVED)

    def test_inactive_reviewer_denied(self):
        self.reviewer.is_active = False
        self.reviewer.save(update_fields=["is_active"])
        self.reviewer = User.objects.get(pk=self.reviewer.pk)
        a = self._pending()
        with self.assertRaises(services.ReviewPermissionDenied):
            services.approve(a, reviewer=self.reviewer)

    # --- admin action gating ------------------------------------------------
    def _admin_actions_for(self, user):
        ma = PendingSignalApprovalAdmin(PendingSignalApproval, AdminSite())
        request = RequestFactory().get("/admin/")
        request.user = user
        return set(ma.get_actions(request).keys())

    def test_admin_actions_hidden_without_permission(self):
        actions = self._admin_actions_for(self.staff)
        self.assertNotIn("action_approve", actions)
        self.assertNotIn("action_reject", actions)

    def test_admin_actions_visible_with_permission(self):
        actions = self._admin_actions_for(self.reviewer)
        self.assertIn("action_approve", actions)
        self.assertIn("action_reject", actions)

    # --- grant command --------------------------------------------------------
    def test_grant_command_grants_and_revokes(self):
        out = StringIO()
        call_command("grant_signal_reviewer", "plain-staff", stdout=out)
        self.assertTrue(User.objects.get(pk=self.staff.pk).has_perm(services.REVIEW_PERMISSION))
        call_command("grant_signal_reviewer", "plain-staff", "--revoke", stdout=out)
        self.assertFalse(User.objects.get(pk=self.staff.pk).has_perm(services.REVIEW_PERMISSION))
