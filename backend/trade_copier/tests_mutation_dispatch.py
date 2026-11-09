"""ADR-0086 — DARK guarded MODIFY/CLOSE lifecycle-dispatch tests.

Pins the dispatch pipeline around an INJECTABLE transport + gate: flag-off / inert-transport no-ops (no broker
contact), single-flight claim, gate-parity refusal, and the outcome→job/replication/record mapping for an
OPEN→MODIFY→CLOSE sequence plus the stale/duplicate/unknown/transport-error cases. No real broker is contacted:
the transport and the authority gate are injected.
"""
from decimal import Decimal

from django.test import override_settings

from execution.models import ExecutionJob
from trade_copier import mutation_dispatch as md
from trade_copier.models import CopierReplication, ReplicationStatus
from trade_copier.tests_execution_producer import ProducerFixture

ARM_ON = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True, COPIER_LIVE_EXECUTE_ENABLED=True,
              COPIER_MUTATION_DISPATCH_ENABLED=True)
_PASS_GATE = lambda rep, op, now: (True, "ok")      # noqa: E731 — injected producer-parity gate (passing)
_FAIL_GATE = lambda rep, op, now: (False, "authorization_revoked")  # noqa: E731


def _applied(cmd):
    return {"ok": True, "lifecycle_outcome": "APPLIED", "lifecycle_reason": "closed"}


def _mismatch(cmd):
    return {"ok": False, "lifecycle_outcome": "PRE_STATE_MISMATCH", "lifecycle_reason": "symbol_mismatch"}


def _unknown(cmd):
    return {"ok": False, "lifecycle_outcome": "UNKNOWN_OUTCOME", "lifecycle_reason": "indeterminate_send",
            "need_reconcile": True}


