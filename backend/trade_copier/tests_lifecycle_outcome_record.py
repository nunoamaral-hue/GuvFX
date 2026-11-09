"""ADR-0085 — copier lifecycle-OUTCOME record tests (persistence -> operator alert).

Pins the backend half of the wiring: a bridge lifecycle-guard outcome is recorded onto the EXISTING canonical
records — ``CopierReplication.reconciliation_status`` and the governed ``reliability.AlertEvent`` (via
``trade_copier.failure_alerts``) — and:

* a bridge result with NO lifecycle_outcome is a DARK no-op (nothing written, no alert);
* a terminal FAILURE reaches BOTH the replication status AND the operator-alert layer (packet test #24);
* a SUCCESS outcome writes status but raises NO alert; UNKNOWN_OUTCOME alerts (never inferred as success);
* BROKER_REJECTED maps to the op-specific EXISTING category; the alert is secret-free (ticket redacted);
* the alert gate OFF still records status but raises no alert; an authoritative partial-terminal status is
  never stomped; and the recorder never recovers / retries / re-sends.
"""
from decimal import Decimal

from django.test import override_settings

from reliability.constants import Component
from reliability.models import AlertEvent
from trade_copier import failure_alerts as fa
from trade_copier import lifecycle_outcome_record as rec
from trade_copier import reconcile as _rec
from trade_copier.models import CopierReplication
from trade_copier.tests_execution_producer import ProducerFixture

ALERTS_ON = dict(COPIER_ENABLED=True, COPIER_ALERTS_ENABLED=True)


