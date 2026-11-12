"""ADR-0099 (Lane A) — governed operator command for the broker-fingerprint lifecycle.

Thin dispatcher over ``broker_intelligence.fingerprint_onboarding`` (the tested core). Actions:

    list        — list fingerprints (redacted summary)
    propose     — create a PROPOSED fingerprint from a JSON spec file (--spec-file)
    validate    — dry-run a fingerprint over its SYNTHETIC examples (marks VALIDATED); prints a redacted report
    certify     — certify a VALIDATED fingerprint (requires --actor --reason --ticket; fail-closed on every gate)
    activate    — activate a CERTIFIED fingerprint
    deactivate  — deactivate a fingerprint
    rollback    — roll a broker back to a prior certified version (--target-version)
    delta       — redacted structural delta of the active version over a JSON example set (--spec-file)
    seed        — idempotently seed the Stream-B brokers as certified fingerprints (checksums printed)

This command NEVER scans a mailbox, advances a cursor, moves money, contacts a broker, publishes statistics, or
touches Copy Trading. It is DARK: a seeded/active fingerprint is consulted by the detector only on the generic path
(after a specific-parser miss) and only for a canary owner. No raw email/PII is printed (only enums/counts/hashes).
It is the reviewable mechanism, not an authorisation to run it against production.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Governed broker-fingerprint lifecycle (propose/validate/certify/activate/deactivate/rollback/delta/seed)."

    def add_arguments(self, parser):
        parser.add_argument("fp_action", choices=[
            "list", "propose", "validate", "certify", "activate", "deactivate", "rollback", "delta", "seed"])
        parser.add_argument("--name", default="")
        parser.add_argument("--version", type=int, default=0)
        parser.add_argument("--actor", default="", help="Staff actor label, e.g. 'user:3' (required for mutations).")
        parser.add_argument("--reason", default="")
        parser.add_argument("--ticket", default="")
        parser.add_argument("--target-version", type=int, default=0, help="rollback: prior certified version to restore.")
        parser.add_argument("--broker-profile-id", type=int, default=0)
        parser.add_argument("--spec-file", default="", help="propose/delta: path to a JSON spec (fields/examples).")
        parser.add_argument("--no-activate", action="store_true", help="seed: certify but do NOT activate.")

    def handle(self, *args, **opts):
        from ...models import BrokerFingerprint, BrokerProfile
        from ... import fingerprint_onboarding as onb
        from ... import fingerprint_seed as seed

        action = opts["fp_action"]

        def emit(obj):
            self.stdout.write(json.dumps(obj, indent=2, sort_keys=True, default=str))

        def resolve_fp():
            if not opts["name"] or not opts["version"]:
                raise CommandError("this action requires --name and --version")
            fp = BrokerFingerprint.objects.filter(name=opts["name"], version=opts["version"]).first()
            if fp is None:
                raise CommandError(f"no fingerprint {opts['name']} v{opts['version']}")
            return fp

        try:
            if action == "list":
                rows = [{"name": f.name, "version": f.version, "status": f.governance_status, "active": f.is_active,
                         "certified": f.certified_capability, "source_hash": f.source_hash[:12],
                         "domains": f.domains()} for f in BrokerFingerprint.objects.all()]
                return emit({"count": len(rows), "fingerprints": rows})

            if action == "seed":
                return emit({"seeded": seed.seed_fingerprints(
                    actor=opts["actor"] or "seed", activate=not opts["no_activate"])})

            if action == "propose":
                if not opts["spec_file"]:
                    raise CommandError("propose requires --spec-file (JSON)")
                with open(opts["spec_file"], "r", encoding="utf-8") as fh:
                    spec = json.load(fh)
                profile = None
                if opts["broker_profile_id"]:
                    profile = BrokerProfile.objects.filter(pk=opts["broker_profile_id"]).first()
                fp = onb.propose_fingerprint(
                    name=spec["name"], broker_display_name=spec["broker_display_name"],
                    actor=opts["actor"], broker_profile=profile, reason=opts["reason"], ticket=opts["ticket"],
                    positive_examples=spec.get("positive_examples", []),
                    negative_examples=spec.get("negative_examples", []), **spec.get("config", {}))
                return emit({"proposed": {"name": fp.name, "version": fp.version, "source_hash": fp.source_hash}})

            if action == "validate":
                return emit(onb.validate_fingerprint(resolve_fp(), actor=opts["actor"] or "validate"))

            if action == "certify":
                profile = BrokerProfile.objects.filter(pk=opts["broker_profile_id"]).first() if opts["broker_profile_id"] else None
                fp = onb.certify_fingerprint(resolve_fp(), actor=opts["actor"], reason=opts["reason"],
                                             ticket=opts["ticket"], broker_profile=profile)
                return emit({"certified": {"name": fp.name, "version": fp.version, "status": fp.governance_status,
                                           "source_hash": fp.source_hash}})

            if action == "activate":
                fp = onb.activate_fingerprint(resolve_fp(), actor=opts["actor"], reason=opts["reason"])
                return emit({"activated": {"name": fp.name, "version": fp.version, "active": fp.is_active}})

            if action == "deactivate":
                fp = onb.deactivate_fingerprint(resolve_fp(), actor=opts["actor"], reason=opts["reason"])
                return emit({"deactivated": {"name": fp.name, "version": fp.version, "status": fp.governance_status}})

            if action == "rollback":
                if not opts["name"] or not opts["target_version"]:
                    raise CommandError("rollback requires --name and --target-version")
                fp = onb.rollback_to_version(opts["name"], opts["target_version"], actor=opts["actor"],
                                             reason=opts["reason"], ticket=opts["ticket"])
                return emit({"rolled_back_to": {"name": fp.name, "version": fp.version, "active": fp.is_active}})

            if action == "delta":
                if not opts["spec_file"]:
                    raise CommandError("delta requires --spec-file (JSON example set)")
                with open(opts["spec_file"], "r", encoding="utf-8") as fh:
                    examples = json.load(fh).get("examples", [])
                return emit(onb.delta_report(opts["name"], examples=examples))
        except Exception as exc:  # noqa: BLE001 — surface a sanitised message, never a stack/PII
            raise CommandError(str(exc))
