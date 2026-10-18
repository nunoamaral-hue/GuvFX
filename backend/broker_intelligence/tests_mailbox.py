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
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from broker_intelligence import gmail_oauth as OAUTH
from broker_intelligence import gmail_source as GS
from broker_intelligence.models import (BrokerEmailIdentity, BrokerEvent, ConnectedMailbox, EvidenceBlob, Provenance,
                                        TransactionCategory, Withdrawal)
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

    def test_reconnect_same_owner_preserves_cursor_id_bindings_and_rotates_credential(self):
        """OAUTH_RECOVERY §3: a same-owner re-consent (the recovery path after a refresh-token expiry) MUST update the
        existing mailbox in place — preserving its id, the in-progress cursor, last_successful_sync, and broker-identity
        bindings — while rotating to the NEW valid credential. It must NOT create a duplicate, reset the cursor to
        'now', widen scope, or destroy the previous credential before the new one is proven usable."""
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        from broker_intelligence.credential_store import load_token
        from broker_intelligence.models import BrokerEmailIdentity
        from broker_intelligence.resolver import canonical_address
        orig_exc, orig_prof = O.exchange_code, S.fetch_profile
        try:
            # (initial connect -> create; historyId 100 anchors the cursor on create only)
            O.exchange_code = lambda **kw: {"access_token": "AT1", "refresh_token": "RT1", "expiry": "",
                                            "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
            S.fetch_profile = lambda at, **kw: {"emailAddress": "owner@gmail.com", "historyId": "100"}
            st = signing.dumps({"uid": self.u.id, "n": "x"}, salt=_STATE_SALT)
            self.assertEqual(self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C1&state={st}").status_code, 200)
            mb = ConnectedMailbox.objects.get(user=self.u)
            orig_id, orig_ref = mb.id, mb.credential_ref
            # simulate an in-progress worker state + a verified broker-identity binding
            mb.cursor_state = "3204241"
            mb.last_successful_sync = timezone.now() - datetime.timedelta(days=2)
            mb.save(update_fields=["cursor_state", "last_successful_sync"])
            orig_sync = ConnectedMailbox.objects.get(pk=orig_id).last_successful_sync
            ident = BrokerEmailIdentity.objects.create(email="owner@googlemail.com", broker_name="TradersWay",
                                                       user=self.u, connected_mailbox=mb,
                                                       status=BrokerEmailIdentity.Status.VERIFIED)
            # (RE-CONNECT -> update: SAME Google mailbox via the googlemail alias, NEW token, historyId now 999)
            O.exchange_code = lambda **kw: {"access_token": "AT2", "refresh_token": "RT2", "expiry": "",
                                            "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
            S.fetch_profile = lambda at, **kw: {"emailAddress": "owner@googlemail.com", "historyId": "999"}
            st2 = signing.dumps({"uid": self.u.id, "n": "y"}, salt=_STATE_SALT)
            self.assertEqual(self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C2&state={st2}").status_code, 200)
            self.assertEqual(ConnectedMailbox.objects.filter(user=self.u).count(), 1)          # (8) no duplicate
            mb2 = ConnectedMailbox.objects.get(user=self.u)
            self.assertEqual(mb2.id, orig_id)                                                   # (1) same row
            self.assertEqual(mb2.provider_mailbox_id, canonical_address("owner@gmail.com"))     # (3) stable canonical id
            self.assertEqual(mb2.cursor_state, "3204241")                                       # (4) cursor PRESERVED (not 999)
            self.assertEqual(mb2.last_successful_sync, orig_sync)                               # (5) last-sync not reset
            self.assertEqual(mb2.status, ConnectedMailbox.Status.CONNECTED)
            self.assertTrue(mb2.credential_ref and mb2.credential_ref != orig_ref)              # (12) credential rotated
            self.assertEqual(load_token(mb2.credential_ref)["access_token"], "AT2")             # new grant usable
            self.assertEqual(mb2.scopes, OAUTH.GMAIL_READONLY_SCOPE)                             # (9/10) readonly only
            ident.refresh_from_db()
            self.assertEqual(ident.connected_mailbox_id, orig_id)                               # (6/7) binding preserved
            self.assertEqual(load_token(orig_ref)["access_token"], "AT1")                       # prev credential not destroyed
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

    def test_reconnect_revoked_while_another_mailbox_is_live_refused(self):
        """Adversarial (§505.3): revoke A, connect B, then reconnect A must NOT reactivate A into a SECOND live
        mailbox. The single-mailbox guard runs on reconnect too (callback mutation point). Exactly one CONNECTED
        mailbox survives, and A stays REVOKED."""
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        from broker_intelligence.resolver import canonical_address
        orig = (O.exchange_code, S.fetch_profile)
        try:
            aid, _oref = self._first_connect(O, S, email="owner@gmail.com")        # A connected
            a = ConnectedMailbox.objects.get(pk=aid)
            a.status = ConnectedMailbox.Status.REVOKED
            a.credential_ref = ""
            a.save(update_fields=["status", "credential_ref"])                      # A revoked
            # B connected (allowed — no live mailbox while A is revoked)
            O.exchange_code = lambda **k: {"access_token": "ATB", "refresh_token": "RTB", "expiry": "",
                                           "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
            S.fetch_profile = lambda at, **k: {"emailAddress": "second@gmail.com", "historyId": "200"}
            stb = signing.dumps({"uid": self.u.id, "n": "b", "op": "CONNECT_FIRST_MAILBOX"}, salt=_STATE_SALT)
            self.assertEqual(
                self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=CB&state={stb}").status_code, 200)
            # reconnect A (its revoked mailbox) while B is live -> MUST be refused (would be a 2nd live mailbox)
            O.exchange_code = lambda **k: {"access_token": "ATA2", "refresh_token": "RTA2", "expiry": "",
                                           "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
            S.fetch_profile = lambda at, **k: {"emailAddress": "owner@gmail.com", "historyId": "300"}
            sta = signing.dumps({"uid": self.u.id, "n": "a2", "op": "RECONNECT_EXISTING_MAILBOX",
                                 "mailbox_pk": aid, "expect": canonical_address("owner@gmail.com")}, salt=_STATE_SALT)
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=CA2&state={sta}")
            self.assertEqual(r.status_code, 409, r.content)
            self.assertEqual(r.json()["error"], "single_mailbox_only")
            self.assertEqual(ConnectedMailbox.objects.filter(
                user=self.u, status=ConnectedMailbox.Status.CONNECTED).count(), 1)       # only B
            self.assertEqual(ConnectedMailbox.objects.get(pk=aid).status,
                             ConnectedMailbox.Status.REVOKED)                             # A NOT reactivated
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


@override_settings(BROKER_MAILBOX_STATUS_ENABLED="1")
class MailboxStatusApi(TestCase):
    """Owner-scoped, customer-safe mailbox status API (§6). DARK by its own flag; HEALTHY only on real last-success
    evidence; never leaks a token/credential/cursor/email body or another member's mailbox."""

    def setUp(self):
        self.u = User.objects.create_user(username="su", email="su@x.invalid", password="x")
        self.c = APIClient()
        self.c.force_authenticate(self.u)

    def _mb(self, user, pmid="owner@gmail.com", **kw):
        d = dict(user=user, provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id=pmid,
                 primary_email=pmid, credential_ref="cr" + "0" * 32, cursor_state="100",
                 status=ConnectedMailbox.Status.CONNECTED)
        d.update(kw)
        return ConnectedMailbox.objects.create(**d)

    def test_status_404_while_flag_off(self):
        self._mb(self.u)
        with override_settings(BROKER_MAILBOX_STATUS_ENABLED="0"):
            self.assertEqual(self.c.get("/api/broker-intelligence/mailboxes/status/").status_code, 404)

    def test_connected_no_sync_is_connected_not_healthy(self):
        self._mb(self.u, last_successful_sync=None)
        r = self.c.get("/api/broker-intelligence/mailboxes/status/")
        self.assertEqual(r.status_code, 200)
        item = r.json()["mailboxes"][0]
        self.assertEqual(item["state"], "CONNECTED")      # NOT 'HEALTHY' on zero success evidence
        self.assertEqual(item["connection_status"], "CONNECTED")

    def test_recent_sync_is_healthy(self):
        self._mb(self.u, last_successful_sync=timezone.now())
        item = self.c.get("/api/broker-intelligence/mailboxes/status/").json()["mailboxes"][0]
        self.assertEqual(item["state"], "HEALTHY")

    def test_stale_sync_is_degraded_not_reconnect(self):
        # §4: a merely-STALE mailbox (a late/slow ingestion pass) is DEGRADED, NEVER RECONNECT_REQUIRED — a member
        # must not be told to re-authorize Gmail just because a pass ran late. Reconnect needs explicit OAuth evidence.
        self._mb(self.u, last_successful_sync=timezone.now() - datetime.timedelta(days=100))
        item = self.c.get("/api/broker-intelligence/mailboxes/status/").json()["mailboxes"][0]
        self.assertEqual(item["state"], "DEGRADED")
        self.assertFalse(item["reconnect_required"])

    def test_revoked_status_is_revoked(self):
        self._mb(self.u, status=ConnectedMailbox.Status.REVOKED, credential_ref="")
        item = self.c.get("/api/broker-intelligence/mailboxes/status/").json()["mailboxes"][0]
        self.assertEqual(item["state"], "REVOKED")
        self.assertTrue(item["reconnect_required"])

    def test_owner_scoped_excludes_other_member(self):
        self._mb(self.u, pmid="owner@gmail.com")
        other = User.objects.create_user(username="other", email="other@x.invalid", password="x")
        self._mb(other, pmid="other@gmail.com")
        data = self.c.get("/api/broker-intelligence/mailboxes/status/").json()
        self.assertEqual(len(data["mailboxes"]), 1)       # only the member's own mailbox
        self.assertNotIn("other@gmail.com", self.c.get("/api/broker-intelligence/mailboxes/status/").content.decode())

    def test_response_has_no_secrets(self):
        self._mb(self.u, last_successful_sync=timezone.now())
        body = self.c.get("/api/broker-intelligence/mailboxes/status/").content.decode()
        for forbidden in ("credential_ref", "cr00000000", "cursor", "access_token", "refresh_token"):
            self.assertNotIn(forbidden, body, forbidden)
        self.assertIn("masked_email", body)
        self.assertNotIn("owner@gmail.com", body)         # full mailbox address never returned

    def test_verified_broker_binding_requires_owned_account_not_email_alone(self):
        # §5: a verified broker BINDING requires a VERIFIED identity with a NON-NULL trading account owned by THIS
        # member AND associated with this mailbox. Canonical email ownership ALONE is insufficient.
        self._mb(self.u, last_successful_sync=timezone.now())
        url = "/api/broker-intelligence/mailboxes/status/"
        self.assertFalse(self.c.get(url).json()["mailboxes"][0]["has_verified_broker_bindings"])
        # VERIFIED identity, canonical googlemail->gmail match to the mailbox, but NO trading account -> still False.
        idn = BrokerEmailIdentity.objects.create(email="owner@googlemail.com", broker_name="TradersWay", user=self.u,
                                                 trading_account=None, status=BrokerEmailIdentity.Status.VERIFIED)
        self.assertFalse(self.c.get(url).json()["mailboxes"][0]["has_verified_broker_bindings"])
        # Bind it to one of THIS member's OWN trading accounts -> now a genuine verified broker binding.
        idn.trading_account = _acct(user=self.u)
        idn.save()
        self.assertTrue(self.c.get(url).json()["mailboxes"][0]["has_verified_broker_bindings"])


@override_settings(**ENV)
class MailboxConnectOps(TestCase):
    """§6 — explicit, signed-state-bound connect OPERATIONS (first/reconnect/add) + V1 single-mailbox enforcement,
    and §7 — the browser return-journey. Reconnection is always a TARGETED update of one named, OWNED mailbox; a
    first/add connect can never create a second live mailbox; the callback redirect lands on the FIXED destination
    and never forwards the Google code/token/state."""

    def setUp(self):
        self.u = User.objects.create_user(username="co", email="co@x.invalid", password="x")
        self.c = APIClient(); self.c.force_authenticate(self.u)

    # --- helpers --------------------------------------------------------------------------------------------------
    def _state_of(self, resp):
        import urllib.parse as U
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT, _STATE_MAX_AGE
        st = U.parse_qs(U.urlparse(resp.json()["authorization_url"]).query)["state"][0]
        return signing.loads(st, salt=_STATE_SALT, max_age=_STATE_MAX_AGE)

    def _mb(self, pmid="owner@gmail.com", **kw):
        d = dict(user=self.u, provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id=pmid,
                 primary_email=pmid, credential_ref="cr" + "0" * 32, status=ConnectedMailbox.Status.CONNECTED)
        d.update(kw)
        return ConnectedMailbox.objects.create(**d)

    # --- §6 connect operations ------------------------------------------------------------------------------------
    def test_first_connect_binds_op_and_no_target(self):
        r = self.c.get("/api/broker-intelligence/mailboxes/connect/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["op"], "CONNECT_FIRST_MAILBOX")
        self.assertIsNone(r.json()["mailbox_id"])
        p = self._state_of(r)
        self.assertEqual(p["op"], "CONNECT_FIRST_MAILBOX")
        self.assertNotIn("expect", p)
        self.assertNotIn("mailbox_pk", p)

    def test_inferred_reconnect_of_single_live_mailbox_binds_expect(self):
        mb = self._mb()
        r = self.c.get("/api/broker-intelligence/mailboxes/connect/")
        self.assertEqual(r.json()["op"], "RECONNECT_EXISTING_MAILBOX")
        self.assertEqual(r.json()["mailbox_id"], mb.id)
        p = self._state_of(r)
        self.assertEqual(p["op"], "RECONNECT_EXISTING_MAILBOX")
        self.assertEqual(p["mailbox_pk"], mb.id)
        self.assertEqual(p["expect"], "owner@gmail.com")

    def test_explicit_reconnect_targets_named_owned_mailbox(self):
        mb = self._mb()
        r = self.c.get(f"/api/broker-intelligence/mailboxes/connect/?op=RECONNECT_EXISTING_MAILBOX&mailbox_id={mb.id}")
        self.assertEqual(r.status_code, 200, r.content)
        p = self._state_of(r)
        self.assertEqual(p["op"], "RECONNECT_EXISTING_MAILBOX")
        self.assertEqual(p["mailbox_pk"], mb.id)
        self.assertEqual(p["expect"], "owner@gmail.com")

    def test_reconnect_requires_mailbox_id(self):
        r = self.c.get("/api/broker-intelligence/mailboxes/connect/?op=RECONNECT_EXISTING_MAILBOX")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "reconnect_requires_mailbox")

    def test_reconnect_foreign_mailbox_is_404(self):
        other = User.objects.create_user(username="co2", email="co2@x.invalid", password="x")
        foreign = ConnectedMailbox.objects.create(user=other, provider=ConnectedMailbox.Provider.GMAIL,
                                                  provider_mailbox_id="foreign@gmail.com",
                                                  status=ConnectedMailbox.Status.CONNECTED)
        r = self.c.get(f"/api/broker-intelligence/mailboxes/connect/?mailbox_id={foreign.id}")
        self.assertEqual(r.status_code, 404)   # owner-scoped: indistinguishable from 'does not exist'

    def test_reconnect_revoked_mailbox_allowed(self):
        mb = self._mb(status=ConnectedMailbox.Status.REVOKED, credential_ref="")
        r = self.c.get(f"/api/broker-intelligence/mailboxes/connect/?mailbox_id={mb.id}")
        self.assertEqual(r.status_code, 200, r.content)   # a revoked mailbox may be reconnected by id (none live)
        self.assertEqual(self._state_of(r)["op"], "RECONNECT_EXISTING_MAILBOX")

    def test_reconnect_refused_at_authorization_when_a_different_mailbox_is_live(self):
        # §505.3 at authorization: revoke A, B live -> a reconnect of A is refused BEFORE any Google round-trip,
        # because reactivating A would leave two live mailboxes.
        a = self._mb(pmid="a@gmail.com", status=ConnectedMailbox.Status.REVOKED, credential_ref="")
        self._mb(pmid="b@gmail.com")   # B CONNECTED (live)
        r = self.c.get(f"/api/broker-intelligence/mailboxes/connect/?mailbox_id={a.id}")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "single_mailbox_only")

    def test_first_connect_refused_when_live_mailbox_exists(self):
        self._mb()
        r = self.c.get("/api/broker-intelligence/mailboxes/connect/?op=CONNECT_FIRST_MAILBOX")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "single_mailbox_only")

    def test_add_new_refused_when_live_mailbox_exists(self):
        self._mb()
        r = self.c.get("/api/broker-intelligence/mailboxes/connect/?op=ADD_NEW_MAILBOX")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "single_mailbox_only")

    def test_invalid_op_rejected(self):
        r = self.c.get("/api/broker-intelligence/mailboxes/connect/?op=DELETE_EVERYTHING")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_op")

    def test_invalid_mailbox_id_rejected(self):
        r = self.c.get("/api/broker-intelligence/mailboxes/connect/?mailbox_id=notanumber")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_mailbox_id")

    def test_callback_single_mailbox_guard_blocks_second_account(self):
        # A non-reconnect (first-connect) state whose consent returns a DIFFERENT account, while a live mailbox already
        # exists, is refused at the mutation point — no second mailbox is ever created (V1 single-mailbox).
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        self._mb(pmid="owner@gmail.com")
        orig = (O.exchange_code, S.fetch_profile)
        try:
            O.exchange_code = lambda **k: {"access_token": "AT", "refresh_token": "RT", "expiry": "",
                                           "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
            S.fetch_profile = lambda at, **k: {"emailAddress": "second@gmail.com", "historyId": "9"}
            st = signing.dumps({"uid": self.u.id, "n": "x", "op": "CONNECT_FIRST_MAILBOX"}, salt=_STATE_SALT)
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C&state={st}")
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.json()["error"], "single_mailbox_only")
        finally:
            O.exchange_code, S.fetch_profile = orig
        self.assertEqual(ConnectedMailbox.objects.filter(user=self.u).count(), 1)   # no second mailbox

    # --- §7 browser return journey --------------------------------------------------------------------------------
    def _fake_ok(self, O, S, email="owner@gmail.com"):
        O.exchange_code = lambda **k: {"access_token": "AT", "refresh_token": "RT", "expiry": "",
                                       "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"}
        S.fetch_profile = lambda at, **k: {"emailAddress": email, "historyId": "100"}

    def test_web_journey_success_redirects_to_frontend_origin(self):
        # §505.2: a browser success lands on the GuvFX FRONTEND origin (guvfx.com) with a NON-sensitive outcome marker
        # — never a relative path (which would resolve to the API origin that serves this callback), never a token.
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        orig = (O.exchange_code, S.fetch_profile)
        try:
            self._fake_ok(O, S)
            st = signing.dumps({"uid": self.u.id, "n": "x", "op": "CONNECT_FIRST_MAILBOX", "rt": True}, salt=_STATE_SALT)
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=SECRETCODE&state={st}")
            self.assertEqual(r.status_code, 302)
            loc = r["Location"]
            self.assertTrue(loc.startswith("https://guvfx.com/profile"), loc)   # FRONTEND origin, not relative/API
            self.assertFalse(loc.startswith("/profile"))                        # never a bare relative path
            self.assertIn("mailbox_connect=connected", loc)
            for leak in ("SECRETCODE", "access_token", "refresh_token", "state=", "RT"):
                self.assertNotIn(leak, loc, leak)                               # code/token/state never forwarded
        finally:
            O.exchange_code, S.fetch_profile = orig

    def test_web_journey_error_redirects_with_outcome_marker(self):
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        orig = (O.exchange_code, S.fetch_profile)
        try:
            self._fake_ok(O, S, email="intruder@gmail.com")
            # a reconnect targeting owner@gmail.com, but consent returns a different account -> error, web journey
            st = signing.dumps({"uid": self.u.id, "n": "w", "op": "RECONNECT_EXISTING_MAILBOX",
                                "expect": "owner@gmail.com", "rt": True}, salt=_STATE_SALT)
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C&state={st}")
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r["Location"].startswith("https://guvfx.com/profile"))
            self.assertIn("mailbox_connect=oauth_account_mismatch", r["Location"])
        finally:
            O.exchange_code, S.fetch_profile = orig

    def test_consent_denied_web_journey_redirects(self):
        # §505.1: the member declining Google consent (error=access_denied, no code) -> customer-safe denied outcome
        # on the frontend; Google's raw error string is NEVER echoed.
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        st = signing.dumps({"uid": self.u.id, "n": "d", "op": "CONNECT_FIRST_MAILBOX", "rt": True}, salt=_STATE_SALT)
        r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?error=access_denied&state={st}")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("https://guvfx.com/profile"))
        self.assertIn("mailbox_connect=consent_denied", r["Location"])
        self.assertNotIn("access_denied", r["Location"])

    def test_consent_denied_api_returns_json(self):
        r = self.c.get("/api/broker-intelligence/mailboxes/callback/?error=access_denied")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "consent_denied")

    def test_reconnect_target_missing_rejected_before_exchange(self):
        # §505.3: a reconnect whose targeted mailbox no longer exists / is not owned is refused at the callback BEFORE
        # any code exchange — never silently creating a new mailbox under a reconnect intent.
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        orig = (O.exchange_code, S.fetch_profile)
        try:
            self._fake_ok(O, S)
            st = signing.dumps({"uid": self.u.id, "n": "m", "op": "RECONNECT_EXISTING_MAILBOX",
                                "mailbox_pk": 999999, "expect": "owner@gmail.com"}, salt=_STATE_SALT)
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C&state={st}")
            self.assertEqual(r.status_code, 404)
            self.assertEqual(r.json()["error"], "reconnect_target_missing")
        finally:
            O.exchange_code, S.fetch_profile = orig

    def test_api_journey_without_rt_returns_json(self):
        # Back-compat: a callback WITHOUT the return-journey flag keeps the JSON contract (operator/API).
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        orig = (O.exchange_code, S.fetch_profile)
        try:
            self._fake_ok(O, S)
            st = signing.dumps({"uid": self.u.id, "n": "x", "op": "CONNECT_FIRST_MAILBOX"}, salt=_STATE_SALT)
            r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C&state={st}")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])
        finally:
            O.exchange_code, S.fetch_profile = orig

    def test_default_return_url_is_frontend_origin_not_api(self):
        # §505.2: with NO explicit BROKER_MAILBOX_CONNECT_RETURN_URL, the destination is FRONTEND_BASE_URL + /profile
        # (the frontend origin), never the relative /profile that would resolve against the API host.
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        orig = (O.exchange_code, S.fetch_profile)
        try:
            self._fake_ok(O, S)
            st = signing.dumps({"uid": self.u.id, "n": "x", "op": "CONNECT_FIRST_MAILBOX", "rt": True}, salt=_STATE_SALT)
            with override_settings(BROKER_MAILBOX_CONNECT_RETURN_URL="", FRONTEND_BASE_URL="https://guvfx.com"):
                r = self.c.get(f"/api/broker-intelligence/mailboxes/callback/?code=C&state={st}")
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r["Location"].startswith("https://guvfx.com/profile"), r["Location"])
            self.assertFalse(r["Location"].startswith("/profile"))
        finally:
            O.exchange_code, S.fetch_profile = orig

    def test_return_destination_is_configured_not_request_controlled(self):
        # Open-redirect guard: a configured destination is honoured; a ?next= in the request is IGNORED.
        import broker_intelligence.gmail_oauth as O
        import broker_intelligence.gmail_source as S
        from django.core import signing
        from broker_intelligence.views import _STATE_SALT
        orig = (O.exchange_code, S.fetch_profile)
        try:
            self._fake_ok(O, S)
            st = signing.dumps({"uid": self.u.id, "n": "x", "op": "CONNECT_FIRST_MAILBOX", "rt": True}, salt=_STATE_SALT)
            with override_settings(BROKER_MAILBOX_CONNECT_RETURN_URL="https://guvfx.com/profile"):
                r = self.c.get("/api/broker-intelligence/mailboxes/callback/"
                               f"?code=C&state={st}&next=https://evil.example/steal")
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r["Location"].startswith("https://guvfx.com/profile"), r["Location"])
            self.assertNotIn("evil.example", r["Location"])
        finally:
            O.exchange_code, S.fetch_profile = orig


