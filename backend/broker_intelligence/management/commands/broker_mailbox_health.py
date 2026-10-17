"""INGESTION_INCIDENT_R1 (§5) — print the DARK, read-only mailbox ingestion health report.

READ-ONLY: reads only ConnectedMailbox metadata (never a credential, never Gmail). Secret-free: emits masked
addresses + stable diagnostic codes only. Makes a mailbox that has been failing for hours identifiable at a glance,
WITHOUT decrypting a credential — the gap the 2026-10-14 outage exposed (a green systemd unit over a dead mailbox).

No flag gate: it is a pure read model and discloses nothing sensitive. It performs no ingestion and no Gmail call."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = ("Print the read-only broker-mailbox ingestion health report (R1 §5). Secret-free; no Gmail call, no "
            "credential read, no write.")

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Emit the report as a single JSON object.")
        parser.add_argument("--all-statuses", action="store_true",
                            help="Include mailboxes in any status (default: CONNECTED only).")
        # Opt-in alerting: let a scheduled run SIGNAL a dead/degraded mailbox via a non-zero exit so ordinary cron /
        # monitoring catches the exact 'green systemd unit over a dead mailbox' shape the 2026-10-14 outage exposed.
        # Default invocation still exits 0 (behaviour preserved) — the report ALWAYS prints first.
        parser.add_argument("--exit-nonzero-on-recovery", action="store_true",
                            help="Exit non-zero if any mailbox is RECOVERY_REQUIRED (for scheduled alerting).")
        parser.add_argument("--exit-nonzero-on-degraded", action="store_true",
                            help="Exit non-zero if any mailbox is degraded (stale or recovery-required).")

    def _maybe_alert(self, report, o):
        """Raise (non-zero exit) AFTER the report has printed, iff an opt-in alert flag is set and its condition
        holds. Default (no flag) never raises, so existing invocations keep exit 0."""
        reasons = []
        if o.get("exit_nonzero_on_recovery") and report.get("any_recovery_required"):
            reasons.append("any_recovery_required")
        if o.get("exit_nonzero_on_degraded") and report.get("any_degraded"):
            reasons.append("any_degraded")
        if reasons:
            raise CommandError("mailbox health alert: %s (see report above)" % ", ".join(reasons))

    def handle(self, *args, **o):
        from broker_intelligence.health import service_health

        report = service_health(include_all_statuses=o["all_statuses"])
        if o["json"]:
            self.stdout.write(json.dumps(report, default=str))
            self._maybe_alert(report, o)
            return
        self.stdout.write(self.style.SUCCESS("broker mailbox ingestion health:"))
        self.stdout.write(f"  generated_at = {report['generated_at']}")
        self.stdout.write(f"  note = {report['service_running_note']}")
        self.stdout.write(f"  stale_seconds = {report['stale_seconds']}  recovery_seconds = {report['recovery_seconds']}")
        self.stdout.write(f"  mailboxes = {report['count']}  any_degraded = {report['any_degraded']}  "
                          f"any_recovery_required = {report['any_recovery_required']}")
        for m in report["mailboxes"]:
            self.stdout.write(
                f"  - mailbox {m['mailbox_id']} [{m['masked_email']}] status={m['status']} state={m['state']} "
                f"elapsed_s={m['elapsed_since_sync_seconds']} last_error_code={m['last_error_code'] or '-'} "
                f"http={m['last_error_http_status']} consec_fail={m['consecutive_failures']} "
                f"cursor_advanced={m['last_pass_cursor_advanced']}")
        self._maybe_alert(report, o)
