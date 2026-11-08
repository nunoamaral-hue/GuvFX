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

Reverse is the TRUE INVERSE of the activation: it restores the pristine seed state (``is_active=False``,
``provenance`` back to ``SEED``) and removes the audit row the forward wrote, so a subsequent reverse of migration
0026 sees an UNTOUCHED seed and its append-only-history guard does not (correctly) refuse. A migration reverse must
leave no trace of itself. (Operationally RETIRING a live Pepperstone profile — keeping its accrued audit history and
appending a retire row — is a separate FORWARD operation, e.g. the audited Django admin edit, not a migration
rollback.)
"""
from django.db import migrations

SLUG = "pepperstone"
_FWD = "migration 0027 (GFX-PEPPERSTONE-WITHDRAWAL-DEMO-003: operator-verified activation for genuine capture)"


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
    # TRUE INVERSE of _activate: restore the pristine seed (is_active=False, provenance=SEED) and delete the audit
    # row the forward wrote. This leaves the profile exactly as migration 0026 left it, so a subsequent reverse of
    # 0026 sees an untouched seed and its append-only-history guard passes. No new audit row is written — a reverse
    # must leave no trace of itself. (This is why rewinding the migration graph, e.g. in execution.tests_tb1_migration,
    # does not trip the 0026 _unseed guard.)
    BrokerProfile = apps.get_model("broker_intelligence", "BrokerProfile")
    BrokerProfileAudit = apps.get_model("broker_intelligence", "BrokerProfileAudit")
    bp = BrokerProfile.objects.filter(slug=SLUG).first()
    if bp is None:
        return
    BrokerProfileAudit.objects.filter(broker_profile=bp, changed_by=_FWD).delete()
    if bp.is_active or bp.provenance != "SEED":
        bp.is_active = False
        bp.provenance = "SEED"
        bp.save(update_fields=["is_active", "provenance", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("broker_intelligence", "0026_stream_b_broker_profiles"),
    ]

    operations = [
        migrations.RunPython(_activate, _retire),
    ]