class MutationDispatchTests(ProducerFixture):
    _seq = 100

    def _job(self, *, job_type="CLOSE_TRADE", op="FULL_CLOSE", ticket=8001, requested_op="CLOSE",
             status=ExecutionJob.Status.PENDING):
        f = self._follower(number=str(2000000 + MutationDispatchTests._seq))
        MutationDispatchTests._seq += 1
        rep = CopierReplication.objects.create(
            master_event=self.event, follower=f, requested_op=requested_op, follower_symbol="XAUUSD",
            target_volume=Decimal("0.05"), follower_ticket=ticket, status=ReplicationStatus.EXECUTION_REQUESTED)
        job = ExecutionJob.objects.create(
            account=f.follower_account, job_type=job_type, status=status,
            payload={"copier_operation": op, "ticket": ticket, "symbol": "XAUUSD", "require_identity_pin": True,
                     "expected_login": "9001", "expected_server": "Broker-Demo",
                     "requested_post_state": {"closed": True}},
            hosted_idempotency_key=f"copier-md-test:{rep.pk}")
        rep.execution_job = job
        rep.save(update_fields=["execution_job"])
        return job, rep

    # ── gating: flag OFF, and inert default transport contacts no broker ────────────────────────────────
    def test_pass_disabled_by_default(self):
        self._job()
        out = md.run_mutation_dispatch_pass()           # all gates OFF (default)
        self.assertFalse(out["enabled"])
        self.assertEqual(out["dispatched"], 0)

    @override_settings(**ARM_ON)
    def test_inert_transport_sends_nothing_and_reverts_pending(self):
        job, rep = self._job()
        out = md.run_mutation_dispatch_pass(gate_check=_PASS_GATE)   # default INERT transport
        self.assertTrue(out["enabled"])
        self.assertEqual(out["dispatched"], 0)                      # nothing sent
        job.refresh_from_db(); rep.refresh_from_db()
        self.assertEqual(job.status, ExecutionJob.Status.PENDING)   # reverted — no broker contact
        self.assertEqual(rep.reconciliation_status, "")             # nothing recorded

    # ── outcome → job/replication/record mapping ────────────────────────────────────────────────────────
    def test_applied_marks_verified(self):
        job, rep = self._job()
        out = md.dispatch_one(job.id, transport=_applied, gate_check=_PASS_GATE)
        self.assertTrue(out["dispatched"]); self.assertEqual(out["outcome"], "APPLIED")
        job.refresh_from_db(); rep.refresh_from_db()
        self.assertEqual(job.status, ExecutionJob.Status.SUCCESS)
        self.assertEqual(rep.status, ReplicationStatus.EXECUTION_VERIFIED)
        self.assertEqual(rep.reconciliation_status, "APPLIED")

    def test_terminal_failure_marks_failed(self):
        job, rep = self._job(job_type="MODIFY_POSITION", op="MODIFY_PROTECTION", requested_op="MODIFY_SLTP")
        out = md.dispatch_one(job.id, transport=_mismatch, gate_check=_PASS_GATE)
        self.assertEqual(out["outcome"], "PRE_STATE_MISMATCH")
        job.refresh_from_db(); rep.refresh_from_db()
        self.assertEqual(job.status, ExecutionJob.Status.FAILED)
        self.assertEqual(rep.status, ReplicationStatus.EXECUTION_FAILED)
        self.assertEqual(rep.reconciliation_status, "PRE_STATE_MISMATCH")

    def test_unknown_outcome_leaves_running_never_success(self):
        job, rep = self._job()
        out = md.dispatch_one(job.id, transport=_unknown, gate_check=_PASS_GATE)
        self.assertEqual(out["outcome"], "UNKNOWN_OUTCOME")
        job.refresh_from_db(); rep.refresh_from_db()
        self.assertEqual(job.status, ExecutionJob.Status.RUNNING)              # reconcile, not terminal
        self.assertNotEqual(rep.status, ReplicationStatus.EXECUTION_VERIFIED)  # never success

    def test_transport_exception_is_unknown_running(self):
        def _boom(cmd):
            raise TimeoutError("lost")
        job, rep = self._job()
        out = md.dispatch_one(job.id, transport=_boom, gate_check=_PASS_GATE)
        self.assertEqual(out["outcome"], "UNKNOWN_OUTCOME")
        job.refresh_from_db()
        self.assertEqual(job.status, ExecutionJob.Status.RUNNING)

    # ── gate parity + single-flight + no-late / exclusions ──────────────────────────────────────────────
    def test_gate_refused_leaves_pending_no_send(self):
        sent = []
        job, rep = self._job()
        out = md.dispatch_one(job.id, transport=lambda c: sent.append(c) or _applied(c), gate_check=_FAIL_GATE)
        self.assertFalse(out["dispatched"]); self.assertEqual(out["reason"], "authorization_revoked")
        self.assertEqual(sent, [])                                            # nothing sent
        job.refresh_from_db()
        self.assertEqual(job.status, ExecutionJob.Status.PENDING)

    def test_already_running_is_idempotent_no_second_send(self):
        job, rep = self._job(status=ExecutionJob.Status.RUNNING)
        sent = []
        out = md.dispatch_one(job.id, transport=lambda c: sent.append(c) or _applied(c), gate_check=_PASS_GATE)
        self.assertFalse(out["dispatched"]); self.assertEqual(out["reason"], "not_pending")
        self.assertEqual(sent, [])

    def test_no_follower_ticket_refused(self):
        job, rep = self._job()
        rep.follower_ticket = None
        rep.save(update_fields=["follower_ticket"])
        out = md.dispatch_one(job.id, transport=_applied, gate_check=_PASS_GATE)
        self.assertEqual(out["reason"], "no_follower_ticket")

    @override_settings(**ARM_ON)
    def test_partial_close_job_is_excluded(self):
        # PARTIAL_CLOSE_TRADE keeps its dedicated worker path — the mutation dispatch must never claim it.
        f = self._follower(number="2009999")
        ExecutionJob.objects.create(account=f.follower_account, job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE,
                                    status=ExecutionJob.Status.PENDING, payload={"copier_operation": "PARTIAL_CLOSE"},
                                    hosted_idempotency_key="copier-md-test-pc")
        out = md.run_mutation_dispatch_pass(gate_check=_PASS_GATE, transport=_applied)
        self.assertEqual(out["scanned"], 0)                                  # not scanned

    def test_default_authority_gate_fails_closed_without_authz(self):
        # The REAL producer-parity gate (not injected): with no governing live authorization, it refuses and
        # sends nothing — proving the default path fails closed.
        sent = []
        job, rep = self._job()
        out = md.dispatch_one(job.id, transport=lambda c: sent.append(c) or _applied(c))   # default gate_check
        self.assertFalse(out["dispatched"])
        self.assertEqual(sent, [])
        job.refresh_from_db()
        self.assertEqual(job.status, ExecutionJob.Status.PENDING)

    # ── watermark (migration-free) ──────────────────────────────────────────────────────────────────────
    def test_watermark_is_max_applied_sequence(self):
        f = self._follower(number="2008888")
        # no applied mutation yet -> -1
        self.assertEqual(md.follower_mutation_hwm(f, 7777), -1)
        # an EXECUTION_VERIFIED mutation at this ticket sets the HWM to its event sequence.
        CopierReplication.objects.create(master_event=self.event, follower=f, requested_op="MODIFY_SLTP",
                                         follower_symbol="XAUUSD", follower_ticket=7777,
                                         status=ReplicationStatus.EXECUTION_VERIFIED)
        self.assertEqual(md.follower_mutation_hwm(f, 7777), int(self.event.sequence))

    def test_build_command_stamps_watermark(self):
        job, rep = self._job(ticket=7777)
        cmd = md._build_command(job, rep)
        self.assertIn("last_applied_revision", cmd)
        self.assertEqual(cmd["copier_operation"], "FULL_CLOSE")              # payload preserved

    # ── OPEN → MODIFY → CLOSE sequence (mutation-killing) ───────────────────────────────────────────────
    def test_modify_then_close_sequence_verified(self):
        mjob, mrep = self._job(job_type="MODIFY_POSITION", op="MODIFY_PROTECTION", requested_op="MODIFY_SLTP",
                               ticket=9100)
        md.dispatch_one(mjob.id, transport=_applied, gate_check=_PASS_GATE)
        mjob.refresh_from_db()
        self.assertEqual(mjob.status, ExecutionJob.Status.SUCCESS)
        cjob, crep = self._job(job_type="CLOSE_TRADE", op="FULL_CLOSE", requested_op="CLOSE", ticket=9100)
        md.dispatch_one(cjob.id, transport=_applied, gate_check=_PASS_GATE)
        cjob.refresh_from_db(); crep.refresh_from_db()
        self.assertEqual(cjob.status, ExecutionJob.Status.SUCCESS)
        self.assertEqual(crep.status, ReplicationStatus.EXECUTION_VERIFIED)
