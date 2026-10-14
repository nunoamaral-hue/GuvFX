"""
T1 R2 §6 — READ-ONLY production reconciliation for the catch-up checkpoint initialization.

Migration 0012 seeds ``SignalProvider.catchup_checkpoint_message_id`` from
``watermark_last_message_id``. That seed is BEHAVIOUR-PRESERVING (the pre-T1 code already
resumes catch-up from the watermark) but is NOT a proof of historical completeness: a past
live event (telethon.sync) could have advanced the watermark, and the pre-pagination 200-cap
could have abandoned a backlog, so a gap BELOW the seed may exist.

What DB evidence CAN establish: the current watermark (= the proposed seed) and the highest
genuine acquired message id on record, and whether they agree.
What it CANNOT establish: that no historical message was missed — Telegram message ids are
NON-CONTIGUOUS (deletions, service messages, channel gaps), so a numbering gap is NOT proof of
a missed message. A PASS therefore means only that the seed is consistent + behaviour-preserving,
never a completeness guarantee.

This command is strictly READ-ONLY (it never writes) and is safe to run against the
pre-migration (0009) schema: it references ONLY columns present there (no ``non_genuine`` /
``chat_id`` / ``catchup_checkpoint_message_id``). Use ``--gate`` as a fail-closed deploy gate.

    python manage.py reconcile_catchup_checkpoint          # human report
    python manage.py reconcile_catchup_checkpoint --gate   # non-zero exit if ANY provider REVIEW
    python manage.py reconcile_catchup_checkpoint --json    # machine-readable
"""

import json as _json

from django.core.management.base import BaseCommand, CommandError
from django.db.utils import DatabaseError

from signal_intake.models import AcquiredMessage, SignalProvider

_CAVEAT = (
    "DB evidence CANNOT prove historical completeness: Telegram message ids are non-contiguous, "
    "so a numbering gap is not proof of a missed message. PASS means only that the seed is "
    "consistent with the ledger and BEHAVIOUR-PRESERVING (identical to the pre-T1 resume cursor) "
    "— NOT a completeness guarantee. Closing a suspected gap below the seed requires a watermark "
    "reset (separate Sponsor-gated cutover), never this command. This command never writes."
)


def _as_int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _genuine_acquired_ids(slug):
    """Highest-genuine detection that is EXACT in BOTH schema states. Post-0011 the authoritative
    signal is ``non_genuine=False`` (this also excludes chat-refusal QUARANTINED rows, which the
    outcome proxy alone would miss). Pre-migration (0009) that column does not exist, so we fall
    back to the outcome proxy (exclude DROPPED_NOT_ARMED) — exact there, because chat-refusal rows
    cannot exist before T1. Read-only; references no column that is absent in its own branch."""
    base = AcquiredMessage.objects.filter(provider__slug=slug)
    try:
        qs = base.filter(non_genuine=False)
        return [i for i in (_as_int(m) for m in qs.values_list("message_id", flat=True))
                if i is not None]
    except DatabaseError:
        # ONLY a DB-level error (pre-0011 schema has no non_genuine column) falls back to the
        # outcome proxy. A FieldError (the field renamed/removed from the model) is NOT caught,
        # so it propagates loudly instead of silently reverting to the over-counting proxy and
        # re-introducing the false-PASS this guard closes (T1 R2 re-verify hardening).
        qs = base.exclude(outcome="DROPPED_NOT_ARMED")
        return [i for i in (_as_int(m) for m in qs.values_list("message_id", flat=True))
                if i is not None]


class Command(BaseCommand):
    help = ("READ-ONLY reconciliation of the catch-up checkpoint seed (= watermark) vs the "
            "acquisition ledger; fail-closed deploy gate (T1 R2 §6). Never writes.")

    def add_arguments(self, parser):
        parser.add_argument("--slug", default=None, help="Limit to one provider slug.")
        parser.add_argument("--gate", action="store_true",
                            help="Exit non-zero if ANY reported provider is REVIEW (deploy gate).")
        parser.add_argument("--json", action="store_true", help="Machine-readable output.")

    def handle(self, *args, **o):
        qs = SignalProvider.objects.all()
        if o["slug"]:
            qs = qs.filter(slug=o["slug"])
        # Select ONLY pre-migration columns — safe against the 0009 schema (no new T1 columns).
        rows = qs.order_by("slug").values(
            "slug", "status", "telegram_chat_id", "watermark_last_message_id")

        report, any_review = [], False
        matched = 0
        for p in rows:
            matched += 1
            slug = p["slug"]
            wm = (p["watermark_last_message_id"] or "").strip()
            wm_int = _as_int(wm)
            ids = _genuine_acquired_ids(slug)   # exact in both schema states; read-only
            max_acc = max(ids) if ids else None
            count = len(ids)

            if not wm:
                verdict, reason = "REVIEW", ("no watermark → seed would be empty; first catch-up "
                                             "would re-scan from 0 (operator decision)")
            elif wm_int is None:
                verdict, reason = "REVIEW", "watermark is not an integer"
            elif max_acc is None:
                verdict, reason = "REVIEW", "watermark set but no genuine acquisitions on record"
            elif wm_int == max_acc:
                verdict, reason = "PASS", "watermark == highest genuine acquired id (consistent seed)"
            elif wm_int > max_acc:
                verdict, reason = "REVIEW", (f"watermark ({wm_int}) AHEAD of highest genuine acquired "
                                             f"id ({max_acc}) — possible live-event contamination or "
                                             f"abandoned backlog below the seed")
            else:
                verdict, reason = "REVIEW", (f"watermark ({wm_int}) BEHIND highest genuine acquired "
                                             f"id ({max_acc}) — inconsistent")
            any_review = any_review or (verdict == "REVIEW")
            report.append({
                "slug": slug, "status": p["status"], "chat_id": p["telegram_chat_id"],
                "watermark": wm, "proposed_seed": wm, "max_genuine_acquired_id": max_acc,
                "genuine_acquired_count": count, "verdict": verdict, "reason": reason,
            })

        if o["json"]:
            self.stdout.write(_json.dumps(
                {"providers": report, "any_review": any_review, "caveat": _CAVEAT}, indent=2))
        else:
            for r in report:
                self.stdout.write(
                    f"[{r['verdict']}] {r['slug']} status={r['status']} chat={r['chat_id']} "
                    f"watermark/seed={r['watermark'] or '(empty)'} "
                    f"max_genuine_acquired={r['max_genuine_acquired_id']} "
                    f"count={r['genuine_acquired_count']} — {r['reason']}")
            self.stdout.write("")
            self.stdout.write(f"NOTE: {_CAVEAT}")

        if o["gate"]:
            # Fail CLOSED: an empty reconciliation set (e.g. a typo'd --slug, or no providers) is
            # NOT clearance — a gate that verified zero providers must never read green.
            if matched == 0:
                raise CommandError(
                    "reconciliation GATE: no providers matched — nothing was reconciled; refusing "
                    "to clear the deploy (fail-closed). Check --slug / provider configuration.")
            if any_review:
                raise CommandError(
                    "reconciliation GATE: one or more providers REVIEW — resolve before applying "
                    "migration 0012 / deploying (fail-closed).")
