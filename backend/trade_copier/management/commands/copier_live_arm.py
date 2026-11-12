"""copier_live_arm — the ONE-ACTION, authorization-bound, auto-expiring backend ARM / DISARM (ADR-0097).

Replaces the per-demo multi-flag/worker ceremony with a single audited command. Engaging creates exactly one
``CopierLiveArm`` bound to an existing ACTIVE ``CopierLiveAuthorization`` and its exact route; the LIVE admission
(``trade_copier.live_execution``) requires that engaged, unexpired, route-exact arm as an ADDITIONAL AND
alongside the env gates and the authorization. Disengaging (``--disengage``) returns the backend control plane to
OFF for that route.

This command is CONTROL-PLANE ONLY. It NEVER: alters Windows-host admission, enables a scheduled task, starts a
worker, creates an ``ExecutionJob``, places/modifies/closes an order, or stores/prints a credential. Signed host
admission remains a SEPARATE operator action after an independent read-back. Backend DISARM does NOT prove the
Windows host admission is OFF — that is reported explicitly and restored by its own signed operation.
"""
from __future__ import annotations

import io
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

User = get_user_model()

_DEFAULT_MIN_WINDOW_MIN = 5


class Command(BaseCommand):
    help = "Engage (or --disengage) the one-action, authorization-bound backend LIVE-copier arm (ADR-0097)."

    def add_arguments(self, parser):
        parser.add_argument("--authorization-id", required=True, type=int,
                            help="The exact ACTIVE CopierLiveAuthorization id to arm.")
        parser.add_argument("--by", required=True, help="STAFF operator/approver email (recorded for audit).")
        parser.add_argument("--reason", required=True, help="Why (recorded for audit).")
        parser.add_argument("--disengage", action="store_true", default=False,
                            help="Disengage the engaged arm for this authorization (return-to-OFF; no kill switch).")
        parser.add_argument("--min-window-minutes", type=int, default=_DEFAULT_MIN_WINDOW_MIN,
                            help="Minimum useful remaining authorization window required to ARM.")
        parser.add_argument("--valid-minutes", type=int, default=0,
                            help="Optional: cap arm expiry to now+N (always also capped at the authorization "
                                 "expiry). 0 = use the authorization expiry.")
        parser.add_argument("--no-require-master-flat", action="store_true", default=False,
                            help="Do not require the master to be flat at arm time (default: require flat).")

    # ────────────────────────────── DISARM (return-to-OFF; never the kill switch) ──────────────────────────────
    def _disengage(self, o, authz):
        from trade_copier.models import CopierLiveArm, CopierLiveArmState
        actor = User.objects.filter(email__iexact=o["by"]).first()
        now = timezone.now()
        with transaction.atomic():
            arm = (CopierLiveArm.objects.select_for_update()
                   .filter(authorization_id=authz.id, state=CopierLiveArmState.ENGAGED).first())
            if arm is None:
                self.stdout.write(json.dumps({"action": "disengage", "changed": False,
                                              "reason": "no_engaged_arm", "authorization_id": authz.id}))
                return
            arm.state = CopierLiveArmState.DISENGAGED
            arm.disengaged_at = now
            arm.disengaged_by = actor
            arm.disengage_reason = (o["reason"] or "")[:256]
            arm.save(update_fields=["state", "disengaged_at", "disengaged_by", "disengage_reason", "updated_at"])
        self.stdout.write(json.dumps({
            "action": "disengage", "changed": True, "arm_id": arm.id, "authorization_id": authz.id,
            "route": f"{arm.master_account_id}->{arm.follower_account_id}",
            "disengaged_at": now.isoformat(), "by": getattr(actor, "email", o["by"]),
            "backend_arm": "OFF",
            "host_admission_note": "Backend arm disengaged. This does NOT restore Windows host admission — run the "
                                   "signed restore_copier_bridge_execution_off and verify independently."}))

    def handle(self, *args, **o):
        from trade_copier.models import (CopierLiveArm, CopierLiveArmState, CopierLiveAuthorization,
                                         CopierLiveAuthorizationStatus)
        from trade_copier.live_execution import kill_switch_engaged, _identities_match

        authz = CopierLiveAuthorization.objects.filter(id=o["authorization_id"]).first()
        if authz is None:
            raise CommandError(f"authorization id={o['authorization_id']} not found")

        if o["disengage"]:
            return self._disengage(o, authz)

        # ───────────────────────────── ARM: fail closed unless every backend gate passes ─────────────────────────
        now = timezone.now()
        approver = User.objects.filter(email__iexact=o["by"]).first()
        if approver is None or not approver.is_staff:
            raise CommandError("--by must be an existing STAFF user (non-staff approver rejected)")

        # 1. Authorization ACTIVE, unexpired, not revoked, with a useful remaining window + entry allowance.
        if authz.status != CopierLiveAuthorizationStatus.ACTIVE or authz.revoked_at is not None:
            raise CommandError(f"authorization not ACTIVE (status={authz.status}, revoked={authz.revoked_at})")
        if now >= authz.expires_at:
            raise CommandError("authorization already expired")
        remaining_min = (authz.expires_at - now).total_seconds() / 60.0
        if remaining_min < float(o["min_window_minutes"]):
            raise CommandError(f"insufficient remaining window ({remaining_min:.1f}m < "
                               f"{o['min_window_minutes']}m minimum)")
        if authz.orders_consumed >= authz.max_orders:
            raise CommandError("authorization entry allowance already consumed")

        # 2. Sole ACTIVE authorization for the pair (the model constraint guarantees it; verify defensively).
        active_pair = CopierLiveAuthorization.objects.filter(
            master_account_id=authz.master_account_id, follower_account_id=authz.follower_account_id,
            status=CopierLiveAuthorizationStatus.ACTIVE).count()
        if active_pair != 1:
            raise CommandError(f"route is not the sole ACTIVE authorization (found {active_pair})")

        # 3. Kill switch clear.
        if kill_switch_engaged():
            raise CommandError("kill switch engaged — refuse to arm")

        # 4. No conflicting ENGAGED arm for this route.
        if CopierLiveArm.objects.filter(
                master_account_id=authz.master_account_id, follower_account_id=authz.follower_account_id,
                state=CopierLiveArmState.ENGAGED).exclude(authorization_id=authz.id).exists():
            raise CommandError("a conflicting ENGAGED arm exists for this route")

        # 5. Identity match (exact pinned master/follower login/server/environment) — reuse the admission check.
        macct = authz.master_account
        facct = authz.follower_account
        ok, reason = _identities_match(authz, macct, facct)
        if not ok:
            raise CommandError(f"identity mismatch: {reason}")

        # 6. No conflicting in-flight copier job for the follower (RUNNING/PENDING PLACE/MODIFY/CLOSE).
        from execution.models import ExecutionJob
        inflight = [getattr(ExecutionJob.Status, s) for s in ("RUNNING", "PENDING", "DISPATCHED")
                    if hasattr(ExecutionJob.Status, s)]
        if ExecutionJob.objects.filter(account_id=facct.id, status__in=inflight).exists():
            raise CommandError("a conflicting in-flight job exists for the follower — refuse to arm")

        # 7. Master flat baseline (backend-verifiable via the latest observation poll), unless waived.
        if not o["no_require_master_flat"]:
            from trade_copier.models import CopierMaster, MasterObservationPoll
            cm = CopierMaster.objects.filter(trading_account_id=macct.id).first()
            poll = (MasterObservationPoll.objects.filter(master=cm).order_by("-id").first() if cm else None)
            if poll is None:
                raise CommandError("no master observation available to confirm a flat baseline")
            if int(getattr(poll, "position_count", 0) or 0) != 0:
                raise CommandError(f"master not flat (position_count={poll.position_count}) — refuse to arm")

        # 8. Re-run the AUTHORITATIVE backend preflight; host-probe items are the separate operator step and do
        #    NOT block the backend arm, but any backend blocker fails closed.
        buf = io.StringIO()
        try:
            call_command("copier_live_demo_preflight",
                         master_account_id=macct.id, follower_account_id=facct.id,
                         source_symbol=authz.source_symbol, follower_symbol=authz.follower_symbol, stdout=buf)
            pf = json.loads(buf.getvalue() or "{}")
        except Exception as e:  # noqa: BLE001 — a preflight that cannot run is fail-closed.
            raise CommandError(f"preflight could not be evaluated: {e}")
        backend_blockers = [b for b in (pf.get("blockers") or [])]
        if backend_blockers:
            raise CommandError(f"preflight backend blockers: {backend_blockers}")

        # 9-10. Persist exactly one engaged arm atomically under the authorization lock. The idempotency check
        #       is INSIDE the lock, so two concurrent ARM calls for the same authorization serialize on the authz
        #       row and the second sees the first's arm (no duplicate, no IntegrityError race).
        cap_expiry = authz.expires_at
        if o["valid_minutes"] and int(o["valid_minutes"]) > 0:
            proposed = now + timezone.timedelta(minutes=int(o["valid_minutes"]))
            cap_expiry = min(proposed, authz.expires_at)
        idempotent = False
        with transaction.atomic():
            locked = (CopierLiveAuthorization.objects.select_for_update()
                      .filter(id=authz.id, status=CopierLiveAuthorizationStatus.ACTIVE,
                              revoked_at__isnull=True).first())
            if locked is None:
                raise CommandError("authorization no longer ACTIVE under lock — refuse to arm")
            if timezone.now() >= locked.expires_at:
                raise CommandError("authorization expired under lock — refuse to arm")
            existing = CopierLiveArm.objects.filter(
                authorization_id=locked.id, state=CopierLiveArmState.ENGAGED).first()
            if existing is not None:
                arm, idempotent = existing, True
            else:
                arm = CopierLiveArm.objects.create(
                    authorization=locked, owner_id=locked.user_id,
                    master_account_id=locked.master_account_id, follower_account_id=locked.follower_account_id,
                    source_symbol=locked.source_symbol, follower_symbol=locked.follower_symbol,
                    state=CopierLiveArmState.ENGAGED, armed_by=approver,
                    expires_at=min(cap_expiry, locked.expires_at),
                    correlation_id=(locked.correlation_id or "")[:64])
        if idempotent:
            self.stdout.write(json.dumps({
                "action": "arm", "changed": False, "idempotent": True, "arm_id": arm.id,
                "authorization_id": authz.id, "route": f"{macct.id}->{facct.id}",
                "symbol": f"{authz.source_symbol}->{authz.follower_symbol}",
                "expires_at": arm.expires_at.isoformat()}))
            return

        self.stdout.write(json.dumps({
            "action": "arm", "changed": True, "arm_id": arm.id, "authorization_id": authz.id,
            "route": f"{macct.id}->{facct.id}", "symbol": f"{authz.source_symbol}->{authz.follower_symbol}",
            "armed_by": approver.email, "armed_at": arm.armed_at.isoformat(),
            "expires_at": arm.expires_at.isoformat(),
            "op_authorities": {"modify": authz.modify_authority, "full_close": authz.full_close_authority,
                               "partial_close": authz.partial_close_authority},
            "note": "Backend control-plane arm only. NO host admission, NO worker, NO job, NO order. Signed host "
                    "admission remains a separate operator step after independent read-back."}))
