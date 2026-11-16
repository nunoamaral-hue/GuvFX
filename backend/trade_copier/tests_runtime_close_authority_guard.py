"""Adversarial regression tests for the RUNTIME close-authority guard, revocation-surviving FULL-CLOSE
containment, and the teardown flatness proof (GFX-COPIER-RUNTIME-CLOSE-AUTHORITY-GUARD-149, ADR-0114).

Three invariants, each with a distinct killing case:

  A. No follower OPEN is admitted (nor produced under the lock) unless that position has a durable, armed path to
     MODIFY and FULL_CLOSE throughout its lifecycle — modify+full-close AUTHORITY, no partial-close, the
     post-expiry close-only policy, AND the MODIFY/FULL_CLOSE/dispatch/transport/close-after-expiry execution
     arms. A legacy OPEN-only authorization fails closed.
  B. An already-bound copier position is never TRAPPED by a revocation: with the opt-in
     ``COPIER_LIVE_CONTAIN_AFTER_REVOCATION_ENABLED`` arm, a FULL CLOSE (never a MODIFY, never a partial, never an
     entry) may still contain it after its governing authorization was revoked — default-OFF, fail-closed.
  C. Teardown PROVES no open copier-originated follower position remains before declaring DARK; if it cannot, it
     stops in an explicit cleanup-required state and performs no mutation.

Nothing here contacts a broker or host; the teardown host actuators are injected stubs.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from execution.models import ExecutionJob
from trade_copier.execution_producer import produce_close_one
from trade_copier.lifecycle_teardown import teardown_live_dark
from trade_copier.live_execution import (
    admit_full_close_live, admit_modify_live, admit_open_live, admit_partial_close_live,
    create_live_open_job, open_close_lifecycle_armable)
from trade_copier.models import (CopierLiveAuthorizationStatus, CopierReplication, MasterEvent,
                                 ReplicationStatus)
from trade_copier.tests_live_copier import LIVE_ARMED, LiveAdmissionTests
from trade_copier.tests_live_lifecycle import LIVE, FOLLOWER_TICKET, LiveLifecycleFixture, _fetch_follower


# ─────────────────────────────── Part A — the runtime OPEN close-authority guard ────────────────────────────────
@override_settings(**LIVE_ARMED)
class OpenCloseAuthorityGuardTests(TestCase):
    """admit_open_live refuses a LIVE OPEN unless the MODIFY+FULL_CLOSE lifecycle is durable and armed.

    Reuses the ADR-0057 admission fixture (now carrying a complete lifecycle authorization + full armed stack)
    and removes exactly one precondition per case."""

    def _fx(self, **authz_over):
        fx = LiveAdmissionTests()
        fx.setUp()
        if authz_over:
            fx.authz.delete()                      # one-ACTIVE-per-pair: replace, don't add
            fx.authz = fx._grant(**authz_over)
        return fx

    # --- happy path: a complete lifecycle authorization + full armed stack admits -------------------------------
    def test_complete_lifecycle_admits(self):
        self.assertEqual(admit_open_live(self._fx().rep), (True, "ok"))

    def test_pure_guard_helper_accepts_complete_authorization(self):
        self.assertEqual(open_close_lifecycle_armable(self._fx().authz), (True, "ok"))

    # --- AUTHORITY layer (immutable on the authorization) -------------------------------------------------------
    def test_missing_modify_authority_denies(self):
        ok, reason = admit_open_live(self._fx(modify_authority=False).rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "open_requires_modify_authority")

    def test_missing_full_close_authority_denies(self):
        ok, reason = admit_open_live(self._fx(full_close_authority=False).rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "open_requires_full_close_authority")

    def test_partial_close_authority_excluded(self):
        ok, reason = admit_open_live(self._fx(partial_close_authority=True).rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "open_excludes_partial_close_authority")

    def test_close_must_survive_entry_expiry(self):
        ok, reason = admit_open_live(self._fx(close_only_after_expiry=False).rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "open_requires_close_survives_expiry")

    # --- EXECUTION-ARM layer (the flags the close/modify path needs to reach the broker) ------------------------
    @override_settings(**{**LIVE_ARMED, "COPIER_MUTATION_DISPATCH_ENABLED": "0"})
    def test_mutation_dispatch_must_be_armed(self):
        ok, reason = admit_open_live(self._fx().rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "open_requires_mutation_dispatch_armed")

    @override_settings(**{**LIVE_ARMED, "COPIER_MUTATION_TRANSPORT_ENABLED": "0"})
    def test_mutation_transport_must_be_armed(self):
        ok, reason = admit_open_live(self._fx().rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "open_requires_mutation_transport_armed")

    @override_settings(**{**LIVE_ARMED, "COPIER_LIVE_FULL_CLOSE_ENABLED": "0"})
    def test_full_close_execution_must_be_armed(self):
        ok, reason = admit_open_live(self._fx().rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "open_requires_full_close_execution_armed")

    @override_settings(**{**LIVE_ARMED, "COPIER_LIVE_MODIFY_ENABLED": "0"})
    def test_modify_execution_must_be_armed(self):
        ok, reason = admit_open_live(self._fx().rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "open_requires_modify_execution_armed")

    @override_settings(**{**LIVE_ARMED, "COPIER_LIVE_CLOSE_ONLY_AFTER_EXPIRY_ENABLED": "0"})
    def test_close_after_expiry_policy_must_be_armed(self):
        ok, reason = admit_open_live(self._fx().rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "open_requires_close_after_expiry_arm")

    # --- under-lock defence in depth: a flag that flips after preflight fails closed before the order is created
    @override_settings(**{**LIVE_ARMED, "COPIER_LIVE_FULL_CLOSE_ENABLED": "0"})
    def test_under_lock_guard_blocks_when_close_flag_off(self):
        fx = self._fx()
        with transaction.atomic():
            locked = CopierReplication.objects.select_for_update().get(id=fx.rep.id)
            out = create_live_open_job(locked)
        self.assertFalse(out["produced"])
        self.assertEqual(out["reason"], "open_requires_full_close_execution_armed")
        self.assertEqual(ExecutionJob.objects.filter(payload__signal_source="copier").count(), 0)


# ──────────────────────────── Part B — FULL-CLOSE containment survives revocation (opt-in) ───────────────────────
class RevocationContainmentTests(LiveLifecycleFixture):
    """A revoked authorization must never TRAP an open position. The opt-in arm permits a FULL CLOSE only."""

    def _revoke(self, **over):
        self.authz.status = CopierLiveAuthorizationStatus.REVOKED
        self.authz.revoked_at = timezone.now()
        fields = ["status", "revoked_at"]
        for k, v in over.items():
            setattr(self.authz, k, v)
            fields.append(k)
        self.authz.save(update_fields=fields)

    # Default (arm OFF): a revoked authorization governs nothing — ADR-0083 preserved.
    def test_full_close_after_revocation_denied_by_default(self):
        self._revoke()
        ok, _t, reason = admit_full_close_live(self._close_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "governing_authorization_revoked_or_absent")

    @override_settings(**{**LIVE, "COPIER_LIVE_CONTAIN_AFTER_REVOCATION_ENABLED": "1"})
    def test_full_close_contains_after_revocation_when_armed(self):
        self._revoke()
        self.assertEqual(admit_full_close_live(self._close_rep()), (True, FOLLOWER_TICKET, "ok"))

    @override_settings(**{**LIVE, "COPIER_LIVE_CONTAIN_AFTER_REVOCATION_ENABLED": "1"})
    def test_containment_produces_the_full_close_job_under_lock(self):
        self._revoke()
        out = produce_close_one(self._close_rep())
        self.assertTrue(out["produced"], out)
        job = ExecutionJob.objects.get(id=out["job_id"])
        self.assertEqual(job.job_type, ExecutionJob.JobType.CLOSE_TRADE)
        self.assertEqual(job.payload["copier_operation"], "FULL_CLOSE")
        self.assertEqual(job.payload["ticket"], FOLLOWER_TICKET)
        self.assertNoPlaceOrder()

    @override_settings(**{**LIVE, "COPIER_LIVE_CONTAIN_AFTER_REVOCATION_ENABLED": "1"})
    def test_revocation_containment_requires_full_close_authority(self):
        self._revoke(full_close_authority=False)
        ok, _t, reason = admit_full_close_live(self._close_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "revocation_containment_no_full_close_authority")

    # Containment is FULL-CLOSE ONLY — a MODIFY or a partial after revocation is never contained.
    @override_settings(**{**LIVE, "COPIER_LIVE_CONTAIN_AFTER_REVOCATION_ENABLED": "1"})
    def test_modify_never_contained_after_revocation(self):
        self._revoke()
        ok, _t, reason = admit_modify_live(self._modify_rep())
        self.assertFalse(ok)
        self.assertEqual(reason, "governing_authorization_revoked_or_absent")

    @override_settings(**{**LIVE, "COPIER_LIVE_CONTAIN_AFTER_REVOCATION_ENABLED": "1"})
    def test_partial_never_contained_after_revocation(self):
        self._revoke()
        ok, _t, _r, _rem, reason = admit_partial_close_live(
            self._partial_rep(), fetch=_fetch_follower(FOLLOWER_TICKET, 0.02))
        self.assertFalse(ok)
        self.assertEqual(reason, "governing_authorization_revoked_or_absent")


# ───────────────────────────── Part C — teardown proves flatness before declaring DARK ──────────────────────────
class TeardownFlatnessProofTests(LiveLifecycleFixture):
    """teardown_live_dark halts in cleanup-required (no mutation) while a copier position is unflattened, and
    reaches DARK only once the position is provably closed."""

    def _ok_actuator(self):
        return lambda: {"ok": True}

    def _mark_jobs_success(self):
        ExecutionJob.objects.filter(account=self.follower_acct).update(status=ExecutionJob.Status.SUCCESS)

    def _verified_close(self, *, ticket=FOLLOWER_TICKET, sequence=50):
        """A VERIFIED copier FULL CLOSE of the bound ticket — the backend evidence the position is flat."""
        ev = self._event(MasterEvent.EventType.FULL_CLOSE, sequence=sequence, vol_before="0.02", vol_after="0")
        return CopierReplication.objects.create(
            master_event=ev, follower=self.f, requested_op="CLOSE",
            status=ReplicationStatus.EXECUTION_VERIFIED, follower_ticket=ticket,
            follower_symbol="XAUUSD.std", constraints_authoritative=True, sltp_translation_ok=True)

    def _failed_open(self, *, sequence=60, recent=True):
        """A LIVE PLACE_ORDER reported FAILED (lost-ACK) — EXECUTION_FAILED, no follower_ticket. It may still have
        FILLED at the broker (execution_health Control 5), so within the lookback it is NOT proven flat."""
        ev = self._event(MasterEvent.EventType.OPEN, sequence=sequence)
        job = ExecutionJob.objects.create(
            account=self.follower_acct, job_type=ExecutionJob.JobType.PLACE_ORDER,
            payload={"execution_mode": "LIVE", "copier_live_authorization_id": self.authz.id},
            hosted_idempotency_key=f"copier-live:failed-{sequence}", status=ExecutionJob.Status.FAILED,
            result={"status": "FAILED", "error": "timeout"})
        rep = CopierReplication.objects.create(
            master_event=ev, follower=self.f, requested_op="OPEN", execution_job=job,
            status=ReplicationStatus.EXECUTION_FAILED, follower_symbol="XAUUSD.std",
            constraints_authoritative=True, sltp_translation_ok=True)
        if not recent:   # push the failure beyond the failed-filled lookback (bypass auto_now via .update)
            CopierReplication.objects.filter(id=rep.id).update(
                updated_at=timezone.now() - timedelta(seconds=99_999))
        return rep

    def test_teardown_blocks_on_recent_failed_order_possibly_filled(self):
        # The one VERIFIED open is cleanly closed; the ONLY open evidence is a recent FAILED order that may have
        # filled (lost-ACK). Teardown must NOT declare DARK — the real position could be open with no ticket.
        self._verified_close()
        self._mark_jobs_success()                       # the close job + the original open job are SUCCESS
        self._failed_open(recent=True)
        res = teardown_live_dark(
            master_account_id=self.master_acct.id, follower_account_id=self.follower_acct.id,
            admission_restore=self._ok_actuator(), emitter_disabler=self._ok_actuator(),
            emitter_key_remover=self._ok_actuator())
        self.assertTrue(res["cleanup_required"])
        self.assertFalse(res["dark"])
        reasons = {u["reason"] for u in res["steps"]["flatness"]["unflattened"]}
        self.assertIn("failed_order_possibly_filled", reasons)
        self.assertNotIn("host_admission_off", res["steps"])

    def test_teardown_reaches_dark_when_failed_order_beyond_lookback(self):
        # Beyond the failed-filled ambiguity window the execution_health backstop has run its course → resolved.
        self._verified_close()
        self._mark_jobs_success()
        self._failed_open(recent=False)
        res = teardown_live_dark(
            master_account_id=self.master_acct.id, follower_account_id=self.follower_acct.id,
            admission_restore=self._ok_actuator(), emitter_disabler=self._ok_actuator(),
            emitter_key_remover=self._ok_actuator())
        self.assertFalse(res.get("cleanup_required", False))
        self.assertTrue(res["backend_dark"], res)

    def test_teardown_blocks_and_mutates_nothing_while_position_open(self):
        # The fixture's MATCHED OPEN (EXECUTION_VERIFIED, LIVE job) is unflattened.
        res = teardown_live_dark(
            master_account_id=self.master_acct.id, follower_account_id=self.follower_acct.id,
            admission_restore=self._ok_actuator(), emitter_disabler=self._ok_actuator(),
            emitter_key_remover=self._ok_actuator())
        self.assertTrue(res["cleanup_required"])
        self.assertFalse(res["dark"])
        self.assertFalse(res["backend_dark"])
        self.assertEqual(res["steps"]["flatness"]["unflattened_count"], 1)
        # NO mutation performed: the arm is still engaged, the admission step never ran.
        from trade_copier.models import CopierLiveArm, CopierLiveArmState
        self.assertTrue(CopierLiveArm.objects.filter(
            master_account_id=self.master_acct.id, follower_account_id=self.follower_acct.id,
            state=CopierLiveArmState.ENGAGED).exists())
        self.assertNotIn("host_admission_off", res["steps"])

    def test_teardown_reaches_dark_once_position_proven_closed(self):
        self._verified_close()
        self._mark_jobs_success()
        res = teardown_live_dark(
            master_account_id=self.master_acct.id, follower_account_id=self.follower_acct.id,
            admission_restore=self._ok_actuator(), emitter_disabler=self._ok_actuator(),
            emitter_key_remover=self._ok_actuator())
        self.assertFalse(res.get("cleanup_required", False))
        self.assertTrue(res["backend_dark"], res)
        self.assertTrue(res["verification"]["open_copier_positions"]["proven_flat"])
        self.assertTrue(res["dark"])   # backend DARK AND all three injected host actuators attested
