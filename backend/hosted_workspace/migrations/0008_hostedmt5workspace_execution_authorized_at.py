# ADR-0047: durable explicit customer authorization to execute (supersedes ADR-0044 Decision 2).
# Additive, null=True: every existing workspace becomes execution_authorized_at=NULL = NOT authorized, so
# no pre-existing row can auto-arm or be order-eligible until its owner explicitly authorizes. No data
# migration is needed — the arm precondition + the belt-and-braces order-gate term both fail closed on NULL.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hosted_workspace', '0007_provisioningstagetiming'),
    ]

    operations = [
        migrations.AddField(
            model_name='hostedmt5workspace',
            name='execution_authorized_at',
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
    ]
