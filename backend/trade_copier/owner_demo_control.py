"""trade_copier.owner_demo_control — the owner-operable control plane for the governed #46→#48 Copy Trading
demonstration (GFX-COPIER-TENANT-OWNER-DEMO-CONTROL-170, ADR-0126).

WHY THIS EXISTS
---------------
The real-arm ceremony (grant → arm → host provisioning → teardown) was staff-and-shell only: it needed a staff
``--approved-by`` on the authorization, a verifier window opened by pasting the ingest key at a hidden prompt,
and management commands run on the host. The tenant OWNER of the governed route (currently ``support@guvfx.com``,
owner of accounts #46 and #48) must be able to drive a bounded demonstration themselves — check readiness,
prepare, deliberately confirm, and stop — WITHOUT staff access and WITHOUT pasting shell commands or secrets.

WHAT THIS IS (and what it is NOT)
---------------------------------
This module is a thin, fail-closed ORCHESTRATION layer over primitives that already exist and are unchanged:

* authorization   — a bounded ``CopierLiveAuthorization`` (ADR-0057), here OWNER-self-approved for the one
                    governed route, caps enforced server-side (≤0.01 vol, exactly one order, partial-close
                    excluded, ≤15 min), identity/symbol snapshot + future-event watermark exactly as the staff
                    grant command captures them;
* concurrency     — the governed operational ACCEPTANCE lease (``hosted_workspace.recovery_lease`` KIND_RECOVERY),
                    so a demonstration is mutually exclusive with a deploy/recovery and with a second operator
                    (single-lease, fail-closed);
* arm             — the EXISTING atomic ``trade_copier.lifecycle_arming.arm_live_lifecycle`` (validate-all →
                    provision → enable emitter → arm LAST → verify, full rollback on any failure). Its host
                    actuators/probes are INJECTED; in production they are UNPROVISIONED, so confirm fails closed
                    (DARK) until a separate, head/runtime-pinned Sponsor real-money GO wires them;
* teardown        — the EXISTING idempotent ``lifecycle_teardown.teardown_live_dark`` (disarm + revoke + signed
                    host OFF), safe to repeat.

It adds NO authority and ORs past NOTHING. It holds/reads NO secret: the browser never receives or submits a
signing key, and no symmetric key is stored in DB/session/frontend/logs/audit. Deliberate confirmation requires
a fresh re-authentication proof (verified in the API layer) plus a signed, single-EFFECT confirm token bound to
the prepared authorization — replay is neutralised by the one-ACTIVE-authz / one-ENGAGED-arm database constraints
(``uq_copier_live_authz_active_pair`` / ``uq_copier_live_arm_engaged_route``), the idempotent ``ALREADY_ARMED``
return and revoke-on-stop, not by consuming the token. Every stage is DARK unless
``copier_owner_demo_control_enabled()`` is on; with it off the endpoints return ``FEATURE_DARK`` and nothing is
created.

The preferred durable evolution (host-only PRIVATE signing key + backend-pinned PUBLIC verification key, so no
bounded demonstration ever re-enters a shared secret) is recorded as the remaining one-time provisioning decision
in ADR-0126; it is NOT performed here. Until then the standing host ingest key + the file-first hosted-executor
keyring (already provisioned, pkt-163/166C) are the signing material, delivered host-side — never via the browser.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Callable, Optional

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone

from trade_copier import flags as cf
from trade_copier import lifecycle_arm_contract as contract

logger = logging.getLogger("trade_copier.owner_demo_control")

# ── the governed demonstration route (canonical, config-pinned; overridable for non-prod/tests) ────────────────
DEMO_MAX_VOLUME: Decimal = contract.DEMO_MAX_VOLUME          # 0.01 — the invariant cap, never widened here
DEMO_MAX_ORDERS: int = contract.DEMO_MAX_ORDERS             # 1
DEMO_MAX_CONCURRENT: int = 1
DEMO_VALID_MINUTES: int = 15                                # bounded entry window
LEASE_TTL_S: int = 20 * 60                                 # covers the 15-min window + teardown slack; self-expires
CONFIRM_TOKEN_SALT = "trade_copier.owner_demo_control.confirm.v1"
CONFIRM_TOKEN_MAX_AGE_S = DEMO_VALID_MINUTES * 60          # a confirm token is useless once the prepare window lapses

_SIGNER = signing.TimestampSigner(salt=CONFIRM_TOKEN_SALT)

# Stable, value-blind reason codes (never a login/secret/balance/other-tenant datum).
R_FEATURE_DARK = "FEATURE_DARK"
R_NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
R_NOT_ROUTE_OWNER = "NOT_ROUTE_OWNER"
R_ROUTE_NOT_CONFIGURED = "ROUTE_NOT_CONFIGURED"
R_ROUTE_NOT_SOLE = "ROUTE_NOT_SOLE"
R_ACCOUNT_NOT_LIVE = "ACCOUNT_NOT_LIVE"
R_SYMBOL_UNVERIFIED = "SYMBOL_UNVERIFIED"
R_INSTRUMENT_NOT_READY = "INSTRUMENT_NOT_READY"
R_LEASE_CONFLICT = "LEASE_CONFLICT"
R_ALREADY_PREPARED = "ALREADY_PREPARED"
R_AUTHZ_EXISTS = "AUTHZ_EXISTS"
R_ARM_EXISTS = "ARM_EXISTS"
R_RECENT_AUTH_REQUIRED = "RECENT_AUTH_REQUIRED"
R_CONFIRM_TOKEN_INVALID = "CONFIRM_TOKEN_INVALID"
R_CONFIRM_TOKEN_EXPIRED = "CONFIRM_TOKEN_EXPIRED"
R_NO_PREPARED_DEMO = "NO_PREPARED_DEMO"
R_READINESS_NOT_READY = "READINESS_NOT_READY"
R_EXECUTION_ACTUATORS_UNPROVISIONED = "EXECUTION_ACTUATORS_UNPROVISIONED"


def governed_master_id() -> int:
    return int(getattr(settings, "COPIER_DEMO_MASTER_ACCOUNT_ID", 46) or 46)


def governed_follower_id() -> int:
    return int(getattr(settings, "COPIER_DEMO_FOLLOWER_ACCOUNT_ID", 48) or 48)


def governed_source_symbol() -> str:
    return str(getattr(settings, "COPIER_DEMO_SOURCE_SYMBOL", "XAUUSD") or "XAUUSD").strip()


@dataclass
class RouteContext:
    ok: bool
    reason: str
    master: object = None
    follower: object = None
    source_symbol: str = ""
    follower_symbol: str = ""
    route_id: Optional[int] = None


@dataclass
class DemoActuators:
    """Injected host-side actuators/probes for the atomic arm + teardown. ALL default None ⇒ unprovisioned ⇒
    confirm fails closed (DARK). In production nothing populates this (host wiring is the separate Sponsor-gated
    provisioning step); tests inject fakes to exercise the full arm/teardown path. NEVER carries a secret — the
    callables themselves obtain any key host-side; this object holds references only.

    CONTRACT: the actuators MUST be idempotent. The dry-run / provisioned check / commit arm run outside the
    arm-creation lock (the serialization point is ``_create_arm``'s ``select_for_update`` inside
    ``arm_live_lifecycle``), so two concurrent confirms may each invoke ``admission_provisioner`` / ``emitter_enabler``
    before one collapses to the idempotent ``ALREADY_ARMED`` return — a provisioned actuator must tolerate that."""
    emitter_key_probe: Optional[Callable] = None
    host_admission_probe: Optional[Callable] = None
    emitter_task_probe: Optional[Callable] = None
    admission_provisioner: Optional[Callable] = None
    emitter_enabler: Optional[Callable] = None
    emitter_disabler: Optional[Callable] = None
    emitter_key_remover: Optional[Callable] = None
    admission_restore: Optional[Callable] = None

    def provisioned(self) -> bool:
        """True only when the risk-INCREASING actuators needed to actually arm are present."""
        return bool(self.admission_provisioner and self.emitter_enabler
                    and self.emitter_key_probe and self.host_admission_probe and self.emitter_task_probe)


def _owner_tag(user) -> str:
    return f"user:{int(getattr(user, 'id', 0) or 0)}"


def _audit(event: str, *, user, rc: "RouteContext | None", correlation_id: str, state: str, **extra) -> None:
    """Append a value-blind audit line: actor/tenant/route/correlation/timestamp/state only. NEVER a login,
    balance, token, key, symbol-secret or other-tenant datum. Best-effort (auditing must not break a stage)."""
    rec = {
        "event": event, "ts": timezone.now().isoformat(), "state": state,
        "actor_user_id": int(getattr(user, "id", 0) or 0),
        "route": (f"{governed_master_id()}->{governed_follower_id()}"),
        "correlation_id": correlation_id or "",
    }
    # Only non-sensitive extras (booleans, counts, stable reason codes).
    for k, v in extra.items():
        if isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 64):
            rec[k] = v
    try:
        logger.info("owner_demo_control %s", rec)
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────── authorization / route resolution (fail-closed) ───────────────────────────────

def resolve_governed_demo_route(user, *, now=None) -> RouteContext:
    """Resolve the governed #46→#48 route for ``user`` FAIL-CLOSED. Permits ONLY the verified owner of BOTH
    governed accounts with the enabled, sole CopierFollower route and a verified source→follower symbol map.
    Denies staff-shortcuts (staff is NOT privileged here — ownership is), other tenants, members, and ambiguity.
    Never leaks another tenant's existence: every denial is a stable reason code only."""
    from trade_copier.models import CopierFollower, CopierMaster
    from trading.account_policy import is_live_environment
    from trade_copier.symbols import resolve_follower_symbol
    from trading.models import TradingAccount

    if user is None or not getattr(user, "is_authenticated", False):
        return RouteContext(False, R_NOT_AUTHENTICATED)
    mid, fid = governed_master_id(), governed_follower_id()
    macct = TradingAccount.objects.select_related("broker_server").filter(pk=mid).first()
    facct = TradingAccount.objects.select_related("broker_server").filter(pk=fid).first()
    # Tenant isolation FIRST, with NO existence oracle: a caller who does not own BOTH governed accounts — whether
    # because an account row is absent, belongs to another tenant, or the caller owns only ONE of the two — gets
    # the SAME R_NOT_ROUTE_OWNER code. R_ROUTE_NOT_CONFIGURED is reachable only AFTER ownership is established, so
    # a non-owner can never distinguish whether the governed account rows exist.
    if (macct is None or facct is None or macct.user_id != user.id or facct.user_id != user.id):
        return RouteContext(False, R_NOT_ROUTE_OWNER)
    if not is_live_environment(macct) or not is_live_environment(facct):
        return RouteContext(False, R_ACCOUNT_NOT_LIVE)
    cm = CopierMaster.objects.filter(trading_account_id=mid).first()
    if cm is None:
        return RouteContext(False, R_ROUTE_NOT_CONFIGURED)
    link = CopierFollower.objects.filter(master=cm, follower_account_id=fid, enabled=True).first()
    if link is None:
        return RouteContext(False, R_ROUTE_NOT_CONFIGURED)
    # Sole enabled follower for this master — fail-closed on any route ambiguity.
    if CopierFollower.objects.filter(master=cm, enabled=True).count() != 1:
        return RouteContext(False, R_ROUTE_NOT_SOLE)
    # Follower broker symbol comes from the VERIFIED canonical map, never hard-coded.
    src = governed_source_symbol()
    sym = resolve_follower_symbol(macct, src, facct)
    if not getattr(sym, "ok", False) or not getattr(sym, "follower_symbol", None):
        return RouteContext(False, R_SYMBOL_UNVERIFIED)
    return RouteContext(True, "ok", master=macct, follower=facct,
                        source_symbol=src, follower_symbol=sym.follower_symbol, route_id=link.id)


