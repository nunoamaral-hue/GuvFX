"""WP3b — mailbox connect/callback/revoke endpoints + standalone ingestion worker + end-to-end synthetic flow.

End-to-end (synthetic, no network, no consent):
  FakeGmail -> GmailMailSource -> EvidenceBlob + BrokerEvent  [owner-scoped attribution]
  SYNTHETIC EXTERNAL_WITHDRAWAL BrokerEvent -> correlation -> Withdrawal -> metrics API
(The two legs are separate because the deterministic TradersWay parser STRUCTURALLY never emits EXTERNAL_WITHDRAWAL
yet — the positive classifier is certified against a genuine external sample, a known pilot dependency — so the
correlation leg is exercised with a labelled-synthetic external event.)
"""
from __future__ import annotations

import datetime
import tempfile
from decimal import Decimal

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from broker_intelligence import gmail_oauth as OAUTH
from broker_intelligence import gmail_source as GS
from broker_intelligence.models import (BrokerEvent, ConnectedMailbox, Provenance, TransactionCategory, Withdrawal)
from broker_intelligence.services import mint_alias_for_account
from trading.models import TradingAccount

User = get_user_model()
_KEY = Fernet.generate_key().decode()
_CRED = tempfile.mkdtemp(prefix="guvfx_mbx_cred_")
_EVID = tempfile.mkdtemp(prefix="guvfx_mbx_evid_")

ENV = dict(
    BROKER_MAILBOX_CONNECT_ENABLED="1",
    GMAIL_OAUTH_CLIENT_ID="CID", GMAIL_OAUTH_CLIENT_SECRET="SEC",
    GMAIL_OAUTH_REDIRECT_URI="https://api.guvfx.com/api/broker-intelligence/mailboxes/callback/",
    BROKER_INTELLIGENCE_CREDENTIAL_KEY=_KEY, BROKER_INTELLIGENCE_CREDENTIAL_ROOT=_CRED,
    BROKER_INTELLIGENCE_EVIDENCE_ROOT=_EVID,
)
_n = 0


def _uniq():
    global _n
    _n += 1
    return "94%04d" % _n


def _acct(user=None, **kw):
    login = _uniq()
    u = user or User.objects.create_user(username="mbx%s" % login, email="%s@x.invalid" % login, password="x")
    d = dict(user=u, name="A", broker_name="TradersWay", account_number=login, is_demo=False, is_active=True)
    d.update(kw)
    return TradingAccount.objects.create(**d)


def _raw_email(to_addr: str) -> bytes:
    return (
        f"From: TradersWay <payments@tradersway.com>\r\nTo: {to_addr}\r\n"
        "Subject: Your withdrawal request has been received\r\n"
        "Date: Wed, 08 Oct 2026 10:00:00 +0000\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "Your withdrawal request has been received for trading account 55442.\nAmount: 200.00 USD\n"
        "Reference: W-ABC123\r\n"
    ).encode("utf-8")


class _FakeGmailClient:
    """Returns one synthetic message addressed to ``to_addr`` and advances the cursor."""
    def __init__(self, to_addr: str, new_cursor: str = "200"):
        self._to, self._cursor = to_addr, new_cursor

    def list_new_messages(self, cursor_state):
        raw = _raw_email(self._to)
        fields = GS.parse_rfc822(raw)
        return [{"provider_message_id": "m1", "raw_bytes": raw, **fields}], self._cursor


class _RaisingGmailClient:
    """Simulates a transient read failure mid-pass (the cursor must NOT advance)."""
    def list_new_messages(self, cursor_state):
        raise RuntimeError("transient gmail api error")


