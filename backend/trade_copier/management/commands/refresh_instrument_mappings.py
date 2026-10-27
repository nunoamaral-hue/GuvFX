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

GOVERNED SAFE-EXACT-ONLY REVIEW MODE (``--safe-exact-only``, ADR-0062): a reviewer-gated path that persists
ONLY candidates satisfying the deterministic ``matrix.is_safe_exact`` predicate, each stamped with a named
reviewer + review evidence (``review_status=APPROVED``). It is additive discovery approval ONLY:

  * It NEVER relabels ``classification`` as VERIFIED (stays UNVERIFIED), NEVER sets ``verified_via_symbol_map``,
    and NEVER creates/mutates a ``CopierSymbolMap`` — discovery approval is SEPARATE from execution authority.
  * It FAILS CLOSED on commit without a non-empty ``--reviewer`` AND ``--evidence``.
  * It SKIPS (never persists) every AMBIGUOUS / INELIGIBLE / ABSENT / STALE candidate, any hard-dimension
    conflict, any material (soft) warning, and any VERIFIED row.

An OPERATOR tool for the later, Sponsor-gated persistence phase. NOT run in production in the current
(read-only discovery) phase; the read API computes the matrix live without persisting anything.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from trade_copier.matrix import ABSENT, generate_candidates, is_safe_exact
from trade_copier.models import CanonicalInstrument, InstrumentMapping, ReviewStatus
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
        parser.add_argument("--safe-exact-only", action="store_true",
                            help="Governed reviewed-persistence mode: persist ONLY SAFE-EXACT candidates, "
                                 "each stamped APPROVED by --reviewer with --evidence. Never marks VERIFIED.")
        parser.add_argument("--reviewer", type=str, default="",
                            help="Named operator identity (required for --safe-exact-only --commit).")
        parser.add_argument("--evidence", type=str, default="",
                            help="Bounded review evidence reference, e.g. a ticket/Notion id; never a secret "
                                 "(required for --safe-exact-only --commit).")
        parser.add_argument("--review-note", type=str, default="",
                            help="Optional short review note (<=512 chars).")

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

        # --- GOVERNED SAFE-EXACT-ONLY REVIEW MODE (ADR-0062) ------------------------------------------
        if opts["safe_exact_only"]:
            safe = [c for c in direction.candidates if is_safe_exact(c)]
            skipped = len(direction.candidates) - len(safe)
            if not opts["commit"]:
                self.stdout.write(
                    f"instrument-matrix SAFE-EXACT DRY-RUN {master.id}->{follower.id}: "
                    f"{len(safe)} SAFE-EXACT of {len(direction.candidates)} candidates "
                    f"would persist; skipped(non-safe-exact)={skipped}; counts={direction.counts}; "
                    f"NOTHING written (pass --commit with --reviewer and --evidence)")
                return
            reviewer = (opts.get("reviewer") or "").strip()
            evidence = (opts.get("evidence") or "").strip()
            if not reviewer:
                raise CommandError("refused: --safe-exact-only --commit requires a non-empty --reviewer "
                                   "(named operator identity); nothing written")
            if not evidence:
                raise CommandError("refused: --safe-exact-only --commit requires a non-empty --evidence "
                                   "reference; nothing written")
            note = (opts.get("review_note") or "").strip()
            now = timezone.now()
            created = updated = 0
            with transaction.atomic():
                for c in safe:
                    canonical = _canonical_for(c.canonical_slug, c.canonical_display)
                    _obj, was_created = InstrumentMapping.objects.update_or_create(
                        master_account=master, follower_account=follower,
                        master_symbol=c.master_symbol, follower_symbol=c.follower_symbol,
                        defaults={
                            "master_broker": direction.master_broker,
                            "follower_broker": direction.follower_broker,
                            "canonical_instrument": canonical,
                            # classification stays the MACHINE value (UNVERIFIED by the SAFE-EXACT predicate);
                            # human approval NEVER relabels it VERIFIED.
                            "classification": c.classification,
                            "reason": c.reason,
                            "identity_confidence": c.identity_confidence,
                            "material_differences": c.material_differences,
                            "compatibility_warnings": c.compatibility_warnings,
                            # SAFE-EXACT is UNVERIFIED by predicate — never a symbol-map verify.
                            "verified_via_symbol_map": False,
                            "review_status": ReviewStatus.APPROVED,
                            "reviewed_by": reviewer,
                            "reviewed_at": now,
                            "review_evidence_ref": evidence,
                            "review_note": note,
                            "observed_at": now,
                        },
                    )
                    created += int(was_created)
                    updated += int(not was_created)
            self.stdout.write(
                f"instrument-matrix SAFE-EXACT COMMIT {master.id}->{follower.id}: "
                f"created={created} updated={updated} skipped(non-safe-exact)={skipped} "
                f"reviewer={reviewer!r} evidence={evidence!r}; classification stays UNVERIFIED; "
                f"no CopierSymbolMap write, no flag, no order, no job")
            return

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
