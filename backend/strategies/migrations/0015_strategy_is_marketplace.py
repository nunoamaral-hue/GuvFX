"""Add Strategy.is_marketplace — the marketplace PUBLICATION flag (availability, distinct from ownership).

Pure add-field-with-default: Django applies the default at the schema level, so NO per-row UPDATE runs and
every existing Strategy (including the per-user copies #8/#10/#15 that back the live #10/#16 assignments)
lands at ``is_marketplace=False`` (PRIVATE) — byte-for-byte behaviour-identical. Intentionally scoped to
ONLY this field: the unrelated pre-existing index-name drift on other models is left untouched.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("strategies", "0014_strategyassignment_magic_number"),
    ]

    operations = [
        migrations.AddField(
            model_name="strategy",
            name="is_marketplace",
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text="Published to the GuvFX marketplace — assignable by any eligible customer (not owned/editable by them).",
            ),
        ),
    ]
