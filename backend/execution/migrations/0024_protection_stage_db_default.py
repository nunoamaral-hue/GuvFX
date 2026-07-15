from django.db import migrations


class Migration(migrations.Migration):
    """GFX-PKT-TI-SIGNALS-NON-EXECUTION-INCIDENT — schema-resilience fix.

    Root cause of the incident: ``0022`` added ``ProposedOrderLeg.protection_stage`` as NOT NULL,
    and Django (by design) DROPS the DB-level default after back-filling existing rows — the default
    then lives only in the Python model. So any process whose model predates ``0022`` (e.g. a
    listener container deployed before the migration ran) inserts a leg WITHOUT the column, hitting a
    NOT-NULL violation. That IntegrityError was mis-handled downstream as ``duplicate_plan`` and two
    valid TI signals were lost silently.

    Fix (defence-in-depth): restore a DB-LEVEL DEFAULT on the columns added in 0022/0023, so a legacy
    or parallel writer that omits the column inserts safely instead of raising. This makes the schema
    tolerant of a deploy where the migration lands before every consumer image is rebuilt. Purely
    additive; existing rows and the Python defaults are unchanged.
    """

    dependencies = [
        ("execution", "0023_signalsourceconfig_incremental_protection"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE execution_proposedorderleg ALTER COLUMN protection_stage SET DEFAULT 'INITIAL';",
            reverse_sql="ALTER TABLE execution_proposedorderleg ALTER COLUMN protection_stage DROP DEFAULT;",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE execution_signalsourceconfig ALTER COLUMN incremental_protection_enabled SET DEFAULT false;",
            reverse_sql="ALTER TABLE execution_signalsourceconfig ALTER COLUMN incremental_protection_enabled DROP DEFAULT;",
        ),
    ]