def _active_authorization(rc: RouteContext):
    from trade_copier.models import CopierLiveAuthorization, CopierLiveAuthorizationStatus
    return (CopierLiveAuthorization.objects
            .filter(master_account_id=rc.master.id, follower_account_id=rc.follower.id,
                    status=CopierLiveAuthorizationStatus.ACTIVE)
            .order_by("-id").first())


def _engaged_arm(rc: RouteContext):
    from trade_copier.models import CopierLiveArm, CopierLiveArmState
    return (CopierLiveArm.objects
            .filter(master_account_id=rc.master.id, follower_account_id=rc.follower.id,
                    state=CopierLiveArmState.ENGAGED).order_by("-id").first())


def _route_restatement(rc: RouteContext, *, expires_at=None) -> dict:
    """The value-blind contract the confirm UI MUST restate (route, caps, symbol/action, expiry, teardown)."""
    return {
        "master_account_id": rc.master.id, "follower_account_id": rc.follower.id,
        "source_symbol": rc.source_symbol, "follower_symbol": rc.follower_symbol,
        "max_volume": str(DEMO_MAX_VOLUME), "max_orders": DEMO_MAX_ORDERS,
        "max_concurrent": DEMO_MAX_CONCURRENT, "valid_minutes": DEMO_VALID_MINUTES,
        "authorities": ["MODIFY", "FULL_CLOSE"], "partial_close": False,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "teardown": "On stop / success / timeout / disconnect / any failure: arm disengaged, authorization "
                    "revoked, verifier window closed, host admission OFF, emitter disabled, lease released, "
                    "endpoint DARK.",
    }


