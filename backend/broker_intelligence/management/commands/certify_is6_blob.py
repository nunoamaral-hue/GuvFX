"""PR-B.1 — strictly READ-ONLY certification of the IS6 parser against a genuine immutable evidence blob.

Repeatable, side-effect-free: it (1) verifies the evidence content hash, (2) runs the DEPLOYED deterministic parser on
the stored raw bytes WITHOUT ingestion, (3) compares every expected field to the actual, and (4) asserts that NO
BrokerEvent/Withdrawal was created, NO mailbox cursor advanced, and NO evidence row changed (counts + the blob hash
are snapshotted before and after). It NEVER calls ingest_message, NEVER writes, and creates nothing.

This command exists to run the mandatory production certification of blob 3 once the Sponsor authorises it; it is safe
to run at any time because it is read-only. Account numbers are masked in output (last 4 digits); pass --expect-* to
assert exact evidence-backed values (the genuine figures stay in the immutable evidence, never in this file).

Usage (read-only):
    python manage.py certify_is6_blob --blob-id 3 \
        --expect-event DEPOSIT_COMPLETED --expect-category DEPOSIT \
        --expect-amount 200 --expect-currency USD --expect-fee 0 --expect-fee-currency USD \
        --expect-account 51763830
Exit code 0 iff every provided expectation matched AND nothing mutated.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError


def _mask(acct: str) -> str:
    acct = acct or ""
    return ("*" * (len(acct) - 4) + acct[-4:]) if len(acct) > 4 else acct


class Command(BaseCommand):
    help = "READ-ONLY certify the IS6 parser against a genuine immutable evidence blob (no ingestion, no writes)."

    def add_arguments(self, parser):
        parser.add_argument("--blob-id", type=int, default=3, help="EvidenceBlob id to evaluate (genuine IS6 deposit = 3).")
        parser.add_argument("--expect-event", default=None)
        parser.add_argument("--expect-category", default=None)
        parser.add_argument("--expect-amount", default=None)
        parser.add_argument("--expect-currency", default=None)
        parser.add_argument("--expect-fee", default=None)
        parser.add_argument("--expect-fee-currency", default=None)
        parser.add_argument("--expect-account", default=None, help="Expected destination/source account (compared exactly; printed masked).")
        parser.add_argument("--expect-broker", default="IS6")

    def handle(self, *args, **o):
        from broker_intelligence.models import BrokerEvent, EvidenceBlob, Withdrawal, ConnectedMailbox
        from broker_intelligence.evidence import EvidenceStore
        from broker_intelligence.gmail_source import parse_rfc822
        from broker_intelligence.parsers_is6 import IS6Parser

        try:
            blob = EvidenceBlob.objects.get(id=o["blob_id"])
        except EvidenceBlob.DoesNotExist:
            raise CommandError("EvidenceBlob id=%s not found" % o["blob_id"])

        # ---- BEFORE snapshot (mutation guards) ----
        before = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
        cursors_before = {m.id: m.cursor_state for m in ConnectedMailbox.objects.all()}
        sha_before = blob.sha256

        # ---- READ-ONLY: load raw, verify hash, parse headers, run the pure parser (NO ingest, NO write) ----
        raw = EvidenceStore().read(blob)
        hash_ok = hashlib.sha256(raw).hexdigest() == sha_before
        f = parse_rfc822(raw)
        parsed = IS6Parser().parse(subject=f["subject"], body=f["body"], from_address=f["from_address"])

        # ---- AFTER snapshot ----
        after = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
        cursors_after = {m.id: m.cursor_state for m in ConnectedMailbox.objects.all()}
        sha_after = EvidenceBlob.objects.get(id=blob.id).sha256
        no_mutation = before == after and cursors_before == cursors_after and sha_before == sha_after

        w = self.stdout.write
        w("=== IS6 PARSER CERTIFICATION (READ-ONLY, blob id=%s sha=%s) ===" % (blob.id, sha_before[:16]))
        w("evidence_hash_verified=%s  from=%s  auth_verdict=%s" % (hash_ok, f["from_address"], f.get("auth_verdict")))
        if parsed is None:
            w("PARSE RESULT: None (no event) — this is a FAILURE if a classification was expected.")
        else:
            w("PARSE RESULT (deployed parser):")
            w("  event_type=%s" % parsed.event_type)
            w("  transaction_category=%s" % parsed.transaction_category)
            w("  broker=%s" % parsed.broker)
            w("  amount=%s currency=%s" % (parsed.amount, parsed.currency))
            w("  fee=%s fee_currency=%s" % (parsed.fee, parsed.fee_currency))
            w("  destination_account(masked)=%s  source_account=%r" % (_mask(parsed.destination_account), parsed.source_account))
            w("  occurred_at=%s broker_reference_id=%r" % (parsed.occurred_at, parsed.broker_reference_id))
        w("MUTATION GUARD: before=%s after=%s cursors_unchanged=%s blob_sha_unchanged=%s -> NO_MUTATION=%s"
          % (before, after, cursors_before == cursors_after, sha_before == sha_after, no_mutation))

        # ---- expectations (actual vs expected) ----
        checks = []
        acct = (parsed.destination_account or parsed.source_account) if parsed else ""

        def chk(name, expected, actual, cmp=lambda a, b: str(a) == str(b)):
            if expected is None:
                return
            ok = parsed is not None and cmp(expected, actual)
            checks.append((name, expected, actual, ok))

        if parsed is not None:
            chk("event_type", o["expect_event"], parsed.event_type)
            chk("transaction_category", o["expect_category"], parsed.transaction_category)
            chk("amount", o["expect_amount"], parsed.amount, lambda e, a: a is not None and Decimal(str(e)) == a)
            chk("currency", o["expect_currency"], parsed.currency)
            chk("fee", o["expect_fee"], parsed.fee, lambda e, a: a is not None and Decimal(str(e)) == a)
            chk("fee_currency", o["expect_fee_currency"], parsed.fee_currency)
            chk("destination/source account", o["expect_account"], acct)
            chk("broker", o["expect_broker"], parsed.broker)
        elif any(o[k] for k in ("expect_event", "expect_category", "expect_amount", "expect_currency",
                                "expect_fee", "expect_fee_currency", "expect_account")):
            checks.append(("parse", "a classification", None, False))

        if checks:
            w("--- EXPECTED vs ACTUAL ---")
            for name, exp, act, ok in checks:
                shown = _mask(str(act)) if name.endswith("account") else act
                exp_shown = _mask(str(exp)) if name.endswith("account") else exp
                w("  %-28s expected=%-18s actual=%-18s %s" % (name, exp_shown, shown, "PASS" if ok else "FAIL"))

        all_ok = hash_ok and no_mutation and all(ok for *_, ok in checks)
        w("=== CERTIFICATION %s ===" % ("PASS" if all_ok else "FAIL"))
        if not all_ok:
            raise CommandError("certification FAIL (see rows above)")