@override_settings(**ENV)
class MailboxIngestE2E(TestCase):
    def setUp(self):
        from broker_intelligence import parsers
        parsers.clear_registry()   # hermetic: ingest_mailbox registers the real parser; don't leak it to other suites
        self.addCleanup(parsers.clear_registry)
        self.u = User.objects.create_user(username="owner", email="owner@x.invalid", password="x")
        self.acct = _acct(user=self.u)
        self.alias = mint_alias_for_account(self.acct)   # ACTIVE bound alias owned by the member
        self.mb = ConnectedMailbox.objects.create(
            user=self.u, provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id="owner@gmail.com",
            credential_ref="cr" + "0" * 32, cursor_state="100", status=ConnectedMailbox.Status.CONNECTED)

    def test_fakegmail_ingests_to_brokerevent_owner_scoped(self):
        from broker_intelligence.mailbox_ingest import ingest_mailbox
        res = ingest_mailbox(self.mb, client=_FakeGmailClient(self.alias.address()))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["events"], 1)
        ev = BrokerEvent.objects.get()
        self.assertEqual(ev.trading_account_id, self.acct.id)          # owner-scoped attribution to the member
        self.assertEqual(ev.provenance, Provenance.REAL)               # the live worker is the REAL ingestion path
        self.assertEqual(ev.event_type, "WITHDRAWAL_REQUESTED")
        self.assertIsNotNone(ev.evidence_id)                           # evidence stored
        self.mb.refresh_from_db()
        self.assertEqual(self.mb.cursor_state, "200")                  # cursor advanced AFTER durable ingestion

    def test_worker_owner_scoping_blocks_cross_user(self):
        from broker_intelligence.mailbox_ingest import ingest_mailbox
        # A victim V with their own alias; a message spoofing V's alias arrives in U's mailbox.
        v = User.objects.create_user(username="victimM", email="vm@x.invalid", password="x")
        aV = _acct(user=v)
        alV = mint_alias_for_account(aV)
        res = ingest_mailbox(self.mb, client=_FakeGmailClient(alV.address()))
        self.assertTrue(res["ok"], res)
        ev = BrokerEvent.objects.get()
        self.assertIsNone(ev.trading_account_id)                       # NOT attributed to the victim (cross-user firewall)

    def test_cursor_not_advanced_on_ingest_failure(self):
        # Invariant 6 (failure path): a mid-pass read/commit failure must NOT advance the cursor — so the next pass
        # safely re-fetches (ingestion is content-hash idempotent) and no message is lost.
        from broker_intelligence.mailbox_ingest import ingest_mailbox
        res = ingest_mailbox(self.mb, client=_RaisingGmailClient())
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "fetch_failed")
        self.mb.refresh_from_db()
        self.assertEqual(self.mb.cursor_state, "100")                  # unchanged — failure never advances the cursor
        self.assertIsNone(self.mb.last_successful_sync)

    def test_external_event_correlates_to_withdrawal_and_metrics(self):
        from broker_intelligence.correlation import run_correlation
        from broker_intelligence.metrics import withdrawal_metrics
        # labelled-SYNTHETIC external-withdrawal event (the parser won't emit EXTERNAL yet; this exercises the
        # correlation->withdrawal->metrics leg honestly).
        BrokerEvent.objects.create(
            trading_account=self.acct, broker="TradersWay", event_type="WITHDRAWAL_REQUESTED",
            transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL, broker_reference_id="W-EXT1",
            amount=Decimal("200.00"), currency="USD", occurred_at=timezone.now() - datetime.timedelta(hours=2),
            received_at=timezone.now(), provenance=Provenance.SYNTHETIC,
            correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)
        BrokerEvent.objects.create(
            trading_account=self.acct, broker="TradersWay", event_type="WITHDRAWAL_COMPLETED",
            transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL, broker_reference_id="W-EXT1",
            amount=Decimal("200.00"), currency="USD", occurred_at=timezone.now(),
            received_at=timezone.now(), provenance=Provenance.SYNTHETIC,
            correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)
        run_correlation()
        self.assertEqual(Withdrawal.objects.filter(trading_account=self.acct).count(), 1)
        m = withdrawal_metrics([self.acct])
        self.assertEqual(m["completed_count"], 1)
        self.assertEqual(m["completed_amount_by_currency"], {"USD": "200.00"})


