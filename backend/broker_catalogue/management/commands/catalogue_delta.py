"""ADR-0102 (Lane C) — governed operator command for the catalogue servers.dat delta workflow.

Thin dispatcher over ``broker_catalogue.delta_governance`` (the tested core). Actions:

    inspect   — show the current ACTIVE catalogue version + manifest (read-only)
    propose   — preview (or, with --apply, create) a hash-pinned delta DRAFT from the current ACTIVE to a DRAFT
                version (--proposed-label); fail-closed on stale base / duplicate / ambiguous / ownership / no-op
    validate  — dry-run re-validate a delta; with --apply mark it REVIEWED
    approve   — (with --apply) APPROVE a REVIEWED delta (requires --actor --ticket --reason). Separate from activation.
    stage     — (with --apply) STAGE an APPROVED delta: emit the deterministic deployment-artifact descriptor
    activate  — (with --apply) ACTIVATE a STAGED delta: CAS hash-pin re-check, then the proven activation gates
    reject    — (with --apply) REJECT a pre-activation delta
    rollback  — (with --apply) ROLL BACK an ACTIVATED delta to its base version
    audit     — show the append-only transition history of a delta (read-only)

All MUTATING actions are DRY-RUN by default; pass --apply to execute. Output is redacted/deterministic (identities,
SHA-256 hashes, enums, counts — never servers.dat bytes, credentials, account numbers or host paths beyond the
non-secret relpath). This command NEVER writes a host / servers.dat, contacts a broker, or restarts a terminal —
it is the reviewable repository mechanism; the actual host staging is a separate Sponsor-gated step.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Governed catalogue servers.dat delta workflow (propose/validate/approve/stage/activate/reject/rollback)."

    def add_arguments(self, parser):
        parser.add_argument("delta_action", choices=[
            "inspect", "propose", "validate", "approve", "stage", "activate", "reject", "rollback", "audit"])
        parser.add_argument("--delta-id", type=int, default=0)
        parser.add_argument("--proposed-label", default="", help="propose: label of the DRAFT version to propose.")
        parser.add_argument("--actor", default="", help="Staff actor label, e.g. 'user:3' (required for mutations).")
        parser.add_argument("--ticket", default="")
        parser.add_argument("--reason", default="")
        parser.add_argument("--expected-base-manifest", dest="expected_base_manifest", default="",
                            help="propose: optional hash-pin — the manifest you expect the current ACTIVE version to "
                                 "carry; a mismatch fails closed (stale base).")
        parser.add_argument("--apply", action="store_true", help="Execute the mutation (default: dry-run).")

    def handle(self, *args, **opts):
        from ... import delta_governance as dg   # broker_catalogue.delta_governance

        action = opts["delta_action"]

        def emit(obj):
            self.stdout.write(json.dumps(obj, indent=2, sort_keys=True, default=str))

        def resolve_delta():
            from broker_catalogue.models import CatalogueDelta
            if not opts["delta_id"]:
                raise CommandError("this action requires --delta-id")
            d = CatalogueDelta.objects.filter(pk=opts["delta_id"]).first()
            if d is None:
                raise CommandError(f"no delta #{opts['delta_id']}")
            return d

        try:
            if action == "inspect":
                return emit({"current": dg.current_state()})

            if action == "audit":
                d = resolve_delta()
                return emit({"delta": d.pk, "state": d.state, "history": dg.audit_history(d)})

            if action == "propose":
                from broker_catalogue import service as svc
                from broker_catalogue.models import CatalogueVersion
                if not opts["proposed_label"]:
                    raise CommandError("propose requires --proposed-label")
                if not opts["apply"]:
                    # DRY-RUN preview: resolve base + proposed, compute the diff + structural validation; create nothing.
                    proposed = CatalogueVersion.objects.filter(label=opts["proposed_label"]).first()
                    if proposed is None:
                        raise CommandError(f"no catalogue version {opts['proposed_label']!r}")
                    base = svc.resolve_active_version()
                    added, removed, changed = dg.compute_diff(base, proposed)
                    return emit({"dry_run": True, "would_propose": {
                        "base_version": (base.label if base else None),
                        "base_manifest_sha256": (svc.norm_sha(base.manifest_sha256) if base else ""),
                        "proposed_version": proposed.label,
                        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)},
                        "added": added, "removed": removed, "changed": changed,
                        "structural_problems": dg.structural_problems(proposed),
                    }, "note": "pass --apply to create the delta"})
                d = dg.propose_delta(proposed_label=opts["proposed_label"], actor=opts["actor"], ticket=opts["ticket"],
                                     reason=opts["reason"],
                                     expected_base_manifest=(opts["expected_base_manifest"] or None))
                return emit({"proposed": dg.delta_report(d)})

            if action == "validate":
                d = resolve_delta()
                return emit(dg.validate_delta(d, actor=opts["actor"] or "validate", persist=bool(opts["apply"])))

            # approve / stage / activate / reject / rollback — mutating; dry-run shows the report + intended transition.
            d = resolve_delta()
            if not opts["apply"]:
                return emit({"dry_run": True, "delta": dg.delta_report(d),
                             "intended_action": action, "note": "pass --apply to execute"})

            if action == "approve":
                d = dg.approve_delta(d, actor=opts["actor"], ticket=opts["ticket"], reason=opts["reason"])
                return emit({"approved": dg.delta_report(d)})
            if action == "stage":
                descriptor = dg.stage_delta(d, actor=opts["actor"])
                return emit({"staged": descriptor})
            if action == "activate":
                d = dg.activate_delta(d, actor=opts["actor"])
                return emit({"activated": dg.delta_report(d), "current": dg.current_state()})
            if action == "reject":
                d = dg.reject_delta(d, actor=opts["actor"], reason=opts["reason"])
                return emit({"rejected": dg.delta_report(d)})
            if action == "rollback":
                d = dg.rollback_delta(d, actor=opts["actor"], ticket=opts["ticket"], reason=opts["reason"])
                return emit({"rolled_back": dg.delta_report(d), "current": dg.current_state()})
        except Exception as exc:  # noqa: BLE001 — surface a sanitised message, never a stack/PII
            raise CommandError(str(exc))
