"""F1 — DB-enforced append-only for ``analytics_dailyfxobservation``.

A Django ``save()``/``delete()`` override is NOT an authoritative immutability control. These BEFORE-UPDATE and
BEFORE-DELETE row triggers reject every UPDATE and DELETE for ALL ordinary roles (including the table owner), so
``QuerySet.update()``, ``QuerySet.delete()``, ``save(force_update=True)`` and raw SQL all fail — not merely the
app layer. Corrections are INSERT-only (a new revision).

TRUNCATE is deliberately NOT trigger-blocked: Django's PostgreSQL test flush (``TransactionTestCase``) issues a
multi-table ``TRUNCATE``, and a ``BEFORE TRUNCATE`` trigger here would break every such suite. TRUNCATE is
instead rejected at the privilege boundary — ``TRUNCATE`` is owner-only in PostgreSQL, and the F-8 SELECT-only
runtime role (see ``ops/fx/grants_fx_observation.sql``) is a non-owner without the TRUNCATE privilege, so it
cannot TRUNCATE (nor UPDATE/DELETE/INSERT). A DELETE trigger does not fire on TRUNCATE, so blocking DELETE is
safe alongside the test flush, and the fx table has no inbound CASCADE.

IMPORTANT (F-8 dependency): full immutability against a SUPERUSER/OWNER additionally requires the runtime-role
cutover. While the application still connects as a superuser/owner identity, that identity can TRUNCATE, or
DISABLE a trigger, or ``SET session_replication_role='replica'`` to bypass these triggers — a deliberate,
auditable act, not a casual mutation. FX ENABLEMENT IS BLOCKED until the runtime identity is separated
(SELECT-only) or an equivalent authoritative boundary is proven (see ADR-0058).
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION guvfx_fx_obs_block_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'analytics_dailyfxobservation is append-only: % is not permitted (insert a new revision)', TG_OP
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS fx_obs_no_update ON analytics_dailyfxobservation;
CREATE TRIGGER fx_obs_no_update BEFORE UPDATE ON analytics_dailyfxobservation
    FOR EACH ROW EXECUTE FUNCTION guvfx_fx_obs_block_mutation();

DROP TRIGGER IF EXISTS fx_obs_no_delete ON analytics_dailyfxobservation;
CREATE TRIGGER fx_obs_no_delete BEFORE DELETE ON analytics_dailyfxobservation
    FOR EACH ROW EXECUTE FUNCTION guvfx_fx_obs_block_mutation();
"""

# Reverse drops ONLY the triggers + function — it never touches rows, so reversing this migration does not
# destroy collected audit history (F6).
REVERSE = r"""
DROP TRIGGER IF EXISTS fx_obs_no_update ON analytics_dailyfxobservation;
DROP TRIGGER IF EXISTS fx_obs_no_delete ON analytics_dailyfxobservation;
DROP FUNCTION IF EXISTS guvfx_fx_obs_block_mutation();
"""


class Migration(migrations.Migration):

    dependencies = [("analytics", "0002_dailyfxobservation")]

    operations = [migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE)]
