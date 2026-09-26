"""Phase C (Concurrent Broker Accounts) — per-user account entitlement enforcement.

Config-driven limits for "one GuvFX user -> N broker accounts". Two distinct caps:

  * OWNED limit  (``max_trading_accounts``)          — how many broker accounts a user may hold at all.
  * CONCURRENT-ACTIVE limit (``concurrent_broker_account_limit`` in CONCURRENT mode; 1 in STANDARD) —
    how many may be ACTIVE (tradeable) simultaneously.

Both derive from ``billing.entitlements.resolve_effective_entitlements`` (plan + operator overrides), so
raising 5 -> 10/20/50 is a data change, not code. Counting excludes tombstoned (disconnected) rows and,
for the active cap, counts only ``is_active=True``.

DARK / per-user activation (Phase C — Wayond POC). Enforcement is now gated PER USER, so concurrency can be
activated for one customer (support@) without changing any other customer:

  * ``enforcement_master_enabled()`` — a global MASTER KILL switch (``CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED``).
    DEFAULT ON (the enforcement system is available); set the flag to a falsey value to KILL it for everyone.
    Its meaning changed: it no longer means "enable for everybody" — a user ALSO needs a per-user grant.
  * ``user_enforcement_enabled(user)`` — the AUTHORITATIVE per-user activation gate. A user is enabled only by
    an ACTIVE, non-expired ``EntitlementOverride`` with capability ``concurrent_accounts_enforcement`` and
    ``{"granted": true}``. The allowlist is EMPTY by default: no user gets new concurrent behaviour until
    explicitly granted. Enabling/disabling one customer is a DATA change (add / deactivate one row), never
    code or a redeploy — and it never affects any other customer.
  * ``enforcement_enabled(user=None)`` — the single predicate every call site uses. True iff the master is on
    AND a concrete user is supplied AND that user is per-user-enabled.

Resulting semantics (exactly as required): master OFF ⇒ nobody; master ON + user not granted ⇒ legacy; master
ON + user granted ⇒ that user consumes their entitlement (``account_mode`` / ``concurrent_broker_account_limit``
/ ``max_trading_accounts`` from ``resolve_effective_entitlements`` — raising 5→20 stays a data change); every
other user is unaffected. The limit is NEVER hard-coded.
"""
from __future__ import annotations

import os

from billing.entitlements import AccountMode, resolve_effective_entitlements

#: Per-user enforcement activation grant (an ``EntitlementOverride`` capability). Separate from the entitlement
#: VALUE caps (account_mode / limits) — this is the boolean "is this user in the concurrent-enforcement
#: programme" switch, read directly here (never via ``resolve_effective_entitlements``).
ENFORCEMENT_GRANT_CAPABILITY = "concurrent_accounts_enforcement"


def enforcement_master_enabled() -> bool:
    """Global MASTER KILL switch for per-user concurrent-account enforcement.

    DEFAULT ON (unset ⇒ the enforcement system is available; the per-user grant is still required). Setting
    ``CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED`` to an explicit falsey value (``0``/``false``/``no``/``off``)
    KILLS enforcement for EVERYONE instantly, regardless of per-user grants — an operator kill switch. Any
    other value (or unset) leaves the system available. Resolution errors fail SAFE to available (True), so a
    misconfigured setting can never silently disable the kill switch's *availability*; to actually kill, an
    operator sets an explicit off value."""
    try:
        from django.conf import settings
        val = getattr(settings, "CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED", None)
    except Exception:  # noqa: BLE001
        val = None
    if val is None:
        val = os.environ.get("CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED", None)
    if val is None or str(val).strip() == "":
        return True  # unset ⇒ master available (per-user grant still gates each user)
    return str(val).strip().lower() not in ("0", "false", "no", "off")


