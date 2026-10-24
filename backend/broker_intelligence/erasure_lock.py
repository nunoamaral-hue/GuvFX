"""Shared per-owner advisory-lock primitive serialising Withdrawal-Intelligence WRITES against governed erasure.

ADR-0054 P0 (post-erasure ingestion race). Every writer of owner-scoped WI data (ingestion, correlation, the
identity/contact binding paths, the mailbox connect path) acquires this lock BEFORE its owner-scoped write, inside
its own ``transaction.atomic()``. The governed erasure procedure takes the SAME lock
(``pg_advisory_xact_lock(26485, owner)``) before deriving or deleting any owner-scoped state and deletes the
mailbox inside that locked transaction. Therefore:
  - an ingestion txn that began before erasure holds the lock; erasure waits, then removes that just-written data;
  - an ingestion txn that reaches the lock after erasure re-reads the (now-deleted/disconnected/regenerated)
    mailbox and REFUSES its stale write — so erased personal data cannot be re-materialised.

The namespace (26485 == 0x6775) MUST match the governed procedure and ``erasure._ADVISORY_NS``. No-op on a
non-PostgreSQL backend (production is PostgreSQL), mirroring the other advisory-lock helpers in this app.
"""
from __future__ import annotations

from django.db import connection

OWNER_ERASURE_LOCK_NS = 26485  # 0x6775 — MUST match the governed erasure procedure + erasure._ADVISORY_NS


def acquire_owner_erasure_lock(owner_id) -> None:
    """Acquire the per-owner transaction advisory lock. MUST be called inside ``transaction.atomic()`` (the lock
    releases on commit/rollback). A None owner or a non-Postgres backend is a no-op."""
    if owner_id is None or connection.vendor != "postgresql":
        return
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", [OWNER_ERASURE_LOCK_NS, int(owner_id)])
