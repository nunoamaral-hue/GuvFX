#!/usr/bin/env bash
# GFX-PKT-TP-PROTECTION-LATENCY — verify the adaptive TP-protection watcher (read-only / dry-run).
#
# Proves, WITHOUT creating any order/trade/notification:
#   1. the watcher command runs a single tick in DRY-RUN (rolled back) and reports cadence + load;
#   2. a live watcher container is up and heart-beating (source=tp_protection_watcher);
#   3. the single-flight advisory lock is held (only one actor).
#
# Usage (on the VPS):  bash verify_watcher.sh
set -euo pipefail
BACKEND="${GUVFX_BACKEND_CONTAINER:-guvfx-backend}"
WATCHER="${GUVFX_WATCHER_CONTAINER:-guvfx-tp-protection-watcher}"

echo "== 1) dry-run tick (no writes) =="
docker exec "$BACKEND" python manage.py run_tp_protection_watcher --once --dry-run

echo "== 2) live watcher container =="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E "$WATCHER" || echo "WATCHER NOT RUNNING"

echo "== 3) heartbeat + single-flight =="
docker exec "$BACKEND" python manage.py shell -c "
from reliability.models import Heartbeat
from django.db import connection
from django.utils import timezone
hb = Heartbeat.objects.filter(source='tp_protection_watcher').first()
print('heartbeat:', None if not hb else {'age_s': round((timezone.now()-hb.last_beat_at).total_seconds()), 'state': (hb.detail or {}).get('state')})
with connection.cursor() as c:
    c.execute('SELECT pg_try_advisory_lock(778866553311)')
    got = c.fetchone()[0]
    if got:
        c.execute('SELECT pg_advisory_unlock(778866553311)')
    print('advisory_lock_free_from_backend:', got, '(False => a watcher holds it, i.e. single-flight active)')
"
echo "== done =="
