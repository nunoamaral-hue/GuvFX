"""Bounded, owner-scoped, reference-aware, append-only RECOVERY of a single orphan broker COMPLETION email.

INCIDENT (2026-10-24): a genuine TradersWay external-payout completion ("Payment request #N status changed from
Confirmed to Completed") was FETCHED and stored as immutable evidence, but was ingested by a parser build that
predated completion-template support (#530, 2026-10-20) — so it produced NO ``BrokerEvent`` and never advanced its
withdrawal. The same old parser also stored the matching CONFIRMATION event ref-less. The parser is now fixed, but the
forward-only ingestion never re-parses an already-fetched message, and ``reclassify_broker_event`` only revises an
EXISTING event's category — it cannot create the missing completion event. This command performs the bounded recovery.

STRICTLY BOUNDED + SAFE:
* Operates on ONE named ``(owner, evidence-sha)`` and REFUSES unless the re-parsed completion's broker reference equals
  the expected ``--reference`` (no broad/mailbox-wide scan; no amount-only matching).
* Re-ingests that ONE stored blob through the normal pipeline (allowlisted sender + authenticated verdict gates still
  apply), creating the missing COMPLETION ``BrokerEvent`` APPEND-ONLY. The original (absence of an) event and all
  immutable evidence are untouched.
* Links the completion to the EXISTING external ``Withdrawal`` ONLY when that withdrawal's own REQUEST event evidence
  re-extracts to the SAME broker reference (deterministic reference match, proven from immutable evidence — never
  amount-only, never invented). The withdrawal's reference and its requested anchor (the request email's broker Date)
  are back-filled on the MUTABLE projection from that evidence; the completion then advances it to COMPLETED via the
  normal ``correlate_completion_event`` path.
* IDEMPOTENT: re-running finds the completion event already present and the withdrawal already COMPLETED → no-op. No
  duplicate event or withdrawal is ever created.
* DRY-RUN by default; ``--execute`` applies. Read-only on Gmail (no network; operates on stored evidence only).
* REFUSES (fail-closed, no mutation) if: the owner/blob is absent; the sender is not an allowlisted broker; the verdict
  is not authenticated; the re-parse is not a COMPLETION of the expected reference; or NO existing external withdrawal
  of this owner has a request whose evidence proves the same reference (it will NOT invent a request/duration).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Append-only recovery of one orphan broker completion email into its existing withdrawal (bounded, idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--owner", required=True, help="Owner email (owner-scoped; must match the mailbox owner).")
        parser.add_argument("--evidence-sha", required=True, help="EvidenceBlob sha256 (full or unambiguous prefix).")
        parser.add_argument("--reference", required=True, help="Expected broker reference the completion must carry.")
        parser.add_argument("--execute", action="store_true", help="Apply the recovery (default: dry-run).")

    def handle(self, *args, **opts):
        from django.contrib.auth import get_user_model
        from ...evidence import EvidenceStore, EvidenceStoreError
        from ...gmail_source import parse_rfc822
        from ...mail_source import MailMessage
        from ...models import BrokerEvent, ConnectedMailbox, EvidenceBlob, Provenance, Withdrawal
        from ...ingestion import ingest_message
        from ...broker_senders import is_allowlisted_broker_sender
        from ...parsers import select_parser
        from ... import parsers_tradersway as _tw
        from ...correlation import correlate_completion_event, _event_time
        _tw.register_default_parsers()

        execute = opts["execute"]
        owner_email = opts["owner"].strip().lower()
        sha = opts["evidence_sha"].strip()
        expected_ref = opts["reference"].strip()

        def emit(msg):
            self.stdout.write(msg)

        User = get_user_model()
        owner = User.objects.filter(email__iexact=owner_email).first()
        if owner is None:
            raise CommandError(f"owner not found: {owner_email}")

        blob_matches = list(EvidenceBlob.objects.filter(sha256__startswith=sha)[:2])
        if not blob_matches:
            raise CommandError(f"evidence blob not found for sha prefix: {sha[:12]}…")
        if len(blob_matches) > 1:
            raise CommandError(f"refused: --evidence-sha prefix {sha[:12]}… is ambiguous (matches >1 blob); "
                               f"pass the full 64-hex sha256.")
        blob = blob_matches[0]

        # Re-derive the message from the IMMUTABLE stored bytes (no Gmail network) and re-parse with the current parser.
        try:
            raw = EvidenceStore().read(blob)
        except (EvidenceStoreError, OSError) as exc:
            raise CommandError(f"cannot read stored evidence (integrity/availability): {type(exc).__name__}")
        d = parse_rfc822(raw)
        subject, body, from_address = d["subject"], d["body"], d.get("from_address", "")
        if not is_allowlisted_broker_sender(from_address):
            raise CommandError("refused: sender is not an allowlisted broker (no recovery).")
        if d.get("auth_verdict") != "pass":
            raise CommandError("refused: message is not authenticated (auth_verdict != pass).")
        parser = select_parser(subject=subject, body=body, from_address=from_address)
        parsed = parser.parse(subject=subject, body=body, from_address=from_address) if parser else None
        if parsed is None or parsed.event_type != "WITHDRAWAL_COMPLETED":
            raise CommandError(f"refused: blob does not re-parse to a WITHDRAWAL_COMPLETED "
                               f"(got {getattr(parsed, 'event_type', None)}).")
        if (parsed.broker_reference_id or "") != expected_ref:
            raise CommandError(f"refused: completion reference {parsed.broker_reference_id!r} != expected "
                               f"{expected_ref!r} (no amount-only / mismatched match).")

        # Find the EXISTING external withdrawal this completion belongs to: the owner's withdrawal whose REQUEST event
        # evidence deterministically re-extracts to the SAME reference. Never amount-only; never invented.
        def _request_ref(w) -> str:
            ev = w.requested_event
            if ev is None or ev.evidence_id is None:
                return ""
            try:
                rraw = EvidenceStore().read(ev.evidence)
            except (EvidenceStoreError, OSError):
                return ""
            rd = parse_rfc822(rraw)
            m = _tw._TW_REQUEST_NUMBER.search(rd.get("body", "")) or _tw._TW_PAYMENT_REQUEST.search(rd.get("body", ""))
            return m.group(1) if m else ""

        candidates = []
        for w in Withdrawal.objects.filter(owner=owner).select_related("requested_event"):
            if w.broker_reference_id == expected_ref or _request_ref(w) == expected_ref:
                candidates.append(w)
        if len(candidates) == 0:
            raise CommandError(f"refused: no existing external withdrawal of {owner_email} has a request proving "
                               f"reference {expected_ref} — will NOT invent a request/duration (hard stop).")
        if len(candidates) > 1:
            raise CommandError(f"refused: {len(candidates)} withdrawals match reference {expected_ref} (ambiguous).")
        wd = candidates[0]
        if wd.status in (Withdrawal.Status.REJECTED, Withdrawal.Status.CANCELLED):
            raise CommandError(f"refused: target withdrawal id={wd.id} is terminal ({wd.status}); a completion must "
                               f"not stamp a completion time/duration on a rejected/cancelled withdrawal (no mutation).")

        mbox = ConnectedMailbox.objects.filter(user=owner, status=ConnectedMailbox.Status.CONNECTED).order_by("id").first()
        req_ev = wd.requested_event
        req_anchor = _event_time(req_ev) if req_ev is not None else None

        def _mask(e: str) -> str:
            if "@" not in e:
                return "***"
            loc, _, dom = e.partition("@")
            return f"{(loc[:2] if len(loc) > 2 else '*')}…@{dom}"

        emit("=== TradersWay completion recovery (bounded, append-only) ===")
        emit(f"  owner:            {_mask(owner_email)} (id={owner.id})")
        emit(f"  evidence sha:     {blob.sha256[:12]}…  received(Date): {blob.received_at.isoformat()}")
        emit(f"  re-parsed:        event_type=WITHDRAWAL_COMPLETED category={parsed.transaction_category} "
             f"ref={parsed.broker_reference_id} amount={parsed.amount} {parsed.currency}")
        emit(f"  target withdrawal: id={wd.id} status={wd.status} amount={wd.amount} {wd.currency} "
             f"ref={wd.broker_reference_id!r} requested_at={wd.requested_at}")
        emit(f"  request event:    id={getattr(req_ev,'id',None)} received(Date)={getattr(req_ev,'received_at',None)}")
        emit(f"  PLAN: create completion event (append-only); set withdrawal.ref={expected_ref} "
             f"(if blank) + requested_at={req_anchor} (if None); advance to COMPLETED (completed_at=completion Date).")
        already_ev = BrokerEvent.objects.filter(evidence=blob, owner=owner, event_type="WITHDRAWAL_COMPLETED").first()
        emit(f"  idempotency:      completion_event_exists={already_ev is not None} "
             f"withdrawal_already_completed={wd.status == Withdrawal.Status.COMPLETED}")

        if not execute:
            emit("  DRY-RUN — no changes written. Re-run with --execute to apply.")
            return

        with transaction.atomic():
            # 1) Create the missing completion event APPEND-ONLY via the normal pipeline (idempotent by (owner, blob)).
            msg = MailMessage(
                provider_message_id=f"recovery:{blob.sha256[:16]}", raw_bytes=raw,
                to_addresses=tuple(d.get("to_addresses", ())), from_address=from_address,
                subject=subject, body=body, received_at=d["received_at"], auth_verdict=d.get("auth_verdict"))
            completion_ev = ingest_message(msg, provenance=Provenance.REAL, owner_user=owner, connected_mailbox=mbox)
            if completion_ev is None:
                raise CommandError("recovery aborted: completion event was not created (gate refused) — no mutation.")

            # 2) Back-fill the MUTABLE projection from IMMUTABLE evidence: reference (so the completion links) and the
            #    requested anchor (the request email's broker Date) — only when absent, never overwriting a known value.
            fields = []
            wd.refresh_from_db()
            if not wd.broker_reference_id:
                wd.broker_reference_id = expected_ref
                fields.append("broker_reference_id")
                # Now anchored by a deterministic broker reference — record that as the method so the member UI
                # attributes the completion to a reference match, not the original ref-less heuristic opening.
                if wd.correlation_method != Withdrawal.CorrelationMethod.REFERENCE_ID:
                    wd.correlation_method = Withdrawal.CorrelationMethod.REFERENCE_ID
                    fields.append("correlation_method")
            if wd.requested_at is None and req_anchor is not None:
                wd.requested_at = req_anchor
                fields.append("requested_at")
            if fields:
                wd.save(update_fields=fields + ["updated_at"])

            # 3) Advance the withdrawal to COMPLETED via the normal completion-by-reference path (sets completed_at =
            #    the completion email's broker Date + completed_event; monotonic; never fabricates externality).
            res = correlate_completion_event(completion_ev)

        wd.refresh_from_db()
        emit("  APPLIED:")
        emit(f"    completion event id={completion_ev.id} correlation_outcome={res.outcome}")
        emit(f"    withdrawal id={wd.id} status={wd.status} ref={wd.broker_reference_id} "
             f"requested_at={wd.requested_at} completed_at={wd.completed_at} "
             f"duration_seconds={wd.duration_seconds()}")
