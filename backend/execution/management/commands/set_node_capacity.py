"""ADR-0082 — governed primitive to SET / CHANGE a hosted ``TerminalNode``'s logical capacity
(``max_accounts``), with a fail-closed physical-host certified-safe guard and an append-only audit row.

The PER-NODE analogue of ``set_host_capacity`` (ADR-0069, which owns the PHYSICAL-HOST ceiling). It is the only
supported, audited way to change a node's ``max_accounts`` — never a raw SQL / Django-admin / ORM field edit.
Dry-run by default; ``--apply`` (with a mandatory ``--reason``) to persist. It changes ONLY ``max_accounts`` and
writes a ``NodeCapacityAudit`` row atomically; it never touches the physical-host ceiling, workspace
assignments, tenant isolation, or execution controls, and it places no order.

Examples (synthetic identifiers only)::

    # Preview (no write) — raise a logical node to 17:
    python manage.py set_node_capacity --node-hostname example-beta-node --max 17

    # Persist (audited) — mandatory --reason:
    python manage.py set_node_capacity --node-hostname example-beta-node --max 17 \
        --apply --reason "GFX-TICKET-123: recover one stuck onboarding within certified-safe" --actor ops@example

Fail-closed refusals (no write): node missing/ambiguous/inactive; blank rdp_host; max below node occupancy or
outside [1, host-ceiling]; no certified HostCapacity row; certified_safe unset; host already over certified_safe;
or a raise that would permit physical-host occupancy beyond certified_safe.
"""
import json

from django.core.management.base import BaseCommand, CommandError

from execution.node_capacity import (
    apply_node_capacity_change,
    evaluate_node_capacity_change,
)


class Command(BaseCommand):
    help = ("Set/change a hosted TerminalNode's max_accounts (ADR-0082), fail-closed against the physical-host "
            "certified-safe occupancy. Dry-run unless --apply; --apply requires --reason. Writes an audit row.")

    def add_arguments(self, parser):
        sel = parser.add_mutually_exclusive_group(required=True)
        sel.add_argument("--node-hostname", help="Exact TerminalNode.hostname (unique).")
        sel.add_argument("--node-id", type=int, help="Immutable TerminalNode id.")
        parser.add_argument("--max", type=int, required=True, dest="max_accounts",
                            help="Proposed max_accounts for this node (>=1, <= physical-host certified ceiling).")
        parser.add_argument("--reason", default="",
                            help="MANDATORY with --apply: secret-free justification recorded in the audit row.")
        parser.add_argument("--actor", default="", help="Who requested this (operator id / ticket).")
        parser.add_argument("--correlation-id", default="", help="Optional request/command correlation id.")
        parser.add_argument("--apply", action="store_true", help="Persist. Omit for a dry-run preview.")
        parser.add_argument("--json", action="store_true", help="Emit the plan as JSON.")

    def _emit(self, plan, *, applied_mode, as_json):
        if as_json:
            self.stdout.write(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
            return
        mode = "APPLY" if applied_mode else "DRY-RUN"
        self.stdout.write(f"set_node_capacity [{mode}] node={plan.node_hostname or '(unresolved)'} "
                          f"(id={plan.node_id}) host={plan.physical_host_key or '(none)'}")
        self.stdout.write(f"  max_accounts: {plan.old_max} -> {plan.proposed_max}")
        self.stdout.write(f"  node_occupancy={plan.node_occupancy}  host_occupancy={plan.host_occupancy}  "
                          f"other_node_occupancy={plan.other_node_occupancy}")
        self.stdout.write(f"  certified_safe={plan.certified_safe}  ceiling={plan.ceiling}")
        self.stdout.write(f"  remaining_certified_slots: before={plan.remaining_safe_before} "
                          f"after={plan.remaining_safe_after}")
        verdict = ("WOULD APPLY" if (plan.ok and not applied_mode)
                   else "APPLIED" if plan.applied
                   else "REFUSED")
        style = self.style.SUCCESS if (plan.ok or plan.applied) else self.style.WARNING
        self.stdout.write(style(f"  verdict={verdict}  reason={plan.reason}"))
        if plan.applied:
            self.stdout.write(self.style.SUCCESS(f"  audit_id={plan.audit_id} written."))

    def handle(self, *args, **o):
        node_hostname = o.get("node_hostname")
        node_id = o.get("node_id")
        proposed_max = o["max_accounts"]
        as_json = o["json"]

        if not o["apply"]:
            plan = evaluate_node_capacity_change(
                node_hostname=node_hostname, node_id=node_id, proposed_max=proposed_max,
            )
            self._emit(plan, applied_mode=False, as_json=as_json)
            if not as_json:
                self.stdout.write(self.style.WARNING(
                    "DRY-RUN: nothing written. Re-run with --apply --reason \"...\" to persist."))
            return

        if not (o["reason"] or "").strip():
            raise CommandError("--reason is mandatory with --apply (recorded in the append-only audit row).")

        plan = apply_node_capacity_change(
            node_hostname=node_hostname, node_id=node_id, proposed_max=proposed_max,
            actor=o["actor"], reason=o["reason"], correlation_id=o["correlation_id"],
        )
        self._emit(plan, applied_mode=True, as_json=as_json)
        if not plan.applied:
            # A refused --apply is NOT a success: exit non-zero (CommandError → exit 1) so operators and
            # automation see the failure, matching the set_host_capacity precedent. No write occurred; the plan
            # (incl. --json) was already emitted to stdout above.
            raise CommandError(f"REFUSED (no change): {plan.reason}")
