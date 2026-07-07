"""
EXEC-E1a tests — approval → ProposedSignalOrder bridge.

Central claim under test: creating a proposal NEVER creates an ExecutionJob,
never places an order, and is structurally invisible to the worker claim path.
Plus the gates: demo-only, functional kill switch, signal-specific disable,
env-var kill switch, symbol allowlist, lot cap, daily/concurrent caps,
duplicate protection, approved-only, and the audit chain.
"""

import importlib
import pathlib
import re
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase

from execution import signal_proposals as bridge
from execution.models import (
    SIGNAL_ALLOWED_SYMBOLS,
    SIGNAL_MAX_CONCURRENT_POSITIONS,
    SIGNAL_MAX_LOT_SIZE,
    SIGNAL_MAX_TRADES_PER_DAY,
    ExecutionControl,
    ExecutionJob,
    ProposalAuditEvent,
    ProposedSignalOrder,
)
from signal_intake.models import PendingSignalApproval
from trading.models import BrokerServer, TradingAccount

User = get_user_model()


def _approval(message_id, *, symbol="EURUSD", direction="BUY",
              status=PendingSignalApproval.Status.APPROVED):
    return PendingSignalApproval.objects.create(
        source=PendingSignalApproval.Source.WAYOND_TELEGRAM,
        message_id=message_id,
        symbol=symbol,
        direction=direction,
        entry="1.0850",
        stop_loss="1.0800",
        take_profit="1.0900",
        status=status,
    )


class ProposalBridgeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="op", email="op@example.invalid", password="x"
        )
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True
        )

    # ----- the central no-order guarantees -------------------------------

    def test_proposal_creates_no_execution_job(self):
        before = ExecutionJob.objects.count()
        proposal = bridge.propose_order_from_approval(
            _approval("m1"), account=self.demo, actor=self.user
        )
        self.assertIsInstance(proposal, ProposedSignalOrder)
        self.assertNotIsInstance(proposal, ExecutionJob)
        self.assertEqual(ExecutionJob.objects.count(), before)
        self.assertEqual(ExecutionJob.objects.count(), 0)
        self.assertEqual(proposal.status, ProposedSignalOrder.Status.PROPOSED)

    def test_proposal_invisible_to_worker_claim_path(self):
        bridge.propose_order_from_approval(_approval("m2"), account=self.demo)
        # The worker claims via this exact queryset (execution.views.next_job).
        claimable = ExecutionJob.objects.filter(status=ExecutionJob.Status.PENDING)
        self.assertEqual(claimable.count(), 0)
        # A proposal is not in the ExecutionJob table at all.
        self.assertEqual(ExecutionJob.objects.count(), 0)
        self.assertEqual(ProposedSignalOrder.objects.count(), 1)

    def test_proposal_has_no_pending_claimable_status(self):
        # ProposedSignalOrder must not expose any status a worker could claim.
        statuses = {s for s, _ in ProposedSignalOrder.Status.choices}
        self.assertNotIn("PENDING", statuses)
        self.assertNotIn("RUNNING", statuses)

    # ----- demo-only ------------------------------------------------------

    def test_live_flag_account_rejected(self):
        live = TradingAccount.objects.create(
            user=self.user, name="Live", account_number="L1", is_demo=False
        )
        with self.assertRaises(bridge.ProposalRejected) as ctx:
            bridge.propose_order_from_approval(_approval("m3"), account=live, actor=self.user)
        self.assertEqual(ctx.exception.code, "account_not_demo")
        self.assertEqual(ProposedSignalOrder.objects.count(), 0)
        self.assertTrue(self._rejected("account_not_demo"))

    def test_live_broker_environment_rejected(self):
        server = BrokerServer.objects.create(
            broker_display_name="LiveBroker", server_name="live-1", environment=BrokerServer.LIVE
        )
        acct = TradingAccount.objects.create(
            user=self.user, name="DemoFlagButLiveServer", account_number="X1",
            is_demo=True, broker_server=server,
        )
        with self.assertRaises(bridge.ProposalRejected) as ctx:
            bridge.propose_order_from_approval(_approval("m4"), account=acct, actor=self.user)
        self.assertEqual(ctx.exception.code, "account_live")
        self.assertEqual(ProposedSignalOrder.objects.count(), 0)

    # ----- kill switch / disable -----------------------------------------

    def test_kill_switch_blocks_proposal(self):
        bridge.engage_kill_switch(actor=self.user, reason="test")
        with self.assertRaises(bridge.ProposalRejected) as ctx:
            bridge.propose_order_from_approval(_approval("m5"), account=self.demo)
        self.assertEqual(ctx.exception.code, "kill_switch_engaged")
        self.assertEqual(ProposedSignalOrder.objects.count(), 0)

    def test_release_kill_switch_allows_again(self):
        bridge.engage_kill_switch(actor=self.user)
        bridge.release_kill_switch(actor=self.user)
        p = bridge.propose_order_from_approval(_approval("m6"), account=self.demo)
        self.assertEqual(p.status, ProposedSignalOrder.Status.PROPOSED)

    def test_signal_proposals_disabled_blocks(self):
        bridge.set_signal_proposals_enabled(False, actor=self.user)
        with self.assertRaises(bridge.ProposalRejected) as ctx:
            bridge.propose_order_from_approval(_approval("m7"), account=self.demo)
        self.assertEqual(ctx.exception.code, "signal_proposals_disabled")

    def test_env_var_kill_switch_blocks(self):
        with self.settings():  # no-op; env via override below
            import os
            os.environ["GUVFX_EXECUTION_DISABLED"] = "true"
            try:
                with self.assertRaises(bridge.ProposalRejected) as ctx:
                    bridge.propose_order_from_approval(_approval("m8"), account=self.demo)
                self.assertEqual(ctx.exception.code, "execution_globally_disabled")
            finally:
                del os.environ["GUVFX_EXECUTION_DISABLED"]

    def test_kill_all_view_engages_switch(self):
        from rest_framework.test import APIClient
        from django.urls import reverse

        admin = User.objects.create_user(
            username="admin", email="admin@example.invalid", password="x",
            is_staff=True, is_superuser=True,
        )
        client = APIClient()
        client.force_authenticate(user=admin)
        resp = client.post(reverse("execution-kill-all"), {"reason": "drill"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["kill_switch_engaged"])
        self.assertTrue(ExecutionControl.get_solo().kill_switch_engaged)
        with self.assertRaises(bridge.ProposalRejected):
            bridge.propose_order_from_approval(_approval("m9"), account=self.demo)

    # ----- allowlist / caps ----------------------------------------------

    def test_symbol_not_in_allowlist_rejected(self):
        # Demo account has no synced broker instruments -> default baseline; BTCUSD is not in it,
        # so the broker-symbol registry rejects it fail-closed (no longer a hardcoded allowlist).
        self.assertNotIn("BTCUSD", SIGNAL_ALLOWED_SYMBOLS)
        with self.assertRaises(bridge.ProposalRejected) as ctx:
            bridge.propose_order_from_approval(
                _approval("m10", symbol="BTCUSD"), account=self.demo
            )
        self.assertEqual(ctx.exception.code, "SYMBOL_NOT_AVAILABLE_ON_BROKER")

    def test_lot_above_cap_rejected(self):
        over = Decimal(str(SIGNAL_MAX_LOT_SIZE)) + Decimal("0.01")
        with self.assertRaises(bridge.ProposalRejected) as ctx:
            bridge.propose_order_from_approval(
                _approval("m11"), account=self.demo, lot_size=over
            )
        self.assertEqual(ctx.exception.code, "lot_exceeds_cap")

    def test_lot_at_cap_allowed(self):
        p = bridge.propose_order_from_approval(
            _approval("m12"), account=self.demo, lot_size=SIGNAL_MAX_LOT_SIZE
        )
        self.assertEqual(p.lot_size, Decimal(str(SIGNAL_MAX_LOT_SIZE)))

    def test_concurrent_cap_rejected(self):
        bridge.propose_order_from_approval(_approval("c1"), account=self.demo)
        # one PROPOSED already; SIGNAL_MAX_CONCURRENT_POSITIONS == 1
        self.assertEqual(SIGNAL_MAX_CONCURRENT_POSITIONS, 1)
        with self.assertRaises(bridge.ProposalRejected) as ctx:
            bridge.propose_order_from_approval(_approval("c2"), account=self.demo)
        self.assertEqual(ctx.exception.code, "concurrent_limit_exceeded")

    def test_daily_cap_rejected(self):
        # Pre-seed SIGNAL_MAX_TRADES_PER_DAY superseded proposals (count toward
        # the daily total but free the concurrency slot).
        for i in range(SIGNAL_MAX_TRADES_PER_DAY):
            ProposedSignalOrder.objects.create(
                approval=_approval(f"d{i}"), account=self.demo, symbol="EURUSD",
                direction="BUY", lot_size=Decimal("0.01"), is_demo=True,
                status=ProposedSignalOrder.Status.SUPERSEDED,
            )
        with self.assertRaises(bridge.ProposalRejected) as ctx:
            bridge.propose_order_from_approval(_approval("dN"), account=self.demo)
        self.assertEqual(ctx.exception.code, "daily_limit_exceeded")

    # ----- duplicate / approved-only -------------------------------------

    def test_duplicate_proposal_rejected(self):
        a = _approval("dup1")
        bridge.propose_order_from_approval(a, account=self.demo)
        with self.assertRaises(bridge.ProposalRejected) as ctx:
            bridge.propose_order_from_approval(a, account=self.demo)
        self.assertEqual(ctx.exception.code, "duplicate_proposal")
        self.assertEqual(ProposedSignalOrder.objects.filter(approval=a).count(), 1)

    def test_unapproved_approval_rejected(self):
        pending = _approval("pa1", status=PendingSignalApproval.Status.PENDING_APPROVAL)
        with self.assertRaises(bridge.ProposalRejected) as ctx:
            bridge.propose_order_from_approval(pending, account=self.demo)
        self.assertEqual(ctx.exception.code, "approval_not_approved")

    # ----- audit chain ----------------------------------------------------

    def test_created_writes_audit_linked_to_approval(self):
        a = _approval("au1")
        p = bridge.propose_order_from_approval(a, account=self.demo, actor=self.user)
        self.assertTrue(
            ProposalAuditEvent.objects.filter(
                event=ProposalAuditEvent.Event.PROPOSAL_CREATED,
                proposal=p, approval=a,
            ).exists()
        )

    def test_rejection_audit_persists(self):
        before = ProposalAuditEvent.objects.count()
        with self.assertRaises(bridge.ProposalRejected):
            bridge.propose_order_from_approval(
                _approval("au2", symbol="BTCUSD"), account=self.demo, actor=self.user
            )
        self.assertEqual(
            ProposalAuditEvent.objects.filter(
                event=ProposalAuditEvent.Event.PROPOSAL_REJECTED
            ).count(),
            before + 1,
        )

    def _rejected(self, code):
        return ProposalAuditEvent.objects.filter(
            event=ProposalAuditEvent.Event.PROPOSAL_REJECTED, detail__code=code
        ).exists()


class ManagementCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="op2", email="op2@example.invalid", password="x"
        )
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D2", is_demo=True
        )

    def test_command_creates_proposal_and_no_jobs(self):
        a = _approval("cmd1")
        out = StringIO()
        call_command(
            "propose_signal_order", "--approval", str(a.id), "--account",
            str(self.demo.id), stdout=out, stderr=StringIO(),
        )
        output = out.getvalue()
        self.assertIn("0 ExecutionJobs created", output)
        self.assertIn("0 orders placed", output)
        self.assertEqual(ExecutionJob.objects.count(), 0)
        self.assertEqual(ProposedSignalOrder.objects.count(), 1)

    def test_command_rejects_live_account_and_creates_no_jobs(self):
        live = TradingAccount.objects.create(
            user=self.user, name="Live", account_number="L2", is_demo=False
        )
        a = _approval("cmd2")
        with self.assertRaises(CommandError):
            call_command(
                "propose_signal_order", "--approval", str(a.id), "--account",
                str(live.id), stdout=StringIO(), stderr=StringIO(),
            )
        self.assertEqual(ExecutionJob.objects.count(), 0)
        self.assertEqual(ProposedSignalOrder.objects.count(), 0)


