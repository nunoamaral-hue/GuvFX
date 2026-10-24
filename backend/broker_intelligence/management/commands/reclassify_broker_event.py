"""WP6.1 / ADR-0059 — bounded, append-only reclassification of ONE BrokerEvent.

Re-reads the event's IMMUTABLE evidence, re-runs the current certified parser, and — only when the re-derived
category differs from the current EFFECTIVE category — appends a write-once ``BrokerEventClassificationRevision``.
The original ``BrokerEvent`` / ``EvidenceBlob`` are NEVER mutated. The outcome is ALWAYS parser-derived: there is NO
``--category`` / operator override. Fail-closed on any ownership / evidence / parser-provenance / scope mismatch.
Atomic + idempotent (a deterministic idempotency key + the ``derived == effective`` short-circuit). Prints COUNTS and
SAFE HASHES only — never an email body, confirmation URL, token, full account number, request number, or any PII.
"""
from __future__ import annotations

import hashlib

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from ...evidence import EvidenceStore, EvidenceStoreError
from ...gmail_source import parse_rfc822
from ...models import BrokerEvent, BrokerEventClassificationRevision, EvidenceBlob
from ...parsers import select_parser
from ...parsers_tradersway import register_default_parsers

# The current category-rule generation (ADR-0059). Bumped when the classification rule changes; recorded on every
# revision so a correction is attributable to an exact rule version, independently of the parser version string.
RULE_VERSION = "ext-rail-v1"


class Command(BaseCommand):
    help = ("Append an append-only classification revision for ONE bounded BrokerEvent, re-deriving its category from "
            "the immutable evidence under the current certified parser (ADR-0059). Parser-derived only; never an "
            "operator override. Fail-closed, atomic, idempotent. Safe output (ids / categories / hash prefixes) only.")

    def add_arguments(self, parser):
        parser.add_argument("--owner", required=True,
                            help="Owner user id or email — MUST match the event's owner (ownership guard).")
        parser.add_argument("--event", required=True, type=int, help="The single BrokerEvent id (bounded target).")
        parser.add_argument("--reason", default="stale_ingestion_reclassify", help="Reason code (audit).")
        parser.add_argument("--dry-run", action="store_true", help="Compute and report only; append nothing.")

    def handle(self, *args, **opts):
        register_default_parsers()                       # idempotent; ensure the certified parsers are registered
        owner_arg = str(opts["owner"]).strip()
        event_id = int(opts["event"])
        reason = str(opts["reason"]).strip() or "stale_ingestion_reclassify"
        dry = bool(opts["dry_run"])

        user_model = get_user_model()
        owner = None
        if owner_arg.isdigit():
            owner = user_model.objects.filter(id=int(owner_arg)).first()
        if owner is None:
            owner = user_model.objects.filter(email__iexact=owner_arg).first()
        if owner is None:
            raise CommandError("REFUSED: owner not found (fail-closed).")

        ev = BrokerEvent.objects.filter(id=event_id).first()
        if ev is None:
            raise CommandError("REFUSED: event not found (fail-closed).")
        if ev.owner_id != owner.id:                      # ownership / scope guard (tenant isolation)
            raise CommandError("REFUSED: event owner mismatch — target scope differs (fail-closed).")
        if ev.evidence_id is None:
            raise CommandError("REFUSED: event has no evidence (fail-closed).")
        blob = EvidenceBlob.objects.filter(id=ev.evidence_id).first()
        if blob is None:
            raise CommandError("REFUSED: evidence blob missing (fail-closed).")

        # Re-read the IMMUTABLE evidence (EvidenceStore.read integrity-checks the bytes against the stored hash).
        try:
            raw = EvidenceStore().read(blob)
        except EvidenceStoreError:
            raise CommandError("REFUSED: evidence integrity check failed (fail-closed).")
        recomputed = hashlib.sha256(raw).hexdigest()
        if blob.sha256 != recomputed or (ev.evidence_hash and ev.evidence_hash != recomputed):
            raise CommandError("REFUSED: evidence hash mismatch (fail-closed).")

        parsed_msg = parse_rfc822(raw)
        subject = parsed_msg.get("subject", "") or ""
        body = parsed_msg.get("body", "") or ""
        from_address = parsed_msg.get("from_address", "") or ""

        parser = select_parser(subject=subject, body=body, from_address=from_address)
        if parser is None:
            raise CommandError("REFUSED: no certified parser claims this message (fail-closed).")
        if ev.parser_name and getattr(parser, "name", "") != ev.parser_name:
            raise CommandError("REFUSED: parser provenance differs from the event's parser (fail-closed).")
        try:
            parsed = parser.parse(subject=subject, body=body, from_address=from_address)
        except Exception:                                # noqa: BLE001 — a parser failure must fail closed, never partial
            raise CommandError("REFUSED: parser raised on re-derivation (fail-closed).")
        if parsed is None:
            raise CommandError("REFUSED: parser declined the message on re-derivation (fail-closed).")

        derived = parsed.transaction_category
        effective = ev.effective_transaction_category()
        key_src = (f"{ev.id}|{derived}|{getattr(parser, 'name', '')}|{getattr(parser, 'version', '')}"
                   f"|{RULE_VERSION}|{recomputed}")
        idem = hashlib.sha256(key_src.encode("utf-8")).hexdigest()

        self.stdout.write(f"event={ev.id} owner={owner.id} effective_category={effective} derived_category={derived} "
                          f"evidence_sha12={recomputed[:12]} idem12={idem[:12]} dry_run={dry}")

        if derived == effective:
            self.stdout.write("NO_CHANGE: derived == effective; no revision appended (idempotent).")
            return
        if dry:
            self.stdout.write(f"DRY_RUN: would append revision {effective} -> {derived} (nothing written).")
            return

        current_effective_rev = ev.classification_revisions.order_by("-id").first()
        try:
            with transaction.atomic():
                if BrokerEventClassificationRevision.objects.filter(idempotency_key=idem).exists():
                    self.stdout.write("IDEMPOTENT: a revision with this key already exists; no-op.")
                    return
                BrokerEventClassificationRevision.objects.create(
                    event=ev, derived_category=derived, original_category=ev.transaction_category,
                    original_parser_version=ev.parser_version or "", parser_name=getattr(parser, "name", ""),
                    parser_version=getattr(parser, "version", ""), classification_rule_version=RULE_VERSION,
                    reason=reason, source_evidence_sha256=recomputed, actor="cmd:reclassify_broker_event",
                    supersedes=current_effective_rev, idempotency_key=idem)
        except IntegrityError:
            self.stdout.write("IDEMPOTENT: concurrent insert hit the unique idempotency key; no duplicate (no-op).")
            return
        self.stdout.write(f"APPLIED: appended revision {effective} -> {derived} for event {ev.id}.")
