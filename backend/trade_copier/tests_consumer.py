"""Dedicated DEMO partial-close consumer certification (ADR-0056). Negative controls for worker-class
separation, authoritative routing, in-lock gates, server-derived pins, durable no-double-send, completion
authorization, the result state machine, and authoritative reconciliation. DARK; nothing is armed/executed."""
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from execution.models import ExecutionJob, TerminalNode, WorkerIdentity
from trade_copier import consumer, reconcile
from trade_copier.consumer import ClaimRefused, assert_dedicated_worker, claim_partial_close, complete_partial_close
from trade_copier.diff import ObservedPosition
from trade_copier.models import CopierReplication, MasterEvent, ReplicationStatus
from trade_copier.observer import MasterReadResult
from trade_copier.tests_execution_producer import ProducerFixture

ARMED = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True, COPIER_EXECUTE_DEMO_ARMED=True)
NODE = "node-pc-1"
NEXT_URL = "/api/execution/jobs/next/"
CLAIM_URL = "/api/copy-trading/partial-close/claim/"


def _fetch(ticket, volume, symbol="XAUUSD", ok=True, reason="ok"):
    def f(_acct):
        if not ok:
            return MasterReadResult(False, reason)
        pos = () if ticket is None else (ObservedPosition(ticket=ticket, symbol=symbol, side="BUY", volume=volume),)
        return MasterReadResult(True, "ok", positions=pos)
    return f


class ConsumerFixture(ProducerFixture):
    def setUp(self):
        super().setUp()
        self.node = TerminalNode.objects.create(
            hostname=NODE, order_bridge_base_url="http://x", status=TerminalNode.Status.ACTIVE)
        self.follower = self._follower()                 # demo, active, broker_server IS6Technologies-Demo
        self.facct = self.follower.follower_account
        self.facct.terminal_node = self.node
        self.facct.save(update_fields=["terminal_node"])
        self._instrument(self.facct)
        self.job = ExecutionJob.objects.create(
            account=self.facct, job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE,
            status=ExecutionJob.Status.PENDING, terminal_node=self.node,
            payload={"ticket": 777, "volume": "0.40", "symbol": "XAUUSD"})
        self.pevent = MasterEvent.objects.create(
            master=self.master, sequence=4, from_poll=self.poll,
            event_type=MasterEvent.EventType.PARTIAL_CLOSE, position_ticket=1, symbol="XAUUSD", side="BUY",
            volume_before=Decimal("1.00"), volume_after=Decimal("0.60"), volume_delta=Decimal("-0.40"),
            observed_at=timezone.now())
        self.rep = CopierReplication.objects.create(
            master_event=self.pevent, follower=self.follower, requested_op="PARTIAL_CLOSE",
            status=ReplicationStatus.EXECUTION_REQUESTED, follower_symbol="XAUUSD", follower_ticket=777,
            target_volume=Decimal("0.40"), execution_job=self.job)

    def _worker(self, **perm_overrides):
        perms = {"partial_close_worker": True, "authorized_nodes": [NODE],
                 "partial_close_account_id": self.facct.id}
        perms.update(perm_overrides)
        return WorkerIdentity.objects.create(
            worker_id=f"pcw{len(WorkerIdentity.objects.all())}",
            worker_secret_hash=WorkerIdentity.hash_secret("s"), status=WorkerIdentity.Status.ACTIVE,
            worker_permissions=perms)

    def _claim(self, worker):
        return claim_partial_close(worker, account_id=self.facct.id, authorized_node_hostnames=[NODE])


# ── worker-class separation (points 1-2) ─────────────────────────────────────────────────────────────
class WorkerClassTests(ConsumerFixture):
    def test_generic_worker_refused_at_dedicated(self):
        w = WorkerIdentity.objects.create(worker_id="g", worker_secret_hash=WorkerIdentity.hash_secret("s"),
                                          status=WorkerIdentity.Status.ACTIVE, worker_permissions={})
        with self.assertRaises(ClaimRefused) as cm:
            assert_dedicated_worker(w)
        self.assertEqual(cm.exception.reason, "not_partial_close_worker")

    def test_mixed_class_refused(self):
        w = self._worker(shadow_worker=True)
        with self.assertRaises(ClaimRefused) as cm:
            assert_dedicated_worker(w)
        self.assertEqual(cm.exception.reason, "mixed_worker_class_refused")

    @override_settings(**ARMED)
    def test_partial_close_worker_refused_on_generic_next_job(self):
        WorkerIdentity.objects.create(worker_id="pcw-x", worker_secret_hash=WorkerIdentity.hash_secret("s"),
                                      status=WorkerIdentity.Status.ACTIVE,
                                      worker_permissions={"partial_close_worker": True, "authorized_nodes": [NODE]})
        resp = APIClient().get(NEXT_URL, HTTP_X_WORKER_ID="pcw-x", HTTP_X_WORKER_SECRET="s")
        self.assertEqual(resp.status_code, 403)

    def test_legacy_token_refused_at_claim(self):
        self._worker()
        resp = APIClient().post(CLAIM_URL, {}, format="json", HTTP_X_WORKER_TOKEN="legacy-tok")
        self.assertEqual(resp.status_code, 403)


