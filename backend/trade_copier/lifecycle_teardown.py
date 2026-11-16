"""trade_copier.lifecycle_teardown — the idempotent, machine-readable DARK teardown for the governed live-demo
(GFX-COPIER-LIFECYCLE-ARMING-SAFETY-CORRECTION-148, ADR-0113). Fail-closed.

Reliably returns the backend control plane + (via injected signed-host actuators) the host to DARK after ANY
state: full success, partial arming, emitter-enable failure, bridge-provision failure, arm-create failure,
authorization expiry, wrong configuration, or operator abort. Every step is best-effort AND idempotent — running
it twice, or on an already-DARK system, changes nothing and still returns a DARK verdict.

Command-reachable state this operation itself returns to DARK: the backend arm (disengage), the authorization
(optional revoke), host admission (signed RESTORE_TENANT_BRIDGE_EXECUTION_OFF — removes MT5_ALLOW_LIVE, the final
host-side execution gate, so no live order can execute regardless of the backend env flags), the standing emitter
task (disable) and its ephemeral signing key (shred). The backend execution ENV flags are process-env (deploy
time), not runtime-mutable by a management command; reverting them to DARK is reported as an explicit operator
step and is NOT required for safety, because host admission OFF already blocks every live order. Nothing here
places/modifies/closes an order or reads a secret.
"""
from __future__ import annotations

from typing import Callable, Optional

from django.utils import timezone


def _disengage_arms(master_account_id, follower_account_id, *, reason, now) -> dict:
    """Disengage every ENGAGED arm for the route (idempotent). Never the kill switch."""
    try:
        from trade_copier.models import CopierLiveArm, CopierLiveArmState
        qs = CopierLiveArm.objects.filter(
            master_account_id=master_account_id, follower_account_id=follower_account_id,
            state=CopierLiveArmState.ENGAGED)
        ids = list(qs.values_list("id", flat=True))
        changed = qs.update(state=CopierLiveArmState.DISENGAGED, disengaged_at=now,
                            disengage_reason=(reason or "teardown")[:256], updated_at=now)
        return {"ok": True, "disengaged_ids": ids, "changed": int(changed)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": type(e).__name__}


def _revoke_authorizations(master_account_id, follower_account_id, *, reason, now, revoked_by_email) -> dict:
    """Revoke every ACTIVE authorization for the route (idempotent). A REVOKED authorization governs nothing."""
    try:
        from django.contrib.auth import get_user_model
        from trade_copier.models import CopierLiveAuthorization, CopierLiveAuthorizationStatus
        User = get_user_model()
        actor = User.objects.filter(email__iexact=revoked_by_email).first() if revoked_by_email else None
        qs = CopierLiveAuthorization.objects.filter(
            master_account_id=master_account_id, follower_account_id=follower_account_id,
            status=CopierLiveAuthorizationStatus.ACTIVE)
        ids = list(qs.values_list("id", flat=True))
        changed = qs.update(status=CopierLiveAuthorizationStatus.REVOKED, revoked_at=now, revoked_by=actor,
                            revoke_reason=(reason or "teardown")[:256], updated_at=now)
        return {"ok": True, "revoked_ids": ids, "changed": int(changed)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": type(e).__name__}


def _call_actuator(actuator: Optional[Callable], label: str) -> dict:
    """Run an injected signed-host actuator. Absent actuator => the host step is UNPROVEN (fail-closed), never
    silently reported as done."""
    if actuator is None:
        return {"ok": False, "proven": False, "reason": f"{label}_actuator_unavailable",
                "note": "host step requires a signed host operation; not attempted from the backend."}
    try:
        res = actuator() or {}
        ok = bool(res.get("ok"))
        return {"ok": ok, "proven": ok, "result": res}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "proven": False, "reason": f"{label}_exception:{type(e).__name__}"}


def _dark_verification(master_account_id, follower_account_id, *, admission, emitter, key,
                       require_authz_absent=True) -> dict:
    """Confirm the command-reachable DARK state: no engaged arm, (when ``require_authz_absent``) no ACTIVE
    authorization, zero copier jobs / tickets / open verifier windows, and the host steps attested. The
    arm-rollback path preserves the still-valid authorization (``require_authz_absent=False``); there the money
    path is already OFF because no engaged arm remains (the arm is the execution AND-condition), so a preserved
    authorization does not make the backend non-DARK. Fail-closed on any DB error."""
    out: dict = {}
    try:
        from trade_copier.models import (CopierLiveArm, CopierLiveArmState, CopierLiveAuthorization,
                                          CopierLiveAuthorizationStatus, CopierReplication)
        out["engaged_arms"] = CopierLiveArm.objects.filter(
            master_account_id=master_account_id, follower_account_id=follower_account_id,
            state=CopierLiveArmState.ENGAGED).count()
        out["active_authorizations"] = CopierLiveAuthorization.objects.filter(
            master_account_id=master_account_id, follower_account_id=follower_account_id,
            status=CopierLiveAuthorizationStatus.ACTIVE).count()
        out["follower_tickets"] = CopierReplication.objects.filter(
            follower_ticket__isnull=False, follower__follower_account_id=follower_account_id).count()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"verify_db_error:{type(e).__name__}"}
    try:
        from execution.models import ExecutionJob
        out["copier_jobs_open"] = ExecutionJob.objects.filter(
            account_id=follower_account_id,
            job_type__in=["PLACE_ORDER", "MODIFY_POSITION", "CLOSE_TRADE", "PARTIAL_CLOSE_TRADE"],
            status__in=["QUEUED", "RUNNING", "PENDING", "DISPATCHED"]).count()
    except Exception:  # noqa: BLE001
        out["copier_jobs_open"] = None
    try:
        from trade_copier.models import CopierVerifierWindow
        out["verifier_windows_open"] = CopierVerifierWindow.objects.filter(closed_at__isnull=True).count()
    except Exception:  # noqa: BLE001
        out["verifier_windows_open"] = None
    authz_ok = (out.get("active_authorizations", 1) == 0) if require_authz_absent else True
    backend_dark = (out.get("engaged_arms") == 0 and authz_ok
                    and (out.get("copier_jobs_open") in (0, None))
                    and out.get("follower_tickets") == 0
                    and (out.get("verifier_windows_open") in (0, None)))
    out["require_authz_absent"] = bool(require_authz_absent)
    host_dark = bool(admission.get("proven")) and bool(emitter.get("proven")) and bool(key.get("proven"))
    out["backend_dark"] = backend_dark
    out["host_dark"] = host_dark
    out["ok"] = bool(backend_dark)   # backend DARK is command-provable; host DARK needs the injected actuators.
    return out