@override_settings(BROKER_MAILBOX_STATUS_ENABLED="1")
class MailboxStatusApiR2(TestCase):
    """§3 schema-0010 safety, §4 health-state mapping (explicit OAuth evidence only), §5 verified-binding + connected
    semantics, plus adversarial checks (cross-member, false states, flag bypass)."""

    def setUp(self):
        self.u = User.objects.create_user(username="sr", email="sr@x.invalid", password="x")
        self.c = APIClient(); self.c.force_authenticate(self.u)

    def _mb(self, user=None, pmid="owner@gmail.com", **kw):
        d = dict(user=user or self.u, provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id=pmid,
                 primary_email=pmid, credential_ref="cr" + "0" * 32, cursor_state="100",
                 status=ConnectedMailbox.Status.CONNECTED)
        d.update(kw)
        return ConnectedMailbox.objects.create(**d)

    # --- §3 schema-0010 safety ------------------------------------------------------------------------------------
    def test_schema_0010_projection_omits_r1_columns(self):
        from unittest.mock import patch
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self._mb(last_successful_sync=timezone.now() - datetime.timedelta(days=100))
        # Simulate the UNMIGRATED (0010) production schema: the R1 health columns are reported absent. The status
        # query MUST then select only the stable 0010 columns and never reference an R1 column.
        with patch("broker_intelligence.health.r1_columns_present", return_value=False):
            with CaptureQueriesContext(connection) as cap:
                r = self.c.get("/api/broker-intelligence/mailboxes/status/")
            self.assertEqual(r.status_code, 200, r.content)
        mbox_selects = [q["sql"] for q in cap.captured_queries
                        if "broker_intelligence_connectedmailbox" in q["sql"] and q["sql"].lstrip().upper().startswith("SELECT")]
        self.assertTrue(mbox_selects)
        for sql in mbox_selects:
            for col in ("consecutive_failures", "last_error_code", "last_error_http_status", "last_error_at",
                        "last_attempted_sync", "last_pass_cursor_advanced"):
                self.assertNotIn(col, sql, f"{col} must not be selected on schema 0010")
        # staleness still classifies -> DEGRADED (never an invented OAuth diagnosis on 0010)
        self.assertEqual(r.json()["mailboxes"][0]["state"], "DEGRADED")

    def test_schema_0010_never_reconnect_required_without_error_status(self):
        from unittest.mock import patch
        self._mb(last_successful_sync=timezone.now() - datetime.timedelta(days=100),
                 last_error_code="GMAIL_HTTP_401")   # the column VALUE exists in-row but must be ignored on 0010
        with patch("broker_intelligence.health.r1_columns_present", return_value=False):
            item = self.c.get("/api/broker-intelligence/mailboxes/status/").json()["mailboxes"][0]
        self.assertEqual(item["state"], "DEGRADED")      # the 0011 error code is NOT read on 0010
        self.assertFalse(item["reconnect_required"])

    # --- §4 health-state mapping (0011 present) -------------------------------------------------------------------
    def test_blocking_oauth_code_is_reconnect_required(self):
        self._mb(last_successful_sync=timezone.now() - datetime.timedelta(minutes=30),
                 last_error_code="GMAIL_HTTP_401", consecutive_failures=3)
        item = self.c.get("/api/broker-intelligence/mailboxes/status/").json()["mailboxes"][0]
        self.assertEqual(item["state"], "RECONNECT_REQUIRED")
        self.assertTrue(item["reconnect_required"])

    def test_non_oauth_blocking_code_is_degraded_not_reconnect(self):
        # GMAIL_HISTORY_CURSOR_INVALID is an operator cursor escalation, NOT a member re-auth -> DEGRADED.
        self._mb(last_successful_sync=timezone.now() - datetime.timedelta(minutes=30),
                 last_error_code="GMAIL_HISTORY_CURSOR_INVALID", consecutive_failures=3)
        item = self.c.get("/api/broker-intelligence/mailboxes/status/").json()["mailboxes"][0]
        self.assertEqual(item["state"], "DEGRADED")
        self.assertFalse(item["reconnect_required"])

    def test_error_status_is_reconnect_required(self):
        self._mb(status=ConnectedMailbox.Status.ERROR, last_successful_sync=timezone.now())
        item = self.c.get("/api/broker-intelligence/mailboxes/status/").json()["mailboxes"][0]
        self.assertEqual(item["state"], "RECONNECT_REQUIRED")

    def test_transient_failure_without_reauth_is_degraded(self):
        self._mb(last_successful_sync=timezone.now() - datetime.timedelta(minutes=30),
                 last_error_code="GMAIL_HTTP_5XX", consecutive_failures=2)
        item = self.c.get("/api/broker-intelligence/mailboxes/status/").json()["mailboxes"][0]
        self.assertEqual(item["state"], "DEGRADED")
        self.assertFalse(item["reconnect_required"])

    # --- §5 connected / verified-binding semantics ----------------------------------------------------------------
    def test_connected_false_when_only_revoked(self):
        self._mb(status=ConnectedMailbox.Status.REVOKED, credential_ref="")
        data = self.c.get("/api/broker-intelligence/mailboxes/status/").json()
        self.assertFalse(data["connected"])            # a revoked-only member is NOT connected...
        self.assertEqual(len(data["mailboxes"]), 1)    # ...though the dead mailbox is still listed truthfully

    def test_connected_true_with_one_connected(self):
        self._mb()
        self.assertTrue(self.c.get("/api/broker-intelligence/mailboxes/status/").json()["connected"])

    def test_binding_to_another_members_account_not_counted(self):
        mb = self._mb(last_successful_sync=timezone.now())
        other = User.objects.create_user(username="sr2", email="sr2@x.invalid", password="x")
        foreign_acct = _acct(user=other)
        # a VERIFIED identity (owned by THIS member) but bound to ANOTHER member's trading account must NOT count
        BrokerEmailIdentity.objects.create(email="owner@googlemail.com", broker_name="TradersWay", user=self.u,
                                           trading_account=foreign_acct, status=BrokerEmailIdentity.Status.VERIFIED)
        item = self.c.get("/api/broker-intelligence/mailboxes/status/").json()["mailboxes"][0]
        self.assertFalse(item["has_verified_broker_bindings"])

    def test_pending_identity_does_not_count_as_verified_binding(self):
        mb = self._mb(last_successful_sync=timezone.now())
        BrokerEmailIdentity.objects.create(email="owner@googlemail.com", broker_name="TradersWay", user=self.u,
                                           trading_account=_acct(user=self.u),
                                           status=BrokerEmailIdentity.Status.PENDING)   # not VERIFIED
        item = self.c.get("/api/broker-intelligence/mailboxes/status/").json()["mailboxes"][0]
        self.assertFalse(item["has_verified_broker_bindings"])

    # --- adversarial ----------------------------------------------------------------------------------------------
    def test_status_dark_by_own_flag(self):
        self._mb()
        with override_settings(BROKER_MAILBOX_STATUS_ENABLED="0"):
            self.assertEqual(self.c.get("/api/broker-intelligence/mailboxes/status/").status_code, 404)

    def test_cross_member_mailbox_never_leaks(self):
        self._mb(pmid="mine@gmail.com")
        other = User.objects.create_user(username="sr3", email="sr3@x.invalid", password="x")
        self._mb(user=other, pmid="theirs@gmail.com")
        body = self.c.get("/api/broker-intelligence/mailboxes/status/").content.decode()
        self.assertNotIn("theirs", body)


