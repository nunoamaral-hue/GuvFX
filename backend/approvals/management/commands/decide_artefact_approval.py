"""OPERATOR-ONLY: approve or reject a PENDING artefact approval. This is the human gate — run by an operator,
never by automation. Requires a STAFF decider identified by email. Example:
  decide_artefact_approval --id 1 --approve --by operator@example.com --reason "certified <=10s, sanitised"
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from approvals.models import ArtefactApproval
from approvals.services import decide


class Command(BaseCommand):
    help = "OPERATOR-ONLY human gate: approve/reject a PENDING artefact approval (requires a staff decider)."

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, required=True)
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument("--approve", action="store_true")
        g.add_argument("--reject", action="store_true")
        parser.add_argument("--by", required=True, help="email of the STAFF operator making the decision")
        parser.add_argument("--reason", default="")

    def handle(self, *args, **opts):
        U = get_user_model()
        decider = U.objects.filter(email__iexact=opts["by"]).first()
        if decider is None:
            raise CommandError(f"no user with email {opts['by']!r}")
        if not decider.is_staff:
            raise CommandError(f"{opts['by']!r} is not staff; refusing (human gate requires a staff decider)")
        row = ArtefactApproval.objects.filter(pk=opts["id"]).first()
        if row is None:
            raise CommandError(f"no ArtefactApproval id={opts['id']}")
        try:
            row = decide(row, approve=bool(opts["approve"]), decided_by=decider, reason=opts["reason"])
        except (ValueError, PermissionError) as exc:
            raise CommandError(str(exc))
        self.stdout.write(f"[artefact-approval] id={row.pk} -> {row.status} by={opts['by']}")
