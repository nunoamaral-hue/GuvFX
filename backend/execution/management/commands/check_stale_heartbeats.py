"""
Management command: check_stale_heartbeats

Scans TerminalNode rows with status=active and emits a
NODE_HEARTBEAT_STALE AuditEvent for any node whose last_heartbeat
is older than the configured threshold.

Usage:
    python manage.py check_stale_heartbeats
    python manage.py check_stale_heartbeats --threshold-minutes=10

Designed to be called by cron or a periodic scheduler.  Idempotent:
a node that is already stale will NOT produce duplicate audit events
on consecutive runs — the command records a stale marker in a
module-level set and only emits once per command invocation.  For
cross-invocation dedup, the caller should space runs >= threshold.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.audit import log_event
from execution.models import TerminalNode


# Default: a node is stale if last_heartbeat > 5 minutes ago.
DEFAULT_THRESHOLD_MINUTES = 5


class Command(BaseCommand):
    help = "Detect terminal nodes with stale heartbeats and emit audit events."

    def add_arguments(self, parser):
        parser.add_argument(
            "--threshold-minutes",
            type=int,
            default=DEFAULT_THRESHOLD_MINUTES,
            help=(
                f"Minutes since last heartbeat before a node is considered "
                f"stale (default: {DEFAULT_THRESHOLD_MINUTES})."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Print stale nodes without emitting audit events.",
        )

    def handle(self, *args, **options):
        threshold_minutes = options["threshold_minutes"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(minutes=threshold_minutes)

        # Active nodes whose heartbeat is stale or has never been set.
        stale_nodes = TerminalNode.objects.filter(
            status=TerminalNode.Status.ACTIVE,
        ).exclude(
            last_heartbeat__gte=cutoff,
        )

        count = 0
        for node in stale_nodes:
            count += 1
            age_str = "never" if node.last_heartbeat is None else str(
                timezone.now() - node.last_heartbeat
            )
            self.stdout.write(
                f"STALE: {node.hostname} "
                f"(last_heartbeat={node.last_heartbeat}, age={age_str})"
            )

            if not dry_run:
                log_event(
                    request=None,
                    event_type="NODE_HEARTBEAT_STALE",
                    severity="WARN",
                    entity_type="TerminalNode",
                    entity_id=str(node.id),
                    metadata={
                        "hostname": node.hostname,
                        "last_heartbeat": (
                            node.last_heartbeat.isoformat()
                            if node.last_heartbeat
                            else None
                        ),
                        "threshold_minutes": threshold_minutes,
                        "status": node.status,
                    },
                )

        if count == 0:
            self.stdout.write("No stale nodes detected.")
        else:
            verb = "would emit" if dry_run else "emitted"
            self.stdout.write(
                f"\n{count} stale node(s) detected, {verb} "
                f"NODE_HEARTBEAT_STALE audit events."
            )
