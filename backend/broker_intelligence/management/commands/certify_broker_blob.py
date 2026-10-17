"""§8 — strictly READ-ONLY, broker-agnostic certification of a deterministic parser against a genuine evidence blob.

A generalization of ``certify_is6_blob`` (which stays IS6/DEPOSIT-specific): it runs the REAL parser-selection path
(``register_default_parsers`` + ``select_parser``) on the stored raw bytes, compares every provided ``--expect-*``
field, and asserts NO mutation (BrokerEvent/EvidenceBlob/Withdrawal counts, every mailbox cursor, and the blob hash
are snapshotted before and after). It NEVER ingests, NEVER writes, and creates nothing.

THREE DISTINCT, NEVER-CONFLATED RESULTS (R1-CLOSEOUT Blocker C — a preliminary check is NOT final certification):
  * PARSER_INSPECTION_PASS — the deployed parser, run read-only on these stored bytes, produced the expected fields
    and nothing mutated. A hash check only proves the STORED BYTES are intact; it establishes NOTHING about
    provenance, sender authenticity, mailbox ownership, account attribution, or external money movement.
  * EVIDENCE_CHAIN_PRECHECK — a PRELIMINARY, machine-verifiable check that a matching REAL EXTERNAL_WITHDRAWAL
    BrokerEvent exists for THIS blob with its whole chain verified: evidence-hash match, broker == requested broker,
    non-null owner, non-null trading_account that BELONGS to that owner, a VERIFIED BrokerEmailIdentity binding
    (owner+account+broker), plus an authenticated (auth_verdict==pass) allowlisted sender. ``.exists()`` alone is
    insufficient — each nullable link is checked, so a REAL event with NULL owner/account, the wrong broker/category,
    or a mismatched hash can never pass. A PRECHECK PASS is still NOT a genuine certification.
  * GENUINE_POSITIVE_CLASSIFIER_CERTIFIED — requires, ON TOP of the precheck: the DEPLOYED parser live-classifying
    THESE bytes as EXTERNAL_WITHDRAWAL (not merely the stored category), the broker in ``CERTIFIED_BROKERS``, AND
    an explicit INDEPENDENT HUMAN attestation (``--attest-authenticity-signoff``) of positive-external movement +
    authenticity — the part the approved schema cannot fully represent. Absent any of these it is authoritatively
    POSITIVE_CERTIFICATION_BLOCKED. The command can NEVER add a broker to CERTIFIED_BROKERS or fabricate provenance.

SECRET-FREE OUTPUT (R1-CLOSEOUT Blocker A): sender, account and broker-reference identifiers are masked in all
stdout/stderr and in every error/assertion message; amount/fee/exact-time are shown only as presence; pass only the
LAST 4 digits of an expected account (compared on last-4) so a full identifier never appears in shell arguments.

Usage (read-only):
    python manage.py certify_broker_blob --blob-id 1 --broker TradersWay --expect-category INTERNAL_TRANSFER
    python manage.py certify_broker_blob --blob-id N --broker TradersWay --expect-category EXTERNAL_WITHDRAWAL \
        --attest-authenticity-signoff "REVIEWER:<ref>" --require-genuine-certified   # the future positive cert
Exit 0 iff the parser inspection passed and, when --require-genuine-certified is set, full genuine certification held.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError


def _mask_tail(value: str, keep: int = 4) -> str:
    """Mask all but the last ``keep`` chars (account/reference identifiers). ``keep<=0`` masks fully; a value no
    longer than ``keep`` is also fully masked. (Note ``s[-0:]`` is the whole string, so keep must be guarded.)"""
    s = "" if value is None else str(value)
    if keep <= 0 or len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def _mask_sender(addr: str) -> str:
    """Reduce a sender to its domain only (never the local-part). rsplit on the LAST '@' + strip any angle brackets
    so a pathological multi-'@' or display-wrapped value still reveals only the final domain label."""
    s = ("" if addr is None else str(addr)).strip().strip("<>").strip()
    return ("<masked>@" + s.rsplit("@", 1)[-1]) if "@" in s else ("<masked>" if s else "")


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
        parser.add_argument("--attest-authenticity-signoff", default=None,
                            help="Reference to an INDEPENDENT HUMAN sign-off of positive-external-movement + "
                                 "authenticity (per the authenticity spec). The machine EVIDENCE_CHAIN_PRECHECK is "
                                 "only a preliminary check; GENUINE certification additionally requires this attestation.")
        parser.add_argument("--require-genuine-certified", action="store_true",
                            help="Exit non-zero unless FULL genuine-positive certification holds (evidence-chain "
                                 "precheck + certified broker + human attestation). Use only once genuinely certified.")

    def handle(self, *args, **o):
        from broker_intelligence.models import (BrokerEvent, EvidenceBlob, Withdrawal, ConnectedMailbox, Provenance,
                                                TransactionCategory, BrokerEmailIdentity)
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

        # ---- EVIDENCE CHAIN (Blocker C): a valid hash proves STORED BYTES only — not provenance, owner, broker, or
        #      account. A GENUINE positive classification needs a matching REAL EXTERNAL_WITHDRAWAL BrokerEvent for
        #      THIS blob whose whole owner<->broker<->account<->identity chain is verified. .exists() alone (the old
        #      check) would pass on a REAL event with NULL owner/account, the wrong broker/category, or a mismatched
        #      evidence hash — all representable in the schema — so each link is checked explicitly. ----
        auth_pass = (f.get("auth_verdict") == "pass")
        allowlisted = is_allowlisted_broker_sender(f.get("from_address", "")) if f else False
        ev = (BrokerEvent.objects
              .filter(evidence=blob, provenance=Provenance.REAL,
                      transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL)
              .select_related("trading_account").first())
        ev_present = ev is not None
        ev_hash_match = bool(ev and ev.evidence_hash and ev.evidence_hash == blob.sha256)
        ev_broker_match = bool(ev and broker and (ev.broker or "").strip() == broker)
        ev_owner_ok = bool(ev and ev.owner_id is not None)
        ev_account_belongs_owner = bool(ev and ev.trading_account_id is not None and ev.trading_account is not None
                                        and ev.owner_id is not None and ev.trading_account.user_id == ev.owner_id)
        ev_verified_identity = bool(
            ev and ev.owner_id and ev.trading_account_id and broker
            and BrokerEmailIdentity.objects.filter(
                user_id=ev.owner_id, trading_account_id=ev.trading_account_id,
                status=BrokerEmailIdentity.Status.VERIFIED, broker_name__iexact=broker).exists())
        # The machine-verifiable chain. A PASS here is a PRELIMINARY check, NOT genuine certification.
        evidence_chain_precheck = all([ev_present, ev_hash_match, ev_broker_match, ev_owner_ok,
                                       ev_account_belongs_owner, ev_verified_identity, auth_pass, allowlisted])
        # The DEPLOYED parser (the artifact being certified) must ALSO live-classify THESE bytes as EXTERNAL — a
        # stored category alone (possibly written by an older parser version) must not certify the current parser.
        is_external_live = bool(parsed is not None
                                and parsed.transaction_category == TransactionCategory.EXTERNAL_WITHDRAWAL)

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

        # A vacuous run (no parser matched AND no --expect-* checks) inspected nothing: it must NOT report a pass, so
        # a script grepping PARSER_INSPECTION_PASS=True can never treat a no-op as a certification.
        parser_inspected = (parser is not None) or bool(checks)
        parser_inspection_ok = (hash_ok and no_mutation and failclosed_ok and parser_inspected
                                and all(ok for _, ok in checks))
        # FINAL genuine certification additionally requires the broker certified AND an explicit INDEPENDENT HUMAN
        # attestation of positive-external movement + authenticity — the part the approved schema cannot fully
        # represent (per the authenticity spec). Without it, a passed precheck stays POSITIVE_CERTIFICATION_BLOCKED.
        attestation = (o.get("attest_authenticity_signoff") or "").strip()
        genuine_certified = bool(parser_inspection_ok and evidence_chain_precheck and is_external_live
                                 and is_certified and attestation)

        # ---- OUTPUT (secret-free: identifiers masked everywhere) ----
        w = self.stdout.write
        w("=== BROKER PARSER CERTIFICATION (READ-ONLY, blob id=%s sha=%s) ===" % (blob.id, sha_before[:16]))
        w("evidence_hash_verified=%s  from=%s  auth_verdict=%s" % (hash_ok, _mask_sender(f.get("from_address", "")), f.get("auth_verdict")))
        w("matched_parser=%s" % (getattr(parser, "name", None)))
        if parsed is None:
            w("PARSE RESULT: None (no event).")
        else:
            # Financial values (amount/fee) and the exact transaction time are a member's private withdrawal detail:
            # show only their PRESENCE + the (non-identifying) currency, never the raw sum/timing, so routine stdout /
            # CI logs stay secret-free. The precise value is still certified via --expect-amount (PASS/FAIL only).
            # The broker reference is masked FULLY (keep=0) — it has no last-4 use-case here.
            w("PARSE RESULT (deployed parser): event_type=%s category=%s broker=%s has_amount=%s currency=%s "
              "has_fee=%s fee_currency=%s account(masked)=%s ref(masked)=%s occurred_at_present=%s"
              % (parsed.event_type, parsed.transaction_category, parsed.broker, parsed.amount is not None,
                 parsed.currency, parsed.fee is not None, parsed.fee_currency, _mask_tail(acct),
                 _mask_tail(parsed.broker_reference_id, keep=0), parsed.occurred_at is not None))
        w("CERTIFICATION POSTURE: broker=%r in CERTIFIED_BROKERS=%s  fail_closed_when_uncertified=%s"
          % (broker, is_certified, failclosed_ok))
        w("EVIDENCE CHAIN (machine-verifiable, this blob): real_external_event=%s hash_match=%s broker_match=%s "
          "owner_present=%s account_belongs_to_owner=%s verified_identity=%s auth_pass=%s allowlisted_sender=%s"
          % (ev_present, ev_hash_match, ev_broker_match, ev_owner_ok, ev_account_belongs_owner,
             ev_verified_identity, auth_pass, allowlisted))
        w("broker_certified=%s  deployed_parser_external=%s  human_attestation_present=%s"
          % (is_certified, is_external_live, bool(attestation)))
        w("MUTATION GUARD: before=%s after=%s cursors_unchanged=%s blob_sha_unchanged=%s -> NO_MUTATION=%s"
          % (before, after, cursors_before == cursors_after, sha_before == sha_after, no_mutation))
        if checks:
            w("--- EXPECTED vs ACTUAL (values masked) ---")
            for name, ok in checks:
                w("  %-28s %s" % (name, "PASS" if ok else "FAIL"))

        # Preliminary machine check vs final certification are reported DISTINCTLY: a passed precheck is NOT a
        # genuine certification. Final cert needs the independent human attestation (and a certified broker).
        w("PARSER_INSPECTION_PASS=%s" % parser_inspection_ok)
        w("EVIDENCE_CHAIN_PRECHECK=%s" % ("PASS" if evidence_chain_precheck else "BLOCKED"))
        w("GENUINE_POSITIVE_CLASSIFIER_CERTIFIED=%s" % ("PASS" if genuine_certified else "POSITIVE_CERTIFICATION_BLOCKED"))

        # ---- exit semantics (no raw identifiers in the error) ----
        if not parser_inspection_ok:
            raise CommandError("PARSER_INSPECTION FAILED (hash_ok=%s no_mutation=%s fail_closed=%s inspected=%s checks_ok=%s)"
                               % (hash_ok, no_mutation, failclosed_ok, parser_inspected, all(ok for _, ok in checks)))
        if o["require_genuine_certified"] and not genuine_certified:
            raise CommandError("GENUINE_POSITIVE_CLASSIFIER_CERTIFIED required but POSITIVE_CERTIFICATION_BLOCKED "
                               "(evidence_chain_precheck=%s deployed_parser_external=%s broker_certified=%s human_attestation=%s)"
                               % (evidence_chain_precheck, is_external_live, is_certified, bool(attestation)))
