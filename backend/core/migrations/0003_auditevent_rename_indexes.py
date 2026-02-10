# Migration to rename AuditEvent indexes
# Aligns auto-generated index names with model state

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_auditevent_path_method"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="auditevent",
            old_name="core_audite_event_t_a3e5c7_idx",
            new_name="core_audite_event_t_68fb24_idx",
        ),
        migrations.RenameIndex(
            model_name="auditevent",
            old_name="core_audite_user_id_c8b2f1_idx",
            new_name="core_audite_user_id_69cb1f_idx",
        ),
        migrations.RenameIndex(
            model_name="auditevent",
            old_name="core_audite_entity__d5f9e3_idx",
            new_name="core_audite_entity__e1955d_idx",
        ),
    ]