# ─────────────────────────────── stage 1: readiness (read-only) ───────────────────────────────

def readiness(user, *, now=None, lease_path=None) -> dict:
    """Read-only, value-blind readiness for the owner. NEVER mutates. Reports authorization, the DARK backend
    gates, the arm-readiness contract (host/human tri-state unverified pre-arm) and concurrency (lease) status."""
    now = now or timezone.now()
    if not cf.copier_owner_demo_control_enabled():
        return {"enabled": False, "stage": "readiness", "reason": R_FEATURE_DARK}
    rc = resolve_governed_demo_route(user, now=now)
    if not rc.ok:
        return {"enabled": True, "stage": "readiness", "authorized": False, "reason": rc.reason}

    from execution.models import ExecutionJob
    from hosted_workspace import recovery_lease as RL
    from trade_copier import lifecycle_arming as arming
    from trade_copier import verifier_window as vw
    from trade_copier.models import CopierLiveKillSwitch

    # Execution-arm flag state — reported INFORMATIONALLY, never a prepare blocker. The base COPIER_* gates may be
    # deployment-config ON at confirm time (the arm is the additional AND), so "all flags off" is not a precondition
    # for prepare; the arm CONTRACT enforces flag coherence at confirm. We block prepare only on concrete in-flight
    # state (a live arm, an open window, an unclean master, a follower job, the kill switch, an existing authz).
    exec_arm_flags = {
        "live_execute": bool(cf.copier_live_execute_enabled()),
        "live_modify": bool(cf.copier_live_modify_enabled()),
        "live_full_close": bool(cf.copier_live_full_close_enabled()),
        "mutation_dispatch": bool(cf.copier_mutation_dispatch_enabled()),
        "mutation_transport": bool(cf.copier_mutation_transport_enabled()),
    }
    kill = CopierLiveKillSwitch.objects.filter(engaged=True).count()
    inflight = 0
    try:
        states = [getattr(ExecutionJob.Status, s) for s in ("RUNNING", "PENDING", "DISPATCHED", "QUEUED")
                  if hasattr(ExecutionJob.Status, s)]
        inflight = ExecutionJob.objects.filter(account_id=rc.follower.id, status__in=states).count()
    except Exception:  # noqa: BLE001 — fail closed (treat as unknown → not clean)
        inflight = -1
    clean, wm, open_pos = arming.master_clean(rc.master.id)
    authz = _active_authorization(rc)
    arm = _engaged_arm(rc)
    window = vw.window_status(now=now)
    lease_blocked, lease_reason = RL.blocks(RL.KIND_RECOVERY, now, lease_path)

    # The authority-layer arm readiness (flags legitimately OFF pre-arm; host/human gates unverified → fail-closed).
    arm_readiness = contract.evaluate_arm_readiness(
        authz, emitter_key_ready=None, follower_flat_confirmed=None,
        host_admission_off=None, emitter_task_disabled=None, require_flag_coherence=False,
        source_symbol=rc.source_symbol, follower_symbol=rc.follower_symbol,
        master_account_id=rc.master.id, follower_account_id=rc.follower.id)

    blockers: list[str] = []
    if kill:
        blockers.append("KILL_SWITCH_ENGAGED")
    if inflight != 0:
        blockers.append("FOLLOWER_JOB_IN_FLIGHT")
    if not clean:
        blockers.append("MASTER_NOT_CLEAN")
    if arm is not None:
        blockers.append(R_ARM_EXISTS)
    if window.get("open"):
        blockers.append("VERIFIER_WINDOW_OPEN")
    if authz is not None:
        blockers.append(R_AUTHZ_EXISTS)   # a demonstration is already prepared — confirm or stop, don't re-prepare

    return {
        "enabled": True, "stage": "readiness", "authorized": True,
        "ready_to_prepare": not blockers,
        "blockers": sorted(set(blockers)),
        "route": _route_restatement(rc),
        "dark": {"exec_arm_flags": exec_arm_flags, "kill_switch_engaged": kill,
                 "follower_jobs_in_flight": inflight, "master_clean": clean,
                 "master_watermark_seq": wm, "master_open_positions": open_pos,
                 "verifier_window_open": bool(window.get("open"))},
        "authorization": None if authz is None else {
            "id": authz.id, "status": authz.status,
            "orders_consumed": authz.orders_consumed, "max_orders": authz.max_orders,
            "expires_at": authz.expires_at.isoformat()},
        "arm_engaged": arm is not None,
        "concurrency": {"lease_blocked": lease_blocked, "lease_reason": lease_reason},
        "arm_readiness": {"arm_ready": arm_readiness["arm_ready"], "blockers": arm_readiness["blockers"]},
        "note": ("Owner-attested that the #46 master trade is closed is HISTORICAL — the arm re-proves #46 "
                 "clean, #48 flat and identity/route/symbol at the confirm instant (fail-closed)."),
    }


