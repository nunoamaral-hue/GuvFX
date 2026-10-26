"""refresh_instrument_mappings — persist the directional instrument-mapping matrix for ONE ordered pair.

Computes the ``master → follower`` candidates (``trade_copier.matrix.generate_candidates``) and upserts the
durable audit record: ``CanonicalInstrument`` (get-or-create by key) + ``InstrumentMapping`` (update-or-create
by the native directional pair). Safety contract (packet §6):

  * DRY-RUN BY DEFAULT — writes NOTHING unless ``--commit`` is given (so an accidental invocation is inert).
  * REQUIRES an explicit ordered pair (``--master`` and ``--follower``) and REFUSES a self-pair.
  * ENFORCES owner/tenant scope — master and follower must belong to the SAME user; a cross-tenant pair is
    refused and writes nothing.
  * NEVER marks a mapping verified on its own — ``verified_via_symbol_map`` only REFLECTS an existing
    verified ``CopierSymbolMap`` row; the command never creates or mutates ``CopierSymbolMap``.
  * SCOPED to exactly one ordered direction — it only ever upserts ``(master, follower)`` rows, so it can
    never delete or overwrite another direction's or another account's rows (idempotent re-run = no new row).
  * READ-ONLY w.r.t. the broker and the execution path — no order, no flag, no execution state, no job.

An OPERATOR tool for the later, Sponsor-gated persistence phase. NOT run in production in the current
(read-only discovery) phase; the read API computes the matrix live without persisting anything.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from trade_copier.matrix import ABSENT, generate_candidates
from trade_copier.models import CanonicalInstrument, InstrumentMapping
from trading.models import TradingAccount


def _canonical_for(slug, display):
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
    help = ("Persist the directional instrument-mapping matrix for one ordered (master, follower) pair. "
            "DRY-RUN by default; pass --commit to write. Same-owner only; refuses self-pairs; never auto-verifies.")

    def add_arguments(self, parser):
        parser.add_argument("--master", type=int, required=True, help="Master account id (source).")
        parser.add_argument("--follower", type=int, required=True, help="Follower account id (destination).")
        parser.add_argument("--commit", action="store_true",
                            help="Actually persist. WITHOUT this flag the command is a dry-run and writes nothing.")

    def handle(self, *args, **opts):
        try:
            master = TradingAccount.objects.get(pk=opts["master"])
            follower = TradingAccount.objects.get(pk=opts["follower"])
        except TradingAccount.DoesNotExist as exc:
            raise CommandError(f"account not found: {exc}")
        if master.id == follower.id:
            raise CommandError("refused: master and follower must be different accounts (no self-pair)")
        if master.user_id != follower.user_id:
            raise CommandError(
                f"refused: cross-tenant pair (master owner {master.user_id} != follower owner {follower.user_id}); "
                "mappings are owner-scoped")

        direction = generate_candidates(master, follower)
        prefix = "COMMIT" if opts["commit"] else "DRY-RUN"
        if not opts["commit"]:
            self.stdout.write(
                f"instrument-matrix {prefix} {master.id}->{follower.id}: "
                f"{len(direction.candidates)} candidates, counts={direction.counts} "
                f"(coverage={direction.coverage_pct}%); NOTHING written (pass --commit to persist)")
            return

        now = timezone.now()
        written = 0
        with transaction.atomic():
            for c in direction.candidates:
                canonical = _canonical_for(c.canonical_slug, c.canonical_display)
                # Keyed on the NATIVE pair so a re-run updates the single row in place (one native pair ->
                # exactly one canonical), never creating a second canonical attachment.
                InstrumentMapping.objects.update_or_create(
                    master_account=master, follower_account=follower,
                    master_symbol=c.master_symbol, follower_symbol=c.follower_symbol,
                    defaults={
                        "master_broker": direction.master_broker,
                        "follower_broker": direction.follower_broker,
                        "canonical_instrument": canonical,
                        "classification": c.classification, "reason": c.reason,
                        "identity_confidence": c.identity_confidence,
                        "material_differences": c.material_differences,
                        "compatibility_warnings": c.compatibility_warnings,
                        # Only ever REFLECTS an existing verified CopierSymbolMap; never a self-granted verify.
                        "verified_via_symbol_map": (c.classification != ABSENT and c.verified_at is not None),
                        "observed_at": now,
                    },
                )
                written += 1
        self.stdout.write(
            f"instrument-matrix {prefix} {master.id}->{follower.id}: upserted {written} rows "
            f"(counts={direction.counts}; coverage={direction.coverage_pct}%); no order, no flag, no symbol-map write")
