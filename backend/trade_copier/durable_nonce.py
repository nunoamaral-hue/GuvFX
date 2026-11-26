"""trade_copier.durable_nonce — a DURABLE single-use replay guard shared by the asymmetric Copy-Trading
verification contracts (GFX-COPIER-INGEST-EVENT-ED25519-173, ADR-0130).

WHY THIS EXISTS
---------------
ADR-0129 shipped the host-private/backend-public WINDOW-OPEN authorization with an IN-MEMORY nonce guard and
recorded the gap: a production opener needs a DURABLE single-use store, so the authorization is single-use not
only within one process but across concurrent workers AND process restarts. This module is that store, reusing
the EXISTING durable burn table ``CopierIngestNonce`` (ADR-0074) — no schema change: one row per (domain, scope,
nonce); the table's UNIQUE constraint makes a replay (including a concurrent one) fail closed, and the row's
durability makes single-use hold across a restart.

Two domains consume it, kept strictly disjoint by a per-domain token prefix so a nonce value can NEVER collide
across domains (nor with the legacy HMAC path, which stores raw nonces):

* ``durable_window_open_nonce_store()`` — a ``NonceStore``-shaped guard (the ADR-0129
  ``verify_window_open_authorization`` ``consume(scope, nonce, now_epoch)`` interface) for the window-open
  authorization; and
* ``durable_ingest_event_nonce_burn()`` — a ``(nonce, expiry_epoch) -> bool`` burn for the Ed25519 INGEST
  event verifier (ADR-0130).

FAIL-CLOSED. The stored token is ``sha256(domain, scope, nonce)`` (value-blind — the raw nonce is never stored
or logged), and ANY persistence error is treated as a replay (``False``), never as a fresh nonce: a transient DB
fault rejects an event rather than admitting a possible replay.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone as _tz
from typing import Callable

# Per-domain token prefixes (short, so ``prefix:<64 hex>`` stays well within CopierIngestNonce.nonce varchar(80)).
_DOMAIN_WINDOW_OPEN = "wo1"
_DOMAIN_INGEST_EVENT = "iev1"

# Retention for a window-open nonce: the ``consume`` interface carries no explicit expiry, so we keep the row at
# least as long as any window could plausibly authorize, plus the issue-age slack — comfortably beyond the
# verifier window's hard TTL (verifier_window.MAX_WINDOW_TTL_SECONDS = 900) so an in-window replay is always
# caught. Pruning (event_ingestion.prune_ingest_nonces) only removes rows whose window has fully elapsed.
DEFAULT_WINDOW_OPEN_TTL_S = 1200


def _token(domain: str, scope: str, nonce: str) -> str:
    """A value-blind, domain-separated unique token for the durable table. The raw nonce is hashed, so it is
    never stored; the domain prefix keeps the three namespaces (window-open / ingest-event / legacy-HMAC-raw)
    disjoint, and the 64-hex digest cannot overflow the varchar(80) column."""
    raw = f"{domain}\x1f{scope}\x1f{nonce}".encode("utf-8")
    return f"{domain}:{hashlib.sha256(raw).hexdigest()}"


def _burn_token(token: str, expires_at: datetime) -> bool:
    """Insert ``token`` exactly once. Returns True on first use, False on a duplicate (replay) OR on ANY
    persistence error (fail closed). Mirrors ``event_ingestion.make_nonce_burn``'s atomic-insert idiom so a
    rolled-back duplicate never poisons the surrounding transaction."""
    from django.db import IntegrityError, transaction
    from trade_copier.models import CopierIngestNonce
    try:
        with transaction.atomic():
            CopierIngestNonce.objects.create(nonce=token, expires_at=expires_at)
        return True
    except IntegrityError:
        return False
    except Exception:  # noqa: BLE001 — a persistence fault is treated as a replay (fail closed), never a fresh use
        return False


class DurableNonceStore:
    """Durable single-use guard exposing the ADR-0129 ``consume(*, scope, nonce, now_epoch) -> bool`` interface,
    so it is a drop-in DURABLE replacement for ``verifier_window_authorization.NonceStore``. ``consume`` returns
    True exactly once per (domain, scope, nonce) and False on every repeat — across concurrent requests (the
    UNIQUE constraint serialises the winner) and across restarts (the row is durable)."""

    def __init__(self, *, domain: str, ttl_seconds: int = DEFAULT_WINDOW_OPEN_TTL_S):
        self.domain = str(domain)
        self.ttl_seconds = int(ttl_seconds)

    def consume(self, *, scope: str, nonce: str, now_epoch: int) -> bool:
        token = _token(self.domain, str(scope), str(nonce))
        expires_at = datetime.fromtimestamp(int(now_epoch) + self.ttl_seconds, tz=_tz.utc)
        return _burn_token(token, expires_at)


def durable_window_open_nonce_store(*, ttl_seconds: int = DEFAULT_WINDOW_OPEN_TTL_S) -> DurableNonceStore:
    """The DURABLE nonce store for the ADR-0129 window-open authorization (closes its documented in-memory gap)."""
    return DurableNonceStore(domain=_DOMAIN_WINDOW_OPEN, ttl_seconds=ttl_seconds)


def durable_ingest_event_nonce_burn() -> Callable[[str, float], bool]:
    """The DURABLE ``(nonce, expiry_epoch) -> bool`` single-use burn for the Ed25519 INGEST event verifier
    (ADR-0130). The nonce row expires with the event, so replay protection holds for the whole event validity."""
    def _burn(nonce: str, expiry_epoch: float) -> bool:
        token = _token(_DOMAIN_INGEST_EVENT, "", str(nonce))
        expires_at = datetime.fromtimestamp(float(expiry_epoch), tz=_tz.utc)
        return _burn_token(token, expires_at)
    return _burn