# ── claim gates (points 3-6) ─────────────────────────────────────────────────────────────────────────
class ClaimGateTests(ConsumerFixture):
    @override_settings(**ARMED)
    def test_happy_claim_server_derived_pin(self):
        out = self._claim(self._worker())
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["ticket"], 777)
        self.assertEqual(out["identity"]["expected_login"], "2000001")        # from account_number
        self.assertEqual(out["identity"]["expected_server"], "IS6Technologies-Demo")  # from broker_server
        self.assertTrue(out["identity"]["require_identity_pin"])
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.RUNNING)
        self.rep.refresh_from_db()
        self.assertEqual(self.rep.reconciliation_status, reconcile.PARTIAL_RECON_PENDING)

    def test_flags_off_refused(self):      # no override -> flags default OFF
        self.assertEqual(self._claim(self._worker())["reason"], "flags_off")

    @override_settings(**ARMED)
    def test_wrong_account_refused(self):
        w = self._worker(partial_close_account_id=self.facct.id + 9999)
        out = claim_partial_close(w, account_id=self.facct.id + 9999, authorized_node_hostnames=[NODE])
        self.assertEqual(out["reason"], "no_jobs")        # no job for that account

    @override_settings(**ARMED)
    def test_wrong_node_refused(self):
        out = claim_partial_close(self._worker(), account_id=self.facct.id, authorized_node_hostnames=["other-node"])
        self.assertEqual(out["reason"], "no_active_authorized_node")

    @override_settings(**ARMED)
    def test_inactive_node_refused(self):
        self.node.status = TerminalNode.Status.OFFLINE
        self.node.save(update_fields=["status"])
        self.assertEqual(self._claim(self._worker())["reason"], "no_active_authorized_node")

    @override_settings(**ARMED)
    def test_null_node_job_unclaimable(self):
        self.job.terminal_node = None
        self.job.save(update_fields=["terminal_node"])
        self.assertEqual(self._claim(self._worker())["reason"], "no_jobs")

    @override_settings(**ARMED)
    def test_live_follower_refused(self):
        bs_live = self.facct.broker_server
        bs_live.environment = "live"
        bs_live.save(update_fields=["environment"])
        self.facct.is_demo = False
        self.facct.save(update_fields=["is_demo"])
        self.assertEqual(self._claim(self._worker())["reason"], "not_demo")

    @override_settings(**ARMED)
    def test_identity_pin_unresolved_refused(self):
        self.facct.broker_server = None
        self.facct.save(update_fields=["broker_server"])
        self.assertEqual(self._claim(self._worker())["reason"], "identity_pin_unresolved")

    @override_settings(**ARMED)
    def test_replication_not_execution_requested_refused(self):
        self.rep.status = ReplicationStatus.SHADOW_RECORDED
        self.rep.save(update_fields=["status"])
        self.assertEqual(self._claim(self._worker())["reason"], "replication_not_execution_requested")

    @override_settings(**ARMED)
    def test_duplicate_claim_single_dispatch(self):
        w = self._worker()
        first = self._claim(w)
        self.assertTrue(first["ok"])
        second = self._claim(w)                            # job now RUNNING -> not claimable again
        self.assertEqual(second["reason"], "no_jobs")

    @override_settings(**ARMED)
    def test_already_dispatched_marker_refused(self):
        self.rep.reconciliation_status = reconcile.PARTIAL_SENT
        self.rep.save(update_fields=["reconciliation_status"])
        # job still PENDING but the replication already entered the dispatch lifecycle -> refuse
        self.assertEqual(self._claim(self._worker())["reason"], "already_dispatched")


# ── lease / orphan recovery (point 6) ────────────────────────────────────────────────────────────────
class LeaseOrphanTests(ConsumerFixture):
    @override_settings(**ARMED)
    def test_expired_lease_not_requeued_by_recovery(self):
        self._claim(self._worker())
        self.job.refresh_from_db()
        self.job.lease_expires_at = timezone.now() - timezone.timedelta(hours=1)   # expired
        self.job.save(update_fields=["lease_expires_at"])
        from execution import execution_health
        # The monitor-chain reclaimers are type-scoped (SYNC/MODIFY/PLACE_ORDER); a partial close is untouched.
        execution_health.reclaim_orphaned_sync_jobs(timezone.now())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.RUNNING)   # NOT requeued to PENDING / FAILED


