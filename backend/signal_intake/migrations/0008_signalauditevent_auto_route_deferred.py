from django.db import migrations, models


class Migration(migrations.Migration):
    """WS-C: add the AUTO_ROUTE_DEFERRED audit event (choices-only, behaviour-preserving) so the
    auto-router can persist the previously-discarded ``effective_mode`` reason."""

    dependencies = [
        ("signal_intake", "0007_parserprofile_certification_level"),
    ]

    operations = [
        migrations.AlterField(
            model_name="signalauditevent",
            name="event",
            field=models.CharField(
                max_length=32,
                choices=[
                    ("SIGNAL_RECEIVED", "Signal received (pending approval)"),
                    ("SIGNAL_QUARANTINED", "Signal quarantined"),
                    ("SIGNAL_APPROVED", "Signal approved (shadow — no order)"),
                    ("SIGNAL_REJECTED", "Signal rejected"),
                    ("APPROVAL_DENIED", "Approve/reject attempt denied (no reviewer permission)"),
                    ("AUTO_ROUTE_DEFERRED", "Auto-route deferred to manual (reason recorded)"),
                ],
            ),
        ),
    ]
