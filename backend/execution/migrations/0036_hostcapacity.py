# ADR-0069 — physical-host-wide capacity guard: certified host ceiling + append-only audit.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('execution', '0035_alter_executionjob_job_type'),
    ]

    operations = [
        migrations.CreateModel(
            name='HostCapacity',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('physical_host_key', models.CharField(help_text="Physical host identity — equals the co-resident TerminalNodes' rdp_host (e.g. an IP).", max_length=255, unique=True)),
                ('max_total_occupants', models.PositiveIntegerField(help_text='CERTIFIED host-wide ceiling: max distinct occupant accounts summed across ALL logical nodes on this physical host. The allocator fail-closes a bind that would exceed it.')),
                ('certified_safe', models.PositiveIntegerField(blank=True, help_text='Informational recommended-safe occupancy (<= max_total_occupants): the operational-headroom target. Not itself enforced — the enforced ceiling is max_total_occupants.', null=True)),
                ('basis', models.TextField(blank=True, default='', help_text='Secret-free certification basis (hardware, methodology, date). No credentials / PII.')),
                ('certified_at', models.DateTimeField(blank=True, null=True)),
                ('certified_by', models.CharField(blank=True, default='', max_length=150)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Host Capacity',
                'verbose_name_plural': 'Host Capacities',
                'ordering': ['physical_host_key'],
            },
        ),
        migrations.CreateModel(
            name='HostCapacityAudit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('physical_host_key', models.CharField(db_index=True, max_length=255)),
                ('previous_max', models.IntegerField(blank=True, null=True)),
                ('new_max', models.IntegerField()),
                ('previous_safe', models.IntegerField(blank=True, null=True)),
                ('new_safe', models.IntegerField(blank=True, null=True)),
                ('actor', models.CharField(blank=True, default='', max_length=150)),
                ('reason', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'Host Capacity Audit',
                'verbose_name_plural': 'Host Capacity Audit',
                'ordering': ['-created_at'],
            },
        ),
    ]
