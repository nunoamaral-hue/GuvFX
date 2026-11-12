"""ADR-0099 (Lane A) — deterministic SEED of the existing Stream-B brokers as data-driven certified fingerprints.

This MIGRATES the per-broker CODE CONSTANTS from ``parsers_stream_b`` (``requires_reference``,
``completion_processed_successfully``, ``account_from_body``, ``currency_from_field``, ``wants_html``) into
fingerprint DATA, each with SYNTHETIC fixtures and a deterministic ``source_hash`` (checksum/provenance).

IMPORTANT — this is a SEED MECHANISM, never a data migration. It does NOT run automatically in production: the
existing SPECIFIC parsers (TradersWay, IS6, Pepperstone, Dominion, Vantage) always win in ``select_parser`` BEFORE
the generic path runs, so a seeded fingerprint can never change an existing broker's behaviour. Brokers with a
bespoke parser (TradersWay internal-transfer grammar, IS6 EN/JP) KEEP their parser — only the Stream-B label/value
brokers whose behaviour the generic+fingerprint path can faithfully reproduce are seeded here.

All examples are SYNTHETIC (provenance SYNTHETIC) — clearly fabricated, never a real broker email.
"""
from __future__ import annotations

from typing import List, Optional

from .models import BrokerFingerprint, BrokerProfile, Provenance

# Each spec maps a broker PROFILE slug to the fingerprint config + SYNTHETIC fixtures. The positive fixtures must be
# recognised (certified match); the negative fixtures must NOT auto-accept (they certify-block otherwise).
FINGERPRINT_SEEDS: List[dict] = [
    {
        "name": "vantage", "slug": "vantage", "broker_display_name": "Vantage",
        "config": {
            "authenticated_sender_domains": ["vantagemarkets.com"],
            "subject_indicators": ["withdrawal"],
            "lifecycle_phrases": {
                "COMPLETED": ["processed successfully", "withdrawal processed"],
                "REQUESTED": ["withdrawal submitted", "withdrawal request received"],
                "PROCESSING": ["is being processed"],
            },
            "amount_hint": "Amount", "currency_hint": "Currency Type", "reference_hint": "Transaction ID",
            "account_suffix_patterns": ["account ending", "ending"],
            "semantic_overrides": {"requires_reference": True, "completion_processed_successfully": True,
                                   "account_from_body": True, "currency_from_field": True},
            "locale": "en",
        },
        # Reproduces the shape of genuine Vantage withdrawal #4 (SYNTHETIC values): COMPLETED, 30.00 GBP, account
        # ending 5017, external (bank), stable reference.
        "positive_examples": [{
            "subject": "Your withdrawal has been processed",
            "body": ("Your withdrawal has been processed successfully.\n"
                     "Amount: 30.00\nCurrency Type: GBP\n"
                     "The funds have been sent to your bank account ending 5017.\n"
                     "Transaction ID: WD-SYNTH-5017\n"),
            "from_domain": "vantagemarkets.com", "auth_verdict": "pass", "expect": "completed_external",
        }],
        "negative_examples": [
            {"subject": "Deposit received", "body": "Your deposit of GBP 50.00 has been received. Thank you.",
             "from_domain": "vantagemarkets.com", "auth_verdict": "pass", "expect": "not_withdrawal"},
            {"subject": "Withdrawal completed", "body": "Your withdrawal of GBP 30.00 was sent to your bank.",
             "from_domain": "vantage-support.evil.com", "auth_verdict": "pass", "expect": "spoofed_domain"},
        ],
    },
    {
        "name": "pepperstone", "slug": "pepperstone", "broker_display_name": "Pepperstone",
        "config": {
            "authenticated_sender_domains": ["pepperstone.com"],
            "subject_indicators": ["withdrawal"],
            "lifecycle_phrases": {
                "COMPLETED": ["withdrawal completed", "has been completed"],
                "REQUESTED": ["withdrawal request received", "we received your withdrawal"],
            },
            "account_suffix_patterns": ["account ending", "ending"],
            "semantic_overrides": {"wants_html": True},
            "locale": "en",
        },
        "positive_examples": [
            {"subject": "Withdrawal completed",
             "body": "Your withdrawal of GBP 100.00 has been completed and sent to your bank account ending 1234.",
             "from_domain": "pepperstone.com", "auth_verdict": "pass", "expect": "completed_external"},
            {"subject": "Withdrawal request received",
             "body": "We received your withdrawal request for GBP 100.00 to your bank account ending 1234.",
             "from_domain": "pepperstone.com", "auth_verdict": "pass", "expect": "requested_external"},
        ],
        "negative_examples": [
            {"subject": "Market update", "body": "Trade now with our special offer and bonus offer this week.",
             "from_domain": "pepperstone.com", "auth_verdict": "pass", "expect": "not_withdrawal"},
        ],
    },
    {
        "name": "dominion_markets", "slug": "dominion_markets", "broker_display_name": "Dominion Markets",
        "config": {
            "authenticated_sender_domains": ["dominionmarkets.com"],
            "subject_indicators": ["withdrawal"],
            "lifecycle_phrases": {"COMPLETED": ["withdrawal completed"], "PROCESSING": ["is being processed"]},
            "account_suffix_patterns": ["account ending", "ending"],
            "semantic_overrides": {},
            "locale": "en",
        },
        "positive_examples": [
            {"subject": "Withdrawal completed",
             "body": "Your withdrawal completed. GBP 75.00 has been sent to your card ending 4455.",
             "from_domain": "dominionmarkets.com", "auth_verdict": "pass", "expect": "completed_external"},
        ],
        "negative_examples": [
            {"subject": "Verify your email", "body": "Please verify your email to activate your account.",
             "from_domain": "dominionmarkets.com", "auth_verdict": "pass", "expect": "not_withdrawal"},
        ],
    },
]


