"""
Ingest Wayond Telegram messages into WIMS **content** (no execution).

Reads a batch of Telegram messages (a JSON export — default fixture, or
``--file``), parses Wayond's format, deduplicates by message_id, quarantines
anything unparseable, and feeds each new SIGNAL into the existing Phase 7A
content path (-> SignalIntelligenceEnvelope -> WIMS ConsumptionContract).

This NEVER places a trade. The signal->execution path is separate, human-gated,
and out of scope for this command (see the Notion packet).

Usage:
    python manage.py ingest_wayond_telegram
    python manage.py ingest_wayond_telegram --file path/to/export.json
"""

import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from intelligence.delivery import ingest_wayond_telegram_signal
from intelligence.telegram_source import classify_messages
from wims.models import ConsumptionContract

User = get_user_model()
DEFAULT_FIXTURE = "intelligence/fixtures/wayond_telegram_messages.json"


class Command(BaseCommand):
    help = "Ingest Wayond Telegram messages into WIMS content (no execution)."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Path to a Telegram messages JSON export.")
        parser.add_argument("--actor", help="Existing username/email to attribute to.")

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

    def handle(self, *args, **opts):
        actor = self._actor(opts.get("actor"))
        rel = opts.get("file") or DEFAULT_FIXTURE
        path = Path(rel)
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / rel
        if not path.exists():
            raise CommandError(f"Telegram export not found: {path}")
        messages = json.loads(path.read_text())

        # Idempotency: skip messages already ingested (by source_reference marker).
        seen = set(
            ConsumptionContract.objects
            .filter(source_type=ConsumptionContract.SourceType.WAYOND)
            .values_list("source_reference", flat=True)
        )
        seen_ids = {s.split("telegram:", 1)[1] for s in seen if "telegram:" in s}

        plan = classify_messages(messages, seen_ids=seen_ids)

        self.stdout.write(self.style.SUCCESS(
            "\nWayond Telegram -> WIMS content ingestion (no execution)"
        ))
        self.stdout.write(
            f"  parsed: {len(plan.signals)} signal(s), {len(plan.updates)} update(s), "
            f"{len(plan.quarantined)} quarantined, {len(plan.duplicates)} duplicate(s)"
        )

        dates = {str(m.get("message_id", "")): m.get("date", "") for m in messages}
        created = 0
        for p in plan.signals:
            _env, contract = ingest_wayond_telegram_signal(
                p, actor=actor, timestamp=dates.get(p.message_id, ""),
            )
            # tag the telegram message id onto the contract for idempotency
            contract.source_reference = f"{contract.source_reference} telegram:{p.message_id}"
            contract.save(update_fields=["source_reference"])
            created += 1
            self.stdout.write(
                f"  + SIGNAL {p.market} {p.direction} @ {p.entry} -> "
                f"ConsumptionContract#{contract.id} (WAYOND, content)"
            )

        for p in plan.quarantined:
            self.stdout.write(self.style.WARNING(
                f"  ~ quarantined msg {p.message_id}: {p.reason}"
            ))
        for p in plan.updates:
            self.stdout.write(
                f"  . update ({p.update_type}) msg {p.message_id} — not a new signal, skipped"
            )

        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS(
            f"Done — {created} new content contract(s); 0 trades placed (content-only)."
        ))
