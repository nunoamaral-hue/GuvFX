"""DB tests for hosted_workspace.models.HostedMt5Workspace — lifecycle, immutable binding, secret-free
contract, and the readiness signal (which is NOT the order-time gate)."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from trading.models import TradingAccount

from .models import HostedMt5Workspace, WorkspaceState

U = get_user_model()


class HostedWorkspaceModelTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="ws", email="ws@example.com", password="x")
        self.account = TradingAccount.objects.create(
            user=self.user, name="ws-acct", broker_name="Broker-Demo",
            account_number="500123", is_demo=True)

    def _ws(self, **kw):
        return HostedMt5Workspace.objects.create(trading_account=self.account, **kw)

    def test_defaults_are_dark_and_unprovisioned(self):
        ws = self._ws()
        self.assertEqual(ws.state, WorkspaceState.NOT_PROVISIONED)
        self.assertIsNone(ws.active_account_match)
        self.assertIsNone(ws.observed_connected)
        self.assertFalse(ws.is_execution_ready)
        self.assertEqual(self.account.hosted_workspace, ws)  # OneToOne back-relation

    def test_is_execution_ready_requires_connected_and_match(self):
        ws = self._ws(state=WorkspaceState.CONNECTED, active_account_match=True)
        self.assertTrue(ws.is_execution_ready)
        # A match without CONNECTED, or CONNECTED without a positive match, is NOT ready.
        ws.state = WorkspaceState.ACTIVE_ACCOUNT_MISMATCH
        self.assertFalse(ws.is_execution_ready)
        ws.state = WorkspaceState.CONNECTED
        ws.active_account_match = False
        self.assertFalse(ws.is_execution_ready)
        ws.active_account_match = None
        self.assertFalse(ws.is_execution_ready)

    def test_immutable_binding_is_enforced(self):
        ws = self._ws()
        original_uuid = ws.workspace_uuid
        import uuid as _uuid
        ws.workspace_uuid = _uuid.uuid4()
        with self.assertRaises(ValueError):
            ws.save()
        # A normal field update on the SAME binding is fine.
        ws.refresh_from_db()
        self.assertEqual(ws.workspace_uuid, original_uuid)
        ws.state = WorkspaceState.CONNECTED
        ws.save(update_fields=["state", "updated_at"])
        ws.refresh_from_db()
        self.assertEqual(ws.state, WorkspaceState.CONNECTED)

    def test_contract_is_secret_free_and_masks_login(self):
        ws = self._ws(state=WorkspaceState.CONNECTED, active_account_match=True,
                      currently_attached_login="500123", currently_attached_server="Broker-Demo")
        c = ws.contract()
        # The full login must NEVER appear; only a masked suffix.
        self.assertNotIn("500123", str(c))
        self.assertEqual(c["active_login_masked"], "***123")
        self.assertEqual(c["server"], "Broker-Demo")  # server is not a secret
        self.assertTrue(c["is_execution_ready"])
        # No credential-ish key ever appears in the contract.
        for k in c:
            self.assertNotIn(k.lower(), {"password", "password_enc", "broker_password", "secret", "token"})

    def test_model_has_no_credential_field(self):
        field_names = {f.name for f in HostedMt5Workspace._meta.get_fields()}
        for forbidden in {"password", "password_enc", "broker_password", "windows_password",
                          "windows_password_enc", "secret", "token", "accounts_dat"}:
            self.assertNotIn(forbidden, field_names, f"workspace must not store {forbidden}")
