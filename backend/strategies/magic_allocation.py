"""Phase A — per-StrategyAssignment MT5 magic-number allocation.

The magic is DB-controlled and DETERMINISTIC:

    magic_number = ASSIGNMENT_MAGIC_BASE + StrategyAssignment.id

Properties (packet §5):
  * globally unique BY CONSTRUCTION — ``id`` is the global PK, so two
    assignments can never collide; a partial ``UniqueConstraint(magic_number)``
    on the model is the registry backstop.
  * immutable after allocation (set once, ``magic_number_allocated_at`` stamped);
    never reused after deletion (Postgres never recycles a PK).
  * disjoint from every legacy magic: the observed legacy values are the
    ``99xxxx`` band (Strategy.magic_number, e.g. 990001/990010) and small
    strategy-id ints (e.g. 6); all are < ASSIGNMENT_MAGIC_BASE. A CheckConstraint
    ``magic_number >= ASSIGNMENT_MAGIC_BASE`` and a validator refusing an operator
    ``Strategy.magic_number >= ASSIGNMENT_MAGIC_BASE`` keep the bands permanently
    disjoint.
  * NO Python ``hash()`` (unstable); NO account-number dependence; NO
    customer-chosen value.
  * fits ``Trade.magic_number`` (int32, max 2,147,483,647): BASE=1e9 leaves
    headroom to id ≈ 1.147e9. If versioning beyond int32 is ever wanted, widen
    the two ``magic_number`` columns to ``BigIntegerField`` (additive).
"""
from __future__ import annotations

# v1 reserved band. Disjoint from legacy 99xxxx / small-int magics; int32-safe.
ASSIGNMENT_MAGIC_BASE = 1_000_000_000
# Postgres/Django IntegerField (int32) upper bound; Trade.magic_number is int32.
MAGIC_INT32_MAX = 2_147_483_647


class MagicAllocationError(Exception):
    """Raised when an assignment magic cannot be safely derived (fail-closed)."""


def magic_for(assignment_id: int) -> int:
    """Deterministic magic for an assignment id. Fail-closed on out-of-range."""
    if not isinstance(assignment_id, int) or assignment_id <= 0:
        raise MagicAllocationError(f"invalid assignment id {assignment_id!r}")
    value = ASSIGNMENT_MAGIC_BASE + assignment_id
    if value > MAGIC_INT32_MAX:
        raise MagicAllocationError(
            f"magic {value} exceeds int32 max for assignment {assignment_id} — "
            "widen magic_number to BigIntegerField before allocating this id")
    return value


def allocate_magic(assignment, *, save: bool = True):
    """Idempotently allocate the deterministic magic for ``assignment``.

    Returns the magic. If already allocated, returns the existing value unchanged
    (immutable). When ``save`` is True, persists ``magic_number`` +
    ``magic_number_allocated_at`` in a single update.
    """
    if assignment.magic_number is not None:
        return assignment.magic_number
    value = magic_for(assignment.id)
    assignment.magic_number = value
    if save:
        from django.utils import timezone
        assignment.magic_number_allocated_at = timezone.now()
        assignment.save(update_fields=["magic_number", "magic_number_allocated_at"])
    return value
