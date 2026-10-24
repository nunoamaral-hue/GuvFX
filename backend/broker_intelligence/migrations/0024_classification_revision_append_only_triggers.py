"""ADR-0059 — DB-level append-only enforcement for BrokerEventClassificationRevision.

Mirrors the EvidenceBlob / BrokerEvent triggers (migration 0002): the app-layer ``save()``/``delete()`` overrides do
NOT cover ``QuerySet.update()`` / ``QuerySet.delete()`` / raw SQL. Because the latest revision's ``derived_category``
IS the effective category that governs whether an event surfaces as a withdrawal, a bulk write here could silently
flip an internal transfer to EXTERNAL_WITHDRAWAL (over-claim) or erase a correction. These BEFORE-UPDATE / BEFORE-DELETE
triggers block both at the database layer, giving the revision the SAME write-once/never-deleted guarantee as the
evidence it corrects. Additive (triggers only; no schema/data change).
"""
from django.db import migrations

_TABLE = "broker_intelligence_brokereventclassificationrevision"

FORWARD = r"""
CREATE OR REPLACE FUNCTION bi_classrev_block_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'BrokerEventClassificationRevision is write-once; a correction is a new row (UPDATE not allowed)';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS bi_classrev_no_update ON broker_intelligence_brokereventclassificationrevision;
CREATE TRIGGER bi_classrev_no_update
    BEFORE UPDATE ON broker_intelligence_brokereventclassificationrevision
    FOR EACH ROW EXECUTE FUNCTION bi_classrev_block_update();

CREATE OR REPLACE FUNCTION bi_classrev_block_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'BrokerEventClassificationRevision is append-only; DELETE is not allowed';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS bi_classrev_no_delete ON broker_intelligence_brokereventclassificationrevision;
CREATE TRIGGER bi_classrev_no_delete
    BEFORE DELETE ON broker_intelligence_brokereventclassificationrevision
    FOR EACH ROW EXECUTE FUNCTION bi_classrev_block_delete();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS bi_classrev_no_delete ON broker_intelligence_brokereventclassificationrevision;
DROP FUNCTION IF EXISTS bi_classrev_block_delete();
DROP TRIGGER IF EXISTS bi_classrev_no_update ON broker_intelligence_brokereventclassificationrevision;
DROP FUNCTION IF EXISTS bi_classrev_block_update();
"""


class Migration(migrations.Migration):

    dependencies = [
        ("broker_intelligence", "0023_broker_event_classification_revision"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE),
    ]