def user_enforcement_enabled(user) -> bool:
    """AUTHORITATIVE per-user activation gate. True iff ``user`` holds an ACTIVE, non-expired
    ``EntitlementOverride`` granting ``concurrent_accounts_enforcement`` (``{"granted": true}``). Empty
    allowlist by default. Data-driven and per-user — enabling/disabling one customer never touches another.
    Fails CLOSED (False) on a missing user or any lookup error."""
    if user is None or not getattr(user, "pk", None):
        return False
    try:
        from django.utils import timezone
        from admin_ops.models import EntitlementOverride
        rows = EntitlementOverride.objects.filter(
            user=user, capability=ENFORCEMENT_GRANT_CAPABILITY,
            is_active=True, expires_at__gt=timezone.now(),
        ).values_list("override_value", flat=True)
        return any(isinstance(v, dict) and bool(v.get("granted")) for v in rows)
    except Exception:  # noqa: BLE001 — a lookup error must never silently ARM enforcement
        return False


def enforcement_enabled(user=None) -> bool:
    """The single Phase-C enforcement predicate. True iff the global master is on AND a concrete ``user`` is
    supplied AND that user is per-user-enabled. A no-arg / ``None`` call returns False (fail-safe: there is no
    user to scope enforcement to — this preserves legacy behaviour for any un-scoped caller)."""
    if not enforcement_master_enabled():
        return False           # global kill → nobody, even a granted user
    if user is None:
        return False           # no user context → never enforce (legacy / fail-safe)
    return user_enforcement_enabled(user)


def grant_concurrent_enforcement(user, *, days=365, reason="", created_by=None):
    """Activate per-user concurrent-account enforcement for ONE user — THE single, reversible activation
    mutation. Creates (or refreshes) the user's ACTIVE ``EntitlementOverride`` grant so
    ``enforcement_enabled(user)`` returns True (subject to the master kill switch). Per-user — never affects
    another customer — and changes NO trading/runtime/strategy/sizing/magic state by itself; it only flips the
    enforcement gate for this user. IDEMPOTENT: a ``(user, capability)`` partial-unique constraint on active
    rows permits exactly one active grant, so a repeat call REFRESHES the existing grant's value/expiry rather
    than stacking a second row (which the constraint would reject) — never a 500, never an over-grant."""
    from datetime import timedelta
    from django.utils import timezone
    from admin_ops.models import EntitlementOverride
    obj, _created = EntitlementOverride.objects.update_or_create(
        user=user, capability=ENFORCEMENT_GRANT_CAPABILITY, is_active=True,
        defaults={"override_value": {"granted": True},
                  "expires_at": timezone.now() + timedelta(days=days),
                  "reason": reason or "Per-user concurrent-account enforcement activation (Phase C)",
                  "created_by": created_by})
    return obj


def revoke_concurrent_enforcement(user) -> int:
    """Reversible per-user disable: deactivate ALL active enforcement grants for ``user`` (rollback of
    ``grant_concurrent_enforcement``). Returns the number of grants deactivated. Never row-deletes (the grant
    history is retained)."""
    from admin_ops.models import EntitlementOverride
    return EntitlementOverride.objects.filter(
        user=user, capability=ENFORCEMENT_GRANT_CAPABILITY, is_active=True).update(is_active=False)


# ---- per-user Broker Accounts UX gate (Phase 9) ---------------------------------------------------
#: Per-user customer-facing UX activation (an ``EntitlementOverride`` capability). SEPARATE from the
#: enforcement grant and from entitlement VALUE caps: it controls only which /accounts experience the
#: customer SEES (new multi-account Broker Accounts UX vs the legacy page). Empty allowlist by default →
#: every user stays on the legacy experience until explicitly granted. Decoupled from enforcement so UX
#: can be shown/withheld independently of concurrent activation.
BROKER_UX_CAPABILITY = "broker_accounts_ux"


def user_broker_ux_enabled(user) -> bool:
    """True iff ``user`` holds an ACTIVE, non-expired ``EntitlementOverride`` granting
    ``broker_accounts_ux`` (``{"granted": true}``). Empty allowlist by default; fails CLOSED (False) on a
    missing user or any lookup error — a customer never sees an unfinished experience by accident."""
    if user is None or not getattr(user, "pk", None):
        return False
    try:
        from django.utils import timezone
        from admin_ops.models import EntitlementOverride
        rows = EntitlementOverride.objects.filter(
            user=user, capability=BROKER_UX_CAPABILITY,
            is_active=True, expires_at__gt=timezone.now(),
        ).values_list("override_value", flat=True)
        return any(isinstance(v, dict) and bool(v.get("granted")) for v in rows)
    except Exception:  # noqa: BLE001 — a lookup error must never expose unfinished UX
        return False


