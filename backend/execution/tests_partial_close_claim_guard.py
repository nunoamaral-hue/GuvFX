"""ADR-0055/0056 claim-guard tests for the generic next_job endpoint: a PARTIAL_CLOSE_TRADE job is
UNCLAIMABLE there (served only by its dedicated DEMO consumer, which is not yet built), and an unclaimable
partial-close is classified as UNSUPPORTED — it must NOT emit a misleading NODE_WRONG_CLAIM routing-violation
audit (Part G). Discriminating: removing the base-qs exclusion makes the first test claim it (200, not 204);
removing the audit-qs exclusion makes the second test see a NODE_WRONG_CLAIM."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from execution.models import ExecutionJob, WorkerIdentity
from trading.models import TradingAccount

User = get_user_model()
NEXT_URL = "/api/execution/jobs/next/"


class PartialCloseClaimGuardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pc", email="pc@x.invalid", password="x")
        self.account = TradingAccount.objects.create(
            user=self.user, name="A", account_number="N1", is_demo=True, broker_name="DemoBroker")
        WorkerIdentity.objects.create(
            worker_id="w1", worker_secret_hash=WorkerIdentity.hash_secret("s1"),
            status=WorkerIdentity.Status.ACTIVE)
        ExecutionJob.objects.create(
            account=self.account, job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE,
            status=ExecutionJob.Status.PENDING, payload={"ticket": 1})

    def _claim(self, job_types=None):
        url = NEXT_URL + (f"?job_types={job_types}" if job_types else "")
        return APIClient().get(url, HTTP_X_WORKER_ID="w1", HTTP_X_WORKER_SECRET="s1")

    def test_explicit_request_for_partial_close_is_unclaimable(self):
        resp = self._claim(job_types="PARTIAL_CLOSE_TRADE")
        self.assertEqual(resp.status_code, 204)        # no_jobs — excluded from the claim set

    def test_partial_close_does_not_emit_wrong_node_audit(self):
        with mock.patch("core.audit.log_event") as m_log:
            resp = self._claim(job_types="PARTIAL_CLOSE_TRADE")
        self.assertEqual(resp.status_code, 204)
        wrong_node = [c for c in m_log.call_args_list
                      if (c.kwargs.get("event_type") or (c.args[1] if len(c.args) > 1 else None)) == "NODE_WRONG_CLAIM"]
        self.assertEqual(wrong_node, [], "unclaimable partial-close must not be a NODE_WRONG_CLAIM")
