"""GFX-BETA-PHASE0 Increment 2 (review S1) — DB-level immutability for RuntimeEvent.

A BEFORE-UPDATE trigger rejects any UPDATE on the RuntimeEvent table, so the audit trail cannot be
rewritten via `QuerySet.update()`, `save(force_update=True)`, or raw SQL — not only the app-layer
`save()` override. DELETE is intentionally NOT blocked, so the `on_delete=CASCADE` lifecycle (removing
an account/runtime removes its events) still works; direct single-row `delete()` remains refused at
the app layer.
"""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION tp_runtimeevent_block_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'RuntimeEvent is append-only/immutable; UPDATE is not allowed';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tp_runtimeevent_no_update ON terminal_provisioning_runtimeevent;
CREATE TRIGGER tp_runtimeevent_no_update
    BEFORE UPDATE ON terminal_provisioning_runtimeevent
    FOR EACH ROW EXECUTE FUNCTION tp_runtimeevent_block_update();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS tp_runtimeevent_no_update ON terminal_provisioning_runtimeevent;
DROP FUNCTION IF EXISTS tp_runtimeevent_block_update();
"""


class Migration(migrations.Migration):

    dependencies = [
        ("terminal_provisioning", "0004_account_runtime"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE),
    ]
