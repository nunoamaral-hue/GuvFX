"""
Management command: cleanup_expired_mt5_sessions

Detects and terminates expired InteractionSessions by invoking
the approved expiry cleanup service.  This command is a thin
scheduler entrypoint only — all domain logic, binding release,
and audit emission are handled by the service layer.

Usage:
    python manage.py cleanup_expired_mt5_sessions
    python manage.py cleanup_expired_mt5_sessions --batch-size=50
"""
from django.core.management.base import BaseCommand

from mt5.services.expiry_cleanup_service import cleanup_expired_sessions


class Command(BaseCommand):
    help = (
        "Terminate expired MT5 InteractionSessions via the "
        "approved service-layer expiry/termination path."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Maximum number of expired sessions to process per run (default: 100).",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]

        result = cleanup_expired_sessions(batch_size=batch_size)

        if result.expired_found == 0:
            self.stdout.write("No expired sessions found.")
            return

        self.stdout.write(
            f"Expiry cleanup: found={result.expired_found} "
            f"terminated={result.terminated_ok} "
            f"failed={result.terminated_failed}"
        )

        for error in result.errors:
            self.stderr.write(f"  ERROR: {error}")

        if result.terminated_failed > 0:
            self.stderr.write(
                f"WARNING: {result.terminated_failed} session(s) "
                f"failed to terminate."
            )
