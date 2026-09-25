"""Phase C (Concurrent Broker Accounts) — per-user account entitlement enforcement.

Config-driven limits for "one GuvFX user -> N broker accounts". Two distinct caps:

  * OWNED limit  (``max_trading_accounts``)          — how many broker accounts a user may hold at all.
  * CONCURRENT-ACTIVE limit (``concurrent_broker_account_limit`` in CONCURRENT mode; 1 in STANDARD) —
    how many may be ACTIVE (tradeable) simultaneously.

Both derive from ``billing.entitlements.resolve_effective_entitlements`` (plan + operator overrides), so
raising 5 -> 10/20/50 is a data change, not code. Counting excludes tombstoned (disconnected) rows and,
for the active cap, counts only ``is_active=True``.

DARK: enforcement is gated by ``CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED`` (default OFF). While OFF, callers
keep their existing behaviour; these helpers change nothing in production until the flag is armed.
"""
from __future__ import annotations

import os

from billing.entitlements import AccountMode, resolve_effective_entitlements


def enforcement_enabled() -> bool:
    """DARK master gate for Phase-C entitlement enforcement (settings-or-env, default OFF, fail-closed)."""
    try:
        from django.conf import settings
        val = getattr(settings, "CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED", None)
    except Exception:  # noqa: BLE001
        val = None
    if val is None:
        val = os.environ.get("CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED", "")
    return str(val).strip().lower() in ("1", "true", "yes", "on")


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
