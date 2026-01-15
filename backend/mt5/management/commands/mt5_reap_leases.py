import json
from pathlib import Path
from django.core.management.base import BaseCommand
from django.utils import timezone
from mt5.models import Mt5Instance

POOL_ROOT = Path("/srv/guvfx/mt5_pool")  # host bind path is visible in backend? adjust if not mounted.

class Command(BaseCommand):
    help = "Reap expired MT5 instance leases and request reset-to-login."

    def handle(self, *args, **opts):
        now = timezone.now()
        expired = Mt5Instance.objects.filter(is_admin=False, is_leased=True, lease_expires_at__lt=now)

        count = 0
        for inst in expired:
            # write reset request flag for the instance container to pick up
            try:
                inst_dir = POOL_ROOT / inst.hostname
                inst_dir.mkdir(parents=True, exist_ok=True)
                (inst_dir / "reset_request.json").write_text(json.dumps({"ts": now.isoformat()}), encoding="utf-8")
            except Exception as e:
                self.stderr.write(f"reset flag write failed for {inst.hostname}: {e}")

            inst.is_leased = False
            inst.leased_to = None
            inst.lease_expires_at = None
            inst.save(update_fields=["is_leased","leased_to","lease_expires_at","updated_at"])
            count += 1

        self.stdout.write(f"✅ reaped={count}")
