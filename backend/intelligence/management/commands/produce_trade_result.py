"""
Phase 7B demonstration — Trade Result Intelligence Producer.

Proves the target flow end-to-end:

    Closed Trade
      -> Trade Result Intelligence Envelope
      -> WIMS Consumption Contract (source_type=TRADE_RESULT)
      -> Existing WIMS Pipeline (context -> content -> review -> publish)

Evidence printed (per packet):
    1. Closed Trade exists
    2. Trade Result Envelope created
    3. Envelope delivered successfully
    4. WIMS Consumption Contract created
    5. Existing WIMS pipeline accepted the object

Trade source (authoritative): ``trading.models.Trade`` closed trades.
    --ticket / --account  -> load a real closed Trade from GuvFX trade history
    --fixture / --trade   -> a representative closed-trade record (local/offline)

Usage:
    python manage.py produce_trade_result
    python manage.py produce_trade_result --ticket 100245789 --account 12
    python manage.py produce_trade_result --no-pipeline
"""

import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from intelligence.delivery import ingest_trade_result
from wims import services
from wims.models import AuditEvent, ConsumptionContract, Content, Publish, Review
from wims.services import workflow_state_for_contract

User = get_user_model()

DEFAULT_FIXTURE = "intelligence/fixtures/closed_trade_sample.json"


