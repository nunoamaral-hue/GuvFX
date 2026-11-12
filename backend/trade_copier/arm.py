"""trade_copier.arm — ADR-0097 authorization-bound, auto-expiring backend ARM evaluation (fail-closed).

An ENGAGED, unexpired ``CopierLiveArm`` is the ADDITIONAL, route-exact AND-condition the LIVE admission requires
alongside the env/config gates and the human ``CopierLiveAuthorization``. This module is pure evaluation: it
reads the arm row and decides effectiveness; it creates nothing, contacts no host/broker, and holds no secret.

Fail-closed in every ambiguous case — no arm, an expired arm, a route/owner/symbol mismatch, more than one
engaged arm, or ANY database error all evaluate to "not armed" so the money path stays OFF. Auto-expiry is by
evaluation only: once ``expires_at`` passes the arm is immediately ineffective with no cleanup command needed.
"""
from __future__ import annotations

from django.utils import timezone


def engaged_arm_for_authorization(authz, *, now=None):
    """The single ENGAGED, unexpired arm bound to EXACTLY this authorization and its route, else ``None``.

    Fail-closed: zero arms (OFF), more than one engaged arm (ambiguous), an expired arm, or a route/owner/symbol
    mismatch all return ``None``; so does any DB error. The arm must agree with the authorization on owner, master,
    follower, source symbol and follower symbol — an arm that drifted from its authorization is never effective."""
    if authz is None or getattr(authz, "id", None) is None:
        return None
    now = now or timezone.now()
    try:
        from trade_copier.models import CopierLiveArm, CopierLiveArmState
        # Cap at 2 so a (constraint-violating) duplicate is detected and fails closed without an unbounded scan.
        rows = list(CopierLiveArm.objects.filter(
            authorization_id=authz.id, state=CopierLiveArmState.ENGAGED).order_by("id")[:2])
    except Exception:  # noqa: BLE001 — any DB error fails closed (not armed).
        return None
    if len(rows) != 1:
        return None  # 0 = OFF; >=2 = ambiguous duplicate → fail closed.
    arm = rows[0]
    # Route must match the authorization EXACTLY — never a rebound/drifted arm, never a sibling route.
    if (arm.master_account_id != authz.master_account_id or
            arm.follower_account_id != authz.follower_account_id or
            str(arm.source_symbol) != str(authz.source_symbol) or
            str(arm.follower_symbol) != str(authz.follower_symbol) or
            arm.owner_id != authz.user_id):
        return None
    # Auto-expiry: an expired (or null-expiry) arm is immediately ineffective — no cleanup command required.
    if arm.expires_at is None or now >= arm.expires_at:
        return None
    return arm


def require_engaged_arm(authz, *, now=None):
    """``(ok, reason)`` — ``ok`` is True ONLY when exactly one engaged, unexpired, route-exact arm is bound to
    this authorization. This is the additional AND the LIVE admission applies; it never widens any gate."""
    if authz is None:
        return False, "no_authorization"
    arm = engaged_arm_for_authorization(authz, now=now)
    if arm is None:
        return False, "no_live_arm"
    return True, "armed"


def arm_summary(arm) -> dict:
    """A redacted operational summary of an arm (no secret/credential/token — there is none to redact)."""
    if arm is None:
        return {"armed": False}
    return {
        "armed": arm.state == "ENGAGED",
        "arm_id": arm.id,
        "authorization_id": arm.authorization_id,
        "route": f"{arm.master_account_id}->{arm.follower_account_id}",
        "symbol": f"{arm.source_symbol}->{arm.follower_symbol}",
        "state": arm.state,
        "armed_at": arm.armed_at.isoformat() if arm.armed_at else None,
        "expires_at": arm.expires_at.isoformat() if arm.expires_at else None,
    }
