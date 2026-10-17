"""§8 — strictly READ-ONLY, broker-agnostic certification of a deterministic parser against a genuine evidence blob.

A generalization of ``certify_is6_blob`` (which stays IS6/DEPOSIT-specific): it runs the REAL parser-selection path
(``register_default_parsers`` + ``select_parser``) on the stored raw bytes, compares every provided ``--expect-*``
field, and asserts NO mutation (BrokerEvent/EvidenceBlob/Withdrawal counts, every mailbox cursor, and the blob hash
are snapshotted before and after). It NEVER ingests, NEVER writes, and creates nothing.

TWO DISTINCT RESULTS (never conflated — R1-CLOSEOUT Blocker C):
  * PARSER_INSPECTION_PASS — the deployed parser, run read-only on these stored bytes, produced the expected fields
    and nothing mutated. A hash check only proves the STORED BYTES are intact; it establishes NOTHING about
    provenance, sender authenticity, mailbox ownership, account attribution, or external money movement.
  * GENUINE_POSITIVE_CLASSIFIER_CERTIFIED — requires, ON TOP of a parser-inspection pass, the full independently
    verified evidence chain: broker in ``CERTIFIED_BROKERS``, authenticated sender (auth_verdict==pass), an
    allowlisted broker sender, a REAL-provenance BrokerEvent for this blob, and an EXTERNAL_WITHDRAWAL category.
    Until a genuine external-withdrawal sample exists and a broker is certified, this is authoritatively
    POSITIVE_CERTIFICATION_BLOCKED. The command can NEVER add a broker to CERTIFIED_BROKERS.

SECRET-FREE OUTPUT (R1-CLOSEOUT Blocker A): sender, account and broker-reference identifiers are masked in all
stdout/stderr and in every error/assertion message; pass only the LAST 4 digits of an expected account (compared on
last-4) so a full identifier never appears in shell arguments/history.

Usage (read-only):
    python manage.py certify_broker_blob --blob-id 1 --broker TradersWay --expect-category INTERNAL_TRANSFER
    python manage.py certify_broker_blob --blob-id N --broker TradersWay \
        --expect-category EXTERNAL_WITHDRAWAL --require-genuine-certified   # the future positive cert
Exit 0 iff the parser inspection passed and, when --require-genuine-certified is set, the full evidence chain held.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError


def _mask_tail(value: str, keep: int = 4) -> str:
    """Mask all but the last ``keep`` chars (account/reference identifiers). Short values are fully masked."""
    s = "" if value is None else str(value)
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def _mask_sender(addr: str) -> str:
    """Reduce a sender to its domain only (never the local-part)."""
    s = "" if addr is None else str(addr).strip()
    return ("<masked>@" + s.split("@", 1)[1]) if "@" in s else ("<masked>" if s else "")


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
        parser.add_argument("--expect-account-last4", default=None,
                            help="Expected account LAST 4 digits only (compared on last-4; never pass a full account).")
        parser.add_argument("--require-genuine-certified", action="store_true",
                            help="Exit non-zero unless the FULL genuine-positive evidence chain holds (use only once "
                                 "a broker is genuinely certified).")

    def handle(self, *args, **o):
        from broker_intelligence.models import BrokerEvent, EvidenceBlob, Withdrawal, ConnectedMailbox, Provenance, TransactionCategory
        from broker_intelligence.evidence import EvidenceStore, EvidenceStoreError
        from broker_intelligence.gmail_source import parse_rfc822
        from broker_intelligence.parsers import select_parser
        from broker_intelligence.parsers_tradersway import register_default_parsers
        from broker_intelligence.broker_senders import is_allowlisted_broker_sender
        from broker_intelligence import external_withdrawal_classifier as ewc

        try:
            blob = EvidenceBlob.objects.get(id=o["blob_id"])
        except EvidenceBlob.DoesNotExist:
            raise CommandError("EvidenceBlob id=%s not found" % o["blob_id"])

        # ---- BEFORE snapshot (mutation guards) ----
        before = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
        cursors_before = {m.id: m.cursor_state for m in ConnectedMailbox.objects.all()}
        sha_before = blob.sha256

        # ---- READ-ONLY: load raw (EvidenceStore.read RE-HASHES and RAISES on tamper -> hash_ok=False, not a
        #      traceback), select+run the deployed parser (NO ingest, NO write) ----
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
        strong = ewc.ExternalWithdrawalSignals(external_destination=True, explicit_external_type=True)
        failclosed_ok = (not ewc.is_external_withdrawal(strong, broker=broker)) if not is_certified else True

        # ---- EVIDENCE CHAIN (Blocker C): a hash pass proves bytes only; a GENUINE positive cert needs ALL of these ----
        auth_pass = (f.get("auth_verdict") == "pass")
        allowlisted = is_allowlisted_broker_sender(f.get("from_address", "")) if f else False
        real_event = BrokerEvent.objects.filter(evidence=blob, provenance=Provenance.REAL).exists()
        is_external = bool(parsed is not None and parsed.transaction_category == TransactionCategory.EXTERNAL_WITHDRAWAL)

        # ---- expectations (parser inspection) ----
        checks = []
        acct = (parsed.destination_account or parsed.source_account) if parsed else ""

        def chk(name, expected, actual, cmp=lambda e, a: str(e) == str(a)):
            if expected is None:
                return
            checks.append((name, parsed is not None and cmp(expected, actual)))

        if o["expect_parser"] is not None:
            checks.append(("parser", getattr(parser, "name", None) == o["expect_parser"]))
        if parsed is not None:
            chk("event_type", o["expect_event"], parsed.event_type)
            chk("transaction_category", o["expect_category"], parsed.transaction_category)
            chk("amount", o["expect_amount"], parsed.amount, lambda e, a: a is not None and Decimal(str(e)) == a)
            chk("currency", o["expect_currency"], parsed.currency)
            chk("fee", o["expect_fee"], parsed.fee, lambda e, a: a is not None and Decimal(str(e)) == a)
            chk("fee_currency", o["expect_fee_currency"], parsed.fee_currency)
            chk("account_last4", o["expect_account_last4"], acct, lambda e, a: str(e)[-4:] == str(a)[-4:])
        elif any(o[k] for k in ("expect_event", "expect_category", "expect_amount", "expect_currency",
                                "expect_fee", "expect_fee_currency", "expect_account_last4")):
            checks.append(("parse", False))

        parser_inspection_ok = hash_ok and no_mutation and failclosed_ok and all(ok for _, ok in checks)
        genuine_certified = bool(parser_inspection_ok and is_certified and auth_pass and allowlisted
                                 and real_event and is_external)

        # ---- OUTPUT (secret-free: identifiers masked everywhere) ----
        w = self.stdout.write
        w("=== BROKER PARSER CERTIFICATION (READ-ONLY, blob id=%s sha=%s) ===" % (blob.id, sha_before[:16]))
        w("evidence_hash_verified=%s  from=%s  auth_verdict=%s" % (hash_ok, _mask_sender(f.get("from_address", "")), f.get("auth_verdict")))
        w("matched_parser=%s" % (getattr(parser, "name", None)))
        if parsed is None:
            w("PARSE RESULT: None (no event).")
        else:
            w("PARSE RESULT (deployed parser): event_type=%s category=%s broker=%s amount=%s %s fee=%s %s "
              "account(masked)=%s ref(masked)=%s occurred_at=%s"
              % (parsed.event_type, parsed.transaction_category, parsed.broker, parsed.amount, parsed.currency,
                 parsed.fee, parsed.fee_currency, _mask_tail(acct), _mask_tail(parsed.broker_reference_id),
                 parsed.occurred_at))
        w("CERTIFICATION POSTURE: broker=%r in CERTIFIED_BROKERS=%s  fail_closed_when_uncertified=%s"
          % (broker, is_certified, failclosed_ok))
        w("EVIDENCE CHAIN: auth_pass=%s allowlisted_sender=%s real_provenance_event=%s external_category=%s broker_certified=%s"
          % (auth_pass, allowlisted, real_event, is_external, is_certified))
        w("MUTATION GUARD: before=%s after=%s cursors_unchanged=%s blob_sha_unchanged=%s -> NO_MUTATION=%s"
          % (before, after, cursors_before == cursors_after, sha_before == sha_after, no_mutation))
        if checks:
            w("--- EXPECTED vs ACTUAL (values masked) ---")
            for name, ok in checks:
                w("  %-28s %s" % (name, "PASS" if ok else "FAIL"))

        w("PARSER_INSPECTION_PASS=%s" % parser_inspection_ok)
        w("GENUINE_POSITIVE_CLASSIFIER_CERTIFIED=%s" % (genuine_certified or "POSITIVE_CERTIFICATION_BLOCKED"))

        # ---- exit semantics (no raw identifiers in the error) ----
        if not parser_inspection_ok:
            raise CommandError("PARSER_INSPECTION FAILED (hash_ok=%s no_mutation=%s fail_closed=%s checks_ok=%s)"
                               % (hash_ok, no_mutation, failclosed_ok, all(ok for _, ok in checks)))
        if o["require_genuine_certified"] and not genuine_certified:
            raise CommandError("GENUINE_POSITIVE_CLASSIFIER_CERTIFIED required but POSITIVE_CERTIFICATION_BLOCKED "
                               "(auth_pass=%s allowlisted=%s real_event=%s external=%s broker_certified=%s)"
                               % (auth_pass, allowlisted, real_event, is_external, is_certified))
