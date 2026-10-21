"""trade_copier.services — audited follower configuration (PR B).

The single front door for creating/enabling/configuring a follower. Enforces the ownership
invariants fail-closed (packet §6): a follower cannot be the master itself, must belong to the SAME
GuvFX user as the master, and cannot activate itself — ``enabled`` flips only through here, and every
create / enable / disable / config change writes an append-only ``CopierFollowerAudit`` row.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from trade_copier.models import CopierFollower, CopierFollowerAudit


class FollowerConfigError(ValueError):
    """Raised fail-closed when a follower config operation violates an ownership/identity invariant."""


_CONFIG_FIELDS = (
    "sizing_mode", "fixed_lot", "multiplier", "risk_per_trade_pct",
    "max_volume", "max_aggregate_exposure", "max_concurrent_positions",
    "max_spread_points", "max_slippage_points",
    "sltp_translation", "disable_policy",
)


def _audit(follower, action, detail, actor):
    CopierFollowerAudit.objects.create(follower=follower, action=action, detail=detail or {}, actor=actor)


def _assert_ownership(master, follower_account):
    if follower_account.id == master.trading_account_id:
        raise FollowerConfigError("self_follow: a master cannot follow itself")
    if follower_account.user_id != master.trading_account.user_id:
        raise FollowerConfigError("cross_user: follower belongs to a different user than the master")


@transaction.atomic
def add_follower(master, follower_account, *, actor=None, **config) -> CopierFollower:
    """Create a follower (DISABLED). Rejects self-follow and cross-user fail-closed."""
    _assert_ownership(master, follower_account)
    clean = {k: v for k, v in config.items() if k in _CONFIG_FIELDS}
    follower = CopierFollower.objects.create(
        master=master, follower_account=follower_account, enabled=False, **clean)
    _audit(follower, "created", {"config": {k: str(v) for k, v in clean.items()}}, actor)
    return follower


@transaction.atomic
def set_follower_enabled(follower: CopierFollower, enabled: bool, *, actor=None) -> CopierFollower:
    """Enable/disable a follower (the ONLY path that flips ``enabled``). Re-asserts ownership."""
    _assert_ownership(follower.master, follower.follower_account)
    was = follower.enabled
    follower.enabled = bool(enabled)
    follower.save(update_fields=["enabled", "updated_at"])
    _audit(follower, "enabled" if enabled else "disabled",
           {"from": was, "to": follower.enabled}, actor)
    return follower


@transaction.atomic
def update_follower_config(follower: CopierFollower, *, actor=None, **changes) -> CopierFollower:
    """Update sizing/caps/policy fields with an audit of what changed. ``enabled`` is NOT settable here."""
    applied = {}
    for k, v in changes.items():
        if k not in _CONFIG_FIELDS:
            continue
        old = getattr(follower, k)
        if old != v:
            setattr(follower, k, v)
            applied[k] = {"from": str(old), "to": str(v)}
    if applied:
        follower.save(update_fields=list(applied.keys()) + ["updated_at"])
        _audit(follower, "config_changed", applied, actor)
    return follower


@transaction.atomic
def authorize_unprotected_close(follower: CopierFollower, *, actor=None) -> CopierFollower:
    """Record a versioned, revocable, account-specific authorization for protective auto-close of
    UNPROTECTED copied positions (packet §11). Config only — no close is actuated in PR B."""
    follower.unprotected_close_enabled = True
    follower.unprotected_close_authorization_version += 1
    follower.unprotected_close_authorized_at = timezone.now()
    follower.unprotected_close_authorized_by = actor
    follower.save(update_fields=[
        "unprotected_close_enabled", "unprotected_close_authorization_version",
        "unprotected_close_authorized_at", "unprotected_close_authorized_by", "updated_at"])
    _audit(follower, "unprotected_close_authorized",
           {"version": follower.unprotected_close_authorization_version}, actor)
    return follower


@transaction.atomic
def revoke_unprotected_close(follower: CopierFollower, *, actor=None) -> CopierFollower:
    """Revoke the protective auto-close authorization (packet §11: revocable)."""
    follower.unprotected_close_enabled = False
    follower.save(update_fields=["unprotected_close_enabled", "updated_at"])
    _audit(follower, "unprotected_close_revoked",
           {"version": follower.unprotected_close_authorization_version}, actor)
    return follower
