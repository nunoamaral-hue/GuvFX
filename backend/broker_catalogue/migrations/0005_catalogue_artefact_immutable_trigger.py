"""Stream A / ADR-0078 — DB-level immutability for CatalogueArtefact.

The model ``save()`` guard refuses to mutate an artefact once its version leaves DRAFT, but a raw
``QuerySet.update()`` / ``bulk_update`` bypasses the ORM. This migration adds a PostgreSQL BEFORE UPDATE
trigger that enforces the SAME rule at the database layer: an UPDATE of a CatalogueArtefact row whose version
is not DRAFT is refused. It mirrors the ORM guard exactly (DRAFT artefacts remain freely editable while being
assembled; a frozen artefact requires a NEW version). Vendor-guarded so non-PostgreSQL backends are a no-op;
fully reversible (drops the trigger + function).
"""
from __future__ import annotations

from django.db import migrations

_TRIGGER_FN = "broker_catalogue_artefact_immutable"
_TRIGGER = "broker_catalogue_artefact_immutable_trg"
_ARTEFACT_TABLE = "broker_catalogue_catalogueartefact"
_VERSION_TABLE = "broker_catalogue_catalogueversion"

_CREATE_SQL = f"""
CREATE OR REPLACE FUNCTION {_TRIGGER_FN}() RETURNS trigger AS $$
DECLARE
    v_status text;
BEGIN
    SELECT status INTO v_status FROM {_VERSION_TABLE} WHERE id = OLD.version_id;
    IF v_status IS NOT NULL AND v_status <> 'DRAFT' THEN
        RAISE EXCEPTION
            'CatalogueArtefact is immutable: version % is %, not DRAFT (db-trigger {_TRIGGER_FN})',
            OLD.version_id, v_status
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS {_TRIGGER} ON {_ARTEFACT_TABLE};
CREATE TRIGGER {_TRIGGER}
    BEFORE UPDATE ON {_ARTEFACT_TABLE}
    FOR EACH ROW EXECUTE FUNCTION {_TRIGGER_FN}();
"""

_DROP_SQL = f"""
DROP TRIGGER IF EXISTS {_TRIGGER} ON {_ARTEFACT_TABLE};
DROP FUNCTION IF EXISTS {_TRIGGER_FN}();
"""


def _apply(sql):
    def _run(apps, schema_editor):
        if schema_editor.connection.vendor != "postgresql":
            return  # no-op on non-PostgreSQL (the ORM save-guard still applies everywhere)
        with schema_editor.connection.cursor() as cur:
            cur.execute(sql)
    return _run


class Migration(migrations.Migration):

    dependencies = [
        ("broker_catalogue", "0004_catalogue_capture_candidate"),
    ]

    operations = [
        migrations.RunPython(_apply(_CREATE_SQL), _apply(_DROP_SQL)),
    ]
