"""refresh_instrument_mappings — persist the directional instrument-mapping matrix for ONE ordered pair.

Computes the ``master → follower`` candidates (``trade_copier.matrix.generate_candidates``) and upserts the
durable audit record: ``CanonicalInstrument`` (get-or-create by key) + ``InstrumentMapping`` (update-or-create
by the full directional key). It is:

  * SCOPED to exactly one ordered direction — it only touches ``(master, follower)`` rows and therefore can
    never delete or overwrite another direction's or another account's rows (idempotent re-run = no-op change).
  * READ-ONLY w.r.t. the broker and the execution path — it reads the stored catalogue + maps and writes only
    the two discovery tables. It never places an order, enables a flag, or writes ``CopierSymbolMap``.

This is an OPERATOR tool for the later, Sponsor-gated persistence phase. It is NOT run in production in the
current (read-only discovery) phase; the read API computes the matrix live without persisting anything.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from trade_copier.canonical import resolve_canonical
from trade_copier.matrix import ABSENT, generate_candidates
from trade_copier.models import CanonicalInstrument, InstrumentMapping
from trading.models import TradingAccount


def _canonical_for(slug, display):
    """Get-or-create the CanonicalInstrument for a slug; None when the symbol had no canonical."""
    if not slug:
        return None
    parts = slug.split(":")
    asset_class = parts[0] if len(parts) > 0 else "UNKNOWN"
    underlying = parts[1] if len(parts) > 1 else slug
    quote = parts[2] if len(parts) > 2 else ""
    obj, _ = CanonicalInstrument.objects.get_or_create(
        key=slug,
        defaults={"asset_class": asset_class, "underlying": underlying,
                  "quote_currency": quote, "display_name": display or ""},
    )
    return obj


class Command(BaseCommand):
    help = "Persist the directional instrument-mapping matrix for one ordered (master, follower) pair."

    def add_arguments(self, parser):
        parser.add_argument("--master", type=int, required=True)
        parser.add_argument("--follower", type=int, required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        try:
            master = TradingAccount.objects.get(pk=opts["master"])
            follower = TradingAccount.objects.get(pk=opts["follower"])
        except TradingAccount.DoesNotExist as exc:
            raise CommandError(f"account not found: {exc}")
        if master.id == follower.id:
            raise CommandError("master and follower must differ")

        direction = generate_candidates(master, follower)
        if opts["dry_run"]:
            self.stdout.write(
                f"instrument-matrix DRY-RUN {master.id}->{follower.id}: "
                f"{len(direction.candidates)} candidates, counts={direction.counts}")
            return

        now = timezone.now()
        written = 0
        with transaction.atomic():
            for c in direction.candidates:
                canonical = _canonical_for(c.canonical_slug, c.canonical_display)
                InstrumentMapping.objects.update_or_create(
                    master_account=master, follower_account=follower,
                    master_symbol=c.master_symbol, follower_symbol=c.follower_symbol,
                    defaults={
                        "master_broker": direction.master_broker,
                        "follower_broker": direction.follower_broker,
                        "canonical_instrument": canonical,
                        "classification": c.classification, "reason": c.reason,
                        "confidence": c.confidence, "material_differences": c.material_differences,
                        "verified_via_symbol_map": (c.classification != ABSENT and c.verified_at is not None),
                        "observed_at": now,
                    },
                )
                written += 1
        self.stdout.write(
            f"instrument-matrix {master.id}->{follower.id}: upserted {written} rows "
            f"(counts={direction.counts}; coverage={direction.coverage_pct}%); no order, no flag, no symbol-map write")
