"""Controlled owner-scoped ERASURE CLI (ADR-0054; GDPR/UK-GDPR Art 17). Staff/governance only; DARK by default.

This is deliberately NOT an HTTP endpoint. It is the ONLY operator surface for governed erasure and it fails
closed. A real mutation needs ALL of: the flag ON, a verified identity, an independent two-person approval
(approver != requester), the matching confirmation token, and the fully-provisioned privileged DB backend.

The destructive backend connects as the separate ``guvfx_erasure_exec`` role whose credential MUST NOT be in the
web/worker runtime environment (configure it as its own DATABASES alias injected only into this governed job) —
see ops/erasure/provision_erasure_privilege.sql. In THIS build the backend is unprovisioned and governed deletion
is gated, so --execute always refuses; nothing is deleted.

Two-person lifecycle (each a SEPARATE invocation — no single run can create + verify + approve + execute):
  1) create:   manage.py erase_broker_intelligence_owner --owner 42 --create --reason "art17" --operator <REQUESTER>
  2) verify:   manage.py erase_broker_intelligence_owner --request 7 --verify --verifier <VERIFIER> --method signed-request
  3) plan:     manage.py erase_broker_intelligence_owner --request 7
  4) approve:  manage.py erase_broker_intelligence_owner --request 7 --approve --approver <APPROVER!=REQUESTER>
  5) execute:  manage.py erase_broker_intelligence_owner --request 7 --execute --executor <EXECUTOR> --confirm <TOKEN>

``--owner N`` alone (no --create/--request) prints a non-persisted plan preview. NEVER run with --execute against
production data without explicit Sponsor authorisation for that exact operation.
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ... import flags
from ...erasure import (
    ErasureRefused, approve_request, execute_owner_erasure, plan_owner_erasure, verify_identity,
)
from ...models import ErasureRequest


class Command(BaseCommand):
    help = "Controlled, owner-scoped erasure of Withdrawal-Intelligence personal data (staff/governance; DARK)."

    def add_arguments(self, parser):
        parser.add_argument("--owner", type=int, help="subject owner (User) id")
        parser.add_argument("--request", type=int, help="an existing ErasureRequest id")
        parser.add_argument("--create", action="store_true", help="create a new ErasureRequest for --owner")
        parser.add_argument("--reason", default="", help="reason category (never message content)")
        parser.add_argument("--operator", type=int, help="the requester staff User id (recorded as requested_by)")
        parser.add_argument("--verify", action="store_true", help="record out-of-band identity verification")
        parser.add_argument("--verifier", type=int, help="the staff User id who verified identity")
        parser.add_argument("--method", default="", help="identity-verification method category (never ID documents)")
        parser.add_argument("--approve", action="store_true", help="independently approve (two-person; != requester)")
        parser.add_argument("--approver", type=int, help="the staff User id approving (must differ from requester)")
        parser.add_argument("--execute", action="store_true", help="perform the erasure (default is dry-run/plan)")
        parser.add_argument("--executor", type=int, help="the staff User id executing (recorded as executed_by)")
        parser.add_argument("--confirm", default="", help="confirmation token from the dry-run plan (with --execute)")

    def _user_or_error(self, User, uid, label):
        u = User.objects.filter(pk=uid).first()
        if u is None:
            raise CommandError(f"no such {label} user id={uid}")
        return u

    def handle(self, *args, **opts):
        User = get_user_model()

        # --- create a request -------------------------------------------------
        if opts["create"]:
            if not opts["owner"]:
                raise CommandError("--create requires --owner")
            if not opts.get("operator"):
                # The requester MUST be recorded or the two-person rule cannot be enforced at approval (round-3
                # adversarial HIGH): a NULL requester would make the approver!=requester check vacuous.
                raise CommandError("--create requires --operator <requester staff id> (two-person separation of duties)")
            if not User.objects.filter(pk=opts["owner"]).exists():
                raise CommandError(f"no such user id={opts['owner']}")
            operator = self._user_or_error(User, opts["operator"], "operator")
            req = ErasureRequest.objects.create(
                subject_user_id=opts["owner"], subject_user_id_snapshot=opts["owner"],
                requested_by=operator, reason=opts["reason"][:200],
                status=ErasureRequest.Status.REQUESTED)
            self.stdout.write(self.style.SUCCESS(
                f"ErasureRequest #{req.id} created for owner={opts['owner']}. "
                f"Next: --request {req.id} --verify --verifier <id> --method <category>."))
            return

        # --- resolve the request ---------------------------------------------
        req = None
        if opts["request"]:
            req = ErasureRequest.objects.filter(pk=opts["request"]).first()
            if req is None:
                raise CommandError(f"no such ErasureRequest id={opts['request']}")
            owner_id = req.subject_user_id_snapshot
        elif opts["owner"]:
            owner_id = opts["owner"]
        else:
            raise CommandError("provide --owner (preview) or --request (lifecycle), optionally with --create")

        # --- VERIFY identity --------------------------------------------------
        if opts["verify"]:
            if req is None:
                raise CommandError("--verify requires a persisted --request")
            if not opts.get("verifier"):
                raise CommandError("--verify requires --verifier <staff id>")
            verifier = self._user_or_error(User, opts["verifier"], "verifier")
            verify_identity(req, verifier, opts["method"])
            self.stdout.write(self.style.SUCCESS(
                f"Request #{req.id}: identity verified by user={verifier.pk} (method={opts['method'] or 'n/a'}). "
                f"Next: --request {req.id} to produce a plan."))
            return

        # --- APPROVE (two-person) --------------------------------------------
        if opts["approve"]:
            if req is None:
                raise CommandError("--approve requires a persisted --request")
            if not opts.get("approver"):
                raise CommandError("--approve requires --approver <staff id>")
            approver = self._user_or_error(User, opts["approver"], "approver")
            try:
                approve_request(req, approver)
            except ErasureRefused as exc:
                raise CommandError(f"REFUSED: {exc}. Nothing was changed.")
            self.stdout.write(self.style.SUCCESS(
                f"Request #{req.id}: APPROVED by user={approver.pk} (two-person). "
                f"Next: --request {req.id} --execute --executor <id> --confirm <TOKEN>."))
            return

        # --- EXECUTE ----------------------------------------------------------
        if opts["execute"]:
            if req is None:
                raise CommandError("--execute requires a persisted --request")
            if not opts["confirm"]:
                raise CommandError("--execute requires --confirm <TOKEN> (the token printed by the dry-run plan)")
            if not flags.broker_intelligence_erasure_enabled():
                raise CommandError("REFUSED: BROKER_INTELLIGENCE_ERASURE_ENABLED is off (DARK). Nothing was changed.")
            executor = self._user_or_error(User, opts["executor"], "executor") if opts.get("executor") else None
            try:
                outcome = execute_owner_erasure(req, opts["confirm"], executor=executor)
            except ErasureRefused as exc:
                raise CommandError(f"REFUSED: {exc}. Nothing was changed.")
            self.stdout.write(self.style.SUCCESS(
                f"ERASURE EXECUTED for owner={owner_id} (request #{req.id}):\n{json.dumps(outcome, indent=2)}"))
            return

        # --- DRY-RUN PLAN (default; no mutation) ------------------------------
        if req is not None and not req.identity_verified:
            raise CommandError("REFUSED: verify identity first (--verify --verifier <id> --method <category>).")
        plan = plan_owner_erasure(owner_id)
        if req is not None:
            req.plan = plan
            req.confirm_token = plan["token"]
            req.status = ErasureRequest.Status.PLANNED
            req.planned_at = timezone.now()
            req.save(update_fields=["plan", "confirm_token", "status", "planned_at"])
        self.stdout.write("=== DRY-RUN PLAN (no data changed) ===")
        self.stdout.write(json.dumps(plan, indent=2))
        if req is not None:
            self.stdout.write(self.style.WARNING(
                f"\nNext: --request {req.id} --approve --approver <id!=requester>, then "
                f"--request {req.id} --execute --executor <id> --confirm {plan['token']}"))
        self.stdout.write("Immutability remains fully in force until a governed --execute with a matching token.")
