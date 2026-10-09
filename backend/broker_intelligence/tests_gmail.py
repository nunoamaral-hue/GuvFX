"""WP3b — Gmail OAuth flow + encrypted credential store + GmailMailSource (synthetic; no network, no real consent).

Security-critical coverage: the credential store encrypts at rest (no plaintext token on disk), is fail-closed when
the key is missing, and refuses tampered ciphertext; the OAuth flow grants ONLY gmail.readonly and fails closed on a
non-200; GmailMailSource maps raw RFC822 to normalised MailMessages and advances its cursor only on commit.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import tempfile

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from broker_intelligence import credential_store as CS
from broker_intelligence import gmail_oauth as OAUTH
from broker_intelligence import gmail_source as GS
from broker_intelligence.models import ConnectedMailbox

User = get_user_model()
_KEY = Fernet.generate_key().decode()
_TMP = tempfile.mkdtemp(prefix="guvfx_cred_test_")

CRED_SETTINGS = dict(BROKER_INTELLIGENCE_CREDENTIAL_KEY=_KEY, BROKER_INTELLIGENCE_CREDENTIAL_ROOT=_TMP)

SYNTHETIC_RAW = (
    b"From: TradersWay <no-reply@tradersway.com>\r\n"
    b"To: ba0123deadbeef@accounts.guvfx.com\r\n"
    b"Subject: Your withdrawal request\r\n"
    b"Date: Wed, 08 Oct 2026 10:00:00 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Your withdrawal request of 200.00 USD (ref 4112808) has been received.\r\n"
)


@override_settings(**CRED_SETTINGS)
class CredentialStoreTests(SimpleTestCase):
    def test_roundtrip_and_ciphertext_is_not_plaintext(self):
        tok = {"access_token": "SECRET-ACCESS-abc123", "refresh_token": "SECRET-REFRESH-xyz", "expiry": "", "scope": OAUTH.GMAIL_READONLY_SCOPE}
        ref = CS.store_token(tok)
        self.assertTrue(ref.startswith("cr"))
        # the raw file on disk must NOT contain the plaintext token anywhere
        raw = (open(os.path.join(_TMP, ref + ".enc"), "rb").read())
        self.assertNotIn(b"SECRET-ACCESS-abc123", raw)
        self.assertNotIn(b"SECRET-REFRESH-xyz", raw)
        self.assertEqual(CS.load_token(ref), tok)

    def test_missing_ref_is_none_and_delete(self):
        self.assertIsNone(CS.load_token("crdoesnotexist" + "0" * 20))
        ref = CS.store_token({"access_token": "a"})
        self.assertTrue(CS.delete_token(ref))
        self.assertIsNone(CS.load_token(ref))

    def test_tampered_ciphertext_fails_closed(self):
        ref = CS.store_token({"access_token": "a"})
        p = os.path.join(_TMP, ref + ".enc")
        open(p, "wb").write(b"not-a-valid-fernet-token")
        with self.assertRaises(CS.CredentialStoreError):
            CS.load_token(ref)

    def test_overwrite_in_place_same_ref(self):
        ref = CS.store_token({"access_token": "one"})
        self.assertEqual(CS.store_token({"access_token": "two"}, credential_ref=ref), ref)
        self.assertEqual(CS.load_token(ref)["access_token"], "two")

    def test_invalid_ref_rejected(self):
        for bad in ("../escape", "a/b", "a\\b"):
            with self.assertRaises(CS.CredentialStoreError):
                CS.load_token(bad)

    def test_file_and_root_perms_are_owner_only(self):
        import stat
        ref = CS.store_token({"access_token": "perm-check"})
        file_mode = stat.S_IMODE(os.stat(os.path.join(_TMP, ref + ".enc")).st_mode)
        root_mode = stat.S_IMODE(os.stat(_TMP).st_mode)
        self.assertEqual(file_mode, 0o600, "credential file must be owner-only")
        self.assertEqual(root_mode, 0o700, "credential root must be owner-only")


class CredentialStoreFailClosedTests(SimpleTestCase):
    @override_settings(BROKER_INTELLIGENCE_CREDENTIAL_KEY="", BROKER_INTELLIGENCE_CREDENTIAL_ROOT=_TMP)
    def test_missing_key_refuses_store_and_load(self):
        os.environ.pop("BROKER_INTELLIGENCE_CREDENTIAL_KEY", None)
        # store with no key -> refuses (NEVER writes plaintext)
        with self.assertRaises(CS.CredentialStoreError):
            CS.store_token({"access_token": "x"})
        # a non-existent ref -> None (nothing to decrypt; no key needed, no plaintext exposed)
        self.assertIsNone(CS.load_token("cr" + "0" * 32))
        # but an EXISTING encrypted file with no key -> refuses (never returns ciphertext / fails open)
        p = os.path.join(_TMP, "crexists" + "0" * 24 + ".enc")
        open(p, "wb").write(b"ciphertext-bytes")
        with self.assertRaises(CS.CredentialStoreError):
            CS.load_token("crexists" + "0" * 24)


class GmailOAuthTests(SimpleTestCase):
    def test_authorization_url_is_readonly_and_offline(self):
        url = OAUTH.build_authorization_url(client_id="CID", redirect_uri="https://x/cb", state="S")
        self.assertIn("scope=" + OAUTH.GMAIL_READONLY_SCOPE.replace(":", "%3A").replace("/", "%2F"), url)
        self.assertIn("access_type=offline", url)
        self.assertIn("prompt=consent", url)
        self.assertIn("client_id=CID", url)
        # NEVER a write/send/modify scope
        for forbidden in ("gmail.modify", "gmail.send", "gmail.compose", "mail.google.com"):
            self.assertNotIn(forbidden, url)

    def test_exchange_code_builds_token(self):
        def fake_post(url, form):
            self.assertEqual(form["grant_type"], "authorization_code")
            return 200, json.dumps({"access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
                                    "scope": OAUTH.GMAIL_READONLY_SCOPE, "token_type": "Bearer"})
        now = datetime.datetime(2026, 10, 8, tzinfo=datetime.timezone.utc)
        tok = OAUTH.exchange_code(code="C", client_id="CID", client_secret="SEC", redirect_uri="https://x/cb",
                                  http_post=fake_post, now=now)
        self.assertEqual(tok["access_token"], "AT")
        self.assertEqual(tok["refresh_token"], "RT")
        self.assertEqual(tok["expiry"], (now + datetime.timedelta(seconds=3600)).isoformat())

    def test_exchange_non_200_fails_closed(self):
        with self.assertRaises(OAUTH.GmailOAuthError):
            OAUTH.exchange_code(code="C", client_id="CID", client_secret="SEC", redirect_uri="https://x/cb",
                                http_post=lambda u, f: (400, '{"error":"invalid_grant"}'))

    def test_refresh_carries_forward_refresh_token(self):
        tok = OAUTH.refresh_access_token(refresh_token="RT-keep", client_id="CID", client_secret="SEC",
                                         http_post=lambda u, f: (200, json.dumps({"access_token": "AT2", "expires_in": 3600})))
        self.assertEqual(tok["access_token"], "AT2")
        self.assertEqual(tok["refresh_token"], "RT-keep")   # reused when the refresh grant omits it

    def test_missing_access_token_fails_closed(self):
        with self.assertRaises(OAUTH.GmailOAuthError):
            OAUTH.exchange_code(code="C", client_id="C", client_secret="S", redirect_uri="https://x/cb",
                                http_post=lambda u, f: (200, '{"token_type":"Bearer"}'))

    def test_rejects_write_capable_scope_grant(self):
        # A grant that comes back with a mail WRITE scope must be refused (never store a write-capable token).
        for bad in ("https://www.googleapis.com/auth/gmail.modify", "https://mail.google.com/",
                    "https://www.googleapis.com/auth/gmail.send"):
            with self.assertRaises(OAUTH.GmailOAuthError):
                OAUTH.exchange_code(code="C", client_id="C", client_secret="S", redirect_uri="https://x/cb",
                                    http_post=lambda u, f, b=bad: (200, json.dumps({"access_token": "AT", "expires_in": 3600, "scope": b})))

    def test_authorization_url_omits_include_granted_scopes(self):
        url = OAUTH.build_authorization_url(client_id="C", redirect_uri="https://x/cb", state="S")
        self.assertNotIn("include_granted_scopes", url)   # request the MINIMAL grant, never widen via prior grants


class ResolverOwnerScopingTests(TestCase):
    """SECURITY (HIGH): recipient headers are attacker-controllable; resolution from a known mailbox MUST be scoped
    to that mailbox's owner so a spoofed To:/X-Original-To carrying another member's opaque alias cannot
    cross-attribute a withdrawal to that member."""

    def test_spoofed_recipient_cannot_cross_attribute(self):
        from broker_intelligence.services import mint_alias_for_account
        from broker_intelligence.resolver import resolve
        from trading.models import TradingAccount
        uV = User.objects.create_user(username="victimW", email="vw@x.invalid", password="x")
        aV = TradingAccount.objects.create(user=uV, name="V", account_number="VW1", is_demo=True, broker_name="B")
        alV = mint_alias_for_account(aV)                 # ACTIVE bound alias owned by the victim
        uU = User.objects.create_user(username="mailboxownerW", email="uw@x.invalid", password="x")
        addr = alV.address()
        # global (no owner) still resolves to V — unchanged legacy behaviour
        self.assertEqual(resolve([addr])[1].id, aV.id)
        # owner-scoped to the mailbox owner U: the victim's alias is NOT matched -> NO cross-attribution
        self.assertEqual(resolve([addr], owner_user=uU), (None, None))
        # owner-scoped to V: V's own alias resolves (legitimate)
        self.assertEqual(resolve([addr], owner_user=uV)[1].id, aV.id)


class ParseRfc822Tests(SimpleTestCase):
    def test_extracts_fields_from_raw(self):
        f = GS.parse_rfc822(SYNTHETIC_RAW)
        self.assertEqual(f["from_address"], "no-reply@tradersway.com")
        self.assertIn("ba0123deadbeef@accounts.guvfx.com", f["to_addresses"])
        self.assertEqual(f["subject"], "Your withdrawal request")
        self.assertIn("200.00 USD", f["body"])
        self.assertEqual(f["received_at"].year, 2026)


class _FakeGmailClient:
    def __init__(self, messages, new_cursor):
        self._messages, self._new_cursor = messages, new_cursor
        self.seen_cursor = None

    def list_new_messages(self, cursor_state):
        self.seen_cursor = cursor_state
        return self._messages, self._new_cursor


class GmailMailSourceTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="gm", email="gm@x.invalid", password="x")
        self.mb = ConnectedMailbox.objects.create(
            user=self.u, provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id="gmail-1",
            primary_email="nrfda1111@googlemail.com", credential_ref="cr" + "0" * 32, cursor_state="100")

    def test_fetch_maps_messages_and_commit_advances_cursor(self):
        f = GS.parse_rfc822(SYNTHETIC_RAW)
        msgs = [{"provider_message_id": "m1", "raw_bytes": SYNTHETIC_RAW, **f}]
        client = _FakeGmailClient(msgs, new_cursor="205")
        src = GS.GmailMailSource(self.mb, client)
        out = list(src.fetch())
        self.assertEqual(client.seen_cursor, "100")                 # used the stored cursor
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].subject, "Your withdrawal request")
        self.assertIn("ba0123deadbeef@accounts.guvfx.com", out[0].to_addresses)
        self.assertEqual(out[0].provider_message_id, "m1")
        # cursor not advanced until commit (crash-safe: re-fetch is idempotent)
        self.mb.refresh_from_db()
        self.assertEqual(self.mb.cursor_state, "100")
        src.commit_cursor()
        self.mb.refresh_from_db()
        self.assertEqual(self.mb.cursor_state, "205")
        self.assertIsNotNone(self.mb.last_successful_sync)


@override_settings(**CRED_SETTINGS)
class GmailApiClientTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="ga", email="ga@x.invalid", password="x")
        tok = {"access_token": "ACCESS-1", "refresh_token": "REF-1",
               "expiry": (timezone.now() + datetime.timedelta(hours=1)).isoformat(),
               "scope": OAUTH.GMAIL_READONLY_SCOPE}
        self.ref = CS.store_token(tok)
        self.mb = ConnectedMailbox.objects.create(
            user=self.u, provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id="gmail-2",
            credential_ref=self.ref, cursor_state="")

    def test_lists_and_decodes_raw_with_bearer_token(self):
        raw_b64 = base64.urlsafe_b64encode(SYNTHETIC_RAW).decode().rstrip("=")
        seen_auth = []

        def http_get(url, headers):
            seen_auth.append(headers.get("Authorization"))
            if url.endswith("/messages?maxResults=50"):
                return 200, json.dumps({"messages": [{"id": "m1"}]})
            if "/messages/m1" in url:
                return 200, json.dumps({"id": "m1", "raw": raw_b64})
            if url.endswith("/profile"):
                return 200, json.dumps({"historyId": "999"})
            return 404, "{}"

        client = GS.GmailApiClient(self.mb, client_id="CID", client_secret="SEC", http_get=http_get)
        msgs, cursor = client.list_new_messages("")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["subject"], "Your withdrawal request")
        self.assertEqual(msgs[0]["provider_message_id"], "m1")
        self.assertEqual(cursor, "999")
        self.assertTrue(all(a == "Bearer ACCESS-1" for a in seen_auth))   # attached the stored access token

    def test_incremental_uses_history_since_cursor(self):
        # With a cursor, the client must use users.history.list (not re-list recent 50) and advance to the history
        # historyId — so no message is ever skipped by the cursor jumping to 'now'.
        raw_b64 = base64.urlsafe_b64encode(SYNTHETIC_RAW).decode().rstrip("=")

        def http_get(url, headers):
            if "/history?startHistoryId=100" in url:
                return 200, json.dumps({"historyId": "150",
                                        "history": [{"messagesAdded": [{"message": {"id": "mX"}}]}]})
            if "/messages/mX" in url:
                return 200, json.dumps({"id": "mX", "raw": raw_b64})
            return 404, "{}"

        client = GS.GmailApiClient(self.mb, client_id="CID", client_secret="SEC", http_get=http_get)
        msgs, cursor = client.list_new_messages("100")
        self.assertEqual([m["provider_message_id"] for m in msgs], ["mX"])
        self.assertEqual(cursor, "150")                 # advanced to the history historyId, never jumped to now

    def test_oversized_message_is_skipped(self):
        big = base64.urlsafe_b64encode(b"x" * (GS.GmailApiClient.MAX_RAW_BYTES + 1000)).decode().rstrip("=")

        def http_get(url, headers):
            if url.endswith("/messages?maxResults=50"):
                return 200, json.dumps({"messages": [{"id": "big"}]})
            if "/messages/big" in url:
                return 200, json.dumps({"id": "big", "raw": big})
            if url.endswith("/profile"):
                return 200, json.dumps({"historyId": "7"})
            return 404, "{}"

        client = GS.GmailApiClient(self.mb, client_id="CID", client_secret="SEC", http_get=http_get)
        msgs, cursor = client.list_new_messages("")      # initial sync
        self.assertEqual(msgs, [])                       # oversized message skipped, never buffered/ingested

    def test_expired_token_is_refreshed_readonly(self):
        # Store an EXPIRED token; the client must refresh (read-only) before calling the API.
        CS.store_token({"access_token": "OLD", "refresh_token": "REF-1",
                        "expiry": (timezone.now() - datetime.timedelta(hours=1)).isoformat()},
                       credential_ref=self.ref)
        import broker_intelligence.gmail_oauth as O
        orig = O.refresh_access_token
        try:
            O.refresh_access_token = lambda **kw: {"access_token": "NEW", "refresh_token": kw["refresh_token"],
                                                   "expiry": (timezone.now() + datetime.timedelta(hours=1)).isoformat(),
                                                   "scope": OAUTH.GMAIL_READONLY_SCOPE}

            def http_get(url, headers):
                self.assertEqual(headers.get("Authorization"), "Bearer NEW")   # used the refreshed token
                if url.endswith("/messages?maxResults=50"):
                    return 200, json.dumps({"messages": []})
                if url.endswith("/profile"):
                    return 200, json.dumps({"historyId": "5"})
                return 404, "{}"

            client = GS.GmailApiClient(self.mb, client_id="CID", client_secret="SEC", http_get=http_get)
            msgs, cursor = client.list_new_messages("")
            self.assertEqual(msgs, [])
            self.assertEqual(cursor, "5")
            self.assertEqual(CS.load_token(self.ref)["access_token"], "NEW")   # refreshed token persisted (same ref)
        finally:
            O.refresh_access_token = orig
