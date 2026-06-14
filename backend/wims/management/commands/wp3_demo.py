"""
WP-3 MVP demonstration — Trade Result Flow.

Transforms an external, transient Trade Result into reviewed and published
educational content through the existing WIMS pipeline:

    Trade Result (external, NOT persisted)
      -> Consumption Contract -> Context -> Content -> Human Review -> Publish

The Trade Result arrives as a JSON payload (a fixture or ``--payload`` string).
It is NEVER persisted as its own object: WIMS persists only the
ConsumptionContract describing it (ADR-009). WIMS persists no Trade / Position /
Deal / Execution / MT5 / Broker object — asserted at the end of the run.

Usage:
    python manage.py wp3_demo
    python manage.py wp3_demo --fixture wims/fixtures/trade_result_eurusd_win.json
    python manage.py wp3_demo --payload '{"symbol": "GBPUSD", ...}'
    python manage.py wp3_demo --decision REJECT
"""

import json
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from wims import services
from wims.models import AuditEvent, ConsumptionContract, Content, Publish, Review
from wims.services import workflow_state_for_contract

User = get_user_model()

DEFAULT_FIXTURE = "wims/fixtures/trade_result_eurusd_win.json"

# Prohibited persisted types (ADR-009). WIMS must define none of these.
PROHIBITED = ("trade", "position", "deal", "execution", "mt5", "broker", "signal")


def _decimal(value):
    return None if value in (None, "") else Decimal(str(value))


