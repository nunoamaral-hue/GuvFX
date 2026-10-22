"""Dedicated DEMO partial-close consumer certification (ADR-0056). Negative controls for worker-class
separation, authoritative routing, in-lock gates, server-derived pins, durable no-double-send, completion
authorization, the result state machine, and authoritative reconciliation. DARK; nothing is armed/executed."""
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from execution.models import ExecutionJob, TerminalNode, WorkerIdentity
from trade_copier import consumer, reconcile
from trade_copier.consumer import (
    ClaimRefused,
    assert_dedicated_worker,
    authorize_send,
    claim_partial_close,
    complete_partial_close,
)
from trade_copier.diff import ObservedPosition
from trade_copier.models import CopierReplication, MasterEvent, ReplicationStatus
from trade_copier.observer import MasterReadResult
from trade_copier.tests_execution_producer import ProducerFixture

ARMED = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True, COPIER_EXECUTE_DEMO_ARMED=True)
# Enabled + execute on, but arming OFF — used to prove authorize-send fails closed when arming drops.
DISARMED = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True, COPIER_EXECUTE_DEMO_ARMED=False)
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
            # expected_remaining is the server-stamped producer value reconciliation compares against.
            payload={"ticket": 777, "volume": "0.40", "symbol": "XAUUSD", "expected_remaining": "0.60"})
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

    def _authorize(self, worker, claim_out):
        return authorize_send(worker, job_id=claim_out["job_id"], dispatch_token=claim_out["dispatch_token"],
                              account_id=self.facct.id, authorized_node_hostnames=[NODE])

    def _dispatch_authorized(self, worker):
        """Full dispatch: claim -> authorize-send. Returns (dispatch_token, send_nonce) for a completion."""
        c = self._claim(worker)
        a = self._authorize(worker, c)
        return c["dispatch_token"], a["send_nonce"]


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
        # Discriminating: send a legacy token ALONGSIDE valid modern creds. The legacy-token guard must fire
        # FIRST (remove it and the modern creds would authenticate -> a different, non-403 outcome).
        WorkerIdentity.objects.create(worker_id="pcw-legacy", worker_secret_hash=WorkerIdentity.hash_secret("s"),
                                      status=WorkerIdentity.Status.ACTIVE,
                                      worker_permissions={"partial_close_worker": True, "authorized_nodes": [NODE],
                                                          "partial_close_account_id": self.facct.id})
        resp = APIClient().post(CLAIM_URL, {}, format="json", HTTP_X_WORKER_TOKEN="legacy-tok",
                                HTTP_X_WORKER_ID="pcw-legacy", HTTP_X_WORKER_SECRET="s")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data["detail"], "legacy_worker_token_refused")

    @override_settings(**ARMED)
    def test_inlock_account_route_mismatch_refused(self):
        # Defense-in-depth: the in-lock admit refuses a found job whose account != the worker's authorized
        # account, even though the claim query would not surface it (direct unit check).
        ok, reason = consumer._admit_claim_locked(
            self.job, self.rep, active_node_ids=[self.node.id], authorized_account_id=self.facct.id + 9999)
        self.assertFalse(ok)
        self.assertEqual(reason, "account_route_mismatch")


# ── claim gates (points 3-6) ─────────────────────────────────────────────────────────────────────────
class ClaimGateTests(ConsumerFixture):
    @override_settings(**ARMED)
    def test_happy_claim_is_acknowledgment_only(self):
        # The claim returns ONLY an ack — NO mutation instruction (ticket/volume/symbol/identity). Those are
        # released solely by authorize_send (ADR-0056 F1), so arming dropped after claim can't reach the broker.
        out = self._claim(self._worker())
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["phase"], consumer.PHASE_DISPATCHED)
        for leaked in ("ticket", "volume", "expected_symbol", "identity"):
            self.assertNotIn(leaked, out)
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


