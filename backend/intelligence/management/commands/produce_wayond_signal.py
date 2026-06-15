"""
Phase 7A demonstration — Signal Intelligence Producer (Wayond only).

Proves the target flow end-to-end:

    Wayond Signal
      -> Signal Intelligence Envelope
      -> WIMS Consumption Contract
      -> Existing WIMS Pipeline (context -> content -> review -> publish)

Evidence printed (per packet):
    1. Wayond Signal exists
    2. Signal Intelligence Envelope created
    3. Envelope delivered successfully
    4. WIMS Consumption Contract created
    5. Existing WIMS pipeline accepted the object

Usage:
    python manage.py produce_wayond_signal
    python manage.py produce_wayond_signal --fixture intelligence/fixtures/wayond_signal_sample.json
    python manage.py produce_wayond_signal --signal '{"signal_id": "...", ...}'
    python manage.py produce_wayond_signal --no-pipeline   # stop after consumption
"""

import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from intelligence.delivery import ingest_wayond_signal
from wims import services
from wims.models import AuditEvent, ConsumptionContract, Content, Publish, Review
from wims.services import workflow_state_for_contract

User = get_user_model()

DEFAULT_FIXTURE = "intelligence/fixtures/wayond_signal_sample.json"


class Command(BaseCommand):
    help = "Phase 7A — produce a Signal Intelligence Envelope from a Wayond signal."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", help="Path to a Wayond signal JSON file.")
        parser.add_argument("--signal", help="Inline Wayond signal JSON string.")
        parser.add_argument("--actor", help="Existing username/email to attribute the run to.")
        parser.add_argument(
            "--no-pipeline", action="store_true",
            help="Stop after WIMS consumption (skip context/content/review/publish).",
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

    def _load_signal(self, opts) -> dict:
        if opts.get("signal"):
            return json.loads(opts["signal"])
        rel = opts.get("fixture") or DEFAULT_FIXTURE
        path = Path(rel)
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / rel
        if not path.exists():
            raise CommandError(f"Wayond signal fixture not found: {path}")
        return json.loads(path.read_text())

    def handle(self, *args, **opts):
        actor = self._resolve_actor(opts["actor"])
        signal = self._load_signal(opts)

        self.stdout.write(self.style.SUCCESS(
            "\nPhase 7A — Signal Intelligence Producer (Wayond) demonstration"
        ))
        self.stdout.write(f"Operator: {actor}\n")

        # 1 — Wayond Signal exists ----------------------------------------
        self._section(1, "WAYOND SIGNAL (external source intelligence)")
        self.stdout.write("  " + json.dumps(signal))

        # 2-4 — produce envelope, deliver, consume (single audited path) ---
        envelope, contract = ingest_wayond_signal(signal, actor=actor)

        self._section(2, "SIGNAL INTELLIGENCE ENVELOPE (immutable)")
        self.stdout.write(
            f"  intelligence_id={envelope.intelligence_id}  "
            f"type={envelope.intelligence_type}  version={envelope.version}"
        )
        self.stdout.write(f"  source={envelope.source}  summary={envelope.summary!r}")
        self.stdout.write(f"  payload={json.dumps(envelope.structured_payload.__dict__)}")

        self._section(3, "DELIVERY")
        self.stdout.write("  Envelope delivered across GuvFX -> WIMS boundary (see audit).")

        self._section(4, "WIMS CONSUMPTION CONTRACT (first persisted WIMS object)")
        self.stdout.write(
            f"  id={contract.id}  source={contract.source_type}  "
            f"{contract.direction} {contract.symbol}  entry={contract.entry_price} "
            f"sl={contract.stop_loss} tp={contract.take_profit}"
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
                    "Educational context generated from a consumed signal envelope. "
                    "Explains the instrument, what an entry/stop/target are as "
                    "concepts. Neutral and educational only."
                ),
                actor=actor,
            )
            content = services.create_content(
                context=ctx, title="Signal terminology explained (educational)",
                content_text="Neutral educational content derived from the consumed envelope.",
                actor=actor,
            )
            services.submit_for_review(content=content, actor=actor)
            services.review_content(
                content=content, decision=Review.Decision.APPROVE, reviewer=actor,
                notes="Phase 7A demonstration.",
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
        events = AuditEvent.objects.filter(
            detail__intelligence_id=envelope.intelligence_id
        ).order_by("timestamp", "id")
        contract_events = AuditEvent.objects.filter(
            object_type="ConsumptionContract", object_id=contract.id
        ).order_by("timestamp", "id")
        seen = set()
        for ev in list(events) + list(contract_events):
            if ev.id in seen:
                continue
            seen.add(ev.id)
            actor_str = ev.actor.get_username() if ev.actor else "system"
            self.stdout.write(
                f"  {ev.timestamp:%H:%M:%S}  {ev.event:<20} "
                f"{ev.object_type}#{ev.object_id:<4} by {actor_str}  {ev.detail or ''}"
            )

        # Boundary + lifecycle verification -------------------------------
        self.stdout.write("=" * 64)
        ok = self._verify(envelope, contract)
        if ok:
            self.stdout.write(self.style.SUCCESS(
                "PASS — Wayond signal -> envelope -> consumption contract -> WIMS "
                "pipeline; lifecycle audited; ADR-009 preserved (no trade object)."
            ))
        else:  # pragma: no cover
            self.stderr.write("FAIL — lifecycle or boundary verification failed.")

    def _verify(self, envelope, contract) -> bool:
        from django.apps import apps

        # All four lifecycle events present for this intelligence_id.
        required = {
            AuditEvent.Event.SIGNAL_RECEIVED,
            AuditEvent.Event.ENVELOPE_CREATED,
            AuditEvent.Event.ENVELOPE_DELIVERED,
            AuditEvent.Event.ENVELOPE_CONSUMED,
        }
        present = set(
            AuditEvent.objects.filter(
                detail__intelligence_id=envelope.intelligence_id
            ).values_list("event", flat=True)
        ) | set(
            AuditEvent.objects.filter(
                object_type="ConsumptionContract", object_id=contract.id,
                event=AuditEvent.Event.ENVELOPE_CONSUMED,
            ).values_list("event", flat=True)
        )
        missing = required - present
        if missing:  # pragma: no cover
            self.stderr.write(f"  Missing lifecycle events: {sorted(missing)}")
            return False
        self.stdout.write("  Lifecycle OK — SIGNAL_RECEIVED, ENVELOPE_CREATED, "
                          "ENVELOPE_DELIVERED, ENVELOPE_CONSUMED all recorded.")

        # GuvFX 'intelligence' app persists no models; WIMS persists no trade objects.
        intel_models = [m.__name__ for m in apps.get_app_config("intelligence").get_models()]
        wims_models = [m.__name__.lower() for m in apps.get_app_config("wims").get_models()]
        banned = ("trade", "position", "deal", "execution", "mt5", "broker")
        offenders = [m for m in wims_models if any(b in m for b in banned)]
        if intel_models or offenders:  # pragma: no cover
            self.stderr.write(
                f"  BOUNDARY VIOLATION intel_models={intel_models} offenders={offenders}"
            )
            return False
        self.stdout.write(
            "  ADR-009 OK — 'intelligence' persists no models (envelope is "
            "transient/immutable); WIMS persists no Trade/Position/Deal/Execution/"
            "MT5/Broker object."
        )
        return True
