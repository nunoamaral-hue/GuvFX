"""Activate the Vantage BrokerProfile for genuine withdrawal-email CAPTURE.

Packet: GFX-WITHDRAWAL-VANTAGE-CORRELATE-EXEC-019 (ADR-0090).

Stream B (migration 0026) seeded Pepperstone / Dominion Markets / Vantage as DARK ``is_active=False`` rows: each is
fully configured (Vantage: domain ``vantagemarkets.com``, auth policy ``DMARC_OR_ALIGNED_DKIM``, parser ``vantage``
v1) but inert, because ``is_active`` is the single switch that admits a profile's sender domain into BOTH the
acquisition filter and the retention gate (``broker_senders.sender_allowlist`` unions the approved sender domains of
ACTIVE profiles ONLY). While Vantage is inactive, an authenticated Vantage withdrawal email is neither fetched nor
stored, and the ingestion cursor would advance past it. The two genuine Vantage withdrawal emails (a submission-stage
"Withdrawal Submitted" and an acceptance-stage "Withdrawal Request Successful") were observed ONLY by the ADR-0089
unknown-sender discovery canary as REDACTED candidates — no raw body, name, account, reference or amount was stored.

This migration flips ONLY Vantage to ``is_active=True``, recording ``ConfigProvenance.OPERATOR_VERIFIED`` and an
append-only ``BrokerProfileAudit`` row — exactly the operator-verification step migration 0026 documents: the Vantage
sender domain ``vantagemarkets.com`` and its DMARC / From-aligned-DKIM posture were verified against the genuine,
authenticated (SPF, DKIM and DMARC PASS) Vantage withdrawal emails received by the owner's connected mailbox.

What this migration deliberately does NOT do:
  * It does NOT add Vantage to the external-withdrawal classifier's ``CERTIFIED_BROKERS``. That frozenset gates only
    the PUBLIC broker-metrics certification path; it is irrelevant to the owner-scoped member dashboard, which
    surfaces any REAL ``EXTERNAL_WITHDRAWAL`` event the deterministic parser produces. Captured Vantage mail is
    classified by the existing ``vantage`` adapter (ADR-0090) exactly as its unit + scenario tests prove, FAIL-CLOSED:
    an unknown template quarantines (no event), completion is never inferred from a submission / request-successful
    email (both open at ``WITHDRAWAL_REQUESTED``, never ``COMPLETED``), and an external-withdrawal assertion without a
    stable broker reference is downgraded to ``UNKNOWN`` so no ref-less withdrawal can be opened or double-counted.
  * It does NOT enable any public publication, weaken sender authentication, broaden recognition, or touch
    Pepperstone / Dominion Markets / TradersWay (Dominion stays DARK; the others are unchanged). It stores NO secret,
    account number, reference, or email body.

Reverse is the TRUE INVERSE of the activation: it restores the pristine seed state (``is_active=False``,
``provenance`` back to ``SEED``) and removes the audit row the forward wrote, so a subsequent reverse of migration
0026 sees an UNTOUCHED seed and its append-only-history guard does not (correctly) refuse. A migration reverse must
leave no trace of itself. (Operationally RETIRING a live Vantage profile — keeping its accrued audit history and
appending a retire row — is a separate FORWARD operation, e.g. the audited Django admin edit, not a migration
rollback.)

This migration is committed to the repository but is NOT applied to production in packet 019; applying it is a
separate Sponsor-gated deploy/reprocess packet.
"""
from django.db import migrations

SLUG = "vantage"
_FWD = "migration 0030 (GFX-WITHDRAWAL-VANTAGE-CORRELATE-EXEC-019: operator-verified activation for genuine capture)"


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
    # must leave no trace of itself.
    BrokerProfile = apps.get_model("broker_intelligence", "BrokerProfile")
    BrokerProfileAudit = apps.get_model("broker_intelligence", "BrokerProfileAudit")
    bp = BrokerProfile.objects.filter(slug=SLUG).first()
    if bp is None:
        return
    # Filter by the raw FK id, NOT the historical model instance. Under the parallel test runner, reversing this
    # migration renders `BrokerProfile` from the historical migration state, and `bp`'s rendered class can differ from
    # the class the `BrokerProfileAudit.broker_profile` FK expects -> Django raises "Cannot query ...: Must be a
    # BrokerProfile instance.". That exception's traceback cannot be pickled, so the parallel worker hangs the whole
    # backend CI job with no timeout. Filtering by the integer pk is identical in effect and is immune to the state
    # rendering mismatch. Forward production semantics are unchanged. (GFX-WITHDRAWAL-CI-HYGIENE-051)
    BrokerProfileAudit.objects.filter(broker_profile_id=bp.pk, changed_by=_FWD).delete()
    if bp.is_active or bp.provenance != "SEED":
        bp.is_active = False
        bp.provenance = "SEED"
        bp.save(update_fields=["is_active", "provenance", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("broker_intelligence", "0029_unknown_sender_discovery"),
    ]

    operations = [
        migrations.RunPython(_activate, _retire),
    ]
