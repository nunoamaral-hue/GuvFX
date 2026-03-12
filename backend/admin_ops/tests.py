"""
Targeted tests for Admin Operations Console security invariants.

Validates:
  - RBAC enforcement (unauthorized users blocked)
  - Immutability preservation (no write paths for immutable objects)
  - Worker secret lifecycle (one-time display)
  - Override governance (expiry, reason, stacking prevention)
  - State-aware execution actions (retry only FAILED, cancel only PENDING)
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from admin_ops.models import EntitlementOverride
from admin_ops.permissions import (
    ROLE_FINANCE_ADMIN,
    ROLE_OPS_ADMIN,
    ROLE_SUPER_ADMIN,
)
from execution.models import ExecutionJob, WorkerIdentity

User = get_user_model()


def _create_user(email, role=None, is_superuser=False):
    user = User.objects.create_user(
        username=email.split("@")[0],
        email=email,
        password="testpass123",
        is_superuser=is_superuser,
    )
    if role:
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
    return user


class RBACEnforcementTest(TestCase):
    """Validate that RBAC is enforced server-side for each surface."""

    def setUp(self):
        self.client = APIClient()
        self.anon_user = _create_user("anon@test.com")  # no admin role
        self.finance = _create_user("finance@test.com", ROLE_FINANCE_ADMIN)
        self.ops = _create_user("ops@test.com", ROLE_OPS_ADMIN)
        self.super = _create_user("super@test.com", ROLE_SUPER_ADMIN)

    def test_unauthenticated_blocked(self):
        """Unauthenticated requests are rejected."""
        r = self.client.get("/api/admin/reconciliation/events/")
        self.assertIn(r.status_code, [401, 403])

    def test_non_admin_blocked(self):
        """Authenticated users without admin roles are blocked."""
        self.client.force_authenticate(self.anon_user)
        r = self.client.get("/api/admin/reconciliation/events/")
        self.assertEqual(r.status_code, 403)

    def test_finance_blocked_from_workers(self):
        """finance_admin cannot access worker management."""
        self.client.force_authenticate(self.finance)
        r = self.client.get("/api/admin/workers/")
        self.assertEqual(r.status_code, 403)

    def test_finance_blocked_from_entitlements(self):
        """finance_admin cannot access entitlement overrides."""
        self.client.force_authenticate(self.finance)
        r = self.client.get("/api/admin/entitlements/overrides/")
        self.assertEqual(r.status_code, 403)

    def test_ops_blocked_from_payments(self):
        """ops_admin cannot access payment events."""
        self.client.force_authenticate(self.ops)
        r = self.client.get("/api/admin/payments/events/")
        self.assertEqual(r.status_code, 403)

    def test_super_admin_access_all(self):
        """super_admin can access all surfaces."""
        self.client.force_authenticate(self.super)
        for url in [
            "/api/admin/reconciliation/events/",
            "/api/admin/payments/events/",
            "/api/admin/workers/",
            "/api/admin/entitlements/overrides/",
            "/api/admin/execution/jobs/",
        ]:
            r = self.client.get(url)
            self.assertIn(r.status_code, [200], msg=f"Failed for {url}")

    def test_superuser_implicit_super_admin(self):
        """Django is_superuser is treated as implicit super_admin."""
        superuser = _create_user("djsuper@test.com", is_superuser=True)
        self.client.force_authenticate(superuser)
        r = self.client.get("/api/admin/workers/")
        self.assertEqual(r.status_code, 200)


class PaymentEventImmutabilityTest(TestCase):
    """Validate PaymentEvent has no write path through admin API."""

    def setUp(self):
        self.client = APIClient()
        self.user = _create_user("super@test.com", ROLE_SUPER_ADMIN)
        self.client.force_authenticate(self.user)

    def test_no_create_endpoint(self):
        r = self.client.post("/api/admin/payments/events/", {})
        self.assertEqual(r.status_code, 405)

    def test_no_update_endpoint(self):
        r = self.client.put("/api/admin/payments/events/1/", {})
        self.assertEqual(r.status_code, 405)

    def test_no_delete_endpoint(self):
        r = self.client.delete("/api/admin/payments/events/1/")
        self.assertEqual(r.status_code, 405)


class WorkerSecretLifecycleTest(TestCase):
    """Validate worker secrets are shown once and never retrievable."""

    def setUp(self):
        self.client = APIClient()
        self.user = _create_user("super@test.com", ROLE_SUPER_ADMIN)
        self.client.force_authenticate(self.user)

    def test_create_returns_secret(self):
        r = self.client.post("/api/admin/workers/", {
            "worker_id": "test-worker-1",
        })
        self.assertEqual(r.status_code, 201)
        self.assertIn("worker_secret", r.data)
        self.assertTrue(len(r.data["worker_secret"]) > 0)

    def test_detail_does_not_expose_secret(self):
        # Create
        r = self.client.post("/api/admin/workers/", {
            "worker_id": "test-worker-2",
        })
        worker_pk = WorkerIdentity.objects.get(worker_id="test-worker-2").pk

        # Retrieve
        r = self.client.get(f"/api/admin/workers/{worker_pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("worker_secret", r.data)
        self.assertNotIn("worker_secret_hash", r.data)

    def test_rotate_returns_new_secret(self):
        r = self.client.post("/api/admin/workers/", {
            "worker_id": "test-worker-3",
        })
        worker_pk = WorkerIdentity.objects.get(worker_id="test-worker-3").pk
        original_secret = r.data["worker_secret"]

        r = self.client.post(f"/api/admin/workers/{worker_pk}/rotate-secret/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("worker_secret", r.data)
        self.assertNotEqual(r.data["worker_secret"], original_secret)

    def test_duplicate_worker_id_rejected(self):
        self.client.post("/api/admin/workers/", {"worker_id": "dup-worker"})
        r = self.client.post("/api/admin/workers/", {"worker_id": "dup-worker"})
        self.assertEqual(r.status_code, 409)


class EntitlementOverrideGovernanceTest(TestCase):
    """Validate override rules: expiry, reason, stacking."""

    def setUp(self):
        self.client = APIClient()
        self.admin = _create_user("super@test.com", ROLE_SUPER_ADMIN)
        self.target = _create_user("target@test.com")
        self.client.force_authenticate(self.admin)

    def test_past_expiry_rejected(self):
        r = self.client.post("/api/admin/entitlements/overrides/", {
            "user_id": self.target.id,
            "capability": "can_deploy_automation",
            "reason": "testing",
            "expires_at": (timezone.now() - timedelta(hours=1)).isoformat(),
        })
        self.assertEqual(r.status_code, 400)

    def test_reason_required(self):
        r = self.client.post("/api/admin/entitlements/overrides/", {
            "user_id": self.target.id,
            "capability": "can_deploy_automation",
            "expires_at": (timezone.now() + timedelta(hours=1)).isoformat(),
        })
        self.assertEqual(r.status_code, 400)

    def test_no_stacking(self):
        """Same capability for same user cannot be stacked."""
        payload = {
            "user_id": self.target.id,
            "capability": "can_deploy_automation",
            "reason": "first",
            "expires_at": (timezone.now() + timedelta(hours=1)).isoformat(),
        }
        r1 = self.client.post("/api/admin/entitlements/overrides/", payload)
        self.assertEqual(r1.status_code, 201)

        payload["reason"] = "second"
        r2 = self.client.post("/api/admin/entitlements/overrides/", payload)
        self.assertEqual(r2.status_code, 409)


class ExecutionJobDiagnosticsTest(TestCase):
    """Validate state-aware retry/cancel actions."""

    def setUp(self):
        from trading.models import TradingAccount, BrokerServer

        self.client = APIClient()
        self.admin = _create_user("super@test.com", ROLE_SUPER_ADMIN)
        self.owner = _create_user("owner@test.com")
        self.client.force_authenticate(self.admin)

        # Create minimal test data
        server = BrokerServer.objects.create(
            broker_display_name="TestBroker",
            server_name="TestServer",
            environment="demo",
        )
        self.account = TradingAccount.objects.create(
            user=self.owner,
            broker_server=server,
            mt5_login="12345",
            is_active=True,
        )

    def _create_job(self, status_val):
        return ExecutionJob.objects.create(
            job_type=ExecutionJob.JobType.TEST_CONNECTION,
            account=self.account,
            payload={"test": True},
            status=status_val,
        )

    def test_retry_only_failed(self):
        job = self._create_job(ExecutionJob.Status.FAILED)
        r = self.client.post(f"/api/admin/execution/jobs/{job.id}/retry/",
                             {"reason": "retry test"})
        self.assertEqual(r.status_code, 201)

    def test_retry_pending_rejected(self):
        job = self._create_job(ExecutionJob.Status.PENDING)
        r = self.client.post(f"/api/admin/execution/jobs/{job.id}/retry/",
                             {"reason": "should fail"})
        self.assertEqual(r.status_code, 409)

    def test_retry_success_rejected(self):
        job = self._create_job(ExecutionJob.Status.SUCCESS)
        r = self.client.post(f"/api/admin/execution/jobs/{job.id}/retry/",
                             {"reason": "should fail"})
        self.assertEqual(r.status_code, 409)

    def test_cancel_only_pending(self):
        job = self._create_job(ExecutionJob.Status.PENDING)
        r = self.client.post(f"/api/admin/execution/jobs/{job.id}/cancel/",
                             {"reason": "cancel test"})
        self.assertEqual(r.status_code, 200)

    def test_cancel_running_rejected(self):
        job = self._create_job(ExecutionJob.Status.RUNNING)
        r = self.client.post(f"/api/admin/execution/jobs/{job.id}/cancel/",
                             {"reason": "should fail"})
        self.assertEqual(r.status_code, 409)

    def test_cancel_finished_rejected(self):
        job = self._create_job(ExecutionJob.Status.SUCCESS)
        r = self.client.post(f"/api/admin/execution/jobs/{job.id}/cancel/",
                             {"reason": "should fail"})
        self.assertEqual(r.status_code, 409)

    def test_finance_cannot_retry(self):
        """finance_admin gets 403 on retry."""
        finance = _create_user("fin@test.com", ROLE_FINANCE_ADMIN)
        self.client.force_authenticate(finance)
        job = self._create_job(ExecutionJob.Status.FAILED)
        r = self.client.post(f"/api/admin/execution/jobs/{job.id}/retry/",
                             {"reason": "should fail"})
        self.assertEqual(r.status_code, 403)

    def test_retry_preserves_original(self):
        """Retry creates a new job, original is untouched."""
        job = self._create_job(ExecutionJob.Status.FAILED)
        r = self.client.post(f"/api/admin/execution/jobs/{job.id}/retry/",
                             {"reason": "retry"})
        self.assertEqual(r.status_code, 201)

        # Original remains FAILED
        job.refresh_from_db()
        self.assertEqual(job.status, ExecutionJob.Status.FAILED)

        # New job is PENDING
        new_job = ExecutionJob.objects.get(pk=r.data["new_job_id"])
        self.assertEqual(new_job.status, ExecutionJob.Status.PENDING)
        self.assertEqual(new_job.payload, job.payload)
