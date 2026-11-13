"""reconcile_monitoring_enrolment — governed, idempotent monitoring-ENROLMENT reconciler (ADR-0103).

GFX-DASHBOARD-AUTO-MONITORING-ENROLMENT-087. Discovers every eligible broker-connected account and reports
(or, with ``--apply``, idempotently reserves/registers) its deterministic monitoring route — so a newly
connected account becomes a complete dashboard data source automatically, not via a bespoke manual packet.

DRY-RUN BY DEFAULT (house style — mirrors ``catalogue_delta`` / ``broker_fingerprint``): with no ``--apply``
it only PROJECTS each account's enrolment stage and prints a deterministic, secret-free JSON report. It makes
no write and no host contact.

``--apply`` is TWO-LEVEL DARK: it reserves/registers ``MonitoringRoute`` rows ONLY when BOTH
``MONITORING_GATEWAY_ENABLED`` and ``MONITORING_ENROLMENT_RECONCILE_ENABLED`` are on (and the underlying
primitive is itself gated on the gateway flag). Even then it reserves/registers from each account's EXISTING
endpoint and raises governed alerts — it NEVER marks a route READY, contacts a host, mutates a
bridge/ACL/account/workspace, or places a trading action. No login/account number is ever printed.
"""
import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = ("Reconcile monitoring enrolment (dry-run by default; --apply reserves/registers routes, "
            "two-level dark). Read-only projection of every eligible account's monitoring stage.")

    def add_arguments(self, parser):
        parser.add_argument("action", nargs="?", default="report", choices=["report", "reconcile"],
                            help="report = project stages only (default); reconcile = same, honouring --apply.")
        parser.add_argument("--apply", action="store_true",
                            help="Reserve/register routes (two-level dark; inert unless the gateway + enrolment "
                                 "flags are on). Omit for a read-only report.")
        parser.add_argument("--account", type=int, default=None,
                            help="Restrict to ONE account id (targeted diagnosis).")
        parser.add_argument("--owner", type=int, default=None,
                            help="Restrict to one owner (user id) — tenant-scoped reconcile.")
        parser.add_argument("--no-alerts", action="store_true",
                            help="With --apply: do not open/resolve enrolment alerts (reserve/register only).")

    def handle(self, *args, **options):
        from execution import monitoring_enrolment as enrol
        action = options.get("action") or "report"
        apply = bool(options.get("apply")) and action == "reconcile"
        owner = None
        owner_id = options.get("owner")
        if owner_id is not None:
            try:
                from django.contrib.auth import get_user_model
                owner = get_user_model().objects.filter(id=owner_id).first()
                if owner is None:
                    raise CommandError(f"owner user id {owner_id} not found")
            except CommandError:
                raise
            except Exception as e:  # noqa: BLE001 — never leak internals
                raise CommandError(f"owner resolution failed: {type(e).__name__}")
        try:
            report = enrol.reconcile_enrolment(
                owner=owner, account_id=options.get("account"), apply=apply,
                emit_alerts=not options.get("no_alerts"))
        except Exception as e:  # noqa: BLE001 — sanitised, never a secret/stack to stdout
            raise CommandError(f"enrolment reconcile failed: {type(e).__name__}")
        self.stdout.write(json.dumps(report, default=str, indent=2, sort_keys=True))