class Command(BaseCommand):
    help = "Demonstrate the WP-3 Trade Result Flow end-to-end."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", help="Path to a trade-result JSON payload.")
        parser.add_argument("--payload", help="Inline trade-result JSON string.")
        parser.add_argument("--actor", help="Existing username/email to attribute the run to.")
        parser.add_argument(
            "--decision", choices=Review.Decision.values, default=Review.Decision.APPROVE
        )
        parser.add_argument(
            "--channel", choices=Publish.Channel.values, default=Publish.Channel.TELEGRAM
        )

    # -- helpers ------------------------------------------------------------
    def _section(self, n, title):
        self.stdout.write("=" * 64)
        self.stdout.write(self.style.MIGRATE_HEADING(f"{n}. {title}"))

    def _resolve_actor(self, identifier):
        if identifier:
            user = (
                User.objects.filter(username=identifier).first()
                or User.objects.filter(email=identifier).first()
            )
            if user:
                return user
            self.stderr.write(f"No user matches {identifier!r}; using demo operator.")
        user, _ = User.objects.get_or_create(
            username="wims_demo_operator",
            defaults={"email": "wims_demo_operator@example.invalid"},
        )
        return user

    def _load_payload(self, opts) -> dict:
        if opts.get("payload"):
            return json.loads(opts["payload"])
        rel = opts.get("fixture") or DEFAULT_FIXTURE
        path = Path(rel)
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / rel
        if not path.exists():
            raise CommandError(f"Trade-result fixture not found: {path}")
        return json.loads(path.read_text())

    def _context_text(self, p: dict) -> str:
        return (
            "This material is educational only and describes a completed trade "
            "outcome after the fact.\n"
            f"- Instrument: {p.get('symbol', '?')} is a currency pair.\n"
            "- 'Entry' and 'exit' are the prices at which a position was opened "
            "and later closed.\n"
            "- 'Pips' measure the change in price between entry and exit.\n"
            "- A result of WIN/LOSS/BREAKEVEN simply categorises the outcome; it "
            "does not indicate skill, repeatability, or future results.\n"
            "Nothing here recommends a trade, validates a provider, predicts an "
            "outcome, or gives financial advice."
        )

    def _content_text(self, p: dict) -> str:
        return (
            "Reading a completed trade as a learning example\n\n"
            f"A closed position on {p.get('symbol', 'a currency pair')} can be used "
            "to understand common terminology. The 'entry' is where a position "
            "opened and the 'exit' is where it closed; the distance between them, "
            "measured in 'pips', is one way people describe the move. Labelling an "
            "outcome a win, loss, or breakeven just categorises what happened.\n\n"
            "Past outcomes describe history only — they are not predictions, "
            "recommendations, or advice. This is general education about how trade "
            "results are described."
        )

    # -- entrypoint ---------------------------------------------------------
    def handle(self, *args, **opts):
        actor = self._resolve_actor(opts["actor"])
        decision, channel = opts["decision"], opts["channel"]
        payload = self._load_payload(opts)

        self.stdout.write(self.style.SUCCESS(
            "\nWP-3 — Trade Result Flow MVP demonstration"
        ))
        self.stdout.write("External Trade Result intelligence (transient, NOT persisted):")
        self.stdout.write("    " + json.dumps(payload))
        self.stdout.write(f"Operator: {actor} | decision: {decision} | channel: {channel}\n")

        # 1 — Consumption Contract (first persisted WIMS object) ----------
        contract = services.create_contract(
            actor=actor,
            source_type=ConsumptionContract.SourceType.TRADE_RESULT,
            source_reference=payload.get("source_reference", ""),
            symbol=payload.get("symbol", ""),
            direction=payload.get("direction", ""),
            entry_price=_decimal(payload.get("entry_price")),
            exit_price=_decimal(payload.get("exit_price")),
            result_type=payload.get("result_type", ""),
            profit_loss=_decimal(payload.get("profit_loss")),
            pips=_decimal(payload.get("pips")),
            close_time=parse_datetime(payload["close_time"]) if payload.get("close_time") else None,
            confidence=_decimal(payload.get("confidence")),
            commentary=payload.get("commentary", ""),
            tags=payload.get("tags", []),
            raw_signal=json.dumps(payload),
        )
        self._section(1, "CONTRACT RECORD (ConsumptionContract, source_type=TRADE_RESULT)")
        self.stdout.write(
            f"  id={contract.id}  {contract.direction} {contract.symbol}  "
            f"entry={contract.entry_price} exit={contract.exit_price} "
            f"result={contract.result_type} pl={contract.profit_loss} pips={contract.pips}"
        )
        self.stdout.write(
            f"  status={contract.status}  source={contract.source_type}  "
            f"workflow={workflow_state_for_contract(contract)}"
        )

        # 2 — Context -----------------------------------------------------
        ctx = services.create_context_from_contract(
            contract=contract, context_text=self._context_text(payload), actor=actor,
        )
        contract.refresh_from_db()
        self._section(2, "CONTEXT RECORD")
        self.stdout.write(f"  id={ctx.id}  contract_id={ctx.contract_id}  status={ctx.status}")
        self.stdout.write(f"  contract.status -> {contract.status} (processed)")

        # 3 — Content (reuses WP-1 model + flow, unchanged) ---------------
        content = services.create_content(
            context=ctx,
            title="Trade result terminology explained (educational)",
            content_text=self._content_text(payload),
            actor=actor,
        )
        self._section(3, "CONTENT RECORD")
        self.stdout.write(f"  id={content.id}  context_id={content.context_id}  status={content.status}")
        self.stdout.write(f"  title={content.title!r}")

        # 4 — Human review ------------------------------------------------
        services.submit_for_review(content=content, actor=actor)
        review = services.review_content(
            content=content, decision=decision, reviewer=actor,
            notes="Reviewed for the WP-3 MVP demonstration.",
        )
        content.refresh_from_db()
        self._section(4, "REVIEW RECORD (mandatory human review)")
        self.stdout.write(f"  id={review.id}  decision={review.review_decision}  reviewer={review.reviewer}")
        self.stdout.write(f"  content.status -> {content.status}")

        # 5 — Publish -----------------------------------------------------
        self._section(5, "PUBLISH RECORD (manual; channel simulated)")
        if content.status == Content.Status.APPROVED:
            pub = services.publish_content(content=content, channel=channel, publisher=actor)
            content.refresh_from_db()
            self.stdout.write(f"  id={pub.id}  channel={pub.channel}  simulated={pub.simulated}")
            self.stdout.write(f"  content.status -> {content.status}")
        else:
            self.stdout.write(self.style.NOTICE(
                "  Content REJECTED — publish correctly blocked (no publish record)."
            ))
        self.stdout.write(f"  contract workflow state -> {workflow_state_for_contract(contract)}")

        # 6 — Audit -------------------------------------------------------
        self._section(6, "AUDIT RECORDS")
        events = AuditEvent.objects.filter(
            object_id__in={contract.id, ctx.id, content.id}
        ).order_by("timestamp", "id")
        for ev in events:
            actor_str = ev.actor.get_username() if ev.actor else "system"
            self.stdout.write(
                f"  {ev.timestamp:%H:%M:%S}  {ev.event:<22} "
                f"{ev.object_type}#{ev.object_id:<4} by {actor_str}  {ev.detail or ''}"
            )

        # 7 — Boundary verification ---------------------------------------
        self.stdout.write("=" * 64)
        ok = (
            content.status in (Content.Status.PUBLISHED, Content.Status.REJECTED)
            and contract.status == ConsumptionContract.Status.PROCESSED
            and self._verify_boundary()
        )
        if ok:
            self.stdout.write(self.style.SUCCESS(
                "PASS — Trade Result transformed into reviewed/published content; "
                "Trade Result not persisted as a WIMS trade object; ADR-009 preserved."
            ))
        else:  # pragma: no cover
            self.stderr.write("FAIL — workflow or boundary check did not pass.")

    def _verify_boundary(self) -> bool:
        """Assert WIMS persists no Trade/Position/Deal/Execution/MT5/Broker model."""
        from django.apps import apps
        models = [m.__name__ for m in apps.get_app_config("wims").get_models()]
        offenders = [m for m in models if any(p in m.lower() for p in PROHIBITED)]
        if offenders:  # pragma: no cover
            self.stderr.write(f"BOUNDARY VIOLATION — prohibited models: {offenders}")
            return False
        self.stdout.write(f"  ADR-009 boundary OK — wims models: {sorted(models)}")
        self.stdout.write(
            "  Trade Result persisted ONLY as ConsumptionContract intelligence "
            "(no Trade/Position/Deal/Execution/MT5/Broker object)."
        )
        return True