# ── single-use pre-send authorization (FINDING 1) ─────────────────────────────────────────────────────
class AuthorizeSendTests(ConsumerFixture):
    @override_settings(**ARMED)
    def test_authorize_releases_server_derived_instruction(self):
        w = self._worker()
        c = self._claim(w)
        a = self._authorize(w, c)
        self.assertTrue(a["ok"], a)
        self.assertEqual(a["bridge_call"], "partial_close_position")
        self.assertEqual(a["ticket"], 777)                                  # server-derived
        self.assertEqual(a["identity"]["expected_login"], "2000001")        # from account_number
        self.assertEqual(a["identity"]["expected_server"], "IS6Technologies-Demo")  # from broker_server
        self.assertTrue(a["identity"]["require_identity_pin"])
        self.assertTrue(a["send_nonce"])
        self.job.refresh_from_db()
        self.assertEqual((self.job.result or {}).get("phase"), consumer.PHASE_AUTHORIZED)

    @override_settings(**ARMED)
    def test_authorize_is_single_use(self):
        w = self._worker()
        c = self._claim(w)
        self.assertTrue(self._authorize(w, c)["ok"])
        second = self._authorize(w, c)                                      # second authorization refused
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], "already_authorized")

    def test_arming_dropped_after_claim_refuses_authorize_no_instruction(self):
        """FINDING 1 — discriminating: claim succeeds while ARMED, arming is turned OFF, then authorize-send
        is refused and NO mutation instruction is returned (so the host worker can never call the bridge)."""
        w = self._worker()
        with override_settings(**ARMED):
            c = self._claim(w)
            self.assertTrue(c["ok"])
        with override_settings(**DISARMED):                                 # arming removed between claim+send
            a = self._authorize(w, c)
        self.assertFalse(a["ok"])
        self.assertEqual(a["reason"], "flags_off")
        for leaked in ("ticket", "volume", "expected_symbol", "identity", "send_nonce"):
            self.assertNotIn(leaked, a)
        self.job.refresh_from_db()
        self.assertEqual((self.job.result or {}).get("phase"), consumer.PHASE_DISPATCHED)  # never authorized

    @override_settings(**ARMED)
    def test_authorize_wrong_worker_refused(self):
        w = self._worker()
        c = self._claim(w)
        other = self._worker()
        a = authorize_send(other, job_id=c["job_id"], dispatch_token=c["dispatch_token"],
                           account_id=self.facct.id, authorized_node_hostnames=[NODE])
        self.assertFalse(a["ok"])
        self.assertEqual(a["reason"], "not_authorized")

    @override_settings(**ARMED)
    def test_authorize_requires_dispatched_phase(self):
        # A job that never went through claim (no dispatch token) cannot be authorized.
        w = self._worker()
        a = authorize_send(w, job_id=self.job.id, dispatch_token="nope",
                           account_id=self.facct.id, authorized_node_hostnames=[NODE])
        self.assertFalse(a["ok"])
        self.assertEqual(a["reason"], "not_authorized")

    @override_settings(**ARMED)
    def test_authorize_node_route_mismatch_refused(self):
        w = self._worker()
        c = self._claim(w)
        self.node.status = TerminalNode.Status.OFFLINE          # node no longer active at authorize time
        self.node.save(update_fields=["status"])
        a = self._authorize(w, c)
        self.assertFalse(a["ok"])
        self.assertEqual(a["reason"], "node_route_mismatch")


# ── lease / orphan recovery (point 6) ────────────────────────────────────────────────────────────────
class LeaseOrphanTests(ConsumerFixture):
    @override_settings(**ARMED)
    def test_expired_lease_not_requeued_by_recovery(self):
        self._claim(self._worker())
        self.job.refresh_from_db()
        self.job.lease_expires_at = timezone.now() - timezone.timedelta(hours=1)   # expired
        self.job.save(update_fields=["lease_expires_at"])
        from execution import execution_health
        now = timezone.now()
        # ALL monitor-chain reclaimers are type-scoped (SYNC/MODIFY/PLACE_ORDER); none touches a partial close.
        execution_health.reclaim_orphaned_sync_jobs(now)
        execution_health.reclaim_orphaned_modify_jobs(now)
        execution_health.reconcile_orphaned_place_orders(now)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.RUNNING)   # NOT requeued to PENDING / FAILED


