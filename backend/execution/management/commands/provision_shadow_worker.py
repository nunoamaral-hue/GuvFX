"""
EXEC-E2b-PERSIST — provision the dedicated shadow-worker WorkerIdentity.

Creates (or updates) the distinct ``WorkerIdentity`` used by the managed shadow
worker service and grants it the ``shadow_worker`` permission. Idempotent.

Safety properties:
- The secret is read ONLY from the ``MT5_SHADOW_WORKER_TOKEN`` environment
  variable (never a CLI arg, so it does not appear in the process list) and is
  never printed — only its SHA-256 hash is stored.
- The shadow worker id MUST differ from the normal ingest worker id; the command
  refuses to reuse the normal identity (packet REQUIRED 1 / prohibition on using
  the normal worker identity for the shadow worker).
- Granting ``shadow_worker`` only lets the identity *claim* PLACE_ORDER_SHADOW
  jobs via the next_job endpoint guard. It does NOT enable any order placement:
  the shadow worker runs order_check-only (see mt5_trade_ingest_worker.py).

Usage (token supplied via env, never on the command line)::

    MT5_SHADOW_WORKER_TOKEN=... python manage.py provision_shadow_worker
    python manage.py provision_shadow_worker --revoke        # rollback
"""

import os

from django.core.management.base import BaseCommand, CommandError

from core.audit import log_credential_event
from execution.models import WorkerIdentity

DEFAULT_SHADOW_WORKER_ID = "mt5-shadow-worker-1"
DEFAULT_NORMAL_WORKER_ID = "mt5-trade-ingest-1"


class Command(BaseCommand):
    help = (
        "Create/update the dedicated shadow-worker WorkerIdentity (shadow_worker "
        "permission). Secret is read from MT5_SHADOW_WORKER_TOKEN and never printed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--worker-id",
            default=os.environ.get("MT5_SHADOW_WORKER_ID", DEFAULT_SHADOW_WORKER_ID),
            help="Shadow worker identity id (default env MT5_SHADOW_WORKER_ID or "
            f"'{DEFAULT_SHADOW_WORKER_ID}').",
        )
        parser.add_argument(
            "--normal-worker-id",
            default=os.environ.get("MT5_WORKER_ID", DEFAULT_NORMAL_WORKER_ID),
            help="Normal ingest worker id to guard against reuse (default env "
            f"MT5_WORKER_ID or '{DEFAULT_NORMAL_WORKER_ID}').",
        )
        parser.add_argument(
            "--revoke",
            action="store_true",
            help="Revoke (set REVOKED + drop shadow_worker) instead of provisioning.",
        )

    def handle(self, *args, **options):
        worker_id = (options["worker_id"] or "").strip()
        normal_id = (options["normal_worker_id"] or "").strip()

        if not worker_id:
            raise CommandError("shadow worker id is empty")
        if worker_id == normal_id:
            raise CommandError(
                f"shadow worker id '{worker_id}' must differ from the normal "
                f"ingest worker id '{normal_id}' — never reuse the normal identity"
            )

        if options["revoke"]:
            updated = WorkerIdentity.objects.filter(worker_id=worker_id).update(
                status=WorkerIdentity.Status.REVOKED,
                worker_permissions={},
            )
            if updated:
                log_credential_event("REVOKED", entity_type="WorkerIdentity",
                                     entity_id=worker_id, actor="provision_shadow_worker")
            self.stdout.write(
                f"shadow worker identity '{worker_id}' revoked (rows={updated})"
            )
            return

        token = os.environ.get("MT5_SHADOW_WORKER_TOKEN")
        if not token:
            raise CommandError(
                "MT5_SHADOW_WORKER_TOKEN environment variable must be set "
                "(the secret is never accepted as a CLI argument)"
            )

        obj, created = WorkerIdentity.objects.get_or_create(
            worker_id=worker_id,
            defaults={
                "worker_secret_hash": WorkerIdentity.hash_secret(token),
                "worker_permissions": {"shadow_worker": True},
                "status": WorkerIdentity.Status.ACTIVE,
            },
        )
        if not created:
            perms = dict(obj.worker_permissions or {})
            perms["shadow_worker"] = True
            obj.worker_permissions = perms
            obj.worker_secret_hash = WorkerIdentity.hash_secret(token)
            obj.status = WorkerIdentity.Status.ACTIVE
            obj.save(update_fields=["worker_secret_hash", "worker_permissions", "status"])

        # Credential lifecycle audit — CREATED on first provision, ROTATED when the
        # secret hash is re-set on an existing identity. The secret is never logged.
        log_credential_event(
            "CREATED" if created else "ROTATED",
            entity_type="WorkerIdentity", entity_id=worker_id,
            actor="provision_shadow_worker",
        )

        # Never print the secret — only the id and resulting state.
        self.stdout.write(
            f"shadow worker identity '{worker_id}' ready "
            f"(created={created}, status={obj.status}, "
            f"shadow_worker={bool((obj.worker_permissions or {}).get('shadow_worker'))})"
        )
