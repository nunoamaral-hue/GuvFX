from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import Mt5Instance

LEASE_MINUTES = 60

POOL_HOSTS = ["mt5free-1", "mt5free-2", "mt5free-3", "mt5free-4"]
ADMIN_HOST = "mt5free-admin"

def seed_pool_if_missing():
    # Create admin + pool rows if not present
    Mt5Instance.objects.get_or_create(hostname=ADMIN_HOST, defaults={"is_admin": True})
    for h in POOL_HOSTS:
        Mt5Instance.objects.get_or_create(hostname=h, defaults={"is_admin": False})

@transaction.atomic
def lease_instance_for_user(user):
    """
    If user already has a valid lease, extend last_seen + expiry and return it.
    Else pick a free instance (oldest last_seen first).
    """
    now = timezone.now()
    seed_pool_if_missing()

    # Reuse existing lease for this user
    inst = (
        Mt5Instance.objects
        .select_for_update()
        .filter(is_admin=False, leased_to=user, is_leased=True)
        .first()
    )
    if inst:
        inst.last_seen_at = now
        inst.lease_expires_at = now + timedelta(minutes=LEASE_MINUTES)
        inst.save(update_fields=["last_seen_at", "lease_expires_at", "updated_at"])
        return inst

    # Find a free instance
    inst = (
        Mt5Instance.objects
        .select_for_update()
        .filter(is_admin=False, is_leased=False)
        .order_by("last_seen_at", "id")
        .first()
    )
    if not inst:
        return None

    inst.is_leased = True
    inst.leased_to = user
    inst.last_seen_at = now
    inst.lease_expires_at = now + timedelta(minutes=LEASE_MINUTES)
    inst.save(update_fields=["is_leased","leased_to","last_seen_at","lease_expires_at","updated_at"])
    return inst
