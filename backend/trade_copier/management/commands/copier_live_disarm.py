"""copier_live_disarm — the one-action emergency disarm for ALL LIVE copier dispatch (ADR-0057 §12).

Engages a durable ``CopierLiveKillSwitch`` so the LIVE admission denies every pair immediately, independent
of flags, surviving a flag flip and a process restart. Audit evidence (who/when/why and every existing
authorization) is preserved — the switch blocks dispatch, it does not delete authorizations. Re-enabling is a
deliberate separate operator action (clearing the switch), never part of disarm.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = "Engage the durable LIVE-copier kill switch — blocks ALL new LIVE copier dispatch (ADR-0057)."

    def add_arguments(self, parser):
        parser.add_argument("--by", default="", help="Operator email engaging the disarm (recorded for audit).")
        parser.add_argument("--reason", default="", help="Why the disarm was engaged (recorded for audit).")

    def handle(self, *args, **o):
        from trade_copier.models import (
            CopierLiveAuthorization, CopierLiveAuthorizationStatus, CopierLiveKillSwitch)

        actor = User.objects.filter(email__iexact=o["by"]).first() if o["by"] else None
        row = CopierLiveKillSwitch.objects.create(engaged=True, engaged_by=actor, reason=(o["reason"] or "")[:256])
        active = CopierLiveAuthorization.objects.filter(
            status=CopierLiveAuthorizationStatus.ACTIVE).count()
        self.stdout.write(self.style.SUCCESS(
            f"LIVE copier DISARMED: CopierLiveKillSwitch id={row.id} engaged at {row.engaged_at.isoformat()} "
            f"by={getattr(actor, 'email', o['by'] or 'unspecified')}. "
            f"{active} ACTIVE authorization(s) preserved but now blocked. No new LIVE copier order can dispatch."))
