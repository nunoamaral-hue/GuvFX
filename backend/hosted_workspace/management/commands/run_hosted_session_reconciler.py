"""run_hosted_session_reconciler — P4 cold-boot SESSION recovery scheduler (DARK, 2026-09-30).

One reconcile pass on its OWN cadence and its OWN Postgres advisory lock (distinct from the observation cron), so
a cold-boot session sweep never contends with — or is throttled by — the minute observation cycle, and P5 can pace
it independently (staggered / rate-limited).

Two-level darkness: a DORMANT no-op unless ``HOSTED_SESSION_RECONCILER_ENABLED`` is on (or ``--force``); and even
then it self-gates on ``hosted_persistent_mt5_enabled()``. Probe-only by construction: this command wires NO
``establish_fn``, so it SELECTS / PROBES / CLASSIFIES / AUDITS and performs ZERO credential decryption and ZERO
self-connect regardless of the arm sub-gate. The actual guacd self-connect driver is added + wired in P4-c.

SINGLETON / no-overlap: a second cycle that finds the lock held simply skips. Emits a secret-free summary line. It
NEVER launches MT5, logs in, places an order, or arms execution.
"""
from django.core.management.base import BaseCommand
from django.db import connection

from hosted_workspace.flags import hosted_session_reconciler_enabled
from hosted_workspace.session_reconciler import run_hosted_session_reconciler

# Fixed 64-bit advisory-lock key for the session-reconciler singleton (distinct from the observation cron's key).
_SINGLETON_LOCK_KEY = 748_293_410_018


def try_acquire_singleton(key: int = _SINGLETON_LOCK_KEY) -> bool:
    """Non-blocking singleton guard. On Postgres, ``pg_try_advisory_lock`` — True iff acquired. On any other backend
    (e.g. a test sqlite) there is no cross-connection lock, so return True and rely on the cron cadence."""
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", [key])
        return bool(cur.fetchone()[0])


def release_singleton(key: int = _SINGLETON_LOCK_KEY) -> None:
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", [key])


class Command(BaseCommand):
    help = "Run one P4 cold-boot SESSION recovery reconcile pass (DARK; probe-only unless self-connect is armed)."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Run even if HOSTED_SESSION_RECONCILER_ENABLED is off (still self-gates on the "
                                 "master flag inside the runner; still probe-only unless separately armed).")

    def handle(self, *args, **options):
        if not (options.get("force") or hosted_session_reconciler_enabled()):
            self.stdout.write("session_reconciler: disabled (HOSTED_SESSION_RECONCILER_ENABLED off) - no-op")
            return
        if not try_acquire_singleton():
            self.stdout.write("session_reconciler: another pass holds the singleton lock - skipping")
            return
        try:
            summary = run_hosted_session_reconciler()
        finally:
            release_singleton()
        # Secret-free one-line summary (counts only; no identity beyond internal counters).
        self.stdout.write("session_reconciler: " + " ".join(f"{k}={v}" for k, v in summary.items()))
