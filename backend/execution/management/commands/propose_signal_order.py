"""
EXEC-E1a — operator command: create a ProposedSignalOrder from an APPROVED signal.

Given an APPROVED ``signal_intake.PendingSignalApproval`` and a DEMO
``TradingAccount``, create one non-executable ``ProposedSignalOrder`` via the
bridge. Creates NO ``ExecutionJob``, places NO order, contacts NO broker, and
activates NO Telegram listener. This is the deliberate, human-driven entry
point for E1a — there is no automatic approval→proposal trigger.

Usage:
    python manage.py propose_signal_order --approval <id> --account <id> \\
        [--lot 0.01] [--actor <username|email>] [--notes "..."]
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from execution.models import ExecutionJob, ProposedSignalOrder
from execution.signal_proposals import ProposalRejected, propose_order_from_approval
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount

User = get_user_model()


class Command(BaseCommand):
    help = (
        "EXEC-E1a: create a ProposedSignalOrder from an APPROVED signal on a "
        "demo account (no order placed, no ExecutionJob created)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--approval", type=int, required=True, help="PendingSignalApproval id.")
        parser.add_argument("--account", type=int, required=True, help="Demo TradingAccount id.")
        parser.add_argument("--lot", type=str, default=None, help="Lot size (default demo fixed lot).")
        parser.add_argument("--actor", help="Existing username/email to attribute to.")
        parser.add_argument("--notes", default="", help="Free-text notes on the proposal.")

    def _actor(self, identifier):
        if not identifier:
            return None
        return (
            User.objects.filter(username=identifier).first()
            or User.objects.filter(email=identifier).first()
        )

    def handle(self, *args, **opts):
        jobs_before = ExecutionJob.objects.count()

        try:
            approval = PendingSignalApproval.objects.get(id=opts["approval"])
        except PendingSignalApproval.DoesNotExist:
            raise CommandError(f"PendingSignalApproval #{opts['approval']} not found")
        try:
            account = TradingAccount.objects.get(id=opts["account"])
        except TradingAccount.DoesNotExist:
            raise CommandError(f"TradingAccount #{opts['account']} not found")

        try:
            proposal = propose_order_from_approval(
                approval,
                account=account,
                actor=self._actor(opts.get("actor")),
                lot_size=opts.get("lot"),
                notes=opts.get("notes", ""),
            )
        except ProposalRejected as exc:
            self.stdout.write(self.style.WARNING(
                f"REJECTED [{exc.code}]: {exc.message} (a PROPOSAL_REJECTED audit row was written)"
            ))
            jobs_after = ExecutionJob.objects.count()
            self.stdout.write(self.style.SUCCESS(
                f"0 orders placed; {jobs_after - jobs_before} ExecutionJobs created."
            ))
            raise CommandError("proposal rejected") from exc

        jobs_after = ExecutionJob.objects.count()
        self.stdout.write(self.style.SUCCESS(
            "\nProposedSignalOrder created (NO ORDER PLACED):"
        ))
        self.stdout.write(
            f"  + PROPOSAL #{proposal.id} {proposal.direction} {proposal.symbol} "
            f"lot={proposal.lot_size} demo={proposal.is_demo} "
            f"(entry {proposal.entry}, SL {proposal.stop_loss}, TP {proposal.take_profit}) "
            f"from approval #{approval.id} onto account #{account.id}"
        )
        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS(
            f"Done — 1 proposal; {jobs_after - jobs_before} ExecutionJobs created; 0 orders placed."
        ))
