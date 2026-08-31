"""A5 approval primitive — behaviour + governance tests."""
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import ArtefactApproval
from .services import decide, is_artefact_approved, register_pending

U = get_user_model()
_SHA = "a" * 64
_SHA2 = "b" * 64
_ENABLED = mock.patch.dict(os.environ, {"APPROVALS_ENABLED": "1"})


class ArtefactApprovalTests(TestCase):
    def _staff(self):
        u, _ = U.objects.get_or_create(username="op", defaults={"email": "op@x.invalid", "is_staff": True})
        return u

    def _reg(self, sha=_SHA):
        return register_pending(artefact_kind="broker_servers_dat", artefact_ref="pepperstone/v1", sha256=sha)

    def test_register_is_pending_and_idempotent(self):
        a = self._reg()
        b = self._reg()
        self.assertEqual(a.pk, b.pk)                      # same exact bytes -> one row
        self.assertEqual(a.status, ArtefactApproval.Status.PENDING)

    def test_malformed_sha_refused(self):
        with self.assertRaises(ValueError):
            register_pending(artefact_kind="k", artefact_ref="r", sha256="not-a-sha")

    def test_consumer_fail_closed_when_dark(self):
        self._reg()
        # even after an approval, consumer returns False while the DARK gate is off.
        row = self._reg()
        decide(row, approve=True, decided_by=self._staff())
        self.assertFalse(is_artefact_approved(
            artefact_kind="broker_servers_dat", artefact_ref="pepperstone/v1", sha256=_SHA))

    def test_approved_consumer_true_only_for_exact_sha_when_armed(self):
        row = self._reg()
        decide(row, approve=True, decided_by=self._staff())
        with _ENABLED:
            self.assertTrue(is_artefact_approved(
                artefact_kind="broker_servers_dat", artefact_ref="pepperstone/v1", sha256=_SHA))
            # A DIFFERENT sha for the same ref is NOT approved (binding is to exact bytes).
            self.assertFalse(is_artefact_approved(
                artefact_kind="broker_servers_dat", artefact_ref="pepperstone/v1", sha256=_SHA2))

    def test_decide_requires_staff(self):
        row = self._reg()
        nonstaff = U.objects.create_user(username="u", email="u@x.invalid", password="x", is_staff=False)
        with self.assertRaises(PermissionError):
            decide(row, approve=True, decided_by=nonstaff)
        with self.assertRaises(PermissionError):
            decide(row, approve=True, decided_by=None)
        row.refresh_from_db()
        self.assertEqual(row.status, ArtefactApproval.Status.PENDING)   # unchanged

    def test_no_redecide(self):
        row = self._reg()
        decide(row, approve=False, decided_by=self._staff())
        with self.assertRaises(ValueError):
            decide(row, approve=True, decided_by=self._staff())

    def test_at_most_one_approved_per_ref(self):
        from django.db import IntegrityError
        r1 = self._reg(_SHA); decide(r1, approve=True, decided_by=self._staff())
        r2 = self._reg(_SHA2)
        # Approving a SECOND sha for the same ref violates the one-approved-per-ref constraint (must supersede).
        with self.assertRaises(IntegrityError):
            decide(r2, approve=True, decided_by=self._staff())
