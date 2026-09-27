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

from rest_framework.exceptions import PermissionDenied, ValidationError

from strategies.magic_allocation import allocate_magic
from strategies.models import seed_default_leg_sizing


def strategy_family(strategy):
    """The stable strategy-FAMILY identity — the marketplace ``template_slug`` — which is the SAME across a
    canonical marketplace strategy and its legacy per-user copies (e.g. every "Wayond WIM" copy shares
    ``wayond-wim``). Used to prevent a semantically-duplicate assignment of the same family on one account
    during the legacy transition. Returns None for a strategy with no family (a bespoke/private strategy),
    in which case only the exact (strategy, account) DB uniqueness applies. NEVER name-string based."""
    filters = getattr(strategy, "filters", None)
    if isinstance(filters, dict):
        slug = filters.get("template_slug")
        if slug:
            return str(slug)
    return None


def assert_no_duplicate_family(*, account, strategy) -> None:
    """During the legacy transition, ONE strategy family may be ACTIVELY assigned per account. Block
    assigning a DIFFERENT strategy row of the SAME family (e.g. the canonical marketplace Wayond WIM onto an
    account that already runs a legacy Wayond WIM copy) — which would double-run the same strategy. The exact
    (strategy, account) duplicate is already blocked by the DB unique constraint; this covers the
    same-family / different-row case. No family (no template_slug) ⇒ no extra restriction. Fail-closed 400."""
    from strategies.models import StrategyAssignment
    fam = strategy_family(strategy)
    if not fam:
        return
    clash = (StrategyAssignment.objects
             .filter(account=account, is_active=True, strategy__filters__template_slug=fam)
             .exclude(strategy_id=getattr(strategy, "id", None))
             .exists())
    if clash:
        raise ValidationError("This strategy is already assigned to this account.")


def assignment_has_history(assignment) -> bool:
    """True when the assignment has ANY execution/trading history that must be preserved on removal — i.e. its
    identity (immutable magic + attribution + append-only audit) is depended on. Used by the history-safe
    "Remove Strategy": a True result means DEACTIVATE (retain), False means a hard delete is safe.

    History = any Trade / ExecutionJob / SignalExecutionPlan referencing this assignment (reverse relations),
    OR a broker deal on the account already carrying this assignment's magic (covers a traded assignment whose
    ``Trade.strategy_assignment`` was never stamped, e.g. before dual-write/sweep)."""
    from trading.models import Trade
    for rel in ("trades", "execution_jobs", "signal_execution_plans"):
        mgr = getattr(assignment, rel, None)
        try:
            if mgr is not None and mgr.exists():
                return True
        except Exception:  # noqa: BLE001 — a missing reverse accessor must never crash the removal path
            pass
    magic = getattr(assignment, "magic_number", None)
    if magic and Trade.objects.filter(account_id=assignment.account_id, magic_number=magic).exists():
        return True
    return False


def assert_assignment_ownership(*, user, account, strategy) -> None:
    """Fail-closed authorization for creating/repointing/removing a StrategyAssignment.

    Rule (WAYOND marketplace packet): the user MUST own the target ``account``, AND the ``strategy`` must
    be ASSIGNABLE to them — i.e. they OWN it (a private strategy) OR it is PUBLISHED to the marketplace
    (``is_marketplace``). A published strategy is assignable without being owned; it never becomes
    customer-owned and (because ``StrategyViewSet`` stays owner-scoped for writes) can never be edited by
    the assignee. TradingAccount ownership is NEVER relaxed — a foreign account is always denied.

    Matrix: own account + own private → ALLOW; own account + published → ALLOW; own account + foreign
    private → DENY; foreign account + anything → DENY (account checked first). Bypass on ``is_superuser``
    (the same flag ``get_queryset`` uses to scope reads). Callers pass resolved instances, not ids.
    """
    if getattr(user, "is_superuser", False):
        return
    if getattr(account, "user_id", None) != user.id:
        raise PermissionDenied("You do not own this trading account.")
    owns_strategy = getattr(strategy, "owner_id", None) == user.id
    published = bool(getattr(strategy, "is_marketplace", False))
    if not (owns_strategy or published):
        raise PermissionDenied("This strategy isn't available to assign to your account.")


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
