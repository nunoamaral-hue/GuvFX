"""run_copier_execution_driver — ADR-0117 governed, supervised copier EXECUTION DRIVER (fallback + reconciliation).

A single-flight, bounded, crash-safe worker that drives the EXISTING, individually-gated copier stages
(replicate -> execute OPEN -> reconcile -> execute MODIFY/FULL_CLOSE -> mutation-dispatch -> lifecycle-dispatch).
The ADR-0094 immediate-dispatch inline path stays the <=1 s primary; this is the fallback/reconciliation executor.

DARK-by-default: with ``COPIER_EXECUTION_DRIVER_ENABLED`` off (the default) it acquires no lease and runs no stage.
Even armed, every stage re-reads its OWN gate + the per-pair live admission/arm, so nothing executes unless the
whole stack is on. Single active driver: a file lease on its OWN path (independent of the deploy/recovery lease).
``--status`` is read-only (no lease). No secret is read or printed.

    python manage.py run_copier_execution_driver --status
    python manage.py run_copier_execution_driver --iterations 1            # one bounded fallback pass (supervisor re-invokes)
    python manage.py run_copier_execution_driver --iterations 10 --interval 2   # bounded loop under one lease
"""
from __future__ import annotations

import json
import secrets
import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("ADR-0117: single-flight, bounded, DARK copier execution driver (fallback + reconciliation). "
            "No-op unless COPIER_EXECUTION_DRIVER_ENABLED; each stage re-reads its own gate.")

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--iterations", type=int, default=1, help="Bounded number of passes under ONE lease.")
        parser.add_argument("--interval", type=float, default=0.0, help="Seconds between iterations (loop mode).")
        parser.add_argument("--lease-ttl", type=int, default=300)
        parser.add_argument("--lease-path", default=None, help="Override the driver lease FILE path (tests/ops).")
        parser.add_argument("--owner", default="copier-execution-driver")
        parser.add_argument("--status", action="store_true", default=False,
                            help="Read-only readiness/status; acquire NO lease, run NO stage.")
        parser.add_argument("--force", action="store_true", default=False,
                            help="Run even if the driver flag is off (diagnostics). Stage gates still apply.")

    def _lease_path(self, override):
        if override:
            return override
        from trade_copier.execution_driver import default_driver_lease_path
        return default_driver_lease_path()

    def handle(self, *args, **o):
        from django.utils import timezone

        from trade_copier.execution_driver import dead_letter_summary, driver_status, run_driver_pass
        from trade_copier.flags import copier_execution_driver_enabled
        lease_path = self._lease_path(o["lease_path"])

        if o["status"]:
            self.stdout.write(json.dumps(driver_status(lease_path=lease_path), indent=1, default=str))
            return

        if not o["force"] and not copier_execution_driver_enabled():
            self.stdout.write(json.dumps({"ok": True, "ran": False, "reason": "driver_disabled_dark"}, indent=1))
            return

        from hosted_workspace.recovery_lease import KIND_COPIER_DRIVER, acquire, release
        corr = "cpd-" + secrets.token_hex(6)
        try:
            ok, info = acquire(KIND_COPIER_DRIVER, purpose="copier-execution-driver", owner=o["owner"],
                               correlation_id=corr, ttl_s=int(o["lease_ttl"]), now=timezone.now(), path=lease_path)
        except Exception as e:  # noqa: BLE001 — a lease-store error must fail closed, never run unguarded
            self.stdout.write(json.dumps({"ok": False, "ran": False, "reason": "lease_error",
                                          "detail": type(e).__name__}, indent=1))
            return
        if not ok:
            self.stdout.write(json.dumps({"ok": True, "ran": False, "reason": "lease_unavailable",
                                          "detail": str(info)}, indent=1))
            return

        passes = []
        try:
            iterations = max(1, int(o["iterations"]))
            for i in range(iterations):
                passes.append(run_driver_pass(limit=int(o["limit"])))
                if o["interval"] and i < iterations - 1:
                    time.sleep(float(o["interval"]))
        finally:
            release(corr, timezone.now(), path=lease_path)

        self.stdout.write(json.dumps({"ok": True, "ran": True, "iterations": len(passes),
                                      "passes": passes, "dead_letter": dead_letter_summary()},
                                     indent=1, default=str))
