"""
TX-1D dedicated-viewer routing operations (observability + pilot prep).

Examples:
  manage.py viewer_session --account-id 6 --action decide
  manage.py viewer_session --account-id 6 --action mark-populated --runtime-version 5.0.0.5833
  manage.py viewer_session --account-id 6 --action descriptor
  manage.py viewer_session --account-id 6 --action rollback-demo
  manage.py viewer_session --action killswitch
"""
import json

from django.core.management.base import BaseCommand, CommandError

from terminal_provisioning import viewer


class Command(BaseCommand):
    help = "TX-1D viewer routing: decide / mark-populated / descriptor / rollback-demo / killswitch."

    def add_arguments(self, parser):
        parser.add_argument("--account-id", type=int)
        parser.add_argument(
            "--action",
            choices=["decide", "mark-populated", "descriptor", "rollback-demo", "killswitch"],
            default="decide",
        )
        # NB: must NOT be "--version" — Django's BaseCommand reserves that.
        parser.add_argument("--runtime-version", default=viewer.GOLDEN_MT5_BUILD)

    def handle(self, *args, **opts):
        action = opts["action"]
        if action == "killswitch":
            self.stdout.write(json.dumps({
                "env": viewer.KILL_SWITCH_ENV,
                "dedicated_path_enabled": viewer.dedicated_path_enabled(),
            }, indent=2))
            return

        aid = opts.get("account_id")
        if aid is None:
            raise CommandError("--account-id required for this action")

        if action == "decide":
            self.stdout.write(json.dumps(viewer.decide_viewer_path(aid), indent=2))
        elif action == "mark-populated":
            prov = viewer.mark_runtime_populated(aid, version=opts["runtime_version"])
            self.stdout.write(json.dumps({
                "account_id": prov.trading_account_id, "runtime_root": prov.runtime_root,
                "runtime_populated": prov.runtime_populated, "runtime_version": prov.runtime_version,
            }, indent=2))
        elif action == "descriptor":
            self.stdout.write(json.dumps({"dedicated_descriptor": viewer.build_dedicated_descriptor(aid)}, indent=2))
        elif action == "rollback-demo":
            self.stdout.write(json.dumps(viewer.demonstrate_rollback(aid), indent=2))