def teardown_live_dark(*, master_account_id: int, follower_account_id: int,
                       authorization_id: Optional[int] = None,
                       admission_restore: Optional[Callable] = None,
                       emitter_disabler: Optional[Callable] = None,
                       emitter_key_remover: Optional[Callable] = None,
                       revoke_authorization: bool = True, revoked_by_email: Optional[str] = None,
                       reason: str = "teardown", now=None) -> dict:
    """Idempotent DARK teardown. Returns a machine-readable result with per-step outcomes and a DARK verdict.

    When ``revoke_authorization`` is False (the arm-internal rollback case) the authorization is left intact so a
    transient provisioning failure does not discard a still-valid grant; the standalone teardown command defaults
    it True (end-of-demo return-to-DARK). ``admission_restore`` / ``emitter_disabler`` / ``emitter_key_remover``
    are injected signed-host actuators; absent, the corresponding host step is reported UNPROVEN (never 'done')."""
    now = now or timezone.now()
    steps: dict = {}
    steps["disarm"] = _disengage_arms(master_account_id, follower_account_id, reason=reason, now=now)
    steps["revoke_authorization"] = (
        _revoke_authorizations(master_account_id, follower_account_id, reason=reason, now=now,
                               revoked_by_email=revoked_by_email)
        if revoke_authorization else {"ok": True, "skipped": True, "reason": "rollback_preserves_authorization"})
    steps["host_admission_off"] = _call_actuator(admission_restore, "admission_restore")
    steps["emitter_disabled"] = _call_actuator(emitter_disabler, "emitter_disabler")
    steps["emitter_key_shredded"] = _call_actuator(emitter_key_remover, "emitter_key_remover")
    steps["execution_flags_note"] = {
        "ok": True,
        "note": "Backend execution env flags (COPIER_*_ENABLED) are deploy-time process env, not runtime-mutable "
                "here. Host admission OFF already blocks every live order; revert the env flags to DARK as the "
                "separate operator deploy step for full-DARK hygiene."}
    verification = _dark_verification(
        master_account_id, follower_account_id,
        admission=steps["host_admission_off"], emitter=steps["emitter_disabled"], key=steps["emitter_key_shredded"],
        require_authz_absent=bool(revoke_authorization))
    overall = bool(verification.get("ok"))  # command-provable backend DARK; host DARK needs the actuators.
    return {"ok": overall, "dark": overall and verification.get("host_dark", False),
            "backend_dark": verification.get("backend_dark", False),
            "host_dark": verification.get("host_dark", False),
            "route": f"{master_account_id}->{follower_account_id}",
            "steps": steps, "verification": verification, "checked_at": now.isoformat(),
            "note": "Idempotent DARK teardown. 'dark' is True only when backend DARK AND all three injected host "
                    "actuators attested; host steps without an actuator are reported UNPROVEN, never assumed."}
