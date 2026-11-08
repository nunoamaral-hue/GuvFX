"""Activate the Pepperstone BrokerProfile for genuine withdrawal-email CAPTURE.

Packet: GFX-PEPPERSTONE-WITHDRAWAL-DEMO-003.

Stream B (migration 0026) seeded Pepperstone / Dominion Markets / Vantage as DARK ``is_active=False`` rows: each is
fully configured (Pepperstone: domain ``pepperstone.com``, auth policy ``DMARC_OR_ALIGNED_DKIM``, parser
``pepperstone`` v1) but inert, because ``is_active`` is the single switch that admits a profile's sender domain into
BOTH the acquisition filter and the retention gate (``broker_senders.sender_allowlist`` unions the approved sender
domains of ACTIVE profiles ONLY). While Pepperstone is inactive, an authenticated Pepperstone withdrawal email is
neither fetched nor stored, and the ingestion cursor would advance past it.

This migration flips ONLY Pepperstone to ``is_active=True``, recording ``ConfigProvenance.OPERATOR_VERIFIED`` and an
append-only ``BrokerProfileAudit`` row. This is exactly the operator-verification step migration 0026 documents: the
Pepperstone sender domain and its DMARC / From-aligned-DKIM auth posture were verified against a genuine,
authenticated (SPF, DKIM and DMARC PASS) Pepperstone withdrawal-request email received by the owner's connected
mailbox.

What this migration deliberately does NOT do:
  * It does NOT add Pepperstone to the external-withdrawal classifier's ``CERTIFIED_BROKERS``. That frozenset gates
    only the PUBLIC broker-metrics certification path; it is irrelevant to the owner-scoped member dashboard, which
    surfaces any REAL ``EXTERNAL_WITHDRAWAL`` event the deterministic parser produces. Captured Pepperstone mail is
    classified by the existing ``pepperstone`` adapter exactly as its unit + scenario tests prove, FAIL-CLOSED: an
    unknown template quarantines (no event), completion is never inferred, and a "confirm your withdrawal request"
    email opens at ``CONFIRMATION_REQUIRED`` (never ``COMPLETED``).
  * It does NOT weaken sender authentication, broaden recognition, or touch Dominion Markets / Vantage (both stay
    DARK). It stores NO secret, account number, reference, or email body.

Reverse RETIRES Pepperstone (``is_active=False``) with its own audit row — non-destructive; accrued append-only
history is preserved.
"""
from django.db import migrations

SLUG = "pepperstone"
_FWD = "migration 0027 (GFX-PEPPERSTONE-WITHDRAWAL-DEMO-003: operator-verified activation for genuine capture)"
_REV = "migration 0027 reverse (retire Pepperstone to is_active=False)"


def _activate(apps, schema_editor):
    BrokerProfile = apps.get_model("broker_intelligence", "BrokerProfile")
    BrokerProfileAudit = apps.get_model("broker_intelligence", "BrokerProfileAudit")
    bp = BrokerProfile.objects.filter(slug=SLUG).first()
    if bp is None:
        # Stream B seed absent (e.g. a partial/legacy state). Nothing to activate; do not fabricate a profile.
        return
    if bp.is_active and bp.provenance == "OPERATOR_VERIFIED":
        return  # idempotent — already activated by this migration
    bp.is_active = True
    bp.provenance = "OPERATOR_VERIFIED"
    bp.save(update_fields=["is_active", "provenance", "updated_at"])
    BrokerProfileAudit.objects.create(
        broker_profile=bp,
        action="UPDATE",
        snapshot={
            "is_active": True,
            "provenance": "OPERATOR_VERIFIED",
            "approved_sender_domains": bp.approved_sender_domains,
            "auth_policy": bp.auth_policy,
            "parser_name": bp.parser_name,
            "parser_version": bp.parser_version,
        },
        changed_by=_FWD,
    )


def _retire(apps, schema_editor):
    BrokerProfile = apps.get_model("broker_intelligence", "BrokerProfile")
    BrokerProfileAudit = apps.get_model("broker_intelligence", "BrokerProfileAudit")
    bp = BrokerProfile.objects.filter(slug=SLUG).first()
    if bp is None or not bp.is_active:
        return
    bp.is_active = False
    bp.save(update_fields=["is_active", "updated_at"])
    BrokerProfileAudit.objects.create(
        broker_profile=bp,
        action="UPDATE",
        snapshot={"is_active": False, "note": "retired by migration 0027 reverse"},
        changed_by=_REV,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("broker_intelligence", "0026_stream_b_broker_profiles"),
    ]

    operations = [
        migrations.RunPython(_activate, _retire),
    ]