# ── completion authorization + state machine + reconciliation + terminal bookkeeping (F3) ────────────
class CompletionTests(ConsumerFixture):
    def _dispatch(self):
        """claim + authorize-send; stash the worker + nonce and return the dispatch token."""
        self._w = self._worker()
        token, self._nonce = self._dispatch_authorized(self._w)
        return token

    def _complete(self, worker, token, br, *, fetch=None, nonce=None):
        return complete_partial_close(worker, job_id=self.job.id, dispatch_token=token, bridge_result=br,
                                      send_nonce=nonce if nonce is not None else getattr(self, "_nonce", None),
                                      fetch=fetch)

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
    def test_completion_requires_prior_authorization(self):
        # Claim WITHOUT authorize-send, then attempt completion -> refused (no send was authorized).
        w = self._worker()
        c = self._claim(w)
        with self.assertRaises(ClaimRefused) as cm:
            complete_partial_close(w, job_id=c["job_id"], dispatch_token=c["dispatch_token"],
                                   bridge_result={"ok": True, "mutated": True, "need_reconcile": True})
        self.assertEqual(cm.exception.reason, "completion_requires_authorization")

    @override_settings(**ARMED)
    def test_done_reconciles_job_terminal_success(self):
        token = self._dispatch()
        br = {"ok": True, "mutated": True, "need_reconcile": True}
        out = self._complete(self._w, token, br, fetch=_fetch(777, 0.60))
        self.assertEqual(out["reconciliation"]["status"], reconcile.PARTIAL_RECONCILED)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.SUCCESS)      # FINDING 3 — terminal, not RUNNING
        self.assertIsNotNone(self.job.finished_at)

    @override_settings(**ARMED)
    def test_done_partial_divergence_job_terminal_failed(self):
        token = self._dispatch()
        br = {"ok": True, "mutated": True, "need_reconcile": True}
        out = self._complete(self._w, token, br, fetch=_fetch(777, 0.50))   # broker left 0.50, intended 0.60
        self.assertEqual(out["reconciliation"]["status"], reconcile.PARTIAL_DIVERGENT_VOLUME)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.FAILED)       # FINDING 3 — terminal exception

    @override_settings(**ARMED)
    def test_lost_ack_reconciles_without_resend(self):
        token = self._dispatch()
        br = {"ok": False, "error": "order_send_none_unknown", "need_reconcile": True}
        out = self._complete(self._w, token, br, fetch=_fetch(777, 0.60))
        # Resolved only by an authoritative read; no second send (no order_send invoked anywhere here).
        self.assertIn(out["reconciliation"]["status"], (reconcile.PARTIAL_RECONCILED,
                                                         reconcile.PARTIAL_DIVERGENT_VOLUME))

    @override_settings(**ARMED)
    def test_reject_reconciles(self):
        token = self._dispatch()
        br = {"ok": False, "error": "partial_close_rejected", "need_reconcile": True}
        out = self._complete(self._w, token, br, fetch=_fetch(777, 1.00))   # unchanged -> divergence from 0.60
        self.assertEqual(out["reconciliation"]["status"], reconcile.PARTIAL_DIVERGENT_VOLUME)

    @override_settings(**ARMED)
    def test_pre_send_refusal_is_dispatch_refused_no_reconcile(self):
        # A bridge PRE-SEND refusal (no need_reconcile, not ok) -> dispatch-refused, FAILED, NO reconcile read.
        token = self._dispatch()
        def _no_read(_acct):
            raise AssertionError("reconcile read must not run for a pre-send refusal")
        br = {"ok": False, "error": "identity_rejected"}                    # no need_reconcile -> nothing sent
        out = self._complete(self._w, token, br, fetch=_no_read)
        self.assertEqual(out["status"], reconcile.PARTIAL_DISPATCH_REFUSED)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.FAILED)

    @override_settings(**ARMED)
    def test_exception_after_send_reconciles_not_dispatch_refused(self):
        """HIGH-1 — the bridge's catch-all ``exception`` (no need_reconcile) can be raised AFTER order_send.
        It must NOT be recorded as dispatch-refused/nothing-sent; the consumer must read authoritatively."""
        token = self._dispatch()
        br = {"ok": False, "error": "exception", "detail": "boom after send"}   # not an enumerated pre-send code
        out = self._complete(self._w, token, br, fetch=_fetch(777, 0.60))       # broker left intended 0.60
        self.assertNotEqual(out.get("status"), reconcile.PARTIAL_DISPATCH_REFUSED)
        self.assertEqual(out["reconciliation"]["status"], reconcile.PARTIAL_RECONCILED)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.SUCCESS)

    @override_settings(**ARMED)
    def test_completion_without_nonce_refused_once_authorized(self):
        """MEDIUM-2 — once a send is authorized the single-use nonce is MANDATORY on completion; a
        dispatch-token holder cannot complete by omitting it."""
        token = self._dispatch()
        with self.assertRaises(ClaimRefused) as cm:
            complete_partial_close(self._w, job_id=self.job.id, dispatch_token=token,
                                   bridge_result={"ok": True, "mutated": True, "need_reconcile": True},
                                   send_nonce=None, fetch=_fetch(777, 0.60))
        self.assertEqual(cm.exception.reason, "send_nonce_mismatch")

    @override_settings(**ARMED)
    def test_missing_ticket_is_divergence(self):
        token = self._dispatch()
        br = {"ok": True, "mutated": True, "need_reconcile": True}
        out = self._complete(self._w, token, br, fetch=_fetch(None, 0))     # ticket gone
        self.assertEqual(out["reconciliation"]["status"], reconcile.PARTIAL_DIVERGENT_TICKET_GONE)

    @override_settings(**ARMED)
    def test_recon_outage_retries_only_read_job_stays_running(self):
        token = self._dispatch()
        br = {"ok": True, "mutated": True, "need_reconcile": True}
        out = self._complete(self._w, token, br,
                             fetch=_fetch(None, 0, ok=False, reason="snapshot_identity_login_mismatch"))
        self.assertEqual(out["reconciliation"]["status"], reconcile.PARTIAL_RECONCILE_UNAVAILABLE)  # non-terminal
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.RUNNING)      # stays for the background sweep

    @override_settings(**ARMED)
    def test_no_authoritative_step_is_operator_review(self):
        # Remove the authoritative broker instrument -> no server-derived step -> operator review (never a
        # worker-supplied or default step).
        from execution.models import BrokerInstrument
        BrokerInstrument.objects.filter(account=self.facct).delete()
        token = self._dispatch()
        br = {"ok": True, "mutated": True, "need_reconcile": True}
        out = self._complete(self._w, token, br, fetch=_fetch(777, 0.60))
        self.assertEqual(out["status"], reconcile.PARTIAL_OPERATOR_REVIEW)
        self.assertEqual(out["reason"], "authoritative_step_unavailable")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.FAILED)

    @override_settings(**ARMED)
    def test_worker_supplied_step_and_remaining_ignored(self):
        """FINDING (prior review) — a worker POSTing a huge tolerance + a matching 'remaining' cannot force a
        false RECONCILED: the step is the authoritative instrument step and intended is the server-stamped
        payload, so a real divergence is still caught."""
        token = self._dispatch()
        br = {"ok": True, "mutated": True, "need_reconcile": True, "volume_step": 1e9,
              "estimated_remaining": 0.50}
        out = self._complete(self._w, token, br, fetch=_fetch(777, 0.50))   # actual 0.50 vs server-intended 0.60
        self.assertEqual(out["reconciliation"]["status"], reconcile.PARTIAL_DIVERGENT_VOLUME)

    @override_settings(**ARMED)
    def test_send_nonce_mismatch_refused(self):
        token = self._dispatch()
        with self.assertRaises(ClaimRefused) as cm:
            self._complete(self._w, token, {"ok": True, "mutated": True, "need_reconcile": True},
                           fetch=_fetch(777, 0.60), nonce="not-the-nonce")
        self.assertEqual(cm.exception.reason, "send_nonce_mismatch")

    @override_settings(**ARMED)
    def test_duplicate_completion_idempotent(self):
        token = self._dispatch()
        br = {"ok": True, "mutated": True, "need_reconcile": True}
        self._complete(self._w, token, br, fetch=_fetch(777, 0.60))
        again = self._complete(self._w, token, br, fetch=_fetch(777, 0.60))
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["status"], reconcile.PARTIAL_RECONCILED)
