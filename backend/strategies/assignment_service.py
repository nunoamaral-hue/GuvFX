"""Canonical, account-scoped StrategyAssignment ownership + initialization.

ONE authoritative place for two concerns the member-launch UX depends on:

1. **Both-axis ownership** (Sponsor decision, WAYOND account-scoped strategy packet): a non-staff
   user may only create/repoint/remove an assignment when they own BOTH the ``TradingAccount`` AND
   the ``Strategy``. Historically the REST create guarded only the account axis, and update/delete
   guarded only the strategy axis (via ``get_queryset``) — asymmetric, so a foreign strategy could be
   attached to an owned account, and a PATCH could repoint the ``account`` FK onto a foreign account.
   ``assert_assignment_ownership`` closes both, fail-closed, with the existing staff bypass preserved.

2. **Complete initialization** so an assignment created from the account page is IDENTICAL in
   completeness to one created via the marketplace — no "marketplace = complete, account-page =
   incomplete" divergence. ``initialize_new_assignment`` seeds the conservative per-leg sizing default
   (0.01) and allocates the deterministic magic (``1e9 + id``) via the certified allocator. Both are
   idempotent: sizing is a OneToOne get_or_create; ``allocate_magic`` is immutable (only allocates when
   ``magic_number`` is NULL, never changes an existing value). It never bulk-allocates and never alters
   an existing magic — #10 (1000000010) and #16 (1000000016) are untouched because they are not
   re-created. Magic is the durable WHO identity and is inert until an assignment is armed for auto
   execution (a separate, gated step) and STRATEGY_MAGIC_SEND is on.
"""
from __future__ import annotations

from rest_framework.exceptions import PermissionDenied

from strategies.magic_allocation import allocate_magic
from strategies.models import seed_default_leg_sizing


def assert_assignment_ownership(*, user, account, strategy) -> None:
    """Fail-closed: require ``user`` to own BOTH ``account`` and ``strategy``.

    The bypass is ``is_superuser`` — deliberately the SAME flag the viewset's ``get_queryset`` uses to
    scope reads (``if not user.is_superuser``), so write authority never exceeds read authority. (A
    staff-but-not-superuser account is scoped like any member on reads, so it must also own both axes on
    writes — otherwise it could repoint an assignment it can see onto an account it does not own.)
    Callers pass the resolved model instances (not ids), so this never widens a queryset.
    """
    if getattr(user, "is_superuser", False):
        return
    if getattr(account, "user_id", None) != user.id:
        raise PermissionDenied("You do not own this trading account.")
    if getattr(strategy, "owner_id", None) != user.id:
        raise PermissionDenied("You do not own this strategy.")


def initialize_new_assignment(assignment):
    """Complete a freshly-created assignment: conservative per-leg sizing + deterministic magic.

    Idempotent and non-destructive:
      * ``seed_default_leg_sizing`` — 0.01/leg via OneToOne get_or_create (existing row untouched);
      * ``allocate_magic`` — deterministic ``1e9 + assignment.id``, allocated ONLY when NULL and never
        mutated afterwards.

    Safe to call on the create path of every assignment surface (account page, marketplace) so they
    produce equally-complete rows. Magic remains inert until the assignment is armed for auto execution.
    """
    seed_default_leg_sizing(assignment)
    allocate_magic(assignment)  # idempotent + immutable; no-op when magic_number is already set
    return assignment
