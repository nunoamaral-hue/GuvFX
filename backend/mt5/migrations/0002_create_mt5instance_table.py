from django.db import migrations

SQL = r"""
CREATE TABLE IF NOT EXISTS mt5_mt5instance (
    id BIGSERIAL PRIMARY KEY,
    hostname VARCHAR(128) NOT NULL UNIQUE,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    is_leased BOOLEAN NOT NULL DEFAULT FALSE,
    leased_to_id BIGINT NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    last_seen_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Optional index (helps lookups)
CREATE INDEX IF NOT EXISTS mt5_mt5instance_leased_idx ON mt5_mt5instance (is_admin, is_leased);
"""

REVERSE_SQL = r"""
DROP TABLE IF EXISTS mt5_mt5instance;
"""

class Migration(migrations.Migration):
    dependencies = [
        ("mt5", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
