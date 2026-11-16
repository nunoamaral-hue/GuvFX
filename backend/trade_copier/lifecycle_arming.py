"""trade_copier.lifecycle_arming — ONE governed, atomic OPEN+MODIFY+FULL_CLOSE live-demo arming operation
(GFX-COPIER-LIFECYCLE-ARMING-SAFETY-CORRECTION-148, ADR-0113). Fail-closed; atomic from the operator's view.

The pre-pkt-148 sequence could arm OPEN (``provision_authorized_live_copier_bridge``) without proving the
authorized MODIFY and FULL_CLOSE lifecycle was also executable, and could enable the immediate emitter without
proving its signing key was present. Either gap could strand a real position on the live follower, or silently
fail the <=1s path. This operation closes both: it validates the COMPLETE contract first, then provisions host
admission, enables the emitter, and creates the arm — in that order, each verified by read-back — and on ANY
failure rolls the ENTIRE stack back to DARK via ``lifecycle_teardown.teardown_live_dark``.

Everything that touches the Windows host is INJECTED (``admission_provisioner`` / ``emitter_enabler`` /
``*_probe``). The module itself performs NO host contact, NO broker order, holds/reads NO secret, and makes NO
DB mutation until ``commit=True`` and every gate has passed. Repository tests inject fakes; a real run supplies
signed-host-executor-backed actuators out of band (a separate, Sponsor-gated step — never in this packet).

Ordering invariant (fail-closed): VALIDATE ALL -> provision admission -> enable emitter -> create arm -> verify.
A failure at any post-validation step triggers full teardown before returning, so a partial arm never persists.
"""
from __future__ import annotations

from typing import Callable, Optional

from django.db import transaction
from django.utils import timezone

from trade_copier import lifecycle_arm_contract as contract


# ── read-only backend facts ──────────────────────────────────────────────────────────────────────────────────
def master_clean(master_account_id: int):
    """``(clean, watermark_seq, open_positions)`` from the authoritative MasterEvent ledger — the same derivation
    the read-only preflight uses. Fail-closed: no CopierMaster -> (False, None, None)."""
    try:
        from django.db.models import Max
        from trade_copier.models import CopierMaster, MasterEvent
        cm = CopierMaster.objects.filter(trading_account_id=master_account_id).first()
        if cm is None:
            return (False, None, None)
        wm = cm.events.aggregate(m=Max("sequence")).get("m") or 0
        latest: dict = {}
        for r in (MasterEvent.objects.filter(master=cm).order_by("position_ticket", "sequence")
                  .values("position_ticket", "event_type")):
            latest[r["position_ticket"]] = r["event_type"]
        open_pos = sum(1 for et in latest.values() if et not in ("FULL_CLOSE", "CLOSE"))
        return (open_pos == 0, int(wm), int(open_pos))
    except Exception:  # noqa: BLE001 — fail closed
        return (False, None, None)


def _probe_bool(probe: Optional[Callable], key: str):
    """Run an injected tri-state probe, returning its boolean under ``key`` or ``None`` (unverified/fail-closed)."""
    if probe is None:
        return None
    try:
        res = probe() or {}
        v = res.get(key, None)
        return bool(v) if v is not None else None
    except Exception:  # noqa: BLE001 — a failed probe is UNVERIFIED, never assumed good.
        return None


