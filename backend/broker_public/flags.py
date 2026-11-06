"""broker_public DARK feature flags (settings-override-then-env; default OFF)."""
from __future__ import annotations

import os

from django.conf import settings

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str, default: str = "") -> bool:
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in _TRUTHY


def broker_public_directory_enabled() -> bool:
    """Master gate for the PUBLIC (unauthenticated) broker directory + metrics endpoints. DEFAULT OFF → every public
    endpoint 404s, so the whole public surface ships DARK and is armed deliberately (and never auto-enables general
    onboarding). Read-only aggregate data only; NO member data, NO execution/credential authority. Independent of
    whether aggregates have been computed — an armed-but-empty directory simply returns INSUFFICIENT_DATA."""
    return _flag("BROKER_PUBLIC_DIRECTORY_ENABLED")


def broker_public_minority_suppression_enabled() -> bool:
    """Gate for k-anonymity small-bucket suppression in the metric aggregation (collapse any lifecycle/currency
    bucket below the category's ``min_bucket_size`` into a 'suppressed' tally). DEFAULT OFF -> aggregation is
    byte-identical to today. Additive privacy hardening; no activation, no member data exposed either way."""
    return _flag("BROKER_PUBLIC_MINORITY_SUPPRESSION_ENABLED")


def broker_public_wtm_preview_enabled() -> bool:
    """Gate for the WAYOND Trader Match (WTM) broker-metrics PREVIEW endpoint (investor demo). DEFAULT OFF → 404.
    The preview returns a clearly-labelled SIMULATED broker scorecard illustrating future multi-sample analytics; it
    NEVER reads/writes Withdrawal/BrokerEvent/PublicBroker/member data, NEVER touches CERTIFIED_BROKERS, and NEVER
    publishes a real rating. Armed ONLY in an isolated demo environment, never prod."""
    return _flag("BROKER_PUBLIC_WTM_PREVIEW_ENABLED")


def broker_public_case_study_enabled() -> bool:
    """Gate for the genuine-evidence PRIVATE case-study endpoint (investor demo). DEFAULT OFF → 404, so the redacted
    real-withdrawal facts are NOT served (not even in the client bundle) until armed in an isolated demo env, never
    prod. Returns ONLY redacted, aggregate-style facts (amount/rail/timestamps/interval/attribution) — never a raw
    email, account number, confirmation link, wallet address or evidence pointer. Read-only; no member data."""
    return _flag("BROKER_PUBLIC_CASE_STUDY_ENABLED")


def broker_public_withdrawal_intelligence_enabled() -> bool:
    """Gate for the PUBLIC broker withdrawal-intelligence page contract (Stream C / ADR-0080). DEFAULT OFF -> 404, so
    the whole withdrawal-intelligence surface ships DARK and is armed deliberately. Returns ONLY privacy-safe,
    reason-coded, aggregate-only withdrawal-performance evidence (three separate lifecycle-stage durations, lifecycle
    outcome rates, sample sufficiency) for a CERTIFIED, published broker; NEVER a member identity, owner id, amount,
    exact timestamp, account number, reference or unsanitised free text. Independent of the directory flag; the broker
    must ALSO be ``is_published`` (editorial) and certified (empty CERTIFIED_BROKERS in prod -> UNAVAILABLE). Armed
    only in an isolated env, never prod."""
    return _flag("BROKER_PUBLIC_WITHDRAWAL_INTELLIGENCE_ENABLED")


def broker_public_consent_required() -> bool:
    """Gate for the participation/consent contract: when ON, only members with an ACTIVE
    ``MetricParticipationConsent`` for the broker scope contribute to a public aggregate (fail-closed -- no row ->
    excluded). DEFAULT OFF -> aggregation byte-identical to today (consent is a prerequisite the PM/Sponsor arms, and
    the public surface is 404 regardless until the directory flag is on). Distinct from Gmail inbox-read consent."""
    return _flag("BROKER_PUBLIC_CONSENT_REQUIRED")
