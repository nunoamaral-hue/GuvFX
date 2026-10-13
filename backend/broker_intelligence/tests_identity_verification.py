"""WP7 — identity VERIFICATION + BINDING: decision matrix, cross-user firewall, idempotency/preservation, canonical
equivalence, multi-broker/account/mailbox, append-only audit, and the DARK owner-scoped API.

The capability must NEVER auto-verify and NEVER attribute across members. These tests pin:
  * composed proof required (mailbox control + GuvFX account ownership + same owner + broker match);
  * email ownership ALONE is not enough (account must be owned too);
  * cross-user + account-conflict refusals (certified bindings preserved, never overwritten);
  * gmail/googlemail/dots/+tag canonical equivalence between the mailbox and the registered address;
  * multi-broker / multi-account / multi-mailbox all supported;
  * every verify AND every refusal is audited (append-only);
  * the HTTP surface is DARK (404) until armed and strictly owner-scoped.
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from broker_intelligence import identity_verification as iv
from broker_intelligence.models import (BrokerEmailIdentity, BrokerEmailIdentityAudit, ConnectedMailbox)
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "73%04d" % _n


def _user():
    s = _uniq()
    return User.objects.create_user(username="iv%s" % s, email="%s@member.invalid" % s, password="x")


def _acct(user, broker="TradersWay", number=None):
    return TradingAccount.objects.create(user=user, name="A", broker_name=broker,
                                         account_number=number or _uniq(), is_demo=True, is_active=True)


def _mailbox(user, email, status=ConnectedMailbox.Status.CONNECTED, pid=None):
    return ConnectedMailbox.objects.create(user=user, provider=ConnectedMailbox.Provider.GMAIL,
                                           provider_mailbox_id=pid or ("pid-" + _uniq()),
                                           primary_email=email, status=status)


class EvaluateDecisionTests(TestCase):
    """The pure decision — every branch of the composed proof."""

    def test_happy_path_verifies_and_binds(self):
        u = _user()
        a = _acct(u, "TradersWay")
        _mailbox(u, "nrfda1111@googlemail.com")
        idn = iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                 trading_account=a)
        self.assertEqual(idn.status, BrokerEmailIdentity.Status.VERIFIED)
        self.assertEqual(idn.user_id, u.id)
        self.assertEqual(idn.trading_account_id, a.id)
        self.assertIsNotNone(idn.connected_mailbox_id)        # receiving mailbox linked
        au = BrokerEmailIdentityAudit.objects.get(identity=idn, action="VERIFY")
        self.assertEqual(au.outcome, "ok")
        self.assertTrue(au.proofs["mailbox_control"] and au.proofs["account_owned"] and au.proofs["same_owner"])

    def test_email_ownership_alone_is_not_account_ownership(self):
        # The claimant controls the mailbox but the account belongs to ANOTHER member → refuse (§2).
        u = _user()
        other = _user()
        a_other = _acct(other, "TradersWay")
        _mailbox(u, "nrfda1111@googlemail.com")
        with self.assertRaises(iv.VerificationRefused) as ctx:
            iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="TradersWay",
                               trading_account=a_other)
        self.assertEqual(ctx.exception.code, "account_not_owned")
        self.assertFalse(BrokerEmailIdentity.objects.filter(status="VERIFIED").exists())

    def test_no_connected_mailbox_refuses(self):
        u = _user()
        a = _acct(u, "TradersWay")
        # Mailbox exists but is only PENDING (consent not completed) → not control proof.
        _mailbox(u, "nrfda1111@googlemail.com", status=ConnectedMailbox.Status.PENDING)
        with self.assertRaises(iv.VerificationRefused) as ctx:
            iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="TradersWay", trading_account=a)
        self.assertEqual(ctx.exception.code, "mailbox_not_connected")

    def test_broker_mismatch_refuses(self):
        u = _user()
        a = _acct(u, "TradersWay")
        _mailbox(u, "nrfda1111@googlemail.com")
        with self.assertRaises(iv.VerificationRefused) as ctx:
            iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="IS6 Technologies",
                               trading_account=a)
        self.assertEqual(ctx.exception.code, "broker_mismatch")

    def test_invalid_input_refuses(self):
        u = _user()
        a = _acct(u, "TradersWay")
        with self.assertRaises(iv.VerificationRefused) as ctx:
            iv.verify_identity(user=u, email="", broker_name="TradersWay", trading_account=a)
        self.assertEqual(ctx.exception.code, "invalid_input")


class CanonicalEquivalenceTests(TestCase):
    def test_dotted_plus_googlemail_matches_gmail_mailbox(self):
        # Identity registered in one Google spelling; mailbox connected in an equivalent one → canonical match.
        u = _user()
        a = _acct(u, "TradersWay")
        _mailbox(u, "nrfda1111@gmail.com")
        idn = iv.verify_identity(user=u, email="n.rfda1111+tw@googlemail.com", broker_name="TradersWay",
                                 trading_account=a)
        self.assertEqual(idn.status, BrokerEmailIdentity.Status.VERIFIED)

    def test_non_google_domain_requires_exact_mailbox(self):
        u = _user()
        a = _acct(u, "TradersWay")
        _mailbox(u, "trader@fastmail.com")
        # A different non-Google address does NOT canonicalise together → refuse.
        with self.assertRaises(iv.VerificationRefused) as ctx:
            iv.verify_identity(user=u, email="trader2@fastmail.com", broker_name="TradersWay", trading_account=a)
        self.assertEqual(ctx.exception.code, "mailbox_not_connected")


class CrossUserFirewallTests(TestCase):
    def test_same_mailbox_different_member_refused(self):
        # One Gmail mailbox = one human. userA verifies it; userB (who somehow presents a canonically-equal
        # connected mailbox + owns an account) must be refused cross-user.
        ua, ub = _user(), _user()
        aa, ab = _acct(ua, "TradersWay"), _acct(ub, "TradersWay")
        _mailbox(ua, "nrfda1111@googlemail.com", pid="google-A")
        iv.verify_identity(user=ua, email="nrfda1111@googlemail.com", broker_name="TradersWay", trading_account=aa)
        _mailbox(ub, "n.rfda1111@gmail.com", pid="google-B")   # canonically equal, different mailbox row
        with self.assertRaises(iv.VerificationRefused) as ctx:
            iv.verify_identity(user=ub, email="nrfda1111@gmail.com", broker_name="TradersWay", trading_account=ab)
        self.assertEqual(ctx.exception.code, "cross_user_conflict")
        # The refused attempt is AUDITED even though no binding was created for ub.
        self.assertTrue(BrokerEmailIdentityAudit.objects.filter(action="REJECT", subject_user=ub,
                                                                outcome="cross_user_conflict").exists())

    def test_api_cannot_bind_another_members_account(self):
        ua, ub = _user(), _user()
        a_other = _acct(ub, "TradersWay")
        _mailbox(ua, "nrfda1111@googlemail.com")
        c = APIClient(); c.force_authenticate(ua)
        with override_settings(BROKER_IDENTITY_VERIFICATION_ENABLED=True):
            r = c.post("/api/broker-intelligence/identities/verify/",
                       {"email": "nrfda1111@googlemail.com", "broker": "TradersWay",
                        "trading_account_id": a_other.id}, format="json")
        self.assertEqual(r.status_code, 422)                        # owner-scoped lookup → not-yours
        self.assertEqual(r.json()["error"], "account_not_owned")
        # The refused CROSS-USER bind (the view short-circuits before verify_identity) MUST leave a durable audit.
        au = BrokerEmailIdentityAudit.objects.filter(action="REJECT", subject_user=ua,
                                                     outcome="account_not_owned").latest("id")
        self.assertEqual(au.proofs.get("attempted_trading_account_id"), a_other.id)
        self.assertIsNone(au.trading_account_id)                    # A's account never loaded/bound
        # No binding was created for either member.
        self.assertFalse(BrokerEmailIdentity.objects.filter(status="VERIFIED").exists())


class IdempotencyAndPreservationTests(TestCase):
    def test_reverify_same_owner_account_is_confirm_noop(self):
        u = _user()
        a = _acct(u, "TradersWay")
        _mailbox(u, "nrfda1111@googlemail.com")
        idn1 = iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                  trading_account=a)
        idn2 = iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                  trading_account=a)
        self.assertEqual(idn1.id, idn2.id)
        self.assertEqual(BrokerEmailIdentity.objects.count(), 1)
        self.assertTrue(BrokerEmailIdentityAudit.objects.filter(action="CONFIRM").exists())

    def test_confirm_fills_missing_mailbox_without_changing_binding(self):
        # Mirrors the prod certified row (VERIFIED, connected_mailbox=None): re-verify ADDS the mailbox link only.
        u = _user()
        a = _acct(u, "TradersWay")
        idn = BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                                 user=u, trading_account=a,
                                                 status=BrokerEmailIdentity.Status.VERIFIED)
        self.assertIsNone(idn.connected_mailbox_id)
        mb = _mailbox(u, "nrfda1111@googlemail.com")
        out = iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                 trading_account=a)
        out.refresh_from_db()
        self.assertEqual(out.id, idn.id)
        self.assertEqual(out.status, BrokerEmailIdentity.Status.VERIFIED)   # unchanged
        self.assertEqual(out.trading_account_id, a.id)                      # unchanged
        self.assertEqual(out.connected_mailbox_id, mb.id)                   # additively linked
        au = BrokerEmailIdentityAudit.objects.filter(action="CONFIRM").latest("id")
        self.assertEqual(au.outcome, "confirmed_mailbox_filled")

    def test_cannot_repoint_certified_binding_to_different_account(self):
        u = _user()
        a1, a2 = _acct(u, "TradersWay"), _acct(u, "TradersWay")
        _mailbox(u, "nrfda1111@googlemail.com")
        iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="TradersWay", trading_account=a1)
        with self.assertRaises(iv.VerificationRefused) as ctx:
            iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="TradersWay",
                               trading_account=a2)
        self.assertEqual(ctx.exception.code, "account_conflict")
        self.assertEqual(BrokerEmailIdentity.objects.get(email="nrfda1111@googlemail.com",
                                                         broker_name="TradersWay").trading_account_id, a1.id)

    def test_verified_row_with_null_user_is_not_overwritten(self):
        # A VERIFIED row whose user is NULL (anomalous) is skipped by evaluate's owner-scoped conflict scan; the
        # under-lock preservation re-check must still refuse to overwrite it (never silently re-point a VERIFIED row).
        u = _user()
        a = _acct(u, "TradersWay")
        _mailbox(u, "nrfda1111@googlemail.com")
        BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                           user=None, status=BrokerEmailIdentity.Status.VERIFIED)
        with self.assertRaises(iv.VerificationRefused) as ctx:
            iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="TradersWay", trading_account=a)
        self.assertEqual(ctx.exception.code, "cross_user_conflict")
        row = BrokerEmailIdentity.objects.get(email="nrfda1111@googlemail.com", broker_name="TradersWay")
        self.assertIsNone(row.user_id)                       # untouched
        self.assertTrue(BrokerEmailIdentityAudit.objects.filter(action="REJECT",
                                                                outcome="cross_user_conflict").exists())

    def test_retired_identity_is_not_reanimated(self):
        u = _user()
        a = _acct(u, "TradersWay")
        _mailbox(u, "nrfda1111@googlemail.com")
        BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                           user=u, trading_account=a, status=BrokerEmailIdentity.Status.RETIRED)
        with self.assertRaises(iv.VerificationRefused) as ctx:
            iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="TradersWay", trading_account=a)
        self.assertEqual(ctx.exception.code, "retired_identity")
        self.assertEqual(BrokerEmailIdentity.objects.get(email="nrfda1111@googlemail.com",
                                                         broker_name="TradersWay").status,
                         BrokerEmailIdentity.Status.RETIRED)
        self.assertTrue(BrokerEmailIdentityAudit.objects.filter(action="REJECT", outcome="retired_identity").exists())

    def test_pending_backfill_row_is_promoted_not_duplicated(self):
        # The real path: backfill created a PENDING, unbound (email, broker) row; verification promotes IT.
        u = _user()
        a = _acct(u, "TradersWay")
        BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                           status=BrokerEmailIdentity.Status.PENDING)   # unbound
        _mailbox(u, "nrfda1111@googlemail.com")
        idn = iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                 trading_account=a)
        self.assertEqual(idn.status, BrokerEmailIdentity.Status.VERIFIED)
        self.assertEqual(BrokerEmailIdentity.objects.filter(email="nrfda1111@googlemail.com",
                                                            broker_name="TradersWay").count(), 1)


class MultiBrokerAccountMailboxTests(TestCase):
    def test_one_mailbox_two_brokers_two_accounts(self):
        u = _user()
        a_tw = _acct(u, "TradersWay")
        a_is6 = _acct(u, "IS6 Technologies")
        _mailbox(u, "nrfda1111@googlemail.com")                         # ONE mailbox receives both brokers' mail
        tw = iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                trading_account=a_tw)
        is6 = iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="IS6 Technologies",
                                 trading_account=a_is6)
        self.assertEqual(tw.status, BrokerEmailIdentity.Status.VERIFIED)
        self.assertEqual(is6.status, BrokerEmailIdentity.Status.VERIFIED)
        self.assertNotEqual(tw.id, is6.id)                              # distinct (email,broker) rows, both verified

    def test_two_mailboxes_identity_binds_the_matching_one(self):
        u = _user()
        a = _acct(u, "IS6 Technologies")
        _mailbox(u, "personal@gmail.com", pid="g-personal")
        mb2 = _mailbox(u, "broker-inbox@googlemail.com", pid="g-broker")
        idn = iv.verify_identity(user=u, email="broker-inbox@gmail.com", broker_name="IS6 Technologies",
                                 trading_account=a)
        self.assertEqual(idn.connected_mailbox_id, mb2.id)


class AuditAppendOnlyTests(TestCase):
    def test_audit_row_is_never_deleted(self):
        u = _user()
        a = _acct(u, "TradersWay")
        _mailbox(u, "nrfda1111@googlemail.com")
        iv.verify_identity(user=u, email="nrfda1111@googlemail.com", broker_name="TradersWay", trading_account=a)
        au = BrokerEmailIdentityAudit.objects.latest("id")
        with self.assertRaises(ValidationError):
            au.delete()


@override_settings(BROKER_IDENTITY_VERIFICATION_ENABLED=True)
class IdentityApiArmedTests(TestCase):
    def setUp(self):
        self.u = _user()
        self.a = _acct(self.u, "TradersWay")
        _mailbox(self.u, "nrfda1111@googlemail.com")
        self.c = APIClient(); self.c.force_authenticate(self.u)

    def test_list_is_owner_scoped(self):
        other = _user()
        ao = _acct(other, "TradersWay")
        _mailbox(other, "someone@gmail.com")
        iv.verify_identity(user=other, email="someone@gmail.com", broker_name="TradersWay", trading_account=ao)
        r = self.c.get("/api/broker-intelligence/identities/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["identities"], [])                    # none of MY identities yet
        self.assertEqual(len(r.json()["mailboxes"]), 1)                 # my one connected mailbox (masked)
        self.assertNotIn("@gmail.com", r.json()["mailboxes"][0]["email_masked"].split("…")[0])

    def test_verify_happy_path(self):
        r = self.c.post("/api/broker-intelligence/identities/verify/",
                        {"email": "nrfda1111@googlemail.com", "broker": "TradersWay",
                         "trading_account_id": self.a.id}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["status"], "VERIFIED")
        # Now listed as the member's own VERIFIED identity (masked, no raw email).
        lst = self.c.get("/api/broker-intelligence/identities/").json()["identities"]
        self.assertEqual(len(lst), 1)
        self.assertTrue(lst[0]["verified"])
        self.assertNotIn("nrfda1111", lst[0]["email_masked"])

    def test_verify_invalid_input_400(self):
        r = self.c.post("/api/broker-intelligence/identities/verify/",
                        {"broker": "TradersWay"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_input")
        # Every refusal is audited, including malformed input.
        self.assertTrue(BrokerEmailIdentityAudit.objects.filter(action="REJECT", subject_user=self.u,
                                                                outcome="invalid_input").exists())

    def test_verify_broker_mismatch_422(self):
        r = self.c.post("/api/broker-intelligence/identities/verify/",
                        {"email": "nrfda1111@googlemail.com", "broker": "IS6 Technologies",
                         "trading_account_id": self.a.id}, format="json")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"], "broker_mismatch")


class IdentityApiDarkTests(TestCase):
    def test_endpoints_404_while_dark(self):
        u = _user()
        c = APIClient(); c.force_authenticate(u)
        self.assertEqual(c.get("/api/broker-intelligence/identities/").status_code, 404)
        self.assertEqual(c.post("/api/broker-intelligence/identities/verify/",
                                {"email": "x@gmail.com", "broker": "TradersWay", "trading_account_id": 1},
                                format="json").status_code, 404)

    def test_requires_auth(self):
        c = APIClient()
        with override_settings(BROKER_IDENTITY_VERIFICATION_ENABLED=True):
            self.assertIn(c.get("/api/broker-intelligence/identities/").status_code, (401, 403))
