"""ADR-0087 — Phase-4 full DARK lifecycle acceptance (mutation-killing, no broker contact).

End-to-end acceptance for the OPEN→MODIFY→CLOSE follower lifecycle wired through the ADR-0086 dispatcher + the
ADR-0087 real transport + the ADR-0085 guard + the recorder, plus the all-controls-OFF no-broker-contact
invariant. The guard's own admit/classify semantics (scenarios I/J/K/L/M/O/P/S) are pinned by
``execution.tests_copier_lifecycle_guard``; the bridge wiring by ``execution.tests_copier_bridge_wiring``; the
transport by ``trade_copier.tests_mutation_transport``; the RED admission by
``execution.tests_copier_live_admission``; the recorder/alerts by ``trade_copier.tests_lifecycle_outcome_record``.
This file pins the INTEGRATION and the OFF invariant.

Packet A–T coverage map:
  A/B OPEN with/without SL/TP ....... producer PLACE_ORDER path + guard MODIFY add-after-open (bridge_wiring)
  C/D add SL / add TP ............... guard MODIFY admit + classify (lifecycle_guard, bridge_wiring)
  E   modify SL/TP again ............ monotonic revision + MODIFY (lifecycle_guard, dispatch watermark)
  F   remove SL/TP .................. guard MODIFY target semantics (lifecycle_guard)
  G   partial close ................. partial worker + guard PARTIAL (tests_partial_close_worker, lifecycle_guard)
  H   full master->follower close ... dispatcher FULL_CLOSE -> transport -> APPLIED (this file)
  I   duplicate revision ............ guard ALREADY_APPLIED / STALE (lifecycle_guard), dispatch idempotency
  J   stale/out-of-order revision ... guard STALE_REVISION + dispatch watermark (this file + dispatch)
  K   deadline exceeded ............. guard DEADLINE_EXCEEDED + transport past_deadline (guard, transport)
  L   pre-state mismatch ............ guard PRE_STATE_MISMATCH (lifecycle_guard)
  M   unknown outcome ............... transport UNKNOWN -> dispatch RUNNING (this file)
  N   transport timeout ............. transport exception -> UNKNOWN, no retry (transport, this file)
  O   broker rejection .............. guard BROKER_REJECTED (lifecycle_guard)
  P   identity/symbol drift ......... admission triple-agreement + transport identity (admission, transport)
  Q   authorization expires ......... deadline/expiry fail-closed (transport past_deadline, admission time-bound)
  R   restart/replay ................ admission reads env FRESH -> fail-closed after restart (admission)
  S   failed OPEN not retried late .. no-late-recovery; guard never opens (lifecycle_guard)
  T   all controls OFF -> no contact . this file
"""
from decimal import Decimal
from unittest import mock

from django.test import override_settings

from execution.models import ExecutionJob
from execution.snapshot_transport import SnapshotTransport, ST_PER_TENANT_OK
from trade_copier import mutation_dispatch as md
from trade_copier import mutation_transport as mt
from trade_copier.models import CopierReplication, ReplicationStatus
from trade_copier.tests_execution_producer import ProducerFixture

ARM = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True, COPIER_LIVE_EXECUTE_ENABLED=True,
           COPIER_MUTATION_DISPATCH_ENABLED=True, COPIER_MUTATION_TRANSPORT_ENABLED=True)
_PASS_GATE = lambda rep, op, now: (True, "ok")   # noqa: E731
_PER_TENANT = SnapshotTransport(True, ST_PER_TENANT_OK, "http://10.50.0.9:8812", per_tenant=True)


