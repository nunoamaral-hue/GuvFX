"""
Management command: reap_stuck_launches

Releases terminal bindings held by InteractionSessions whose MT5Session is
stuck in the non-terminal 'launching' state beyond a timeout — i.e. a launch
that never reached 'connected'. Without this watchdog a hung launch holds the
binding occupied indefinitely (occupied_by_session set, binding status
'launching') and blocks Terminal Access for everyone — the recurring
"terminal keeps getting stuck on launching" symptom.

Reaping uses the canonical terminate_session service (ends MT5Sessions, marks
the InteractionSession ended, releases binding occupancy + audit). Idempotent:
already-ended sessions and successfully-connected sessions are never touched.

Designed to be called every minute by cron alongside the strategy schedulers.

Usage:
    python manage.py reap_stuck_launches
    python manage.py reap_stuck_launches --max-launching-minutes 10
    python manage.py reap_stuck_launches --dry-run
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from mt5.models import MT5Session
from mt5.services.session_terminate_service import terminate_session, TerminateError

# MT5Session states that represent an in-flight launch that has not yet
# connected. A session that reached 'connected'/'active' is a healthy live
# session and must NOT be reaped here.
NON_TERMINAL_LAUNCH_STATES = ["launching"]

DEFAULT_MAX_LAUNCHING_MINUTES = 10


class Command(BaseCommand):
    help = (
        "Terminate InteractionSessions stuck mid-launch (MT5Session 'launching', "
        "never connected) beyond a timeout, releasing the held terminal binding."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-launching-minutes",
            type=int,
            default=DEFAULT_MAX_LAUNCHING_MINUTES,
            help=(
                "Minutes a launch may remain in 'launching' before it is "
                f"considered hung (default: {DEFAULT_MAX_LAUNCHING_MINUTES})."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Report what would be reaped without terminating anything.",
        )

    def handle(self, *args, **options):
        max_minutes = options["max_launching_minutes"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(minutes=max_minutes)

        stuck = (
            MT5Session.objects
            .filter(state__in=NON_TERMINAL_LAUNCH_STATES)
            .filter(connected_at__isnull=True)
            .filter(created_at__lt=cutoff)
            .select_related("interaction_session")
        )

        reaped = 0
        failed = 0
        seen_sessions = set()

        for mt5s in stuck:
            sess = mt5s.interaction_session
            if sess is None or sess.pk in seen_sessions or sess.state == "ended":
                continue
            seen_sessions.add(sess.pk)

            if dry_run:
                self.stdout.write(
                    f"[DRY-RUN] would reap interaction_session={sess.pk} "
                    f"mt5_session={mt5s.pk} binding={sess.terminal_binding_id} "
                    f"created={mt5s.created_at.isoformat()}"
                )
                continue

            try:
                terminate_session(
                    sess,
                    reason=f"reaper: launch stuck in 'launching' > {max_minutes}m",
                    actor_user_id=None,
                )
                reaped += 1
                self.stdout.write(
                    f"Reaped stuck launch: interaction_session={sess.pk} "
                    f"binding={sess.terminal_binding_id}"
                )
            except TerminateError:
                # Already terminated by a concurrent run — fine.
                continue
            except Exception as exc:  # noqa: BLE001 — keep the watchdog resilient
                failed += 1
                self.stderr.write(
                    f"  ERROR reaping interaction_session={sess.pk}: {exc}"
                )

        if not dry_run:
            self.stdout.write(
                f"reap_stuck_launches done: reaped={reaped} failed={failed}"
            )
