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
from broker_intelligence.models import (BrokerEvent, ConnectedMailbox, EvidenceBlob, Provenance, TransactionCategory, Withdrawal)
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
    BROKER_INTELLIGENCE_SENDER_ALLOWLIST="tradersway.com",   # R1: only broker-sender mail is acquired/retained
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
        # Gmail-style authenticity header so the R3b auth gate sees a PASS verdict (genuine broker mail).
        "Authentication-Results: mx.google.com; dkim=pass header.d=tradersway.com; spf=pass; dmarc=pass\r\n"
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


def _raw_unparseable(to_addr: str) -> bytes:
    # Allowlisted + authenticated broker mail that NEITHER parser claims (no withdrawal/deposit/transfer content) —>
    # retained as quarantined evidence, no event. Exercises the `no_event` (acquired-but-no-event) summary counter.
    return (
        f"From: TradersWay <news@tradersway.com>\r\nTo: {to_addr}\r\n"
        "Subject: Monthly market newsletter\r\n"
        "Authentication-Results: mx.google.com; dkim=pass header.d=tradersway.com; spf=pass; dmarc=pass\r\n"
        "Date: Wed, 08 Oct 2026 10:00:00 +0000\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "Markets were mixed this week. An informational newsletter with no transaction.\r\n"
    ).encode("utf-8")


