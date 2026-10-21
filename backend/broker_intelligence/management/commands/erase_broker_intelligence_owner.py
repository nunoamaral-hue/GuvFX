"""Controlled owner-scoped ERASURE CLI (ADR-0054; GDPR/UK-GDPR Art 17). Staff/governance only; DARK by default.

This is deliberately NOT an HTTP endpoint. It is the ONLY operator surface for governed erasure and it fails
closed: a dry-run PLAN must be produced first, and a real mutation needs the matching confirmation token, a
verified ErasureRequest, and the BROKER_INTELLIGENCE_ERASURE_ENABLED flag ON.

Typical flow:
  1) create a request:   manage.py erase_broker_intelligence_owner --owner 42 --create --reason "art17 request" --identity-verified
  2) dry-run plan:       manage.py erase_broker_intelligence_owner --request 7
  3) execute:            manage.py erase_broker_intelligence_owner --request 7 --execute --confirm <TOKEN-from-step-2>

``--owner N`` alone (no --create/--request) prints a non-persisted plan preview. NEVER run with --execute against
production data without explicit Sponsor authorisation for that exact operation.
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ... import flags
from ...erasure import ErasureRefused, execute_owner_erasure, plan_owner_erasure
from ...models import ErasureRequest


class Command(BaseCommand):
    help = "Controlled, owner-scoped erasure of Withdrawal-Intelligence personal data (staff/governance; DARK)."

    def add_arguments(self, parser):
        parser.add_argument("--owner", type=int, help="subject owner (User) id")
        parser.add_argument("--request", type=int, help="an existing ErasureRequest id")
        parser.add_argument("--create", action="store_true", help="create a new ErasureRequest for --owner")
        parser.add_argument("--reason", default="", help="reason category (never message content)")
        parser.add_argument("--identity-verified", action="store_true",
                            help="attest the data subject's identity was verified out-of-band")
        parser.add_argument("--operator", type=int, help="the staff User id performing this (recorded as requested_by)")
        parser.add_argument("--execute", action="store_true", help="perform the erasure (default is dry-run/plan)")
        parser.add_argument("--confirm", default="", help="confirmation token from the dry-run plan (required with --execute)")

    def handle(self, *args, **opts):
        User = get_user_model()

        # --- create a request -------------------------------------------------
        if opts["create"]:
            if not opts["owner"]:
                raise CommandError("--create requires --owner")
            if not User.objects.filter(pk=opts["owner"]).exists():
                raise CommandError(f"no such user id={opts['owner']}")
            operator = User.objects.filter(pk=opts["operator"]).first() if opts.get("operator") else None
            req = ErasureRequest.objects.create(
                subject_user_id=opts["owner"], subject_user_id_snapshot=opts["owner"],
                requested_by=operator, reason=opts["reason"][:200],
                identity_verified=bool(opts["identity_verified"]),
                status=ErasureRequest.Status.REQUESTED)
            self.stdout.write(self.style.SUCCESS(
                f"ErasureRequest #{req.id} created for owner={opts['owner']} "
                f"(identity_verified={req.identity_verified}). Next: run with --request {req.id} to produce a plan."))
            return

        # --- resolve the subject / request -----------------------------------
        req = None
        if opts["request"]:
            req = ErasureRequest.objects.filter(pk=opts["request"]).first()
            if req is None:
                raise CommandError(f"no such ErasureRequest id={opts['request']}")
            owner_id = req.subject_user_id_snapshot
        elif opts["owner"]:
            owner_id = opts["owner"]
        else:
            raise CommandError("provide --owner (preview) or --request (planned/execute), optionally with --create")

        # --- EXECUTE ----------------------------------------------------------
        if opts["execute"]:
            if req is None:
                raise CommandError("--execute requires a persisted --request (create one with --create first)")
            if not opts["confirm"]:
                raise CommandError("--execute requires --confirm <TOKEN> (the token printed by the dry-run plan)")
            if not flags.broker_intelligence_erasure_enabled():
                raise CommandError("REFUSED: BROKER_INTELLIGENCE_ERASURE_ENABLED is off (DARK). Nothing was changed.")
            try:
                outcome = execute_owner_erasure(req, opts["confirm"])
            except ErasureRefused as exc:
                raise CommandError(f"REFUSED: {exc}. Nothing was changed.")
            self.stdout.write(self.style.SUCCESS(
                f"ERASURE EXECUTED for owner={owner_id} (request #{req.id}):\n{json.dumps(outcome, indent=2)}"))
            return

        # --- DRY-RUN PLAN (default; no mutation) ------------------------------
        plan = plan_owner_erasure(owner_id)
        if req is not None:
            req.plan = plan
            req.confirm_token = plan["token"]
            req.status = ErasureRequest.Status.PLANNED
            req.planned_at = timezone.now()
            req.save(update_fields=["plan", "confirm_token", "status", "planned_at"])
        self.stdout.write("=== DRY-RUN PLAN (no data changed) ===")
        self.stdout.write(json.dumps(plan, indent=2))
        self.stdout.write(self.style.WARNING(
            f"\nTo erase, re-run with:  --request {req.id if req else '<create one>'} --execute --confirm {plan['token']}"))
        self.stdout.write("Immutability remains fully in force until a governed --execute with a matching token.")
