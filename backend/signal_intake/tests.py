"""
EXEC-E0 tests — shadow Telegram signal intake.

Assert: parsed signals become PendingSignalApproval rows; dedup by message_id is
idempotent; unparseable messages are quarantined; approve/reject change status
only; NO ExecutionJob can be created; and the ADR-009 boundary holds (this app
and wims/intelligence never import execution / create orders).
"""

import pathlib
import re
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db.utils import IntegrityError
from django.db import transaction
from django.test import TestCase

from intelligence.telegram_source import parse_message
from signal_intake import services
from signal_intake.models import PendingSignalApproval, SignalAuditEvent

User = get_user_model()

SIGNAL_MSG = (
    "XAUUSD | Potential downward movement\n\nXAUUSD | SELL 3350.0\n\n"
    "❌ Stop Loss 3360.0 (100 pips)\n\n✅ TP1 3335.0\n✅ TP2 3320.0"
)


class IntakeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="op", email="op@example.invalid", password="x"
        )
        # E3-APPROVAL-RBAC: approve/reject now require the review_signals
        # permission; grant it to the test operator (and refresh the perm cache).
        from django.contrib.auth.models import Permission
        self.user.user_permissions.add(
            Permission.objects.get(codename="review_signals",
                                   content_type__app_label="signal_intake")
        )
        self.user = User.objects.get(pk=self.user.pk)

    def test_parsed_signal_becomes_pending_approval(self):
        a = services.intake_message(SIGNAL_MSG, "m1", actor=self.user)
        self.assertEqual(a.status, PendingSignalApproval.Status.PENDING_APPROVAL)
        self.assertEqual(a.symbol, "XAUUSD")
        self.assertEqual(a.direction, "SELL")
        self.assertEqual(a.entry, "3350.0")
        self.assertEqual(a.stop_loss, "3360.0")
        self.assertEqual(a.take_profit, "3335.0")
        self.assertTrue(
            SignalAuditEvent.objects.filter(
                approval=a, event=SignalAuditEvent.Event.SIGNAL_RECEIVED
            ).exists()
        )

    def test_dedup_is_idempotent(self):
        a1 = services.intake_message(SIGNAL_MSG, "dup1", actor=self.user)
        a2 = services.intake_message(SIGNAL_MSG, "dup1", actor=self.user)
        self.assertEqual(a1.id, a2.id)
        self.assertEqual(PendingSignalApproval.objects.filter(message_id="dup1").count(), 1)

    def test_dedup_db_constraint(self):
        services.intake_message(SIGNAL_MSG, "uc1", actor=self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PendingSignalApproval.objects.create(
                source=PendingSignalApproval.Source.WAYOND_TELEGRAM,
                message_id="uc1", symbol="X",
            )

    def test_unparseable_message_is_quarantined(self):
        a = services.intake_message("Good luck everyone, trade safe.", "m2", actor=self.user)
        self.assertEqual(a.status, PendingSignalApproval.Status.QUARANTINED)
        self.assertTrue(
            SignalAuditEvent.objects.filter(
                approval=a, event=SignalAuditEvent.Event.SIGNAL_QUARANTINED
            ).exists()
        )

    def test_tp_hit_update_is_not_a_tradeable_signal(self):
        p = parse_message("TP1 hit! +150 pips. Move SL to 3350.0", "m3")
        a = services.intake_parsed(p, actor=self.user)
        self.assertEqual(a.status, PendingSignalApproval.Status.QUARANTINED)

    def test_approve_changes_status_only_no_order(self):
        a = services.intake_message(SIGNAL_MSG, "ap1", actor=self.user)
        services.approve(a, reviewer=self.user, notes="ok")
        a.refresh_from_db()
        self.assertEqual(a.status, PendingSignalApproval.Status.APPROVED)
        self.assertEqual(a.reviewer, self.user)
        self.assertIsNotNone(a.reviewed_at)
        self.assertTrue(
            SignalAuditEvent.objects.filter(
                approval=a, event=SignalAuditEvent.Event.SIGNAL_APPROVED
            ).exists()
        )

    def test_reject_changes_status_only(self):
        a = services.intake_message(SIGNAL_MSG, "rj1", actor=self.user)
        services.reject(a, reviewer=self.user, notes="no")
        a.refresh_from_db()
        self.assertEqual(a.status, PendingSignalApproval.Status.REJECTED)

    def test_batch_ingest_dedups_and_quarantines(self):
        msgs = [
            {"message_id": "b1", "text": SIGNAL_MSG},
            {"message_id": "b1", "text": SIGNAL_MSG},      # duplicate
            {"message_id": "b2", "text": "random chatter"},
        ]
        res = services.ingest_messages(msgs, actor=self.user)
        self.assertEqual(len(res["created"]), 1)
        self.assertEqual(len(res["quarantined"]), 1)
        self.assertEqual(res["duplicates_skipped"], 1)

    def test_command_creates_no_execution_jobs(self):
        out = StringIO()
        call_command("ingest_wayond_signals_for_approval", stdout=out, stderr=StringIO())
        output = out.getvalue()
        self.assertIn("0 ExecutionJobs created", output)
        self.assertGreaterEqual(PendingSignalApproval.objects.count(), 1)


class Adr009BoundaryGuardTests(TestCase):
    """Static guard: the signal-intake + content apps never reach into execution.

    Imports are checked via AST (definitive, ignores strings/comments); order
    calls via a precise regex that cannot match prose like "create an
    ExecutionJob" (we require an actual ``(`` or ``.objects``).
    """

    # Real usage only — `ExecutionJob(`, `ExecutionJob.objects`, or order calls.
    CALL = re.compile(
        r"\bcreate_open_trade_job\s*\(|\border_send\s*\(|"
        r"\bExecutionJob\s*\(|\bExecutionJob\.objects\b"
    )

    def _app_dir(self, app):
        import importlib
        return pathlib.Path(importlib.import_module(app).__file__).parent

    @staticmethod
    def _imports_execution(source: str) -> bool:
        import ast
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(n.name == "execution" or n.name.startswith("execution.")
                       for n in node.names):
                    return True
            elif isinstance(node, ast.ImportFrom):
                m = node.module or ""
                if m == "execution" or m.startswith("execution."):
                    return True
        return False

    def _assert_clean(self, app):
        for py in self._app_dir(app).rglob("*.py"):
            if py.name.startswith("test"):
                continue
            src = py.read_text()
            self.assertFalse(self._imports_execution(src),
                             f"ADR-009: {app}/{py.name} imports the execution app")
            self.assertIsNone(self.CALL.search(src),
                              f"ADR-009: {app}/{py.name} creates an order/ExecutionJob")

    def test_signal_intake_does_not_import_or_create_execution(self):
        self._assert_clean("signal_intake")

    def test_wims_and_intelligence_never_import_execution(self):
        self._assert_clean("wims")
        self._assert_clean("intelligence")

    def test_no_executionjob_model_in_signal_intake(self):
        from django.apps import apps
        names = [m.__name__ for m in apps.get_app_config("signal_intake").get_models()]
        # ADR-009: no execution/order model may live in signal_intake.
        self.assertNotIn("ExecutionJob", names)
        for banned in ("ExecutionJob", "ProposedSignalOrder", "SignalExecutionPlan",
                       "ProposedOrderLeg"):
            self.assertNotIn(banned, names)
        # Allowlist: intake + SIGNAL-ACQUISITION-MVP provider-platform models only.
        self.assertEqual(
            sorted(names),
            sorted([
                "PendingSignalApproval", "SignalAuditEvent",
                "SignalProvider", "ParserProfile", "AcquiredMessage", "SignalUpdate",
                "MessageAmendment",
            ]),
        )