# ─────────────────────────────── stage 2: prepare ───────────────────────────────

def prepare(user, *, now=None, lease_path=None, correlation_id=None) -> dict:
    """Create the bounded owner-self-approved ``CopierLiveAuthorization`` and hold the acceptance lease. Does NOT
    arm, open a window, enable a flag, or contact the host. Returns a signed single-use confirm token. Fail-closed:
    refuses under a held lease (concurrency / duplicate), an existing active authorization or arm, or any
    readiness blocker. Idempotent under the lease — a duplicate click loses the race and gets LEASE_CONFLICT."""
    now = now or timezone.now()
    if not cf.copier_owner_demo_control_enabled():
        return {"enabled": False, "stage": "prepare", "prepared": False, "reason": R_FEATURE_DARK}
    rc = resolve_governed_demo_route(user, now=now)
    if not rc.ok:
        return {"enabled": True, "stage": "prepare", "prepared": False, "authorized": False, "reason": rc.reason}

    # Re-prove the DARK readiness before creating any authorization (never prepare onto a non-DARK backend).
    rd = readiness(user, now=now, lease_path=lease_path)
    if not rd.get("ready_to_prepare"):
        return {"enabled": True, "stage": "prepare", "prepared": False, "authorized": True,
                "reason": R_READINESS_NOT_READY, "blockers": rd.get("blockers", [])}

    from hosted_workspace import recovery_lease as RL
    from trade_copier.models import (CopierLiveAuthorization, CopierLiveAuthorizationStatus)
    from trading.account_policy import account_environment
    from django.db.models import Max

    corr = (correlation_id or f"odc-{uuid.uuid4().hex[:16]}").strip()
    ok_acq, lease_or = RL.acquire(RL.KIND_RECOVERY, "owner_demo_control", _owner_tag(user), corr,
                                  LEASE_TTL_S, now, lease_path)
    if not ok_acq:
        _audit("prepare_refused", user=user, rc=rc, correlation_id=corr, state="refused", reason=R_LEASE_CONFLICT)
        return {"enabled": True, "stage": "prepare", "prepared": False, "authorized": True,
                "reason": R_LEASE_CONFLICT, "detail": str(lease_or)}

    try:
        with transaction.atomic():
            # Fail-closed: a pre-existing active authorization / engaged arm means a demonstration is already set up.
            if _active_authorization(rc) is not None:
                raise _StageRefused(R_AUTHZ_EXISTS)
            if _engaged_arm(rc) is not None:
                raise _StageRefused(R_ARM_EXISTS)
            cm_events_seq = 0
            from trade_copier.models import CopierMaster
            cm = CopierMaster.objects.filter(trading_account_id=rc.master.id).first()
            if cm is not None:
                cm_events_seq = int(cm.events.aggregate(m=Max("sequence")).get("m") or 0)
            expires_at = now + timedelta(minutes=DEMO_VALID_MINUTES)
            authz = CopierLiveAuthorization.objects.create(
                user=user, master_account=rc.master, follower_account=rc.follower,
                master_login=str(rc.master.account_number or "").strip(),
                master_server=str(getattr(rc.master.broker_server, "server_name", "") or "").strip(),
                master_environment=account_environment(rc.master),
                follower_login=str(rc.follower.account_number or "").strip(),
                follower_server=str(getattr(rc.follower.broker_server, "server_name", "") or "").strip(),
                follower_environment=account_environment(rc.follower),
                source_symbol=rc.source_symbol, follower_symbol=rc.follower_symbol,
                max_volume=DEMO_MAX_VOLUME, max_concurrent_positions=DEMO_MAX_CONCURRENT,
                max_orders=DEMO_MAX_ORDERS, orders_consumed=0,
                valid_from=now, expires_at=expires_at,
                watermark_at=now, watermark_event_sequence=cm_events_seq,
                status=CopierLiveAuthorizationStatus.ACTIVE,
                approved_by=user,   # OWNER self-approval for this one governed route (ADR-0126); recent-auth at confirm
                reason="owner_demo_control prepare (GFX-170)",
                # The bounded lifecycle authority for a one-order OPEN→MODIFY→FULL_CLOSE demo; partial-close EXCLUDED.
                modify_authority=True, full_close_authority=True, partial_close_authority=False,
                close_only_after_expiry=False, correlation_id=corr)
        # Token signing + audit are INSIDE the try, so ANY post-acquire failure releases the lease — never strand
        # an ACTIVE authorization behind a held lease (single-effect token, bound to this one authorization).
        token = _SIGNER.sign(f"{authz.id}:{int(user.id)}:{corr}")
        _audit("prepared", user=user, rc=rc, correlation_id=corr, state="prepared", authorization_id=authz.id)
    except _StageRefused as e:
        RL.release(corr, now, lease_path)
        _audit("prepare_refused", user=user, rc=rc, correlation_id=corr, state="refused", reason=e.reason)
        return {"enabled": True, "stage": "prepare", "prepared": False, "authorized": True, "reason": e.reason}
    except Exception:
        RL.release(corr, now, lease_path)
        raise

    return {
        "enabled": True, "stage": "prepare", "prepared": True, "authorized": True,
        "authorization_id": authz.id, "confirm_token": token,
        "expires_at": authz.expires_at.isoformat(),
        "route": _route_restatement(rc, expires_at=authz.expires_at),
        "note": "Preparation does NOT authorize execution. Confirm requires fresh re-authentication; the arm "
                "re-proves every money-path invariant at the confirm instant and rolls back to DARK on any failure.",
    }