def expected_source_hash(spec: dict) -> str:
    """The deterministic matching-snapshot checksum a seed would carry (without touching the DB)."""
    fp = BrokerFingerprint(name=spec["name"], version=1, broker_display_name=spec["broker_display_name"],
                           **spec["config"])
    return fp.compute_source_hash()


def seed_fingerprints(*, actor: str = "seed", activate: bool = True, only: Optional[List[str]] = None,
                      ticket: str = "ADR-0099-seed") -> List[dict]:
    """Idempotently propose → validate → certify → (optionally) activate the seed fingerprints. Requires the broker's
    ACTIVE ``BrokerProfile`` to exist (fail-closed/skip otherwise). Returns a per-broker result with the checksum.
    Never mutates an already-active+certified version carrying the same ``source_hash`` (idempotent)."""
    from . import fingerprint_onboarding as onb
    results: List[dict] = []
    for spec in FINGERPRINT_SEEDS:
        if only and spec["name"] not in only:
            continue
        want_hash = expected_source_hash(spec)
        profile = BrokerProfile.objects.filter(slug=spec["slug"], is_active=True).first()
        if profile is None:
            results.append({"name": spec["name"], "status": "SKIPPED_NO_ACTIVE_PROFILE", "source_hash": want_hash})
            continue
        existing = BrokerFingerprint.objects.filter(
            name=spec["name"], is_active=True, certified_capability=True, source_hash=want_hash).first()
        if existing is not None:
            results.append({"name": spec["name"], "status": "ALREADY_SEEDED", "version": existing.version,
                            "source_hash": want_hash})
            continue
        fp = onb.propose_fingerprint(
            name=spec["name"], broker_display_name=spec["broker_display_name"], actor=actor, broker_profile=profile,
            provenance=Provenance.SYNTHETIC, reason="ADR-0099 Stream-B seed", ticket=ticket,
            positive_examples=spec["positive_examples"], negative_examples=spec["negative_examples"], **spec["config"])
        onb.validate_fingerprint(fp, actor=actor)
        onb.certify_fingerprint(fp, actor=actor, reason="ADR-0099 Stream-B seed", ticket=ticket, broker_profile=profile)
        if activate:
            onb.activate_fingerprint(fp, actor=actor, reason="ADR-0099 Stream-B seed")
        fp.refresh_from_db()
        results.append({"name": spec["name"], "status": "SEEDED", "version": fp.version,
                        "source_hash": fp.source_hash, "active": fp.is_active,
                        "certified": fp.certified_capability, "checksum_match": fp.source_hash == want_hash})
    return results
