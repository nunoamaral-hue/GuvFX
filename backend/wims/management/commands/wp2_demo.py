"""
WP-2 MVP demonstration — Wayond Signal Flow.

Runs the signal-sourced pipeline end-to-end and prints the six required
evidence artefacts:

    1. Contract record    (ConsumptionContract)
    2. Context record
    3. Content record
    4. Review record
    5. Publish record
    6. Audit records

    Wayond Signal -> Consumption Contract -> Context -> Content
        -> Human Review -> Publish

The "Wayond Signal" is an external source (not persisted). WIMS persists only
the ConsumptionContract describing the received intelligence — never a Signal,
Trade, MT5, Broker or Execution object (ADR-009).

Usage:
    python manage.py wp2_demo
    python manage.py wp2_demo --decision REJECT
    python manage.py wp2_demo --actor someuser --channel X
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from wims import services
from wims.models import AuditEvent, ConsumptionContract, Content, Publish, Review
from wims.services import workflow_state_for_contract

User = get_user_model()

# The external "Wayond Signal" — plain text, NOT persisted as a signal object.
WAYOND_SIGNAL_RAW = "BUY XAUUSD\nEntry 3350\nSL 3335\nTP 3370"

# Educational, informative, neutral, human-readable. Deliberately does NOT
# explain provider intent, recommend a trade, predict an outcome, validate the
# signal, generate a signal, or give financial advice.
EDU_CONTEXT = (
    "This material is educational only.\n"
    "- Market/instrument: XAUUSD denotes the price of gold (XAU) quoted "
    "against the US dollar (USD).\n"
    "- 'BUY' (long) and 'SELL' (short) describe the two directions an order "
    "can take, as general concepts.\n"
    "- An entry price is the level at which a position would be opened.\n"
    "- A stop-loss is a pre-set level used to cap the loss on a position if "
    "price moves against it.\n"
    "- A take-profit is a pre-set level at which a position would be closed in "
    "profit.\n"
    "These are definitions of common trading terms; nothing here is a "
    "recommendation, prediction, or endorsement of any position."
)

EDU_CONTENT = (
    "Understanding the building blocks of a trade idea\n\n"
    "When people discuss a position on gold (quoted as XAUUSD — gold priced in "
    "US dollars), a few standard terms come up. 'Buy' and 'sell' simply name "
    "the direction of a position. An 'entry' is the price at which a position "
    "would open. A 'stop-loss' is a level set in advance to limit how much can "
    "be lost if the market moves the other way, and a 'take-profit' is a level "
    "set in advance to close a position in profit.\n\n"
    "Knowing these definitions helps you read market commentary critically. "
    "This is general education about terminology, not advice, a recommendation, "
    "or a prediction about what any price will do."
)


class Command(BaseCommand):
    help = "Demonstrate the WP-2 Wayond Signal Flow end-to-end."

    def add_arguments(self, parser):
        parser.add_argument("--actor", help="Existing username/email to attribute the run to.")
        parser.add_argument(
            "--decision", choices=Review.Decision.values,
            default=Review.Decision.APPROVE,
        )
        parser.add_argument(
            "--channel", choices=Publish.Channel.values,
            default=Publish.Channel.TELEGRAM,
        )

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

    def handle(self, *args, **opts):
        actor = self._resolve_actor(opts["actor"])
        decision, channel = opts["decision"], opts["channel"]

        self.stdout.write(self.style.SUCCESS(
            "\nWP-2 — Wayond Signal Flow MVP demonstration"
        ))
        self.stdout.write("External Wayond Signal (not persisted):")
        for line in WAYOND_SIGNAL_RAW.splitlines():
            self.stdout.write(f"    {line}")
        self.stdout.write(f"Operator: {actor} | decision: {decision} | channel: {channel}\n")

        # 1 — Consumption Contract ----------------------------------------
        contract = services.create_contract(
            actor=actor,
            source_type=ConsumptionContract.SourceType.WAYOND,
            source_reference="wayond:demo",
            signal_type=ConsumptionContract.SignalType.ENTRY,
            symbol="XAUUSD",
            direction=ConsumptionContract.Direction.BUY,
            entry_price=Decimal("3350"),
            stop_loss=Decimal("3335"),
            take_profit=Decimal("3370"),
            confidence=Decimal("70"),
            raw_signal=WAYOND_SIGNAL_RAW,
        )
        self._section(1, "CONTRACT RECORD (ConsumptionContract)")
        self.stdout.write(
            f"  id={contract.id}  {contract.direction} {contract.symbol}  "
            f"entry={contract.entry_price} sl={contract.stop_loss} tp={contract.take_profit}"
        )
        self.stdout.write(
            f"  status={contract.status}  source={contract.source_type}  "
            f"workflow={workflow_state_for_contract(contract)}"
        )

        # 2 — Context (from contract) -------------------------------------
        ctx = services.create_context_from_contract(
            contract=contract, context_text=EDU_CONTEXT, actor=actor,
        )
        contract.refresh_from_db()
        self._section(2, "CONTEXT RECORD")
        self.stdout.write(f"  id={ctx.id}  contract_id={ctx.contract_id}  status={ctx.status}")
        self.stdout.write(f"  contract.status -> {contract.status} (processed)")

        # 3 — Content (reuses WP-1 model + flow, unchanged) ---------------
        content = services.create_content(
            context=ctx,
            title="Trading terms explained: gold (XAUUSD), entries, stops and targets",
            content_text=EDU_CONTENT,
            actor=actor,
        )
        self._section(3, "CONTENT RECORD")
        self.stdout.write(f"  id={content.id}  context_id={content.context_id}  status={content.status}")
        self.stdout.write(f"  title={content.title!r}")

        # 4 — Human review ------------------------------------------------
        services.submit_for_review(content=content, actor=actor)
        review = services.review_content(
            content=content, decision=decision, reviewer=actor,
            notes="Reviewed for the WP-2 MVP demonstration.",
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
        ids = {("ConsumptionContract", contract.id), ("Context", ctx.id), ("Content", content.id)}
        events = AuditEvent.objects.filter(
            object_id__in={i for _, i in ids}
        ).order_by("timestamp", "id")
        for ev in events:
            actor_str = ev.actor.get_username() if ev.actor else "system"
            self.stdout.write(
                f"  {ev.timestamp:%H:%M:%S}  {ev.event:<22} "
                f"{ev.object_type}#{ev.object_id:<4} by {actor_str}  {ev.detail or ''}"
            )

        # Boundary assertion: no prohibited object types persisted.
        self.stdout.write("=" * 64)
        self._assert_no_prohibited_persistence()
        ok = (
            content.status in (Content.Status.PUBLISHED, Content.Status.REJECTED)
            and contract.status == ConsumptionContract.Status.PROCESSED
            and events.filter(event=AuditEvent.Event.CONTRACT_CREATED).exists()
            and events.filter(event=AuditEvent.Event.CONTRACT_PROCESSED).exists()
        )
        if ok:
            self.stdout.write(self.style.SUCCESS(
                "PASS — Wayond Signal Flow demonstrated end-to-end with persisted "
                "records and audit evidence; no prohibited object types persisted."
            ))
        else:  # pragma: no cover
            self.stderr.write("FAIL — workflow did not complete as expected.")

    def _assert_no_prohibited_persistence(self):
        """ADR-009 guard: WIMS must not persist Signal/Trade/MT5/Broker/Execution."""
        from django.apps import apps
        prohibited = ("signal", "trade", "mt5", "broker", "execution")
        models = [m.__name__ for m in apps.get_app_config("wims").get_models()]
        offenders = [m for m in models if any(p in m.lower() for p in prohibited)]
        if offenders:  # pragma: no cover
            self.stderr.write(f"BOUNDARY VIOLATION — prohibited models in wims: {offenders}")
        else:
            self.stdout.write(
                f"  ADR-009 boundary OK — wims models: {sorted(models)}"
            )
