"""WS3 incident — TradersWay completion-recovery tests (ADR-0061 + recover_withdrawal_completion).

Covers the 2026-10-19 #4113783 incident class: a genuine broker completion email was fetched + stored but produced no
event (parsed by a pre-completion-template build), and its matching confirmation was stored ref-less. Certifies:
correlation requested-anchor symmetry (duration derivable from the broker email Date headers), and the bounded,
owner-scoped, reference-aware, idempotent, append-only recovery command.
"""
from __future__ import annotations

import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence import parsers
from broker_intelligence.correlation import correlate_event
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.gmail_source import parse_rfc822
from broker_intelligence.models import BrokerEvent, EvidenceBlob, Provenance, TransactionCategory, Withdrawal
from broker_intelligence.parsers_tradersway import register_default_parsers
from broker_intelligence.withdrawal_speed import withdrawal_speed
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "97%04d" % _n


_AR = ("mx.google.com; dkim=pass header.i=@tradersway.com header.d=tradersway.com; "
       "spf=pass smtp.mailfrom=bounce.icpbounce.com; dmarc=fail (p=NONE) header.from=tradersway.com")


def _confirmation_raw(ref, amount="24.12 USD", date="Mon, 19 Oct 2026 07:55:04 +0000"):
    html = (f"<html><body><p>Dear [REDACTED],</p><p>Please confirm your withdrawal of {amount}. "
            f"Your withdrawal request number #{ref}.</p></body></html>")
    return (f"From: TradersWay <payments@tradersway.com>\r\nTo: nrfda1111@googlemail.com\r\n"
            f"Subject: Please confirm your withdrawal request\r\nAuthentication-Results: {_AR}\r\n"
            f"Date: {date}\r\n"
            'Content-Type: multipart/related; boundary="BXYZ"\r\n\r\n'
            "--BXYZ\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + html + "\r\n--BXYZ--\r\n").encode("utf-8")


def _completion_raw(ref, date="Mon, 19 Oct 2026 08:28:05 +0000"):
    html = (f"<html><body><p>Dear [REDACTED],</p><p>Your payment request #{ref} status was changed from "
            f"Confirmed to Completed.</p></body></html>")
    return (f"From: Finance Department <payments@tradersway.com>\r\nTo: nrfda1111@googlemail.com\r\n"
            f"Subject: Your payment request #{ref} status changed from Confirmed to Completed\r\n"
            f"Authentication-Results: {_AR}\r\nDate: {date}\r\n"
            'Content-Type: multipart/related; boundary="BXYZ"\r\n\r\n'
            "--BXYZ\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + html + "\r\n--BXYZ--\r\n").encode("utf-8")


# ----------------------------------------------------------------------------------------------------------------
# Correlation requested-anchor symmetry (ADR-0061)
# ----------------------------------------------------------------------------------------------------------------
class CorrelationAnchorSymmetry(TestCase):
    def test_requested_at_falls_back_to_received_at_when_no_occurred_at(self):
        u = User.objects.create_user(username="cs%s" % _uniq(), email="cs@x.invalid", password="x")
        a = TradingAccount.objects.create(user=u, name="A", broker_name="TradersWay", account_number=_uniq(),
                                          is_demo=False, is_active=True)
        import datetime
        rx = timezone.now() - datetime.timedelta(minutes=33)
        ev = BrokerEvent.objects.create(
            event_type="WITHDRAWAL_REQUESTED", transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL,
            broker="TradersWay", occurred_at=None, received_at=rx, amount=Decimal("24.12"), currency="USD",
            owner=u, trading_account=a, provenance=Provenance.REAL, broker_reference_id="R1")
        correlate_event(ev)
        w = Withdrawal.objects.get(broker_reference_id="R1")
        # requested_at is backfilled from received_at (email Date) even though occurred_at is None.
        self.assertEqual(w.requested_at, rx)

    def test_impossible_duration_returns_none_single_chokepoint(self):
        """ADR-0061 safety: an out-of-order pair (completed_at < requested_at) yields duration_seconds()==None at the
        model chokepoint, so NO consumer (WS3, metrics, public aggregation) can ship a negative/impossible figure."""
        import datetime
        u = User.objects.create_user(username="imp%s" % _uniq(), email="imp@x.invalid", password="x")
        a = TradingAccount.objects.create(user=u, name="A", broker_name="TradersWay", account_number=_uniq(),
                                          is_demo=False, is_active=True)
        now = timezone.now()
        w = Withdrawal.objects.create(trading_account=a, owner=u, amount=Decimal("5"), currency="USD",
                                      requested_at=now, completed_at=now - datetime.timedelta(hours=1),
                                      status=Withdrawal.Status.COMPLETED, provenance=Provenance.REAL)
        self.assertIsNone(w.duration_seconds())   # negative interval → None, never a bogus number

    def test_completed_event_on_terminal_withdrawal_does_not_stamp_completion(self):
        """A COMPLETED event matching a REJECTED/CANCELLED withdrawal (same monotonic rank) must NOT stamp completed_at
        or a duration onto it — a rejected withdrawal must never show a processing time."""
        from broker_intelligence.correlation import correlate_completion_event
        u = User.objects.create_user(username="rej%s" % _uniq(), email="rej@x.invalid", password="x")
        a = TradingAccount.objects.create(user=u, name="A", broker_name="TradersWay", account_number=_uniq(),
                                          is_demo=False, is_active=True)
        w = Withdrawal.objects.create(trading_account=a, owner=u, broker_reference_id="RJ1", amount=Decimal("5"),
                                      currency="USD", requested_at=timezone.now(), status=Withdrawal.Status.REJECTED,
                                      correlation_method=Withdrawal.CorrelationMethod.REFERENCE_ID,
                                      provenance=Provenance.REAL)
        comp = BrokerEvent.objects.create(
            event_type="WITHDRAWAL_COMPLETED", transaction_category=TransactionCategory.UNKNOWN, broker="TradersWay",
            occurred_at=None, received_at=timezone.now(), owner=u, trading_account=a, broker_reference_id="RJ1",
            provenance=Provenance.REAL)
        correlate_completion_event(comp)
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.REJECTED)   # unchanged
        self.assertIsNone(w.completed_at)                        # NOT stamped
        self.assertIsNone(w.duration_seconds())


# ----------------------------------------------------------------------------------------------------------------
# Bounded append-only recovery command
# ----------------------------------------------------------------------------------------------------------------
@override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="tradersway.com",
                   BROKER_INTELLIGENCE_TRUSTED_AUTHSERV_IDS="mx.google.com")