class _FakeGmailClientUnparseable:
    """Returns one authenticated, allowlisted broker message that produces NO event (unparseable)."""
    def list_new_messages(self, cursor_state):
        raw = _raw_unparseable("owner@gmail.com")
        return [{"provider_message_id": "mN", "raw_bytes": raw, **GS.parse_rfc822(raw)}], "200"


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

    def test_no_event_messages_surfaced_in_summary(self):
        # Observability: an authenticated, allowlisted broker email that produces NO event (here: unparseable) is
        # retained as quarantined evidence AND surfaced in the summary via `no_event` (not only in the logs), so a
        # rising no-event volume (e.g. a broker template whose parser is crashing) is visible at the run level.
        from broker_intelligence.mailbox_ingest import ingest_mailbox
        res = ingest_mailbox(self.mb, client=_FakeGmailClientUnparseable())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["ingested"], 1)
        self.assertEqual(res["events"], 0)                             # nothing parsed into an event
        self.assertEqual(res["no_event"], 1)                           # acquired-but-no-event volume surfaced
        self.assertEqual(BrokerEvent.objects.count(), 0)
        self.assertEqual(EvidenceBlob.objects.count(), 1)              # evidence retained (quarantined)
        self.mb.refresh_from_db()
        self.assertEqual(self.mb.cursor_state, "200")                  # cursor advances (nothing captured, nothing lost)

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

    def _first_connect(self, O, S, email="owner@gmail.com"):
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        O.exchange_code = lambda **k: {"access_token": "AT1", "refresh_token": "RT1", "expiry": "",
                                       "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
        S.fetch_profile = lambda at, **k: {"emailAddress": email, "historyId": "100"}
        st = signing.dumps({"uid": self.u.id, "n": "a"}, salt=_STATE_SALT)
        self.assertEqual(self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C0&state={st}").status_code, 200)
        mb = ConnectedMailbox.objects.get(user=self.u)
        return mb.id, mb.credential_ref

    def test_reconnect_same_owner_preserves_cursor_id_bindings_and_rotates_credential(self):
        """OAUTH_RECOVERY §5 (positive): same-owner re-consent UPDATES in place — id/cursor/last_sync/BrokerEmailIdentity
        binding preserved, credential rotated to the new token, no duplicate, readonly-only, previous credential kept."""
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        from broker_intelligence.credential_store import load_token
        from broker_intelligence.models import BrokerEmailIdentity
        from broker_intelligence.resolver import canonical_address
        orig = (O.exchange_code, S.fetch_profile)
        try:
            oid, oref = self._first_connect(O, S)
            ConnectedMailbox.objects.filter(pk=oid).update(cursor_state="3204241")
            mb = ConnectedMailbox.objects.get(pk=oid)
            mb.last_successful_sync = timezone.now() - datetime.timedelta(days=2)
            mb.save(update_fields=["last_successful_sync"])
            orig_sync = ConnectedMailbox.objects.get(pk=oid).last_successful_sync
            ident = BrokerEmailIdentity.objects.create(email="owner@googlemail.com", broker_name="TradersWay",
                                                       user=self.u, connected_mailbox=mb,
                                                       status=BrokerEmailIdentity.Status.VERIFIED)
            # RE-CONNECT: same Google mailbox (googlemail alias), NEW token, historyId 999
            O.exchange_code = lambda **k: {"access_token": "AT2", "refresh_token": "RT2", "expiry": "",
                                           "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
            S.fetch_profile = lambda at, **k: {"emailAddress": "owner@googlemail.com", "historyId": "999"}
            st = signing.dumps({"uid": self.u.id, "n": "y"}, salt=_STATE_SALT)
            self.assertEqual(self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C2&state={st}").status_code, 200)
            self.assertEqual(ConnectedMailbox.objects.filter(user=self.u).count(), 1)     # no duplicate
            mb2 = ConnectedMailbox.objects.get(user=self.u)
            self.assertEqual(mb2.id, oid)                                                  # same row
            self.assertEqual(mb2.provider_mailbox_id, canonical_address("owner@gmail.com"))
            self.assertEqual(mb2.cursor_state, "3204241")                                  # cursor PRESERVED (not 999)
            self.assertEqual(mb2.last_successful_sync, orig_sync)                           # last-sync not reset
            self.assertTrue(mb2.credential_ref and mb2.credential_ref != oref)             # credential rotated
            self.assertEqual(load_token(mb2.credential_ref)["access_token"], "AT2")
            self.assertEqual(mb2.scopes, OAUTH.GMAIL_READONLY_SCOPE)                        # readonly only
            ident.refresh_from_db()
            self.assertEqual(ident.connected_mailbox_id, oid)                              # binding preserved
            self.assertEqual(load_token(oref)["access_token"], "AT1")                      # prev credential kept
        finally:
            O.exchange_code, S.fetch_profile = orig

    def test_reconnect_rejects_token_without_refresh_token_preserving_existing(self):
        """OAUTH_RECOVERY §6 / §5(3): a replacement grant lacking a refresh_token is REJECTED (400) and the existing
        usable credential + cursor are NOT overwritten (unattended ingestion needs a refresh_token)."""
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        from broker_intelligence.credential_store import load_token
        orig = (O.exchange_code, S.fetch_profile)
        try:
            oid, oref = self._first_connect(O, S)
            ConnectedMailbox.objects.filter(pk=oid).update(cursor_state="777")
            O.exchange_code = lambda **k: {"access_token": "AT2", "refresh_token": "", "expiry": "",
                                           "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
            S.fetch_profile = lambda at, **k: {"emailAddress": "owner@gmail.com", "historyId": "999"}
            st = signing.dumps({"uid": self.u.id, "n": "b"}, salt=_STATE_SALT)
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C2&state={st}")
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["error"], "oauth_no_refresh_token")
            mb = ConnectedMailbox.objects.get(pk=oid)
            self.assertEqual(mb.credential_ref, oref)                                      # NOT overwritten
            self.assertEqual(mb.cursor_state, "777")                                       # cursor preserved
            self.assertEqual(load_token(oref)["access_token"], "AT1")                      # old credential still usable
        finally:
            O.exchange_code, S.fetch_profile = orig

    def test_reconnect_rejects_insufficient_scope_preserving_existing(self):
        """OAUTH_RECOVERY §6 / §5(4,5): a replacement grant missing gmail.readonly is REJECTED (400); existing
        credential preserved."""
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        orig = (O.exchange_code, S.fetch_profile)
        try:
            oid, oref = self._first_connect(O, S)
            O.exchange_code = lambda **k: {"access_token": "AT2", "refresh_token": "RT2", "expiry": "",
                                           "scope": "openid email", "token_type": "Bearer"}   # no gmail.readonly
            S.fetch_profile = lambda at, **k: {"emailAddress": "owner@gmail.com", "historyId": "999"}
            st = signing.dumps({"uid": self.u.id, "n": "c"}, salt=_STATE_SALT)
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C2&state={st}")
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["error"], "oauth_scope_insufficient")
            self.assertEqual(ConnectedMailbox.objects.get(pk=oid).credential_ref, oref)    # preserved
        finally:
            O.exchange_code, S.fetch_profile = orig

    def test_connect_rejects_token_without_mailbox_identity(self):
        """OAUTH_RECOVERY §5(I): a profile with no emailAddress is REJECTED (400) — never creates a random-id duplicate."""
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        orig = (O.exchange_code, S.fetch_profile)
        try:
            O.exchange_code = lambda **k: {"access_token": "AT", "refresh_token": "RT", "expiry": "",
                                           "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
            S.fetch_profile = lambda at, **k: {"emailAddress": "", "historyId": "100"}       # no identity
            st = signing.dumps({"uid": self.u.id, "n": "d"}, salt=_STATE_SALT)
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C&state={st}")
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["error"], "oauth_no_mailbox_identity")
            self.assertEqual(ConnectedMailbox.objects.filter(user=self.u).count(), 0)       # no duplicate/random mailbox
        finally:
            O.exchange_code, S.fetch_profile = orig

    def test_connect_binds_expected_identity_on_reconnect_only(self):
        """R2 §3: the connect state binds the EXISTING CONNECTED mailbox's canonical identity as `expect` (targeted
        reconnect); a first-time connect (no existing mailbox) binds no target."""
        import urllib.parse as U
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT, _STATE_MAX_AGE
        r1 = self.c.get("/api/broker-intelligence/mailboxes/connect/")
        self.assertEqual(r1.status_code, 200, r1.content)
        st1 = U.parse_qs(U.urlparse(r1.json()["authorization_url"]).query)["state"][0]
        self.assertNotIn("expect", signing.loads(st1, salt=_STATE_SALT, max_age=_STATE_MAX_AGE))   # first connect
        ConnectedMailbox.objects.create(user=self.u, provider=ConnectedMailbox.Provider.GMAIL,
                                        provider_mailbox_id="owner@gmail.com", primary_email="owner@gmail.com",
                                        credential_ref="cr" + "0" * 32, status=ConnectedMailbox.Status.CONNECTED)
        r2 = self.c.get("/api/broker-intelligence/mailboxes/connect/")
        st2 = U.parse_qs(U.urlparse(r2.json()["authorization_url"]).query)["state"][0]
        self.assertEqual(signing.loads(st2, salt=_STATE_SALT, max_age=_STATE_MAX_AGE).get("expect"), "owner@gmail.com")

    def test_reconnect_wrong_account_rejected_preserving_existing(self):
        """R2 §3 / §4(A): a reconnect whose consent returns a DIFFERENT Google account is rejected (400
        oauth_account_mismatch) — no duplicate mailbox, existing credential + cursor preserved."""
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        from broker_intelligence.credential_store import load_token
        orig = (O.exchange_code, S.fetch_profile)
        try:
            oid, oref = self._first_connect(O, S, email="owner@gmail.com")
            ConnectedMailbox.objects.filter(pk=oid).update(cursor_state="3204241")
            # state targets owner@gmail.com (as the connect view would bind), but consent returns a DIFFERENT account
            O.exchange_code = lambda **k: {"access_token": "ATX", "refresh_token": "RTX", "expiry": "",
                                           "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
            S.fetch_profile = lambda at, **k: {"emailAddress": "intruder@gmail.com", "historyId": "999"}
            st = signing.dumps({"uid": self.u.id, "n": "w", "expect": "owner@gmail.com"}, salt=_STATE_SALT)
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=CX&state={st}")
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["error"], "oauth_account_mismatch")
            self.assertEqual(ConnectedMailbox.objects.filter(user=self.u).count(), 1)    # no duplicate created
            mb = ConnectedMailbox.objects.get(pk=oid)
            self.assertEqual(mb.credential_ref, oref)                                     # existing credential preserved
            self.assertEqual(mb.cursor_state, "3204241")                                  # cursor preserved
            self.assertEqual(load_token(oref)["access_token"], "AT1")
        finally:
            O.exchange_code, S.fetch_profile = orig

    def test_reconnect_matching_account_with_expect_updates_in_place(self):
        """R2 §3 positive: a reconnect whose consent returns the EXPECTED account (even via the googlemail alias)
        passes the targeted-identity check and updates in place (cursor preserved)."""
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        orig = (O.exchange_code, S.fetch_profile)
        try:
            oid, oref = self._first_connect(O, S, email="owner@gmail.com")
            ConnectedMailbox.objects.filter(pk=oid).update(cursor_state="3204241")
            O.exchange_code = lambda **k: {"access_token": "AT2", "refresh_token": "RT2", "expiry": "",
                                           "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
            S.fetch_profile = lambda at, **k: {"emailAddress": "owner@googlemail.com", "historyId": "999"}  # same account
            st = signing.dumps({"uid": self.u.id, "n": "m", "expect": "owner@gmail.com"}, salt=_STATE_SALT)
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=CM&state={st}")
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(ConnectedMailbox.objects.filter(user=self.u).count(), 1)
            mb = ConnectedMailbox.objects.get(pk=oid)
            self.assertEqual(mb.id, oid)
            self.assertEqual(mb.cursor_state, "3204241")                                  # preserved
            self.assertTrue(mb.credential_ref and mb.credential_ref != oref)              # rotated
        finally:
            O.exchange_code, S.fetch_profile = orig

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