# ── completion authorization + state machine + reconciliation (points 9-11) ──────────────────────────
class CompletionTests(ConsumerFixture):
    def _dispatch(self):
        out = self._claim(self._worker_for_completion())
        return out["dispatch_token"]

    def _worker_for_completion(self):
        self._w = self._worker()
        return self._w

    @override_settings(**ARMED)
    def test_wrong_worker_cannot_complete(self):
        token = self._dispatch()
        other = self._worker()
        with self.assertRaises(ClaimRefused) as cm:
            complete_partial_close(other, job_id=self.job.id, dispatch_token=token, bridge_result={})
        self.assertEqual(cm.exception.reason, "completion_not_authorized")

    @override_settings(**ARMED)
    def test_stale_token_cannot_complete(self):
        self._dispatch()
        with self.assertRaises(ClaimRefused) as cm:
            complete_partial_close(self._w, job_id=self.job.id, dispatch_token="wrong", bridge_result={})
        self.assertEqual(cm.exception.reason, "completion_not_authorized")

    @override_settings(**ARMED)
    def test_done_reconciles(self):
        token = self._dispatch()
        br = {"ok": True, "mutated": True, "volume_step": 0.01, "estimated_remaining": 0.60}
        out = complete_partial_close(self._w, job_id=self.job.id, dispatch_token=token, bridge_result=br,
                                     fetch=_fetch(777, 0.60))
        self.assertEqual(out["reconciliation"]["status"], reconcile.PARTIAL_RECONCILED)

    @override_settings(**ARMED)
    def test_done_partial_reconciles(self):
        token = self._dispatch()
        br = {"ok": True, "mutated": True, "volume_step": 0.01, "estimated_remaining": 0.60}
        out = complete_partial_close(self._w, job_id=self.job.id, dispatch_token=token, bridge_result=br,
                                     fetch=_fetch(777, 0.50))     # broker left 0.50, intended 0.60
        self.assertEqual(out["reconciliation"]["status"], reconcile.PARTIAL_DIVERGENT_VOLUME)

    @override_settings(**ARMED)
    def test_lost_ack_reconciles_without_resend(self):
        token = self._dispatch()
        br = {"ok": False, "error": "order_send_none_unknown", "need_reconcile": True,
              "volume_step": 0.01, "estimated_remaining": 0.60}
        out = complete_partial_close(self._w, job_id=self.job.id, dispatch_token=token, bridge_result=br,
                                     fetch=_fetch(777, 0.60))
        # Resolved only by an authoritative read; no second send (no order_send invoked anywhere here).
        self.assertIn(out["reconciliation"]["status"], (reconcile.PARTIAL_RECONCILED,
                                                         reconcile.PARTIAL_DIVERGENT_VOLUME))

    @override_settings(**ARMED)
    def test_reject_reconciles(self):
        token = self._dispatch()
        br = {"ok": False, "error": "partial_close_rejected", "need_reconcile": True,
              "volume_step": 0.01, "estimated_remaining": 0.60}
        out = complete_partial_close(self._w, job_id=self.job.id, dispatch_token=token, bridge_result=br,
                                     fetch=_fetch(777, 1.00))     # unchanged -> divergence from intended 0.60
        self.assertEqual(out["reconciliation"]["status"], reconcile.PARTIAL_DIVERGENT_VOLUME)

    @override_settings(**ARMED)
    def test_missing_ticket_is_divergence(self):
        token = self._dispatch()
        br = {"ok": True, "mutated": True, "volume_step": 0.01, "estimated_remaining": 0.60}
        out = complete_partial_close(self._w, job_id=self.job.id, dispatch_token=token, bridge_result=br,
                                     fetch=_fetch(None, 0))       # ticket gone
        self.assertEqual(out["reconciliation"]["status"], reconcile.PARTIAL_DIVERGENT_TICKET_GONE)

    @override_settings(**ARMED)
    def test_recon_outage_retries_only_read(self):
        token = self._dispatch()
        br = {"ok": True, "mutated": True, "volume_step": 0.01, "estimated_remaining": 0.60}
        out = complete_partial_close(self._w, job_id=self.job.id, dispatch_token=token, bridge_result=br,
                                     fetch=_fetch(None, 0, ok=False, reason="snapshot_identity_login_mismatch"))
        self.assertEqual(out["reconciliation"]["status"], reconcile.PARTIAL_RECONCILE_UNAVAILABLE)  # non-terminal

    @override_settings(**ARMED)
    def test_missing_step_is_operator_review(self):
        token = self._dispatch()
        br = {"ok": True, "mutated": True, "estimated_remaining": 0.60}   # NO volume_step
        out = complete_partial_close(self._w, job_id=self.job.id, dispatch_token=token, bridge_result=br,
                                     fetch=_fetch(777, 0.60))
        self.assertEqual(out["status"], reconcile.PARTIAL_OPERATOR_REVIEW)

    @override_settings(**ARMED)
    def test_duplicate_completion_idempotent(self):
        token = self._dispatch()
        br = {"ok": True, "mutated": True, "volume_step": 0.01, "estimated_remaining": 0.60}
        complete_partial_close(self._w, job_id=self.job.id, dispatch_token=token, bridge_result=br,
                               fetch=_fetch(777, 0.60))
        again = complete_partial_close(self._w, job_id=self.job.id, dispatch_token=token, bridge_result=br,
                                       fetch=_fetch(777, 0.60))
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["status"], reconcile.PARTIAL_RECONCILED)
