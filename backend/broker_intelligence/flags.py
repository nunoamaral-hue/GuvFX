"""Broker Intelligence DARK feature flags (settings-override-then-env; default OFF).

Mirrors the ``hosted_workspace.flags`` idiom so the whole stream ships DARK and is armed deliberately. While OFF,
every lifecycle hook (alias mint/retire) is a dormant no-op and existing account create/remove behaviour is
byte-identical — no alias row is ever written.
"""
from __future__ import annotations

import os

from django.conf import settings

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str, default: str = "") -> bool:
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in _TRUTHY


def broker_email_identity_enabled() -> bool:
    """Master gate for the Broker Email Identity capability: whether a ``BrokerEmailAlias`` is minted per
    Model-A BrokerAccount lifecycle instance and retired on tombstone. DEFAULT OFF. While OFF the mint/retire
    hooks are no-ops (account create/remove byte-identical). Grants NO execution/strategy/credential authority."""
    return _flag("BROKER_EMAIL_IDENTITY_ENABLED")


def broker_email_identity_open_new_enabled() -> bool:
    """Independent sub-gate for Journey B (OPEN_NEW): minting an alias BEFORE a broker account exists (so the
    customer registers at the brokerage using it). DEFAULT OFF. Kept separate because Journey B hands a real
    brokerage a receivable address and must stay dark until the mailbox/MX infra exists. Journey A
    (CONNECT_EXISTING) needs no inbound mail and is gated only by the master flag above."""
    return _flag("BROKER_EMAIL_IDENTITY_OPEN_NEW_ENABLED")


def broker_intelligence_ingest_enabled() -> bool:
    """Gate for the WP3b standalone ingestion WORKER (whether it polls the mailbox and ingests). DEFAULT OFF. The
    ingestion pipeline function itself is pure/testable; this flag only governs the live worker loop + real MailSource
    so the whole ingestion plane ships DARK until the pilot mailbox/credential exist and the Sponsor arms it. Grants
    NO execution/strategy/credential authority."""
    return _flag("BROKER_INTELLIGENCE_INGEST_ENABLED")


def broker_withdrawal_correlation_enabled() -> bool:
    """Gate for the WP5 withdrawal CORRELATION run loop (the management command / scheduled pass that projects
    EXTERNAL_WITHDRAWAL events into Withdrawal records). DEFAULT OFF. The correlation engine
    (``broker_intelligence.correlation``) is pure/testable and operates only on already-ingested events; this flag
    only governs whether the LIVE pass executes, so the projection plane ships DARK until the Sponsor arms it. It
    writes ONLY broker_intelligence withdrawal projections — NO execution/strategy/credential authority, never a
    financial action."""
    return _flag("BROKER_WITHDRAWAL_CORRELATION_ENABLED")