@override_settings(BROKER_MAILBOX_STATUS_ENABLED="1")
class MailboxStatusPhysicalSchema0010(TransactionTestCase):
    """§505.4 — PHYSICAL schema-0010 certification (NOT mocked introspection). Migrate broker_intelligence DOWN to
    migration 0010 (the production schema) so the migration-0011 health columns genuinely DO NOT EXIST, prove
    r1_columns_present() reports False from REAL DB introspection, and the owner-scoped status endpoint still returns
    200 without referencing a non-existent column. Restores the schema to 0011 afterwards regardless of outcome.

    A TransactionTestCase is required (schema DDL cannot run inside the single outer transaction of a TestCase). The
    down→up is done via the real migration (not an ad-hoc schema_editor DROP), so the schema is fully restored for
    subsequent tests — avoiding the shared-DB corruption that an un-restored DDL drop caused in the R1 incident."""
    _APP = "broker_intelligence"
    _V0010 = "0010_brokerprofile_supported_categories_provenance"
    _V0011 = "0011_connectedmailbox_consecutive_failures_and_more"

    def _migrate(self, target):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        MigrationExecutor(connection).migrate([(self._APP, target)])

    def setUp(self):
        self._migrate(self._V0010)                 # physically drop the 0011 health columns (prod schema 0010)
        self.addCleanup(self._migrate, self._V0011)  # always restore, even if the test fails

    def test_status_endpoint_certified_on_physical_schema_0010(self):
        from django.db import connection
        from broker_intelligence.health import r1_columns_present
        self.assertFalse(r1_columns_present())     # REAL introspection: the 0011 columns are physically absent
        u = User.objects.create_user(username="phys", email="phys@x.invalid", password="x")
        c = APIClient(); c.force_authenticate(u)
        # Insert the mailbox via RAW SQL using ONLY the 0010 columns — an ORM .create() would reference the absent
        # 0011 columns and fail, which is exactly the production hazard the endpoint's projection guards against.
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO broker_intelligence_connectedmailbox "
                "(user_id, provider, provider_mailbox_id, primary_email, status, credential_ref, scopes, "
                " cursor_state, last_successful_sync, created_at, updated_at) "
                "VALUES (%s,'GMAIL','owner@gmail.com','owner@gmail.com','CONNECTED','cr','', '100', "
                " NOW() - INTERVAL '100 days', NOW(), NOW())",
                [u.id])
        r = c.get("/api/broker-intelligence/mailboxes/status/")
        self.assertEqual(r.status_code, 200, r.content)   # no ProgrammingError over a missing column
        item = r.json()["mailboxes"][0]
        self.assertEqual(item["state"], "DEGRADED")       # staleness classifies; no invented OAuth diagnosis on 0010
        self.assertFalse(item["reconnect_required"])
