"""Monitoring-Runner WS-G — send ONE clearly-marked SYNTHETIC alert through the CONFIGURED ops alert sink,
to prove the delivery path end-to-end BEFORE (and after) arming — the pre-arm gate the Aug-5 outage lacked.

    python manage.py test_agent_alert_delivery --correlation-id ops-check-2026-08-06
    python manage.py test_agent_alert_delivery --severity HIGH --json

It is a delivery *probe*, nothing else. It:
  * builds a synthetic ``Alert`` whose name/detail SHOUT that it is a test (never mistakable for a real page);
  * routes it through ``build_alert_sink()`` — the exact sink the monitor uses, so a green result proves the
    real channel (in the repo default that sink is NULL, so it sends nothing and says so);
  * makes NO state change (does not touch ``AgentMonitorState``), performs NO broker validation, touches NO
    credential, creates NO attempt, starts NO MT5, and reads/writes NO customer account;
  * prints the (secret-free) ``DeliveryResult`` so an operator can confirm the alert arrived.

Exit code: 0 if delivered (or intentionally suppressed/no-channel), 1 if a configured channel FAILED.
"""
from __future__ import annotations

import json
import time

from django.core.management.base import BaseCommand

from terminal_provisioning.agent_alert_sink import build_alert_sink
from terminal_provisioning.agent_monitoring import Alert


class Command(BaseCommand):
    help = "Deliver one synthetic TEST alert through the configured ops alert sink (no state/broker/customer)."

    def add_arguments(self, parser):
        parser.add_argument("--correlation-id", default="", help="operator-supplied id to trace the message")
        parser.add_argument("--severity", default="HIGH", choices=["HIGH", "MEDIUM", "LOW"],
                            help="severity to stamp on the synthetic alert (default HIGH)")
        parser.add_argument("--json", action="store_true", help="emit machine-readable result on stdout")

    def handle(self, *args, **o):
        corr = (o.get("correlation_id") or f"alert-selftest-{int(time.time())}").strip()
        alert = Alert(
            name="agent_alert_delivery_test",
            severity=o["severity"],
            detects_state="TEST",
            runbook="alert-delivery-self-test",
            detail="SYNTHETIC TEST alert - no incident. Verifies the ops alert channel end-to-end.")
        sink = build_alert_sink()
        result = sink.deliver(alert, now=time.time(), correlation_id=corr)
        payload = {"channel": getattr(sink, "channel", "?"), "owner": getattr(sink, "owner", ""),
                   "correlation_id": corr, **result.as_dict()}

        if o["json"]:
            self.stdout.write(json.dumps(payload))
        else:
            self.stdout.write(
                f"channel={payload['channel']} owner={payload['owner']} delivered={payload['delivered']} "
                f"suppressed={payload['suppressed']} reason={payload['reason']} corr={corr}")

        # A configured channel that FAILED is the only non-zero exit. NULL (no_channel_configured) and a
        # debounced/suppressed result are both exit 0 — they are not delivery FAILURES.
        if not result.delivered and not result.suppressed and result.reason not in ("", "no_channel_configured"):
            raise SystemExit(1)
