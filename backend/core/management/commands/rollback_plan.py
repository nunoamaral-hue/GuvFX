"""rollback_plan — print the safe flag-disable rollback plan (READ-ONLY; EXECUTES NOTHING).

    python manage.py rollback_plan          # human-readable plan
    python manage.py rollback_plan --json    # machine-readable plan

Reads the current feature-flag posture and prints the DARK rollback an operator would then perform
through the sanctioned mechanism. It changes NO flag, touches NO database, contacts NO host.
"""
import json

from django.core.management.base import BaseCommand

from core.rollback_planner import plan_rollback


class Command(BaseCommand):
    help = "Print the read-only, flag-disable rollback plan (executes nothing)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="as_json",
                            help="Emit the rollback plan as JSON.")

    def handle(self, *args, **opts):
        plan = plan_rollback()
        if opts["as_json"]:
            self.stdout.write(json.dumps(plan, indent=2, sort_keys=True))
            return
        self.stdout.write(f"POSTURE: {plan['posture']}")
        self.stdout.write(f"RULE: {plan['guiding_rule']}")
        self.stdout.write("")
        if plan["rollback_steps"]:
            self.stdout.write("Rollback steps (perform via the sanctioned mechanism; none are executed here):")
            for s in plan["rollback_steps"]:
                self.stdout.write(f"  {s['order']}. {s['action']}")
                self.stdout.write(f"       effect: {s['effect']} (returns_to={s['returns_to']}, "
                                  f"destructive={s['destructive']})")
        else:
            self.stdout.write("Already FULLY_DARK — no flag rollback required.")
        dep = plan["deploy_image_rollback"]
        self.stdout.write("")
        self.stdout.write(f"Deploy-image rollback (manual, Sponsor-approved): tag={dep['image_tag']}; "
                          f"reverse_migrations={dep['reverse_migrations']}")