def grant_broker_ux(user, *, days=365, reason="", created_by=None):
    """Show the new Broker Accounts UX to ONE user (reversible, idempotent, per-user). Data-only; changes no
    trading/runtime/strategy state — it only changes which /accounts experience this customer renders."""
    from datetime import timedelta
    from django.utils import timezone
    from admin_ops.models import EntitlementOverride
    obj, _created = EntitlementOverride.objects.update_or_create(
        user=user, capability=BROKER_UX_CAPABILITY, is_active=True,
        defaults={"override_value": {"granted": True},
                  "expires_at": timezone.now() + timedelta(days=days),
                  "reason": reason or "Per-user Broker Accounts UX activation (Phase 9)",
                  "created_by": created_by})
    return obj


def revoke_broker_ux(user) -> int:
    """Reversible per-user disable of the Broker Accounts UX (rollback of ``grant_broker_ux``). Returns the
    number of grants deactivated; never row-deletes."""
    from admin_ops.models import EntitlementOverride
    return EntitlementOverride.objects.filter(
        user=user, capability=BROKER_UX_CAPABILITY, is_active=True).update(is_active=False)


# ---- counts (tombstone-aware) --------------------------------------------------------------------

def owned_account_count(user, *, exclude_account_id=None) -> int:
    """Broker accounts the user HOLDS (excludes disconnected/tombstoned rows, which are retained for
    history but do not consume the owned cap)."""
    from trading.models import TradingAccount
    qs = TradingAccount.objects.filter(user=user, disconnected_at__isnull=True)
    if exclude_account_id is not None:
        qs = qs.exclude(id=exclude_account_id)
    return qs.count()


def active_account_count(user, *, exclude_account_id=None) -> int:
    """Broker accounts currently ACTIVE (tradeable) for the user (excludes tombstoned)."""
    from trading.models import TradingAccount
    qs = TradingAccount.objects.filter(user=user, is_active=True, disconnected_at__isnull=True)
    if exclude_account_id is not None:
        qs = qs.exclude(id=exclude_account_id)
    return qs.count()


# ---- effective limits ----------------------------------------------------------------------------

def effective_owned_limit(ent) -> int:
    return int(getattr(ent, "max_trading_accounts", 0) or 0)


def effective_concurrent_limit(ent) -> int:
    """Max simultaneously-active accounts: the configured limit in CONCURRENT mode, else 1 (STANDARD)."""
    if str(getattr(ent, "account_mode", AccountMode.STANDARD)) == AccountMode.CONCURRENT:
        return int(getattr(ent, "concurrent_broker_account_limit", 1) or 0)
    return 1


# ---- enforcement predicates (raise on violation; callers gate on ``enforcement_enabled()``) -------

def check_can_add_account(user) -> None:
    """Raise ``ValidationError`` if adding another broker account would exceed the user's OWNED limit.
    Config-driven + override-aware + active/tombstone-correct (unlike the legacy ``min(10, ...)`` cap)."""
    from rest_framework.exceptions import ValidationError
    ent = resolve_effective_entitlements(user)
    limit = effective_owned_limit(ent)
    if owned_account_count(user) >= limit:
        raise ValidationError({"detail": f"Broker-account limit reached (maximum {limit})."})


def check_can_activate(user, *, exclude_account_id=None) -> None:
    """Raise ``ValidationError`` if activating another broker account would exceed the user's
    CONCURRENT-ACTIVE limit. ``exclude_account_id`` omits the account being (re)activated from the count.
    In STANDARD mode the limit is 1 (the caller is responsible for deactivating the current account
    first — see Phase C7 STANDARD switching)."""
    from rest_framework.exceptions import ValidationError
    ent = resolve_effective_entitlements(user)
    limit = effective_concurrent_limit(ent)
    if active_account_count(user, exclude_account_id=exclude_account_id) >= limit:
        raise ValidationError({
            "detail": f"Concurrent active-account limit reached (maximum {limit}). "
                      f"Deactivate another account before activating this one."})
