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


def broker_mailbox_connect_enabled() -> bool:
    """Gate for the member mailbox CONNECT/callback/revoke endpoints (the OAuth connection flow). DEFAULT OFF → the
    endpoints 404 until the Sponsor arms connecting + the Google OAuth client credentials are provisioned. Kept
    SEPARATE from the ingestion-worker gate so a mailbox can be connected + verified BEFORE live polling is armed.
    Read-only member action (connect/revoke a mailbox); NO execution/strategy/MT5 authority."""
    return _flag("BROKER_MAILBOX_CONNECT_ENABLED")


def broker_mailbox_status_enabled() -> bool:
    """Member-facing gate for the owner-scoped mailbox STATUS API (the customer Connected-Email-Accounts UI reads it).
    DEFAULT OFF → the endpoint 404s so the new customer onboarding UX stays DARK until its release gate, independently
    of the connect flow (which may already be armed) and of the identity-verification flag. Read-only member data
    only — never a token/credential/cursor/email body; NO execution/strategy/MT5 authority."""
    return _flag("BROKER_MAILBOX_STATUS_ENABLED")


def broker_withdrawal_ux_enabled() -> bool:
    """Member-facing gate for the Withdrawals UX + its metrics API. DEFAULT OFF. While OFF the metrics endpoint
    responds 404 (feature absent) so the half-built member feature never surfaces; the projection
    (``broker_intelligence.metrics``) is pure/testable regardless. Read-only member data only — NO execution/
    strategy/credential authority, never a financial action."""
    return _flag("BROKER_WITHDRAWAL_UX_ENABLED")


def broker_identity_verification_enabled() -> bool:
    """Gate for the member identity-verification/binding API (list identities + verify owner↔broker↔account). DEFAULT
    OFF → the endpoints 404 until the Sponsor arms the capability. Verification is an explicit, audited action that
    binds a BrokerEmailIdentity to its owner, trading-account and receiving mailbox ONLY on composed proof (connected
    mailbox control + GuvFX trading-account ownership + same-owner, fail-closed on ambiguity); it NEVER auto-verifies
    and grants NO execution/strategy/credential authority and never a financial action. The decision + write service
    (``broker_intelligence.identity_verification``) is pure/testable regardless of this flag; the flag only governs
    whether the HTTP surface is reachable."""
    return _flag("BROKER_IDENTITY_VERIFICATION_ENABLED")


def broker_withdrawal_correlation_enabled() -> bool:
    """Gate for the WP5 withdrawal CORRELATION run loop (the management command / scheduled pass that projects
    EXTERNAL_WITHDRAWAL events into Withdrawal records). DEFAULT OFF. The correlation engine
    (``broker_intelligence.correlation``) is pure/testable and operates only on already-ingested events; this flag
    only governs whether the LIVE pass executes, so the projection plane ships DARK until the Sponsor arms it. It
    writes ONLY broker_intelligence withdrawal projections — NO execution/strategy/credential authority, never a
    financial action."""
    return _flag("BROKER_WITHDRAWAL_CORRELATION_ENABLED")


def broker_cross_inbox_dedup_enabled() -> bool:
    """Gate for per-delivery de-duplication (MULTI_ACCOUNT_PUBLIC_METRICS §D, corrected per the PD review of #511).
    DEFAULT OFF. While OFF the ingestion pipeline NEVER touches the additive ``BrokerMessageObservation`` delivery
    table, so the feature is fully DARK and unmigrated-safe. While ON (and after the migration is applied), a genuine
    RE-FETCH of the SAME message from the SAME inbox — identified by the TRUSTED mailbox-native
    ``(connected_mailbox, provider_message_id)`` — reuses the existing event instead of creating a second, and each
    delivery records its RFC822 Message-ID as a NON-AUTHORITATIVE cross-inbox correlation hint (never a suppression
    key). The SAME logical email arriving through TWO different inboxes is TWO preserved owner-scoped events (never
    discarded); they collapse to ONE Withdrawal only at the correlation layer. (NB: the owner-scoped BrokerEvent
    idempotency itself is UNCONDITIONAL — not gated by this flag — once the migration is applied.) Read-only
    observation plane; NO execution/strategy/credential authority."""
    return _flag("BROKER_CROSS_INBOX_DEDUP_ENABLED")


