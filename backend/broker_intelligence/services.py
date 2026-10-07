"""Broker Intelligence — the single writer of ``BrokerEmailAlias`` (mint / bind / retire).

All alias lifecycle lives here (never inlined into trading/onboarding), so the never-reuse + retire invariants
sit in one auditable place. Every function is idempotent + best-effort-safe; nothing here places an order, arms
execution, touches a strategy or reads a broker credential.
"""
from __future__ import annotations

import logging
import secrets

from django.db import IntegrityError, transaction
from django.utils import timezone

from .flags import broker_email_identity_enabled
from .models import BrokerEmailAlias, alias_domain

logger = logging.getLogger("guvfx.broker_intelligence")

_MINT_RETRIES = 5


def _gen_local() -> str:
    """A CSPRNG, email-safe lowercase opaque local-part (~128 bits), embedding NO account id / user / broker /
    PII. Fixed non-identifying prefix 'ba' only (encodes nothing per-tenant)."""
    return "ba" + secrets.token_hex(16)   # 'ba' + 32 lowercase hex = 34 chars


def mint_alias_for_account(account, *, journey=BrokerEmailAlias.Journey.CONNECT_EXISTING):
    """Idempotently mint the ONE alias for a live account instance (ACTIVE, bound). Returns the alias. Opaque
    local-part with bounded collision-retry against the global-unique constraint; fail-closed if exhausted."""
    existing = BrokerEmailAlias.objects.filter(trading_account=account).first()
    if existing is not None:
        return existing
    last_exc = None
    for _ in range(_MINT_RETRIES):
        try:
            with transaction.atomic():
                return BrokerEmailAlias.objects.create(
                    alias_local=_gen_local(), domain_at_creation=alias_domain(),
                    trading_account=account, user=account.user,
                    origin_journey=journey, status=BrokerEmailAlias.Status.ACTIVE,
                    bound_at=timezone.now())
        except IntegrityError as exc:   # astronomically-rare local-part collision OR a race on the OneToOne
            last_exc = exc
            dup = BrokerEmailAlias.objects.filter(trading_account=account).first()
            if dup is not None:         # lost the race → the account already has its alias (idempotent)
                return dup
            continue                    # local-part collision → regenerate
    raise RuntimeError("broker_email_alias mint failed after retries") from last_exc


def retire_alias_for_account(account) -> int:
    """Idempotently RETIRE the account's alias on tombstone. Row + token are RETAINED forever (never deleted,
    never recycled). Returns rowcount (0 if none / already retired). Best-effort; never raises into a tombstone.

    SAVEPOINT-ISOLATED (required, not cosmetic): the UPDATE runs in its OWN ``transaction.atomic()`` because this
    runs INSIDE ``remove_account``'s tombstone transaction. A bare ``QuerySet.update()`` routes a DatabaseError
    through Django's ``mark_for_rollback_on_error``, which sets ``connection.needs_rollback=True`` on the ENCLOSING
    transaction; the ``except`` below swallows the Python exception but cannot clear that flag, so the whole
    tombstone (credential destruction, ``disconnected_at``, Stage-2 enqueue) would silently roll back while
    ``remove_account`` still reports success — leaving the account LIVE with intact credentials. The inner
    savepoint rolls the DB error back to the savepoint so the tombstone transaction stays clean. Realistic
    trigger: the master flag armed before migration 0001 lands (UPDATE hits a missing relation on every removal),
    or a transient lock/serialization failure. Mirrors the create path (``mint_alias_for_account``)."""
    try:
        with transaction.atomic():      # savepoint — a DB error here must NOT poison the tombstone transaction
            return (BrokerEmailAlias.objects
                    .filter(trading_account=account)
                    .exclude(status=BrokerEmailAlias.Status.RETIRED)
                    .update(status=BrokerEmailAlias.Status.RETIRED, retired_at=timezone.now()))
    except Exception:  # noqa: BLE001
        logger.warning("broker_email_alias retire failed account=%s", getattr(account, "id", None))
        return 0


# --- lifecycle hooks (flag-gated, best-effort, savepoint-isolated) ----------------------------------------
def ensure_broker_email_alias(account) -> None:
    """Create-path hook (Journey A). No-op unless the master flag is on. Wrapped in its OWN savepoint so an alias
    hiccup can never poison the account-create transaction, and swallows errors so it never fails a create."""
    if not broker_email_identity_enabled():
        return
    try:
        with transaction.atomic():      # savepoint — isolate from the caller's create transaction
            mint_alias_for_account(account)
    except Exception:  # noqa: BLE001
        logger.warning("ensure_broker_email_alias failed account=%s", getattr(account, "id", None))


def retire_broker_email_alias(account) -> None:
    """Tombstone-path hook. No-op unless the master flag is on. Best-effort (retire_alias_for_account already
    swallows); never blocks the authoritative tombstone."""
    if not broker_email_identity_enabled():
        return
    retire_alias_for_account(account)