class NoOrderStaticGuardTests(TestCase):
    """Static proof that the bridge cannot create an order or ExecutionJob."""

    ORDER_CALL = re.compile(
        r"\bcreate_open_trade_job\s*\(|\border_send\s*\(|"
        r"\bExecutionJob\s*\(|\bExecutionJob\.objects\.(create|get_or_create|"
        r"bulk_create|update_or_create)\b"
    )

    def _src(self, module):
        return pathlib.Path(importlib.import_module(module).__file__).read_text()

    @staticmethod
    def _code_names(src):
        """Identifiers actually used in code (AST) — ignores docstrings/comments."""
        import ast

        names = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                for n in node.names:
                    names.add(n.asname or n.name)
            elif isinstance(node, ast.Import):
                for n in node.names:
                    names.add((n.asname or n.name).split(".")[0])
        return names

    def test_bridge_module_never_references_execution_job_or_orders(self):
        src = self._src("execution.signal_proposals")
        names = self._code_names(src)
        # The bridge neither imports nor references ExecutionJob or any order fn
        # in CODE (the words appear only in the explanatory docstring).
        self.assertNotIn("ExecutionJob", names)
        self.assertNotIn("create_open_trade_job", names)
        self.assertNotIn("order_send", names)
        self.assertIsNone(self.ORDER_CALL.search(src))

    def test_e1a_modules_create_no_orders(self):
        for module in (
            "execution.signal_proposals",
            "execution.management.commands.propose_signal_order",
            "execution.admin",
        ):
            src = self._src(module)
            self.assertIsNone(
                self.ORDER_CALL.search(src),
                f"E1a module {module} contains an order/ExecutionJob creation call",
            )
