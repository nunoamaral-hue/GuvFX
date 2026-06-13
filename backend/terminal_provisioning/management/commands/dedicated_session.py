"""
TX-1E dedicated session delivery operations (observability + pilot prep).

  manage.py dedicated_session --account-id 6 --action deliver
  manage.py dedicated_session --account-id 6 --action descriptor
  manage.py dedicated_session --action hygiene
  manage.py dedicated_session --account-id 6 --action rollback-demo
"""
import json

from django.core.management.base import BaseCommand, CommandError

from terminal_provisioning import delivery, viewer


class Command(BaseCommand):
    help = "TX-1E: deliver / descriptor / kiosk-enabled / hygiene / rollback-demo."

    def add_arguments(self, parser):
        parser.add_argument("--account-id", type=int)
        parser.add_argument(
            "--action",
            choices=["deliver", "descriptor", "kiosk-enabled", "hygiene", "rollback-demo"],
            default="deliver",
        )

    def handle(self, *args, **opts):
        action = opts["action"]
        if action == "hygiene":
            self.stdout.write(json.dumps(delivery.hygiene_policy(), indent=2))
            return
        aid = opts.get("account_id")
        if aid is None:
            raise CommandError("--account-id required")
        if action == "deliver":
            self.stdout.write(json.dumps(delivery.deliver_session(aid), indent=2))
        elif action == "descriptor":
            self.stdout.write(json.dumps({"descriptor": delivery.build_dedicated_session_descriptor(aid)}, indent=2))
        elif action == "kiosk-enabled":
            prov = delivery.record_kiosk_enabled(aid)
            self.stdout.write(json.dumps({"account_id": prov.trading_account_id, "kiosk_recorded": True}, indent=2))
        elif action == "rollback-demo":
            self.stdout.write(json.dumps(viewer.demonstrate_rollback(aid), indent=2))