class Command(BaseCommand):
    help = "Phase 7B — produce a Trade Result Intelligence Envelope from a closed trade."

    def add_arguments(self, parser):
        parser.add_argument("--ticket", help="Ticket of a real closed trading.Trade.")
        parser.add_argument("--account", type=int, help="Account id for --ticket lookup.")
        parser.add_argument("--fixture", help="Path to a closed-trade JSON record.")
        parser.add_argument("--trade", help="Inline closed-trade JSON string.")
        parser.add_argument("--actor", help="Existing username/email to attribute the run to.")
        parser.add_argument("--no-pipeline", action="store_true",
                            help="Stop after WIMS consumption.")

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

    def _load_trade(self, opts):
        # Authoritative path: real closed trade from GuvFX trade history.
        if opts.get("ticket"):
            try:
                from trading.models import Trade
            except Exception as exc:  # pragma: no cover - app not installed in demo
                raise CommandError(f"trading app unavailable for --ticket: {exc}")
            qs = Trade.objects.filter(ticket=opts["ticket"], close_time__isnull=False)
            if opts.get("account"):
                qs = qs.filter(account_id=opts["account"])
            trade = qs.first()
            if not trade:
                raise CommandError("No matching closed trade found.")
            return trade, "trading.models.Trade (real)"
        # Offline/local path: representative closed-trade record.
        if opts.get("trade"):
            return json.loads(opts["trade"]), "inline record"
        rel = opts.get("fixture") or DEFAULT_FIXTURE
        path = Path(rel)
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / rel
        if not path.exists():
            raise CommandError(f"Closed-trade fixture not found: {path}")
        return json.loads(path.read_text()), f"fixture {rel}"

    def handle(self, *args, **opts):
        actor = self._resolve_actor(opts["actor"])
        trade, origin = self._load_trade(opts)

        self.stdout.write(self.style.SUCCESS(
            "\nPhase 7B — Trade Result Intelligence Producer demonstration"
        ))
        self.stdout.write(f"Operator: {actor}  |  trade source: {origin}\n")

        # 1 — Closed Trade exists -----------------------------------------
        self._section(1, "CLOSED TRADE (authoritative GuvFX trade outcome)")
        if isinstance(trade, dict):
            self.stdout.write("  " + json.dumps(trade))
        else:
            self.stdout.write(
                f"  ticket={trade.ticket} {trade.symbol} {trade.side} "
                f"open={trade.open_price}@{trade.open_time} close={trade.close_price}@{trade.close_time} "
                f"profit={trade.profit}"
            )

        # 2-4 — produce, deliver, consume ---------------------------------
        envelope, contract = ingest_trade_result(trade, actor=actor)
        p = envelope.structured_payload

        self._section(2, "TRADE RESULT INTELLIGENCE ENVELOPE (immutable)")
        self.stdout.write(
            f"  intelligence_id={envelope.intelligence_id}  "
            f"type={envelope.intelligence_type}  version={envelope.version}"
        )
        self.stdout.write(f"  source={envelope.source}  summary={envelope.summary!r}")
        self.stdout.write(f"  payload={json.dumps(p.__dict__)}")

        self._section(3, "DELIVERY")
        self.stdout.write("  Envelope delivered across GuvFX -> WIMS boundary (see audit).")

        self._section(4, "WIMS CONSUMPTION CONTRACT (first persisted WIMS object)")
        self.stdout.write(
            f"  id={contract.id}  source={contract.source_type}  "
            f"{contract.direction} {contract.symbol}  outcome={contract.result_type} "
            f"pnl={contract.profit_loss} pips={contract.pips}"
        )
        self.stdout.write(
            f"  source_reference={contract.source_reference}  status={contract.status}  "
            f"workflow={workflow_state_for_contract(contract)}"
        )

        # 5 — Existing WIMS pipeline accepts the object -------------------
        self._section(5, "EXISTING WIMS PIPELINE ACCEPTS THE OBJECT")
        if opts["no_pipeline"]:
            self.stdout.write("  (--no-pipeline) consumption verified; pipeline not run.")
        else:
            ctx = services.create_context_from_contract(
                contract=contract,
                context_text=(
                    "Educational context generated from a consumed trade-result "
                    "envelope. Explains the instrument and what an outcome/pnl/pips "
                    "describe as concepts. Neutral and educational; no advice, "
                    "prediction, or recommendation."
                ),
                actor=actor,
            )
            content = services.create_content(
                context=ctx, title="Trade outcome terminology explained (educational)",
                content_text="Neutral educational content derived from the consumed envelope.",
                actor=actor,
            )
            services.submit_for_review(content=content, actor=actor)
            services.review_content(
                content=content, decision=Review.Decision.APPROVE, reviewer=actor,
                notes="Phase 7B demonstration.",
            )
            services.publish_content(
                content=content, channel=Publish.Channel.TELEGRAM, publisher=actor,
            )
            content.refresh_from_db()
            contract.refresh_from_db()
            self.stdout.write(
                f"  context#{ctx.id} -> content#{content.id} ({content.status}) -> "
                f"contract workflow {workflow_state_for_contract(contract)}"
            )

        # Audit evidence --------------------------------------------------
        self._section(6, "AUDIT EVIDENCE (existing WIMS audit capability)")
        rows = list(AuditEvent.objects.filter(
            detail__intelligence_id=envelope.intelligence_id
        ).order_by("timestamp", "id")) + list(AuditEvent.objects.filter(
            object_type="ConsumptionContract", object_id=contract.id
        ).order_by("timestamp", "id"))
        seen = set()
        for ev in rows:
            if ev.id in seen:
                continue
            seen.add(ev.id)
            actor_str = ev.actor.get_username() if ev.actor else "system"
            self.stdout.write(
                f"  {ev.timestamp:%H:%M:%S}  {ev.event:<20} "
                f"{ev.object_type}#{ev.object_id:<4} by {actor_str}  {ev.detail or ''}"
            )

        # Verification ----------------------------------------------------
        self.stdout.write("=" * 64)
        if self._verify(envelope, contract):
            self.stdout.write(self.style.SUCCESS(
                "PASS — Closed trade -> trade-result envelope -> consumption "
                "contract -> WIMS pipeline; lifecycle audited; ADR-009 preserved."
            ))
        else:  # pragma: no cover
            self.stderr.write("FAIL — lifecycle or boundary verification failed.")

    def _verify(self, envelope, contract) -> bool:
        from django.apps import apps
        required = {
            AuditEvent.Event.TRADE_DETECTED,
            AuditEvent.Event.ENVELOPE_CREATED,
            AuditEvent.Event.ENVELOPE_DELIVERED,
            AuditEvent.Event.ENVELOPE_CONSUMED,
        }
        present = set(
            AuditEvent.objects.filter(detail__intelligence_id=envelope.intelligence_id)
            .values_list("event", flat=True)
        )
        missing = required - present
        if missing:  # pragma: no cover
            self.stderr.write(f"  Missing lifecycle events: {sorted(missing)}")
            return False
        self.stdout.write("  Lifecycle OK — TRADE_DETECTED, ENVELOPE_CREATED, "
                          "ENVELOPE_DELIVERED, ENVELOPE_CONSUMED all recorded.")

        if contract.source_type != ConsumptionContract.SourceType.TRADE_RESULT:  # pragma: no cover
            self.stderr.write("  Contract is not TRADE_RESULT.")
            return False

        intel_models = [m.__name__ for m in apps.get_app_config("intelligence").get_models()]
        wims_models = [m.__name__.lower() for m in apps.get_app_config("wims").get_models()]
        banned = ("trade", "position", "deal", "execution", "mt5", "broker")
        offenders = [m for m in wims_models if any(b in m for b in banned)]
        if intel_models or offenders:  # pragma: no cover
            self.stderr.write(f"  BOUNDARY VIOLATION intel={intel_models} offenders={offenders}")
            return False
        self.stdout.write(
            "  ADR-009 OK — 'intelligence' persists no models (envelope transient/"
            "immutable); WIMS persists no Trade/Position/Deal/Execution/MT5/Broker object."
        )
        return True