def _validate(*, master_account_id, follower_account_id, source_symbol, follower_symbol, authorization_id,
              follower_flat_confirmed, emitter_key_probe, host_admission_probe, emitter_task_probe, now,
              check_host_entry=True):
    """Evaluate the COMPLETE arm-time contract read-only. Returns ``(authz, result_dict)`` where result_dict has
    ``ok`` and, on failure, a de-duplicated ``blockers`` list. Mutates nothing.

    ``check_host_entry`` gates the two ENTRY-PRECONDITION host gates (admission currently OFF, emitter task
    currently DISABLED). It is True for the pre-arm validation; the post-arm re-verification passes False because
    those two are DELIBERATELY flipped by the arming sequence (admission provisioned ON, emitter enabled) and are
    already confirmed by the provision/emitter read-backs — re-requiring them OFF would spuriously tear down a
    correctly-armed stack. Every money-path INVARIANT (authority + flag coherence, master-clean, kill switch,
    identity, authz window, emitter-key readiness, follower flatness, no conflicting arm) is still re-checked."""
    from trade_copier.models import (CopierLiveArm, CopierLiveArmState, CopierLiveAuthorization,
                                      CopierLiveAuthorizationStatus)
    from trade_copier.live_execution import _identities_match, kill_switch_engaged

    blockers: list[str] = []
    authz = CopierLiveAuthorization.objects.filter(pk=authorization_id).select_related(
        "master_account", "follower_account").first()
    if authz is None:
        return (None, {"ok": False, "stage": "validate", "blockers": [contract.AUTHZ_NONE]})

    # 1 — authorization status / window / allowance
    if authz.status != CopierLiveAuthorizationStatus.ACTIVE or authz.revoked_at is not None:
        blockers.append("AUTHZ_NOT_ACTIVE")
    if now >= authz.expires_at or now < authz.valid_from:
        blockers.append("AUTHZ_OUT_OF_WINDOW")
    if authz.orders_consumed >= authz.max_orders:
        blockers.append("AUTHZ_ALLOWANCE_CONSUMED")

    # 2 — route / symbol / caps / lifecycle authority (authority-layer contract)
    blockers += contract.authority_contract_blockers(
        authz, source_symbol=source_symbol, follower_symbol=follower_symbol,
        master_account_id=master_account_id, follower_account_id=follower_account_id)

    # 3 — FLAG-layer coherence (this operation runs at ARM time, flags ON): OPEN never without executable CLOSE.
    blockers += contract.execution_flag_blockers(authz)

    # 4 — identity pin (exact master/follower login/server/environment)
    ok_id, reason_id = _identities_match(authz, authz.master_account, authz.follower_account)
    if not ok_id:
        blockers.append(f"IDENTITY_MISMATCH:{reason_id}")

    # 5 — kill switch clear
    if kill_switch_engaged():
        blockers.append("KILL_SWITCH_ENGAGED")

    # 6 — sole ACTIVE authorization + no conflicting engaged arm for the route
    if CopierLiveAuthorization.objects.filter(
            master_account_id=authz.master_account_id, follower_account_id=authz.follower_account_id,
            status=CopierLiveAuthorizationStatus.ACTIVE).count() != 1:
        blockers.append("AUTHZ_NOT_SOLE_ACTIVE")
    if CopierLiveArm.objects.filter(
            master_account_id=authz.master_account_id, follower_account_id=authz.follower_account_id,
            state=CopierLiveArmState.ENGAGED).exclude(authorization_id=authz.id).exists():
        blockers.append("CONFLICTING_ENGAGED_ARM")

    # 7 — no in-flight follower job
    try:
        from execution.models import ExecutionJob
        inflight = [getattr(ExecutionJob.Status, s) for s in ("RUNNING", "PENDING", "DISPATCHED", "QUEUED")
                    if hasattr(ExecutionJob.Status, s)]
        if ExecutionJob.objects.filter(account_id=authz.follower_account_id, status__in=inflight).exists():
            blockers.append("FOLLOWER_JOB_IN_FLIGHT")
    except Exception:  # noqa: BLE001 — fail closed
        blockers.append("FOLLOWER_JOB_PROBE_ERROR")

    # 8 — #46 clean at the watermark
    clean, wm, open_pos = master_clean(master_account_id)
    if not clean:
        blockers.append("MASTER_NOT_CLEAN")

    # 9 — host / emitter / human tri-state gates (only an explicit True clears a gate)
    key_probe = (emitter_key_probe() if emitter_key_probe else {}) or {}
    key_ready = key_probe.get("ready", None)
    key_identity_ok = key_probe.get("identity_ok", None)
    emitter_key_ready: Optional[bool]
    if key_ready is True and key_identity_ok is False:
        emitter_key_ready = "identity_invalid"  # sentinel -> EMITTER_KEY_IDENTITY_INVALID
    elif key_ready is True:
        emitter_key_ready = True
    else:
        emitter_key_ready = None if key_ready is None else False
    if check_host_entry:
        host_admission_off = _probe_bool(host_admission_probe, "off")
        emitter_task_disabled = _probe_bool(emitter_task_probe, "disabled")
    else:
        # Post-arm: admission is intentionally ON and the emitter ENABLED (confirmed by the provision/emitter
        # read-backs). Treat these two entry gates as satisfied so the standing-invariant re-check does not
        # roll back a correctly-armed stack; all other invariants are still enforced below.
        host_admission_off = True
        emitter_task_disabled = True

    readiness = contract.evaluate_arm_readiness(
        authz, emitter_key_ready=emitter_key_ready, follower_flat_confirmed=bool(follower_flat_confirmed) or None,
        host_admission_off=host_admission_off, emitter_task_disabled=emitter_task_disabled,
        require_flag_coherence=True, source_symbol=source_symbol, follower_symbol=follower_symbol,
        master_account_id=master_account_id, follower_account_id=follower_account_id)
    blockers += readiness["blockers"]

    blockers = sorted(set(blockers))
    return (authz, {"ok": not blockers, "stage": "validate", "blockers": blockers,
                    "master_watermark_seq": wm, "master_open_positions": open_pos,
                    "readiness": readiness})


