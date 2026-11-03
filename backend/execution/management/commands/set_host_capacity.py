"""ADR-0069 — governed primitive to SET / CHANGE a physical host's CERTIFIED host-wide capacity ceiling.

Writes ``execution.HostCapacity`` (the single documented source the allocator guard reads) plus an append-only
``execution.HostCapacityAudit`` row, so a capacity ceiling is never an undocumented constant and every change
carries an actor, a reason and a timestamp (governance: data.md "configurable paths", evidence.md). Dry-run by
default; ``--apply`` to persist. It NEVER raises capacity silently and NEVER arms execution.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from execution.models import HostCapacity, HostCapacityAudit, TerminalNode


class Command(BaseCommand):
    help = ("Set/change a physical host's certified host-wide capacity ceiling (ADR-0069). "
            "Dry-run unless --apply. Records an append-only audit row.")

    def add_arguments(self, parser):
        parser.add_argument("--host-key", required=True,
                            help="Physical host identity = the co-resident TerminalNodes' rdp_host (e.g. an IP).")
        parser.add_argument("--max", type=int, required=True, dest="max_total",
                            help="CERTIFIED host-wide ceiling (max distinct occupants across ALL co-resident nodes).")
        parser.add_argument("--safe", type=int, default=None,
                            help="Informational recommended-safe occupancy (<= --max).")
        parser.add_argument("--basis", default="",
                            help="Secret-free certification basis (hardware/methodology/date). No credentials/PII.")
        parser.add_argument("--actor", default="", help="Who certified this (operator id / ticket).")
        parser.add_argument("--apply", action="store_true", help="Persist. Omit for a dry-run preview.")

    def handle(self, *args, **o):
        hk = (o["host_key"] or "").strip()
        if not hk:
            raise CommandError("--host-key must be non-empty")
        if o["max_total"] < 0:
            raise CommandError("--max must be >= 0")
        if o["safe"] is not None and o["safe"] > o["max_total"]:
            raise CommandError("--safe must be <= --max")

        nodes = list(TerminalNode.objects.filter(rdp_host__iexact=hk).order_by("id"))
        node_desc = (", ".join(f"{n.hostname}(status={n.status},max={n.max_accounts})" for n in nodes)
                     or "(no TerminalNode currently has this rdp_host)")
        existing = HostCapacity.objects.filter(physical_host_key__iexact=hk).first()
        prev_max = existing.max_total_occupants if existing else None
        prev_safe = existing.certified_safe if existing else None

        self.stdout.write(f"host_key={hk}")
        self.stdout.write(f"  co-resident logical nodes: {node_desc}")
        self.stdout.write(f"  host-wide ceiling: {prev_max} -> {o['max_total']}   safe: {prev_safe} -> {o['safe']}")
        if not nodes:
            self.stdout.write(self.style.WARNING(
                "  NOTE: no TerminalNode has this rdp_host yet; the ceiling is being pre-seeded."))

        if not o["apply"]:
            self.stdout.write(self.style.WARNING("DRY-RUN: nothing written. Re-run with --apply to persist."))
            return

        with transaction.atomic():
            row = (HostCapacity.objects.select_for_update()
                   .filter(physical_host_key__iexact=hk).first())
            if row is None:
                row = HostCapacity(physical_host_key=hk)
            row.max_total_occupants = o["max_total"]
            row.certified_safe = o["safe"]
            if o["basis"]:
                row.basis = o["basis"]
            if o["actor"]:
                row.certified_by = o["actor"]
            row.certified_at = timezone.now()
            row.save()
            HostCapacityAudit.objects.create(
                physical_host_key=hk, previous_max=prev_max, new_max=o["max_total"],
                previous_safe=prev_safe, new_safe=o["safe"], actor=o["actor"], reason=o["basis"])
        self.stdout.write(self.style.SUCCESS(
            f"Applied. {hk} host-wide ceiling now {o['max_total']} (safe {o['safe']}). Audit row written."))