class LifecycleOutcomeRecordTests(ProducerFixture):
    def _repl(self, *, ticket=7000123, status=""):
        f = self._follower()
        return CopierReplication.objects.create(
            master_event=self.event, follower=f, requested_op="MODIFY_SLTP",
            follower_symbol="XAUUSD", target_volume=Decimal("0.05"),
            follower_ticket=ticket, reconciliation_status=status)

    def test_no_lifecycle_outcome_is_dark_noop(self):
        r = self._repl()
        out = rec.record_lifecycle_outcome(r, {"ok": True, "closed": True}, op="FULL_CLOSE")
        self.assertFalse(out["recorded"])
        r.refresh_from_db()
        self.assertEqual(r.reconciliation_status, "")
        self.assertEqual(AlertEvent.objects.count(), 0)

    @override_settings(**ALERTS_ON)
    def test_applied_writes_status_no_alert(self):
        r = self._repl()
        out = rec.record_lifecycle_outcome(r, {"lifecycle_outcome": "APPLIED"}, op="MODIFY_PROTECTION")
        self.assertTrue(out["recorded"])
        self.assertEqual(out["outcome"], "APPLIED")
        r.refresh_from_db()
        self.assertEqual(r.reconciliation_status, "APPLIED")
        self.assertEqual(AlertEvent.objects.count(), 0)

    @override_settings(**ALERTS_ON)
    def test_terminal_failure_reaches_replication_and_operator_alert(self):
        # Packet test #24 — the failure outcome reaches CopierReplication AND the operator-alert layer.
        r = self._repl()
        out = rec.record_lifecycle_outcome(
            r, {"lifecycle_outcome": "PRE_STATE_MISMATCH", "lifecycle_reason": "symbol_mismatch"},
            op="FULL_CLOSE")
        self.assertTrue(out["recorded"])
        self.assertTrue(out["alerted"])
        r.refresh_from_db()
        self.assertEqual(r.reconciliation_status, "PRE_STATE_MISMATCH")
        a = AlertEvent.objects.get()
        self.assertEqual(a.component, Component.COPY_TRADING)
        self.assertEqual(a.detail["category"], fa.PRE_STATE_MISMATCH)
        self.assertEqual(a.trading_account_id, r.follower.follower_account_id)
        # secret-free: the ticket is redacted to a tail; no raw login/server appears.
        self.assertTrue(str(a.detail["ticket_redacted"]).startswith("…"))
        self.assertNotIn("7000123", a.body)

    @override_settings(**ALERTS_ON)
    def test_unknown_outcome_alerts_never_success(self):
        r = self._repl()
        out = rec.record_lifecycle_outcome(r, {"lifecycle_outcome": "UNKNOWN_OUTCOME"}, op="PARTIAL_CLOSE")
        self.assertTrue(out["alerted"])
        a = AlertEvent.objects.get()
        self.assertEqual(a.detail["category"], fa.UNKNOWN_OUTCOME)

    def test_broker_rejected_is_op_specific(self):
        self.assertEqual(rec.failure_category_for("BROKER_REJECTED", "MODIFY_PROTECTION"), fa.MODIFICATION_FAILED)
        self.assertEqual(rec.failure_category_for("BROKER_REJECTED", "FULL_CLOSE"), fa.FULL_CLOSE_FAILED)
        self.assertEqual(rec.failure_category_for("BROKER_REJECTED", "PARTIAL_CLOSE"), fa.PARTIAL_CLOSE_FAILED)
        self.assertIsNone(rec.failure_category_for("APPLIED", "FULL_CLOSE"))

    def test_alert_gate_off_records_status_no_alert(self):
        r = self._repl()
        with override_settings(COPIER_ENABLED=True, COPIER_ALERTS_ENABLED=False):
            out = rec.record_lifecycle_outcome(r, {"lifecycle_outcome": "DEADLINE_EXCEEDED"}, op="FULL_CLOSE")
        self.assertFalse(out["alerted"])
        r.refresh_from_db()
        self.assertEqual(r.reconciliation_status, "DEADLINE_EXCEEDED")
        self.assertEqual(AlertEvent.objects.count(), 0)

    @override_settings(**ALERTS_ON)
    def test_never_stomps_partial_terminal_status(self):
        r = self._repl(status=_rec.PARTIAL_RECONCILED)
        rec.record_lifecycle_outcome(r, {"lifecycle_outcome": "CONTAINED_FAILURE"},
                                     op="PARTIAL_CLOSE", set_reconciliation=True)
        r.refresh_from_db()
        self.assertEqual(r.reconciliation_status, _rec.PARTIAL_RECONCILED)   # authoritative status untouched

    @override_settings(**ALERTS_ON)
    def test_set_reconciliation_false_leaves_status_but_alerts(self):
        r = self._repl(status="MATCHED")
        out = rec.record_lifecycle_outcome(r, {"lifecycle_outcome": "CONTAINED_FAILURE"},
                                           op="PARTIAL_CLOSE", set_reconciliation=False)
        self.assertTrue(out["alerted"])
        self.assertIsNone(out["reconciliation_status"])
        r.refresh_from_db()
        self.assertEqual(r.reconciliation_status, "MATCHED")

    @override_settings(**ALERTS_ON)
    def test_never_stomps_follower_ticket_binding_status(self):
        # MATCHED is the follower-ticket binding later lifecycle ops depend on — never overwrite it.
        r = self._repl(status="MATCHED")
        out = rec.record_lifecycle_outcome(r, {"lifecycle_outcome": "APPLIED"},
                                           op="MODIFY_PROTECTION", set_reconciliation=True)
        self.assertIsNone(out["reconciliation_status"])   # not written
        r.refresh_from_db()
        self.assertEqual(r.reconciliation_status, "MATCHED")

    @override_settings(**ALERTS_ON)
    def test_never_stomps_in_flight_partial_status(self):
        # PCLOSE_SENT is an in-flight partial awaiting the authoritative read — never overwrite it.
        r = self._repl(status=_rec.PARTIAL_SENT)
        rec.record_lifecycle_outcome(r, {"lifecycle_outcome": "UNKNOWN_OUTCOME"},
                                     op="MODIFY_PROTECTION", set_reconciliation=True)
        r.refresh_from_db()
        self.assertEqual(r.reconciliation_status, _rec.PARTIAL_SENT)

    @override_settings(**ALERTS_ON)
    def test_writes_over_empty_and_prior_guard_outcome(self):
        # Empty -> written; and a PRIOR guard outcome -> overwritten by the newer guard outcome.
        r = self._repl(status="")
        rec.record_lifecycle_outcome(r, {"lifecycle_outcome": "UNKNOWN_OUTCOME"}, op="FULL_CLOSE")
        r.refresh_from_db()
        self.assertEqual(r.reconciliation_status, "UNKNOWN_OUTCOME")
        rec.record_lifecycle_outcome(r, {"lifecycle_outcome": "CONTAINED_FAILURE"}, op="FULL_CLOSE")
        r.refresh_from_db()
        self.assertEqual(r.reconciliation_status, "CONTAINED_FAILURE")

    @override_settings(**ALERTS_ON)
    def test_unrecognised_outcome_fails_closed_to_contained(self):
        r = self._repl()
        out = rec.record_lifecycle_outcome(r, {"lifecycle_outcome": "WAT"}, op="FULL_CLOSE")
        self.assertEqual(out["outcome"], "CONTAINED_FAILURE")
        self.assertTrue(out["alerted"])
