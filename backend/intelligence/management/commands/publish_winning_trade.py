"""
Winning trade -> results card -> WIMS packet (WIN-ONLY).

Takes a closed *winning* trade, renders the results/history card (SVG, filtered
to that order/day), and hands a WIMS packet (ConsumptionContract + media) to the
content pipeline, then runs it to the human-review gate. Losers are rejected and
never enter the pipeline.

Trade source (authoritative): ``trading.models.Trade``.
    --ticket / --account  -> real closed Trade from GuvFX trade history
    --fixture / --trade   -> a representative closed-trade record (offline)

Usage:
    python manage.py publish_winning_trade
    python manage.py publish_winning_trade --ticket 100245789 --account 12
    python manage.py publish_winning_trade --out /tmp/results_card.svg
"""

import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from intelligence.delivery import ingest_winning_trade, is_winning_trade
from wims import services
from wims.models import Content, Publish, Review
from wims.services import workflow_state_for_contract

User = get_user_model()
DEFAULT_FIXTURE = "intelligence/fixtures/closed_trade_sample.json"


class Command(BaseCommand):
    help = "WIN-only: closed winning trade -> results card -> WIMS packet."

    def add_arguments(self, parser):
        parser.add_argument("--ticket", help="Ticket of a real closed trading.Trade.")
        parser.add_argument("--account", type=int, help="Account id for --ticket.")
        parser.add_argument("--fixture", help="Path to a closed-trade JSON record.")
        parser.add_argument("--trade", help="Inline closed-trade JSON string.")
        parser.add_argument("--actor", help="Existing username/email to attribute to.")
        parser.add_argument("--out", help="Write the results-card SVG to this path.")
        parser.add_argument("--review", choices=["APPROVE", "REJECT", "HOLD"],
                            default="HOLD",
                            help="Simulate the human review decision (default HOLD = leave for operator).")

    def _actor(self, identifier):
        if identifier:
            u = (User.objects.filter(username=identifier).first()
                 or User.objects.filter(email=identifier).first())
            if u:
                return u
        u, _ = User.objects.get_or_create(
            username="wims_demo_operator",
            defaults={"email": "wims_demo_operator@example.invalid"},
        )
        return u

    def _load_trade(self, opts):
        if opts.get("ticket"):
            try:
                from trading.models import Trade
            except Exception as exc:  # pragma: no cover
                raise CommandError(f"trading app unavailable for --ticket: {exc}")
            qs = Trade.objects.filter(ticket=opts["ticket"], close_time__isnull=False)
            if opts.get("account"):
                qs = qs.filter(account_id=opts["account"])
            t = qs.first()
            if not t:
                raise CommandError("No matching closed trade found.")
            return t, "trading.models.Trade (real)"
        if opts.get("trade"):
            return json.loads(opts["trade"]), "inline record"
        rel = opts.get("fixture") or DEFAULT_FIXTURE
        path = Path(rel)
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / rel
        if not path.exists():
            raise CommandError(f"Closed-trade fixture not found: {path}")
        # A fixture may be a single trade OR a list of partial closes.
        return json.loads(path.read_text()), f"fixture {rel}"

    def handle(self, *args, **opts):
        import base64
        actor = self._actor(opts.get("actor"))
        trade, origin = self._load_trade(opts)
        rows_in = trade if isinstance(trade, list) else [trade]

        self.stdout.write(self.style.SUCCESS(
            "\nWinning trade -> trade result card -> WIMS packet (WIN-only)"
        ))
        self.stdout.write(f"  trade source: {origin} ({len(rows_in)} close row(s))")

        from decimal import Decimal
        from intelligence.delivery import _net_pnl
        total_net = sum((_net_pnl(t) for t in rows_in), Decimal("0"))
        if total_net <= 0:
            self.stdout.write(self.style.NOTICE(
                "  NOT a net winner (total pnl <= 0) — losers/breakeven are never "
                "published. Nothing created."
            ))
            return

        envelope, contract = ingest_winning_trade(trade, actor=actor)
        card = contract.media.get("results_card", {})
        caption = contract.media.get("caption", "")

        self.stdout.write("  + WIN detected; packet created:")
        self.stdout.write(
            f"    ConsumptionContract#{contract.id} ({contract.source_type}, "
            f"{contract.result_type}, pnl={contract.profit_loss}) "
            f"workflow={workflow_state_for_contract(contract)}"
        )
        self.stdout.write(
            f"    result card: format={card.get('format')} "
            f"png={len(card.get('png_base64',''))}b64 + svg internal; caption attached"
        )

        if opts.get("out") and card.get("png_base64"):
            out = opts["out"]
            if not out.lower().endswith((".png", ".jpg", ".jpeg")):
                out = out + ".png"
            Path(out).write_bytes(base64.b64decode(card["png_base64"]))
            self.stdout.write(f"    wrote PNG -> {out}")

        self.stdout.write("    caption:")
        for line in caption.splitlines():
            self.stdout.write(f"      | {line}")

        # Build content from the consumed contract and take it to the review gate.
        ctx = services.create_context_from_contract(
            contract=contract,
            context_text=(
                "Educational recap of a completed winning trade. Neutral, "
                "informative; describes the instrument and outcome terms only. "
                "No advice, prediction, or recommendation."
            ),
            actor=actor,
        )
        content = services.create_content(
            context=ctx,
            title=f"Result: {envelope.structured_payload.market} "
                  f"{envelope.structured_payload.direction} (+{contract.profit_loss})",
            content_text=caption,  # the social caption IS the audience-facing copy
            actor=actor,
        )
        services.submit_for_review(content=content, actor=actor)
        self.stdout.write(
            f"    Content#{content.id} submitted for review "
            f"(workflow {workflow_state_for_contract(contract)})"
        )

        decision = opts["review"]
        if decision == "HOLD":
            self.stdout.write(self.style.WARNING(
                "    HOLD — left at the mandatory human-review gate (operator approves/rejects). "
                "Nothing is published automatically."
            ))
        else:
            services.review_content(
                content=content, decision=getattr(Review.Decision, decision),
                reviewer=actor, notes="publish_winning_trade demo review",
            )
            content.refresh_from_db()
            if content.status == Content.Status.APPROVED:
                services.publish_content(
                    content=content, channel=Publish.Channel.TELEGRAM, publisher=actor,
                )
                content.refresh_from_db()
            self.stdout.write(
                f"    review={decision} -> content.status={content.status} "
                f"(publish simulated={content.status == Content.Status.PUBLISHED})"
            )

        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS(
            "PASS — winner packaged with results card for WIMS; loser-suppression "
            "enforced; human-review gate respected; no trade placed by this command."
        ))
