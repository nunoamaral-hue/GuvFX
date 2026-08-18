"""ADR-0034 Execution Engine capstone (P4) — the DARK operator provisioning command.

``provision_hosted_execution`` orchestrates already-tested primitives, but its ONE new security-relevant
step — granting a WorkerIdentity node-awareness by appending the node hostname to
``worker_permissions['authorized_nodes']`` — is exercised here: it must append the CORRECT node, PRESERVE
any existing permissions, be idempotent (no duplicate), and fail closed on an unknown worker. It places no
order and sets no credential.
"""
from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from execution.models import TerminalNode, WorkerIdentity
from execution.readiness import PERSISTENT_WORKSPACE
from trading.models import BrokerServer, TradingAccount

U = get_user_model()


def _account(login="900100"):
    user = U.objects.create_user(username=f"c{login}", email=f"{login}@x.invalid", password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    return TradingAccount.objects.create(
        user=user, name="a", broker_name="B", account_number=login, is_demo=True,
        broker_server=srv, readiness_provider=PERSISTENT_WORKSPACE)


class ProvisionHostedCommandTests(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("provision_hosted_execution", *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_grant_worker_appends_node_and_preserves_existing_perms(self):
        acct = _account()
        # Worker starts with NO node (only an unrelated perm). Per-node isolation forbids granting a
        # node to a worker already on a DIFFERENT node (see test_grant_worker_refuses_cross_node), so
        # the "preserve existing perms" case is proven with an unrelated non-node permission.
        wi = WorkerIdentity.objects.create(
            worker_id="hosted-node-worker", worker_secret_hash="unused",
            worker_permissions={"shadow_worker": False, "authorized_nodes": []})
        self._run("--account-id", str(acct.pk), "--node-hostname", "node-A",
                  "--grant-worker", "hosted-node-worker")
        wi.refresh_from_db()
        # correct node appended, unrelated perm preserved
        self.assertEqual(wi.worker_permissions["authorized_nodes"], ["node-A"])
        self.assertIs(wi.worker_permissions["shadow_worker"], False)
        # and the account+workspace got bound to the node (generation stamped)
        acct.refresh_from_db()
        self.assertEqual(acct.terminal_node.hostname, "node-A")
        self.assertEqual(acct.hosted_workspace.execution_node.hostname, "node-A")
        self.assertGreaterEqual(acct.hosted_workspace.execution_binding_generation, 1)

    def test_grant_worker_refuses_cross_node(self):
        # Per-node isolation (adversarial MEDIUM fix): a dedicated worker serves exactly ONE node.
        # Granting a SECOND, different node to a worker already authorized for node-A must fail closed
        # and mutate nothing (pre-flight, before any binding).
        acct = _account()
        wi = WorkerIdentity.objects.create(
            worker_id="node-a-worker", worker_secret_hash="unused",
            worker_permissions={"authorized_nodes": ["node-A"]})
        with self.assertRaises(CommandError):
            self._run("--account-id", str(acct.pk), "--node-hostname", "node-B",
                      "--grant-worker", "node-a-worker")
        wi.refresh_from_db()
        self.assertEqual(wi.worker_permissions["authorized_nodes"], ["node-A"])   # unchanged
        acct.refresh_from_db()
        self.assertIsNone(acct.terminal_node_id)          # rejected command mutated nothing

    def test_grant_worker_is_idempotent(self):
        acct = _account()
        WorkerIdentity.objects.create(worker_id="w", worker_secret_hash="unused", worker_permissions={})
        self._run("--account-id", str(acct.pk), "--node-hostname", "node-A", "--grant-worker", "w")
        self._run("--account-id", str(acct.pk), "--node-hostname", "node-A", "--grant-worker", "w")
        wi = WorkerIdentity.objects.get(worker_id="w")
        self.assertEqual(wi.worker_permissions["authorized_nodes"], ["node-A"])  # no duplicate

    def test_grant_unknown_worker_fails_closed(self):
        acct = _account()
        with self.assertRaises(CommandError):
            self._run("--account-id", str(acct.pk), "--node-hostname", "node-A",
                      "--grant-worker", "does-not-exist")

    def test_grant_shared_legacy_worker_refused(self):
        # Per-node isolation: the command must REFUSE to grant a node to the shared legacy-worker row (which
        # every legacy X-Worker-Token bridge resolves to). Fail closed; the row is never mutated.
        from execution.auth import LEGACY_WORKER_ID
        acct = _account()
        WorkerIdentity.objects.create(worker_id=LEGACY_WORKER_ID, worker_secret_hash="unused",
                                      worker_permissions={})
        with self.assertRaises(CommandError):
            self._run("--account-id", str(acct.pk), "--node-hostname", "node-A",
                      "--grant-worker", LEGACY_WORKER_ID)
        wi = WorkerIdentity.objects.get(worker_id=LEGACY_WORKER_ID)
        self.assertNotIn("authorized_nodes", wi.worker_permissions)  # never granted

    def test_grant_requires_node_hostname(self):
        # --grant-worker without --node-hostname is a no-op (nothing to authorise), leaving perms untouched.
        acct = _account()
        wi = WorkerIdentity.objects.create(worker_id="w", worker_secret_hash="unused", worker_permissions={})
        self._run("--account-id", str(acct.pk), "--grant-worker", "w")
        wi.refresh_from_db()
        self.assertNotIn("authorized_nodes", wi.worker_permissions)

    def test_missing_account_fails_closed(self):
        with self.assertRaises(CommandError):
            self._run("--account-id", "999999", "--node-hostname", "node-A")