def _create_arm(authz, *, by_email, reason, now):
    """Create exactly one ENGAGED, route-exact, authorization-bound arm (flags-ON-aware; the atomic operation's
    own arm creation — it does NOT reuse the pre-arm flags-off preflight). Idempotent under the authorization
    lock. Returns ``(arm, idempotent)``."""
    from django.contrib.auth import get_user_model
    from trade_copier.models import CopierLiveArm, CopierLiveArmState, CopierLiveAuthorization, \
        CopierLiveAuthorizationStatus
    User = get_user_model()
    approver = User.objects.filter(email__iexact=by_email).first() if by_email else None
    with transaction.atomic():
        locked = (CopierLiveAuthorization.objects.select_for_update()
                  .filter(id=authz.id, status=CopierLiveAuthorizationStatus.ACTIVE,
                          revoked_at__isnull=True).first())
        if locked is None or now >= locked.expires_at:
            raise RuntimeError("authorization no longer ACTIVE/unexpired under lock")
        existing = CopierLiveArm.objects.filter(
            authorization_id=locked.id, state=CopierLiveArmState.ENGAGED).first()
        if existing is not None:
            return existing, True
        arm = CopierLiveArm.objects.create(
            authorization=locked, owner_id=locked.user_id,
            master_account_id=locked.master_account_id, follower_account_id=locked.follower_account_id,
            source_symbol=locked.source_symbol, follower_symbol=locked.follower_symbol,
            state=CopierLiveArmState.ENGAGED, armed_by=approver,
            expires_at=locked.expires_at, correlation_id=(locked.correlation_id or "")[:64])
        return arm, False