@override_settings(**ENV)
class MailboxConnectApi(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="c", email="c@x.invalid", password="x")
        self.c = APIClient()
        self.c.force_authenticate(self.u)

    def test_connect_returns_consent_url_with_readonly_scope(self):
        r = self.c.get("/api/broker-intelligence/mailboxes/connect/")
        self.assertEqual(r.status_code, 200, r.content)
        url = r.json()["authorization_url"]
        self.assertIn("accounts.google.com", url)
        self.assertIn(OAUTH.GMAIL_READONLY_SCOPE.replace(":", "%3A").replace("/", "%2F"), url)
        self.assertIn("state=", url)

    def test_callback_verifies_state_stores_token_and_connects(self):
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from broker_intelligence.credential_store import load_token
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        orig_exc, orig_prof = O.exchange_code, S.fetch_profile
        try:
            O.exchange_code = lambda **kw: {"access_token": "AT", "refresh_token": "RT", "expiry": "",
                                            "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
            S.fetch_profile = lambda at, **kw: {"emailAddress": "owner@gmail.com", "historyId": "500"}
            state = signing.dumps({"uid": self.u.id, "n": "x"}, salt=_STATE_SALT)
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C&state={state}")
            self.assertEqual(r.status_code, 200, r.content)
            mb = ConnectedMailbox.objects.get(user=self.u)
            self.assertEqual(mb.status, ConnectedMailbox.Status.CONNECTED)
            self.assertTrue(mb.credential_ref and mb.credential_ref != "AT")   # DB holds a ref, NOT the token
            self.assertEqual(load_token(mb.credential_ref)["access_token"], "AT")  # token only in the encrypted store
            self.assertEqual(mb.cursor_state, "500")
        finally:
            O.exchange_code, S.fetch_profile = orig_exc, orig_prof

    def test_callback_rejects_foreign_or_bad_state(self):
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        # state issued to a DIFFERENT user
        other = signing.dumps({"uid": self.u.id + 999, "n": "x"}, salt=_STATE_SALT)
        self.assertEqual(self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C&state={other}").status_code, 400)
        # garbage/unsigned state
        self.assertEqual(self.c.get("/api/broker-intelligence/mailboxes/callback/?code=C&state=garbage").status_code, 400)

    def test_callback_rejects_expired_state(self):
        # CSRF: a correctly-signed, correctly-attributed state that is too OLD is rejected (replay/stale window).
        from django.core import signing
        import broker_intelligence.views as V
        state = signing.dumps({"uid": self.u.id, "n": "x"}, salt=V._STATE_SALT)
        orig = V._STATE_MAX_AGE
        try:
            V._STATE_MAX_AGE = -1   # any issued token is now older than the window
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C&state={state}")
            self.assertEqual(r.status_code, 400, r.content)
        finally:
            V._STATE_MAX_AGE = orig

    def test_callback_rejects_cross_user_mailbox_without_orphaning_credential(self):
        # A second member completing OAuth for a mailbox already owned by another member must fail CLEANLY (409),
        # and must NOT write an encrypted credential file (no orphan) — the uniqueness is (provider, mailbox id).
        import os
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        other = User.objects.create_user(username="first", email="first@x.invalid", password="x")
        ConnectedMailbox.objects.create(user=other, provider=ConnectedMailbox.Provider.GMAIL,
                                        provider_mailbox_id="shared@gmail.com",
                                        status=ConnectedMailbox.Status.CONNECTED)
        before = len(os.listdir(_CRED))
        orig_exc, orig_prof = O.exchange_code, S.fetch_profile
        try:
            O.exchange_code = lambda **kw: {"access_token": "AT", "refresh_token": "RT", "expiry": "",
                                            "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
            S.fetch_profile = lambda at, **kw: {"emailAddress": "shared@gmail.com", "historyId": "9"}
            state = signing.dumps({"uid": self.u.id, "n": "x"}, salt=_STATE_SALT)
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C&state={state}")
            self.assertEqual(r.status_code, 409, r.content)
        finally:
            O.exchange_code, S.fetch_profile = orig_exc, orig_prof
        self.assertFalse(ConnectedMailbox.objects.filter(user=self.u).exists())   # no row created for the 2nd user
        self.assertEqual(len(os.listdir(_CRED)), before)                          # no orphaned credential written

    def test_revoke_is_owner_scoped_and_destroys_credential(self):
        from broker_intelligence import credential_store as CS
        ref = CS.store_token({"access_token": "REVOKE-ME"})
        mb = ConnectedMailbox.objects.create(user=self.u, provider=ConnectedMailbox.Provider.GMAIL,
                                             provider_mailbox_id="owner@gmail.com", credential_ref=ref,
                                             status=ConnectedMailbox.Status.CONNECTED)
        r = self.c.post(f"/api/broker-intelligence/mailboxes/{mb.id}/revoke/")
        self.assertEqual(r.status_code, 200, r.content)
        mb.refresh_from_db()
        self.assertEqual(mb.status, ConnectedMailbox.Status.REVOKED)
        self.assertEqual(mb.credential_ref, "")
        self.assertIsNone(CS.load_token(ref))           # token destroyed
        # another member cannot revoke it
        other = User.objects.create_user(username="c2", email="c2@x.invalid", password="x")
        oc = APIClient(); oc.force_authenticate(other)
        mb2 = ConnectedMailbox.objects.create(user=self.u, provider=ConnectedMailbox.Provider.GMAIL,
                                              provider_mailbox_id="owner2@gmail.com", status=ConnectedMailbox.Status.CONNECTED)
        self.assertEqual(oc.post(f"/api/broker-intelligence/mailboxes/{mb2.id}/revoke/").status_code, 404)


class MailboxConnectDark(TestCase):
    def test_endpoints_are_404_while_flag_off(self):
        u = User.objects.create_user(username="d", email="d@x.invalid", password="x")
        c = APIClient(); c.force_authenticate(u)
        # revoke targets a REAL mailbox owned by the caller, so the 404 can ONLY be the DARK gate — not a missing
        # row (which would also 404 and let the assertion pass even if the gate were removed).
        mb = ConnectedMailbox.objects.create(user=u, provider=ConnectedMailbox.Provider.GMAIL,
                                             provider_mailbox_id="d@gmail.com",
                                             status=ConnectedMailbox.Status.CONNECTED)
        self.assertEqual(c.get("/api/broker-intelligence/mailboxes/connect/").status_code, 404)
        self.assertEqual(c.get("/api/broker-intelligence/mailboxes/callback/?code=C&state=S").status_code, 404)
        self.assertEqual(c.post(f"/api/broker-intelligence/mailboxes/{mb.id}/revoke/").status_code, 404)
