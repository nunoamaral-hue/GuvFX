"""CVM-Inc-3 B1 — beta ProvisioningJob worker (thin CLI wrapper around ``beta_worker.process_one``).

Dark by default: does nothing unless ``BETA_RUNTIMES_ENABLED`` is on AND a claimable beta job exists.

    python manage.py run_beta_provisioning_worker --once
    python manage.py run_beta_provisioning_worker            # loop
"""
import time

from django.core.management.base import BaseCommand

from terminal_provisioning.beta_worker import process_one


class Command(BaseCommand):
    help = "Claim + advance beta ProvisioningJobs through the signed management channel (dark until armed)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="process one claim and exit")
        parser.add_argument("--interval", type=float, default=2.0, help="loop poll interval (seconds)")
        parser.add_argument("--no-negotiate", action="store_true",
                            help="skip the version handshake (testing only)")

    def handle(self, *args, **o):
        negotiate = not o["no_negotiate"]
        if o["once"]:
            self.stdout.write(process_one(negotiate=negotiate))
            return
        self.stdout.write("beta provisioning worker started (dark unless BETA_RUNTIMES_ENABLED).")
        while True:
            status = process_one(negotiate=negotiate)
            if status not in ("advanced",):
                time.sleep(o["interval"])