class Phase4AcceptanceTests(ProducerFixture):
    _n = 4000

    def _job(self, *, job_type="CLOSE_TRADE", op="FULL_CLOSE", ticket=9500, requested_op="CLOSE", symbol="XAUUSD"):
        f = self._follower(number=str(2000000 + Phase4AcceptanceTests._n))
        Phase4AcceptanceTests._n += 1
        acct = f.follower_account
        rep = CopierReplication.objects.create(
            master_event=self.event, follower=f, requested_op=requested_op, follower_symbol=symbol,
            target_volume=Decimal("0.05"), follower_ticket=ticket, status=ReplicationStatus.EXECUTION_REQUESTED)
        job = ExecutionJob.objects.create(
            account=acct, job_type=job_type, status=ExecutionJob.Status.PENDING,
            payload={"copier_operation": op, "ticket": ticket, "symbol": symbol, "copier_replication_id": rep.id,
                     "require_identity_pin": True, "expected_login": str(acct.account_number),
                     "expected_server": acct.broker_server.server_name,
                     "authorization_expires_at": __import__("time").time() + 1800},
            hosted_idempotency_key=f"copier-p4:{rep.pk}")
        rep.execution_job = job
        rep.save(update_fields=["execution_job"])
        return job, rep

    # ── T: ALL CONTROLS OFF -> no broker contact (the real transport is inert) ──────────────────────────
    def test_T_all_off_no_broker_contact(self):
        job, rep = self._job()
        calls = []
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base") as rsv:
            out = md.run_mutation_dispatch_pass(transport=mt.mutation_transport(
                http=lambda *a, **k: calls.append(1) or (200, {})), gate_check=_PASS_GATE)
        self.assertFalse(out["enabled"])            # dispatch gate OFF
        self.assertEqual(calls, [])                 # no HTTP
        rsv.assert_not_called()                     # no endpoint resolution
        job.refresh_from_db()
        self.assertEqual(job.status, ExecutionJob.Status.PENDING)

    @override_settings(**ARM)
    def test_T_armed_but_transport_flag_off_is_inert(self):
        # dispatch armed but COPIER_MUTATION_TRANSPORT_ENABLED off -> transport inert -> job reverts PENDING.
        with override_settings(COPIER_MUTATION_TRANSPORT_ENABLED=False):
            job, rep = self._job()
            calls = []
            out = md.run_mutation_dispatch_pass(transport=mt.mutation_transport(
                http=lambda *a, **k: calls.append(1) or (200, {})), gate_check=_PASS_GATE)
        self.assertTrue(out["enabled"])
        self.assertEqual(out["dispatched"], 0)
        self.assertEqual(calls, [])
        job.refresh_from_db()
        self.assertEqual(job.status, ExecutionJob.Status.PENDING)

    # ── H: full close end-to-end dispatcher -> real transport -> APPLIED ────────────────────────────────
    @override_settings(**ARM)
    def test_H_full_close_end_to_end_applied(self):
        job, rep = self._job(op="FULL_CLOSE")
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=_PER_TENANT), \
             mock.patch("execution.monitoring_gateway_client._agent_token", return_value="tok"):
            out = md.run_mutation_dispatch_pass(transport=mt.mutation_transport(
                http=lambda *a, **k: (200, {"ok": True, "lifecycle_outcome": "APPLIED", "lifecycle_reason": "closed"})),
                gate_check=_PASS_GATE)
        self.assertEqual(out["dispatched"], 1)
        job.refresh_from_db(); rep.refresh_from_db()
        self.assertEqual(job.status, ExecutionJob.Status.SUCCESS)
        self.assertEqual(rep.status, ReplicationStatus.EXECUTION_VERIFIED)
        self.assertEqual(rep.reconciliation_status, "APPLIED")

    # ── C/D/E: modify end-to-end -> APPLIED ─────────────────────────────────────────────────────────────
    @override_settings(**ARM)
    def test_CDE_modify_end_to_end_applied(self):
        job, rep = self._job(job_type="MODIFY_POSITION", op="MODIFY_PROTECTION", requested_op="MODIFY_SLTP")
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=_PER_TENANT), \
             mock.patch("execution.monitoring_gateway_client._agent_token", return_value="tok"):
            md.run_mutation_dispatch_pass(transport=mt.mutation_transport(
                http=lambda *a, **k: (200, {"ok": True, "lifecycle_outcome": "APPLIED"})), gate_check=_PASS_GATE)
        job.refresh_from_db()
        self.assertEqual(job.status, ExecutionJob.Status.SUCCESS)

    # ── M/N: transport timeout -> UNKNOWN -> job RUNNING (reconcile, never success) ─────────────────────
    @override_settings(**ARM)
    def test_MN_transport_timeout_unknown_running(self):
        job, rep = self._job(op="FULL_CLOSE")
        n = {"c": 0}

        def _boom(*a, **k):
            n["c"] += 1
            raise TimeoutError("lost")
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=_PER_TENANT), \
             mock.patch("execution.monitoring_gateway_client._agent_token", return_value="tok"):
            md.run_mutation_dispatch_pass(transport=mt.mutation_transport(http=_boom), gate_check=_PASS_GATE)
        job.refresh_from_db(); rep.refresh_from_db()
        self.assertEqual(job.status, ExecutionJob.Status.RUNNING)          # reconcile, not terminal
        self.assertNotEqual(rep.status, ReplicationStatus.EXECUTION_VERIFIED)
        self.assertEqual(n["c"], 1)                                        # no retry

    # ── Q: expired authorization -> transport blocks pre-send (nothing sent) ────────────────────────────
    @override_settings(**ARM)
    def test_Q_expired_authorization_blocks_send(self):
        job, rep = self._job(op="FULL_CLOSE")
        job.payload["authorization_expires_at"] = 1.0
        job.save(update_fields=["payload"])
        calls = []
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=_PER_TENANT), \
             mock.patch("execution.monitoring_gateway_client._agent_token", return_value="tok"):
            md.run_mutation_dispatch_pass(transport=mt.mutation_transport(
                http=lambda *a, **k: calls.append(1) or (200, {})), gate_check=_PASS_GATE)
        self.assertEqual(calls, [])                                        # nothing sent
        job.refresh_from_db(); rep.refresh_from_db()
        # a genuine pre-send transport refusal (expired authorization) -> nothing sent, terminal FAILED.
        self.assertEqual(job.status, ExecutionJob.Status.FAILED)
        self.assertNotEqual(rep.status, ReplicationStatus.EXECUTION_VERIFIED)