class _StageRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ─────────────────────────────── stage 3: confirm (deliberate, recent-auth) ───────────────────────────────

def _verify_confirm_token(token: str, user) -> "tuple[Optional[int], Optional[str], Optional[str]]":
    """Return (authorization_id, correlation_id, error_reason). Fail-closed: bad signature / expiry / wrong user."""
    if not token or not isinstance(token, str):
        return None, None, R_CONFIRM_TOKEN_INVALID
    try:
        raw = _SIGNER.unsign(token, max_age=CONFIRM_TOKEN_MAX_AGE_S)
    except signing.SignatureExpired:
        return None, None, R_CONFIRM_TOKEN_EXPIRED
    except signing.BadSignature:
        return None, None, R_CONFIRM_TOKEN_INVALID
    try:
        authz_id_s, uid_s, corr = raw.split(":", 2)
        authz_id, uid = int(authz_id_s), int(uid_s)
    except (ValueError, TypeError):
        return None, None, R_CONFIRM_TOKEN_INVALID
    if uid != int(getattr(user, "id", -1)):
        return None, None, R_CONFIRM_TOKEN_INVALID
    return authz_id, corr, None


def confirm(user, *, confirm_token: str, recent_auth_ok: bool, now=None,
            actuators: Optional[DemoActuators] = None, lease_path=None) -> dict:
    """Deliberately confirm (arm) the prepared demonstration. Requires a fresh re-authentication proof
    (``recent_auth_ok``, verified in the API layer) AND the single-EFFECT confirm token from prepare. Re-proves the
    full arm contract and invokes the EXISTING atomic arm with the INJECTED host actuators; in production those are
    UNPROVISIONED, so confirm fails closed (DARK) with ``EXECUTION_ACTUATORS_UNPROVISIONED`` — the authorization
    stays intact and the owner can stop. Idempotent: an already-engaged arm for the route is returned, not doubled."""
    now = now or timezone.now()
    if not cf.copier_owner_demo_control_enabled():
        return {"enabled": False, "stage": "confirm", "armed": False, "reason": R_FEATURE_DARK}
    rc = resolve_governed_demo_route(user, now=now)
    if not rc.ok:
        return {"enabled": True, "stage": "confirm", "armed": False, "authorized": False, "reason": rc.reason}
    if not recent_auth_ok:
        _audit("confirm_refused", user=user, rc=rc, correlation_id="", state="refused", reason=R_RECENT_AUTH_REQUIRED)
        return {"enabled": True, "stage": "confirm", "armed": False, "authorized": True,
                "reason": R_RECENT_AUTH_REQUIRED}
    authz_id, corr, err = _verify_confirm_token(confirm_token, user)
    if err is not None:
        _audit("confirm_refused", user=user, rc=rc, correlation_id="", state="refused", reason=err)
        return {"enabled": True, "stage": "confirm", "armed": False, "authorized": True, "reason": err}

    authz = _active_authorization(rc)
    if authz is None or authz.id != authz_id or authz.correlation_id != corr:
        return {"enabled": True, "stage": "confirm", "armed": False, "authorized": True, "reason": R_NO_PREPARED_DEMO}

    # Idempotency / duplicate-click / concurrent operator: if an arm already exists, return it (do not double-arm).
    existing_arm = _engaged_arm(rc)
    if existing_arm is not None:
        return {"enabled": True, "stage": "confirm", "armed": True, "authorized": True,
                "reason": "ALREADY_ARMED", "arm_id": existing_arm.id, "idempotent": True}

    acts = actuators or DemoActuators()
    # 1) Dry-run the full atomic contract (no mutation) so the owner sees a precise verdict.
    from trade_copier import lifecycle_arming as arming
    dry = arming.arm_live_lifecycle(
        master_account_id=rc.master.id, follower_account_id=rc.follower.id,
        source_symbol=rc.source_symbol, follower_symbol=rc.follower_symbol,
        authorization_id=authz.id, follower_flat_confirmed=True,
        emitter_key_probe=acts.emitter_key_probe, host_admission_probe=acts.host_admission_probe,
        emitter_task_probe=acts.emitter_task_probe, commit=False, by_email=getattr(user, "email", ""),
        reason="owner_demo_control confirm dry-run", now=now)
    if not dry.get("ok"):
        _audit("confirm_refused", user=user, rc=rc, correlation_id=corr, state="refused",
               reason=R_READINESS_NOT_READY)
        return {"enabled": True, "stage": "confirm", "armed": False, "authorized": True,
                "reason": R_READINESS_NOT_READY, "blockers": dry.get("blockers", [])}

    # 2) Execution requires the risk-INCREASING host actuators. Unprovisioned ⇒ fail closed (DARK), authz intact.
    if not acts.provisioned():
        _audit("confirm_dark", user=user, rc=rc, correlation_id=corr, state="confirmed_dark",
               reason=R_EXECUTION_ACTUATORS_UNPROVISIONED)
        return {"enabled": True, "stage": "confirm", "armed": False, "authorized": True,
                "reason": R_EXECUTION_ACTUATORS_UNPROVISIONED, "dry_run_ok": True,
                "note": "Contract valid. Real execution requires the separately-provisioned signed-host actuators "
                        "and a head/runtime-pinned Sponsor real-money GO; the backend stays DARK."}

    # 3) Commit the atomic arm (tests inject real fakes; production never reaches here without provisioning).
    res = arming.arm_live_lifecycle(
        master_account_id=rc.master.id, follower_account_id=rc.follower.id,
        source_symbol=rc.source_symbol, follower_symbol=rc.follower_symbol,
        authorization_id=authz.id, follower_flat_confirmed=True,
        emitter_key_probe=acts.emitter_key_probe, host_admission_probe=acts.host_admission_probe,
        emitter_task_probe=acts.emitter_task_probe, admission_provisioner=acts.admission_provisioner,
        emitter_enabler=acts.emitter_enabler, emitter_disabler=acts.emitter_disabler,
        emitter_key_remover=acts.emitter_key_remover, commit=True, by_email=getattr(user, "email", ""),
        reason="owner_demo_control confirm", now=now)
    armed = bool(res.get("armed"))
    _audit("confirm", user=user, rc=rc, correlation_id=corr, state="armed" if armed else "arm_failed",
           armed=armed, arm_id=int(res.get("arm_id") or 0))
    return {"enabled": True, "stage": "confirm", "armed": armed, "authorized": True,
            "reason": res.get("reason"), "arm_id": res.get("arm_id"),
            "rolled_back": res.get("rolled_back")}


