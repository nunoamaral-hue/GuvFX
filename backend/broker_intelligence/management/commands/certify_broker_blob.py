"""§8 — strictly READ-ONLY, broker-agnostic certification of a deterministic parser against a genuine evidence blob.

A generalization of ``certify_is6_blob`` (which stays IS6/DEPOSIT-specific): it runs the REAL parser-selection path
(``register_default_parsers`` + ``select_parser``) on the stored raw bytes, compares every provided ``--expect-*``
field, and asserts NO mutation (BrokerEvent/EvidenceBlob/Withdrawal counts, every mailbox cursor, and the blob hash
are snapshotted before and after). It NEVER ingests, NEVER writes, and creates nothing.

It is ready for the moment a GENUINE external-withdrawal sample exists: pass ``--expect-category EXTERNAL_WITHDRAWAL``
and ``--require-certified`` to certify the positive path. Until a broker is certified, it also PROVES the fail-closed
posture: for the named broker, the external-withdrawal decision core returns "not external" even given strong positive
signals (``external_withdrawal_classifier`` with the shipping empty ``CERTIFIED_BROKERS``). It can never ADD a broker
to ``CERTIFIED_BROKERS`` — certification is a deliberate operator act against genuine evidence, never a side effect.

Usage (read-only):
    python manage.py certify_broker_blob --blob-id 1 --broker TradersWay --expect-category INTERNAL_TRANSFER
    python manage.py certify_broker_blob --blob-id N --broker TradersWay \
        --expect-category EXTERNAL_WITHDRAWAL --require-certified   # the future positive cert
Exit 0 iff hash verified, nothing mutated, every expectation matched, and the certification posture held.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError


def _mask(acct: str) -> str:
    acct = acct or ""
    return ("*" * (len(acct) - 4) + acct[-4:]) if len(acct) > 4 else acct


class Command(BaseCommand):
    help = "READ-ONLY certify a broker parser against a genuine immutable evidence blob (no ingestion, no writes)."

    def add_arguments(self, parser):
        parser.add_argument("--blob-id", type=int, required=True)
        parser.add_argument("--broker", default="", help="Broker label for the CERTIFIED_BROKERS posture check (e.g. TradersWay).")
        parser.add_argument("--expect-parser", default=None, help="Assert which parser matched (e.g. tradersway/is6).")
        parser.add_argument("--expect-event", default=None)
        parser.add_argument("--expect-category", default=None)
        parser.add_argument("--expect-amount", default=None)
        parser.add_argument("--expect-currency", default=None)
        parser.add_argument("--expect-fee", default=None)
        parser.add_argument("--expect-fee-currency", default=None)
        parser.add_argument("--expect-account", default=None)
        parser.add_argument("--require-certified", action="store_true",
                            help="Assert the broker IS in CERTIFIED_BROKERS (use only once genuinely certified).")

    def handle(self, *args, **o):
        from broker_intelligence.models import BrokerEvent, EvidenceBlob, Withdrawal, ConnectedMailbox
        from broker_intelligence.evidence import EvidenceStore, EvidenceStoreError
        from broker_intelligence.gmail_source import parse_rfc822
        from broker_intelligence.parsers import select_parser
        from broker_intelligence.parsers_tradersway import register_default_parsers
        from broker_intelligence import external_withdrawal_classifier as ewc

        try:
            blob = EvidenceBlob.objects.get(id=o["blob_id"])
        except EvidenceBlob.DoesNotExist:
            raise CommandError("EvidenceBlob id=%s not found" % o["blob_id"])

        # ---- BEFORE snapshot (mutation guards) ----
        before = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
        cursors_before = {m.id: m.cursor_state for m in ConnectedMailbox.objects.all()}
        sha_before = blob.sha256

        # ---- READ-ONLY: load raw (EvidenceStore.read RE-HASHES and RAISES on tamper -> reported as hash_ok=False,
        #      not an uncaught traceback), select+run the deployed parser (NO ingest, NO write) ----
        f = {}
        parser = parsed = None
        try:
            raw = EvidenceStore().read(blob)
            hash_ok = hashlib.sha256(raw).hexdigest() == sha_before
            f = parse_rfc822(raw)
            register_default_parsers()
            parser = select_parser(subject=f["subject"], body=f["body"], from_address=f["from_address"])
            parsed = parser.parse(subject=f["subject"], body=f["body"], from_address=f["from_address"]) if parser else None
        except EvidenceStoreError:
            hash_ok = False                        # stored bytes no longer match the recorded hash (tamper/corruption)

        # ---- AFTER snapshot ----
        after = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
        cursors_after = {m.id: m.cursor_state for m in ConnectedMailbox.objects.all()}
        sha_after = EvidenceBlob.objects.get(id=blob.id).sha256
        no_mutation = before == after and cursors_before == cursors_after and sha_before == sha_after

        # ---- CERTIFICATION POSTURE: is this broker allowed a positive EXTERNAL_WITHDRAWAL today? ----
        broker = (o["broker"] or "").strip()
        is_certified = broker in ewc.CERTIFIED_BROKERS
        # With strong positive signals, an UNCERTIFIED broker must still be refused (fail-closed). Prove it.
        strong = ewc.ExternalWithdrawalSignals(external_destination=True, explicit_external_type=True)
        failclosed_ok = (not ewc.is_external_withdrawal(strong, broker=broker)) if not is_certified else True

        w = self.stdout.write
        w("=== BROKER PARSER CERTIFICATION (READ-ONLY, blob id=%s sha=%s) ===" % (blob.id, sha_before[:16]))
        w("evidence_hash_verified=%s  from=%s  auth_verdict=%s" % (hash_ok, f.get("from_address"), f.get("auth_verdict")))
        w("matched_parser=%s" % (getattr(parser, "name", None)))
        if parsed is None:
            w("PARSE RESULT: None (no event).")
        else:
            w("PARSE RESULT (deployed parser): event_type=%s category=%s broker=%s amount=%s %s fee=%s %s "
              "dest(masked)=%s src=%r occurred_at=%s ref=%r"
              % (parsed.event_type, parsed.transaction_category, parsed.broker, parsed.amount, parsed.currency,
                 parsed.fee, parsed.fee_currency, _mask(parsed.destination_account), parsed.source_account,
                 parsed.occurred_at, parsed.broker_reference_id))
        w("CERTIFICATION POSTURE: broker=%r in CERTIFIED_BROKERS=%s  fail_closed_when_uncertified=%s"
          % (broker, is_certified, failclosed_ok))
        w("MUTATION GUARD: before=%s after=%s cursors_unchanged=%s blob_sha_unchanged=%s -> NO_MUTATION=%s"
          % (before, after, cursors_before == cursors_after, sha_before == sha_after, no_mutation))

        # ---- expectations ----
        checks = []
        acct = (parsed.destination_account or parsed.source_account) if parsed else ""

        def chk(name, expected, actual, cmp=lambda a, b: str(a) == str(b)):
            if expected is None:
                return
            checks.append((name, expected, actual, parsed is not None and cmp(expected, actual)))

        if o["expect_parser"] is not None:
            checks.append(("parser", o["expect_parser"], getattr(parser, "name", None),
                           getattr(parser, "name", None) == o["expect_parser"]))
        if parsed is not None:
            chk("event_type", o["expect_event"], parsed.event_type)
            chk("transaction_category", o["expect_category"], parsed.transaction_category)
            chk("amount", o["expect_amount"], parsed.amount, lambda e, a: a is not None and Decimal(str(e)) == a)
            chk("currency", o["expect_currency"], parsed.currency)
            chk("fee", o["expect_fee"], parsed.fee, lambda e, a: a is not None and Decimal(str(e)) == a)
            chk("fee_currency", o["expect_fee_currency"], parsed.fee_currency)
            chk("destination/source account", o["expect_account"], acct)
        elif any(o[k] for k in ("expect_event", "expect_category", "expect_amount", "expect_currency",
                                "expect_fee", "expect_fee_currency", "expect_account")):
            checks.append(("parse", "a classification", None, False))

        if o["require_certified"]:
            checks.append(("broker_certified", True, is_certified, is_certified))

        if checks:
            w("--- EXPECTED vs ACTUAL ---")
            for name, exp, act, ok in checks:
                shown = _mask(str(act)) if name.endswith("account") else act
                exp_shown = _mask(str(exp)) if name.endswith("account") else exp
                w("  %-28s expected=%-20s actual=%-20s %s" % (name, exp_shown, shown, "PASS" if ok else "FAIL"))

        all_ok = hash_ok and no_mutation and failclosed_ok and all(ok for *_, ok in checks)
        w("=== CERTIFICATION %s ===" % ("PASS" if all_ok else "FAIL"))
        if not all_ok:
            raise CommandError("certification FAIL (see rows above)")