class CompletionRecovery(TestCase):
    REF = "4113999"

    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="bi-rec-")
        self._ctx = override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=self._root)
        self._ctx.enable()
        self.store = EvidenceStore()
        parsers.clear_registry()
        register_default_parsers()
        self.addCleanup(parsers.clear_registry)
        self.u = User.objects.create_user(username="rc%s" % _uniq(), email="owner@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(user=self.u, name="A", broker_name="TradersWay",
                                                  account_number=_uniq(), is_demo=False, is_active=True)
        # The HISTORICAL state: a ref-LESS external request event (old parser) + its 24.12 REQUESTED withdrawal, whose
        # request EVIDENCE (the confirmation blob) contains the reference; and an ORPHAN completion blob (no event).
        conf_raw = _confirmation_raw(self.REF)
        self.conf_blob, _ = self.store.put(conf_raw, content_type="message/rfc822", source="EMAIL",
                                           received_at=parse_rfc822(conf_raw)["received_at"])
        self.req_ev = BrokerEvent.objects.create(
            event_type="WITHDRAWAL_CONFIRMATION_REQUIRED", transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL,
            broker="TradersWay", occurred_at=None, received_at=self.conf_blob.received_at,
            amount=Decimal("24.12"), currency="USD", owner=self.u, trading_account=self.acct,
            evidence=self.conf_blob, provenance=Provenance.REAL, broker_reference_id="",
            correlation_status=BrokerEvent.CorrelationStatus.CORRELATED)
        self.wd = Withdrawal.objects.create(
            trading_account=self.acct, owner=self.u, broker_reference_id="", amount=Decimal("24.12"), currency="USD",
            requested_at=None, status=Withdrawal.Status.REQUESTED, requested_event=self.req_ev,
            correlation_method=Withdrawal.CorrelationMethod.HEURISTIC, provenance=Provenance.REAL)
        comp_raw = _completion_raw(self.REF)
        self.comp_blob, _ = self.store.put(comp_raw, content_type="message/rfc822", source="EMAIL",
                                           received_at=parse_rfc822(comp_raw)["received_at"])

    def tearDown(self):
        self._ctx.disable()
        shutil.rmtree(self._root, ignore_errors=True)

    def _recover(self, **over):
        kw = dict(owner="owner@x.invalid", evidence_sha=self.comp_blob.sha256, reference=self.REF, execute=True)
        kw.update(over)
        call_command("recover_withdrawal_completion", **kw)

    def test_dry_run_makes_no_changes(self):
        self._recover(execute=False)
        self.assertFalse(BrokerEvent.objects.filter(evidence=self.comp_blob).exists())
        self.wd.refresh_from_db()
        self.assertEqual(self.wd.status, Withdrawal.Status.REQUESTED)

    def test_recovery_links_completion_and_computes_duration(self):
        self._recover()
        comp = BrokerEvent.objects.get(evidence=self.comp_blob, owner=self.u)
        self.assertEqual(comp.event_type, "WITHDRAWAL_COMPLETED")
        self.assertEqual(comp.broker_reference_id, self.REF)
        self.wd.refresh_from_db()
        self.assertEqual(self.wd.status, Withdrawal.Status.COMPLETED)
        self.assertEqual(self.wd.broker_reference_id, self.REF)
        self.assertIsNotNone(self.wd.requested_at)
        self.assertIsNotNone(self.wd.completed_at)
        self.assertEqual(self.wd.duration_seconds(), 33 * 60 + 1)   # 33m01s observed email interval
        self.assertEqual(self.wd.completed_event_id, comp.id)
        # request event (append-only evidence) is UNCHANGED (still ref-less, still CONFIRMATION_REQUIRED)
        self.req_ev.refresh_from_db()
        self.assertEqual(self.req_ev.broker_reference_id, "")
        self.assertEqual(self.req_ev.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")

    def test_recovery_is_idempotent(self):
        self._recover()
        self._recover()   # second run: no-op
        self.assertEqual(BrokerEvent.objects.filter(evidence=self.comp_blob, owner=self.u).count(), 1)
        self.assertEqual(Withdrawal.objects.filter(owner=self.u).count(), 1)
        self.wd.refresh_from_db()
        self.assertEqual(self.wd.status, Withdrawal.Status.COMPLETED)

    def test_ws3_shows_one_sample_not_yet_typical(self):
        self._recover()
        v = withdrawal_speed([self.acct], owner=self.u)
        self.assertEqual(v["headline"]["state"], "insufficient")   # 1 sample → shown, labelled not-yet-typical
        self.assertEqual(v["headline"]["sample_count"], 1)
        self.assertEqual(v["summary"]["completed_with_duration_count"], 1)
        self.assertEqual(len(v["series"]), 1)
        self.assertEqual(v["series"][0]["duration_seconds"], 33 * 60 + 1)
        self.assertEqual(v["series"][0]["match_basis"], "reference")

    def test_refuses_on_reference_mismatch(self):
        with self.assertRaises(CommandError):
            self._recover(reference="9999999")
        self.assertFalse(BrokerEvent.objects.filter(evidence=self.comp_blob).exists())

    def test_refuses_when_no_matching_request(self):
        other = User.objects.create_user(username="ot%s" % _uniq(), email="other@x.invalid", password="x")
        with self.assertRaises(CommandError):   # owner has no withdrawal proving this reference → no invention
            self._recover(owner="other@x.invalid")
        self.assertFalse(BrokerEvent.objects.filter(evidence=self.comp_blob, owner=other).exists())

    def test_refuses_unknown_owner(self):
        with self.assertRaises(CommandError):
            self._recover(owner="nobody@x.invalid")

    def test_refuses_terminal_target(self):
        self.wd.status = Withdrawal.Status.REJECTED
        self.wd.save(update_fields=["status"])
        with self.assertRaises(CommandError):   # a completion must not complete a rejected withdrawal
            self._recover()
        self.assertFalse(BrokerEvent.objects.filter(evidence=self.comp_blob).exists())

    def test_existing_24_12_unchanged_until_recovered(self):
        # Before recovery the 24.12 stays REQUESTED (no fabricated completion).
        self.wd.refresh_from_db()
        self.assertEqual(self.wd.status, Withdrawal.Status.REQUESTED)
        self.assertIsNone(self.wd.completed_at)