# ─────────────────────────────── stage 4: stop / return DARK ───────────────────────────────

def stop(user, *, now=None, actuators: Optional[DemoActuators] = None, lease_path=None,
         reason: str = "owner_stop") -> dict:
    """Return the governed route to DARK: disarm, revoke the authorization, signed host OFF (via injected
    actuators), and release the acceptance lease. Idempotent and safe to repeat (already-DARK ⇒ success). Touches
    NO unrelated service or account beyond the governed route. Always attempts lease release for this owner."""
    now = now or timezone.now()
    if not cf.copier_owner_demo_control_enabled():
        # Even DARK, stop must be able to release a stale lease the owner holds (fail-safe), but create nothing.
        return {"enabled": False, "stage": "stop", "reason": R_FEATURE_DARK}
    rc = resolve_governed_demo_route(user, now=now)
    if not rc.ok:
        return {"enabled": True, "stage": "stop", "authorized": False, "reason": rc.reason}

    from hosted_workspace import recovery_lease as RL
    from trade_copier.lifecycle_teardown import teardown_live_dark

    authz = _active_authorization(rc)
    acts = actuators or DemoActuators()
    td = teardown_live_dark(
        master_account_id=rc.master.id, follower_account_id=rc.follower.id,
        authorization_id=(authz.id if authz else None),
        admission_restore=acts.admission_restore, emitter_disabler=acts.emitter_disabler,
        emitter_key_remover=acts.emitter_key_remover, revoke_authorization=True,
        revoked_by_email=getattr(user, "email", ""), reason=reason, now=now)

    # Release the acceptance lease IFF this owner holds it AND teardown PROVED the route DARK. If teardown
    # could not prove DARK (e.g. a still-unflat follower position ⇒ teardown_live_dark aborts without mutation,
    # cleanup_required=True), KEEP the lease so the deploy/recovery mutual-exclusion guard holds until a re-run
    # proves DARK — never drop the guard over a still-live route. Never release another holder's lease, and never
    # reclaim a MALFORMED lease (it is left fail-closed for operator review).
    proved_dark = bool(td.get("backend_dark")) and not td.get("cleanup_required")
    lease_released = None
    meta = RL.status_metadata(now, lease_path)
    owns = (meta.get("owner") == _owner_tag(user))
    if meta.get("status") == "ACTIVE" and owns:
        if proved_dark:
            ok_rel, _ = RL.release(meta.get("correlation_id", ""), now, lease_path)
            lease_released = bool(ok_rel)
        else:
            lease_released = False   # guard retained — stop again after the route is provably flat/DARK
    elif meta.get("status") == "EXPIRED" and owns:
        RL.reclaim(now, lease_path)
        lease_released = True

    _audit("stopped", user=user, rc=rc, correlation_id=(authz.correlation_id if authz else ""),
           state="dark", backend_dark=bool(td.get("backend_dark")), host_dark=bool(td.get("host_dark")))
    return {
        "enabled": True, "stage": "stop", "authorized": True,
        "backend_dark": bool(td.get("backend_dark")), "host_dark": bool(td.get("host_dark")),
        "lease_released": lease_released, "teardown": td,
        "note": "Backend returned to DARK. Host steps are UNPROVEN unless the signed host actuators ran "
                "(reported host_dark); re-run stop after provisioning if host_dark is false.",
    }


