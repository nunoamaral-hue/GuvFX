"""
TX-1C session assignment / routing operations (read + lifecycle).

Examples:
  manage.py session_assignment --account-id 6 --action assign
  manage.py session_assignment --account-id 6 --action readiness
  manage.py session_assignment --account-id 6 --action enable
  manage.py session_assignment --account-id 6 --action resolve
"""
import json

from django.core.management.base import BaseCommand, CommandError

from terminal_provisioning import session as routing
from terminal_provisioning.models import SessionAssignment


class Command(BaseCommand):
    help = "TX-1C: session assignment routing (assign/readiness/enable/disable/resolve/status)."

    def add_arguments(self, parser):
        parser.add_argument("--account-id", type=int, required=True)
        parser.add_argument(
            "--action",
            choices=["assign", "readiness", "enable", "disable", "resolve", "status"],
            default="readiness",
        )

    def handle(self, *args, **opts):
        aid = opts["account_id"]
        action = opts["action"]
        try:
            if action == "readiness":
                self.stdout.write(json.dumps(routing.evaluate_readiness(aid), indent=2))
                return
            if action == "resolve":
                self.stdout.write(json.dumps({"route": routing.resolve_route(aid)}, indent=2))
                return
            if action == "assign":
                sa = routing.assign(aid)
            elif action == "enable":
                sa = routing.set_enabled(aid, True)
            elif action == "disable":
                sa = routing.set_enabled(aid, False)
            else:  # status
                sa = SessionAssignment.objects.filter(trading_account_id=aid).first()
                if sa is None:
                    self.stdout.write(json.dumps({"account_id": aid, "assigned": False}))
                    return
        except routing.SessionAssignmentError as e:
            raise CommandError(f"routing failed (controlled): {e}")

        self.stdout.write(json.dumps({
            "account_id": sa.trading_account_id,
            "windows_username": sa.windows_username,
            "runtime_root": sa.runtime_root,
            "readiness": sa.readiness,
            "eligible": sa.eligible,
            "enabled": sa.enabled,
            "reason": sa.readiness_detail.get("reason"),
        }, indent=2))