def arm_live_lifecycle(*, master_account_id: int, follower_account_id: int, source_symbol: str,
                       follower_symbol: str, authorization_id: int, follower_flat_confirmed: bool,
                       emitter_key_probe: Optional[Callable] = None,
                       host_admission_probe: Optional[Callable] = None,
                       emitter_task_probe: Optional[Callable] = None,
                       admission_provisioner: Optional[Callable] = None,
                       emitter_enabler: Optional[Callable] = None,
                       teardown: Optional[Callable] = None,
                       commit: bool = False, by_email: Optional[str] = None, reason: str = "",
                       now=None) -> dict:
    """Validate the complete contract; (commit) provision admission -> enable emitter -> create arm -> verify, with
    FULL rollback to DARK on any failure. Default dry-run mutates nothing. Returns a machine-readable dict."""
    now = now or timezone.now()
    authz, v = _validate(
        master_account_id=master_account_id, follower_account_id=follower_account_id,
        source_symbol=source_symbol, follower_symbol=follower_symbol, authorization_id=authorization_id,
        follower_flat_confirmed=follower_flat_confirmed, emitter_key_probe=emitter_key_probe,
        host_admission_probe=host_admission_probe, emitter_task_probe=emitter_task_probe, now=now)
    if not v["ok"]:
        return {"ok": False, "armed": False, "stage": "validate", "reason": "contract_refused",
                "blockers": v["blockers"], "detail": v}
    if not commit:
        return {"ok": True, "armed": False, "dry_run": True, "reason": "dry_run_contract_ok", "detail": v}

    def _default_admission_restore():
        # Signed host-admission OFF — idempotent, needs no flags/authorization; so a partial arm that provisioned
        # host admission can never leave the host LIVE-admitted. Fail-closed if the signed executor is absent.
        from trade_copier.live_bridge_provision import restore_off
        return restore_off(int(follower_account_id), commit=True)

    def _teardown(reason_code):
        try:
            if teardown is not None:
                td = teardown()
            else:
                from trade_copier.lifecycle_teardown import teardown_live_dark
                td = teardown_live_dark(
                    master_account_id=master_account_id, follower_account_id=follower_account_id,
                    authorization_id=authorization_id, admission_restore=_default_admission_restore,
                    emitter_disabler=None, emitter_key_remover=None, now=timezone.now(),
                    revoke_authorization=False)
        except Exception as e:  # noqa: BLE001 — teardown must never raise past the caller
            td = {"ok": False, "reason": f"teardown_error:{type(e).__name__}"}
        return {"ok": False, "armed": False, "reason": reason_code, "rolled_back": td, "detail": v}

    # 1 — provision host admission (signed, account-scoped). Must attest LIVE for the exact pair.
    if admission_provisioner is None:
        return _teardown("admission_provisioner_unavailable")
    try:
        prov = admission_provisioner(authz)
    except Exception as e:  # noqa: BLE001
        return _teardown(f"admission_exception:{type(e).__name__}")
    if not (isinstance(prov, dict) and prov.get("ok") and prov.get("armed")):
        return _teardown("admission_failed")

    # 2 — enable the immediate emitter ONLY now that admission is attested (key readiness already proven in 9).
    if emitter_enabler is None:
        return _teardown("emitter_enabler_unavailable")
    try:
        em = emitter_enabler()
    except Exception as e:  # noqa: BLE001
        return _teardown(f"emitter_exception:{type(e).__name__}")
    if not (isinstance(em, dict) and em.get("ok") and em.get("enabled")):
        return _teardown("emitter_enable_failed")

    # 3 — create the arm LAST (the final AND-condition), after the whole stack is attested.
    try:
        arm, idempotent = _create_arm(authz, by_email=by_email, reason=reason, now=now)
    except Exception as e:  # noqa: BLE001
        return _teardown(f"arm_create_exception:{type(e).__name__}")

    # 4 — final standing-invariant re-verification (TOCTOU): every money-path invariant must still hold with the
    # arm now present. The two entry-precondition host gates (admission OFF, emitter task disabled) are
    # DELIBERATELY flipped by steps 1-2 and already attested by their read-backs, so check_host_entry=False
    # excludes them — otherwise a correctly-armed stack would always be torn down.
    _authz2, v2 = _validate(
        master_account_id=master_account_id, follower_account_id=follower_account_id,
        source_symbol=source_symbol, follower_symbol=follower_symbol, authorization_id=authorization_id,
        follower_flat_confirmed=follower_flat_confirmed, emitter_key_probe=emitter_key_probe,
        host_admission_probe=host_admission_probe, emitter_task_probe=emitter_task_probe, now=timezone.now(),
        check_host_entry=False)
    residual = [b for b in v2["blockers"]]
    if residual:
        return _teardown("post_arm_verify_failed")

    return {"ok": True, "armed": True, "reason": "lifecycle_armed",
            "arm_id": arm.id, "arm_idempotent": idempotent,
            "route": f"{master_account_id}->{follower_account_id}",
            "symbol": f"{source_symbol}->{follower_symbol}",
            "admission": prov, "emitter": em, "detail": v,
            "note": "OPEN+MODIFY+FULL_CLOSE armed atomically; partial-close excluded; full rollback on any failure."}
