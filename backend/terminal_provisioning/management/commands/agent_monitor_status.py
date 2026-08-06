"""Monitoring-Runner WS-I — read-only ops EVIDENCE for the validation-agent monitor.

    python manage.py agent_monitor_status            # human-readable
    python manage.py agent_monitor_status --json      # machine-readable

Prints the durable ``AgentMonitorState`` projection: last probe age, current state/band, supervised, last
reason, consecutive success/fail, flap count, open alert names, last delivery result, whether monitoring +
the alert channel are enabled, and the scheduler interval. It is a backend-shell (staff-only) surface and
makes NO change of any kind. It NEVER prints a Telegram chat id, bot token, keyring, key id, or any
credential — only the sanitised operational fields the evidence presenter already carries."""
from __future__ import annotations

import json
import time

from django.core.management.base import BaseCommand

from terminal_provisioning import agent_monitor_runner as runner
from terminal_provisioning.agent_alert_sink import build_alert_sink
from terminal_provisioning.models import AgentMonitorState


class Command(BaseCommand):
    help = "Show the durable validation-agent monitor state (read-only ops evidence; no secrets)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="emit machine-readable evidence on stdout")

    def handle(self, *args, **o):
        state = AgentMonitorState.load()
        sink = build_alert_sink()                       # channel + owner only; never resolves a secret here
        evidence = runner.state_evidence(
            state, now=time.time(),
            sink_channel=getattr(sink, "channel", ""), sink_owner=getattr(sink, "owner", ""))

        if o["json"]:
            self.stdout.write(json.dumps(evidence))
            return
        age = evidence["last_probe_age_seconds"]
        age_s = f"{age:.0f}s ago" if age is not None else "never"
        self.stdout.write("validation-agent monitor status")
        self.stdout.write(f"  monitoring_enabled : {evidence['monitoring_enabled']}")
        self.stdout.write(f"  scheduler_interval : {evidence['scheduler_interval_seconds']}s")
        self.stdout.write(f"  state / band       : {evidence['current_state'] or '-'} / "
                          f"{evidence['current_band'] or '-'}")
        self.stdout.write(f"  supervised         : {evidence['supervised']}")
        self.stdout.write(f"  alerting (open)    : {evidence['alerting']}  {evidence['open_alert_names']}")
        self.stdout.write(f"  last_reason        : {evidence['last_reason'] or '-'}")
        self.stdout.write(f"  last_probe         : {age_s}")
        self.stdout.write(f"  consec healthy/unav: {evidence['consecutive_healthy']} / "
                          f"{evidence['consecutive_unavailable']}   flaps={evidence['flap_count']}")
        self.stdout.write(f"  last_delivery      : {evidence['last_delivery'] or '-'}")
        self.stdout.write(f"  alert_channel/owner: {evidence['alert_channel'] or '-'} / "
                          f"{evidence['alert_owner'] or '-'}")
        self.stdout.write(f"  run_count          : {evidence['run_count']}")