def broker_multi_mailbox_enabled() -> bool:
    """Gate for connecting MULTIPLE independent Gmail inboxes per member (MULTI_ACCOUNT §3). DEFAULT OFF → the V1
    single-mailbox policy is byte-identical (the three view guards refuse a second live mailbox). While ON, an
    explicit ADD_NEW_MAILBOX while a live mailbox exists is permitted, bounded by the per-owner cap
    ``BROKER_MAX_MAILBOXES_PER_OWNER`` enforced atomically at the OAuth callback (under the per-owner advisory lock).
    The cross-user ``(provider, provider_mailbox_id)`` uniqueness (anti-hijack), signed OAuth state, gmail.readonly
    scope, encrypted credentials, per-mailbox cursors and independent health/revocation are all unchanged. Kept
    SEPARATE from ``broker_mailbox_connect_enabled`` so multi-connect is armed deliberately."""
    return _flag("BROKER_MULTI_MAILBOX_ENABLED")


def broker_account_contact_enabled() -> bool:
    """Gate for the ACCOUNT-LEVEL broker-email contact HTTP surface (list / create / verify a
    ``TradingAccountBrokerContact`` binding, scoped to one of the member's OWN trading accounts). DEFAULT OFF → the
    endpoints 404. The association model + its append-only audit + the DB-enforced fail-closed VERIFIED invariant and
    the ``contact_verification`` writer already exist (merged DARK); this flag only governs whether the member HTTP
    surface is reachable. One ``(email, broker)`` may bind to many of the member's accounts; binding is explicit and
    audited; NEVER auto-verifies; grants NO execution/strategy/credential authority and never a financial action."""
    return _flag("BROKER_ACCOUNT_CONTACT_ENABLED")


def broker_attribution_contact_scoped_enabled() -> bool:
    """Gate for CONTACT-SCOPED transaction-account attribution (MULTI_ACCOUNT §5). DEFAULT OFF → the resolver is
    byte-identical (broker-stated account numbers matched against ALL the owner's accounts). While ON, when an
    attributed VERIFIED identity is known, the broker-stated-number match is NARROWED to the account(s) that identity
    is VERIFIED-bound to — so a shared email across several of the owner's accounts resolves to the ONE bound account
    instead of going ambiguous. The stated-number match stays MANDATORY (the binding only narrows; inbox/identity
    ownership ALONE never assigns an account). No execution/financial authority."""
    return _flag("BROKER_ATTRIBUTION_CONTACT_SCOPED_ENABLED")


def broker_routing_evidence_required() -> bool:
    """Gate for requiring independently-supportable ROUTING evidence before a TradingAccountBrokerContact reaches
    VERIFIED (MULTI_ACCOUNT §2). DEFAULT OFF → contact_verification is byte-identical (owner + broker + connected
    mailbox proof only). While ON, a binding additionally needs routing proof: either the bound mailbox IS the
    broker-registered address (direct), or an AUTHENTICATED broker delivery was observed through it
    (BrokerMessageObservation → a REAL event) — so a mere same-owner mailbox no longer proves routing.

    ARMING / HARD DEPENDENCY: the 'observed_delivery' kind depends on BrokerMessageObservation rows, which are written
    ONLY under broker_cross_inbox_dedup_enabled(). The 'direct_address' kind needs no ingestion history, so the common
    (member connected the broker's own inbox) case still verifies; but a forwarding/alias case (connected mailbox !=
    registered address) can be proven only once the delivery plane is populated. Arm this flag only after that is in
    place (or rely on direct_address). No execution/financial authority."""
    return _flag("BROKER_ROUTING_EVIDENCE_REQUIRED")
