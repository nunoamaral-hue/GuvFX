"""Stream B (ADR-0079) — seed Pepperstone, Dominion Markets and Vantage as DARK, operator-verify-before-activation
broker-config rows.

These three rows are seeded ``is_active=False`` DELIBERATELY. ``is_active`` is the switch that admits a profile's
approved sender domains into the live acquisition/retention allowlist (``broker_senders.sender_allowlist``), its
authserv-ids into the trust set (``gmail_source``), and the profile into catalogue reconciliation. The sender domains
below are the brokers' *public brand* domains and have NOT been verified against the brokers' actual notification
mail (verifying would require live broker/mailbox access, which this packet forbids). Under the no-assumption rule
they must therefore not widen any live allowlist. They stay DARK until an operator verifies the real notification
sender domain + auth posture against genuine broker email and flips ``is_active=True`` (recording provenance
OPERATOR_VERIFIED). The deterministic adapters in ``parsers_stream_b`` are likewise registered only by the armed
ingestion worker, so nothing here is active on import.

``supported_categories`` is the structural capability guard: these adapters CAN emit EXTERNAL_WITHDRAWAL (a
line-anchored withdrawal-method rail field), INTERNAL_TRANSFER, and UNKNOWN — never anything they cannot produce.

Additive. Reverse is NON-DESTRUCTIVE of accrued history: for each slug it removes ONLY the untouched seed (still
is_active=False, provenance=SEED, and carrying nothing but this migration's own CREATE audit row), deleting that
CREATE row then the profile (audit-first; the profile FK is PROTECT). If a profile has been activated, operator-
verified, or otherwise edited (i.e. the operator's BrokerProfile.save() has appended append-only UPDATE audit rows),
the reverse REFUSES with a clear error rather than silently destroying operator-verified config and its append-only
audit history — rolling back over such a profile must be an explicit operator decision (retire via is_active=False
or remove it deliberately first).
"""
from django.db import migrations

# provenance=SEED (the honest "initial seed" value; there is no SYNTHETIC choice — the provisional nature is recorded
# in account_id_rules and in the is_active=False gate). Domains are PROVISIONAL pending operator verification.
_PROFILES = [
    dict(name="Pepperstone", slug="pepperstone", aliases="Pepperstone,Pepperstone Group",
         official_domains="pepperstone.com", approved_sender_domains="pepperstone.com",
         trusted_authserv_ids="mx.google.com", auth_policy="DMARC_OR_ALIGNED_DKIM",
         parser_name="pepperstone", parser_version="v1", mt_environments="MT4,MT5",
         supported_categories="EXTERNAL_WITHDRAWAL,INTERNAL_TRANSFER,UNKNOWN",
         account_id_rules="PROVISIONAL (Stream B synthetic): sender domain unverified against real broker mail; "
                          "DARK (is_active=False) until operator-verified. Live/demo distinguished by server, not here.",
         provenance="SEED", is_active=False),
    dict(name="Dominion Markets", slug="dominion_markets", aliases="Dominion,Dominion Markets",
         official_domains="dominionmarkets.com", approved_sender_domains="dominionmarkets.com",
         trusted_authserv_ids="mx.google.com", auth_policy="DMARC_OR_ALIGNED_DKIM",
         parser_name="dominion_markets", parser_version="v1", mt_environments="MT4,MT5",
         supported_categories="EXTERNAL_WITHDRAWAL,INTERNAL_TRANSFER,UNKNOWN",
         account_id_rules="PROVISIONAL (Stream B synthetic): sender domain unverified against real broker mail; "
                          "DARK (is_active=False) until operator-verified. Live/demo distinguished by server, not here.",
         provenance="SEED", is_active=False),
    dict(name="Vantage", slug="vantage", aliases="Vantage,Vantage Markets,Vantage FX",
         official_domains="vantagemarkets.com", approved_sender_domains="vantagemarkets.com",
         trusted_authserv_ids="mx.google.com", auth_policy="DMARC_OR_ALIGNED_DKIM",
         parser_name="vantage", parser_version="v1", mt_environments="MT4,MT5",
         supported_categories="EXTERNAL_WITHDRAWAL,INTERNAL_TRANSFER,UNKNOWN",
         account_id_rules="PROVISIONAL (Stream B synthetic): sender domain unverified against real broker mail; "
                          "DARK (is_active=False) until operator-verified. Live/demo distinguished by server, not here.",
         provenance="SEED", is_active=False),
]
_SLUGS = [p["slug"] for p in _PROFILES]


def _seed(apps, schema_editor):
    BrokerProfile = apps.get_model("broker_intelligence", "BrokerProfile")
    BrokerProfileAudit = apps.get_model("broker_intelligence", "BrokerProfileAudit")
    for spec in _PROFILES:
        bp, created = BrokerProfile.objects.get_or_create(slug=spec["slug"], defaults=spec)
        if created:
            BrokerProfileAudit.objects.create(
                broker_profile=bp, action="CREATE",
                snapshot={k: spec.get(k, "") for k in spec}, changed_by="migration 0026 (Stream B, DARK)")


_CREATE_CHANGED_BY = "migration 0026 (Stream B, DARK)"


def _unseed(apps, schema_editor):
    BrokerProfile = apps.get_model("broker_intelligence", "BrokerProfile")
    BrokerProfileAudit = apps.get_model("broker_intelligence", "BrokerProfileAudit")
    for slug in _SLUGS:
        bp = BrokerProfile.objects.filter(slug=slug).first()
        if bp is None:
            continue
        audit = BrokerProfileAudit.objects.filter(broker_profile=bp)
        # Any audit row this migration did NOT write is accrued operator history (BrokerProfile.save() appends an
        # UPDATE row on any edit). Activation / verification / any such edit means the profile is no longer the
        # untouched seed, so reversing must NOT silently destroy it or its append-only history.
        accrued = audit.exclude(changed_by=_CREATE_CHANGED_BY).exists()
        if bp.is_active or bp.provenance != "SEED" or accrued:
            raise RuntimeError(
                "Refusing to reverse migration 0026: BrokerProfile '%s' has been activated, operator-verified, or "
                "edited (is_active=%s, provenance=%s, accrued_audit=%s). Retire it via is_active=False or remove it "
                "deliberately before rolling back Stream B, so its append-only audit history is not destroyed."
                % (slug, bp.is_active, bp.provenance, accrued))
        # Untouched seed: it carries only this migration's CREATE audit row(s). Remove them (audit-first; the profile
        # FK is PROTECT) then the profile. No accrued operator history exists to lose.
        audit.delete()
        bp.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('broker_intelligence', '0025_withdrawal_lifecycle_stream_b'),
    ]

    operations = [
        migrations.RunPython(_seed, _unseed),
    ]