def status(user, *, now=None, lease_path=None) -> dict:
    """A compact, value-blind state read for the owner UI (no mutation): current stage facts + concurrency."""
    now = now or timezone.now()
    if not cf.copier_owner_demo_control_enabled():
        return {"enabled": False, "stage": "status", "reason": R_FEATURE_DARK}
    rc = resolve_governed_demo_route(user, now=now)
    if not rc.ok:
        return {"enabled": True, "stage": "status", "authorized": False, "reason": rc.reason}
    from hosted_workspace import recovery_lease as RL
    authz = _active_authorization(rc)
    arm = _engaged_arm(rc)
    meta = RL.status_metadata(now, lease_path)
    owns_lease = (meta.get("status") == "ACTIVE" and meta.get("owner") == _owner_tag(user))
    return {
        "enabled": True, "stage": "status", "authorized": True,
        "route": _route_restatement(rc),
        "authorization": None if authz is None else {
            "id": authz.id, "status": authz.status, "expires_at": authz.expires_at.isoformat(),
            "orders_consumed": authz.orders_consumed, "max_orders": authz.max_orders},
        "arm_engaged": arm is not None,
        "arm": None if arm is None else {"id": arm.id, "expires_at": arm.expires_at.isoformat()},
        "lease": {"status": meta.get("status"), "owned_by_caller": owns_lease,
                  "remaining_s": meta.get("remaining_s")},
    }
